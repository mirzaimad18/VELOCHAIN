"""
Unit tests for the Parle-G Rebalancing Agent.
=============================================
Tests cover all required business rules:
1. Rule 1: Only orders with routing_status == "insufficient_stock" are processed;
   others pass through unchanged.
2. Rule 2: Shortage is calculated accurately (quantity_requested - available stock).
3. Rule 3 & 4: Surplus candidate with the MOST stock is selected.
4. Rule 5: When no warehouse has surplus, marked as "no_surplus_available" and no transfer created.
5. Rule 6: Transfer request created with exact fields and quantity capped at available stock.
6. Rule 7: Warehouse stock levels are updated in real-time (deducted from source, added to destination).
7. Sequential orders: Multiple transfers update stock sequentially and increment transfer IDs.
8. Metadata preservation: All original order fields (priority_tier, priority_score, etc.) are kept intact.
9. End-to-end integration: Pipeline execution from spatial routing output into rebalancing.
"""

import copy
import pytest
from agents.rebalancing_agent import (
    rebalance_orders,
    rebalance_inventory,
    find_best_surplus_warehouse,
    STATUS_TRANSFER_CREATED,
    STATUS_NO_SURPLUS_AVAILABLE,
    TRANSFER_STATUS_CREATED,
    TARGET_ROUTING_STATUS,
)
from agents.spatial_routing_agent import (
    route_orders,
    STATUS_FULFILLED_DIRECTLY,
    STATUS_INSUFFICIENT_STOCK,
    STATUS_NO_WAREHOUSE_FOR_REGION,
)


@pytest.fixture
def sample_warehouses():
    """
    Returns a fresh sample warehouse list for test isolation.
    """
    return [
        {
            "warehouse_id": "WH-WEST-01",
            "region_served": "West",
            "stock_per_sku": {
                "PARLE-G-250G": 1000,
                "PARLE-G-100G": 3000,
            },
        },
        {
            "warehouse_id": "WH-NORTH-01",
            "region_served": "North",
            "stock_per_sku": {
                "PARLE-G-250G": 50,
                "PARLE-G-100G": 200,
            },
        },
        {
            "warehouse_id": "WH-SOUTH-01",
            "region_served": "South",
            "stock_per_sku": {
                "PARLE-G-250G": 500,
                "PARLE-G-800G": 100,
            },
        },
    ]


def test_rule_1_unaffected_orders_passed_through_unchanged(sample_warehouses):
    """
    Rule 1: Orders whose routing_status is NOT 'insufficient_stock'
    must be ignored and passed through unchanged (no rebalancing_status or transfer_id).
    """
    orders = [
        {
            "store_id": "STORE-01",
            "routing_status": STATUS_FULFILLED_DIRECTLY,
            "assigned_warehouse_id": "WH-WEST-01",
            "sku": "PARLE-G-250G",
            "quantity_requested": 100,
        },
        {
            "store_id": "STORE-02",
            "routing_status": STATUS_NO_WAREHOUSE_FOR_REGION,
            "assigned_warehouse_id": None,
            "sku": "PARLE-G-250G",
            "quantity_requested": 100,
        },
    ]

    result = rebalance_orders(orders=orders, warehouses=sample_warehouses)

    assert len(result.orders) == 2
    assert len(result.transfers) == 0

    # Verify both orders passed through without rebalancing modifications
    for order in result.orders:
        assert "rebalancing_status" not in order
        assert "transfer_id" not in order


def test_rule_2_and_6_shortage_calculation_and_transfer_creation(sample_warehouses):
    """
    Rule 2 & Rule 6:
    - WH-NORTH-01 has 50 units of PARLE-G-250G on hand.
    - Order requests 150 units -> shortage = 150 - 50 = 100.
    - Surplus warehouse WH-WEST-01 has 1000 units.
    - Transfer created for 100 units from WH-WEST-01 -> WH-NORTH-01.
    - Order marked as 'transfer_created' with transfer_id 'TR-0001'.
    """
    order = {
        "store_id": "STORE-N1",
        "routing_status": TARGET_ROUTING_STATUS,
        "assigned_warehouse_id": "WH-NORTH-01",
        "sku": "PARLE-G-250G",
        "quantity_requested": 150,
    }

    result = rebalance_orders(orders=[order], warehouses=sample_warehouses)

    assert len(result.orders) == 1
    assert len(result.transfers) == 1

    updated_order = result.orders[0]
    assert updated_order["rebalancing_status"] == STATUS_TRANSFER_CREATED
    assert updated_order["transfer_id"] == "TR-0001"

    transfer = result.transfers[0]
    assert transfer["transfer_id"] == "TR-0001"
    assert transfer["from_warehouse"] == "WH-WEST-01"
    assert transfer["to_warehouse"] == "WH-NORTH-01"
    assert transfer["sku"] == "PARLE-G-250G"
    assert transfer["quantity"] == 100  # Exactly the shortage
    assert transfer["status"] == TRANSFER_STATUS_CREATED


def test_rule_3_and_4_selects_warehouse_with_most_stock(sample_warehouses):
    """
    Rule 3 & Rule 4:
    WH-NORTH-01 needs PARLE-G-250G.
    Both WH-WEST-01 (1000 units) and WH-SOUTH-01 (500 units) carry surplus.
    WH-WEST-01 has the most stock and must be chosen.
    """
    order = {
        "store_id": "STORE-N1",
        "routing_status": TARGET_ROUTING_STATUS,
        "assigned_warehouse_id": "WH-NORTH-01",
        "sku": "PARLE-G-250G",
        "quantity_requested": 200,
    }

    result = rebalance_orders(orders=[order], warehouses=sample_warehouses)

    assert len(result.transfers) == 1
    assert result.transfers[0]["from_warehouse"] == "WH-WEST-01"


def test_rule_5_no_surplus_available(sample_warehouses):
    """
    Rule 5: When no other warehouse carries stock of the requested SKU:
    - Order marked as 'no_surplus_available'.
    - No transfer request created.
    """
    order = {
        "store_id": "STORE-N2",
        "routing_status": TARGET_ROUTING_STATUS,
        "assigned_warehouse_id": "WH-NORTH-01",
        "sku": "PARLE-G-800G",  # Only WH-SOUTH-01 has 800G, and suppose it has 0
        "quantity_requested": 50,
    }
    # Set all other warehouses to 0 for PARLE-G-800G
    sample_warehouses[2]["stock_per_sku"]["PARLE-G-800G"] = 0

    result = rebalance_orders(orders=[order], warehouses=sample_warehouses)

    assert len(result.orders) == 1
    assert len(result.transfers) == 0

    updated_order = result.orders[0]
    assert updated_order["rebalancing_status"] == STATUS_NO_SURPLUS_AVAILABLE
    assert "transfer_id" not in updated_order


def test_rule_6_partial_surplus_capped_at_available_stock(sample_warehouses):
    """
    Rule 6: If the shortage is larger than the best candidate's available stock,
    transfer ONLY what is available (don't transfer more than available).
    - WH-NORTH-01 has 0 units of 800G. Shortage = 150.
    - WH-SOUTH-01 only has 100 units of 800G.
    - Transfer quantity should be capped at 100.
    """
    order = {
        "store_id": "STORE-N3",
        "routing_status": TARGET_ROUTING_STATUS,
        "assigned_warehouse_id": "WH-NORTH-01",
        "sku": "PARLE-G-800G",
        "quantity_requested": 150,
    }

    result = rebalance_orders(orders=[order], warehouses=sample_warehouses)

    assert len(result.transfers) == 1
    transfer = result.transfers[0]
    assert transfer["quantity"] == 100  # Capped at available surplus
    assert transfer["from_warehouse"] == "WH-SOUTH-01"
    assert transfer["to_warehouse"] == "WH-NORTH-01"


def test_rule_7_inventory_levels_updated_consistently(sample_warehouses):
    """
    Rule 7: In local mock, actually update stock_per_sku in both warehouses:
    - Deduct transferred quantity from source.
    - Add transferred quantity to destination.
    """
    order = {
        "store_id": "STORE-N1",
        "routing_status": TARGET_ROUTING_STATUS,
        "assigned_warehouse_id": "WH-NORTH-01",
        "sku": "PARLE-G-250G",
        "quantity_requested": 150,  # WH-NORTH-01 has 50; shortage is 100
    }

    # Pre-transfer: WH-WEST-01 has 1000, WH-NORTH-01 has 50
    assert sample_warehouses[0]["stock_per_sku"]["PARLE-G-250G"] == 1000
    assert sample_warehouses[1]["stock_per_sku"]["PARLE-G-250G"] == 50

    rebalance_orders(orders=[order], warehouses=sample_warehouses)

    # Post-transfer: WH-WEST-01: 1000 - 100 = 900
    # WH-NORTH-01: 50 + 100 = 150
    assert sample_warehouses[0]["stock_per_sku"]["PARLE-G-250G"] == 900
    assert sample_warehouses[1]["stock_per_sku"]["PARLE-G-250G"] == 150


def test_sequential_orders_decrement_inventory_and_increment_ids(sample_warehouses):
    """
    Verifies that sequential orders in a batch update stock incrementally
    and that transfer IDs increment sequentially (TR-0001, TR-0002).
    """
    orders = [
        {
            "store_id": "STORE-01",
            "routing_status": TARGET_ROUTING_STATUS,
            "assigned_warehouse_id": "WH-NORTH-01",
            "sku": "PARLE-G-250G",
            "quantity_requested": 100,  # Has 50, needs 50 from WH-WEST (1000 -> 950)
        },
        {
            "store_id": "STORE-02",
            "routing_status": TARGET_ROUTING_STATUS,
            "assigned_warehouse_id": "WH-SOUTH-01",
            "sku": "PARLE-G-100G",
            "quantity_requested": 500,  # Has 0, needs 500 from WH-WEST (3000 -> 2500)
        },
    ]

    result = rebalance_orders(orders=orders, warehouses=sample_warehouses)

    assert len(result.transfers) == 2
    assert result.transfers[0]["transfer_id"] == "TR-0001"
    assert result.transfers[1]["transfer_id"] == "TR-0002"

    assert result.orders[0]["transfer_id"] == "TR-0001"
    assert result.orders[1]["transfer_id"] == "TR-0002"

    # Verify inventory updates
    assert sample_warehouses[0]["stock_per_sku"]["PARLE-G-250G"] == 950
    assert sample_warehouses[0]["stock_per_sku"]["PARLE-G-100G"] == 2500


def test_preserves_order_metadata(sample_warehouses):
    """
    Ensures existing metadata from Order Intake and Spatial Routing
    is preserved after rebalancing.
    """
    order = {
        "store_id": "STORE-GOV",
        "region": "North",
        "customer_type": "Government",
        "sku": "PARLE-G-250G",
        "quantity_requested": 100,
        "priority_tier": 1,
        "priority_score": 1.0,
        "routing_status": TARGET_ROUTING_STATUS,
        "assigned_warehouse_id": "WH-NORTH-01",
    }

    result = rebalance_orders(orders=[order], warehouses=sample_warehouses)
    updated = result.orders[0]

    assert updated["store_id"] == "STORE-GOV"
    assert updated["region"] == "North"
    assert updated["customer_type"] == "Government"
    assert updated["priority_tier"] == 1
    assert updated["priority_score"] == 1.0
    assert updated["routing_status"] == TARGET_ROUTING_STATUS
    assert updated["rebalancing_status"] == STATUS_TRANSFER_CREATED


def test_end_to_end_pipeline_integration(sample_warehouses):
    """
    Tests the full pipeline from Spatial Routing to Rebalancing Agent.
    """
    raw_orders = [
        {
            "store_id": "STORE-WEST",
            "region": "West",
            "sku": "PARLE-G-250G",
            "quantity_requested": 200,  # WH-WEST has 1000 -> fulfilled directly
        },
        {
            "store_id": "STORE-NORTH",
            "region": "North",
            "sku": "PARLE-G-250G",
            "quantity_requested": 200,  # WH-NORTH has 50 -> insufficient_stock
        },
    ]

    # Step 1: Spatial Routing
    routed = route_orders(orders=raw_orders, warehouses=sample_warehouses)
    assert routed[0]["routing_status"] == STATUS_FULFILLED_DIRECTLY
    assert routed[1]["routing_status"] == STATUS_INSUFFICIENT_STOCK

    # Step 2: Rebalancing
    rebalanced_orders, transfers = rebalance_inventory(orders=routed, warehouses=sample_warehouses)

    # First order should remain untouched
    assert "rebalancing_status" not in rebalanced_orders[0]

    # Second order should have a transfer
    assert rebalanced_orders[1]["rebalancing_status"] == STATUS_TRANSFER_CREATED
    assert len(transfers) == 1
    assert transfers[0]["sku"] == "PARLE-G-250G"
    assert transfers[0]["to_warehouse"] == "WH-NORTH-01"

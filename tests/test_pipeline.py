"""
Unit and integration tests for the Parle-G End-to-End Pipeline.
==============================================================
Tests verify:
1. Full pipeline execution on a curated set of sample orders covering all key customer
   types (Government, NGO, Loyal, Regular) and fulfillment scenarios:
   - Direct fulfillment from regional warehouse
   - Stock shortage triggering an auto-approved transfer (< 50,000 units)
   - Stock shortage triggering pending manager approval (>= 50,000 units)
   - Unserviced region (no warehouse for region)
2. Priority-first stock allocation: High-priority orders claim on-hand inventory first
   when competing with lower-priority orders for the same SKU and warehouse.
3. Default execution: Pipeline runs end-to-end with default data/ JSON files.
"""

import pytest
from pipeline import run_pipeline, determine_final_outcome, PipelineResult


@pytest.fixture
def mock_pipeline_environment():
    """
    Sets up a controlled warehouse network and test orders for deterministic testing.
    """
    warehouses = [
        {
            "warehouse_id": "WH-WEST-01",
            "region_served": "West",
            "stock_per_sku": {
                "PARLE-G-250G": 90000,  # Plentiful surplus to satisfy a 50k+ transfer
                "PARLE-G-100G": 2000,
            },
        },
        {
            "warehouse_id": "WH-NORTH-01",
            "region_served": "North",
            "stock_per_sku": {
                "PARLE-G-250G": 100,    # Limited stock; causes shortages for larger requests
                "PARLE-G-100G": 1000,
            },
        },
        {
            "warehouse_id": "WH-SOUTH-01",
            "region_served": "South",
            "stock_per_sku": {
                "PARLE-G-800G": 500,
            },
        },
    ]

    orders = [
        # Order 1: Government (Tier 1) in West -> Directly fulfilled from WH-WEST-01
        {
            "store_id": "STORE-GOV-01",
            "region": "West",
            "customer_type": "Government",
            "sku": "PARLE-G-100G",
            "quantity_requested": 500,
            "order_timestamp": "2026-09-25T08:00:00Z",
            "current_stock_level": 10,
        },
        # Order 2: NGO (Tier 2) in North -> Directly fulfilled from WH-NORTH-01
        {
            "store_id": "STORE-NGO-02",
            "region": "North",
            "customer_type": "NGO",
            "sku": "PARLE-G-100G",
            "quantity_requested": 400,
            "order_timestamp": "2026-09-25T08:15:00Z",
            "current_stock_level": 5,
        },
        # Order 3: Loyal (Tier 3) in North -> Needs 200 of 250G.
        # WH-NORTH-01 has 100 -> shortage of 100.
        # Sourced from WH-WEST-01 (100 units < 50,000) -> transfer auto-approved
        {
            "store_id": "STORE-LOY-03",
            "region": "North",
            "customer_type": "Loyal",
            "sku": "PARLE-G-250G",
            "quantity_requested": 200,
            "order_timestamp": "2026-09-25T08:30:00Z",
            "current_stock_level": 0,
        },
        # Order 4: Regular (Tier 4) in North -> Needs 55,000 units of 250G.
        # Massive shortage >= 50,000 units sourced from WH-WEST-01 -> pending manager approval
        {
            "store_id": "STORE-REG-04",
            "region": "North",
            "customer_type": "Regular",
            "sku": "PARLE-G-250G",
            "quantity_requested": 55000,
            "order_timestamp": "2026-09-25T09:00:00Z",
            "current_stock_level": 0,
        },
        # Order 5: Regular (Tier 4) in East -> No warehouse serves East -> no warehouse for region
        {
            "store_id": "STORE-REG-05",
            "region": "East",
            "customer_type": "Regular",
            "sku": "PARLE-G-100G",
            "quantity_requested": 50,
            "order_timestamp": "2026-09-25T09:15:00Z",
            "current_stock_level": 20,
        },
    ]

    return orders, warehouses


def test_full_pipeline_multi_scenario(mock_pipeline_environment):
    """
    Runs the full end-to-end pipeline on 5 sample orders and validates
    that the final outcome for each order matches hand-calculated expectations.
    """
    orders, warehouses = mock_pipeline_environment

    result = run_pipeline(orders=orders, warehouses=warehouses)

    assert isinstance(result, PipelineResult)
    assert len(result.orders) == 5

    # Index orders by store_id for explicit assertion checks
    orders_by_store = {o["store_id"]: o for o in result.orders}

    # 1. Government Order -> Fulfilled directly
    gov_order = orders_by_store["STORE-GOV-01"]
    assert gov_order["priority_tier"] == 1
    assert gov_order["assigned_warehouse"] == "WH-WEST-01"
    assert gov_order["final_routing_outcome"] == "fulfilled directly"

    # 2. NGO Order -> Fulfilled directly
    ngo_order = orders_by_store["STORE-NGO-02"]
    assert ngo_order["priority_tier"] == 2
    assert ngo_order["assigned_warehouse"] == "WH-NORTH-01"
    assert ngo_order["final_routing_outcome"] == "fulfilled directly"

    # 3. Loyal Order -> Shortage bridged via auto-approved transfer (< 50k)
    loy_order = orders_by_store["STORE-LOY-03"]
    assert loy_order["priority_tier"] == 3
    assert loy_order["assigned_warehouse"] == "WH-NORTH-01"
    assert loy_order["final_routing_outcome"] == "transfer auto-approved"
    assert loy_order.get("transfer_id") is not None

    # 4. Regular Order -> Large shortage requiring manager approval (>= 50k)
    reg_order = orders_by_store["STORE-REG-04"]
    assert reg_order["priority_tier"] == 4
    assert reg_order["assigned_warehouse"] == "WH-NORTH-01"
    assert reg_order["final_routing_outcome"] == "transfer pending approval"
    assert reg_order.get("transfer_id") is not None

    # 5. East Order -> Unserviced region
    east_order = orders_by_store["STORE-REG-05"]
    assert east_order["priority_tier"] == 4
    assert east_order["assigned_warehouse"] is None
    assert east_order["final_routing_outcome"] == "no warehouse for region"

    # Validate overall summary stats
    stats = result.stats
    assert stats["total_orders"] == 5
    assert stats["fulfilled_directly"] == 2
    assert stats["transfers_created"] == 2
    assert stats["auto_approved"] == 1
    assert stats["pending_manager_approval"] == 1
    assert stats["unfulfillable"] == 1
    assert stats["no_warehouse_for_region"] == 1


def test_priority_sorting_allocates_stock_to_higher_tier_first():
    """
    Verifies that when two orders compete for the same stock at the same warehouse,
    the higher-priority order (Tier 2 NGO) claims the stock first, leaving the
    lower-priority order (Tier 4 Regular) to be marked as insufficient_stock.
    """
    warehouses = [
        {
            "warehouse_id": "WH-NORTH-01",
            "region_served": "North",
            "stock_per_sku": {
                "PARLE-G-100G": 300,  # Only 300 units available
            },
        },
        {
            "warehouse_id": "WH-WEST-01",
            "region_served": "West",
            "stock_per_sku": {
                "PARLE-G-100G": 500,  # Surplus for rebalancing
            },
        },
    ]

    # Two orders submitted in reverse priority order:
    # Regular order submitted first in list, NGO order submitted second.
    competing_orders = [
        {
            "store_id": "STORE-REGULAR",
            "region": "North",
            "customer_type": "Regular",
            "sku": "PARLE-G-100G",
            "quantity_requested": 300,
            "order_timestamp": "2026-09-25T07:00:00Z",  # Earlier timestamp
            "current_stock_level": 5,
        },
        {
            "store_id": "STORE-NGO",
            "region": "North",
            "customer_type": "NGO",
            "sku": "PARLE-G-100G",
            "quantity_requested": 300,
            "order_timestamp": "2026-09-25T09:00:00Z",  # Later timestamp
            "current_stock_level": 0,
        },
    ]

    result = run_pipeline(orders=competing_orders, warehouses=warehouses)

    # After priority sorting (Step 3), NGO (Tier 2) must be processed before Regular (Tier 4)
    processed_orders = result.orders
    assert processed_orders[0]["store_id"] == "STORE-NGO"
    assert processed_orders[0]["final_routing_outcome"] == "fulfilled directly"

    # Regular order had to be rebalanced because NGO claimed the on-hand stock
    assert processed_orders[1]["store_id"] == "STORE-REGULAR"
    assert processed_orders[1]["final_routing_outcome"] == "transfer auto-approved"


def test_pipeline_with_default_data_files():
    """
    Verifies that run_pipeline() executes cleanly without arguments using
    the project's default data/orders.json and data/warehouses.json.
    """
    result = run_pipeline()

    assert isinstance(result, PipelineResult)
    assert len(result.orders) == 8
    assert result.stats["total_orders"] == 8
    assert result.stats["fulfilled_directly"] == 5
    assert result.stats["transfers_created"] == 1
    assert result.stats["auto_approved"] == 1
    assert result.stats["unfulfillable"] == 2

"""
Unit tests for the Parle-G Spatial Routing Agent.
=================================================
Tests cover:
1. An order with a valid region and sufficient stock (fulfilled_directly, stock deducted).
2. An order whose region has no assigned warehouse (no_warehouse_for_region, None warehouse).
3. An order where the warehouse exists but stock is insufficient (insufficient_stock, warehouse recorded).
4. An order whose SKU does not exist in that warehouse's stock_per_sku (treated as 0, insufficient_stock).
5. Preservation of existing fields (such as priority_tier and priority_score from Agent 1).
"""

import copy
import pytest
from agents.spatial_routing_agent import (
    load_warehouses,
    route_orders,
    route_single_order,
    build_warehouse_region_map,
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
                "PARLE-G-100G": 2000,
            },
        },
        {
            "warehouse_id": "WH-NORTH-01",
            "region_served": "North",
            "stock_per_sku": {
                "PARLE-G-250G": 50,
                "PARLE-G-100G": 300,
            },
        },
    ]


def test_order_valid_region_sufficient_stock(sample_warehouses):
    """
    Test Case 1: Valid region and sufficient stock.
    Verifies Rule 5:
    - Status is 'fulfilled_directly'.
    - assigned_warehouse_id is recorded.
    - quantity_requested is deducted from warehouse's stock_per_sku.
    """
    order = {
        "store_id": "STORE-W1",
        "region": "West",
        "sku": "PARLE-G-250G",
        "quantity_requested": 200,
    }

    # Warehouse starts with 1000 units
    initial_stock = sample_warehouses[0]["stock_per_sku"]["PARLE-G-250G"]
    assert initial_stock == 1000

    results = route_orders(orders=[order], warehouses=sample_warehouses)

    assert len(results) == 1
    routed = results[0]

    # Verify routing outcome
    assert routed["routing_status"] == STATUS_FULFILLED_DIRECTLY
    assert routed["assigned_warehouse_id"] == "WH-WEST-01"

    # Verify stock deduction: 1000 - 200 = 800
    updated_stock = sample_warehouses[0]["stock_per_sku"]["PARLE-G-250G"]
    assert updated_stock == 800


def test_order_region_no_assigned_warehouse(sample_warehouses):
    """
    Test Case 2: Order whose region has no assigned warehouse.
    Verifies Rule 3:
    - Status is 'no_warehouse_for_region'.
    - assigned_warehouse_id is None.
    - Processing stops cleanly without crashing.
    """
    order = {
        "store_id": "STORE-E1",
        "region": "East",  # No warehouse in sample_warehouses serves 'East'
        "sku": "PARLE-G-250G",
        "quantity_requested": 100,
    }

    results = route_orders(orders=[order], warehouses=sample_warehouses)

    assert len(results) == 1
    routed = results[0]

    assert routed["routing_status"] == STATUS_NO_WAREHOUSE_FOR_REGION
    assert routed["assigned_warehouse_id"] is None


def test_order_warehouse_exists_insufficient_stock(sample_warehouses):
    """
    Test Case 3: Warehouse exists, but available stock is insufficient.
    Verifies Rule 6:
    - Status is 'insufficient_stock'.
    - assigned_warehouse_id IS recorded anyway (for downstream rebalancing).
    - Stock is NOT deducted.
    """
    order = {
        "store_id": "STORE-N1",
        "region": "North",
        "sku": "PARLE-G-250G",
        "quantity_requested": 200,  # WH-NORTH-01 only has 50 units
    }

    initial_stock = sample_warehouses[1]["stock_per_sku"]["PARLE-G-250G"]
    assert initial_stock == 50

    results = route_orders(orders=[order], warehouses=sample_warehouses)

    assert len(results) == 1
    routed = results[0]

    assert routed["routing_status"] == STATUS_INSUFFICIENT_STOCK
    assert routed["assigned_warehouse_id"] == "WH-NORTH-01"

    # Stock must NOT be deducted
    remaining_stock = sample_warehouses[1]["stock_per_sku"]["PARLE-G-250G"]
    assert remaining_stock == 50


def test_order_sku_not_in_warehouse_stock(sample_warehouses):
    """
    Test Case 4: Warehouse exists, but requested SKU does not exist in stock_per_sku.
    Verifies Rule 6:
    - Missing SKU is treated as 0 stock.
    - Status is 'insufficient_stock'.
    - assigned_warehouse_id is recorded.
    """
    order = {
        "store_id": "STORE-W2",
        "region": "West",
        "sku": "PARLE-G-800G",  # WH-WEST-01 does not carry 800G in sample_warehouses
        "quantity_requested": 50,
    }

    # Verify SKU is indeed absent
    assert "PARLE-G-800G" not in sample_warehouses[0]["stock_per_sku"]

    results = route_orders(orders=[order], warehouses=sample_warehouses)

    assert len(results) == 1
    routed = results[0]

    assert routed["routing_status"] == STATUS_INSUFFICIENT_STOCK
    assert routed["assigned_warehouse_id"] == "WH-WEST-01"


def test_preserves_order_intake_priority_fields(sample_warehouses):
    """
    Test Case 5: Verifies that prior metadata from order_intake_agent
    (such as priority_tier and priority_score) is preserved through spatial routing.
    """
    order_with_priority = {
        "store_id": "STORE-W1",
        "region": "West",
        "customer_type": "Government",
        "sku": "PARLE-G-250G",
        "quantity_requested": 100,
        "priority_tier": 1,
        "priority_score": 1.0,
    }

    results = route_orders(orders=[order_with_priority], warehouses=sample_warehouses)

    assert len(results) == 1
    routed = results[0]

    assert routed["priority_tier"] == 1
    assert routed["priority_score"] == 1.0
    assert routed["routing_status"] == STATUS_FULFILLED_DIRECTLY
    assert routed["assigned_warehouse_id"] == "WH-WEST-01"

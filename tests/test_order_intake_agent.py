"""
Unit tests for the Parle-G Order Intake Agent.
==============================================
Tests cover:
1. One order per customer type (Government, NGO, Loyal, Regular).
2. Two Regular orders with different stock levels (lower stock = higher urgency).
3. Two Regular orders with identical stock levels (earlier timestamp tiebreaker).
4. One order with an unrecognized customer_type (warning logged, lowest priority assigned, no crash).
5. Edge cases: Empty list and loading from file.
"""

import logging
import pytest
from agents.order_intake_agent import (
    load_orders,
    process_orders,
    get_customer_tier,
    KNOWN_CUSTOMER_TIERS,
    UNKNOWN_CUSTOMER_TIER,
)


def test_one_order_per_customer_type():
    """
    Test Case 1: One order per customer type.
    Verifies that:
    - Government ranks 1st (Tier 1)
    - NGO ranks 2nd (Tier 2)
    - Loyal ranks 3rd (Tier 3)
    - Regular ranks 4th (Tier 4)
    - Priority scores strictly increase (lower score = higher priority)
    """
    sample_orders = [
        {
            "store_id": "STORE-REG",
            "region": "West",
            "customer_type": "Regular",
            "sku": "PARLE-G-250G",
            "quantity_requested": 100,
            "order_timestamp": "2026-09-25T10:00:00Z",
            "current_stock_level": 30,
        },
        {
            "store_id": "STORE-LOY",
            "region": "South",
            "customer_type": "Loyal",
            "sku": "PARLE-G-250G",
            "quantity_requested": 200,
            "order_timestamp": "2026-09-25T09:00:00Z",
            "current_stock_level": 0,
        },
        {
            "store_id": "STORE-GOV",
            "region": "North",
            "customer_type": "Government",
            "sku": "PARLE-G-250G",
            "quantity_requested": 500,
            "order_timestamp": "2026-09-25T08:00:00Z",
            "current_stock_level": 0,
        },
        {
            "store_id": "STORE-NGO",
            "region": "East",
            "customer_type": "NGO",
            "sku": "PARLE-G-250G",
            "quantity_requested": 300,
            "order_timestamp": "2026-09-25T08:30:00Z",
            "current_stock_level": 0,
        },
    ]

    # Process orders through the agent
    prioritized = process_orders(sample_orders)

    # Must return all 4 orders
    assert len(prioritized) == 4

    # Verify order of customer types: Government -> NGO -> Loyal -> Regular
    assert prioritized[0]["customer_type"] == "Government"
    assert prioritized[0]["priority_tier"] == 1
    assert prioritized[0]["priority_score"] == 1.0

    assert prioritized[1]["customer_type"] == "NGO"
    assert prioritized[1]["priority_tier"] == 2
    assert prioritized[1]["priority_score"] == 2.0

    assert prioritized[2]["customer_type"] == "Loyal"
    assert prioritized[2]["priority_tier"] == 3
    assert prioritized[2]["priority_score"] == 3.0

    assert prioritized[3]["customer_type"] == "Regular"
    assert prioritized[3]["priority_tier"] == 4
    assert prioritized[3]["priority_score"] > 3.0

    # Verify scores are sorted in ascending order (highest priority first)
    scores = [order["priority_score"] for order in prioritized]
    assert scores == sorted(scores)


def test_regular_orders_different_stock_levels():
    """
    Test Case 2: Two Regular orders with different stock levels.
    Verifies Rule 4:
    - The lower the current_stock_level, the MORE urgent (higher priority) the order is.
    - An order with 5 units remaining must rank higher than one with 40 units remaining.
    """
    order_low_stock = {
        "store_id": "STORE-URGENT",
        "region": "West",
        "customer_type": "Regular",
        "sku": "PARLE-G-250G",
        "quantity_requested": 150,
        "order_timestamp": "2026-09-25T10:00:00Z",
        "current_stock_level": 5,  # Very low stock -> high urgency
    }
    order_high_stock = {
        "store_id": "STORE-COMFORTABLE",
        "region": "East",
        "customer_type": "Regular",
        "sku": "PARLE-G-250G",
        "quantity_requested": 150,
        "order_timestamp": "2026-09-25T08:00:00Z",  # Even though earlier, stock is high
        "current_stock_level": 40,  # Higher stock -> lower urgency
    }

    # Pass in high stock first to ensure sorting actually changes the order
    prioritized = process_orders([order_high_stock, order_low_stock])

    assert len(prioritized) == 2

    # The store with stock 5 must be ranked 1st
    assert prioritized[0]["store_id"] == "STORE-URGENT"
    assert prioritized[0]["current_stock_level"] == 5

    # The store with stock 40 must be ranked 2nd
    assert prioritized[1]["store_id"] == "STORE-COMFORTABLE"
    assert prioritized[1]["current_stock_level"] == 40

    # Low stock order must have lower priority score (higher priority)
    assert prioritized[0]["priority_score"] < prioritized[1]["priority_score"]


def test_regular_orders_identical_stock_levels_tiebreaker():
    """
    Test Case 3: Two Regular orders with identical stock levels.
    Verifies Rule 5:
    - If two Regular orders have identical current_stock_level, break the tie
      using order_timestamp (earlier timestamp wins).
    """
    order_earlier = {
        "store_id": "STORE-EARLY",
        "region": "North",
        "customer_type": "Regular",
        "sku": "PARLE-G-250G",
        "quantity_requested": 200,
        "order_timestamp": "2026-09-25T08:15:00Z",  # Earlier timestamp
        "current_stock_level": 15,
    }
    order_later = {
        "store_id": "STORE-LATE",
        "region": "South",
        "customer_type": "Regular",
        "sku": "PARLE-G-250G",
        "quantity_requested": 200,
        "order_timestamp": "2026-09-25T11:45:00Z",  # Later timestamp
        "current_stock_level": 15,  # Identical stock level
    }

    # Pass later order first to ensure the tiebreaker logic handles the sort
    prioritized = process_orders([order_later, order_earlier])

    assert len(prioritized) == 2

    # Earlier timestamp must win the tiebreaker and rank first
    assert prioritized[0]["store_id"] == "STORE-EARLY"
    assert prioritized[1]["store_id"] == "STORE-LATE"

    # Store with earlier timestamp has a better (lower) priority_score
    assert prioritized[0]["priority_score"] < prioritized[1]["priority_score"]


def test_unrecognized_customer_type(caplog):
    """
    Test Case 4: One order with an unrecognized customer_type.
    Verifies Rule 6:
    - Logs a warning message.
    - Treats the unrecognized order as lowest priority (Tier 5).
    - Does NOT crash.
    """
    valid_order = {
        "store_id": "STORE-REG-VALID",
        "region": "West",
        "customer_type": "Regular",
        "sku": "PARLE-G-250G",
        "quantity_requested": 100,
        "order_timestamp": "2026-09-25T10:00:00Z",
        "current_stock_level": 10,
    }
    unknown_order = {
        "store_id": "STORE-UNKNOWN-TYPE",
        "region": "North",
        "customer_type": "WholesaleClub",  # Unrecognized customer type
        "sku": "PARLE-G-250G",
        "quantity_requested": 500,
        "order_timestamp": "2026-09-25T07:00:00Z",
        "current_stock_level": 5,
    }

    with caplog.at_level(logging.WARNING):
        prioritized = process_orders([unknown_order, valid_order])

    # 1. Check that a warning was indeed logged
    assert any("Unrecognized customer_type 'WholesaleClub'" in record.message for record in caplog.records)

    # 2. Check that the program did not crash and returned both orders
    assert len(prioritized) == 2

    # 3. Check that the valid order ranks ahead of the unrecognized one
    assert prioritized[0]["store_id"] == "STORE-REG-VALID"
    assert prioritized[0]["priority_tier"] == 4

    # 4. Check that the unrecognized order gets the lowest priority (Tier 5)
    assert prioritized[1]["store_id"] == "STORE-UNKNOWN-TYPE"
    assert prioritized[1]["priority_tier"] == UNKNOWN_CUSTOMER_TIER
    assert prioritized[1]["priority_score"] == float(UNKNOWN_CUSTOMER_TIER)


def test_empty_orders_list():
    """
    Test edge case: Empty list of orders.
    Verifies that passing an empty list returns an empty list safely.
    """
    result = process_orders([])
    assert result == []


def test_load_orders_from_file(tmp_path):
    """
    Test loading orders from a temporary JSON file.
    Verifies that load_orders parses valid JSON successfully.
    """
    test_file = tmp_path / "test_orders.json"
    test_file.write_text(
        '[{"store_id": "S1", "region": "West", "customer_type": "Loyal", '
        '"sku": "PARLE-G-100G", "quantity_requested": 50, '
        '"order_timestamp": "2026-09-25T08:00:00Z", "current_stock_level": 0}]',
        encoding="utf-8",
    )

    loaded = load_orders(test_file)
    assert len(loaded) == 1
    assert loaded[0]["store_id"] == "S1"
    assert loaded[0]["customer_type"] == "Loyal"

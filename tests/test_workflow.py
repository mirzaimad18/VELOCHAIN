"""Unit tests for core supply-chain scoring, routing, and approval logic."""

import pytest

from order_intake import calculate_priority_score
from supervisor_agent import supervisor_node
from tools.spatial_routing import haversine_distance


@pytest.mark.parametrize(
    ("urgency", "loyalty_tier", "expected_score"),
    [
        ("High", "Gold", 81.0),
        ("High", "Silver", 71.0),
        ("High", "Bronze", 61.0),
        ("Medium", "Gold", 61.0),
        ("Medium", "Silver", 51.0),
        ("Medium", "Bronze", 41.0),
        ("Low", "Gold", 41.0),
        ("Low", "Silver", 31.0),
        ("Low", "Bronze", 21.0),
    ],
)
def test_calculate_priority_score(urgency, loyalty_tier, expected_score):
    assert calculate_priority_score(1_000, urgency, loyalty_tier) == expected_score


def test_haversine_distance_one_degree_at_equator():
    assert haversine_distance(0, 0, 0, 1) == pytest.approx(
        111.1949266, rel=1e-7
    )


def test_haversine_distance_same_coordinate_is_zero():
    assert haversine_distance(19.0760, 72.8777, 19.0760, 72.8777) == 0


def _supervisor_state(quantity: int) -> dict:
    return {
        "order": {
            "store_id": 1,
            "quantity": quantity,
            "urgency_level": "High",
            "raw_notes": None,
        },
        "priority_score": 80.0,
        "assigned_warehouse": None,
        "transfer_request": {
            "from_warehouse_id": 2,
            "from_warehouse_name": "Nagpur Central Depot",
            "to_warehouse_id": 1,
            "quantity": quantity,
            "requires_approval": quantity > 50_000,
        },
        "approval_status": "NEEDS_REBALANCE",
        "messages": [],
        "status_logs": ["Transfer proposed."],
    }


def _persist_transfer_for_test(proposal):
    assert proposal.quantity > 0
    return 123


@pytest.mark.parametrize("quantity", [50_001, 75_000])
def test_supervisor_requires_human_approval_over_threshold(
    monkeypatch, quantity
):
    monkeypatch.setattr(
        "supervisor_agent._persist_pending_transfer",
        _persist_transfer_for_test,
    )

    result = supervisor_node(_supervisor_state(quantity))

    assert result["approval_status"] == "REQUIRES_HUMAN_APPROVAL"
    assert result["transfer_request"]["transfer_id"] == 123
    assert result["status_logs"][-1] == (
        "ALERT: Transfer exceeding 50,000 units requires "
        "Human-in-the-Loop manager approval."
    )


@pytest.mark.parametrize("quantity", [1, 50_000])
def test_supervisor_approves_transfer_at_or_below_threshold(
    monkeypatch, quantity
):
    monkeypatch.setattr(
        "supervisor_agent._persist_pending_transfer",
        _persist_transfer_for_test,
    )

    result = supervisor_node(_supervisor_state(quantity))

    assert result["approval_status"] == "APPROVED"
    assert result["transfer_request"]["transfer_id"] == 123
    assert "ALERT: Transfer exceeding 50,000 units requires" not in "\n".join(
        result["status_logs"]
    )

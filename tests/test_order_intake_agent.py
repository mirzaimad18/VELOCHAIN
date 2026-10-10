"""Tests for structured email parsing and order ranking."""

from unittest.mock import MagicMock

from google.genai.errors import APIError
import pytest

from agents import order_intake


@pytest.mark.parametrize(
    ("model_store_id", "expected_store_id"),
    [(2, 2), (999, 1), (None, 1)],
)
def test_parse_email_validates_store_id_and_uses_fallback(
    monkeypatch, model_store_id, expected_store_id
):
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.setattr(
        order_intake,
        "fetch_query",
        MagicMock(
            return_value=[
                (1, "Mumbai Central Supermart", "Mumbai"),
                (2, "Pune Retail Depot", "Pune"),
            ]
        ),
    )
    parsed_order = order_intake.ParsedEmailOrder(
        store_id=model_store_id,
        quantity=2500,
        urgency_level="High",
        summary_reason="Urgent restock requested.",
    )
    llm = MagicMock()
    llm.invoke.return_value = parsed_order
    chat_model = MagicMock()
    chat_model.with_structured_output.return_value = llm
    monkeypatch.setattr(
        order_intake, "ChatGoogleGenerativeAI", MagicMock(return_value=chat_model)
    )

    result = order_intake.parse_email_to_order_json(
        "Please urgently send 2,500 units to the Pune depot."
    )

    assert result == {
        "store_id": expected_store_id,
        "quantity": 2500,
        "urgency_level": "High",
        "summary_reason": "Urgent restock requested.",
    }
    chat_model.with_structured_output.assert_called_once_with(
        order_intake.ParsedEmailOrder
    )


@pytest.mark.parametrize(
    ("email_text", "expected"),
    [
        (
            "Store 2 needs 12,500 units immediately.",
            {
                "store_id": 2,
                "quantity": 12500,
                "urgency_level": "High",
                "summary_reason": "Extracted from email using regex fallback.",
            },
        ),
        (
            "Please send 40 boxes to our outlet.",
            {
                "store_id": 1,
                "quantity": 40,
                "urgency_level": "Medium",
                "summary_reason": "Extracted from email using regex fallback.",
            },
        ),
        (
            "We need stock as soon as possible.",
            {
                "store_id": 1,
                "quantity": 60000,
                "urgency_level": "Medium",
                "summary_reason": "Extracted from email using regex fallback.",
            },
        ),
    ],
)
def test_parse_email_falls_back_when_api_key_is_missing(
    monkeypatch, email_text, expected
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        order_intake,
        "fetch_query",
        MagicMock(return_value=[(1, "Mumbai Central Supermart", "Mumbai")]),
    )
    monkeypatch.setattr(
        order_intake,
        "ChatGoogleGenerativeAI",
        MagicMock(side_effect=AssertionError("Gemini should not be initialized")),
    )

    assert order_intake.parse_email_to_order_json(email_text) == expected


@pytest.mark.parametrize(
    "api_error",
    [
        APIError(400, {"message": "Bad request"}),
        APIError(403, {"message": "API key is invalid"}),
    ],
)
def test_parse_email_falls_back_for_gemini_api_errors(monkeypatch, api_error):
    monkeypatch.setenv("GEMINI_API_KEY", "invalid-api-key")
    monkeypatch.setattr(
        order_intake,
        "fetch_query",
        MagicMock(return_value=[(1, "Mumbai Central Supermart", "Mumbai")]),
    )
    model = MagicMock()
    model.with_structured_output.return_value.invoke.side_effect = api_error
    monkeypatch.setattr(
        order_intake, "ChatGoogleGenerativeAI", MagicMock(return_value=model)
    )

    result = order_intake.parse_email_to_order_json(
        "Store 3 urgently needs 2,000 packets."
    )

    assert result["store_id"] == 3
    assert result["quantity"] == 2000
    assert result["urgency_level"] == "High"


def test_parse_email_falls_back_for_network_errors(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.setattr(
        order_intake,
        "fetch_query",
        MagicMock(return_value=[(1, "Mumbai Central Supermart", "Mumbai")]),
    )
    model = MagicMock()
    model.with_structured_output.return_value.invoke.side_effect = ConnectionError(
        "Network unavailable"
    )
    monkeypatch.setattr(
        order_intake, "ChatGoogleGenerativeAI", MagicMock(return_value=model)
    )

    result = order_intake.parse_email_to_order_json(
        "Store 1 needs 10,000 units ASAP."
    )

    assert result["store_id"] == 1
    assert result["quantity"] == 10000
    assert result["urgency_level"] == "High"


def test_parse_email_reraises_unrelated_llm_errors(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.setattr(
        order_intake,
        "fetch_query",
        MagicMock(return_value=[(1, "Mumbai Central Supermart", "Mumbai")]),
    )
    model = MagicMock()
    model.with_structured_output.return_value.invoke.side_effect = ValueError(
        "Unexpected model response"
    )
    monkeypatch.setattr(
        order_intake, "ChatGoogleGenerativeAI", MagicMock(return_value=model)
    )

    with pytest.raises(ValueError, match="Unexpected model response"):
        order_intake.parse_email_to_order_json("Store 1 needs 10,000 units.")


def test_rank_orders_sorts_by_score_and_looks_up_store_tier(monkeypatch):
    monkeypatch.setattr(
        order_intake,
        "fetch_query",
        MagicMock(return_value=[(1, "Gold"), (2, "Bronze")]),
    )
    orders = [
        {
            "store_id": 2,
            "quantity": 1000,
            "urgency_level": "High",
        },
        {
            "store_id": 1,
            "quantity": 1000,
            "urgency_level": "Medium",
        },
        {
            "store_id": 1,
            "quantity": 1000,
            "urgency_level": "Low",
        },
    ]

    ranked = order_intake.rank_orders(orders)

    assert [order["priority_score"] for order in ranked] == [61.0, 61.0, 41.0]
    assert ranked[0]["store_id"] == 2
    assert ranked[1]["store_id"] == 1
    assert all("priority_score" not in order for order in orders)


def test_rank_orders_uses_supplied_tier_without_database_lookup(monkeypatch):
    monkeypatch.setattr(
        order_intake,
        "fetch_query",
        MagicMock(
            side_effect=AssertionError(
                "Database should not be queried for supplied tiers"
            )
        ),
    )

    ranked = order_intake.rank_orders(
        [
            {
                "store_id": 3,
                "quantity": 5000,
                "urgency_level": "Low",
                "loyalty_tier": "Silver",
            }
        ]
    )

    assert ranked[0]["priority_score"] == 35.0


def test_rank_orders_rejects_unknown_store_tier(monkeypatch):
    monkeypatch.setattr(
        order_intake, "fetch_query", MagicMock(return_value=[])
    )

    with pytest.raises(ValueError, match="loyalty_tier or a store_id"):
        order_intake.rank_orders(
            [{"store_id": 999, "quantity": 100, "urgency_level": "Low"}]
        )

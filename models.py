"""Pydantic request/result models and LangGraph workflow state."""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


class StoreOrderRequest(BaseModel):
    store_id: int
    quantity: int = Field(gt=0)
    urgency_level: Literal["High", "Medium", "Low"]
    raw_notes: Optional[str] = None


class PriorityScoreResult(BaseModel):
    priority_score: float
    breakdown: dict[str, Any]
    urgency_level: str


class StockTransferRequest(BaseModel):
    from_warehouse_id: int
    to_warehouse_id: int
    quantity: int = Field(gt=0)
    requires_human_approval: bool
    status: str


class AgentState(TypedDict):
    order: dict[str, Any]
    priority_score: float
    assigned_warehouse: Optional[dict[str, Any]]
    transfer_request: Optional[dict[str, Any]]
    approval_status: str
    messages: list[Any]
    status_logs: list[str]

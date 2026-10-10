"""LangGraph state machine for the Parle-G supply-chain workflow."""

from __future__ import annotations

from typing import Any, Callable, Literal
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from inventory_rebalancing_agent import rebalance_inventory_node
from models import AgentState, StoreOrderRequest
from order_intake import order_intake_node
from spatial_routing_agent import spatial_routing_node
from supervisor_agent import (
    approve_transfer,
    reject_transfer,
    supervisor_node,
)

HumanDecision = Literal["approve", "reject"]
ProgressCallback = Callable[[str, dict[str, Any]], None]


def route_after_spatial(state: AgentState) -> str:
    """Route orders requiring stock movement through inventory rebalancing."""
    if state["approval_status"] == "NEEDS_REBALANCE":
        return "rebalance_inventory"
    return "supervisor"


def _supervisor_with_human_interrupt(state: AgentState) -> dict:
    result = supervisor_node(state)
    if result.get("approval_status") != "REQUIRES_HUMAN_APPROVAL":
        return result

    transfer_request = result["transfer_request"]
    decision = interrupt(
        {
            "type": "manager_approval",
            "message": "Review and approve or reject this stock transfer.",
            "transfer_request": transfer_request,
        }
    )

    if decision == "approve":
        return approve_transfer({**state, **result})
    if decision == "reject":
        return reject_transfer({**state, **result})
    raise ValueError("Human approval response must be 'approve' or 'reject'.")


def _build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("order_intake", order_intake_node)
    graph.add_node("spatial_routing", spatial_routing_node)
    graph.add_node("rebalance_inventory", rebalance_inventory_node)
    graph.add_node("supervisor", _supervisor_with_human_interrupt)

    graph.add_edge(START, "order_intake")
    graph.add_edge("order_intake", "spatial_routing")
    graph.add_conditional_edges(
        "spatial_routing",
        route_after_spatial,
        {
            "rebalance_inventory": "rebalance_inventory",
            "supervisor": "supervisor",
        },
    )
    graph.add_edge("rebalance_inventory", "supervisor")
    graph.add_edge("supervisor", END)
    return graph.compile(checkpointer=MemorySaver())


supply_chain_graph = _build_graph()


def _invoke_graph(
    graph_input: dict[str, Any] | Command,
    config: dict[str, Any],
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    if progress_callback is None:
        return supply_chain_graph.invoke(graph_input, config=config)

    for update in supply_chain_graph.stream(
        graph_input,
        config=config,
        stream_mode="updates",
    ):
        if "__interrupt__" in update:
            progress_callback(
                "__interrupt__",
                {"interrupt": update["__interrupt__"][0].value},
            )
            continue
        for node, output in update.items():
            progress_callback(node, output)

    snapshot = supply_chain_graph.get_state(config)
    result = dict(snapshot.values)
    interrupts = [
        interrupt
        for task in snapshot.tasks
        for interrupt in task.interrupts
    ]
    if interrupts:
        result["__interrupt__"] = interrupts
    return result


def run_supply_chain_workflow(
    order_payload: dict,
    *,
    thread_id: str | None = None,
    human_decision: HumanDecision | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict:
    """Run the workflow or resume a paused transfer with a manager decision.

    Pass the same ``thread_id`` and ``human_decision="approve"`` or
    ``"reject"`` to resume a workflow paused for human approval. On the initial
    call, a generated thread ID is included in the returned paused state.
    """
    if human_decision is not None and thread_id is None:
        raise ValueError("thread_id is required to resume a paused workflow.")

    current_thread_id = thread_id or str(uuid4())
    config = {"configurable": {"thread_id": current_thread_id}}
    if human_decision is not None:
        return _invoke_graph(
            Command(resume=human_decision),
            config,
            progress_callback,
        )

    order = StoreOrderRequest.model_validate(order_payload).model_dump()
    initial_state: AgentState = {
        "order": order,
        "priority_score": 0.0,
        "assigned_warehouse": None,
        "transfer_request": None,
        "approval_status": "PENDING",
        "messages": [],
        "status_logs": [],
    }
    result = _invoke_graph(initial_state, config, progress_callback)

    if "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        if interrupts:
            interrupt_value = interrupts[0].value
            transfer_request = interrupt_value["transfer_request"]
            status_logs = list(result.get("status_logs", []))
            status_logs.append(
                "ALERT: Transfer exceeding 50,000 units requires "
                "Human-in-the-Loop manager approval."
            )
            result = {
                **result,
                "transfer_request": transfer_request,
                "approval_status": "REQUIRES_HUMAN_APPROVAL",
                "status_logs": status_logs,
                "workflow_thread_id": current_thread_id,
            }

    return result

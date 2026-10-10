"""Streamlit dashboard for the Parle-G agentic supply-chain workflow."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agents.order_intake import parse_email_to_order_json, rank_orders
from database import fetch_query, init_db, seed_data
from models import StoreOrderRequest
from workflow import run_supply_chain_workflow

NODE_LABELS = {
    "order_intake": "Order Intake",
    "spatial_routing": "Spatial Routing",
    "rebalance_inventory": "Inventory Rebalance",
    "supervisor": "Supervisor",
}

FALLBACK_STORES = [
    (1, "Mumbai Central Supermart", "Mumbai"),
    (2, "Pune Retail Depot", "Pune"),
    (3, "Nagpur Small Hub", "Nagpur"),
]
FALLBACK_WAREHOUSES = pd.DataFrame(
    [
        {
            "warehouse_id": 1,
            "warehouse": "Mumbai Main Hub",
            "city": "Mumbai",
            "type": "Urban Hub",
            "latitude": 19.0760,
            "longitude": 72.8777,
            "product_name": "Parle-G 100g",
            "stock_quantity": 5_000,
            "last_updated": "Sample data",
        },
        {
            "warehouse_id": 2,
            "warehouse": "Nagpur Central Depot",
            "city": "Nagpur",
            "type": "Rural Depot",
            "latitude": 21.1458,
            "longitude": 79.0882,
            "product_name": "Parle-G 100g",
            "stock_quantity": 150_000,
            "last_updated": "Sample data",
        },
        {
            "warehouse_id": 3,
            "warehouse": "Nashik Secondary Hub",
            "city": "Nashik",
            "type": "Urban Hub",
            "latitude": 19.9975,
            "longitude": 73.7898,
            "product_name": "Parle-G 100g",
            "stock_quantity": 30_000,
            "last_updated": "Sample data",
        },
    ]
)

def _initialize_database() -> bool:
    init_db()
    return True


def _load_stores() -> list[tuple[int, str, str]]:
    return fetch_query(
        "SELECT id, name, city FROM stores ORDER BY name"
    )


def _load_warehouse_inventory() -> pd.DataFrame:
    rows = fetch_query(
        """
        SELECT
            w.id AS warehouse_id,
            w.name AS warehouse,
            w.city,
            w.type,
            w.latitude,
            w.longitude,
            i.product_name,
            COALESCE(i.stock_quantity, 0) AS stock_quantity,
            i.last_updated
        FROM warehouses AS w
        LEFT JOIN inventory AS i ON i.warehouse_id = w.id
        ORDER BY w.name, i.product_name
        """
    )
    if not rows:
        return FALLBACK_WAREHOUSES.copy()
    return pd.DataFrame(
        rows,
        columns=[
            "warehouse_id",
            "warehouse",
            "city",
            "type",
            "latitude",
            "longitude",
            "product_name",
            "stock_quantity",
            "last_updated",
        ],
    )


def _record_workflow_progress(
    node: str,
    output: dict[str, Any],
    status_box: Any,
    events: list[dict[str, Any]],
    seen_logs: set[str],
) -> None:
    if node == "__interrupt__":
        node = "supervisor"
        interrupt_info = output["interrupt"]
        logs = [
            "Workflow paused for manager approval.",
            interrupt_info["message"],
        ]
    else:
        logs = output.get("status_logs", [])

    new_logs = [log for log in logs if log not in seen_logs]
    seen_logs.update(new_logs)
    if not new_logs and node != "supervisor":
        return

    events.append({"node": node, "logs": new_logs})
    status_box.markdown(
        f"**{NODE_LABELS.get(node, node)}**"
        + (
            "\n\n" + "\n\n".join(f"- {log}" for log in new_logs)
            if new_logs
            else ""
        )
    )


def _run_new_order(order_payload: dict[str, Any]) -> None:
    thread_id = str(uuid4())
    events: list[dict[str, Any]] = []
    seen_logs: set[str] = set()
    with st.status("Running supply-chain agents...", expanded=True) as status:
        validated_order = StoreOrderRequest.model_validate(
            order_payload
        ).model_dump()
        result = run_supply_chain_workflow(
            {**order_payload, **validated_order},
            thread_id=thread_id,
            progress_callback=lambda node, output: _record_workflow_progress(
                node, output, status, events, seen_logs
            ),
        )
        interrupted = result.get("approval_status") == "REQUIRES_HUMAN_APPROVAL"
        result["workflow_thread_id"] = thread_id
        status.update(
            label=(
                "Waiting for manager approval"
                if interrupted
                else "Supply-chain workflow complete"
            ),
            state="running" if interrupted else "complete",
            expanded=interrupted,
        )

    st.session_state["workflow_result"] = result
    st.session_state["workflow_events"] = events
    st.session_state["workflow_interrupted"] = interrupted
    order_rankings = list(st.session_state.get("order_rankings", []))
    order_rankings.append(
        {
            **validated_order,
            "priority_score": result.get("priority_score", 0.0),
            "fulfillment_status": _fulfillment_status(result),
            "workflow_thread_id": thread_id,
        }
    )
    st.session_state["order_rankings"] = order_rankings


def _fulfillment_status(result: dict[str, Any]) -> str:
    status = result.get("approval_status", "PENDING")
    if status == "CONFIRMED_DIRECT_DISPATCH" or (
        status == "APPROVED" and result.get("assigned_warehouse")
    ):
        return "Dispatched"
    if status == "REQUIRES_HUMAN_APPROVAL":
        return "Requires Approval"
    if status == "NEEDS_REBALANCE":
        return "Needs Rebalance"
    if status == "APPROVED" and result.get("transfer_request"):
        return "Transfer Approved"
    if status == "APPROVED":
        return "Approved"
    if status == "REJECTED":
        return "Rejected"
    return status.replace("_", " ").title()


def _resume_approval(decision: str) -> None:
    result = st.session_state["workflow_result"]
    thread_id = result["workflow_thread_id"]
    previous_events = list(st.session_state.get("workflow_events", []))
    new_events: list[dict[str, Any]] = []
    seen_logs = {
        log
        for event in previous_events
        for log in event.get("logs", [])
    }
    with st.status(
        f"Processing transfer {decision}...", expanded=True
    ) as status:
        updated_result = run_supply_chain_workflow(
            {},
            thread_id=thread_id,
            human_decision=decision,
            progress_callback=lambda node, output: _record_workflow_progress(
                node, output, status, new_events, seen_logs
            ),
        )
        interrupted = (
            updated_result.get("approval_status")
            == "REQUIRES_HUMAN_APPROVAL"
        )
        status.update(
            label="Transfer approval workflow complete",
            state="complete",
            expanded=False,
        )

    st.session_state["workflow_result"] = updated_result
    st.session_state["workflow_events"] = previous_events + new_events
    st.session_state["workflow_interrupted"] = interrupted
    for ranked_order in st.session_state.get("order_rankings", []):
        if ranked_order.get("workflow_thread_id") == thread_id:
            ranked_order["fulfillment_status"] = _fulfillment_status(
                updated_result
            )
    st.rerun()


def main() -> None:
    st.set_page_config(
        page_title="Parle-G Supply Chain Dashboard",
        page_icon="📦",
        layout="wide",
    )
    st.title("📦 Parle-G Agentic Supply Chain Management")
    st.caption("Monitor store orders, warehouse inventory, and agent decisions.")

    try:
        load_dotenv()
    except Exception as error:
        st.error(f"Could not load environment configuration: {error}")

    database_ready = False
    try:
        _initialize_database()
        database_ready = True
    except Exception as error:
        st.error(f"Could not initialize the configured database: {error}")

    try:
        stores = _load_stores() if database_ready else []
        if not stores:
            stores = FALLBACK_STORES.copy()
    except Exception as error:
        st.error(f"Could not load stores from the database: {error}")
        stores = FALLBACK_STORES.copy()

    with st.sidebar:
        st.header("Parse Store Order Email")
        if not database_ready:
            st.info(
                "Using sample store and warehouse data. "
                "Database-backed order processing requires a working database."
            )
        email_text = st.text_area(
            "Paste Order Email Here",
            placeholder="Paste the store's order email here.",
        )
        parse_order = st.button(
            "Parse & Process Email",
            type="primary",
            width="stretch",
        )

        if st.button("Load sample data", width="stretch"):
            try:
                seed_data()
            except Exception as error:
                st.error(f"Could not load sample data: {error}")
            else:
                st.success("Sample data loaded.")
                st.rerun()

        if parse_order:
            if not database_ready:
                st.error(
                    "Order processing is unavailable until the database "
                    "connection is restored."
                )
            else:
                try:
                    parsed_order = parse_email_to_order_json(email_text)
                    st.session_state["parsed_order"] = parsed_order
                    _run_new_order(parsed_order)
                except Exception as error:
                    st.error(
                        f"The email order could not be parsed or processed: "
                        f"{error}"
                    )

        if st.session_state.get("parsed_order"):
            st.subheader("Parsed Order")
            st.json(st.session_state["parsed_order"])

    left_column, right_column = st.columns([1.35, 1])
    with left_column:
        st.subheader("Live Agent Status Log")
        workflow_events = st.session_state.get("workflow_events", [])
        if workflow_events:
            for event in workflow_events:
                label = NODE_LABELS.get(event["node"], event["node"])
                with st.expander(label, expanded=True):
                    if event["logs"]:
                        for log in event["logs"]:
                            st.write(f"- {log}")
                    else:
                        st.write(
                            "Agent completed without additional log entries."
                        )
        else:
            st.info(
                "Submit an order to see Intake → Spatial Routing → "
                "Rebalance (if needed) → Supervisor."
            )

    with right_column:
        st.subheader("Order Metrics")
        workflow_result = st.session_state.get("workflow_result")
        if workflow_result:
            metric_columns = st.columns(3)
            metric_columns[0].metric(
                "Priority Score",
                f"{workflow_result.get('priority_score', 0):.1f}",
            )
            assigned_warehouse = workflow_result.get("assigned_warehouse")
            metric_columns[1].metric(
                "Assigned Warehouse",
                assigned_warehouse.get("name", "—")
                if assigned_warehouse
                else "—",
            )
            metric_columns[2].metric(
                "Approval Status",
                workflow_result.get("approval_status", "—").replace("_", " "),
            )
            st.caption(
                f"Order #{workflow_result['order']['store_id']} · "
                f"{workflow_result['order']['quantity']:,} units · "
                f"{workflow_result['order']['urgency_level']} urgency"
            )
        else:
            st.metric("Priority Score", "—")
            st.metric("Assigned Warehouse", "—")
            st.metric("Approval Status", "—")

        if (
            workflow_result
            and workflow_result.get("approval_status")
            == "REQUIRES_HUMAN_APPROVAL"
        ):
            transfer = workflow_result["transfer_request"]
            st.warning(
                "Manager approval required for this transfer of "
                f"{transfer['quantity']:,} units from "
                f"{transfer['from_warehouse_name']} to warehouse "
                f"#{transfer['to_warehouse_id']}."
            )
            approve_column, reject_column = st.columns(2)
            if approve_column.button(
                "Approve Transfer", type="primary", width="stretch"
            ):
                try:
                    _resume_approval("approve")
                except Exception as error:
                    st.error(f"Transfer approval failed: {error}")
            if reject_column.button("Reject Transfer", width="stretch"):
                try:
                    _resume_approval("reject")
                except Exception as error:
                    st.error(f"Transfer rejection failed: {error}")

    st.divider()
    st.subheader("Incoming & Outgoing Order Rankings")
    ranking_orders = st.session_state.get("order_rankings", [])
    if ranking_orders:
        try:
            ranked_orders = rank_orders(ranking_orders)
            store_names = {
                store_id: name for store_id, name, _ in stores
            }
            ranking_rows = [
                {
                    "Rank": rank,
                    "Store Name": store_names.get(
                        order["store_id"], f"Store {order['store_id']}"
                    ),
                    "Units": order["quantity"],
                    "Urgency": order["urgency_level"],
                    "Priority Score": order["priority_score"],
                    "Fulfillment Status": order["fulfillment_status"],
                }
                for rank, order in enumerate(ranked_orders, start=1)
            ]
            st.dataframe(
                pd.DataFrame(
                    ranking_rows,
                    columns=[
                        "Rank",
                        "Store Name",
                        "Units",
                        "Urgency",
                        "Priority Score",
                        "Fulfillment Status",
                    ],
                ),
                hide_index=True,
                width="stretch",
            )
        except Exception as error:
            st.error(f"Could not rank processed orders: {error}")
    else:
        st.info("Parsed orders will appear here, ranked by priority.")

    st.divider()
    st.subheader("Warehouse Locations & Inventory")
    try:
        warehouse_data = _load_warehouse_inventory()
    except Exception as error:
        st.error(
            f"Could not load warehouse inventory; showing sample data: {error}"
        )
        warehouse_data = FALLBACK_WAREHOUSES.copy()

    if warehouse_data.empty:
        st.info("No warehouse records found; showing sample data.")
        warehouse_data = FALLBACK_WAREHOUSES.copy()

    map_column, table_column = st.columns([1, 1.4])
    with map_column:
        st.map(
            warehouse_data.dropna(subset=["latitude", "longitude"]),
            latitude="latitude",
            longitude="longitude",
            width="stretch",
        )
    with table_column:
        st.dataframe(
            warehouse_data[
                [
                    "warehouse_id",
                    "warehouse",
                    "city",
                    "type",
                    "product_name",
                    "stock_quantity",
                    "last_updated",
                ]
            ],
            hide_index=True,
            width="stretch",
        )


try:
    main()
except Exception as error:
    st.error(f"The dashboard encountered an unexpected error: {error}")

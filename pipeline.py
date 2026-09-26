"""
End-to-End Supply Chain Engine Pipeline for Parle-G.
====================================================
This script connects all 4 autonomous agents into a unified, deterministic,
multi-agent fulfillment pipeline for Parle-G retail orders:

1. Agent #1: Order Intake Agent       (agents/order_intake_agent.py)
   - Assigns priority tiers & scores based on customer types & inventory urgency.
2. Agent #2: Spatial Routing Agent     (agents/spatial_routing_agent.py)
   - Matches orders to regional warehouses, checks stock, and allocates inventory.
3. Agent #3: Rebalancing Agent         (agents/rebalancing_agent.py)
   - Handles stock shortages by creating inter-warehouse transfer requests.
4. Agent #4: Guardrail Agent           (agents/guardrail_agent.py)
   - Enforces transfer size thresholds (50,000 units) and Human-in-the-Loop gating.

Pipeline Execution Flow:
------------------------
Step 1: Load orders (data/orders.json) and warehouses (data/warehouses.json).
Step 2: Run Order Intake Agent -> adds priority_tier and priority_score.
Step 3: Sort orders by priority -> highest priority (lowest score) first.
Step 4: Run Spatial Routing Agent sequentially -> earlier orders claim stock first.
Step 5: Run Rebalancing Agent on 'insufficient_stock' orders -> creates transfers.
Step 6: Run Guardrail Agent on transfers -> sets 'auto_approved' or 'pending_manager_approval'.
Step 7: Produce final summary and overall metrics across all orders.

Determinism & Architecture:
---------------------------
- 100% deterministic rule-based execution. No external AI model calls or stochastic operations.
- Clean architectural handoffs between agents with complete auditability.
"""

import copy
import logging
from typing import Any, Dict, List, NamedTuple, Optional

# Import business logic directly from the 4 existing agents
from agents.order_intake_agent import load_orders, process_orders
from agents.spatial_routing_agent import load_warehouses, route_orders
from agents.rebalancing_agent import rebalance_orders
from agents.guardrail_agent import evaluate_transfers

# Configure pipeline logger
logger = logging.getLogger("supply_chain_pipeline")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class PipelineResult(NamedTuple):
    """
    Standard return container for the end-to-end pipeline execution.
    Supports tuple unpacking: orders, transfers, stats = run_pipeline(...)
    and attribute access: result.orders, result.transfers, result.stats
    """
    orders: List[Dict[str, Any]]
    transfers: List[Dict[str, Any]]
    stats: Dict[str, Any]


def determine_final_outcome(
    order: Dict[str, Any],
    transfers_by_id: Dict[str, Dict[str, Any]],
) -> str:
    """
    Determines the final business routing outcome for an order based on the
    combined state across Spatial Routing, Rebalancing, and Guardrail evaluations.

    Possible Final Outcomes:
    - 'fulfilled directly'        : Sourced immediately from local regional warehouse.
    - 'transfer auto-approved'    : Shortage resolved via transfer under 50,000 units.
    - 'transfer pending approval' : Shortage transfer >= 50,000 units requires manager sign-off.
    - 'transfer manager-approved' : Manager has manually signed off on a pending transfer.
    - 'no surplus available'      : Shortage cannot be bridged (no warehouse has surplus).
    - 'no warehouse for region'   : Order region has no assigned fulfillment warehouse.
    """
    routing_status = order.get("routing_status")

    # Outcome 1: Direct Regional Fulfillment
    if routing_status == "fulfilled_directly":
        return "fulfilled directly"

    # Outcome 2: Unserviced Region
    if routing_status == "no_warehouse_for_region":
        return "no warehouse for region"

    # Outcome 3: Shortage evaluated by Rebalancing and Guardrail agents
    if routing_status == "insufficient_stock":
        rebalancing_status = order.get("rebalancing_status")

        if rebalancing_status == "no_surplus_available":
            return "no surplus available"

        transfer_id = order.get("transfer_id")
        if transfer_id and transfer_id in transfers_by_id:
            transfer_status = transfers_by_id[transfer_id].get("status")
            if transfer_status == "auto_approved":
                return "transfer auto-approved"
            elif transfer_status == "pending_manager_approval":
                return "transfer pending approval"
            elif transfer_status == "manager_approved":
                return "transfer manager-approved"

        return "no surplus available"

    return str(routing_status)


def run_pipeline(
    orders: Optional[List[Dict[str, Any]]] = None,
    warehouses: Optional[List[Dict[str, Any]]] = None,
) -> PipelineResult:
    """
    Executes the 7-step end-to-end supply chain agentic pipeline.

    Parameters:
    -----------
    orders : Optional[List[Dict[str, Any]]]
        List of retail orders. If None, loads from data/orders.json.
    warehouses : Optional[List[Dict[str, Any]]]
        List of warehouse records. If None, loads from data/warehouses.json.

    Returns:
    --------
    PipelineResult
        NamedTuple containing:
        - orders: Final list of orders with priorities, routing statuses, and final outcomes.
        - transfers: Final list of evaluated transfer requests.
        - stats: Summary statistics dictionary.
    """
    # -------------------------------------------------------------------------
    # STEP 1: Load Data
    # -------------------------------------------------------------------------
    # Load orders from data/orders.json and warehouses from data/warehouses.json
    if orders is None:
        logger.info("Loading orders from data/orders.json...")
        orders = load_orders()
    if warehouses is None:
        logger.info("Loading warehouses from data/warehouses.json...")
        warehouses = load_warehouses()

    # Create deep copies of warehouses and orders to ensure clean state
    working_warehouses = copy.deepcopy(warehouses)
    working_orders = copy.deepcopy(orders)

    # -------------------------------------------------------------------------
    # STEP 2: Run Order Intake Agent (Priority Scoring)
    # -------------------------------------------------------------------------
    # Assigns priority_tier and priority_score to each order
    # Rules: Government (Tier 1) > NGO (Tier 2) > Loyal (Tier 3) > Regular (Tier 4) > Unknown (Tier 5)
    prioritized_orders = process_orders(working_orders)

    # -------------------------------------------------------------------------
    # STEP 3: Sort Orders by Priority
    # -------------------------------------------------------------------------
    # Sort orders by priority_score ascending (lower score = higher priority).
    # Higher-priority orders claim available stock before lower-priority ones.
    sorted_orders = sorted(
        prioritized_orders,
        key=lambda o: (o.get("priority_tier", 999), o.get("priority_score", 999.0)),
    )

    # -------------------------------------------------------------------------
    # STEP 4: Run Spatial Routing Agent Sequentially
    # -------------------------------------------------------------------------
    # Matches orders to their region's warehouse, one at a time in priority order.
    # Earlier orders get first claim on stock; stock is deducted when fulfilled.
    routed_orders = route_orders(orders=sorted_orders, warehouses=working_warehouses)
    for order in routed_orders:
        order["assigned_warehouse"] = order.get("assigned_warehouse_id")

    # -------------------------------------------------------------------------
    # STEP 5: Run Rebalancing Agent on Insufficient Stock Orders
    # -------------------------------------------------------------------------
    # Filters for orders where routing_status == "insufficient_stock".
    # Creates inter-warehouse transfer requests from surplus warehouses.
    rebalance_result = rebalance_orders(orders=routed_orders, warehouses=working_warehouses)
    rebalanced_orders = rebalance_result.orders
    raw_transfers = rebalance_result.transfers

    # -------------------------------------------------------------------------
    # STEP 6: Run Guardrail Agent on Generated Transfer Requests
    # -------------------------------------------------------------------------
    # Checks transfer sizes: quantity < 50,000 -> auto_approved;
    # quantity >= 50,000 -> pending_manager_approval.
    evaluated_transfers = evaluate_transfers(raw_transfers)

    # Index transfers by transfer_id for fast lookup in Step 7
    transfers_by_id = {t["transfer_id"]: t for t in evaluated_transfers}

    # -------------------------------------------------------------------------
    # STEP 7: Produce Final Summary for Every Order
    # -------------------------------------------------------------------------
    # Map final routing outcome to each order and calculate overall statistics
    final_orders: List[Dict[str, Any]] = []
    for order in rebalanced_orders:
        order_record = dict(order)
        final_outcome = determine_final_outcome(order_record, transfers_by_id)
        order_record["final_routing_outcome"] = final_outcome
        order_record["assigned_warehouse"] = order_record.get("assigned_warehouse_id")
        final_orders.append(order_record)

    # Compile Overall Pipeline Metrics
    count_fulfilled_directly = sum(
        1 for o in final_orders if o.get("final_routing_outcome") == "fulfilled directly"
    )
    count_auto_approved = sum(
        1 for t in evaluated_transfers if t.get("status") == "auto_approved"
    )
    count_pending_approval = sum(
        1 for t in evaluated_transfers if t.get("status") == "pending_manager_approval"
    )
    count_no_surplus = sum(
        1 for o in final_orders if o.get("final_routing_outcome") == "no surplus available"
    )
    count_no_warehouse = sum(
        1 for o in final_orders if o.get("final_routing_outcome") == "no warehouse for region"
    )
    count_unfulfillable = count_no_surplus + count_no_warehouse

    stats = {
        "total_orders": len(final_orders),
        "fulfilled_directly": count_fulfilled_directly,
        "transfers_created": len(evaluated_transfers),
        "auto_approved": count_auto_approved,
        "pending_manager_approval": count_pending_approval,
        "unfulfillable": count_unfulfillable,
        "no_surplus_available": count_no_surplus,
        "no_warehouse_for_region": count_no_warehouse,
    }

    # Print summary tables and overall statistics
    print_pipeline_summary(final_orders, evaluated_transfers, stats)

    return PipelineResult(orders=final_orders, transfers=evaluated_transfers, stats=stats)


def print_pipeline_summary(
    orders: List[Dict[str, Any]],
    transfers: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> None:
    """
    Prints a clear, readable summary table (order-by-order, sorted by priority)
    and an overall statistics summary at the end.
    """
    print("\n" + "=" * 120)
    print("                       PARLE-G AGENTIC AI SUPPLY CHAIN ENGINE: END-TO-END PIPELINE")
    print("=" * 120)

    # Order-by-order summary table
    header_orders = (
        f"{'#':<3} | {'Store ID':<14} | {'Customer Type':<14} | {'Tier':<6} | "
        f"{'SKU':<14} | {'Qty Req':<8} | {'Assigned WH':<14} | {'Final Routing Outcome':<26}"
    )
    print(header_orders)
    print("-" * 120)

    for idx, order in enumerate(orders, start=1):
        store_id = str(order.get("store_id", "N/A"))
        customer_type = str(order.get("customer_type", "N/A"))
        tier = f"Tier {order.get('priority_tier', 'N/A')}"
        sku = str(order.get("sku", "N/A"))
        qty = f"{order.get('quantity_requested', 0):,}"
        assigned_wh = str(order.get("assigned_warehouse") or "None")
        outcome = str(order.get("final_routing_outcome", "N/A"))

        row = (
            f"{idx:<3} | {store_id:<14} | {customer_type:<14} | {tier:<6} | "
            f"{sku:<14} | {qty:<8} | {assigned_wh:<14} | {outcome:<26}"
        )
        print(row)

    print("-" * 120)

    # Transfer requests summary table if any transfers were generated
    if transfers:
        print("\n" + "-" * 120)
        print("                                       INTER-WAREHOUSE TRANSFERS")
        print("-" * 120)
        header_tr = (
            f"{'#':<3} | {'Transfer ID':<12} | {'From WH':<14} | {'To WH':<14} | "
            f"{'SKU':<14} | {'Quantity':<10} | {'Guardrail Status':<26}"
        )
        print(header_tr)
        print("-" * 120)
        for idx, tr in enumerate(transfers, start=1):
            tr_id = str(tr.get("transfer_id", "N/A"))
            from_wh = str(tr.get("from_warehouse", "N/A"))
            to_wh = str(tr.get("to_warehouse", "N/A"))
            sku = str(tr.get("sku", "N/A"))
            qty = f"{tr.get('quantity', 0):,}"
            status = str(tr.get("status", "N/A"))

            row = (
                f"{idx:<3} | {tr_id:<12} | {from_wh:<14} | {to_wh:<14} | "
                f"{sku:<14} | {qty:<10} | {status:<26}"
            )
            print(row)
        print("-" * 120)

    # Overall Pipeline Stats Summary
    print("\n" + "=" * 120)
    print("                                      OVERALL PIPELINE METRICS")
    print("=" * 120)
    print(f"Total Orders Processed         : {stats.get('total_orders', 0)}")
    print(f" - Fulfilled Directly          : {stats.get('fulfilled_directly', 0)}")
    print(f" - Transfers Created           : {stats.get('transfers_created', 0)}")
    print(f"    * Auto-Approved (< 50k)    : {stats.get('auto_approved', 0)}")
    print(f"    * Pending Approval (>= 50k): {stats.get('pending_manager_approval', 0)}")
    print(f" - Unfulfillable Orders        : {stats.get('unfulfillable', 0)}")
    print(f"    * No Warehouse for Region  : {stats.get('no_warehouse_for_region', 0)}")
    print(f"    * No Surplus Available     : {stats.get('no_surplus_available', 0)}")
    print("=" * 120 + "\n")


if __name__ == "__main__":
    run_pipeline()

"""
Rebalancing Agent for Parle-G Supply Chain Engine.
==================================================
This module is Agent #3 in the Agentic AI Supply Chain Engine for Parle-G.
It handles inventory shortages identified by Agent #2 (Spatial Routing Agent).
When an order is flagged with routing_status == "insufficient_stock", this agent
evaluates the network of regional warehouses and determines whether stock can be
transferred from a surplus warehouse to resolve the shortage.

System Architecture Context:
----------------------------
1. Order Intake Agent      -> Assigns priority scores (Government > NGO > Loyal > Regular).
2. Spatial Routing Agent   -> Aligns orders to regional warehouses; flags stock shortages.
3. Rebalancing Agent (This)-> Creates inter-warehouse stock transfers to fulfill shortages.
4. Guardrail Agent (Next)  -> Evaluates transfers against business rules & approval policies.

Rebalancing Business Logic (Exact Rules):
-----------------------------------------
Rule 1: Filter on Shortages
        - Only process orders where routing_status == "insufficient_stock".
        - Ignore all other orders (pass them through unchanged).

Rule 2: Calculate Shortage Amount
        - shortage = quantity_requested - current available stock at the assigned warehouse for that SKU.
        - Accounts for any on-hand stock already present at the destination warehouse.

Rule 3: Scan Surplus Candidates
        - Scan all OTHER warehouses (excluding the assigned destination warehouse).
        - Find candidate warehouses where available stock of that SKU > safety buffer (safety buffer = 0).

Rule 4: Select Best Surplus Warehouse
        - Among candidates, pick the warehouse with the MOST available stock of that SKU
          (the largest surplus / greedy allocation).

Rule 5: Handle No Surplus Available
        - If no warehouse has any stock of that SKU (candidates list is empty),
          mark the order's rebalancing_status as "no_surplus_available" and do not create a transfer.

Rule 6: Create Transfer Request & Tag Order
        - If a surplus warehouse is found, create a transfer request record:
          * transfer_id   : Incrementing unique identifier (e.g., "TR-0001", "TR-0002")
          * from_warehouse: Source warehouse ID with largest surplus
          * to_warehouse  : Destination warehouse ID (originally assigned warehouse)
          * sku           : The requested SKU
          * quantity      : min(shortage, surplus warehouse available stock)
          * status        : "created"
        - Mark the order's rebalancing_status as "transfer_created" and attach transfer_id.

Rule 7: Synchronize Local Inventory State
        - Deduct transferred quantity from the source warehouse's stock_per_sku.
        - Add transferred quantity to the destination warehouse's stock_per_sku.
        - Guarantees sequential orders and subsequent pipeline stages observe consistent inventory.

Viva Voce / Technical Interview Notes:
--------------------------------------
Q: Why do we only transfer the shortage amount instead of the full quantity requested?
A: The destination warehouse may already hold partial inventory. Transferring only the delta
   (shortage = quantity_requested - on_hand_stock) minimizes logistics cost, transit risk,
   and unnecessary depletion of the source warehouse.

Q: Why prioritize the warehouse with the MOST stock (largest surplus)?
A: Greedy max-surplus allocation minimizes the risk of causing a secondary stockout at the
   supplying warehouse, preserving higher safety reserves across the supply chain network.

Q: Why update stock levels immediately during the mock execution?
A: Real-time inventory synchronization maintains ledger consistency. If multiple orders in the
   same batch require the same SKU, each subsequent order evaluates the updated surplus rather
   than double-allocating phantom inventory.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

# Set up dedicated logger for the rebalancing agent
logger = logging.getLogger("rebalancing_agent")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Status Constants
TARGET_ROUTING_STATUS = "insufficient_stock"
STATUS_TRANSFER_CREATED = "transfer_created"
STATUS_NO_SURPLUS_AVAILABLE = "no_surplus_available"
TRANSFER_STATUS_CREATED = "created"

# Configurable safety buffer (fixed at 0 per current capstone specification)
DEFAULT_SAFETY_BUFFER = 0


class RebalanceResult(NamedTuple):
    """
    Standard return container for rebalancing results.
    Inherits from NamedTuple to support both tuple unpacking:
        orders, transfers = rebalance_orders(...)
    and attribute access:
        result.orders, result.transfers
    """
    orders: List[Dict[str, Any]]
    transfers: List[Dict[str, Any]]


def get_default_warehouses_path() -> Path:
    """
    Locates the default warehouses.json file path.

    Checks:
    1. 'data/warehouses.json' relative to current working directory
    2. 'data/warehouses.json' relative to project root (parent of agents/)
    """
    cwd_path = Path.cwd() / "data" / "warehouses.json"
    if cwd_path.exists():
        return cwd_path

    script_root_path = Path(__file__).resolve().parent.parent / "data" / "warehouses.json"
    if script_root_path.exists():
        return script_root_path

    return cwd_path


def load_warehouses(file_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """
    Loads warehouse records from a JSON file.

    Parameters:
    -----------
    file_path : Optional[str | Path]
        Path to warehouses.json. If None, uses default data/warehouses.json.

    Returns:
    --------
    List[Dict[str, Any]]
        List of warehouse dictionaries containing warehouse_id, region_served,
        and stock_per_sku.
    """
    if file_path is None:
        target_path = get_default_warehouses_path()
    else:
        target_path = Path(file_path)

    if not target_path.exists():
        error_msg = f"Warehouses file not found at: {target_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    with open(target_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON format in {target_path}: {e}"
            logger.error(error_msg)
            raise ValueError(error_msg) from e

    if not isinstance(data, list):
        error_msg = f"Expected JSON list in {target_path}, got {type(data).__name__}"
        logger.error(error_msg)
        raise ValueError(error_msg)

    logger.info(f"Loaded {len(data)} warehouses from {target_path}.")
    return data


def find_best_surplus_warehouse(
    warehouses: List[Dict[str, Any]],
    assigned_warehouse_id: str,
    sku: str,
    safety_buffer: int = DEFAULT_SAFETY_BUFFER,
) -> Optional[Tuple[Dict[str, Any], int]]:
    """
    Rule 3 & Rule 4: Scans other warehouses and selects the candidate with the
    largest available surplus stock for the requested SKU.

    Parameters:
    -----------
    warehouses : List[Dict[str, Any]]
        List of all warehouse dictionaries in the supply chain network.
    assigned_warehouse_id : str
        The ID of the warehouse currently assigned to the order (to be excluded).
    sku : str
        The SKU identifier needing replenishment.
    safety_buffer : int
        Minimum stock that must remain untouched; surplus is any stock > safety_buffer.

    Returns:
    --------
    Optional[Tuple[Dict[str, Any], int]]
        A tuple of (selected_warehouse_dict, available_stock) if a candidate is found;
        None if no warehouse has stock above the safety buffer.
    """
    candidates: List[Tuple[Dict[str, Any], int]] = []

    # Rule 3: Scan all OTHER warehouses (excluding the assigned one)
    for wh in warehouses:
        wh_id = wh.get("warehouse_id")
        if wh_id == assigned_warehouse_id:
            continue  # Exclude the destination warehouse

        stock_map = wh.get("stock_per_sku", {})
        available_stock = stock_map.get(sku, 0)
        if available_stock is None:
            available_stock = 0

        # Must have stock greater than the safety buffer (default > 0)
        if available_stock > safety_buffer:
            candidates.append((wh, available_stock))

    # Rule 5 check: No surplus candidate available
    if not candidates:
        return None

    # Rule 4: Pick candidate with the MOST available stock (largest surplus)
    # Python's max selects the item with highest stock; ties resolve stably
    best_candidate = max(candidates, key=lambda item: item[1])
    return best_candidate


def rebalance_single_order(
    order: Dict[str, Any],
    warehouses: List[Dict[str, Any]],
    warehouse_id_map: Dict[str, Dict[str, Any]],
    transfer_counter: int,
    safety_buffer: int = DEFAULT_SAFETY_BUFFER,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
    """
    Applies the rebalancing rules to a single order.

    Parameters:
    -----------
    order : Dict[str, Any]
        The incoming order dictionary (already processed by spatial routing).
    warehouses : List[Dict[str, Any]]
        List of all warehouse records.
    warehouse_id_map : Dict[str, Dict[str, Any]]
        Fast lookup mapping from warehouse_id to warehouse record.
    transfer_counter : int
        Current incrementing sequence number for generating transfer IDs.
    safety_buffer : int
        Minimum stock threshold for surplus eligibility.

    Returns:
    --------
    Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]
        - Updated order dictionary (with rebalancing_status and transfer_id if created).
        - Transfer request dictionary if created, else None.
        - Updated transfer_counter.
    """
    # Create shallow copy of order to preserve all incoming fields
    processed_order = dict(order)
    store_id = processed_order.get("store_id", "Unknown")
    routing_status = processed_order.get("routing_status")

    # Rule 1: Only process orders where routing_status == "insufficient_stock"
    # Ignore all other orders (pass them through completely unchanged)
    if routing_status != TARGET_ROUTING_STATUS:
        return processed_order, None, transfer_counter

    assigned_wh_id = processed_order.get("assigned_warehouse_id")
    sku = processed_order.get("sku")
    quantity_requested = processed_order.get("quantity_requested", 0)

    # Validate that an assigned warehouse ID exists
    assigned_wh = warehouse_id_map.get(assigned_wh_id) if assigned_wh_id else None
    if assigned_wh is None:
        logger.warning(
            f"Order '{store_id}' marked as '{TARGET_ROUTING_STATUS}' but has invalid "
            f"assigned_warehouse_id: {assigned_wh_id}."
        )
        processed_order["rebalancing_status"] = STATUS_NO_SURPLUS_AVAILABLE
        return processed_order, None, transfer_counter

    # Rule 2: Calculate the shortage amount
    # shortage = quantity_requested - current available stock at assigned warehouse for SKU
    dest_stock_map = assigned_wh.setdefault("stock_per_sku", {})
    current_dest_stock = dest_stock_map.get(sku, 0)
    if current_dest_stock is None:
        current_dest_stock = 0

    shortage = quantity_requested - current_dest_stock

    # If current stock somehow already meets or exceeds quantity requested, no shortage exists
    if shortage <= 0:
        logger.info(
            f"Order '{store_id}' at '{assigned_wh_id}' has on-hand stock ({current_dest_stock}) "
            f">= requested ({quantity_requested}). No transfer needed."
        )
        processed_order["rebalancing_status"] = STATUS_NO_SURPLUS_AVAILABLE
        return processed_order, None, transfer_counter

    # Rule 3 & Rule 4: Scan all other warehouses and pick one with the largest surplus
    surplus_result = find_best_surplus_warehouse(
        warehouses=warehouses,
        assigned_warehouse_id=assigned_wh_id,
        sku=sku,
        safety_buffer=safety_buffer,
    )

    # Rule 5: If no warehouse has any stock of that SKU, mark no_surplus_available
    if surplus_result is None:
        logger.warning(
            f"Order '{store_id}': No surplus warehouse found carrying SKU '{sku}'. "
            f"Shortage of {shortage} units cannot be fulfilled."
        )
        processed_order["rebalancing_status"] = STATUS_NO_SURPLUS_AVAILABLE
        return processed_order, None, transfer_counter

    source_wh, available_surplus = surplus_result
    from_wh_id = source_wh.get("warehouse_id")

    # Rule 6: Determine transfer quantity
    # Quantity is smaller of shortage amount OR surplus warehouse available stock
    transfer_quantity = min(shortage, available_surplus)

    # Generate sequential transfer ID (e.g., TR-0001, TR-0002)
    transfer_id = f"TR-{transfer_counter:04d}"
    transfer_counter += 1

    # Construct the transfer request with exact required fields
    transfer_request = {
        "transfer_id": transfer_id,
        "from_warehouse": from_wh_id,
        "to_warehouse": assigned_wh_id,
        "sku": sku,
        "quantity": transfer_quantity,
        "status": TRANSFER_STATUS_CREATED,
    }

    # Tag the order
    processed_order["rebalancing_status"] = STATUS_TRANSFER_CREATED
    processed_order["transfer_id"] = transfer_id

    # Rule 7: Actually update stock_per_sku in both warehouses to reflect the transfer
    # Deduct from source warehouse
    source_wh["stock_per_sku"][sku] = available_surplus - transfer_quantity

    # Add to destination warehouse
    dest_stock_map[sku] = current_dest_stock + transfer_quantity

    logger.info(
        f"Order '{store_id}': Created {transfer_id} moving {transfer_quantity}x '{sku}' "
        f"from '{from_wh_id}' to '{assigned_wh_id}'. Source remaining: "
        f"{source_wh['stock_per_sku'][sku]}, Dest updated: {dest_stock_map[sku]}."
    )

    return processed_order, transfer_request, transfer_counter


def rebalance_orders(
    orders: List[Dict[str, Any]],
    warehouses: Optional[List[Dict[str, Any]]] = None,
    safety_buffer: int = DEFAULT_SAFETY_BUFFER,
    start_transfer_id: int = 1,
) -> RebalanceResult:
    """
    Main entry point for the Rebalancing Agent.
    Accepts already-routed orders, loads or accepts warehouses, and executes
    deterministic inventory rebalancing rules.

    Parameters:
    -----------
    orders : List[Dict[str, Any]]
        List of orders that have already passed through spatial routing.
        Orders contain routing_status, assigned_warehouse_id, sku, quantity_requested,
        and original metadata fields.
    warehouses : Optional[List[Dict[str, Any]]]
        List of warehouse dictionaries. If None, loads from data/warehouses.json.
    safety_buffer : int
        Minimum stock threshold for surplus candidates (default 0).
    start_transfer_id : int
        Starting sequence integer for transfer IDs (default 1 -> "TR-0001").

    Returns:
    --------
    RebalanceResult
        NamedTuple containing:
        - orders: Full list of orders with affected orders tagged with rebalancing_status.
        - transfers: List of created transfer request dictionaries.
    """
    if warehouses is None:
        warehouses = load_warehouses()

    if not orders:
        logger.info("No orders provided for rebalancing.")
        return RebalanceResult(orders=[], transfers=[])

    # Index warehouses by warehouse_id for O(1) destination lookups
    warehouse_id_map: Dict[str, Dict[str, Any]] = {
        wh.get("warehouse_id"): wh for wh in warehouses if wh.get("warehouse_id")
    }

    processed_orders: List[Dict[str, Any]] = []
    transfers: List[Dict[str, Any]] = []
    transfer_counter = start_transfer_id

    # Process each order sequentially
    for order in orders:
        updated_order, transfer, transfer_counter = rebalance_single_order(
            order=order,
            warehouses=warehouses,
            warehouse_id_map=warehouse_id_map,
            transfer_counter=transfer_counter,
            safety_buffer=safety_buffer,
        )
        processed_orders.append(updated_order)
        if transfer is not None:
            transfers.append(transfer)

    logger.info(
        f"Rebalancing completed: {len(transfers)} transfers created across "
        f"{len(processed_orders)} total orders evaluated."
    )

    return RebalanceResult(orders=processed_orders, transfers=transfers)


# Alias for alternative semantic invocation
rebalance_inventory = rebalance_orders


def print_rebalancing_summary(
    orders: List[Dict[str, Any]],
    transfers: List[Dict[str, Any]],
) -> None:
    """
    Prints a clear, beginner-friendly table of affected orders and generated transfers.
    Suitable for demonstrations and viva presentations.
    """
    print("\n" + "=" * 108)
    print("             PARLE-G AGENTIC AI SUPPLY CHAIN ENGINE: INVENTORY REBALANCING")
    print("=" * 108)

    # 1. Orders Table
    header_orders = (
        f"{'#':<3} | {'Store ID':<14} | {'SKU':<14} | {'Qty Req':<8} | "
        f"{'Assigned WH':<14} | {'Rebalancing Status':<24} | {'Transfer ID':<12}"
    )
    print(header_orders)
    print("-" * 108)

    affected_count = 0
    for idx, order in enumerate(orders, start=1):
        status = order.get("rebalancing_status")
        # Display all orders or highlight affected ones
        store_id = str(order.get("store_id", "N/A"))
        sku = str(order.get("sku", "N/A"))
        qty = str(order.get("quantity_requested", 0))
        assigned_wh = str(order.get("assigned_warehouse_id") or "None")
        status_str = str(status if status else "(passed through)")
        transfer_id = str(order.get("transfer_id") or "None")

        if status:
            affected_count += 1

        row = (
            f"{idx:<3} | {store_id:<14} | {sku:<14} | {qty:<8} | "
            f"{assigned_wh:<14} | {status_str:<24} | {transfer_id:<12}"
        )
        print(row)

    print("-" * 108)
    print(f"Total Orders: {len(orders)} | Affected (Shortage) Orders Processed: {affected_count}")

    # 2. Transfer Requests Table
    print("\n" + "-" * 108)
    print("                              GENERATED INTER-WAREHOUSE TRANSFERS")
    print("-" * 108)

    if not transfers:
        print("  No transfer requests created (no eligible shortages or surplus available).")
    else:
        header_tr = (
            f"{'#':<3} | {'Transfer ID':<12} | {'From Warehouse':<16} | {'To Warehouse':<16} | "
            f"{'SKU':<14} | {'Quantity':<10} | {'Status':<10}"
        )
        print(header_tr)
        print("-" * 108)

        for idx, tr in enumerate(transfers, start=1):
            tr_id = str(tr.get("transfer_id", "N/A"))
            from_wh = str(tr.get("from_warehouse", "N/A"))
            to_wh = str(tr.get("to_warehouse", "N/A"))
            tr_sku = str(tr.get("sku", "N/A"))
            tr_qty = str(tr.get("quantity", 0))
            tr_status = str(tr.get("status", "N/A"))

            row = (
                f"{idx:<3} | {tr_id:<12} | {from_wh:<16} | {to_wh:<16} | "
                f"{tr_sku:<14} | {tr_qty:<10} | {tr_status:<10}"
            )
            print(row)

    print("=" * 108)
    print("Rebalancing Status Descriptions:")
    print(" - 'transfer_created'     : Shortage successfully bridged via transfer from surplus warehouse.")
    print(" - 'no_surplus_available' : No other warehouse carries surplus stock of this SKU.")
    print(" - '(passed through)'     : Order was not 'insufficient_stock' (e.g. fulfilled_directly) and left untouched.")
    print("=" * 108 + "\n")


def demo_rebalancing() -> RebalanceResult:
    """
    Self-contained demonstration for manual execution, testing, and viva evaluation.
    Constructs sample orders with 'insufficient_stock' and sample warehouses,
    applies the rebalancing logic, and prints detailed summary tables.
    """
    print("\n>>> Running Self-Contained Parle-G Rebalancing Agent Demo...")

    # Sample Warehouses
    mock_warehouses = [
        {
            "warehouse_id": "WH-WEST-01",
            "region_served": "West",
            "stock_per_sku": {
                "PARLE-G-250G": 5000,  # Plentiful surplus for 250G
                "PARLE-G-800G": 60,    # Limited stock for 800G
                "PARLE-G-100G": 8000,
            },
        },
        {
            "warehouse_id": "WH-NORTH-01",
            "region_served": "North",
            "stock_per_sku": {
                "PARLE-G-250G": 30,    # Has 30 on hand, but order needs 100
                "PARLE-G-100G": 1000,
            },
        },
        {
            "warehouse_id": "WH-SOUTH-01",
            "region_served": "South",
            "stock_per_sku": {
                "PARLE-G-800G": 20,    # Has 20 on hand, but order needs 120
                "PARLE-G-100G": 4000,
            },
        },
    ]

    # Sample Orders (already through Spatial Routing)
    mock_routed_orders = [
        # Order 1: Shortage of 70 units of 250G at WH-NORTH-01 (WH-WEST-01 has 5000 surplus)
        # Expected: TR-0001 for 70 units from WH-WEST-01 -> WH-NORTH-01; status: transfer_created
        {
            "store_id": "STORE-NORTH-01",
            "region": "North",
            "customer_type": "Regular",
            "sku": "PARLE-G-250G",
            "quantity_requested": 100,
            "routing_status": "insufficient_stock",
            "assigned_warehouse_id": "WH-NORTH-01",
            "priority_tier": 4,
            "priority_score": 4.001,
        },
        # Order 2: SKU PARLE-G-500G not carried in any warehouse
        # Expected: No transfer; status: no_surplus_available
        {
            "store_id": "STORE-NORTH-02",
            "region": "North",
            "customer_type": "NGO",
            "sku": "PARLE-G-500G",
            "quantity_requested": 250,
            "routing_status": "insufficient_stock",
            "assigned_warehouse_id": "WH-NORTH-01",
            "priority_tier": 2,
            "priority_score": 2.0,
        },
        # Order 3: Shortage of 100 units of 800G at WH-SOUTH-01 (WH-WEST-01 only has 60 surplus)
        # Expected: Partial transfer of 60 units (all available) from WH-WEST-01 -> WH-SOUTH-01; status: transfer_created
        {
            "store_id": "STORE-SOUTH-03",
            "region": "South",
            "customer_type": "Government",
            "sku": "PARLE-G-800G",
            "quantity_requested": 120,
            "routing_status": "insufficient_stock",
            "assigned_warehouse_id": "WH-SOUTH-01",
            "priority_tier": 1,
            "priority_score": 1.0,
        },
        # Order 4: Already fulfilled directly in spatial routing
        # Expected: Ignored by rebalancing; passed through unchanged
        {
            "store_id": "STORE-WEST-04",
            "region": "West",
            "customer_type": "Loyal",
            "sku": "PARLE-G-100G",
            "quantity_requested": 500,
            "routing_status": "fulfilled_directly",
            "assigned_warehouse_id": "WH-WEST-01",
            "priority_tier": 3,
            "priority_score": 3.0,
        },
    ]

    # Execute rebalancing
    result = rebalance_orders(orders=mock_routed_orders, warehouses=mock_warehouses)

    # Print summary tables
    print_rebalancing_summary(orders=result.orders, transfers=result.transfers)

    # Print updated warehouse inventory states to demonstrate Rule 7
    print("Warehouse Stock Levels After Transfers (Rule 7 Invariant Check):")
    for wh in mock_warehouses:
        print(f" - {wh['warehouse_id']} ({wh['region_served']}): {wh['stock_per_sku']}")
    print("-" * 108 + "\n")

    return result


if __name__ == "__main__":
    demo_rebalancing()

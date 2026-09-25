"""
Spatial Routing Agent for Parle-G Supply Chain Engine.
======================================================
This module is Agent #2 in the Agentic AI Supply Chain Engine for Parle-G.
It determines which regional warehouse should fulfill each incoming retail order,
based on exact regional alignment and SKU inventory levels.

Business Routing Rules:
-----------------------
Rule 1: Look up the order's 'region' field.
Rule 2: Find the regional warehouse whose 'region_served' exactly matches the order's region.
Rule 3: If no warehouse serves that region:
        - Mark routing_status as "no_warehouse_for_region".
        - Set assigned_warehouse_id to None.
        - Stop processing that order (flag it safely without crashing).
Rule 4: If a matching warehouse is found, check 'stock_per_sku' for the order's requested SKU.
Rule 5: If available stock >= quantity_requested:
        - Mark routing_status as "fulfilled_directly".
        - Record assigned_warehouse_id.
        - Deduct quantity_requested from that warehouse's stock_per_sku for that SKU.
Rule 6: If available stock < quantity_requested (or if SKU is missing entirely, treated as 0):
        - Mark routing_status as "insufficient_stock".
        - Record assigned_warehouse_id anyway (so the downstream Rebalancing Agent can resolve it).
        - Do NOT deduct stock when insufficient.

Independence & Determinism:
---------------------------
- This agent uses pure deterministic lookup and comparison logic (no AI/LLM, no GPS/distance formulas).
- Operates independently from order_intake_agent.py and accepts raw or pre-scored orders.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

# Set up dedicated logger for the spatial routing agent
logger = logging.getLogger("spatial_routing_agent")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Routing Status Constants
STATUS_FULFILLED_DIRECTLY = "fulfilled_directly"
STATUS_INSUFFICIENT_STOCK = "insufficient_stock"
STATUS_NO_WAREHOUSE_FOR_REGION = "no_warehouse_for_region"


def get_default_file_path(filename: str) -> Path:
    """
    Finds the default path for a data file (orders.json or warehouses.json).
    
    Checks:
    1. 'data/<filename>' in current working directory
    2. 'data/<filename>' relative to the project root (parent of agents/)
    """
    cwd_path = Path.cwd() / "data" / filename
    if cwd_path.exists():
        return cwd_path

    # Check relative to this script (agents/ -> project_root / data / filename)
    script_root_path = Path(__file__).resolve().parent.parent / "data" / filename
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
        target_path = get_default_file_path("warehouses.json")
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


def load_orders(file_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """
    Loads retail order records from a JSON file.
    Allows spatial_routing_agent to run independently without importing order_intake_agent.

    Parameters:
    -----------
    file_path : Optional[str | Path]
        Path to orders.json. If None, uses default data/orders.json.

    Returns:
    --------
    List[Dict[str, Any]]
        List of order dictionaries.
    """
    if file_path is None:
        target_path = get_default_file_path("orders.json")
    else:
        target_path = Path(file_path)

    if not target_path.exists():
        error_msg = f"Orders file not found at: {target_path}"
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

    logger.info(f"Loaded {len(data)} orders from {target_path}.")
    return data


def build_warehouse_region_map(warehouses: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Builds a quick lookup dictionary mapping region_served -> warehouse object.
    
    Parameters:
    -----------
    warehouses : List[Dict[str, Any]]
        List of warehouse dictionaries.

    Returns:
    --------
    Dict[str, Dict[str, Any]]
        Dictionary where key is region_served and value is the warehouse dict.
    """
    region_map: Dict[str, Dict[str, Any]] = {}
    for wh in warehouses:
        region = wh.get("region_served")
        if region:
            region_map[region] = wh
    return region_map


def route_single_order(
    order: Dict[str, Any],
    warehouse_region_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Applies the routing rules to a single order.

    Parameters:
    -----------
    order : Dict[str, Any]
        The order dictionary to route.
    warehouse_region_map : Dict[str, Dict[str, Any]]
        Mapping from region string to warehouse dictionary.

    Returns:
    --------
    Dict[str, Any]
        A copy of the order with 'routing_status' and 'assigned_warehouse_id' assigned.
    """
    # Create a shallow copy of order to preserve original fields
    routed_order = dict(order)
    store_id = routed_order.get("store_id", "Unknown")

    # Rule 1: Look up order's region
    order_region = routed_order.get("region")

    # Rule 2: Find the warehouse serving this exact region
    warehouse = warehouse_region_map.get(order_region) if order_region else None

    # Rule 3: Check if no warehouse serves this region
    if warehouse is None:
        logger.warning(
            f"Order from store '{store_id}': No warehouse found serving region '{order_region}'."
        )
        routed_order["routing_status"] = STATUS_NO_WAREHOUSE_FOR_REGION
        routed_order["assigned_warehouse_id"] = None
        # Stop processing this order further per Rule 3
        return routed_order

    # A matching regional warehouse was found
    assigned_wh_id = warehouse.get("warehouse_id")
    routed_order["assigned_warehouse_id"] = assigned_wh_id

    # Rule 4: Check stock_per_sku for the requested SKU
    sku = routed_order.get("sku")
    quantity_requested = routed_order.get("quantity_requested", 0)

    # Missing SKU in stock_per_sku is treated as 0 available stock (Rule 6)
    stock_dict = warehouse.setdefault("stock_per_sku", {})
    available_stock = stock_dict.get(sku, 0)
    if available_stock is None:
        available_stock = 0

    # Rule 5: Sufficient stock -> fulfilled_directly and deduct stock
    if available_stock >= quantity_requested:
        routed_order["routing_status"] = STATUS_FULFILLED_DIRECTLY
        # Deduct requested quantity from warehouse inventory
        stock_dict[sku] = available_stock - quantity_requested
        logger.info(
            f"Order '{store_id}' fulfilled directly by '{assigned_wh_id}'. "
            f"SKU '{sku}': {available_stock} -> {stock_dict[sku]} units remaining."
        )
    else:
        # Rule 6: Insufficient stock (or SKU not carried) -> flag shortage
        # Record assigned_warehouse_id anyway so downstream Rebalancing Agent can rebalance it
        routed_order["routing_status"] = STATUS_INSUFFICIENT_STOCK
        logger.warning(
            f"Order '{store_id}': Insufficient stock at warehouse '{assigned_wh_id}' "
            f"for SKU '{sku}'. Requested {quantity_requested}, available {available_stock}."
        )
        # Note: Do not deduct inventory when stock is insufficient

    return routed_order


def route_orders(
    orders: Optional[List[Dict[str, Any]]] = None,
    warehouses: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Main entry point for routing a batch of orders.

    Parameters:
    -----------
    orders : Optional[List[Dict[str, Any]]]
        List of orders to route. If None, loads from data/orders.json.
    warehouses : Optional[List[Dict[str, Any]]]
        List of warehouse records. If None, loads from data/warehouses.json.

    Returns:
    --------
    List[Dict[str, Any]]
        List of routed orders, each containing 'routing_status' and 'assigned_warehouse_id'.
    """
    # Load default data files if not explicitly provided
    if orders is None:
        orders = load_orders()
    if warehouses is None:
        warehouses = load_warehouses()

    if not orders:
        logger.info("No orders to route.")
        return []

    # Build region lookup map
    warehouse_map = build_warehouse_region_map(warehouses)

    routed_orders: List[Dict[str, Any]] = []

    # Process each order sequentially
    for order in orders:
        routed = route_single_order(order, warehouse_map)
        routed_orders.append(routed)

    logger.info(f"Finished routing {len(routed_orders)} orders.")
    return routed_orders


def print_routing_table(routed_orders: List[Dict[str, Any]]) -> None:
    """
    Prints a clear, beginner-friendly summary table of order routing decisions.
    """
    if not routed_orders:
        print("No routed orders to display.")
        return

    print("\n" + "=" * 98)
    print("             PARLE-G AGENTIC AI SUPPLY CHAIN ENGINE: SPATIAL ROUTING")
    print("=" * 98)
    header = (
        f"{'#':<3} | {'Store ID':<14} | {'Region':<8} | {'SKU':<14} | "
        f"{'Qty':<6} | {'Assigned WH':<14} | {'Routing Status':<24}"
    )
    print(header)
    print("-" * 98)

    for idx, order in enumerate(routed_orders, start=1):
        store_id = str(order.get("store_id", "N/A"))
        region = str(order.get("region", "N/A"))
        sku = str(order.get("sku", "N/A"))
        qty = str(order.get("quantity_requested", 0))
        assigned_wh = str(order.get("assigned_warehouse_id") or "None")
        status = str(order.get("routing_status", "N/A"))

        row = (
            f"{idx:<3} | {store_id:<14} | {region:<8} | {sku:<14} | "
            f"{qty:<6} | {assigned_wh:<14} | {status:<24}"
        )
        print(row)

    print("=" * 98)
    print("Routing Status Summary:")
    print(" - 'fulfilled_directly': Matching warehouse has sufficient stock. Quantity deducted.")
    print(" - 'insufficient_stock': Warehouse assigned, but stock < requested. (Handled by Rebalancing Agent)")
    print(" - 'no_warehouse_for_region': No warehouse exists for this region.")
    print("=" * 98 + "\n")


def main() -> List[Dict[str, Any]]:
    """
    CLI execution entry point. Loads orders and warehouses from data/,
    routes every order, prints the summary table, and returns the routed orders.
    """
    print("Starting Parle-G Spatial Routing Agent...")
    try:
        orders = load_orders()
        warehouses = load_warehouses()
    except Exception as e:
        logger.error(f"Failed to load data for spatial routing: {e}")
        return []

    routed_orders = route_orders(orders=orders, warehouses=warehouses)
    print_routing_table(routed_orders)
    return routed_orders


if __name__ == "__main__":
    main()

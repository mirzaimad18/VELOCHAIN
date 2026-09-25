"""
Order Intake Agent for Parle-G Supply Chain Engine.
===================================================
This module is Agent #1 in the Agentic AI Supply Chain Engine for Parle-G.
It reads incoming retail orders from a local JSON file (data/orders.json)
and deterministically assigns priority tiers and priority scores based on
business rules.

Business Priority Rules:
------------------------
Rule 1: Government customers always rank highest (Priority Tier 1).
Rule 2: NGO customers rank second (Priority Tier 2).
Rule 3: Loyal customers rank third (Priority Tier 3) - fixed tag from customer_type.
Rule 4: Regular customers rank fourth (Priority Tier 4). Among Regular customers,
        the lower the current_stock_level, the more urgent (higher priority) the order.
Rule 5: If two Regular orders have identical current_stock_level, break the tie
        using order_timestamp - earlier timestamp wins (higher priority).
Rule 6: If customer_type is unrecognized, log a warning and assign lowest priority
        (Priority Tier 5) rather than crashing.

Priority Scoring Convention:
----------------------------
Each order is assigned:
- priority_tier (int): 1 (Government), 2 (NGO), 3 (Loyal), 4 (Regular), 5 (Unrecognized).
- priority_score (float): A single sortable numeric value where a LOWER number indicates
  HIGHER priority (similar to rank 1 being top priority).
  * Tier 1 (Government)  -> 1.0
  * Tier 2 (NGO)         -> 2.0
  * Tier 3 (Loyal)       -> 3.0
  * Tier 4 (Regular)     -> 4.0 + (urgency_rank * 0.001)
    where urgency_rank is 1 for the most urgent Regular order, 2 for the second, etc.
    Urgency is ordered by:
      1) current_stock_level (ascending: lower stock = higher urgency)
      2) order_timestamp (ascending: earlier timestamp = tiebreaker winner)
  * Tier 5 (Unrecognized) -> 5.0 (lowest priority)
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Set up logger for the order intake agent
logger = logging.getLogger("order_intake_agent")
if not logger.handlers:
    # Set default level to INFO and configure a clear output format
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Mapping of standard customer types to priority tiers
# Tier 1 = Highest priority, Tier 4 = Regular customer tier
KNOWN_CUSTOMER_TIERS: Dict[str, int] = {
    "Government": 1,
    "NGO": 2,
    "Loyal": 3,
    "Regular": 4,
}

# Tier assigned to unrecognized customer types as a safe fallback
UNKNOWN_CUSTOMER_TIER: int = 5


def get_default_data_path() -> Path:
    """
    Locates the default orders.json file path.
    
    Checks:
    1. 'data/orders.json' relative to current working directory
    2. 'data/orders.json' relative to the project root directory
    """
    cwd_path = Path.cwd() / "data" / "orders.json"
    if cwd_path.exists():
        return cwd_path

    # Check relative to this script's directory (agents/ -> parent is project root)
    script_root_path = Path(__file__).resolve().parent.parent / "data" / "orders.json"
    if script_root_path.exists():
        return script_root_path

    # Fallback to standard relative path
    return cwd_path


def load_orders(file_path: Optional[str | Path] = None) -> List[Dict[str, Any]]:
    """
    Loads retail orders from a JSON file.

    Parameters:
    -----------
    file_path : Optional[str | Path]
        Path to the JSON file containing orders. If None, uses default data/orders.json.

    Returns:
    --------
    List[Dict[str, Any]]
        A list of order dictionaries parsed from the JSON file.

    Raises:
    -------
    FileNotFoundError
        If the specified JSON file does not exist.
    ValueError
        If the JSON file contains invalid JSON or is not a list.
    """
    if file_path is None:
        target_path = get_default_data_path()
    else:
        target_path = Path(file_path)

    if not target_path.exists():
        error_msg = f"Orders file not found at: {target_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    logger.info(f"Loading orders from: {target_path}")

    with open(target_path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON format in {target_path}: {e}"
            logger.error(error_msg)
            raise ValueError(error_msg) from e

    if not isinstance(data, list):
        error_msg = f"Expected a JSON list of orders, but got {type(data).__name__}"
        logger.error(error_msg)
        raise ValueError(error_msg)

    logger.info(f"Successfully loaded {len(data)} orders.")
    return data


def parse_timestamp(timestamp_str: Optional[str]) -> datetime:
    """
    Parses an ISO 8601 timestamp string into a datetime object for tie-breaking.
    If parsing fails or timestamp is missing, returns datetime.max as a safe fallback.

    Parameters:
    -----------
    timestamp_str : Optional[str]
        ISO 8601 timestamp string (e.g., '2026-09-25T08:00:00Z').

    Returns:
    --------
    datetime
        Parsed datetime object.
    """
    if not timestamp_str:
        logger.warning("Order has missing timestamp. Using fallback latest time.")
        return datetime.max

    try:
        # datetime.fromisoformat handles standard ISO strings (including 'Z' in Python 3.11+)
        # If timestamp ends with 'Z', normalize to '+00:00' for universal compatibility
        normalized = timestamp_str.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception as e:
        logger.warning(f"Could not parse timestamp '{timestamp_str}': {e}. Using fallback.")
        return datetime.max


def get_customer_tier(customer_type: Any, store_id: str = "Unknown") -> Tuple[int, bool]:
    """
    Determines the priority tier based on the customer type.
    
    Implements:
    - Rule 1: Government -> Tier 1
    - Rule 2: NGO -> Tier 2
    - Rule 3: Loyal -> Tier 3
    - Rule 4: Regular -> Tier 4
    - Rule 6: Unrecognized -> Log warning and return Tier 5 (lowest priority)

    Parameters:
    -----------
    customer_type : Any
        The customer type string from the order.
    store_id : str
        Store ID for informative warning logging.

    Returns:
    --------
    Tuple[int, bool]
        A tuple of (priority_tier, is_recognized).
    """
    if isinstance(customer_type, str) and customer_type in KNOWN_CUSTOMER_TIERS:
        tier = KNOWN_CUSTOMER_TIERS[customer_type]
        return tier, True
    else:
        # Rule 6: Unrecognized customer_type edge case
        logger.warning(
            f"Unrecognized customer_type '{customer_type}' for store '{store_id}'. "
            f"Assigning lowest priority (Tier {UNKNOWN_CUSTOMER_TIER})."
        )
        return UNKNOWN_CUSTOMER_TIER, False


def rank_regular_orders(orders: List[Dict[str, Any]]) -> Dict[int, int]:
    """
    Ranks Regular customer orders among themselves by urgency according to Rules 4 & 5.
    
    Urgency Rules for Regular Orders:
    1. Lower current_stock_level = More urgent (Rule 4).
    2. Identical current_stock_level = Earlier order_timestamp wins (Rule 5).

    Parameters:
    -----------
    orders : List[Dict[str, Any]]
        List of all orders.

    Returns:
    --------
    Dict[int, int]
        Mapping from order's original index (or id) to its urgency_rank (1, 2, 3, ...).
    """
    # Collect tuples of: (original_index, stock_level, parsed_timestamp)
    regular_entries = []
    for idx, order in enumerate(orders):
        customer_type = order.get("customer_type")
        if customer_type == "Regular":
            # Extract stock level; default to 0 if missing
            stock_level = order.get("current_stock_level", 0)
            if stock_level is None:
                stock_level = 0
            
            # Parse timestamp for tiebreaking
            timestamp_dt = parse_timestamp(order.get("order_timestamp"))
            regular_entries.append((idx, stock_level, timestamp_dt))

    # Sort regular entries:
    # 1. stock_level ascending (lower stock comes first -> more urgent)
    # 2. timestamp_dt ascending (earlier timestamp comes first -> tiebreaker)
    regular_entries.sort(key=lambda item: (item[1], item[2]))

    # Assign urgency rank: 1 is most urgent, 2 is second most urgent, etc.
    urgency_rankings: Dict[int, int] = {}
    for rank, (orig_idx, _, _) in enumerate(regular_entries, start=1):
        urgency_rankings[orig_idx] = rank

    return urgency_rankings


def process_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Main processing function for the Order Intake Agent.
    
    Takes a raw list of orders, determines the priority tier and priority score
    for each order according to business rules, and returns the full list of
    orders sorted by final priority (highest priority first).

    Parameters:
    -----------
    orders : List[Dict[str, Any]]
        Raw list of order dictionaries loaded from JSON.

    Returns:
    --------
    List[Dict[str, Any]]
        Processed orders with 'priority_tier' and 'priority_score' fields,
        sorted from highest priority to lowest priority.
    """
    if not orders:
        logger.info("No orders to process.")
        return []

    # Step 1: Calculate urgency rankings among Regular orders (Rules 4 & 5)
    urgency_rankings = rank_regular_orders(orders)

    processed_orders: List[Dict[str, Any]] = []

    # Step 2: Assign priority tier and priority score to each order
    for idx, raw_order in enumerate(orders):
        order = dict(raw_order)  # Create a shallow copy to avoid mutating input data
        customer_type = order.get("customer_type")
        store_id = str(order.get("store_id", f"INDEX-{idx}"))

        # Rule 1, 2, 3, 4, 6: Determine tier based on customer type
        tier, is_recognized = get_customer_tier(customer_type, store_id=store_id)
        order["priority_tier"] = tier

        # Assign priority_score (single sortable number):
        # Lower score = Higher priority (1.0 is highest, 5.0 is lowest)
        if tier == 1:
            # Rule 1: Government (highest tier)
            score = 1.0
        elif tier == 2:
            # Rule 2: NGO (second highest tier)
            score = 2.0
        elif tier == 3:
            # Rule 3: Loyal (third tier - fixed tag)
            score = 3.0
        elif tier == 4:
            # Rule 4 & 5: Regular customer
            # Combine Tier 4 with the urgency ranking offset:
            # Rank 1 -> 4.001, Rank 2 -> 4.002, etc.
            # Lower stock level gets smaller rank, hence lower score (higher priority)
            rank = urgency_rankings.get(idx, 1)
            order["urgency_rank"] = rank
            score = round(4.0 + (rank * 0.001), 4)
        else:
            # Rule 6: Unrecognized customer type gets lowest priority
            score = float(UNKNOWN_CUSTOMER_TIER)

        order["priority_score"] = score
        processed_orders.append(order)

    # Step 3: Sort all orders by final priority (highest priority first)
    # Since lower priority_score represents higher priority, sort in ascending order
    processed_orders.sort(key=lambda o: o["priority_score"])

    logger.info(f"Processed and prioritized {len(processed_orders)} orders.")
    return processed_orders


def print_orders_table(orders: List[Dict[str, Any]]) -> None:
    """
    Prints a formatted, beginner-friendly table of prioritized orders to the console.
    """
    if not orders:
        print("No orders to display.")
        return

    print("\n" + "=" * 92)
    print("           PARLE-G AGENTIC AI SUPPLY CHAIN ENGINE: PRIORITIZED ORDERS")
    print("=" * 92)
    header = (
        f"{'Rank':<5} | {'Store ID':<14} | {'Customer Type':<14} | "
        f"{'SKU':<14} | {'Qty':<6} | {'Stock':<6} | {'Tier':<5} | {'Priority Score':<14}"
    )
    print(header)
    print("-" * 92)

    for rank, order in enumerate(orders, start=1):
        store_id = str(order.get("store_id", "N/A"))
        c_type = str(order.get("customer_type", "Unknown"))
        sku = str(order.get("sku", "N/A"))
        qty = str(order.get("quantity_requested", 0))
        stock = str(order.get("current_stock_level", "-")) if c_type == "Regular" else "-"
        tier = str(order.get("priority_tier", "N/A"))
        score = f"{order.get('priority_score', 0.0):.3f}"

        row = (
            f"{rank:<5} | {store_id:<14} | {c_type:<14} | "
            f"{sku:<14} | {qty:<6} | {stock:<6} | {tier:<5} | {score:<14}"
        )
        print(row)

    print("=" * 92)
    print("Notes:")
    print(" - Tier 1: Government (Top Priority)")
    print(" - Tier 2: NGO")
    print(" - Tier 3: Loyal Customer")
    print(" - Tier 4: Regular Customer (Ranked by low stock urgency; earlier timestamp breaks ties)")
    print(" - Tier 5: Unrecognized Customer Type (Lowest Priority Fallback)")
    print("=" * 92 + "\n")


def main() -> List[Dict[str, Any]]:
    """
    CLI execution entry point. Loads orders from data/orders.json,
    processes their priorities, prints the sorted table, and returns the list.
    """
    print("Starting Parle-G Order Intake Agent...")
    try:
        raw_orders = load_orders()
    except Exception as e:
        logger.error(f"Failed to load orders: {e}")
        return []

    prioritized_orders = process_orders(raw_orders)
    print_orders_table(prioritized_orders)
    return prioritized_orders


if __name__ == "__main__":
    main()

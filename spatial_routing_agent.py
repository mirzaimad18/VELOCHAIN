"""LangGraph node for assigning orders to the nearest stocked warehouse."""

from __future__ import annotations

import sqlite3

from database.db import get_connection
from models import AgentState
from tools.spatial_routing import (
    NoWarehouseWithSufficientStockError,
    find_nearest_warehouse,
)


def _get_store_coordinates(store_id: int) -> tuple[float, float]:
    query = "SELECT latitude, longitude FROM stores WHERE id = "
    with get_connection() as connection:
        if isinstance(connection, sqlite3.Connection):
            cursor = connection.execute(query + "?", (store_id,))
        else:
            cursor = connection.cursor()
            cursor.execute(query + "%s", (store_id,))

        try:
            row = cursor.fetchone()
        finally:
            cursor.close()

    if row is None:
        raise ValueError(f"No store found with id {store_id}.")
    return row[0], row[1]


def spatial_routing_node(state: AgentState) -> dict:
    """Assign a stocked warehouse or request an inter-warehouse rebalance."""
    order = state["order"]
    store_id = order["store_id"]
    required_quantity = order["quantity"]
    status_logs = list(state.get("status_logs", []))
    store_lat, store_lon = _get_store_coordinates(store_id)
    try:
        warehouse = find_nearest_warehouse.invoke(
            {
                "store_lat": store_lat,
                "store_lon": store_lon,
                "required_quantity": required_quantity,
            }
        )
    except NoWarehouseWithSufficientStockError:
        status_logs.append(
            f"No warehouse has {required_quantity} units in stock; "
            "inter-warehouse transfer is required."
        )
        return {
            "assigned_warehouse": None,
            "approval_status": "NEEDS_REBALANCE",
            "status_logs": status_logs,
        }

    status_logs.append(
        f"Assigned warehouse {warehouse['warehouse_id']} "
        f"({warehouse['name']}) at {warehouse['distance_km']:.2f} km; "
        "direct dispatch confirmed."
    )
    return {
        "assigned_warehouse": warehouse,
        "approval_status": "CONFIRMED_DIRECT_DISPATCH",
        "status_logs": status_logs,
    }

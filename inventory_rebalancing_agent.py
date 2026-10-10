"""LangGraph node for proposing inter-warehouse inventory transfers."""

from __future__ import annotations

import sqlite3
from typing import Any

from database.db import get_connection
from models import AgentState
from tools.spatial_routing import haversine_distance

EXCESS_STOCK_THRESHOLD = 50_000
PRODUCT_NAME = "Parle-G 100g"


def _get_store_and_warehouses(
    store_id: int,
) -> tuple[tuple[float, float], list[tuple[int, str, str, float, float, int]]]:
    store_query = "SELECT latitude, longitude FROM stores WHERE id = "
    warehouse_query = """
        SELECT
            w.id,
            w.name,
            w.type,
            w.latitude,
            w.longitude,
            COALESCE(SUM(i.stock_quantity), 0) AS available_stock
        FROM warehouses AS w
        LEFT JOIN inventory AS i
            ON i.warehouse_id = w.id
            AND i.product_name = ?
        GROUP BY w.id, w.name, w.type, w.latitude, w.longitude
    """

    with get_connection() as connection:
        if isinstance(connection, sqlite3.Connection):
            store_cursor = connection.execute(
                store_query + "?", (store_id,)
            )
            warehouse_cursor = connection.execute(
                warehouse_query, (PRODUCT_NAME,)
            )
        else:
            store_cursor = connection.cursor()
            store_cursor.execute(store_query + "%s", (store_id,))
            warehouse_cursor = connection.cursor()
            warehouse_cursor.execute(
                warehouse_query.replace("?", "%s"), (PRODUCT_NAME,)
            )

        try:
            store = store_cursor.fetchone()
            warehouses = warehouse_cursor.fetchall()
        finally:
            store_cursor.close()
            warehouse_cursor.close()

    if store is None:
        raise ValueError(f"No store found with id {store_id}.")
    return (store[0], store[1]), warehouses


def rebalance_inventory_node(state: AgentState) -> dict:
    """Propose a transfer when direct dispatch requires inventory rebalancing."""
    if state["approval_status"] != "NEEDS_REBALANCE":
        return {}

    order = state["order"]
    store_id = order["store_id"]
    required_quantity = order["quantity"]
    if required_quantity <= 0:
        raise ValueError("Order quantity must be greater than zero.")

    (store_lat, store_lon), warehouses = _get_store_and_warehouses(store_id)
    if not warehouses:
        raise ValueError("No warehouses are available for inventory rebalancing.")

    receiving_warehouses = [
        warehouse
        for warehouse in warehouses
        if warehouse[0] is not None
        and warehouse[5] < required_quantity
    ]
    if not receiving_warehouses:
        raise ValueError(
            "No warehouse with a stock shortfall was found for this order."
        )

    destination = min(
        receiving_warehouses,
        key=lambda warehouse: haversine_distance(
            store_lat, store_lon, warehouse[3], warehouse[4]
        ),
    )

    sources = [
        warehouse
        for warehouse in warehouses
        if warehouse[0] != destination[0]
        and warehouse[5] > EXCESS_STOCK_THRESHOLD
    ]
    if not sources:
        raise ValueError(
            "No warehouse has excess inventory above "
            f"{EXCESS_STOCK_THRESHOLD} units."
        )

    def source_rank(
        warehouse: tuple[int, str, str, float, float, int],
    ) -> tuple[bool, bool, int, float]:
        distance_to_destination = haversine_distance(
            warehouse[3], warehouse[4], destination[3], destination[4]
        )
        return (
            warehouse[2] != "Rural Depot",
            warehouse[5] < required_quantity,
            -warehouse[5],
            distance_to_destination,
        )

    source = min(sources, key=source_rank)
    transfer_quantity = min(required_quantity, source[5])
    requires_approval = transfer_quantity > EXCESS_STOCK_THRESHOLD

    transfer_request: dict[str, Any] = {
        "from_warehouse_id": source[0],
        "from_warehouse_name": source[1],
        "to_warehouse_id": destination[0],
        "quantity": transfer_quantity,
        "requires_approval": requires_approval,
    }

    status_logs = list(state.get("status_logs", []))
    approval_note = (
        "human approval required"
        if requires_approval
        else "human approval not required"
    )
    status_logs.append(
        f"Proposed transfer of {transfer_quantity} units of {PRODUCT_NAME} "
        f"from {source[1]} (warehouse {source[0]}) to {destination[1]} "
        f"(warehouse {destination[0]}); {approval_note}."
    )

    return {
        "transfer_request": transfer_request,
        "status_logs": status_logs,
    }

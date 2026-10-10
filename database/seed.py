"""Seed representative Parle-G supply-chain data."""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import get_connection, init_db

STORES = (
    (1, "Mumbai Central Supermart", "Mumbai", 18.9696, 72.8194, "Gold"),
    (2, "Pune Retail Depot", "Pune", 18.5204, 73.8567, "Silver"),
    (3, "Nagpur Small Hub", "Nagpur", 21.1458, 79.0882, "Bronze"),
)

WAREHOUSES = (
    (1, "Mumbai Main Hub", "Mumbai", 19.0760, 72.8777, "Urban Hub"),
    (2, "Nagpur Central Depot", "Nagpur", 21.1458, 79.0882, "Rural Depot"),
    (3, "Nashik Secondary Hub", "Nashik", 19.9975, 73.7898, "Urban Hub"),
)

INVENTORY = (
    (1, 1, "Parle-G 100g", 5_000),
    (2, 2, "Parle-G 100g", 150_000),
    (3, 3, "Parle-G 100g", 30_000),
)


def _upsert(
    cursor: Any,
    table: str,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
    update_columns: tuple[str, ...],
    placeholder: str,
) -> None:
    column_list = ", ".join(columns)
    placeholders = ", ".join(placeholder for _ in columns)
    updates = ", ".join(
        f"{column} = excluded.{column}" for column in update_columns
    )
    cursor.execute(
        f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}",
        values,
    )


def seed_data() -> None:
    """Insert or refresh the sample stores, warehouses, and inventory.

    Store tier labels map High to Gold and Medium to Silver. Coordinates are
    representative city-center locations for these fictional sample sites.
    """
    with get_connection() as connection:
        placeholder = (
            "?" if isinstance(connection, sqlite3.Connection) else "%s"
        )
        cursor = connection.cursor()
        try:
            for store in STORES:
                _upsert(
                    cursor,
                    "stores",
                    ("id", "name", "city", "latitude", "longitude", "loyalty_tier"),
                    store,
                    ("name", "city", "latitude", "longitude", "loyalty_tier"),
                    placeholder,
                )

            for warehouse in WAREHOUSES:
                _upsert(
                    cursor,
                    "warehouses",
                    ("id", "name", "city", "latitude", "longitude", "type"),
                    warehouse,
                    ("name", "city", "latitude", "longitude", "type"),
                    placeholder,
                )

            for item in INVENTORY:
                _upsert(
                    cursor,
                    "inventory",
                    ("id", "warehouse_id", "product_name", "stock_quantity"),
                    item,
                    ("warehouse_id", "product_name", "stock_quantity"),
                    placeholder,
                )
                cursor.execute(
                    f"UPDATE inventory SET last_updated = CURRENT_TIMESTAMP "
                    f"WHERE id = {placeholder}",
                    (item[0],),
                )
        finally:
            cursor.close()


if __name__ == "__main__":
    init_db()
    seed_data()

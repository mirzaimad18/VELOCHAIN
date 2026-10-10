"""Spatial routing tools for supply-chain warehouse selection."""

from __future__ import annotations

import math
from typing import TypedDict

from langchain_core.tools import tool

from database import fetch_query

EARTH_RADIUS_KM = 6371.0


class WarehouseRoute(TypedDict):
    warehouse_id: int
    name: str
    distance_km: float
    available_stock: int


class NoWarehouseWithSufficientStockError(ValueError):
    """Raised when no warehouse can fulfill the requested quantity."""


def haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Return the great-circle distance between two coordinates in kilometers."""
    coordinates = (lat1, lon1, lat2, lon2)
    if not all(math.isfinite(value) for value in coordinates):
        raise ValueError("Coordinates must be finite numbers.")
    if not -90 <= lat1 <= 90 or not -90 <= lat2 <= 90:
        raise ValueError("Latitude must be between -90 and 90 degrees.")
    if not -180 <= lon1 <= 180 or not -180 <= lon2 <= 180:
        raise ValueError("Longitude must be between -180 and 180 degrees.")

    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    lat_delta = math.radians(lat2 - lat1)
    lon_delta = math.radians(lon2 - lon1)
    haversine = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(lon_delta / 2) ** 2
    )
    haversine = min(1.0, max(0.0, haversine))
    central_angle = 2 * math.atan2(
        math.sqrt(haversine), math.sqrt(1 - haversine)
    )
    return EARTH_RADIUS_KM * central_angle


@tool
def find_nearest_warehouse(
    store_lat: float, store_lon: float, required_quantity: int
) -> WarehouseRoute:
    """Find the nearest warehouse with enough Parle-G 100g inventory."""
    if required_quantity <= 0:
        raise ValueError("required_quantity must be greater than zero.")

    warehouses = fetch_query(
        """
        SELECT
            w.id,
            w.name,
            w.latitude,
            w.longitude,
            COALESCE(SUM(i.stock_quantity), 0) AS available_stock
        FROM warehouses AS w
        LEFT JOIN inventory AS i
            ON i.warehouse_id = w.id
            AND i.product_name = 'Parle-G 100g'
        GROUP BY w.id, w.name, w.latitude, w.longitude
        """
    )

    candidates: list[tuple[float, int, str, int]] = []
    for warehouse_id, name, latitude, longitude, available_stock in warehouses:
        if available_stock >= required_quantity:
            distance = haversine_distance(
                store_lat, store_lon, latitude, longitude
            )
            candidates.append(
                (distance, warehouse_id, name, available_stock)
            )

    if not candidates:
        raise NoWarehouseWithSufficientStockError(
            "No warehouse has sufficient Parle-G 100g stock "
            f"for the requested quantity of {required_quantity}."
        )

    distance, warehouse_id, name, available_stock = min(
        candidates, key=lambda candidate: candidate[0]
    )
    return {
        "warehouse_id": warehouse_id,
        "name": name,
        "distance_km": distance,
        "available_stock": available_stock,
    }

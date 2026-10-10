"""LangGraph supervisor and transfer approval operations."""

from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from database.db import get_connection
from models import AgentState

HUMAN_APPROVAL_THRESHOLD = 50_000
PRODUCT_NAME = "Parle-G 100g"
AGENT_STATE_ADAPTER = TypeAdapter(AgentState)


class TransferProposal(BaseModel):
    from_warehouse_id: int = Field(gt=0)
    from_warehouse_name: str
    to_warehouse_id: int = Field(gt=0)
    quantity: int = Field(gt=0)
    requires_approval: bool
    transfer_id: int | None = Field(default=None, gt=0)


def _execute(cursor: Any, query: str, params: tuple[Any, ...], sqlite: bool) -> None:
    cursor.execute(query.replace("%s", "?") if sqlite else query, params)


def _persist_pending_transfer(proposal: TransferProposal) -> int:
    if proposal.from_warehouse_id == proposal.to_warehouse_id:
        raise ValueError("A transfer source and destination must be different.")

    with get_connection() as connection:
        is_sqlite = isinstance(connection, sqlite3.Connection)
        if is_sqlite:
            connection.execute("BEGIN IMMEDIATE")
        cursor = connection.cursor()
        try:
            if not is_sqlite:
                cursor.execute("LOCK TABLE transfers IN EXCLUSIVE MODE")

            if proposal.transfer_id is not None:
                _execute(
                    cursor,
                    """
                    SELECT from_warehouse_id, to_warehouse_id, quantity, status
                    FROM transfers
                    WHERE id = %s
                    """,
                    (proposal.transfer_id,),
                    is_sqlite,
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise ValueError(
                        f"Transfer {proposal.transfer_id} does not exist."
                    )
                if existing[:3] != (
                    proposal.from_warehouse_id,
                    proposal.to_warehouse_id,
                    proposal.quantity,
                ):
                    raise ValueError(
                        "Transfer request does not match its persisted record."
                    )
                if existing[3] != "Pending Approval":
                    raise ValueError(
                        f"Transfer {proposal.transfer_id} is already "
                        f"{existing[3]}."
                    )
                return proposal.transfer_id

            _execute(
                cursor,
                """
                SELECT id
                FROM transfers
                WHERE from_warehouse_id = %s
                    AND to_warehouse_id = %s
                    AND quantity = %s
                    AND status = 'Pending Approval'
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    proposal.from_warehouse_id,
                    proposal.to_warehouse_id,
                    proposal.quantity,
                ),
                is_sqlite,
            )
            existing_pending = cursor.fetchone()
            if existing_pending is not None:
                return existing_pending[0]

            _execute(
                cursor,
                "SELECT COALESCE(MAX(id), 0) + 1 FROM transfers",
                (),
                is_sqlite,
            )
            transfer_id = cursor.fetchone()[0]
            _execute(
                cursor,
                """
                INSERT INTO transfers (
                    id,
                    from_warehouse_id,
                    to_warehouse_id,
                    quantity,
                    status
                )
                VALUES (%s, %s, %s, %s, 'Pending Approval')
                """,
                (
                    transfer_id,
                    proposal.from_warehouse_id,
                    proposal.to_warehouse_id,
                    proposal.quantity,
                ),
                is_sqlite,
            )
            return transfer_id
        finally:
            cursor.close()


def supervisor_node(state: AgentState) -> dict:
    """Validate workflow state and route transfers through approval."""
    try:
        validated_state = AGENT_STATE_ADAPTER.validate_python(state)
    except ValidationError as error:
        raise ValueError(f"Invalid supervisor state: {error}") from error

    status_logs = list(validated_state.get("status_logs", []))
    transfer_data = validated_state.get("transfer_request")
    if transfer_data is None:
        status_logs.append("Supervisor approved the order; no transfer needed.")
        return {
            "approval_status": "APPROVED",
            "status_logs": status_logs,
        }

    try:
        proposal = TransferProposal.model_validate(transfer_data)
    except ValidationError as error:
        raise ValueError(f"Invalid transfer request: {error}") from error

    transfer_id = _persist_pending_transfer(proposal)
    transfer_request = {
        **transfer_data,
        "transfer_id": transfer_id,
        "status": "Pending Approval",
    }

    if proposal.quantity > HUMAN_APPROVAL_THRESHOLD:
        status_logs.append(
            "ALERT: Transfer exceeding 50,000 units requires "
            "Human-in-the-Loop manager approval."
        )
        return {
            "transfer_request": transfer_request,
            "approval_status": "REQUIRES_HUMAN_APPROVAL",
            "status_logs": status_logs,
        }

    status_logs.append(
        f"Transfer {transfer_id} is within the automatic approval limit."
    )
    return {
        "transfer_request": transfer_request,
        "approval_status": "APPROVED",
        "status_logs": status_logs,
    }


def _load_transfer(cursor: Any, transfer_id: int, sqlite: bool) -> tuple[Any, ...]:
    query = """
        SELECT from_warehouse_id, to_warehouse_id, quantity, status
        FROM transfers
        WHERE id = %s
    """
    if not sqlite:
        query += " FOR UPDATE"
    _execute(cursor, query, (transfer_id,), sqlite)
    transfer = cursor.fetchone()
    if transfer is None:
        raise ValueError(f"Transfer {transfer_id} does not exist.")
    if transfer[3] != "Pending Approval":
        raise ValueError(
            f"Transfer {transfer_id} cannot be processed from status "
            f"{transfer[3]}."
        )
    return transfer


def _move_inventory(
    cursor: Any,
    source_id: int,
    destination_id: int,
    quantity: int,
    sqlite: bool,
) -> None:
    inventory_query = """
        SELECT id, warehouse_id, stock_quantity
        FROM inventory
        WHERE warehouse_id IN (%s, %s) AND product_name = %s
        ORDER BY warehouse_id, id
    """
    if not sqlite:
        inventory_query += " FOR UPDATE"
    _execute(
        cursor,
        inventory_query,
        (source_id, destination_id, PRODUCT_NAME),
        sqlite,
    )
    inventory_rows = cursor.fetchall()

    source_rows = [
        row for row in inventory_rows if row[1] == source_id and row[2] > 0
    ]
    if sum(row[2] for row in source_rows) < quantity:
        raise ValueError(
            f"Warehouse {source_id} no longer has {quantity} units available."
        )

    remaining = quantity
    for inventory_id, _, stock_quantity in source_rows:
        amount = min(stock_quantity, remaining)
        _execute(
            cursor,
            """
            UPDATE inventory
            SET stock_quantity = stock_quantity - %s,
                last_updated = CURRENT_TIMESTAMP
            WHERE id = %s AND stock_quantity >= %s
            """,
            (amount, inventory_id, amount),
            sqlite,
        )
        if cursor.rowcount != 1:
            raise ValueError(
                f"Inventory at warehouse {source_id} changed during transfer."
            )
        remaining -= amount
        if remaining == 0:
            break

    destination_rows = [
        row for row in inventory_rows if row[1] == destination_id
    ]
    if destination_rows:
        destination_inventory_id = destination_rows[0][0]
        _execute(
            cursor,
            """
            UPDATE inventory
            SET stock_quantity = stock_quantity + %s,
                last_updated = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (quantity, destination_inventory_id),
            sqlite,
        )
    else:
        _execute(
            cursor,
            """
            INSERT INTO inventory (warehouse_id, product_name, stock_quantity)
            VALUES (%s, %s, %s)
            """,
            (destination_id, PRODUCT_NAME, quantity),
            sqlite,
        )


def approve_transfer(state: AgentState) -> dict:
    """Apply a signed-off transfer once and mark it completed."""
    transfer_data = state.get("transfer_request")
    if transfer_data is None:
        raise ValueError("Cannot approve: state has no transfer request.")
    try:
        proposal = TransferProposal.model_validate(transfer_data)
    except ValidationError as error:
        raise ValueError(f"Invalid transfer request: {error}") from error
    if proposal.transfer_id is None:
        raise ValueError("Cannot approve a transfer without a persisted transfer_id.")
    if state.get("approval_status") not in {
        "APPROVED",
        "REQUIRES_HUMAN_APPROVAL",
    }:
        raise ValueError(
            "Transfer can only be applied after approval or human sign-off."
        )

    with get_connection() as connection:
        is_sqlite = isinstance(connection, sqlite3.Connection)
        if is_sqlite:
            connection.execute("BEGIN IMMEDIATE")
        cursor = connection.cursor()
        try:
            source_id, destination_id, quantity, _ = _load_transfer(
                cursor, proposal.transfer_id, is_sqlite
            )
            if (
                source_id != proposal.from_warehouse_id
                or destination_id != proposal.to_warehouse_id
                or quantity != proposal.quantity
            ):
                raise ValueError(
                    "Transfer request does not match its persisted record."
                )

            _move_inventory(
                cursor, source_id, destination_id, quantity, is_sqlite
            )
            _execute(
                cursor,
                "UPDATE transfers SET status = 'Completed' WHERE id = %s",
                (proposal.transfer_id,),
                is_sqlite,
            )
        finally:
            cursor.close()

    status_logs = list(state.get("status_logs", []))
    status_logs.append(
        f"Transfer {proposal.transfer_id} approved and inventory movement "
        "completed."
    )
    return {
        "transfer_request": {**transfer_data, "status": "Completed"},
        "approval_status": "APPROVED",
        "status_logs": status_logs,
    }


def reject_transfer(state: AgentState) -> dict:
    """Reject a signed-off transfer without changing inventory."""
    transfer_data = state.get("transfer_request")
    if transfer_data is None:
        raise ValueError("Cannot reject: state has no transfer request.")
    try:
        proposal = TransferProposal.model_validate(transfer_data)
    except ValidationError as error:
        raise ValueError(f"Invalid transfer request: {error}") from error
    if proposal.transfer_id is None:
        raise ValueError("Cannot reject a transfer without a persisted transfer_id.")
    if state.get("approval_status") != "REQUIRES_HUMAN_APPROVAL":
        raise ValueError("Only transfers awaiting human approval can be rejected.")

    with get_connection() as connection:
        is_sqlite = isinstance(connection, sqlite3.Connection)
        cursor = connection.cursor()
        try:
            _load_transfer(cursor, proposal.transfer_id, is_sqlite)
            _execute(
                cursor,
                "UPDATE transfers SET status = 'Rejected' WHERE id = %s",
                (proposal.transfer_id,),
                is_sqlite,
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Transfer {proposal.transfer_id} could not be rejected."
                )
        finally:
            cursor.close()

    status_logs = list(state.get("status_logs", []))
    status_logs.append(
        f"Transfer {proposal.transfer_id} rejected; inventory was not changed."
    )
    return {
        "transfer_request": {**transfer_data, "status": "Rejected"},
        "approval_status": "REJECTED",
        "status_logs": status_logs,
    }

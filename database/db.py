"""Database connection and query helpers for SQLite and PostgreSQL.

Use ``?`` placeholders for SQLite queries and ``%s`` for PostgreSQL queries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
from typing import Any, Generator

from dotenv import load_dotenv
import psycopg2
from psycopg2.extensions import connection as PostgreSQLConnection

DatabaseConnection = sqlite3.Connection | PostgreSQLConnection
QueryParams = Sequence[Any] | Mapping[str, Any]
QueryRow = tuple[Any, ...]

DEFAULT_DATABASE_URL = "sqlite:///./supply_chain.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

load_dotenv()


def _connect(database_url: str) -> DatabaseConnection:
    if database_url.startswith("sqlite:///"):
        database_path = database_url.removeprefix("sqlite:///")
        if not database_path:
            raise ValueError("DATABASE_URL must include a SQLite database path.")

        if database_path != ":memory:":
            Path(database_path).expanduser().parent.mkdir(
                parents=True, exist_ok=True
            )

        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    if database_url.startswith(("postgresql://", "postgres://")):
        return psycopg2.connect(database_url)

    raise ValueError(
        "DATABASE_URL must use a sqlite:/// or postgresql:// URL."
    )


def _migrate_sqlite_transfer_status(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'transfers'"
    ).fetchone()
    if table is None or "'Rejected'" in table[0]:
        return

    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript(
            """
            ALTER TABLE transfers RENAME TO transfers_before_rejected_status;
            CREATE TABLE transfers (
                id INTEGER PRIMARY KEY,
                from_warehouse_id INTEGER NOT NULL,
                to_warehouse_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                status TEXT NOT NULL DEFAULT 'Pending Approval'
                    CHECK (
                        status IN (
                            'Pending Approval',
                            'Approved',
                            'Completed',
                            'Rejected'
                        )
                    ),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (from_warehouse_id) REFERENCES warehouses (id),
                FOREIGN KEY (to_warehouse_id) REFERENCES warehouses (id),
                CHECK (from_warehouse_id <> to_warehouse_id)
            );
            INSERT INTO transfers (
                id,
                from_warehouse_id,
                to_warehouse_id,
                quantity,
                status,
                created_at
            )
            SELECT
                id,
                from_warehouse_id,
                to_warehouse_id,
                quantity,
                status,
                created_at
            FROM transfers_before_rejected_status;
            DROP TABLE transfers_before_rejected_status;
            """
        )
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _migrate_postgresql_transfer_status(
    connection: PostgreSQLConnection,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'transfers'::regclass
                AND contype = 'c'
            """
        )
        constraints = cursor.fetchall()
        status_constraints = [
            (name, definition)
            for name, definition in constraints
            if "status" in definition.lower()
        ]
        if any("'Rejected'" in definition for _, definition in status_constraints):
            return

        for name, _ in status_constraints:
            cursor.execute(
                f'ALTER TABLE transfers DROP CONSTRAINT "{name}"'
            )
        cursor.execute(
            """
            ALTER TABLE transfers
            ADD CONSTRAINT transfers_status_check
            CHECK (
                status IN (
                    'Pending Approval',
                    'Approved',
                    'Completed',
                    'Rejected'
                )
            )
            """
        )


@contextmanager
def get_connection() -> Generator[DatabaseConnection, None, None]:
    """Yield a connection, commit on success, roll back on error, and close it."""
    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    if not database_url:
        raise ValueError("DATABASE_URL cannot be empty.")

    connection = _connect(database_url)
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    """Create the database tables and indexes from ``database/schema.sql``."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection() as connection:
        if isinstance(connection, sqlite3.Connection):
            connection.executescript(schema)
            _migrate_sqlite_transfer_status(connection)
            connection.executescript(schema)
        else:
            with connection.cursor() as cursor:
                cursor.execute(schema)
            _migrate_postgresql_transfer_status(connection)


def fetch_query(
    query: str, params: QueryParams = ()
) -> list[QueryRow]:
    """Execute a query and return all result rows."""
    with get_connection() as connection:
        if isinstance(connection, sqlite3.Connection):
            cursor = connection.execute(query, params)
        else:
            cursor = connection.cursor()
            cursor.execute(query, params)

        try:
            return cursor.fetchall()
        finally:
            cursor.close()


def execute_query(
    query: str, params: QueryParams = ()
) -> int:
    """Execute a statement and return the affected row count."""
    with get_connection() as connection:
        if isinstance(connection, sqlite3.Connection):
            cursor = connection.execute(query, params)
        else:
            cursor = connection.cursor()
            cursor.execute(query, params)

        try:
            return cursor.rowcount
        finally:
            cursor.close()

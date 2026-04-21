"""
Provenance Model and Run Tracking

Every operation that touches the database gets a run record.
Every row that can affect analysis carries provenance columns.

This module provides:
- RunContext: context manager that creates/completes run records
- upsert(): idempotent insert/update helper
- new_id(): UUID generation for primary keys
- hash_content(): SHA-256 for change detection on source documents
"""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.schemas.canonical_schema import CANONICAL_SCHEMA


DB_PATH = Path(__file__).parent.parent.parent / "data" / "workbench.db"


def new_id() -> str:
    """Generate a UUID for primary keys."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def hash_content(content: str | bytes) -> str:
    """SHA-256 hash for change detection on source documents."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def init_db(db_path: Path = None) -> sqlite3.Connection:
    """
    Initialize the database with the canonical schema.
    Returns a connection with FK enforcement enabled.
    """
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(CANONICAL_SCHEMA)
    conn.commit()
    return conn


@dataclass
class RunContext:
    """
    Context manager for run tracking.

    Usage:
        db = init_db()
        with RunContext(db, "ingest", {"ticker": "AAPL"}) as run:
            # do work
            run.run_id  # use this in all inserts
        # run record automatically completed on exit
    """
    db: sqlite3.Connection
    run_type: str
    parameters: dict = field(default_factory=dict)
    parent_run_id: str = None
    run_id: str = field(default_factory=new_id)
    _started: bool = field(default=False, init=False)

    def __enter__(self):
        self.db.execute(
            """INSERT INTO run (run_id, run_type, status, started_at, parameters, parent_run_id)
               VALUES (?, ?, 'running', ?, ?, ?)""",
            (
                self.run_id,
                self.run_type,
                now_iso(),
                json.dumps(self.parameters) if self.parameters else None,
                self.parent_run_id,
            ),
        )
        self.db.commit()
        self._started = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.db.execute(
                """UPDATE run SET status = 'failed', completed_at = ?, error_message = ?
                   WHERE run_id = ?""",
                (now_iso(), str(exc_val), self.run_id),
            )
        else:
            self.db.execute(
                """UPDATE run SET status = 'success', completed_at = ?
                   WHERE run_id = ?""",
                (now_iso(), self.run_id),
            )
        self.db.commit()
        return False  # don't suppress exceptions


def upsert(
    db: sqlite3.Connection,
    table: str,
    data: dict,
    conflict_columns: list[str],
    update_columns: list[str] = None,
) -> str:
    """
    Idempotent insert with conflict handling.

    Args:
        db: SQLite connection
        table: target table name
        data: dict of column: value pairs
        conflict_columns: columns that form the natural key
        update_columns: columns to update on conflict (None = ignore)

    Returns:
        The primary key value (first column in data)
    """
    columns = list(data.keys())
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)

    if update_columns:
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
        conflict_str = ", ".join(conflict_columns)
        sql = (
            f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_str}) DO UPDATE SET {update_clause}"
        )
    else:
        conflict_str = ", ".join(conflict_columns)
        sql = (
            f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_str}) DO NOTHING"
        )

    db.execute(sql, list(data.values()))
    return data[columns[0]]


def create_run(db: sqlite3.Connection, run_type: str,
               description: str = "", config: dict = None,
               parent_run_id: str = None) -> RunContext:
    """Convenience wrapper for RunContext."""
    return RunContext(db, run_type, config or {}, parent_run_id)

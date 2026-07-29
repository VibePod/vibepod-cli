"""Shared ALTER-based sqlite column migrations."""

from __future__ import annotations

import sqlite3


def add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> bool:
    """Add each column in *columns* missing from *table*.

    Returns ``True`` when the table changed. A concurrently launching process
    can add a column between the PRAGMA read and the ALTER — that
    duplicate-column failure is tolerated (the column exists either way, and
    callers' backfills are idempotent); any other ``OperationalError``
    propagates.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    changed = False
    for column, definition in columns.items():
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        changed = True
    return changed

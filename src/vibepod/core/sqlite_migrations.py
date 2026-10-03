"""Shared ALTER-based sqlite column migrations."""

from __future__ import annotations

import re
import sqlite3
from typing import Final

# Identifiers cannot be bound as parameters, so table and column names are
# restricted to plain SQL identifiers and then quoted.
_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Column definitions: a type name, optionally NOT NULL, optionally a literal
# DEFAULT (single-quoted string without embedded quotes, number, or NULL).
_DEFINITION: Final = re.compile(
    r"(?:TEXT|INTEGER|REAL|NUMERIC|BLOB)"
    r"(?: NOT NULL)?"
    r"(?: DEFAULT (?:'[^']*'|-?[0-9]+(?:\.[0-9]+)?|NULL))?",
    re.IGNORECASE,
)


def _quote_identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"invalid sqlite identifier: {name!r}")
    return f'"{name}"'


def _validate_definition(definition: str) -> str:
    if not _DEFINITION.fullmatch(definition):
        raise ValueError(f"unsupported column definition: {definition!r}")
    return definition


def add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> bool:
    """Add each column in *columns* missing from *table*.

    Returns ``True`` when the table changed. A concurrently launching process
    can add a column between the PRAGMA read and the ALTER — that
    duplicate-column failure is tolerated (the column exists either way, and
    callers' backfills are idempotent); any other ``OperationalError``
    propagates.

    Table and column names must be plain identifiers and definitions a simple
    type with optional ``NOT NULL`` / literal ``DEFAULT``; anything else raises
    ``ValueError`` before touching the database.
    """
    quoted_table = _quote_identifier(table)
    statements = {
        column: (
            f"ALTER TABLE {quoted_table} ADD COLUMN "
            f"{_quote_identifier(column)} {_validate_definition(definition)}"
        )
        for column, definition in columns.items()
    }
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()}
    changed = False
    for column, statement in statements.items():
        if column in existing:
            continue
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
        changed = True
    return changed

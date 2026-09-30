"""Shared sqlite column-migration helper tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vibepod.core.sqlite_migrations import add_missing_columns


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute("CREATE TABLE things (id TEXT PRIMARY KEY)")
    return conn


def test_adds_missing_columns_and_reports_change(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)

    changed = add_missing_columns(conn, "things", {"extra": "TEXT", "count": "INTEGER"})

    assert changed is True
    columns = {row[1] for row in conn.execute("PRAGMA table_info(things)").fetchall()}
    assert {"id", "extra", "count"} <= columns


def test_noop_when_columns_exist(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    conn.execute("ALTER TABLE things ADD COLUMN extra TEXT")

    changed = add_missing_columns(conn, "things", {"extra": "TEXT"})

    assert changed is False


class _StalePragmaConn:
    """Connection wrapper whose PRAGMA reads claim no columns exist.

    Emulates the concurrent-launch race: another process ALTERs the table
    after this process read the column list.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: object) -> object:
        if sql.startswith("PRAGMA"):

            class _Empty:
                @staticmethod
                def fetchall() -> list:
                    return []

            return _Empty()
        return self._conn.execute(sql, *args)


def test_concurrently_added_column_is_tolerated(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    conn.execute("ALTER TABLE things ADD COLUMN extra TEXT")

    changed = add_missing_columns(_StalePragmaConn(conn), "things", {"extra": "TEXT"})

    assert changed is True  # the column exists either way; callers may backfill


def test_other_operational_errors_propagate(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)

    with pytest.raises(sqlite3.OperationalError):
        add_missing_columns(_StalePragmaConn(conn), "no_such_table", {"extra": "TEXT"})


@pytest.mark.parametrize("table", ["things; DROP TABLE things", "1things", "th ings", ""])
def test_rejects_invalid_table_name(tmp_path: Path, table: str) -> None:
    conn = _make_conn(tmp_path)

    with pytest.raises(ValueError):
        add_missing_columns(conn, table, {"extra": "TEXT"})


@pytest.mark.parametrize("column", ["extra TEXT; DROP TABLE things; --", "ex-tra", ""])
def test_rejects_invalid_column_name(tmp_path: Path, column: str) -> None:
    conn = _make_conn(tmp_path)

    with pytest.raises(ValueError):
        add_missing_columns(conn, "things", {column: "TEXT"})


@pytest.mark.parametrize(
    "definition",
    [
        "TEXT; DROP TABLE things",
        "TEXT DEFAULT 'a'); DROP TABLE things; --",
        "TEXT REFERENCES other(id)",
        "",
    ],
)
def test_rejects_unsupported_definition(tmp_path: Path, definition: str) -> None:
    conn = _make_conn(tmp_path)

    with pytest.raises(ValueError):
        add_missing_columns(conn, "things", {"extra": definition})

    # Validation happens before any ALTER runs.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(things)").fetchall()}
    assert columns == {"id"}


@pytest.mark.parametrize(
    "definition",
    ["TEXT", "INTEGER", "TEXT NOT NULL DEFAULT 'running'", "INTEGER DEFAULT 0"],
)
def test_accepts_supported_definitions(tmp_path: Path, definition: str) -> None:
    conn = _make_conn(tmp_path)

    assert add_missing_columns(conn, "things", {"extra": definition}) is True

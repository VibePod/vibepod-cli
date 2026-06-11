"""TaskStore tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from vibepod.core.tasks import TaskStore


def _make_store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.db")


def _new_kwargs(**overrides: object) -> dict:
    base = {
        "agent": "claude",
        "prompt": "do the thing",
        "workspace": "/tmp/ws",
        "container_id": "c" * 64,
        "container_name": "vibepod-claude-abc",
        "image": "vibepod/claude:latest",
        "vibepod_version": "0.11.0",
    }
    base.update(overrides)
    return base


def test_create_returns_record_and_persists_row(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = store.create(**_new_kwargs())

    assert len(record.id) == 32  # uuid4 hex
    assert record.agent == "claude"
    assert record.prompt == "do the thing"
    assert record.status == "running"
    assert record.exit_code is None
    assert record.started_at is None
    assert record.finished_at is None
    assert record.updated_at == record.created_at

    retrieved = store.get(record.id)
    assert retrieved is not None
    assert retrieved.as_dict() == record.as_dict()


def test_update_transitions_lifecycle_state(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = store.create(**_new_kwargs())

    updated = store.update(
        record.id,
        status="completed",
        exit_code=0,
        started_at="2026-06-11T14:00:00Z",
        finished_at="2026-06-11T14:05:00Z",
    )

    assert updated is not None
    assert updated.status == "completed"
    assert updated.exit_code == 0
    assert updated.started_at == "2026-06-11T14:00:00Z"
    assert updated.finished_at == "2026-06-11T14:05:00Z"
    assert updated.updated_at > record.updated_at
    assert store.get(record.id) == updated


def test_update_missing_task_returns_none(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    assert store.update("missing", status="failed", exit_code=1) is None


def test_existing_database_is_migrated_with_lifecycle_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, "
        "agent TEXT NOT NULL, "
        "prompt TEXT NOT NULL, "
        "workspace TEXT NOT NULL, "
        "container_id TEXT NOT NULL, "
        "container_name TEXT NOT NULL, "
        "image TEXT NOT NULL, "
        "vibepod_version TEXT NOT NULL, "
        "created_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO tasks "
        "(id, agent, prompt, workspace, container_id, container_name, image, "
        "vibepod_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "abc123",
            "claude",
            "legacy task",
            "/tmp/ws",
            "legacy-container",
            "vibepod-task-legacy",
            "vibepod/claude:latest",
            "0.11.0",
            "2026-06-11T14:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    record = TaskStore(db_path).get("abc123")

    assert record is not None
    assert record.status == "running"
    assert record.exit_code is None
    assert record.started_at is None
    assert record.finished_at is None
    assert record.updated_at == "2026-06-11T14:00:00+00:00"


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.get("does-not-exist") is None


def test_find_by_prefix_returns_matches(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    r1 = store.create(**_new_kwargs(container_id="c1"))
    r2 = store.create(**_new_kwargs(container_id="c2"))

    assert store.find_by_prefix(r1.id[:8]) == [r1]
    assert store.find_by_prefix(r2.id[:8]) == [r2]


def test_find_by_empty_prefix_returns_empty(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.create(**_new_kwargs())
    assert store.find_by_prefix("") == []


def test_list_orders_by_created_at_desc(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    r1 = store.create(**_new_kwargs(prompt="first", container_id="c1"))
    r2 = store.create(**_new_kwargs(prompt="second", container_id="c2"))
    r3 = store.create(**_new_kwargs(prompt="third", container_id="c3"))

    rows = store.list()
    # Newest first. created_at is ISO8601; later inserts have equal-or-later ts.
    assert {rec.id for rec in rows} == {r1.id, r2.id, r3.id}
    # Last inserted is first in list (same or later timestamp)
    assert rows[0].prompt == "third"


def test_list_filters_by_agent(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.create(**_new_kwargs(agent="claude", container_id="c1"))
    store.create(**_new_kwargs(agent="codex", container_id="c2"))
    store.create(**_new_kwargs(agent="claude", container_id="c3"))

    claude_only = store.list(agent="claude")
    assert len(claude_only) == 2
    assert all(r.agent == "claude" for r in claude_only)


def test_list_limit_applies(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    for i in range(5):
        store.create(**_new_kwargs(container_id=f"c{i}"))

    assert len(store.list(limit=3)) == 3


def test_delete_removes_row(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    record = store.create(**_new_kwargs())

    assert store.delete(record.id) is True
    assert store.get(record.id) is None
    assert store.delete(record.id) is False  # idempotent: no row → False


def test_store_creates_db_file_on_first_use(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "tasks.db"
    store = TaskStore(db_path)
    store.create(**_new_kwargs())
    assert db_path.exists()

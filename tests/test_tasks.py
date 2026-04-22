"""TaskStore tests."""

from __future__ import annotations

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

    retrieved = store.get(record.id)
    assert retrieved is not None
    assert retrieved.as_dict() == record.as_dict()


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

"""SQLite-backed registry for background agent tasks."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    agent            TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    workspace        TEXT NOT NULL,
    container_id     TEXT NOT NULL,
    container_name   TEXT NOT NULL,
    image            TEXT NOT NULL,
    vibepod_version  TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
"""


@dataclass(frozen=True)
class TaskRecord:
    id: str
    agent: str
    prompt: str
    workspace: str
    container_id: str
    container_name: str
    image: str
    vibepod_version: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> TaskRecord:
        return cls(
            id=row["id"],
            agent=row["agent"],
            prompt=row["prompt"],
            workspace=row["workspace"],
            container_id=row["container_id"],
            container_name=row["container_name"],
            image=row["image"],
            vibepod_version=row["vibepod_version"],
            created_at=row["created_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "prompt": self.prompt,
            "workspace": self.workspace,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "image": self.image,
            "vibepod_version": self.vibepod_version,
            "created_at": self.created_at,
        }


class TaskStore:
    """Registry of agent tasks. One row per `vp task run` invocation."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    def create(
        self,
        *,
        agent: str,
        prompt: str,
        workspace: str,
        container_id: str,
        container_name: str,
        image: str,
        vibepod_version: str,
    ) -> TaskRecord:
        task_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id, agent, prompt, workspace, container_id, container_name, "
                "image, vibepod_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    agent,
                    prompt,
                    workspace,
                    container_id,
                    container_name,
                    image,
                    vibepod_version,
                    created_at,
                ),
            )
        return TaskRecord(
            id=task_id,
            agent=agent,
            prompt=prompt,
            workspace=workspace,
            container_id=container_id,
            container_name=container_name,
            image=image,
            vibepod_version=vibepod_version,
            created_at=created_at,
        )

    def get(self, task_id: str) -> TaskRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return TaskRecord.from_row(row) if row else None

    def find_by_prefix(self, prefix: str) -> list[TaskRecord]:
        """Find tasks whose id starts with *prefix*. Supports short-id lookup."""
        if not prefix:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE id LIKE ? ORDER BY created_at DESC",
                (f"{prefix}%",),
            ).fetchall()
        return [TaskRecord.from_row(row) for row in rows]

    def list(self, *, agent: str | None = None, limit: int | None = None) -> list[TaskRecord]:
        query = "SELECT * FROM tasks"
        params: tuple[Any, ...] = ()
        if agent is not None:
            query += " WHERE agent = ?"
            params = (agent,)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [TaskRecord.from_row(row) for row in rows]

    def delete(self, task_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount > 0

"""SQLite session logging — captures user-submitted messages."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from uuid import uuid4

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    image           TEXT NOT NULL,
    workspace       TEXT NOT NULL,
    container_id    TEXT NOT NULL,
    container_name  TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    exit_reason     TEXT,
    vibepod_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    timestamp   TEXT NOT NULL,
    content     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    timestamp   TEXT NOT NULL,
    stream      TEXT NOT NULL,
    content     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_outputs_session_id ON outputs(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
"""


def new_session_id() -> str:
    """Return a new VibePod session/task id."""
    return uuid4().hex


class SessionLogger:
    """SQLite logger that captures user-submitted messages.

    Buffers keystrokes and logs the accumulated text when the user
    presses Enter (``\\r`` in raw terminal mode).
    """

    # Escape-sequence parser states
    _ST_NORMAL = 0  # not inside any escape sequence
    _ST_ESC = 1  # saw ESC (0x1B), waiting for discriminator byte
    _ST_CSI = 2  # inside CSI sequence (ESC [), waiting for final byte
    _ST_SS3 = 3  # inside SS3 sequence (ESC O), skip one more byte

    def __init__(self, db_path: str | Path, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._db_path = Path(db_path) if enabled else None
        self._conn: sqlite3.Connection | None = None
        self._session_id: str | None = None
        self._input_buffer: bytearray = bytearray()
        self._esc_state: int = self._ST_NORMAL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_session(
        self,
        *,
        agent: str,
        image: str,
        workspace: str,
        container_id: str,
        container_name: str,
        vibepod_version: str,
        session_id: str | None = None,
    ) -> str | None:
        """Create the session row.  Returns the session id, or ``None`` when disabled."""
        if not self._enabled:
            return None

        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

        self._session_id = session_id or new_session_id()
        now = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            "INSERT INTO sessions "
            "(id, agent, image, workspace, container_id, container_name, "
            "started_at, vibepod_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._session_id,
                agent,
                image,
                workspace,
                container_id,
                container_name,
                now,
                vibepod_version,
            ),
        )
        self._conn.commit()
        return self._session_id

    @staticmethod
    def create_session(
        db_path: str | Path,
        *,
        session_id: str,
        agent: str,
        image: str,
        workspace: str,
        container_id: str,
        container_name: str,
        vibepod_version: str,
    ) -> None:
        """Create a session row without keeping a logger connection open."""
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, agent, image, workspace, container_id, container_name, "
                "started_at, vibepod_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    agent,
                    image,
                    workspace,
                    container_id,
                    container_name,
                    now,
                    vibepod_version,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def append_output(
        db_path: str | Path,
        *,
        session_id: str,
        content: str,
        stream: str = "stdout",
    ) -> None:
        """Append container output for a session."""
        if not content:
            return
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.executescript(_SCHEMA)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO outputs (session_id, timestamp, stream, content) "
                "VALUES (?, ?, ?, ?)",
                (session_id, now, stream, content),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def close_session_by_id(
        db_path: str | Path,
        *,
        session_id: str,
        exit_reason: str = "normal",
    ) -> None:
        """Mark a session as ended without requiring an open logger instance."""
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.executescript(_SCHEMA)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE sessions SET ended_at = ?, exit_reason = ? WHERE id = ?",
                (now, exit_reason, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def get_session(db_path: str | Path, session_id: str) -> dict[str, str | None] | None:
        """Return session metadata for a task id."""
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            return {str(key): cast(str | None, row[key]) for key in row.keys()}
        finally:
            conn.close()

    @staticmethod
    def get_outputs(db_path: str | Path, session_id: str) -> list[dict[str, str]]:
        """Return persisted container output for a task id."""
        db = Path(db_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA)
            rows = conn.execute(
                "SELECT timestamp, stream, content FROM outputs "
                "WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [{str(key): cast(str, row[key]) for key in row.keys()} for row in rows]
        finally:
            conn.close()

    def log_input(self, data: bytes) -> None:
        """Process raw input bytes, buffering keystrokes and logging on Enter."""
        if not self._enabled or not data:
            return

        for byte in data:
            # --- escape sequence state machine ---
            if self._esc_state == self._ST_ESC:
                # Byte immediately after ESC — determines sequence type
                if byte == 0x5B:  # '[' → CSI sequence
                    self._esc_state = self._ST_CSI
                elif byte == 0x4F:  # 'O' → SS3 sequence (e.g. F1-F4)
                    self._esc_state = self._ST_SS3
                else:
                    # Two-byte sequence (e.g. Alt+key) — done
                    self._esc_state = self._ST_NORMAL
                continue

            if self._esc_state == self._ST_CSI:
                # CSI: parameter bytes 0x30-0x3F, intermediate 0x20-0x2F,
                # final byte 0x40-0x7E terminates the sequence
                if 0x40 <= byte <= 0x7E:
                    self._esc_state = self._ST_NORMAL
                continue

            if self._esc_state == self._ST_SS3:
                # SS3: exactly one byte after ESC O
                self._esc_state = self._ST_NORMAL
                continue

            if byte == 0x1B:  # ESC — start of escape sequence
                self._esc_state = self._ST_ESC
                continue

            # --- normal input handling ---
            if byte == 0x0D:  # \r — Enter
                self._flush_message()
            elif byte == 0x09:  # \t — Tab (often used for completion)
                pass  # ignore tab characters
            elif byte in (0x7F, 0x08):  # Backspace / Delete
                if self._input_buffer:
                    self._input_buffer.pop()
            elif byte >= 0x20:  # Printable characters and UTF-8 continuation
                self._input_buffer.append(byte)
            # Ignore other control characters

    def close_session(self, exit_reason: str = "normal") -> None:
        """Flush remaining buffer, update ``ended_at``, and close the DB."""
        if not self._enabled or self._conn is None:
            return

        self._flush_message()

        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, exit_reason = ? WHERE id = ?",
            (now, exit_reason, self._session_id),
        )
        self._conn.commit()
        self._conn.close()
        self._conn = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _flush_message(self) -> None:
        """Write the current input buffer as a message row and clear it."""
        if not self._input_buffer or self._conn is None:
            return

        content = self._input_buffer.decode("utf-8", errors="replace")
        self._input_buffer.clear()

        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO messages (session_id, timestamp, content) VALUES (?, ?, ?)",
            (self._session_id, now, content),
        )
        self._conn.commit()

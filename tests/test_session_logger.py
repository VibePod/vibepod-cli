"""Tests for the SQLite session logger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from vibepod.core.session_logger import SessionLogger


class TestSessionLogger:
    """Tests for the SessionLogger lifecycle."""

    def _make_logger(self, tmp_path: Path, enabled: bool = True) -> SessionLogger:
        db = tmp_path / "test.db"
        return SessionLogger(db, enabled=enabled)

    def _open(self, logger: SessionLogger) -> str:
        sid = logger.open_session(
            agent="claude",
            image="ghcr.io/anthropics/claude-code:latest",
            workspace="/workspace",
            container_id="abc123",
            container_name="vibepod-claude-test",
            vibepod_version="0.2.1",
        )
        return sid  # type: ignore[return-value]

    # -- Schema ----------------------------------------------------------

    def test_schema_created(self, tmp_path):
        logger = self._make_logger(tmp_path)
        self._open(logger)
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "sessions" in tables
        assert "messages" in tables
        conn.close()

    # -- Session lifecycle ------------------------------------------------

    def test_open_creates_session_row(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)
        assert sid is not None and len(sid) == 32

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute(
            "SELECT agent, container_name FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        assert row == ("claude", "vibepod-claude-test")
        conn.close()
        logger.close_session()

    def test_close_sets_ended_at_and_reason(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)
        logger.close_session("keyboard_interrupt")

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute(
            "SELECT ended_at, exit_reason FROM sessions WHERE id = ?", (sid,)
        ).fetchone()
        assert row[0] is not None
        assert row[1] == "keyboard_interrupt"
        conn.close()

    # -- Message logging --------------------------------------------------

    def test_message_logged_on_enter(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # Simulate typing "ls" then pressing Enter (\r)
        logger.log_input(b"ls\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute("SELECT content FROM messages WHERE session_id = ?", (sid,)).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "ls"
        conn.close()

    def test_keystroke_by_keystroke(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # Individual keystrokes then Enter
        for ch in b"hello":
            logger.log_input(bytes([ch]))
        logger.log_input(b"\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "hello"
        conn.close()

    def test_backspace_removes_last_char(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # Type "helo", backspace, then "lo" → "hello"
        logger.log_input(b"helo\x7flo\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "hello"
        conn.close()

    def test_backspace_on_empty_buffer(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        logger.log_input(b"\x7f\x7fhi\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "hi"
        conn.close()

    def test_multiple_messages(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        logger.log_input(b"first\rsecond\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        rows = conn.execute(
            "SELECT content FROM messages WHERE session_id = ? ORDER BY id", (sid,)
        ).fetchall()
        assert [r[0] for r in rows] == ["first", "second"]
        conn.close()

    def test_enter_on_empty_buffer_no_message(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        logger.log_input(b"\r\r\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert count == 0
        conn.close()

    def test_control_chars_ignored(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # \x03 = Ctrl-C — should be ignored
        logger.log_input(b"\x03hi\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "hi"
        conn.close()

    # -- Escape sequence filtering ----------------------------------------

    def test_csi_arrow_keys_ignored(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # ESC [ A = Up arrow, ESC [ B = Down arrow interspersed with text
        logger.log_input(b"\x1b[Ahello\x1b[B\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "hello"
        conn.close()

    def test_csi_with_params_ignored(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # ESC [ 1 ; 5 C = Ctrl+Right arrow (parameterized CSI)
        logger.log_input(b"ab\x1b[1;5Ccd\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "abcd"
        conn.close()

    def test_ss3_sequence_ignored(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # ESC O P = F1 key (SS3 sequence)
        logger.log_input(b"x\x1bOPy\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "xy"
        conn.close()

    def test_two_byte_escape_ignored(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # ESC f = Alt+f (forward word) — two-byte escape
        logger.log_input(b"ab\x1bfcd\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "abcd"
        conn.close()

    def test_tab_characters_ignored(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # Tab (\x09) used for completion — should not appear in output
        logger.log_input(b"doc\tker\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "docker"
        conn.close()

    def test_escape_split_across_calls(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # ESC arrives in one call, [ and A in the next
        logger.log_input(b"hi\x1b")
        logger.log_input(b"[Alo\r")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "hilo"
        conn.close()

    def test_buffered_input_flushed_on_close(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        # Type without pressing Enter
        logger.log_input(b"pending")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        content = conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert content == "pending"
        conn.close()

    # -- Disabled logger --------------------------------------------------

    def test_disabled_no_db(self, tmp_path):
        logger = self._make_logger(tmp_path, enabled=False)
        result = logger.open_session(
            agent="claude",
            image="img",
            workspace="/ws",
            container_id="c1",
            container_name="cn",
            vibepod_version="0.2.1",
        )
        assert result is None
        logger.log_input(b"data\r")
        logger.close_session()
        assert not (tmp_path / "test.db").exists()

    # -- Empty data -------------------------------------------------------

    def test_empty_data_ignored(self, tmp_path):
        logger = self._make_logger(tmp_path)
        sid = self._open(logger)

        logger.log_input(b"")
        logger.close_session()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()[0]
        assert count == 0
        conn.close()

    # -- WAL mode ---------------------------------------------------------

    def test_wal_mode_enabled(self, tmp_path):
        logger = self._make_logger(tmp_path)
        self._open(logger)

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()
        logger.close_session()

    # -- DB path parent creation ------------------------------------------

    def test_creates_parent_dirs(self, tmp_path):
        db = tmp_path / "nested" / "deep" / "logs.db"
        logger = SessionLogger(db, enabled=True)
        logger.open_session(
            agent="claude",
            image="img",
            workspace="/ws",
            container_id="c1",
            container_name="cn",
            vibepod_version="0.2.1",
        )
        logger.close_session()
        assert db.exists()

"""Relay herdr agent state from a container through a file.

Off Linux every container engine runs in a VM whose file share cannot carry
the herdr unix socket, but regular files do cross it. In-container reporters
then append one ``pane.report_agent`` params object per line to
``HERDR_EVENTS_FILE``; the attached ``vp run`` on the host tails that file and
forwards each valid line to the herdr socket. The container is not trusted:
only known fields, the run's own pane and the known states are forwarded.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibepod.core.config import get_config_root

logger = logging.getLogger(__name__)

CONTAINER_EVENTS_DIR = "/herdr-events"
EVENTS_FILENAME = "herdr-events.jsonl"
CONTAINER_EVENTS_FILE = f"{CONTAINER_EVENTS_DIR}/{EVENTS_FILENAME}"

#: Reporters keep each line under this size so O_APPEND writes stay atomic.
MAX_LINE_BYTES = 4096
MAX_FIELD_CHARS = 512
#: Bytes read per poll, so a container growing the file cannot exhaust host memory.
MAX_READ_BYTES = 1024 * 1024
ALLOWED_STATES = frozenset({"working", "blocked", "idle"})
REQUIRED_FIELDS = ("pane_id", "source", "agent", "state")
OPTIONAL_FIELDS = ("display_agent", "agent_session_id")
POLL_INTERVAL = 0.25

# The container can replace the events file, so it is opened without following
# a symlink (Windows lacks O_NOFOLLOW: see _open_events_file) and without
# blocking on a FIFO.
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_OPEN_FLAGS = (
    os.O_RDONLY
    | _O_NOFOLLOW
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)

Sender = Callable[[dict[str, Any]], bool]


def relay_root() -> Path:
    """Parent of the per-run events dirs (under the VibePod config root, which
    lives in the user's home and is therefore shared with every engine VM)."""
    return get_config_root() / "herdr-relay"


def validate_event(line: bytes, pane_id: str) -> dict[str, Any] | None:
    """Return the ``pane.report_agent`` params for a valid line, else None."""
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        event = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    if set(event) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS):
        return None
    for value in event.values():
        if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
            return None
    if any(not event.get(key) for key in REQUIRED_FIELDS):
        return None
    if event["pane_id"] != pane_id or event["state"] not in ALLOWED_STATES:
        return None
    return event


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: alive, owned by someone else
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) terminates the process on Windows, so ask for its exit code instead.
    if sys.platform != "win32":
        raise OSError("only available on Windows")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to someone else.
        return bool(ctypes.get_last_error() == 5)
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _open_events_file(path: Path) -> tuple[int, os.stat_result] | None:
    """Open *path* for reading; None unless it is a regular file.

    Raises OSError when the file is missing or a symlink (ELOOP).
    """
    expected: tuple[int, int] | None = None
    if not _O_NOFOLLOW:
        # no O_NOFOLLOW: refuse a link up front, then require the opened file
        # to be the one inspected so a swap in between is refused too
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            return None
        expected = (before.st_dev, before.st_ino)
    fd = os.open(path, _OPEN_FLAGS)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    if not stat.S_ISREG(st.st_mode) or (
        expected is not None and (st.st_dev, st.st_ino) != expected
    ):
        os.close(fd)
        return None
    return fd, st


def prune_stale_dirs(root: Path) -> None:
    """Remove events dirs left behind by runs whose vp process is gone."""
    if not root.is_dir():
        return
    for child in root.iterdir():
        pid_text = child.name.split("-", 1)[0]
        if not pid_text.isdigit() or int(pid_text) == os.getpid():
            continue
        if not _pid_alive(int(pid_text)):
            shutil.rmtree(child, ignore_errors=True)


class HerdrEventRelay:
    """Tail a run's events file and forward valid lines to the herdr socket.

    Lifecycle: ``prepare()`` creates the per-run dir right before the container
    is created, ``start()`` begins tailing once it runs, and ``close()`` drains
    the remaining lines, stops the thread and removes the dir. ``close()`` is
    idempotent and safe at any stage.
    """

    def __init__(
        self,
        pane_id: str,
        send: Sender,
        *,
        root: Path | None = None,
        interval: float = POLL_INTERVAL,
    ) -> None:
        self.pane_id = pane_id
        self._send = send
        self._root = root or relay_root()
        self.host_dir = self._root / f"{os.getpid()}-{secrets.token_hex(4)}"
        self.host_file = self.host_dir / EVENTS_FILENAME
        self._interval = interval
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._buffer = b""
        self._discarding = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.forwarded = 0
        self.dropped = 0

    def volume(self) -> tuple[str, str, str]:
        return (str(self.host_dir), CONTAINER_EVENTS_DIR, "rw")

    def container_env(self) -> dict[str, str]:
        return {"HERDR_EVENTS_FILE": CONTAINER_EVENTS_FILE}

    def prepare(self) -> None:
        """Create the per-run dir and an empty events file the container can append to."""
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            self._root.chmod(0o700)
            prune_stale_dirs(self._root)
        except OSError:
            pass
        self.host_dir.mkdir()
        # the container may run as another uid (image entrypoints remap users)
        self.host_dir.chmod(0o777)
        self.host_file.touch()
        self.host_file.chmod(0o666)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="herdr-relay", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
            # lines written after the last tick still belong to this run
            self._safe_poll()
        shutil.rmtree(self.host_dir, ignore_errors=True)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._safe_poll()

    def _safe_poll(self) -> None:
        try:
            self.poll()
        except Exception as exc:  # noqa: BLE001 - the relay must never kill the run
            logger.debug("herdr relay: poll failed: %s", exc)

    def poll(self) -> int:
        """Forward every complete new line; return how many were forwarded."""
        with self._lock:
            forwarded = 0
            for line in self._read_lines():
                event = validate_event(line, self.pane_id)
                if event is None:
                    self.dropped += 1
                    logger.debug("herdr relay: dropped invalid event %r", line[:200])
                    continue
                if self._send(event):
                    forwarded += 1
                else:
                    logger.debug("herdr relay: herdr rejected event %r", event)
            self.forwarded += forwarded
            return forwarded

    def _reset(self, identity: tuple[int, int] | None) -> None:
        self._identity = identity
        self._offset = 0
        self._buffer = b""
        self._discarding = False

    def _read_lines(self) -> list[bytes]:
        try:
            opened = _open_events_file(self.host_file)
        except FileNotFoundError:
            self._reset(None)
            return []
        except OSError:  # ELOOP: replaced with a symlink
            opened = None
        if opened is None:
            logger.debug("herdr relay: refused an events file that is not a regular file")
            self._reset(None)
            return []
        fd, st = opened
        try:
            identity = (st.st_dev, st.st_ino)
            if identity != self._identity:
                self._reset(identity)  # first read or the file was replaced
            elif st.st_size < self._offset:
                self._reset(identity)  # truncated
            os.lseek(fd, self._offset, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = MAX_READ_BYTES
            while remaining > 0 and (chunk := os.read(fd, min(remaining, 65536))):
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
        self._offset += len(data)
        self._buffer += data
        *complete, self._buffer = self._buffer.split(b"\n")
        lines: list[bytes] = []
        for line in complete:
            if self._discarding:
                # tail of an oversized line whose head was already dropped
                self._discarding = False
                self.dropped += 1
                continue
            if line.strip():
                lines.append(line)
        if len(self._buffer) > MAX_LINE_BYTES:
            self._buffer = b""
            self._discarding = True
        return lines

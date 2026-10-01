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
ALLOWED_STATES = frozenset({"working", "blocked", "idle"})
REQUIRED_FIELDS = ("pane_id", "source", "agent", "state")
OPTIONAL_FIELDS = ("display_agent", "agent_session_id")
POLL_INTERVAL = 0.25

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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: alive, owned by someone else
        return True
    return True


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
            self.poll()
        shutil.rmtree(self.host_dir, ignore_errors=True)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
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
            with self.host_file.open("rb") as handle:
                st = os.fstat(handle.fileno())
                identity = (st.st_dev, st.st_ino)
                if identity != self._identity:
                    self._reset(identity)  # first read or the file was replaced
                elif st.st_size < self._offset:
                    self._reset(identity)  # truncated
                handle.seek(self._offset)
                data = handle.read()
        except FileNotFoundError:
            self._reset(None)
            return []
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

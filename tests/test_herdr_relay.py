"""Tests for the herdr events-file relay (core/herdr_relay.py) and the
in-container reporters' file fallback."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import socket as socket_module
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from vibepod.core import herdr
from vibepod.core.herdr_relay import (
    CONTAINER_EVENTS_DIR,
    CONTAINER_EVENTS_FILE,
    MAX_LINE_BYTES,
    HerdrEventRelay,
    prune_stale_dirs,
    validate_event,
)

PANE = "w1:p1"


def _event(state: str = "working", **extra: str) -> dict[str, str]:
    return {"pane_id": PANE, "source": "vibepod", "agent": "claude", "state": state, **extra}


def _line(event: dict) -> bytes:
    return (json.dumps(event) + "\n").encode()


class _Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, event: dict) -> bool:
        self.events.append(event)
        return True

    @property
    def states(self) -> list[str]:
        return [event["state"] for event in self.events]


@pytest.fixture
def relay(tmp_path: Path) -> Iterator[tuple[HerdrEventRelay, _Recorder]]:
    recorder = _Recorder()
    relay = HerdrEventRelay(PANE, recorder, root=tmp_path / "relay", interval=0.01)
    relay.prepare()
    try:
        yield relay, recorder
    finally:
        relay.close()


def _append(relay: HerdrEventRelay, data: bytes) -> None:
    with relay.host_file.open("ab") as handle:
        handle.write(data)


# --- validation ---------------------------------------------------------------


def test_validate_accepts_known_fields() -> None:
    event = _event("blocked", display_agent="vp:claude", agent_session_id="s1")
    assert validate_event(json.dumps(event).encode(), PANE) == event


@pytest.mark.parametrize(
    "event",
    [
        _event(pane_id="other"),
        _event("exited"),
        _event(method="pane.release_agent"),
        _event(agent_session_path="/etc/passwd"),
        {"pane_id": PANE, "source": "vibepod", "state": "idle"},
        {**_event(), "agent": ""},
        {**_event(), "agent": 3},
        _event(display_agent="x" * 600),
    ],
)
def test_validate_rejects_untrusted_events(event: dict) -> None:
    assert validate_event(json.dumps(event).encode(), PANE) is None


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b"\xff\xfe", b'"idle"'])
def test_validate_rejects_malformed_lines(raw: bytes) -> None:
    assert validate_event(raw, PANE) is None


def test_validate_rejects_oversized_line() -> None:
    raw = json.dumps(_event(display_agent="x" * 400)).encode() + b" " * MAX_LINE_BYTES
    assert validate_event(raw, PANE) is None


# --- tailing ----------------------------------------------------------------------


def test_prepare_creates_writable_events_file(relay) -> None:
    relay_obj, _ = relay
    assert relay_obj.host_file.is_file()
    assert relay_obj.host_file.stat().st_size == 0
    assert relay_obj.volume() == (str(relay_obj.host_dir), CONTAINER_EVENTS_DIR, "rw")
    assert relay_obj.container_env() == {"HERDR_EVENTS_FILE": CONTAINER_EVENTS_FILE}


def test_poll_forwards_lines_in_order(relay) -> None:
    relay_obj, recorder = relay
    _append(relay_obj, _line(_event("working")) + _line(_event("blocked")))
    _append(relay_obj, _line(_event("idle")))
    assert relay_obj.poll() == 3
    assert recorder.states == ["working", "blocked", "idle"]
    assert relay_obj.poll() == 0


def test_poll_drops_invalid_and_foreign_pane_lines(relay) -> None:
    relay_obj, recorder = relay
    _append(
        relay_obj,
        _line(_event("working"))
        + b"garbage\n"
        + _line(_event("idle", pane_id="someone-else"))
        + b"\n"
        + _line(_event("blocked")),
    )
    relay_obj.poll()
    assert recorder.states == ["working", "blocked"]
    assert relay_obj.dropped == 2


def test_partial_last_line_is_buffered_until_complete(relay) -> None:
    relay_obj, recorder = relay
    raw = _line(_event("blocked"))
    _append(relay_obj, _line(_event("working")) + raw[:10])
    relay_obj.poll()
    assert recorder.states == ["working"]
    _append(relay_obj, raw[10:])
    relay_obj.poll()
    assert recorder.states == ["working", "blocked"]


def test_truncation_restarts_from_the_top(relay) -> None:
    relay_obj, recorder = relay
    _append(relay_obj, _line(_event("working")) + _line(_event("blocked")))
    relay_obj.poll()
    relay_obj.host_file.write_bytes(_line(_event("idle")))
    relay_obj.poll()
    assert recorder.states == ["working", "blocked", "idle"]


def test_replaced_file_is_read_from_the_start(relay) -> None:
    relay_obj, recorder = relay
    _append(relay_obj, _line(_event("working")) + _line(_event("working")))
    relay_obj.poll()
    replacement = relay_obj.host_dir / "new.jsonl"
    replacement.write_bytes(_line(_event("blocked")) + _line(_event("idle")) + _line(_event()))
    os.replace(replacement, relay_obj.host_file)
    relay_obj.poll()
    assert recorder.states == ["working", "working", "blocked", "idle", "working"]


def test_deleted_then_recreated_file_is_picked_up(relay) -> None:
    relay_obj, recorder = relay
    relay_obj.host_file.unlink()
    assert relay_obj.poll() == 0
    relay_obj.host_file.write_bytes(_line(_event("idle")))
    relay_obj.poll()
    assert recorder.states == ["idle"]


def test_oversized_line_is_dropped_without_losing_the_next(relay) -> None:
    relay_obj, recorder = relay
    _append(relay_obj, b"x" * (MAX_LINE_BYTES + 10))
    relay_obj.poll()
    _append(relay_obj, b"y" * 100 + b"\n" + _line(_event("idle")))
    relay_obj.poll()
    assert recorder.states == ["idle"]
    assert relay_obj.dropped == 1


def test_rejected_send_is_not_counted(tmp_path: Path) -> None:
    relay_obj = HerdrEventRelay(PANE, lambda event: False, root=tmp_path / "relay")
    relay_obj.prepare()
    _append(relay_obj, _line(_event()))
    assert relay_obj.poll() == 0
    relay_obj.close()


# --- lifecycle --------------------------------------------------------------------


def test_thread_forwards_and_close_drains_and_removes_dir(tmp_path: Path) -> None:
    recorder = _Recorder()
    relay_obj = HerdrEventRelay(PANE, recorder, root=tmp_path / "relay", interval=0.01)
    relay_obj.prepare()
    relay_obj.start()
    _append(relay_obj, _line(_event("working")))
    for _ in range(200):
        if recorder.events:
            break
        threading.Event().wait(0.01)
    assert recorder.states == ["working"]

    relay_obj.close()
    assert recorder.states == ["working"]
    assert not relay_obj.host_dir.exists()
    relay_obj.close()  # idempotent


def test_close_drains_lines_written_after_the_last_tick(tmp_path: Path) -> None:
    recorder = _Recorder()
    relay_obj = HerdrEventRelay(PANE, recorder, root=tmp_path / "relay", interval=60)
    relay_obj.prepare()
    relay_obj.start()
    _append(relay_obj, _line(_event("working")) + _line(_event("idle")))
    relay_obj.close()
    assert recorder.states == ["working", "idle"]
    assert not relay_obj.host_dir.exists()


def test_close_before_prepare_is_harmless(tmp_path: Path) -> None:
    HerdrEventRelay(PANE, _Recorder(), root=tmp_path / "relay").close()


def test_prepare_prunes_dirs_of_dead_runs(tmp_path: Path) -> None:
    root = tmp_path / "relay"
    root.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    stale = root / f"{dead.pid}-deadbeef"
    stale.mkdir()
    alive = root / f"{os.getppid()}-cafecafe"
    alive.mkdir()
    foreign = root / "not-a-run"
    foreign.mkdir()

    prune_stale_dirs(root)

    assert not stale.exists()
    assert alive.exists()
    assert foreign.exists()


# --- forwarding to a real (fake) herdr socket ------------------------------------------


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    if not hasattr(socket_module, "AF_UNIX"):
        pytest.skip("AF_UNIX sockets unavailable on this platform")
    # short path: sockaddr_un caps socket paths at ~104 bytes on macOS
    parent = Path(tempfile.mkdtemp(prefix="vp-sock-"))
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _serve(sock_path: Path, received: list, count: int) -> threading.Thread:
    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(count)

    def serve() -> None:
        for _ in range(count):
            conn, _ = server.accept()
            received.append(json.loads(conn.recv(65536).decode().splitlines()[0]))
            conn.sendall(b'{"id":"x","result":{}}\n')
            conn.close()
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread


def test_relay_forwards_to_herdr_socket_as_report_agent(
    monkeypatch,
    tmp_path: Path,
    sock_dir: Path,
) -> None:
    received: list = []
    thread = _serve(sock_dir / "herdr.sock", received, 2)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock_dir / "herdr.sock"))
    relay_obj = HerdrEventRelay(PANE, herdr.forward_event, root=tmp_path / "relay")
    relay_obj.prepare()
    _append(relay_obj, _line(_event("working")) + b"{bad\n" + _line(_event("idle")))

    assert relay_obj.poll() == 2
    relay_obj.close()
    thread.join(timeout=5)

    assert [request["method"] for request in received] == ["pane.report_agent"] * 2
    assert [request["params"] for request in received] == [_event("working"), _event("idle")]


def test_create_event_relay_needs_a_reporting_pane(monkeypatch, tmp_path: Path) -> None:
    assert herdr.create_event_relay({}, no_herdr=False) is None
    monkeypatch.setattr(herdr, "pane_reporting_enabled", lambda config, no_herdr: not no_herdr)
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    assert herdr.create_event_relay({}, no_herdr=True) is None
    relay_obj = herdr.create_event_relay({}, no_herdr=False)
    assert relay_obj is not None
    assert relay_obj.pane_id == PANE
    assert relay_obj.host_dir.parent == tmp_path / "herdr-relay"
    assert not relay_obj.host_dir.exists()


def test_apply_wires_events_file_when_socket_cannot_be_mounted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(herdr, "pane_reporting_enabled", lambda config, no_herdr: True)
    monkeypatch.setenv("HERDR_PANE_ID", PANE)
    monkeypatch.setenv("HERDR_TAB_ID", "t1")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    relay_obj = HerdrEventRelay(PANE, _Recorder(), root=tmp_path / "relay")

    volumes, env = herdr.apply_herdr_if_enabled(
        "claude",
        config_dir,
        {},
        no_herdr=False,
        mount_socket=False,
        event_relay=relay_obj,
    )

    assert volumes == [relay_obj.volume()]
    assert env == {
        "HERDR_EVENTS_FILE": CONTAINER_EVENTS_FILE,
        "HERDR_PANE_ID": PANE,
        "HERDR_TAB_ID": "t1",
    }
    # hooks are injected: they report through the events file now
    assert (config_dir / "hooks" / "herdr-agent-state.sh").is_file()
    assert "herdr-agent-state.sh" in (config_dir / "settings.json").read_text()


# --- in-container reporters -------------------------------------------------------------


def _need_node() -> None:
    if shutil.which("node") is None:
        pytest.skip("node not available")


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_claude_hook_appends_to_events_file_without_socket(tmp_path: Path) -> None:
    _need_node()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    herdr.sync_herdr_files("claude", config_dir, {})
    events_file = tmp_path / "herdr-events.jsonl"
    script = config_dir / "hooks" / "herdr-agent-state.sh"
    env = {
        "PATH": os.environ["PATH"],
        "HERDR_EVENTS_FILE": str(events_file),
        "HERDR_PANE_ID": PANE,
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }

    for payload in (
        '{"hook_event_name":"SessionStart","session_id":"s1","transcript_path":"/t"}',
        '{"hook_event_name":"Notification","session_id":"s1"}',
    ):
        proc = subprocess.run(
            ["sh", str(script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        assert proc.returncode == 0

    events = _read_events(events_file)
    # report_agent_session is not relayed: its session path means nothing on the host
    assert events == [
        {
            "pane_id": PANE,
            "source": "vibepod",
            "agent": "claude",
            "display_agent": "vp:claude",
            "state": state,
            "agent_session_id": "s1",
        }
        for state in ("idle", "blocked")
    ]
    assert all(validate_event(json.dumps(event).encode(), PANE) for event in events)
    assert "via=file rc=0" in (config_dir / "herdr-hook.log").read_text()


def test_reporter_prefers_socket_when_both_are_set(tmp_path: Path, sock_dir: Path) -> None:
    _need_node()
    received: list = []
    thread = _serve(sock_dir / "herdr.sock", received, 1)
    events_file = tmp_path / "herdr-events.jsonl"
    reporter = herdr.resource_root() / "herdr-report.js"

    proc = subprocess.run(
        ["node", str(reporter), "pane.report_agent", "codex", "idle"],
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "PATH": os.environ["PATH"],
            "HERDR_SOCKET_PATH": str(sock_dir / "herdr.sock"),
            "HERDR_EVENTS_FILE": str(events_file),
            "HERDR_PANE_ID": PANE,
        },
    )
    thread.join(timeout=5)

    assert proc.returncode == 0, proc.stderr
    assert received[0]["params"]["state"] == "idle"
    assert not events_file.exists()


def test_opencode_plugin_appends_to_events_file(tmp_path: Path) -> None:
    _need_node()
    events_file = tmp_path / "herdr-events.jsonl"
    plugin = herdr.resource_root() / "opencode" / "plugins" / "herdr-agent-state.js"
    module = tmp_path / "plugin.mjs"
    shutil.copy(plugin, module)
    script = (
        f"const {{ HerdrAgentState }} = await import({json.dumps(module.as_uri())});"
        "const hooks = await HerdrAgentState();"
        "await hooks.event({ event: { type: 'message.updated' } });"
        "await hooks.event({ event: { type: 'permission.updated' } });"
        "await hooks.event({ event: { type: 'session.idle' } });"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
        env={
            "PATH": os.environ["PATH"],
            "HERDR_EVENTS_FILE": str(events_file),
            "HERDR_PANE_ID": PANE,
        },
    )
    assert proc.returncode == 0, proc.stderr
    events = _read_events(events_file)
    assert [event["state"] for event in events] == ["working", "blocked", "idle"]
    assert all(event["agent"] == "opencode" for event in events)


def test_tau_extension_appends_to_events_file(monkeypatch, tmp_path: Path) -> None:
    extension_path = herdr.resource_root() / "tau" / "extensions" / "herdr_agent_state.py"
    spec = importlib.util.spec_from_file_location("vibepod_tau_herdr_file", extension_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    handlers: dict = {}

    class FakeTau:
        def on(self, event: str, handler):
            handlers[event] = handler

    module.setup(FakeTau())
    events_file = tmp_path / "herdr-events.jsonl"
    monkeypatch.setenv("HERDR_EVENTS_FILE", str(events_file))
    monkeypatch.setenv("HERDR_PANE_ID", PANE)

    class Context:
        session_id = "tau-1"

    async def exercise() -> None:
        await handlers["turn_start"](object(), Context())
        await handlers["turn_end"](object(), Context())

    asyncio.run(exercise())

    events = _read_events(events_file)
    assert events == [
        {
            "pane_id": PANE,
            "source": "vibepod",
            "agent": "tau",
            "display_agent": "vp:tau",
            "state": state,
            "agent_session_id": "tau-1",
        }
        for state in ("working", "idle")
    ]

"""Tests for the VibePod Dash integration (core/dash.py)."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from vibepod.core import dash

DASH_CONFIG: dict[str, Any] = {"dash": {"url": "http://dash.local:8765", "token": "t0ken"}}


class _Recorder(BaseHTTPRequestHandler):
    """Collects every POST body in ``server.received``; answers /healthz."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/healthz":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        self.server.received.append((json.loads(body), dict(self.headers)))  # type: ignore[attr-defined]
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def dash_server() -> Iterator[Any]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    server.received = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def server_url(server: Any) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture(autouse=True)
def _clean_dash_state() -> Iterator[None]:
    """The warn-once and host-URL caches are process-global."""
    dash.reset_state()
    yield
    dash.reset_state()


# -- configuration --------------------------------------------------------


def test_no_dashboard_configured_is_a_no_op(tmp_path: Path) -> None:
    target, env = dash.apply_dash_if_enabled(
        "claude",
        tmp_path,
        tmp_path,
        {},
        config_mount_path="/claude",
        no_dash=False,
    )
    assert target is None
    assert env == {}


def test_url_and_token_come_from_config() -> None:
    assert dash.resolve_url(DASH_CONFIG) == "http://dash.local:8765"
    assert dash.resolve_token(DASH_CONFIG) == "t0ken"


def test_env_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("VPDASH_URL", "http://other:9000/")
    monkeypatch.setenv("VPDASH_TOKEN", "env-token")
    assert dash.resolve_url(DASH_CONFIG) == "http://other:9000"
    assert dash.resolve_token(DASH_CONFIG) == "env-token"


def test_bare_host_gets_a_scheme() -> None:
    assert dash.resolve_url({"dash": {"url": "dash.local:8765"}}) == "http://dash.local:8765"


def test_disabled_by_config() -> None:
    assert dash.dash_enabled({}) is True
    assert dash.dash_enabled({"dash": False}) is False
    assert dash.dash_enabled({"dash": {"enabled": False, "url": "http://x"}}) is False


def test_no_dash_flag_wins(tmp_path: Path) -> None:
    target, env = dash.apply_dash_if_enabled(
        "claude",
        tmp_path,
        tmp_path,
        DASH_CONFIG,
        config_mount_path="/claude",
        no_dash=True,
    )
    assert target is None
    assert env == {}


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://localhost:8765", "http://host.docker.internal:8765"),
        ("http://127.0.0.1:8765", "http://host.docker.internal:8765"),
        ("http://localhost", "http://host.docker.internal"),
        ("https://dash.example.com", "https://dash.example.com"),
        ("http://192.168.1.5:8765", "http://192.168.1.5:8765"),
    ],
)
def test_loopback_urls_are_rewritten_for_the_container(configured: str, expected: str) -> None:
    assert dash.container_url(configured) == expected


def test_container_url_can_be_pinned_for_a_shared_network() -> None:
    """The dash container on vibepod-network is reachable by name — but only
    from other containers, so the CLI keeps posting to the host URL."""
    config = {"dash": {"url": "http://localhost:8765", "container_url": "http://vibepod-dash:8765"}}
    target = dash.make_target("claude", Path("/work/proj"), config)
    assert target is not None
    assert target.host_url == "http://localhost:8765"
    assert target.container_url == "http://vibepod-dash:8765"
    assert dash.container_env(target, "/claude")["VPDASH_URL"] == "http://vibepod-dash:8765"


def test_container_url_override_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("VPDASH_CONTAINER_URL", "vibepod-dash:8765")
    assert dash.resolve_container_url({}, "http://localhost:8765") == "http://vibepod-dash:8765"


def test_container_url_defaults_to_the_gateway_rewrite() -> None:
    assert (
        dash.resolve_container_url({}, "http://localhost:8765")
        == "http://host.docker.internal:8765"
    )


def test_a_container_only_url_falls_back_to_the_published_port(
    dash_server: Any,
    capsys,
) -> None:
    """`dash.url: http://vibepod-dash:8765` is what people naturally configure
    once the board is on the VibePod network; the CLI must still be able to
    report from the host."""
    port = dash_server.server_address[1]
    config = {"dash": {"url": f"http://vibepod-dash.invalid:{port}"}}

    target = dash.make_target("claude", Path("/work/proj"), config)
    assert target is not None
    # Agents keep the name they can resolve...
    assert target.container_url == f"http://vibepod-dash.invalid:{port}"
    # ...the CLI switches to the loopback address that answered.
    assert target.host_url == f"http://127.0.0.1:{port}"
    assert "container-only" in capsys.readouterr().out

    assert dash.report(target, "idle") is True
    assert dash_server.received[0][0]["state"] == "idle"


def test_the_fallback_is_only_used_when_something_answers(capsys) -> None:
    # Nothing is listening on port 1, so the configured URL is kept and the
    # failure is reported honestly rather than papered over.
    url = "http://vibepod-dash.invalid:1"
    assert dash.usable_host_url(url) == url
    assert "container-only" not in capsys.readouterr().out


def test_a_resolvable_url_is_never_probed(monkeypatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("should not probe a URL whose host resolves")

    monkeypatch.setattr(dash, "_answers", fail)
    assert dash.usable_host_url("http://localhost:8765") == "http://localhost:8765"


def test_report_failures_are_only_warned_about_once(capsys) -> None:
    target = dash.make_target("claude", Path("/work/proj"), {"dash": {"url": "http://127.0.0.1:1"}})
    assert target is not None
    dash.report(target, "idle")
    first = capsys.readouterr().out
    dash.report(target, "done")
    assert first != ""
    assert capsys.readouterr().out == ""


def test_name_resolution_errors_are_recognized() -> None:
    gai = socket.gaierror(-3, "Temporary failure in name resolution")
    assert dash.is_name_resolution_error(gai) is True
    assert dash.is_name_resolution_error(urllib.error.URLError(gai)) is True
    assert dash.is_name_resolution_error(urllib.error.URLError(ConnectionRefusedError())) is False


def test_an_unresolvable_host_explains_itself(capsys) -> None:
    """What `vp run` printed before this hint existed was just errno -3."""
    failure = urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution"))
    dash._warn_once("http://vibepod-dash:8765", "idle", failure)

    out = capsys.readouterr().out
    assert "vibepod-dash" in out
    assert "does not resolve" in out
    assert "dash.container_url" in out


def test_agent_id_is_stable_per_workspace() -> None:
    first = dash.agent_id("claude", Path("/work/proj"), "box")
    assert first == dash.agent_id("claude", Path("/work/proj"), "box")
    assert first != dash.agent_id("claude", Path("/work/other"), "box")
    assert first != dash.agent_id("codex", Path("/work/proj"), "box")
    assert first != dash.agent_id("claude", Path("/work/proj"), "laptop")


def test_agent_identity_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("VPDASH_AGENT_ID", "mine")
    monkeypatch.setenv("VPDASH_AGENT_NAME", "my agent")
    target = dash.make_target("claude", Path("/work/proj"), DASH_CONFIG)
    assert target is not None
    assert (target.agent_id, target.name) == ("mine", "my agent")


def test_display_name_marks_vibepod_runs() -> None:
    assert dash.display_name("claude", Path("/work/vibepod-cli")) == "vp:claude · vibepod-cli"


def test_container_env_carries_identity_and_log(monkeypatch) -> None:
    monkeypatch.setenv("VPDASH_HOST", "box")
    target = dash.make_target("claude", Path("/work/proj"), DASH_CONFIG)
    assert target is not None
    env = dash.container_env(target, "/claude")
    assert env["VPDASH_URL"] == "http://dash.local:8765"
    assert env["VPDASH_AGENT"] == "claude"
    assert env["VPDASH_AGENT_NAME"] == "vp:claude · proj"
    assert env["VPDASH_HOST"] == "box"
    assert env["VPDASH_LOG"] == "/claude/dash-hook.log"
    assert env["VPDASH_TOKEN"] == "t0ken"


def test_container_env_omits_an_unset_token() -> None:
    target = dash.make_target("claude", Path("/work/proj"), {"dash": {"url": "http://d:1"}})
    assert target is not None
    assert "VPDASH_TOKEN" not in dash.container_env(target, "/claude")


# -- file sync ------------------------------------------------------------


@pytest.mark.parametrize("agent", sorted(dash.BUILTIN_INTEGRATIONS))
def test_builtin_files_are_copied_and_executable(agent: str, tmp_path: Path) -> None:
    synced = dash.sync_dash_files(agent, tmp_path, {})
    assert synced == len(dash.BUILTIN_INTEGRATIONS[agent])
    for _, dest_rel in dash.BUILTIN_INTEGRATIONS[agent]:
        dest = tmp_path / dest_rel
        assert dest.is_file()
        assert os.access(dest, os.X_OK)
    # The hook always sits next to the reporter it calls.
    hooks = {(tmp_path / dest).parent for _, dest in dash.BUILTIN_INTEGRATIONS[agent]}
    assert len(hooks) == 1


def test_packaged_resources_exist_for_every_integration() -> None:
    root = dash.resource_root()
    for entries in dash.BUILTIN_INTEGRATIONS.values():
        for resource_rel, _ in entries:
            assert (root / resource_rel).is_file(), resource_rel


def test_custom_integration_entries_are_copied(tmp_path: Path) -> None:
    source = tmp_path / "mine.sh"
    source.write_text("#!/bin/sh\n", encoding="utf-8")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    config = {"dash": {"integrations": {"gemini": [{"source": str(source), "dest": "h/mine.sh"}]}}}

    assert dash.sync_dash_files("gemini", config_dir, config) == 1
    assert (config_dir / "h" / "mine.sh").is_file()


def test_integration_dest_cannot_escape_the_config_dir(tmp_path: Path) -> None:
    source = tmp_path / "mine.sh"
    source.write_text("#!/bin/sh\n", encoding="utf-8")
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    config = {
        "dash": {"integrations": {"gemini": [{"source": str(source), "dest": "../escaped.sh"}]}},
    }

    assert dash.sync_dash_files("gemini", config_dir, config) == 0
    assert not (tmp_path / "escaped.sh").exists()


# -- hook registration ----------------------------------------------------


def test_claude_hooks_registered_once(tmp_path: Path) -> None:
    dash.register_claude_hooks(tmp_path)
    dash.register_claude_hooks(tmp_path)

    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    for event in dash._CLAUDE_EVENTS:
        commands = [
            hook["command"] for entry in settings["hooks"][event] for hook in entry["hooks"]
        ]
        assert commands == ['"$CLAUDE_CONFIG_DIR"/hooks/dash-agent-state.sh']


def test_claude_hooks_keep_existing_entries(tmp_path: Path) -> None:
    existing = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}],
        },
        "env": {"FOO": "bar"},
    }
    (tmp_path / "settings.json").write_text(json.dumps(existing), encoding="utf-8")

    dash.register_claude_hooks(tmp_path)

    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["env"] == {"FOO": "bar"}
    stop_commands = [
        hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]
    ]
    assert stop_commands[0] == "mine.sh"
    assert any("dash-agent-state.sh" in command for command in stop_commands)


def test_claude_hooks_survive_unparsable_settings(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    dash.register_claude_hooks(tmp_path)
    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == "{not json"


def test_codex_notify_goes_into_the_root_table(tmp_path: Path) -> None:
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[profile]\nmodel = "gpt"\n', encoding="utf-8")

    dash.register_codex_notify(tmp_path)
    dash.register_codex_notify(tmp_path)

    content = config_path.read_text(encoding="utf-8")
    assert content.splitlines()[0] == dash._CODEX_NOTIFY_LINE
    assert content.count("dash-agent-state.sh") == 1


def test_codex_notify_leaves_an_existing_program_alone(tmp_path: Path) -> None:
    """herdr registers the same key; whoever got there first keeps it."""
    config_path = tmp_path / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('notify = ["/config/.codex/herdr-agent-state.sh"]\n', encoding="utf-8")

    dash.register_codex_notify(tmp_path)

    assert "dash-agent-state.sh" not in config_path.read_text(encoding="utf-8")


# -- reporting ------------------------------------------------------------


def test_report_posts_the_agent_state(dash_server: Any, monkeypatch) -> None:
    monkeypatch.setenv("VPDASH_HOST", "box")
    config = {"dash": {"url": server_url(dash_server), "token": "t0ken"}}
    target = dash.make_target("claude", Path("/work/proj"), config)
    assert target is not None

    assert dash.report(target, "working", event="run", message="hi", cwd="/work/proj") is True

    payload, headers = dash_server.received[0]
    assert payload["state"] == "working"
    assert payload["agent"] == "claude"
    assert payload["agent_id"] == target.agent_id
    assert payload["name"] == "vp:claude · proj"
    assert payload["message"] == "hi"
    assert payload["host"] == "box"
    assert headers["Authorization"] == "Bearer t0ken"


def test_report_survives_an_unreachable_dashboard(capsys) -> None:
    # Port 1 refuses connections everywhere; nothing must be raised.
    target = dash.make_target("claude", Path("/work/proj"), {"dash": {"url": "http://127.0.0.1:1"}})
    assert target is not None
    assert dash.report(target, "done") is False
    assert "dash" in capsys.readouterr().out.lower()


def test_report_can_stay_quiet(capsys) -> None:
    target = dash.make_target("claude", Path("/work/proj"), {"dash": {"url": "http://127.0.0.1:1"}})
    assert target is not None
    assert dash.report(target, "done", quiet=True) is False
    assert capsys.readouterr().out == ""


def test_apply_wires_env_and_hooks(dash_server: Any, tmp_path: Path) -> None:
    config = {"dash": {"url": server_url(dash_server)}}
    target, env = dash.apply_dash_if_enabled(
        "claude",
        tmp_path,
        Path("/work/proj"),
        config,
        config_mount_path="/claude",
        no_dash=False,
    )
    assert target is not None
    # The CLI keeps the loopback URL; the container gets the gateway one.
    assert target.host_url == server_url(dash_server)
    port = dash_server.server_address[1]
    assert env["VPDASH_URL"] == f"http://host.docker.internal:{port}"
    assert (tmp_path / "hooks" / "dash-agent-state.sh").is_file()
    assert (tmp_path / "hooks" / "vpdash-report.sh").is_file()
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert len(settings["hooks"]) == len(dash._CLAUDE_EVENTS)


def test_target_from_labels_round_trip(dash_server: Any) -> None:
    config = {"dash": {"url": server_url(dash_server)}}
    labels = {dash.AGENT_LABEL: "codex", dash.AGENT_ID_LABEL: "abc123"}

    target = dash.target_from_labels(labels, config)
    assert target is not None
    assert (target.agent, target.agent_id) == ("codex", "abc123")
    # No name: a stop report must not rename the card on the board.
    assert target.name == ""


def test_target_from_labels_needs_both_labels() -> None:
    assert dash.target_from_labels({dash.AGENT_LABEL: "codex"}, DASH_CONFIG) is None
    assert dash.target_from_labels({}, DASH_CONFIG) is None


def test_stop_reports_done_from_container_labels(dash_server: Any, monkeypatch) -> None:
    from vibepod.commands import stop as stop_module

    monkeypatch.setattr(
        stop_module,
        "get_config",
        lambda: {"dash": {"url": server_url(dash_server)}},
    )

    class FakeContainer:
        labels = {
            "vibepod.agent": "codex",
            dash.AGENT_LABEL: "codex",
            dash.AGENT_ID_LABEL: "abc123",
        }

    stop_module._release_agent_entries([FakeContainer()])

    payload, _ = dash_server.received[0]
    assert payload["state"] == "done"
    assert payload["agent_id"] == "abc123"
    assert "name" not in payload


def test_stop_ignores_containers_without_dash_labels(dash_server: Any, monkeypatch) -> None:
    from vibepod.commands import stop as stop_module

    monkeypatch.setattr(
        stop_module,
        "get_config",
        lambda: {"dash": {"url": server_url(dash_server)}},
    )

    class FakeContainer:
        labels = {"vibepod.agent": "codex"}

    stop_module._release_agent_entries([FakeContainer()])
    assert dash_server.received == []


@pytest.mark.skipif(
    shutil.which("curl") is None or os.name == "nt",
    reason="the vendored hooks need POSIX sh and curl",
)
def test_vendored_claude_hook_reports_over_http(dash_server: Any, tmp_path: Path) -> None:
    """Run the injected hook exactly as the container would, minus Docker."""
    config = {"dash": {"url": server_url(dash_server), "token": "t0ken"}}
    target = dash.make_target("claude", Path("/work/proj"), config)
    assert target is not None
    dash.sync_dash_files("claude", tmp_path, {})

    env = {
        **os.environ,
        # The config dir stands in for the container's config mount, so
        # VPDASH_LOG lands next to the hooks the same way it does in a run.
        **dash.container_env(target, str(tmp_path)),
        # The hook runs on this host, so it needs the host-side URL.
        "VPDASH_URL": server_url(dash_server),
    }
    payload = json.dumps(
        {
            "hook_event_name": "Notification",
            "session_id": "s1",
            "cwd": "/work/proj",
            "message": "Claude needs your permission to run Bash",
        },
    )
    subprocess.run(
        [str(tmp_path / "hooks" / "dash-agent-state.sh")],
        input=payload,
        text=True,
        env=env,
        check=True,
        timeout=30,
    )

    body, headers = dash_server.received[0]
    assert body["state"] == "blocked"
    assert body["message"] == "Claude needs your permission to run Bash"
    assert body["agent"] == "claude"
    assert body["agent_id"] == target.agent_id
    assert body["name"] == "vp:claude · proj"
    assert headers["Authorization"] == "Bearer t0ken"
    # The trace log `vp doctor dash` reads back.
    assert "state=blocked" in (tmp_path / "dash-hook.log").read_text(encoding="utf-8")


# -- doctor ---------------------------------------------------------------


def test_doctor_dash_needs_a_url(monkeypatch, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from vibepod.cli import app

    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["doctor", "dash"])
    assert result.exit_code == 1
    assert "no dashboard URL" in result.output


def test_doctor_dash_summarizes_every_agent(monkeypatch, tmp_path: Path, dash_server) -> None:
    from typer.testing import CliRunner

    from vibepod.cli import app
    from vibepod.constants import SUPPORTED_AGENTS

    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("VPDASH_URL", server_url(dash_server))

    result = CliRunner().invoke(app, ["doctor", "dash"])
    output = result.output
    # The reachability probe hits /healthz, which the recorder does not serve;
    # the summary must still be printed.
    for agent in SUPPORTED_AGENTS:
        assert agent in output
    assert "dash integration per agent" in output


def test_run_and_task_expose_the_opt_out() -> None:
    from typer.testing import CliRunner

    from vibepod.cli import app

    runner = CliRunner()
    assert "--no-dash" in runner.invoke(app, ["run", "--help"]).output
    assert "--no-dash" in runner.invoke(app, ["task", "create", "--help"]).output


def test_a_rejected_token_says_where_to_find_the_right_one(capsys) -> None:
    rejection = urllib.error.HTTPError("http://d:8765", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
    dash._warn_once("http://d:8765", "idle", rejection)

    out = capsys.readouterr().out
    assert "rejected the token" in out
    assert "VPDASH_TOKEN" in out
    assert "ingest token" in out

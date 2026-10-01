"""Doctor command smoke tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

from typer.testing import CliRunner

from vibepod.cli import app

runner = CliRunner()


def test_engine_mounts_host_sockets_assumes_capable_when_engine_unreachable(monkeypatch) -> None:
    """A dead engine must not read as a capability skip, so the probe reports the
    real connection error instead of a misleading 'skipped' message."""
    from vibepod.commands import doctor as doctor_cmd

    class _UnreachableEngine:
        def __init__(self) -> None:
            raise RuntimeError("engine not reachable")

    monkeypatch.setattr("vibepod.core.docker.DockerManager", _UnreachableEngine)
    assert doctor_cmd._engine_mounts_host_sockets() is True


def _herdr_doctor_env(monkeypatch, tmp_path: Path, *, sockets: bool) -> Path:
    from vibepod.commands import doctor as doctor_cmd

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "vp"))
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setattr(doctor_cmd, "agent_config_dir", lambda _agent, _profile="default": cfg_dir)
    monkeypatch.setattr(doctor_cmd, "resolve_profile", lambda _profile, _config: "default")
    monkeypatch.setattr(doctor_cmd, "get_config", lambda: {})
    monkeypatch.setattr("vibepod.core.herdr.resolve_socket", lambda: Path("/fake/herdr.sock"))
    monkeypatch.setattr("vibepod.core.herdr.resolve_binary", lambda: None)
    monkeypatch.setattr("vibepod.core.herdr.release_agent", lambda *a, **kw: False)
    monkeypatch.setattr(doctor_cmd, "_engine_mounts_host_sockets", lambda: sockets)
    return cfg_dir


def test_herdr_doctor_summary_reports_socket_transport(monkeypatch, tmp_path: Path, capsys) -> None:
    from vibepod.commands import doctor as doctor_cmd

    _herdr_doctor_env(monkeypatch, tmp_path, sockets=True)
    doctor_cmd.herdr_doctor(agent=None)
    out = capsys.readouterr().out
    assert "socket: the herdr socket is mounted" in out
    assert "file relay" not in out


def test_herdr_doctor_probes_file_relay_on_vm_backed_engine(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """Off Linux the probe replays the hook through the events file and checks
    that the relay forwards what the container wrote."""
    from vibepod.commands import doctor as doctor_cmd
    from vibepod.core import herdr as herdr_core

    cfg_dir = _herdr_doctor_env(monkeypatch, tmp_path, sockets=False)
    herdr_core.sync_herdr_files("claude", cfg_dir, {})
    herdr_core.register_claude_hooks(cfg_dir)
    forwarded: list[dict] = []
    monkeypatch.setattr(herdr_core, "forward_event", lambda e: forwarded.append(e) or True)
    seen: dict = {}

    class _Containers:
        def run(self, image, **kwargs):
            seen.update(kwargs)
            host_dir = next(
                host for host, bind in kwargs["volumes"].items() if bind["bind"] == "/herdr-events"
            )
            env = kwargs["environment"]
            event = {
                "pane_id": env["HERDR_PANE_ID"],
                "source": "vibepod",
                "agent": "claude",
                "state": "idle",
            }
            with (Path(host_dir) / "herdr-events.jsonl").open("a") as handle:
                handle.write(json.dumps(event) + "\n")
            return b""

    class _Manager:
        client = type("_Client", (), {"containers": _Containers()})()

    monkeypatch.setattr("vibepod.core.docker.DockerManager", _Manager)

    doctor_cmd.herdr_doctor(agent="claude")

    out = capsys.readouterr().out
    assert "file relay: this engine runs in a VM" in out
    assert "1 event(s) forwarded to herdr" in out
    assert seen["environment"]["HERDR_EVENTS_FILE"] == "/herdr-events/herdr-events.jsonl"
    assert "HERDR_SOCKET_PATH" not in seen["environment"]
    assert all(bind["bind"] != "/herdr/herdr.sock" for bind in seen["volumes"].values())
    assert forwarded == [
        {"pane_id": "pane-1", "source": "vibepod", "agent": "claude", "state": "idle"},
    ]
    # the probe's events dir is gone again
    assert list((tmp_path / "vp" / "herdr-relay").iterdir()) == []


def test_herdr_doctor_fails_when_no_relayed_event_arrives(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    import pytest
    import typer

    from vibepod.commands import doctor as doctor_cmd
    from vibepod.core import herdr as herdr_core

    cfg_dir = _herdr_doctor_env(monkeypatch, tmp_path, sockets=False)
    herdr_core.sync_herdr_files("claude", cfg_dir, {})
    herdr_core.register_claude_hooks(cfg_dir)

    class _Manager:
        client = type(
            "_Client",
            (),
            {"containers": type("_C", (), {"run": lambda self, image, **kw: b""})()},
        )()

    monkeypatch.setattr("vibepod.core.docker.DockerManager", _Manager)

    with pytest.raises(typer.Exit):
        doctor_cmd.herdr_doctor(agent="claude")
    assert "no event from the container reached herdr" in capsys.readouterr().out


def test_doctor_missing_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent, _profile="default": tmp_path / "does-not-exist",
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 1


def test_doctor_valid_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent, _profile="default": tmp_path,
    )
    future_ms = int((time.time() + 3600) * 1000)
    (tmp_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "refreshToken": "r",
                    "expiresAt": future_ms,
                    "scopes": ["user:inference"],
                },
            },
        ),
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "refreshToken:  present" in result.stdout
    assert "accessToken:   present" in result.stdout


def test_doctor_expired_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent, _profile="default": tmp_path,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    past_ms = int((time.time() - 3600) * 1000)
    (tmp_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "refreshToken": "r",
                    "expiresAt": past_ms,
                },
            },
        ),
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 2
    assert "EXPIRED" in result.stdout


def test_doctor_expired_creds_but_stored_token_is_ok(tmp_path: Path, monkeypatch) -> None:
    """Expired credentials.json should NOT exit 2 when a stored token covers auth."""
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent, _profile="default": tmp_path,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    past_ms = int((time.time() - 3600) * 1000)
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": past_ms}}),
    )
    (tmp_path / "oauth-token").write_text("sk-stored\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "stored long-lived token" in result.stdout


def test_doctor_missing_refresh_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent, _profile="default": tmp_path,
    )
    future_ms = int((time.time() + 3600) * 1000)
    (tmp_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "expiresAt": future_ms,
                },
            },
        ),
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "refreshToken:  MISSING" in result.stdout


def test_doctor_reports_stored_token_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent, _profile="default": tmp_path,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    (tmp_path / "oauth-token").write_text("sk-xyz\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "stored long-lived token" in result.stdout


def test_doctor_reports_host_env_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent, _profile="default": tmp_path,
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-token-abc")
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.stdout
    assert "passed from host env" in result.stdout

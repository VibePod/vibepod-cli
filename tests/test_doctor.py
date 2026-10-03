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


def test_herdr_doctor_vm_backed_engine_skips_injected_file_failures(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """On a VM-backed engine the deliberately-uninjected hook files and unregistered
    settings are reported as expected, not counted as failures."""
    from vibepod.commands import doctor as doctor_cmd

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()

    monkeypatch.setattr(doctor_cmd, "agent_config_dir", lambda _agent, _profile="default": cfg_dir)
    monkeypatch.setattr(doctor_cmd, "resolve_profile", lambda _profile, _config: "default")
    monkeypatch.setattr(doctor_cmd, "get_config", lambda: {})
    monkeypatch.setattr("vibepod.core.herdr.resolve_socket", lambda: Path("/fake/herdr.sock"))
    monkeypatch.setattr("vibepod.core.herdr.resolve_binary", lambda: None)
    monkeypatch.setattr(doctor_cmd, "_engine_mounts_host_sockets", lambda: False)

    # A VM-backed engine can't inject hooks, so this must NOT raise typer.Exit(1)
    # even though no hook files or settings registration exist yet.
    doctor_cmd.herdr_doctor(agent="claude")

    out = capsys.readouterr().out
    assert "run `vp run claude` inside a pane to inject" not in out
    assert "absent (expected" in out


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

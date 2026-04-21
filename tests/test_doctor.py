"""Doctor command smoke tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

from typer.testing import CliRunner

from vibepod.cli import app

runner = CliRunner()


def test_doctor_missing_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir",
        lambda _agent: tmp_path / "does-not-exist",
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 1


def test_doctor_valid_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir", lambda _agent: tmp_path
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
                }
            }
        )
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "refreshToken:  present" in result.stdout
    assert "accessToken:   present" in result.stdout


def test_doctor_expired_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir", lambda _agent: tmp_path
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
                }
            }
        )
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 2
    assert "EXPIRED" in result.stdout


def test_doctor_expired_creds_but_stored_token_is_ok(tmp_path: Path, monkeypatch) -> None:
    """Expired credentials.json should NOT exit 2 when a stored token covers auth."""
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir", lambda _agent: tmp_path
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    past_ms = int((time.time() - 3600) * 1000)
    (tmp_path / ".credentials.json").write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "a", "expiresAt": past_ms}}
        )
    )
    (tmp_path / "oauth-token").write_text("sk-stored\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "stored long-lived token" in result.stdout


def test_doctor_missing_refresh_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir", lambda _agent: tmp_path
    )
    future_ms = int((time.time() + 3600) * 1000)
    (tmp_path / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "expiresAt": future_ms,
                }
            }
        )
    )
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "refreshToken:  MISSING" in result.stdout


def test_doctor_reports_stored_token_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir", lambda _agent: tmp_path
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    (tmp_path / "oauth-token").write_text("sk-xyz\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "stored long-lived token" in result.stdout


def test_doctor_reports_host_env_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "vibepod.commands.doctor.agent_config_dir", lambda _agent: tmp_path
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-token-abc")
    result = runner.invoke(app, ["doctor", "claude"])
    assert result.exit_code == 0
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.stdout
    assert "passed from host env" in result.stdout

"""Quotas are provider observations, not reconstructed token budgets."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.core import subscription_usage as usage

SECRET = "credential-sentinel-do-not-export"


def codex_payload(used=25):
    return {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": used,
                    "windowDurationMins": 300,
                    "resetsAt": 2000000000,
                    "accessToken": SECRET,
                },
                "secondary": {"usedPercent": 40, "windowDurationMins": 10080},
            },
            SECRET: {"primary": {"usedPercent": 0}},
        },
        "email": SECRET,
        "accessToken": SECRET,
    }


def agy_payload():
    return {
        "status": "SUCCESS",
        "response": SECRET,
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": SECRET,
                                "window": "weekly",
                                "remaining_fraction": 0.86,
                                "reset_time": "2033-05-18T03:33:20Z",
                            },
                            {"window": "5h", "remaining_fraction": 0, "disabled": True},
                        ],
                    },
                    {"name": SECRET, "buckets": [{"window": "weekly", "remaining_fraction": 1}]},
                ]
            },
        },
    }


def test_only_allowlisted_quota_fields_reach_disk(tmp_path):
    store = usage.UsageStore(tmp_path / "usage.db")
    for provider, payload in (
        ("codex", codex_payload()),
        ("agy", agy_payload()),
        (
            "claude",
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 12, "resets_at": 2000000000, "token": SECRET}
                },
                "email": SECRET,
            },
        ),
    ):
        store.record(provider, "default", usage.parse_windows(provider, payload))
    encoded = json.dumps(store.snapshot())
    assert SECRET not in encoded
    assert SECRET.encode() not in store.path.read_bytes()
    assert len(store.snapshot()["providers"]) == 3
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad", [True, False, "42", -1, 101, math.nan, math.inf, 10**500])
def test_invalid_percentages_are_unknown_not_zero(bad):
    windows = usage.parse_windows("codex", codex_payload(bad))
    assert windows[0].used_percent is None


def test_codex_windows_and_fallback_are_preserved():
    windows = usage.parse_windows("codex", codex_payload())
    assert [w.used_percent for w in windows] == [25, 40, 0]
    assert [w.window_minutes for w in windows[:2]] == [300, 10080]
    assert (
        usage.parse_windows("codex", {"result": {"rateLimits": {"primary": {"usedPercent": 100}}}})[
            0
        ].used_percent
        == 100
    )
    assert usage.parse_windows("codex", {"result": None}) == []


def test_agy_report_is_not_token_usage_or_model_availability():
    payload = agy_payload()
    windows = usage.parse_windows("agy", payload)
    assert windows[0].used_percent == 14
    assert windows[0].resets_at == 2000000000
    assert windows[1].disabled
    assert windows[2].bucket == "Model group 2 Weekly"
    payload["status"] = "ERROR"
    assert usage.parse_windows("agy", payload) == []
    assert usage.parse_windows("agy", {"usage": {"total_tokens": 2000}}) == []


@pytest.mark.parametrize("payload", [None, [], 5, {"rate_limits": []}, {"result": []}])
def test_schema_drift_does_not_invent_usage(payload):
    for provider in usage.PROVIDERS:
        assert usage.parse_windows(provider, payload) == []


def test_failed_refresh_keeps_last_observation_and_marks_it_stale(tmp_path):
    store = usage.UsageStore(tmp_path / "usage.db")
    now = time.time()
    store.record("codex", "default", usage.parse_windows("codex", codex_payload()), observed_at=now)
    store.record("codex", "default", [], error="probe_failed")
    row = store.snapshot()["providers"][0]
    assert row["windows"][0]["used_percent"] == 25
    assert row["observed_at"] == now
    assert row["error"] == "probe_failed" and row["stale"]
    with pytest.raises(ValueError):
        store.record("codex", "default", [], error=SECRET)


def test_reset_does_not_imply_refill_and_old_snapshots_cannot_overwrite_new(tmp_path):
    store = usage.UsageStore(tmp_path / "usage.db")
    now = time.time()
    store.record("codex", "default", [usage.Window("Codex primary", 95, now - 1)], observed_at=now)
    store.record("codex", "default", [usage.Window("Codex primary", 20)], observed_at=now - 10)
    row = store.snapshot(now=now)["providers"][0]
    assert row["windows"][0]["used_percent"] == 95
    assert row["windows"][0]["stale"]
    assert store.snapshot(now=now + 301)["providers"][0]["stale"]


def container(name="codex-a", agent="codex", profile="default", **labels):
    return SimpleNamespace(
        name=name,
        id=name,
        status="running",
        labels={
            "vibepod.managed": "true",
            "vibepod.agent": agent,
            "vibepod.profile": profile,
            **labels,
        },
        attrs={"Config": {"Env": ["USER_UID=1000", "SECRET=" + SECRET]}},
    )


def test_refresh_respects_profile_and_refuses_ambiguous_containers(tmp_path, monkeypatch):
    targets = [container(), container("work", profile="work")]
    manager = SimpleNamespace(list_managed=lambda: targets)
    monkeypatch.setattr(usage, "DockerManager", lambda: manager)
    calls = []

    def probe(manager, target, provider):
        calls.append(target.name)
        return usage.parse_windows(provider, codex_payload())

    monkeypatch.setattr(usage, "probe", probe)
    store = usage.UsageStore(tmp_path / "usage.db")
    usage.refresh("work", ("codex",), store=store)
    assert calls == ["work"]
    targets.append(container("work-two", profile="work"))
    usage.refresh("work", ("codex",), store=store)
    assert store.snapshot()["providers"][0]["error"] == "multiple_containers"
    usage.refresh("work", ("codex",), container_name="work-two", store=store)
    assert calls == ["work", "work-two"]


def test_custom_api_provider_is_not_a_subscription_probe(tmp_path, monkeypatch):
    manager = SimpleNamespace(list_managed=lambda: [container(**{"vibepod.provider": "local"})])
    monkeypatch.setattr(usage, "DockerManager", lambda: manager)
    monkeypatch.setattr(usage, "probe", lambda *a: pytest.fail("must not run"))
    store = usage.UsageStore(tmp_path / "usage.db")
    usage.refresh("default", ("codex",), store=store)
    assert store.snapshot()["providers"][0]["error"] == "not_running"


def test_capture_proxy_refuses_authentication_probe_without_bypassing_policy(monkeypatch):
    monkeypatch.setattr(usage, "_exec", lambda *args: pytest.fail("no authenticated request"))
    with pytest.raises(usage.ProbeError, match="proxy_capture_enabled"):
        usage.probe(None, container(**{"vibepod.proxy-policy": "policy-id"}), "codex")


@pytest.mark.parametrize(
    "name", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
)
def test_unlabeled_and_custom_proxies_also_refuse_authentication_probe(monkeypatch, name):
    target = container()
    target.attrs["Config"]["Env"].append(f"{name}=http://{SECRET}@vibepod-proxy:8080")
    monkeypatch.setattr(usage, "_exec", lambda *args: pytest.fail("no authenticated request"))
    with pytest.raises(usage.ProbeError, match="proxy_capture_enabled") as caught:
        usage.probe(None, target, "agy")
    assert SECRET not in str(caught.value)


class FakeAPI:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def exec_create(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"Id": "probe"}

    def exec_start(self, *args, **kwargs):
        return iter([self.output])

    def exec_inspect(self, *args):
        return {"ExitCode": 0}


def test_probe_uses_container_user_fixed_commands_and_no_stderr():
    api = FakeAPI(json.dumps(codex_payload()).encode())
    manager = SimpleNamespace(client=SimpleNamespace(api=api))
    assert usage.probe(manager, container(), "codex")[0].used_percent == 25
    args, kwargs = api.calls[0]
    assert args[0] == "codex-a"
    assert args[1][:5] == ["timeout", "--signal=TERM", "--kill-after=2s", "30s", "node"]
    assert kwargs["user"] == "1000" and kwargs["workdir"] == "/tmp"
    assert not kwargs["stderr"] and not kwargs["stdin"]
    assert SECRET not in json.dumps(api.calls)


def test_old_agy_cannot_accidentally_send_usage_as_a_model_prompt():
    api = FakeAPI(b"agy 1.1.10")
    manager = SimpleNamespace(client=SimpleNamespace(api=api))
    with pytest.raises(usage.ProbeError, match="unsupported_version"):
        usage.probe(manager, container(agent="agy"), "agy")
    assert len(api.calls) == 1
    assert api.calls[0][0][1][-2:] == ["agy", "--version"]


def test_stdout_is_bounded_and_provider_errors_are_not_exposed():
    api = FakeAPI(SECRET.encode() + b"x" * usage.MAX_BYTES)
    with pytest.raises(usage.ProbeError) as caught:
        usage.probe(SimpleNamespace(client=SimpleNamespace(api=api)), container(), "codex")
    assert SECRET not in str(caught.value)


def test_setup_preserves_and_restores_existing_statusline(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    root = usage.agent_config_dir("claude")
    root.mkdir(parents=True)
    original = {
        "model": "example",
        "statusLine": {"type": "command", "command": "echo ORIGINAL", "padding": 3},
    }
    (root / "settings.json").write_text(json.dumps(original))
    usage.setup_claude("default")
    usage.setup_claude("default")
    settings = json.loads((root / "settings.json").read_text())
    assert settings["model"] == "example"
    assert settings["statusLine"]["padding"] == 3
    usage.setup_claude("default", disable=True)
    assert json.loads((root / "settings.json").read_text()) == original


def test_disabling_capture_does_not_overwrite_a_new_user_statusline(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    usage.setup_claude("default")
    path = usage.agent_config_dir("claude") / "settings.json"
    changed = {"statusLine": {"type": "command", "command": "echo changed"}}
    path.write_text(json.dumps(changed))
    usage.setup_claude("default", disable=True)
    assert json.loads(path.read_text()) == changed


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for agent helper tests")
def test_claude_wrapper_is_passive_sanitized_and_does_not_refresh_on_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    usage.setup_claude("default")
    root = usage.agent_config_dir("claude")
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(root)}
    script = root / "vibepod-usage/statusline.cjs"
    payload = {
        "session_id": SECRET,
        "transcript_path": SECRET,
        "cost": {"total_api_duration_ms": 125},
        "rate_limits": {
            "five_hour": {"used_percentage": 25, "resets_at": 2000000000},
            "secret": SECRET,
        },
        "access_token": SECRET,
    }
    first = subprocess.run(
        ["node", str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )
    assert first.returncode == 0 and "75% left" in first.stdout
    path = root / "vibepod-usage/snapshot.json"
    initial = path.read_bytes()
    subprocess.run(
        ["node", str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=True,
    )
    assert path.read_bytes() == initial
    assert SECRET not in initial.decode()
    store = usage.UsageStore(tmp_path / "data/usage.db")
    monkeypatch.setattr(
        usage, "DockerManager", lambda: pytest.fail("passive Claude needs no Docker")
    )
    usage.refresh("default", ("claude",), store=store)
    assert store.snapshot()["providers"][0]["windows"][0]["used_percent"] == 25


@pytest.mark.skipif(shutil.which("node") is None or os.name == "nt", reason="Unix Node fixture")
def test_codex_rpc_initializes_then_reads_limits_without_starting_turn(tmp_path):
    binary = tmp_path / "codex"
    record = tmp_path / "requests.jsonl"
    binary.write_text("""#!/usr/bin/env node
const fs = require('node:fs');
require('node:readline').createInterface({input:process.stdin}).on('line', line => {
  fs.appendFileSync(process.env.REQUESTS, line + '\\n');
  const r = JSON.parse(line);
  if (r.id === 1) console.log(JSON.stringify({id:1,result:{}}));
  if (r.id === 2) console.log(JSON.stringify({id:2,result:{
    rateLimits:{primary:{usedPercent:22,resetsAt:2000000000,accessToken:'SECRET'}},
    refreshToken:'SECRET'
  }}));
});
""")
    binary.chmod(0o755)
    script = usage.files("vibepod.resources").joinpath("codex-quota.cjs")
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "REQUESTS": str(record),
        },
    )
    assert result.returncode == 0
    assert "SECRET" not in result.stdout
    assert usage.parse_windows("codex", json.loads(result.stdout))[0].used_percent == 22
    methods = [json.loads(line)["method"] for line in record.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]


def test_cli_json_reports_unavailability_without_a_fake_balance(tmp_path, monkeypatch):
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["usage", "refresh", "--provider", "claude", "--json"])
    assert result.exit_code == 1
    row = json.loads(result.stdout)["providers"][0]
    assert row["windows"] == [] and row["observed_at"] is None
    assert row["error"] == "no_snapshot"

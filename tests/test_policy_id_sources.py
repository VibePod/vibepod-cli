"""Where proxy policy ids come from, and that every source yields a valid ``str``.

PR #235 guards ``validate_policy_id`` against non-string input. These tests pin
down the sources that feed it so the guard can be judged as a fix or as
defensive hardening. The source tests hold on main and the PR; the contract
tests at the bottom fail on main and pass with the guard.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import list_cmd
from vibepod.core.docker import DockerClientError
from vibepod.core.launch import managed_proxy_policy_ids, materialize_launch_policy
from vibepod.core.proxy_filter import container_policy_path
from vibepod.core.proxy_identity import new_policy_id, validate_policy_id

SRC = Path(__file__).resolve().parents[1] / "src" / "vibepod"
POLICY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SINKS = {
    "validate_policy_id",
    "container_policy_path",
    "remove_container_policy",
    "resolve_container_policy",
    "identified_proxy_url",
}


class _Container:
    def __init__(self, labels: Any, *, name: str = "c", status: str = "running") -> None:
        self.labels = labels
        self.name = name
        self.status = status


class _Manager:
    def __init__(self, containers: list[_Container]) -> None:
        self.containers = containers

    def list_managed(self, all_containers: bool = True) -> list[_Container]:  # noqa: ARG002
        return self.containers


@pytest.fixture()
def config(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("VP_PROXY_FILTER_MODE", raising=False)
    return {"proxy": {"db_path": str(tmp_path / "proxy" / "proxy.db")}}


# --- source 1: generator ---


def test_new_policy_id_is_valid_str() -> None:
    for _ in range(50):
        policy_id = new_policy_id()
        assert isinstance(policy_id, str)
        assert POLICY_ID_RE.fullmatch(policy_id)
        assert validate_policy_id(policy_id) == policy_id


# --- source 2: launch materialization (used by run.py and task.py) ---


def test_materialize_launch_policy_returns_valid_str(config, tmp_path: Path) -> None:
    policy_id = materialize_launch_policy(
        _Manager([]),
        config,
        profile="default",
        workspace=tmp_path,
    )
    assert isinstance(policy_id, str)
    assert POLICY_ID_RE.fullmatch(policy_id)
    assert container_policy_path(config, policy_id).is_file()


def test_materialize_launch_policy_ignores_bad_labels(config, tmp_path: Path) -> None:
    manager = _Manager([_Container({"vibepod.proxy-policy": None}), _Container(None)])
    policy_id = materialize_launch_policy(manager, config, profile="default", workspace=tmp_path)
    assert POLICY_ID_RE.fullmatch(policy_id)


# --- source 3: container labels ---


def test_managed_proxy_policy_ids_keeps_only_str() -> None:
    good = "a" * 32
    manager = _Manager(
        [
            _Container({"vibepod.proxy-policy": good}),
            _Container({"vibepod.proxy-policy": None}),
            _Container({"vibepod.proxy-policy": 123}),
            _Container({}),
            _Container(None),
        ],
    )
    assert managed_proxy_policy_ids(manager) == {good}


def test_managed_proxy_policy_ids_unknowable_returns_none() -> None:
    class _Failing:
        def list_managed(self, all_containers: bool = True) -> list[_Container]:  # noqa: ARG002
            raise DockerClientError("down")

    assert managed_proxy_policy_ids(object()) is None
    assert managed_proxy_policy_ids(_Failing()) is None


# --- source 4: `vp list` reads the label directly ---


@pytest.mark.parametrize(
    "labels",
    [{}, {"vibepod.proxy-policy": None}, {"vibepod.proxy-policy": ""}],
)
def test_list_skips_missing_policy_label(monkeypatch, tmp_path: Path, labels) -> None:
    calls: list[Any] = []
    manager = _Manager([_Container({"vibepod.agent": "claude", **labels})])
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(list_cmd, "DockerManager", lambda: manager)
    monkeypatch.setattr(
        list_cmd,
        "resolve_container_policy",
        lambda config, pid: calls.append(pid) or {},
    )

    result = CliRunner().invoke(app, ["list", "--running", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == []


def test_list_passes_str_label_through(monkeypatch, tmp_path: Path) -> None:
    calls: list[Any] = []
    label = "b" * 32
    manager = _Manager([_Container({"vibepod.agent": "claude", "vibepod.proxy-policy": label})])
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(list_cmd, "DockerManager", lambda: manager)
    monkeypatch.setattr(
        list_cmd,
        "resolve_container_policy",
        lambda config, pid: calls.append(pid) or {"mode": "allow"},
    )

    result = CliRunner().invoke(app, ["list", "--running", "--json"])

    assert result.exit_code == 0, result.output
    assert calls == [label]


# --- the sink itself: reject non-str on both main (TypeError) and PR (ValueError) ---


# These fail on main (TypeError escapes) and pass with PR #235. Real callers
# never pass non-str (see above), so this pins the documented contract.


@pytest.mark.parametrize("bad", [None, 123, b"a" * 32])
def test_validate_policy_id_raises_value_error_for_non_str(bad: Any) -> None:
    with pytest.raises(ValueError, match="Invalid proxy policy id"):
        validate_policy_id(bad)


def test_list_reports_unavailable_for_non_str_policy_label(monkeypatch, tmp_path: Path) -> None:
    # `vp list` catches only (OSError, ValueError); a TypeError would crash it.
    manager = _Manager([_Container({"vibepod.agent": "claude", "vibepod.proxy-policy": 123})])
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(list_cmd, "DockerManager", lambda: manager)

    result = CliRunner().invoke(app, ["list", "--running", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["running"][0]["proxy_mode"] == "unavailable"


# --- call-site audit: new callers must be reviewed against this file ---


def _sink_call_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in SINKS:
                sites.add((path.relative_to(SRC).as_posix(), name))
    return sites


def test_sink_call_sites_are_the_audited_ones() -> None:
    assert _sink_call_sites() == {
        ("commands/list_cmd.py", "resolve_container_policy"),
        ("commands/run.py", "remove_container_policy"),
        ("commands/task.py", "remove_container_policy"),
        ("core/launch.py", "identified_proxy_url"),
        ("core/proxy_filter.py", "validate_policy_id"),
        ("core/proxy_filter.py", "container_policy_path"),
        ("core/proxy_identity.py", "validate_policy_id"),
    }

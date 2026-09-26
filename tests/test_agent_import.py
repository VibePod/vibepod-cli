"""Tests for the agent configuration import core."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.core.agent_import import (
    DEFAULT_CATEGORIES,
    OPT_IN_CATEGORIES,
    ImportConflictError,
    agent_import_entries,
    apply_import,
    host_path_warnings,
    plan_import,
    scan_host,
)
from vibepod.core.agents import get_agent_spec


def test_every_supported_agent_has_import_entries() -> None:
    for agent in SUPPORTED_AGENTS:
        entries = agent_import_entries(agent)
        assert entries, f"{agent} has no import entries"


def test_entries_use_relative_paths_only() -> None:
    for agent in SUPPORTED_AGENTS:
        for entry in agent_import_entries(agent):
            assert not entry.source.startswith("/"), (agent, entry.source)
            assert not entry.source.startswith("~"), (agent, entry.source)
            assert not entry.dest.startswith("/"), (agent, entry.dest)
            assert ".." not in entry.source.split("/"), (agent, entry.source)
            assert ".." not in entry.dest.split("/"), (agent, entry.dest)


def test_unsupported_agent_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported agent"):
        agent_import_entries("nope")


def test_default_and_opt_in_categories_are_disjoint() -> None:
    assert not DEFAULT_CATEGORIES & OPT_IN_CATEGORIES


#: Agents whose mount is their own config dir rather than a container HOME.
_CONFIG_DIR_AGENTS = {"claude", "qwen", "freebuff", "hermes"}


def test_home_mounted_agents_keep_home_relative_layout() -> None:
    """For HOME-mounted agents the destination equals the host-home-relative path."""
    for agent in SUPPORTED_AGENTS:
        if agent in _CONFIG_DIR_AGENTS:
            continue
        for entry in agent_import_entries(agent):
            assert entry.dest == entry.source, (
                f"{agent}: {entry.source} -> {entry.dest}; HOME-mounted agents keep "
                "the path relative to HOME, see AGENT_SPECS extra_env"
            )


def test_config_dir_agents_strip_their_host_prefix() -> None:
    """claude/qwen/freebuff/hermes mount the config dir itself, so the prefix is dropped."""
    prefixes = {
        "claude": ".claude/",
        "qwen": ".qwen",
        "freebuff": ".config/manicode",
        "hermes": ".hermes/",
    }
    for agent, prefix in prefixes.items():
        for entry in agent_import_entries(agent):
            assert entry.source.startswith(prefix), (agent, entry.source)
            assert not entry.dest.startswith(prefix), (agent, entry.dest)


def test_config_dir_agents_are_exactly_the_non_home_mounts() -> None:
    """The exemption list matches the specs, so a new agent cannot drift silently."""
    derived = set()
    for agent in SUPPORTED_AGENTS:
        spec = get_agent_spec(agent)
        if spec.extra_env.get("HOME") != spec.config_mount_path:
            derived.add(agent)
    assert derived == _CONFIG_DIR_AGENTS


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_plan_maps_sources_onto_destination(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "agents" / "claude"
    _write(home / ".claude" / "settings.json", "{}")
    _write(home / ".claude" / "commands" / "ship.md")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    mapped = {
        (f.source.relative_to(home).as_posix(), f.dest.relative_to(dest).as_posix())
        for f in plan.files
    }
    assert mapped == {
        (".claude/settings.json", "settings.json"),
        (".claude/commands/ship.md", "commands/ship.md"),
    }


def test_plan_skips_categories_not_selected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "{}")
    _write(home / ".claude" / ".credentials.json", "{}")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    assert not any(f.category == "credentials" for f in plan.files)
    assert any("credentials" in s.reason for s in plan.skipped)


@pytest.mark.parametrize(
    ("agent", "credential"),
    [
        ("pi", ".pi/agent/auth.json"),
        ("tau", ".tau/credentials.json"),
        ("hermes", ".hermes/.env"),
        ("hermes", ".hermes/auth.json"),
    ],
)
def test_blanket_entries_leave_credentials_opt_in(
    tmp_path: Path,
    agent: str,
    credential: str,
) -> None:
    """A directory-wide settings entry must not carry the agent's key file along."""
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    _write(home / credential, "{}")

    plan = plan_import(agent, home, dest, DEFAULT_CATEGORIES)
    assert plan.files == []

    plan = plan_import(agent, home, dest, DEFAULT_CATEGORIES | {"credentials"})
    assert [f.category for f in plan.files] == ["credentials"]


def test_pi_models_live_under_the_agent_dir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    _write(home / ".pi" / "agent" / "models.json", "{}")

    plan = plan_import("pi", home, dest, DEFAULT_CATEGORIES)

    assert [(f.category, f.dest.relative_to(dest).as_posix()) for f in plan.files] == [
        ("models", ".pi/agent/models.json"),
    ]


def test_plan_includes_credentials_when_selected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    _write(home / ".claude" / ".credentials.json", "{}")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"credentials"})

    assert [f.dest.name for f in plan.files] == [".credentials.json"]


def test_plan_reports_conflicts_without_touching_disk(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "new")
    _write(dest / "settings.json", "old")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    assert [c.dest.name for c in plan.conflicts] == ["settings.json"]
    assert (dest / "settings.json").read_text() == "old"


def test_plan_skips_symlinks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    _write(tmp_path / "outside" / "secret.json", "s")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").symlink_to(tmp_path / "outside" / "secret.json")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    assert not plan.files
    assert any("symlink" in s.reason for s in plan.skipped)


def test_plan_reports_unclassified_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "{}")
    _write(home / ".claude" / "brand-new-thing.json", "{}")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    assert [p.name for p in plan.unclassified] == ["brand-new-thing.json"]


def test_apply_copies_files_and_creates_parents(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "commands" / "ship.md", "body")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    result = apply_import(plan, force=False)

    assert result.copied == 1
    assert not result.failed
    assert (dest / "commands" / "ship.md").read_text() == "body"


def test_apply_refuses_conflicts_without_force(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "new")
    _write(dest / "settings.json", "old")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    with pytest.raises(ImportConflictError):
        apply_import(plan, force=False)
    assert (dest / "settings.json").read_text() == "old"


def test_apply_overwrites_only_planned_files_with_force(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "new")
    _write(dest / "settings.json", "old")
    _write(dest / "untouched.json", "keep")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    apply_import(plan, force=True)

    assert (dest / "settings.json").read_text() == "new"
    assert (dest / "untouched.json").read_text() == "keep"


def test_apply_tightens_credential_permissions(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / ".credentials.json", "{}")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"credentials"})

    apply_import(plan, force=False)

    mode = stat.S_IMODE((dest / ".credentials.json").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file modes")
def test_apply_reports_unreadable_sources(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    target = home / ".claude" / "settings.json"
    _write(target, "{}")
    target.chmod(0o000)
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    result = apply_import(plan, force=False)

    target.chmod(0o600)  # so tmp_path cleanup works
    assert result.copied == 0
    assert len(result.failed) == 1


def test_scan_host_reports_agents_with_an_installation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".claude" / "settings.json", "{}")
    _write(home / ".codex" / "config.toml", "")

    found = scan_host(home)

    assert set(found) == {"claude", "codex"}
    assert home / ".claude" in found["claude"]


def test_scan_host_ignores_empty_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    assert scan_host(home) == {}


def test_host_path_warnings_flags_absolute_host_paths(tmp_path: Path) -> None:
    config = tmp_path / "settings.json"
    _write(config, '{"hook": "/Users/harald/bin/lint.sh"}')

    warnings = host_path_warnings([config])

    assert len(warnings) == 1
    assert "/Users/harald/bin/lint.sh" in warnings[0][1]


def test_host_path_warnings_ignores_binaries_and_clean_config(tmp_path: Path) -> None:
    _write(tmp_path / "clean.json", '{"model": "opus"}')
    (tmp_path / "blob.bin").write_bytes(b"\x00\xff/Users/x")

    assert host_path_warnings([tmp_path / "clean.json", tmp_path / "blob.bin"]) == []


def test_docs_table_matches_the_import_map() -> None:
    """docs/import.md lists every agent; the CI script enforces the detail."""
    docs = (Path(__file__).resolve().parents[1] / "docs" / "import.md").read_text()
    for agent in SUPPORTED_AGENTS:
        assert f"`{agent}`" in docs, f"docs/import.md does not mention {agent}"

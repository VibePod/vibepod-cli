"""Tests for the agent configuration import core."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.core.agent_import import (
    AGENT_ROOTS,
    DEFAULT_CATEGORIES,
    IMPORT_SPECS,
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


#: A known on-disk credential per agent; every agent in the import table is listed.
_KNOWN_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "claude": (".claude/.credentials.json",),
    "opencode": (".local/share/opencode/auth.json",),
    "codex": (".codex/auth.json",),
    "gemini": (".gemini/oauth_creds.json", ".gemini/mcp-oauth-tokens.json", ".gemini/.env"),
    "devstral": (".config/mistral/.env",),
    "auggie": (".augment/session.json",),
    "copilot": (".copilot/config.json",),
    "pi": (".pi/agent/auth.json",),
    "agy": (".agy/oauth_token.json",),
    "tau": (".tau/credentials.json",),
    "jcode": (".jcode/auth.json", ".config/jcode/auth.json"),
    "freebuff": (".config/manicode/credentials.json",),
    "qwen": (".qwen/oauth_creds.json", ".qwen/.env"),
    "dsh": (".dsh/.credentials.yaml",),
    "hermes": (".hermes/.env", ".hermes/auth.json"),
}


def test_known_credentials_cover_every_agent() -> None:
    assert set(_KNOWN_CREDENTIALS) == set(IMPORT_SPECS)


@pytest.mark.parametrize(
    ("agent", "credential"),
    [(agent, path) for agent, paths in _KNOWN_CREDENTIALS.items() for path in paths],
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


_CREDENTIAL_LIKE_NAMES = (
    "oauth_creds.json",
    "mcp-oauth-tokens.json",
    "credentials.json",
    ".credentials.yaml",
    "auth.json",
    "api_token",
    "client_secret.json",
    ".env",
    "private.key",
    "cert.pem",
)


@pytest.mark.parametrize("agent", sorted(IMPORT_SPECS))
def test_credential_named_files_under_any_entry_stay_opt_in(tmp_path: Path, agent: str) -> None:
    """Whatever a directory entry holds, credential-looking files are never default-on."""
    home = tmp_path / "home"
    dest = tmp_path / "dest"
    for entry in agent_import_entries(agent):
        for name in _CREDENTIAL_LIKE_NAMES:
            _write(home / entry.source / "nested" / name, "{}")

    plan = plan_import(agent, home, dest, DEFAULT_CATEGORIES)
    assert plan.files == []

    plan = plan_import(agent, home, dest, DEFAULT_CATEGORIES | {"credentials"})
    assert plan.files
    assert {f.category for f in plan.files} == {"credentials"}


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


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra privileges on Windows")
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


def test_with_other_copies_unclassified_files(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "{}")
    _write(home / ".claude" / "ide" / "state.json", "{}")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"other"})

    assert plan.unclassified == []
    assert {(f.category, f.dest.relative_to(dest).as_posix()) for f in plan.files} == {
        ("settings", "settings.json"),
        ("other", "ide/state.json"),
    }
    apply_import(plan, force=False)
    assert (dest / "ide" / "state.json").read_text() == "{}"


def test_with_other_keeps_the_home_relative_layout(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".config" / "opencode" / "themes" / "dark.json", "{}")

    plan = plan_import("opencode", home, dest, DEFAULT_CATEGORIES | {"other"})

    assert [f.dest.relative_to(dest).as_posix() for f in plan.files] == [
        ".config/opencode/themes/dark.json",
    ]


def test_with_other_reports_conflicts(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "ide" / "state.json", "new")
    _write(dest / "ide" / "state.json", "old")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"other"})

    assert [c.dest.relative_to(dest).as_posix() for c in plan.conflicts] == ["ide/state.json"]


def test_with_other_leaves_credential_named_files_opt_in(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "ide" / "api_token", "t")

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"other"})
    assert plan.files == []
    assert any("--with-credentials" in s.reason for s in plan.skipped)

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"other", "credentials"})
    assert [f.category for f in plan.files] == ["credentials"]


def test_with_other_skips_the_hermes_installer_checkout(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".hermes" / "hermes-agent" / "run.py", "")
    _write(home / ".hermes" / "cron" / "jobs.json", "{}")

    plan = plan_import("hermes", home, dest, DEFAULT_CATEGORIES | {"other"})

    assert [f.dest.relative_to(dest).as_posix() for f in plan.files] == ["cron/jobs.json"]


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


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX-only")
def test_apply_tightens_credential_permissions(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / ".credentials.json", "{}")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"credentials"})

    apply_import(plan, force=False)

    mode = stat.S_IMODE((dest / ".credentials.json").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra privileges on Windows")
def test_dangling_destination_symlink_is_not_written_through(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "new")
    dest.mkdir()
    outside = tmp_path / "outside" / "planted.json"
    (dest / "settings.json").symlink_to(outside)

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)
    assert not plan.files
    assert any("destination" in s.reason and "symlink" in s.reason for s in plan.skipped)

    apply_import(plan, force=True)
    assert not outside.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra privileges on Windows")
def test_symlinked_destination_directory_is_refused(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "commands" / "ship.md", "body")
    outside = tmp_path / "outside"
    outside.mkdir()
    dest.mkdir()
    (dest / "commands").symlink_to(outside)

    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)
    apply_import(plan, force=True)

    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra privileges on Windows")
def test_apply_rechecks_destination_symlinks(tmp_path: Path) -> None:
    """A symlink planted between planning and applying is still refused."""
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "new")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)
    dest.mkdir()
    outside = tmp_path / "outside.json"
    (dest / "settings.json").symlink_to(outside)

    result = apply_import(plan, force=True)

    assert result.copied == 0
    assert len(result.failed) == 1
    assert "symlink" in result.failed[0][1]
    assert not outside.exists()
    assert (dest / "settings.json").is_symlink()


def test_failed_copy_leaves_destination_and_no_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / "settings.json", "new")
    _write(dest / "settings.json", "old")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("vibepod.core.agent_import.shutil.copyfileobj", _boom)
    result = apply_import(plan, force=True)

    assert result.copied == 0
    assert (dest / "settings.json").read_text() == "old"
    assert sorted(p.name for p in dest.iterdir()) == ["settings.json"]


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX-only")
def test_credential_is_never_visible_with_a_wider_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".claude" / ".credentials.json", "{}")
    plan = plan_import("claude", home, dest, DEFAULT_CATEGORIES | {"credentials"})
    modes: list[int] = []
    real_replace = os.replace

    def _replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        modes.append(stat.S_IMODE(os.stat(src).st_mode))
        real_replace(src, dst)

    monkeypatch.setattr("vibepod.core.agent_import.os.replace", _replace)
    apply_import(plan, force=False)

    assert modes == [0o600]
    assert (dest / ".credentials.json").read_text() == "{}"


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="file modes are POSIX-only and root bypasses them",
)
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


def test_agent_roots_map_every_entry() -> None:
    """Each entry sits under one agent root and maps onto the root's destination."""
    assert set(AGENT_ROOTS) == set(IMPORT_SPECS)
    for agent, entries in IMPORT_SPECS.items():
        for entry in entries:
            matches = [
                root
                for root in AGENT_ROOTS[agent]
                if entry.source == root.source or entry.source.startswith(f"{root.source}/")
            ]
            assert len(matches) == 1, (agent, entry.source)
            root = matches[0]
            rest = entry.source[len(root.source) :].lstrip("/")
            expected = "/".join(part for part in (root.dest, rest) if part)
            assert entry.dest == expected, (agent, entry.source, entry.dest)


def test_agent_roots_are_agent_specific() -> None:
    for agent, roots in AGENT_ROOTS.items():
        for root in roots:
            assert root.source not in {".config", ".local", ".local/share"}, (agent, root)


def test_scan_host_ignores_unrelated_config_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".config" / "git" / "config", "")
    _write(home / ".local" / "share" / "fonts" / "a.ttf", "")

    assert scan_host(home) == {}


def test_scan_host_reports_the_agent_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".config" / "opencode" / "opencode.json", "{}")

    assert scan_host(home) == {"opencode": [home / ".config" / "opencode"]}


def test_unclassified_files_come_from_agent_directories_only(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".config" / "opencode" / "opencode.json", "{}")
    _write(home / ".config" / "opencode" / "themes" / "dark.json", "{}")
    _write(home / ".config" / "git" / "config", "")

    plan = plan_import("opencode", home, dest, DEFAULT_CATEGORIES)

    assert [p.relative_to(home).as_posix() for p in plan.unclassified] == [
        ".config/opencode/themes/dark.json",
    ]


def test_hermes_installer_checkout_is_not_reported(tmp_path: Path) -> None:
    home, dest = tmp_path / "home", tmp_path / "dest"
    _write(home / ".hermes" / "config.yaml", "")
    _write(home / ".hermes" / "hermes-agent" / "run.py", "")

    plan = plan_import("hermes", home, dest, DEFAULT_CATEGORIES)

    assert plan.unclassified == []


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

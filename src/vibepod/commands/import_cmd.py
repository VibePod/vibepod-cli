"""`vp import`: copy an existing agent configuration into a profile."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vibepod.constants import SUPPORTED_AGENTS
from vibepod.core.agent_import import (
    ALL_CATEGORIES,
    CATEGORY_FLAGS,
    DEFAULT_CATEGORIES,
    Category,
    ImportConflictError,
    ImportEntry,
    ImportPlan,
    agent_import_entries,
    apply_import,
    host_path_warnings,
    plan_import,
    scan_host,
)
from vibepod.core.agents import agent_config_dir, resolve_agent_name
from vibepod.core.config import get_config
from vibepod.core.profiles import (
    create_profile,
    profile_exists,
    resolve_profile,
    validate_profile_name,
)
from vibepod.utils.console import console, error, info, success, warning


def _parse_categories(raw: str) -> set[Category]:
    """Parse a comma-separated category list. Raises ValueError on unknown names."""
    names = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = sorted(name for name in names if name not in ALL_CATEGORIES)
    if unknown:
        raise ValueError(f"Unknown category: {', '.join(unknown)}")
    return {name for name in ALL_CATEGORIES if name in names}


def _resolve_categories(only: str | None, skip: str | None, extras: set[Category]) -> set[Category]:
    """Turn the CLI selection into the category set to copy."""
    selected = _parse_categories(only) if only else set(DEFAULT_CATEGORIES) | extras
    if skip:
        selected -= _parse_categories(skip)
    return selected


def _print_plan(plan: ImportPlan) -> None:
    for planned in plan.files:
        rel_source = planned.source.relative_to(plan.source_root)
        rel_dest = planned.dest.relative_to(plan.dest_root)
        console.print(f"  {planned.category:<12} {rel_source}  ->  {rel_dest}")
    for skipped in plan.skipped:
        console.print(f"  [dim]skip[/dim]         {skipped.source.name}  ({skipped.reason})")
    if plan.unclassified:
        warning(
            f"{len(plan.unclassified)} unrecognized file(s) not copied "
            "(use --with-other, or open an issue so they get classified)",
        )


def _print_agent_help(agent: str) -> None:
    """Describe, from IMPORT_SPECS, exactly what an import copies for *agent*."""
    info(f"vp import {agent} copies these categories:")
    by_category: dict[Category, list[ImportEntry]] = {}
    for entry in agent_import_entries(agent):
        by_category.setdefault(entry.category, []).append(entry)
    # Categories copied by default first, opt-in ones after them.
    for category in sorted(by_category, key=lambda name: (name not in DEFAULT_CATEGORIES, name)):
        default = "on by default" if category in DEFAULT_CATEGORIES else "opt-in"
        flag = CATEGORY_FLAGS.get(category)
        suffix = f" ({default}: {flag})" if flag else f" ({default})"
        console.print(f"\n  [bold]{category}[/bold]{suffix}")
        for entry in by_category[category]:
            dest = entry.dest or "<agent dir root>"
            console.print(f"    ~/{entry.source}  ->  {dest}")
            if entry.note:
                console.print(f"      [dim]{entry.note}[/dim]")
    console.print("")
    info("Files no category claims are reported and copied only with --with-other.")


def _resolve_source(
    agent: str,
    source_home: Path,
    from_profile: str | None,
    dest_root: Path,
) -> tuple[Path, tuple[ImportEntry, ...] | None]:
    """Return the source root and the entry override (None = use the host table)."""
    if from_profile is None:
        return source_home, None
    if not profile_exists(from_profile):
        error(f"Profile '{from_profile}' does not exist.")
        raise typer.Exit(code=1)
    source_root = agent_config_dir(agent, from_profile)
    if source_root == dest_root:
        error("Source and destination are the same directory.")
        raise typer.Exit(code=1)
    # In a profile the layout already matches the destination, so each entry
    # maps its own destination path onto itself; classification is preserved.
    entries = tuple(
        ImportEntry(
            source=entry.dest or ".",
            dest=entry.dest,
            category=entry.category,
            exclude=entry.exclude,
            note=entry.note,
        )
        for entry in agent_import_entries(agent)
    )
    return source_root, entries


def _scan_and_report(source_home: Path) -> None:
    found = scan_host(source_home)
    if not found:
        warning(f"No supported agent configuration found under {source_home}.")
        return
    info(f"Found agent configuration under {source_home}:")
    for agent, roots in sorted(found.items()):
        paths = ", ".join(str(root) for root in roots)
        console.print(f"  {agent:<10} {paths}")
    console.print("")
    info("Import one with: vp import <agent> [--to-profile NAME] [--dry-run]")
    console.print(f"  e.g. vp import {sorted(found)[0]} --dry-run")


def import_config(
    agent: Annotated[str | None, typer.Argument(help="Agent to import")] = None,
    help_agent: Annotated[
        bool,
        typer.Option("--help-agent", help="Show what gets copied for this agent, then exit"),
    ] = False,
    home: Annotated[
        Path | None,
        typer.Option("--home", help="Source home directory (defaults to your home)"),
    ] = None,
    from_profile: Annotated[
        str | None,
        typer.Option("--from-profile", help="Copy from this profile instead of the host"),
    ] = None,
    to_profile: Annotated[
        str | None,
        typer.Option("--to-profile", help="Destination profile (defaults to the active one)"),
    ] = None,
    create_profile_flag: Annotated[
        bool,
        typer.Option("--create-profile", help="Create the destination profile when missing"),
    ] = False,
    only: Annotated[
        str | None,
        typer.Option("--only", help="Comma-separated categories to copy"),
    ] = None,
    skip: Annotated[
        str | None,
        typer.Option("--skip", help="Comma-separated categories to leave out"),
    ] = None,
    with_credentials: Annotated[
        bool,
        typer.Option("--with-credentials", help="Include stored credentials"),
    ] = False,
    with_sessions: Annotated[
        bool,
        typer.Option("--with-sessions", help="Include session transcripts and caches"),
    ] = False,
    with_other: Annotated[
        bool,
        typer.Option("--with-other", help="Include files no category claims"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the plan, write nothing"),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Overwrite conflicting files")] = False,
) -> None:
    """Copy an existing agent configuration into a VibePod profile.

    Run without an agent to scan your home directory for installed agents.
    Run `vp import <agent> --help-agent` to see exactly which files that agent
    copies and which categories are opt-in.
    """
    source_home = (home or Path.home()).expanduser()

    if agent is None:
        _scan_and_report(source_home)
        return

    resolved = resolve_agent_name(agent)
    if resolved is None:
        error(f"Unsupported agent '{agent}'. Supported: {', '.join(sorted(SUPPORTED_AGENTS))}")
        raise typer.Exit(code=1)

    if help_agent:
        _print_agent_help(resolved)
        return

    extras: set[Category] = set()
    if with_credentials:
        extras.add("credentials")
    if with_sessions:
        extras.add("sessions")
    if with_other:
        extras.add("other")
    try:
        categories = _resolve_categories(only, skip, extras)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc

    config = get_config()
    try:
        dest_profile = to_profile or resolve_profile(None, config)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc

    if to_profile and not profile_exists(to_profile):
        if not create_profile_flag:
            error(
                f"Profile '{to_profile}' does not exist. Create it with: "
                f"vp profile create {to_profile} (or pass --create-profile)",
            )
            raise typer.Exit(code=1)
        try:
            validate_profile_name(to_profile)
            create_profile(to_profile)
        except (ValueError, OSError) as exc:
            error(f"Could not create profile '{to_profile}': {exc}")
            raise typer.Exit(code=1) from exc
        success(f"Created profile '{to_profile}'")

    dest_root = agent_config_dir(resolved, dest_profile)
    source_root, entries = _resolve_source(resolved, source_home, from_profile, dest_root)

    # A profile source is a single directory, so unmapped files are looked for
    # in it directly rather than under the host dotdirs the table names.
    unclassified_roots = ("",) if from_profile else None
    plan = plan_import(
        resolved,
        source_root,
        dest_root,
        categories,
        entries=entries,
        unclassified_roots=unclassified_roots,
    )
    if plan.is_empty and not plan.skipped:
        checked = ", ".join(
            str(source_root / entry.source) for entry in agent_import_entries(resolved)
        )
        error(f"No {resolved} configuration found. Checked: {checked}")
        raise typer.Exit(code=1)

    info(f"{resolved}: {source_root}  ->  {dest_root}")
    _print_plan(plan)

    if dry_run:
        info("Dry run: nothing was written.")
        return

    try:
        result = apply_import(plan, force=force)
    except ImportConflictError as exc:
        error(
            f"{len(exc.conflicts)} destination file(s) already exist; nothing was written. "
            "Re-run with --force to overwrite them:",
        )
        for planned in exc.conflicts:
            console.print(f"  {planned.dest}")
        raise typer.Exit(code=1) from exc

    for path, reason in result.failed:
        warning(f"Failed to copy {path}: {reason}")
    for path, matches in host_path_warnings([f.dest for f in plan.files]):
        warning(f"{path.name} references host paths ({matches}); they will not resolve in the pod.")

    if any("credentials" in skipped.reason for skipped in plan.skipped):
        info("Credentials were not copied. Re-run with --with-credentials to include them.")
    if resolved == "claude":
        info(
            "Note: user-level MCP servers live in ~/.claude.json, which VibePod does not "
            "mount. Use a project-level .mcp.json instead.",
        )

    summary = f"Imported {result.copied} file(s) into {dest_root}"
    if result.failed:
        error(f"{summary}; {len(result.failed)} failed.")
        raise typer.Exit(code=1)
    success(summary)

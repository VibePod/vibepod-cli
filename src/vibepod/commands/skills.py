"""`vp skills` — manage installed skills via the skills-engine container."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.table import Table

from vibepod.core import skills_engine
from vibepod.core.skills_engine import Scope, SkillsEngineError
from vibepod.utils.console import console, error, info, success, warning

_VALID_SCOPES = {"local", "user"}

app = typer.Typer(help="Manage VibePod skills", no_args_is_help=True)


def _resolve_scope(scope: str | None) -> Scope:
    if scope is not None:
        if scope not in _VALID_SCOPES:
            raise typer.BadParameter(f"--scope must be local|user, got {scope!r}")
        return scope  # type: ignore[return-value]
    return skills_engine.detect_scope_default()


def _emit_or_raise(result: skills_engine.EngineResult, json_out: bool) -> None:
    if json_out:
        if result.data is not None:
            typer.echo(json.dumps(result.data, indent=2))
        else:
            typer.echo(result.stdout, nl=False)
    if result.exit_code != 0:
        if result.stderr:
            error(result.stderr.strip())
        raise typer.Exit(result.exit_code)


@app.command("add")
def add_cmd(
    locator: Annotated[
        str, typer.Argument(help="Skill locator (github:..., npm:..., ./path, ...)")
    ],
    skill_id: Annotated[
        str | None, typer.Option("--id", help="Override the derived skill ID")
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="local|user (defaults to local inside a project, else user)"),
    ] = None,
    link: Annotated[
        bool, typer.Option("--link", help="Symlink instead of copy (local sources only)")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Install a skill from a locator."""
    resolved_scope = _resolve_scope(scope)
    info(f"Adding {locator} → scope={resolved_scope}")
    try:
        result = skills_engine.add(locator, scope=resolved_scope, skill_id=skill_id, link=link)
    except SkillsEngineError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if not json_out and result.exit_code == 0 and result.data:
        for record in result.data:
            if record.get("command") != "add":
                continue
            if record.get("bundle"):
                installed = record.get("installed", [])
                success(
                    f"Installed {len(installed)} skill(s) from bundle "
                    f"{record.get('locator', '')}"
                )
                for item in installed:
                    info(f"  + {item.get('id', '?')} ({item.get('name', '')})")
                for fail in record.get("failed", []) or []:
                    error(f"  ! {fail.get('subpath', '?')}: {fail.get('error', 'unknown error')}")
            else:
                success(
                    f"Installed {record.get('id', '?')} "
                    f"({record.get('name', '')}) → {record.get('path', '')}"
                )
    _emit_or_raise(result, json_out)


@app.command("delete")
def delete_cmd(
    skill_id: Annotated[str, typer.Argument(help="Skill ID to remove")],
    scope: Annotated[str | None, typer.Option("--scope", help="local|user")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove an installed skill."""
    resolved_scope = _resolve_scope(scope)
    try:
        result = skills_engine.delete(skill_id, scope=resolved_scope)
    except SkillsEngineError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if not json_out and result.exit_code == 0:
        success(f"Deleted {skill_id} from {resolved_scope}")
    _emit_or_raise(result, json_out)


@app.command("list")
def list_cmd(
    scope: Annotated[
        str | None, typer.Option("--scope", help="Filter by scope (local|user)")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List installed skills across both scopes."""
    if scope is not None and scope not in _VALID_SCOPES:
        raise typer.BadParameter(f"--scope must be local|user, got {scope!r}")
    try:
        result = skills_engine.list_skills(scope)  # type: ignore[arg-type]
    except SkillsEngineError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if json_out:
        _emit_or_raise(result, json_out=True)
        return

    if result.exit_code != 0:
        error(result.stderr.strip())
        raise typer.Exit(result.exit_code)

    rows: list[dict[str, Any]] = []
    if result.data:
        for record in result.data:
            if record.get("command") == "list":
                rows = record.get("skills", [])
                break

    if not rows:
        warning("No skills installed.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Scope")
    table.add_column("Status")
    for row in rows:
        status = row.get("status", "active")
        if status == "shadowed" and row.get("shadowedBy"):
            status = f"shadowed by {row['shadowedBy']}"
        elif row.get("shadows"):
            status = f"active (shadows {','.join(row['shadows'])})"
        table.add_row(
            row.get("id", ""),
            row.get("name", ""),
            str(row.get("version", "-")),
            row.get("scope", ""),
            status,
        )
    console.print(table)


@app.command("sync")
def sync_cmd(
    scope: Annotated[str | None, typer.Option("--scope", help="local|user")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Reconcile installed/ with the lockfile (no re-resolve)."""
    resolved_scope = _resolve_scope(scope)
    try:
        result = skills_engine.sync(resolved_scope)
    except SkillsEngineError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if not json_out and result.exit_code == 0 and result.data:
        for record in result.data:
            if record.get("command") == "sync":
                success(
                    f"Synced {resolved_scope}: restored={len(record.get('restored', []))}, "
                    f"unchanged={len(record.get('unchanged', []))}"
                )
    _emit_or_raise(result, json_out)


@app.command("update")
def update_cmd(
    skill_id: Annotated[str | None, typer.Argument(help="Skill ID (omit to update all)")] = None,
    scope: Annotated[str | None, typer.Option("--scope", help="local|user")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Re-resolve locators and rewrite the lockfile."""
    resolved_scope = _resolve_scope(scope)
    try:
        result = skills_engine.update(resolved_scope, skill_id)
    except SkillsEngineError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if not json_out and result.exit_code == 0:
        success(f"Updated skills in {resolved_scope}")
    _emit_or_raise(result, json_out)

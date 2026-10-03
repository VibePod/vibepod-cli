"""Config subcommands."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.prompt import Prompt

from vibepod.constants import SUPPORTED_AGENTS, agent_ids
from vibepod.core import microsandbox as msb
from vibepod.core.agents import resolve_agent_name
from vibepod.core.allowed_dirs import (
    add_allowed_dir,
    is_protected_dir,
    load_allowed_dirs,
    remove_allowed_dir,
)
from vibepod.core.config import (
    deep_merge,
    get_config,
    get_config_root,
    get_global_config_path,
    get_project_config_path,
)
from vibepod.utils.console import console, error, success, warning

app = typer.Typer(help="Manage configuration")

PROJECT_CONFIG_MINIMAL = "version: 1\n"


def _load_project_mapping(project_path: Path) -> dict[str, Any]:
    if not project_path.exists():
        return {}
    loaded = yaml.safe_load(project_path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        error(f"Project config must contain a YAML mapping: {project_path}")
        raise typer.Exit(1)
    return loaded


def _prompt_runtime_settings(
    runtime: str | None,
    msb_cpus: int | None,
    msb_memory: str | None,
) -> tuple[str, int | None, str | None]:
    """Ask for the runtime and its microsandbox resources on a TTY."""
    current = str(get_config().get("runtime") or msb.DEFAULT_RUNTIME)
    runtime = Prompt.ask(
        "Sandbox runtime",
        choices=list(msb.RUNTIMES),
        default=runtime or (current if current in msb.RUNTIMES else msb.DEFAULT_RUNTIME),
    )
    if runtime == msb.RUNTIME_MICROSANDBOX:
        while True:
            answer = Prompt.ask(
                "microVM vCPUs (blank for the microsandbox default)",
                default=str(msb_cpus) if msb_cpus is not None else "",
                show_default=False,
            )
            try:
                msb_cpus = msb.normalize_cpus(answer, source="vCPUs")
                break
            except ValueError as exc:
                error(str(exc))
        while True:
            answer = Prompt.ask(
                "microVM memory, e.g. 2G (blank for the microsandbox default)",
                default=msb_memory or "",
                show_default=False,
            )
            try:
                msb_memory = msb.normalize_memory(answer, source="memory")
                break
            except ValueError as exc:
                error(str(exc))
    return runtime, msb_cpus, msb_memory


def _apply_runtime_settings(
    project_config: dict[str, Any],
    *,
    runtime: str | None,
    msb_binary: str | None,
    msb_cpus: int | None,
    msb_memory: str | None,
) -> str | None:
    """Validate runtime options and store them in *project_config*."""
    msb_options = [
        flag
        for flag, value in (
            ("--msb-binary", msb_binary),
            ("--msb-cpus", msb_cpus),
            ("--msb-memory", msb_memory),
        )
        if value is not None
    ]
    if runtime is None:
        if msb_options:
            error(f"{', '.join(msb_options)} require --runtime microsandbox.")
            raise typer.Exit(1)
        return None
    try:
        selected = msb.resolve_runtime({}, runtime)
        cpus = msb.normalize_cpus(msb_cpus, source="--msb-cpus")
        memory = msb.normalize_memory(msb_memory, source="--msb-memory")
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    if msb_options and selected != msb.RUNTIME_MICROSANDBOX:
        error(f"{', '.join(msb_options)} require --runtime microsandbox.")
        raise typer.Exit(1)
    if msb_binary is not None and not msb_binary.strip():
        error("--msb-binary cannot be empty.")
        raise typer.Exit(1)

    project_config["runtime"] = selected
    if selected == msb.RUNTIME_MICROSANDBOX:
        section = project_config.get("microsandbox")
        if not isinstance(section, dict):
            section = {}
        if msb_binary is not None:
            section["binary"] = msb_binary.strip()
        if cpus is not None:
            section["cpus"] = cpus
        if memory is not None:
            section["memory"] = memory
        if section:
            project_config["microsandbox"] = section
    return selected


def _report_microsandbox_readiness(project_config: dict[str, Any]) -> None:
    """Warn (without failing) when this host cannot run microsandbox yet."""
    try:
        settings = msb.load_settings(deep_merge(get_config(), project_config))
        msb.ensure_available(settings)
    except (ValueError, msb.MicrosandboxError) as exc:
        warning(f"microsandbox is not ready on this host: {exc}")
        warning("Check again with `vp doctor microsandbox`.")


@app.command("init")
def init(
    agent: Annotated[
        str | None,
        typer.Argument(help="Optional agent config to copy into project"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing project config if present"),
    ] = False,
    runtime: Annotated[
        str | None,
        typer.Option("--runtime", help="Sandbox runtime to store: docker or microsandbox"),
    ] = None,
    msb_binary: Annotated[
        str | None,
        typer.Option("--msb-binary", help="Path or name of the microsandbox `msb` CLI"),
    ] = None,
    msb_cpus: Annotated[
        int | None,
        typer.Option("--msb-cpus", help="vCPUs for each microsandbox microVM"),
    ] = None,
    msb_memory: Annotated[
        str | None,
        typer.Option("--msb-memory", help="Memory for each microsandbox microVM, e.g. 2G"),
    ] = None,
    interactive: Annotated[
        bool,
        typer.Option("-i", "--interactive", help="Choose the runtime and its settings by prompt"),
    ] = False,
) -> None:
    """Create a minimal project config or add a specific agent config."""
    project_path = get_project_config_path()
    if interactive:
        if not sys.stdin.isatty():
            error("--interactive needs a terminal; pass --runtime and --msb-* options instead.")
            raise typer.Exit(1)
        runtime, msb_cpus, msb_memory = _prompt_runtime_settings(runtime, msb_cpus, msb_memory)
    runtime_requested = runtime is not None or any(
        value is not None for value in (msb_binary, msb_cpus, msb_memory)
    )

    if agent is not None:
        resolved = resolve_agent_name(agent)
        if resolved is None:
            error(f"Unknown agent '{agent}'. Supported: {', '.join(SUPPORTED_AGENTS)}")
            raise typer.Exit(1)
        agent = resolved

        try:
            project_path.parent.mkdir(parents=True, exist_ok=True)

            project_config = _load_project_mapping(project_path)

            project_config.setdefault("version", 1)
            agents_config = project_config.setdefault("agents", {})
            if not isinstance(agents_config, dict):
                error(f"Project config key 'agents' must be a YAML mapping: {project_path}")
                raise typer.Exit(1)

            if any(agent_id in agents_config for agent_id in agent_ids(agent)):
                error(f"Project config already contains agent '{agent}': {project_path}")
                raise typer.Exit(1)

            effective_agents = get_config().get("agents", {})
            if not isinstance(effective_agents, dict):
                error(f"Could not resolve config for agent '{agent}'.")
                raise typer.Exit(1)

            effective_agent = effective_agents.get(agent)
            if not isinstance(effective_agent, dict):
                error(f"Could not resolve config for agent '{agent}'.")
                raise typer.Exit(1)

            agents_config[agent] = copy.deepcopy(effective_agent)
            selected_runtime = _apply_runtime_settings(
                project_config,
                runtime=runtime,
                msb_binary=msb_binary,
                msb_cpus=msb_cpus,
                msb_memory=msb_memory,
            )
            project_path.write_text(
                yaml.safe_dump(project_config, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            error(f"Failed to update project config at {project_path}: {exc}")
            raise typer.Exit(1) from exc
        except yaml.YAMLError as exc:
            error(f"Failed to parse project config at {project_path}: {exc}")
            raise typer.Exit(1) from exc

        success(f"Added agent config '{agent}' to project config: {project_path}")
        if selected_runtime == msb.RUNTIME_MICROSANDBOX:
            _report_microsandbox_readiness(project_config)
        return

    if runtime_requested and project_path.exists() and not force:
        # Selecting a runtime for an existing project updates it in place.
        try:
            project_config = _load_project_mapping(project_path)
        except yaml.YAMLError as exc:
            error(f"Failed to parse project config at {project_path}: {exc}")
            raise typer.Exit(1) from exc
        project_config.setdefault("version", 1)
        selected_runtime = _apply_runtime_settings(
            project_config,
            runtime=runtime,
            msb_binary=msb_binary,
            msb_cpus=msb_cpus,
            msb_memory=msb_memory,
        )
        try:
            project_path.write_text(
                yaml.safe_dump(project_config, sort_keys=False),
                encoding="utf-8",
            )
        except OSError as exc:
            error(f"Failed to update project config at {project_path}: {exc}")
            raise typer.Exit(1) from exc
        success(f"Set runtime '{selected_runtime}' in project config: {project_path}")
        if selected_runtime == msb.RUNTIME_MICROSANDBOX:
            _report_microsandbox_readiness(project_config)
        return

    if project_path.exists() and not force:
        error(f"Project config already exists: {project_path}")
        error("Use --force to overwrite.")
        raise typer.Exit(1)

    project_config = {"version": 1}
    selected_runtime = _apply_runtime_settings(
        project_config,
        runtime=runtime,
        msb_binary=msb_binary,
        msb_cpus=msb_cpus,
        msb_memory=msb_memory,
    )
    content = (
        yaml.safe_dump(project_config, sort_keys=False)
        if selected_runtime is not None
        else PROJECT_CONFIG_MINIMAL
    )
    try:
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        error(f"Failed to create project config at {project_path}: {exc}")
        raise typer.Exit(1) from exc
    success(f"Created project config: {project_path}")
    if selected_runtime == msb.RUNTIME_MICROSANDBOX:
        _report_microsandbox_readiness(project_config)


@app.command("show")
def show(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show effective merged config."""
    cfg = get_config()
    if as_json:
        print(json.dumps(cfg, indent=2))
        return
    dumped = yaml.safe_dump(cfg, sort_keys=False)
    console.print(dumped)


@app.command("path")
def path(
    global_only: Annotated[
        bool,
        typer.Option("--global", help="Show global config path only"),
    ] = False,
    project_only: Annotated[
        bool,
        typer.Option("--project", help="Show project config path only"),
    ] = False,
) -> None:
    """Show configuration, logs, and proxy database paths."""
    global_path = get_global_config_path()
    project_path = get_project_config_path()

    if global_only and project_only:
        raise typer.BadParameter("Use only one of --global or --project")

    if global_only:
        print(global_path)
        return

    if project_only:
        print(project_path)
        return

    config = get_config()
    config_root = get_config_root()
    logs_path = Path(
        str(config.get("logging", {}).get("db_path", "~/.config/vibepod/logs.db")),
    )
    logs_path = logs_path.expanduser().resolve()
    proxy_db_path = Path(
        str(config.get("proxy", {}).get("db_path", "~/.config/vibepod/proxy/proxy.db")),
    )
    proxy_db_path = proxy_db_path.expanduser().resolve()

    print(f"Config:  {config_root}")
    print(f"Global:  {global_path}")
    print(f"Project: {project_path}")
    print(f"Logs:    {logs_path}")
    print(f"Proxy:   {proxy_db_path}")


@app.command("allow-dir")
def allow_dir(
    directory: Annotated[
        Path | None,
        typer.Argument(help="Directory to allow (defaults to current directory)"),
    ] = None,
) -> None:
    """Add a directory to the vp run allow list."""
    try:
        target = (directory or Path.cwd()).expanduser().resolve()
    except (OSError, ValueError) as exc:
        error(f"Could not resolve directory path: {exc}")
        raise typer.Exit(1) from exc
    if not target.exists() or not target.is_dir():
        error(f"Not a valid directory: {target}")
        raise typer.Exit(1)
    if is_protected_dir(target):
        error(
            f"'{target}' is a protected directory (home or root) and cannot be added "
            "to the allow list.",
        )
        raise typer.Exit(1)
    try:
        add_allowed_dir(target)
    except OSError as exc:
        error(f"Could not update allow list: {exc}")
        raise typer.Exit(1) from exc
    print(f"Allowed: {target}")


@app.command("remove-dir")
def remove_dir(
    directory: Annotated[
        Path | None,
        typer.Argument(help="Directory to remove (defaults to current directory)"),
    ] = None,
) -> None:
    """Remove a directory from the vp run allow list."""
    try:
        target = (directory or Path.cwd()).expanduser().resolve()
    except (OSError, ValueError) as exc:
        error(f"Could not resolve directory path: {exc}")
        raise typer.Exit(1) from exc
    try:
        removed = remove_allowed_dir(target)
    except OSError as exc:
        error(f"Could not update allow list: {exc}")
        raise typer.Exit(1) from exc
    if removed:
        print(f"Removed: {target}")
    else:
        error(f"Directory not in allow list: {target}")
        raise typer.Exit(1)


@app.command("list-allowed-dirs")
def list_allowed_dirs() -> None:
    """List all directories in the vp run allow list."""
    dirs = load_allowed_dirs()
    if not dirs:
        console.print("No directories in the allow list.")
        return
    for d in dirs:
        print(d)

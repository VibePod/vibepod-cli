"""Task command implementation — create headless background agent tasks."""

from __future__ import annotations

import json as _json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.prompt import Confirm
from rich.table import Table

from vibepod import __version__
from vibepod.constants import EXIT_DOCKER_NOT_RUNNING
from vibepod.core.agents import (
    agent_config_dir,
    effective_agent_image,
    get_agent_spec,
    resolve_agent_name,
)
from vibepod.core.allowed_dirs import add_allowed_dir, is_dir_allowed, is_protected_dir
from vibepod.core.config import get_config, get_config_root
from vibepod.core.docker import DockerClientError, DockerManager, _is_latest_tag
from vibepod.core.launch import (
    agent_extra_volumes,
    agent_init_commands,
    get_container_ip,
    host_user,
    init_entrypoint,
    parse_env_pairs,
    read_claude_stored_token,
    terminal_env_defaults,
    update_container_mapping,
)
from vibepod.core.tasks import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_STARTING,
    TERMINAL_TASK_STATUSES,
    TaskRecord,
    TaskStore,
)
from vibepod.utils.console import console, error, info, success, warning

app = typer.Typer(
    name="task",
    help="Create and manage headless background agent tasks",
    no_args_is_help=True,
)


def _task_store() -> TaskStore:
    db_path = get_config_root() / "tasks.db"
    return TaskStore(db_path)


def _resolve_task(store: TaskStore, id_or_prefix: str) -> TaskRecord:
    """Look up by full id first, then by prefix. Error on miss or ambiguous."""
    exact = store.get(id_or_prefix)
    if exact is not None:
        return exact
    matches = store.find_by_prefix(id_or_prefix)
    if not matches:
        error(f"No task with id matching '{id_or_prefix}'")
        raise typer.Exit(1)
    if len(matches) > 1:
        joined = ", ".join(m.id[:12] for m in matches[:5])
        error(f"Ambiguous task id '{id_or_prefix}' (matches: {joined}). Use a longer prefix.")
        raise typer.Exit(1)
    return matches[0]


def _context_args(ctx: typer.Context | None) -> list[str]:
    return list(ctx.args) if ctx is not None and ctx.args else []


def _state_timestamp(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)
    if value is None:
        return None
    timestamp = str(value)
    if not timestamp or timestamp.startswith("0001-01-01"):
        return None
    return timestamp


def _task_state_from_docker(
    state: dict[str, Any],
) -> tuple[str, int | None, str | None, str | None]:
    docker_status = str(state.get("Status") or "")
    exit_code = state.get("ExitCode")
    normalized_exit_code = exit_code if isinstance(exit_code, int) else None
    started_at = _state_timestamp(state, "StartedAt")
    finished_at = _state_timestamp(state, "FinishedAt")

    if docker_status == "exited":
        status = TASK_STATUS_COMPLETED if normalized_exit_code == 0 else TASK_STATUS_FAILED
    elif docker_status in {"dead", "removing"}:
        status = TASK_STATUS_FAILED
    elif docker_status == "created":
        status = TASK_STATUS_STARTING
    elif docker_status:
        status = TASK_STATUS_RUNNING
    else:
        status = TASK_STATUS_RUNNING

    if status not in TERMINAL_TASK_STATUSES:
        normalized_exit_code = None
        finished_at = None

    return status, normalized_exit_code, started_at, finished_at


def _record_with_container_state(
    store: TaskStore,
    record: TaskRecord,
    state: dict[str, Any],
) -> TaskRecord:
    status, exit_code, started_at, finished_at = _task_state_from_docker(state)
    if (
        record.status == status
        and record.exit_code == exit_code
        and record.started_at == started_at
        and record.finished_at == finished_at
    ):
        return record
    return (
        store.update(
            record.id,
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
        )
        or record
    )


def _format_task_status(record: TaskRecord) -> str:
    if record.status in TERMINAL_TASK_STATUSES and record.exit_code is not None:
        return f"{record.status} ({record.exit_code})"
    return record.status


_TASK_CREATE_CONTEXT = {"allow_extra_args": True, "ignore_unknown_options": True}


@app.command(
    "create",
    context_settings=_TASK_CREATE_CONTEXT,
)
def task_create_command(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent to run headlessly")],
    prompt: Annotated[str, typer.Argument(help="Prompt to send to the agent")],
    workspace: Annotated[
        Path, typer.Option("-w", "--workspace", help="Workspace directory")
    ] = Path("."),
    env: Annotated[
        list[str] | None,
        typer.Option("-e", "--env", help="Environment variable KEY=VALUE", show_default=False),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Custom container name")] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Additional Docker network to connect the container to"),
    ] = None,
    pull: Annotated[bool, typer.Option("--pull", help="Pull latest image before run")] = False,
    ikwid: Annotated[
        bool,
        typer.Option(
            "--ikwid",
            help="I Know What I'm Doing: enable auto-approval flags for supported agents",
        ),
    ] = False,
) -> None:
    """Start an agent task in the background and print its id."""
    task_create(
        agent=agent,
        prompt=prompt,
        workspace=workspace,
        env=env,
        name=name,
        network=network,
        pull=pull,
        ikwid=ikwid,
        passthrough_args=_context_args(ctx),
    )


@app.command(
    "run",
    context_settings=_TASK_CREATE_CONTEXT,
    hidden=True,
)
def task_run_command(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="Agent to run headlessly")],
    prompt: Annotated[str, typer.Argument(help="Prompt to send to the agent")],
    workspace: Annotated[
        Path, typer.Option("-w", "--workspace", help="Workspace directory")
    ] = Path("."),
    env: Annotated[
        list[str] | None,
        typer.Option("-e", "--env", help="Environment variable KEY=VALUE", show_default=False),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Custom container name")] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Additional Docker network to connect the container to"),
    ] = None,
    pull: Annotated[bool, typer.Option("--pull", help="Pull latest image before run")] = False,
    ikwid: Annotated[
        bool,
        typer.Option(
            "--ikwid",
            help="I Know What I'm Doing: enable auto-approval flags for supported agents",
        ),
    ] = False,
) -> None:
    """Deprecated alias for `task create`."""
    task_create(
        agent=agent,
        prompt=prompt,
        workspace=workspace,
        env=env,
        name=name,
        network=network,
        pull=pull,
        ikwid=ikwid,
        passthrough_args=_context_args(ctx),
        deprecated_alias=True,
    )


def task_create(
    agent: Annotated[str, typer.Argument(help="Agent to run headlessly")],
    prompt: Annotated[str, typer.Argument(help="Prompt to send to the agent")],
    workspace: Annotated[
        Path, typer.Option("-w", "--workspace", help="Workspace directory")
    ] = Path("."),
    env: Annotated[
        list[str] | None,
        typer.Option("-e", "--env", help="Environment variable KEY=VALUE", show_default=False),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Custom container name")] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Additional Docker network to connect the container to"),
    ] = None,
    pull: Annotated[bool, typer.Option("--pull", help="Pull latest image before run")] = False,
    ikwid: Annotated[
        bool,
        typer.Option(
            "--ikwid",
            help="I Know What I'm Doing: enable auto-approval flags for supported agents",
        ),
    ] = False,
    passthrough_args: list[str] | None = None,
    deprecated_alias: bool = False,
) -> None:
    """Start an agent task in the background and print its id.

    Extra arguments after `--` are forwarded to the agent's command, after the
    prompt (matches the documented invocation for `claude -p`, `codex exec`, etc).
    """
    passthrough_args = list(passthrough_args or [])
    if deprecated_alias:
        warning("`vp task run` is deprecated; use `vp task create`.")

    config = get_config()
    selected = resolve_agent_name(agent)
    if selected is None:
        error(f"Unknown agent '{agent}'.")
        raise typer.Exit(1)

    spec = get_agent_spec(selected)
    if not spec.headless_prefix:
        error(
            f"Agent '{selected}' does not yet support headless task mode. "
            "Supported in v1: claude, codex, auggie."
        )
        raise typer.Exit(1)

    workspace_path = workspace.expanduser().resolve()
    if not workspace_path.exists() or not workspace_path.is_dir():
        raise typer.BadParameter(f"Workspace not found: {workspace_path}")

    if is_protected_dir(workspace_path):
        error(
            f"'{workspace_path}' is a protected directory (home or root) and cannot be "
            "added to the allow list. Change to a project directory first."
        )
        raise typer.Exit(1)

    if not is_dir_allowed(workspace_path):
        if not sys.stdin.isatty():
            error(
                f"'{workspace_path}' is not in the allowed directories list. "
                "Run `vp config allow-dir` to add it."
            )
            raise typer.Exit(1)
        if not Confirm.ask(
            f"'{workspace_path}' is not allowed for `vp task`. Would you like to allow it?",
            default=True,
        ):
            error("Directory not allowed. Aborting.")
            raise typer.Exit(1)
        try:
            add_allowed_dir(workspace_path)
        except OSError as exc:
            error(f"Could not update allow list for '{workspace_path}': {exc}")
            raise typer.Exit(1) from exc

    agent_cfg = config.get("agents", {}).get(selected, {})
    init_commands = agent_init_commands(selected, agent_cfg)
    merged_env = {
        "USER_UID": str(os.getuid()),
        "USER_GID": str(os.getgid()),
        **terminal_env_defaults(),
        **spec.extra_env,
        **{str(k): str(v) for k, v in agent_cfg.get("env", {}).items()},
        **parse_env_pairs(env or []),
    }

    if selected == "codex" and "CODEX_API_KEY" not in merged_env and "OPENAI_API_KEY" in merged_env:
        merged_env["CODEX_API_KEY"] = merged_env["OPENAI_API_KEY"]

    if (
        selected == "claude"
        and "CLAUDE_CODE_OAUTH_TOKEN" not in merged_env
        and "ANTHROPIC_API_KEY" not in merged_env
    ):
        stored_token = read_claude_stored_token(agent_config_dir(selected))
        if stored_token:
            merged_env["CLAUDE_CODE_OAUTH_TOKEN"] = stored_token
            info("Using stored Claude OAuth token")

    # LLM env vars are applied; CLI model flag is NOT appended in task mode.
    # Users who need a specific model can pass it via passthrough args after `--`.
    llm_cfg = config.get("llm", {})
    if llm_cfg.get("enabled") and spec.llm_env_map:
        llm_values = {
            "base_url": str(llm_cfg.get("base_url", "")).strip(),
            "api_key": str(llm_cfg.get("api_key", "")).strip(),
            "model": str(llm_cfg.get("model", "")).strip(),
        }
        for key, env_var in spec.llm_env_map.items():
            value = llm_values.get(key, "")
            if value:
                targets = [env_var] if isinstance(env_var, str) else env_var
                for target in targets:
                    merged_env.setdefault(target, value)

    image = effective_agent_image(selected, config)

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    network_name = str(config.get("network", "vibepod-network"))
    manager.ensure_network(network_name)

    agent_auto_pull = agent_cfg.get("auto_pull")
    auto_pull_enabled = (
        agent_auto_pull if agent_auto_pull is not None else bool(config.get("auto_pull", False))
    )
    should_pull = pull or (auto_pull_enabled and _is_latest_tag(image))
    if should_pull:
        info(f"Pulling image: {image}")
        manager.pull_image(image)

    base_command = spec.command
    entrypoint: list[str] | None = None
    if init_commands or (base_command is None):
        try:
            base_command = manager.resolve_launch_command(image=image, command=spec.command)
        except DockerClientError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        if init_commands:
            info(f"Applying {len(init_commands)} init command(s) before startup")
            entrypoint = init_entrypoint(init_commands)

    ikwid_prefix: list[str] = []
    if ikwid:
        if spec.ikwid_args:
            ikwid_prefix = list(spec.ikwid_args)
        else:
            warning(f"--ikwid has no effect for agent '{selected}' (no auto-approve flag defined)")

    command = (
        list(base_command or [])
        + ikwid_prefix
        + list(spec.headless_prefix)
        + [prompt]
        + passthrough_args
    )

    config_dir = agent_config_dir(selected)
    config_dir.mkdir(parents=True, exist_ok=True)

    extra_volumes = agent_extra_volumes(selected, config_dir)
    for host_path, _, _ in extra_volumes:
        Path(host_path).mkdir(parents=True, exist_ok=True)

    proxy_cfg = config.get("proxy", {})
    proxy_enabled = bool(proxy_cfg.get("enabled", True))
    proxy_ca_dir_value = str(proxy_cfg.get("ca_dir", "")).strip()
    proxy_ca_path_value = str(proxy_cfg.get("ca_path", "")).strip()
    proxy_ca_dir = Path(proxy_ca_dir_value).expanduser().resolve() if proxy_ca_dir_value else None
    proxy_ca_path = (
        Path(proxy_ca_path_value).expanduser().resolve() if proxy_ca_path_value else None
    )
    proxy_db_path: Path | None = None

    if proxy_enabled:
        proxy_image = str(proxy_cfg.get("image", "vibepod/proxy:latest"))
        proxy_db_path = (
            Path(str(proxy_cfg.get("db_path", "~/.config/vibepod/proxy/proxy.db")))
            .expanduser()
            .resolve()
        )

        if _is_latest_tag(proxy_image):
            manager.pull_if_newer(proxy_image)

        actual_ca_dir = proxy_ca_dir or proxy_db_path.parent / "mitmproxy"
        manager.ensure_proxy(
            image=proxy_image,
            db_path=proxy_db_path,
            ca_dir=actual_ca_dir,
            network=network_name,
        )

        if proxy_ca_path:
            deadline = time.time() + 10
            while time.time() < deadline:
                if proxy_ca_path.exists():
                    break
                time.sleep(0.25)

        proxy_url = "http://vibepod-proxy:8080"
        merged_env.setdefault("HTTP_PROXY", proxy_url)
        merged_env.setdefault("HTTPS_PROXY", proxy_url)
        merged_env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
        _ca = "/etc/vibepod-proxy-ca/mitmproxy-ca-cert.pem"
        merged_env.setdefault("NODE_EXTRA_CA_CERTS", _ca)
        merged_env.setdefault("REQUESTS_CA_BUNDLE", _ca)
        merged_env.setdefault("SSL_CERT_FILE", _ca)
        merged_env.setdefault("CURL_CA_BUNDLE", _ca)

        extra_volumes.append((str(actual_ca_dir), "/etc/vibepod-proxy-ca", "ro"))

    info(f"Starting task on {selected} with image {image}")
    container_user = host_user() if spec.run_as_host_user else None
    container = manager.run_agent(
        agent=selected,
        image=image,
        workspace=workspace_path,
        config_dir=config_dir,
        config_mount_path=spec.config_mount_path,
        env=merged_env,
        command=command,
        auto_remove=False,  # tasks keep the container so logs/exit survive
        name=name,
        version=__version__,
        network=network_name,
        extra_volumes=extra_volumes,
        platform=spec.platform,
        user=container_user,
        entrypoint=entrypoint,
    )

    container.reload()
    if container.status not in {"running", "created"}:
        recent = container.logs(tail=50).decode("utf-8", errors="replace")
        error("Container exited immediately after start.")
        if recent.strip():
            print(recent)
        raise typer.Exit(1)

    if network and network != network_name:
        try:
            manager.connect_network(container, network)
            info(f"Connected to additional network: {network}")
        except DockerClientError as exc:
            warning(str(exc))

    if proxy_db_path is not None:
        container_ip = get_container_ip(container, network_name)
        if container_ip:
            mapping_path = proxy_db_path.parent / "containers.json"
            update_container_mapping(
                mapping_path, container_ip, container.id, container.name, selected
            )

    state = container.attrs.get("State", {}) or {}
    if not isinstance(state, dict):
        state = {}
    initial_status = TASK_STATUS_RUNNING if container.status == "running" else TASK_STATUS_STARTING

    store = _task_store()
    try:
        record = store.create(
            agent=selected,
            prompt=prompt,
            workspace=str(workspace_path),
            container_id=container.id,
            container_name=container.name,
            image=image,
            vibepod_version=__version__,
            status=initial_status,
            started_at=_state_timestamp(state, "StartedAt"),
        )
    except Exception as exc:
        error(f"Failed to persist task record: {exc}. Stopping container {container.name}.")
        try:
            manager.stop_container(container.id, force=True)
            container.remove(force=True)
        except Exception as cleanup_exc:
            warning(f"Container {container.name} may be orphaned: {cleanup_exc}")
        raise typer.Exit(1) from exc
    success(f"Task started: {record.id}")
    info(f"  container: {container.name}")
    info(f"  follow:    vp task logs {record.id[:12]} --follow")


@app.command("list")
def task_list(
    agent: Annotated[str | None, typer.Option("--agent", help="Filter by agent name")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Output JSON")] = False,
    limit: Annotated[int | None, typer.Option("--limit", help="Max rows to show")] = 20,
) -> None:
    """List recent tasks with container status."""
    filter_agent: str | None = None
    if agent:
        filter_agent = resolve_agent_name(agent)
        if filter_agent is None:
            error(f"Unknown agent '{agent}'.")
            raise typer.Exit(1)
    store = _task_store()
    tasks = store.list(agent=filter_agent, limit=limit)

    if not tasks:
        console.print("No tasks recorded.")
        return

    try:
        manager: DockerManager | None = DockerManager()
    except DockerClientError:
        manager = None

    rows: list[tuple[TaskRecord, str]] = []
    for task in tasks:
        record = task
        display_status = _format_task_status(record)
        if manager is not None:
            try:
                container = manager.get_container(record.container_id)
                container.reload()
                state = container.attrs.get("State", {}) or {}
                if not isinstance(state, dict):
                    state = {}
                record = _record_with_container_state(store, record, state)
                display_status = _format_task_status(record)
            except DockerClientError:
                if record.status not in TERMINAL_TASK_STATUSES:
                    display_status = "removed"
        rows.append((record, display_status))
    if as_json:
        payloads = []
        for record, display_status in rows:
            payload = record.as_dict()
            if display_status == "removed":
                payload["status"] = "removed"
                payload["exit_code"] = None
            payloads.append(payload)
        print(_json.dumps(payloads, indent=2))
        return

    table = Table(title="Tasks", title_justify="left")
    table.add_column("ID", style="cyan")
    table.add_column("AGENT", style="magenta")
    table.add_column("STATUS")
    table.add_column("CREATED")
    table.add_column("PROMPT")

    for task, status in rows:
        first_line = next((ln for ln in task.prompt.splitlines() if ln.strip()), "")
        prompt_preview = first_line.strip()
        if len(prompt_preview) > 60:
            prompt_preview = prompt_preview[:57] + "..."
        table.add_row(task.id[:12], task.agent, status, task.created_at, prompt_preview)

    console.print(table)


@app.command("logs")
def task_logs(
    task_id: Annotated[str, typer.Argument(help="Task id (full or prefix)")],
    follow: Annotated[
        bool, typer.Option("-f", "--follow", help="Stream logs as they are written")
    ] = False,
) -> None:
    """Print the agent's stdout/stderr for a task."""
    store = _task_store()
    record = _resolve_task(store, task_id)

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    try:
        container = manager.get_container(record.container_id)
    except DockerClientError as exc:
        error(
            f"{exc}. Container for this task is gone; "
            f"remove the registry entry with `vp task rm {record.id[:12]}`."
        )
        raise typer.Exit(1) from exc

    if follow:
        try:
            for chunk in container.logs(stream=True, follow=True):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        except KeyboardInterrupt:
            pass
        return

    logs = container.logs().decode("utf-8", errors="replace")
    sys.stdout.write(logs)
    if logs and not logs.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


@app.command("status")
def task_status(
    task_id: Annotated[str, typer.Argument(help="Task id (full or prefix)")],
    as_json: Annotated[bool, typer.Option("--json", help="Output JSON")] = False,
) -> None:
    """Show container status and exit code for a task."""
    store = _task_store()
    record = _resolve_task(store, task_id)

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    payload: dict[str, Any]
    try:
        container = manager.get_container(record.container_id)
        container.reload()
        state = container.attrs.get("State", {}) or {}
        if not isinstance(state, dict):
            state = {}
        record = _record_with_container_state(store, record, state)
        payload = record.as_dict()
    except DockerClientError:
        payload = record.as_dict()
        if record.status not in TERMINAL_TASK_STATUSES:
            payload["status"] = "removed"
            payload["exit_code"] = None

    if as_json:
        print(_json.dumps(payload, indent=2))
        return

    info(f"Task:       {record.id}")
    info(f"Agent:      {record.agent}")
    info(f"Workspace:  {record.workspace}")
    info(f"Container:  {record.container_name} ({record.container_id[:12]})")
    info(f"Status:     {payload.get('status')}")
    if payload.get("exit_code") is not None:
        info(f"Exit code:  {payload['exit_code']}")
    info(f"Created:    {record.created_at}")


@app.command("rm")
def task_rm(
    task_id: Annotated[
        str | None,
        typer.Argument(help="Task id (full or prefix); omit when using --all"),
    ] = None,
    all_tasks: Annotated[
        bool,
        typer.Option("--all", help="Remove all task records and their containers"),
    ] = False,
    force: Annotated[
        bool, typer.Option("-f", "--force", help="Kill a running container before removing")
    ] = False,
) -> None:
    """Remove one task, or all tasks with --all."""
    store = _task_store()

    if all_tasks and task_id is not None:
        raise typer.BadParameter("Do not provide TASK_ID when using --all")
    if not all_tasks and task_id is None:
        raise typer.BadParameter("Provide TASK_ID, or use --all")

    try:
        manager = DockerManager()
    except DockerClientError as exc:
        error(str(exc))
        raise typer.Exit(EXIT_DOCKER_NOT_RUNNING) from exc

    if all_tasks:
        records = store.list(limit=None)
        if not records:
            info("No tasks recorded.")
            return

        if not force:
            running = _running_task_records(records, manager)
            if running:
                ids = ", ".join(record.id[:12] for record in running[:8])
                if len(running) > 8:
                    ids += ", ..."
                error(
                    f"{len(running)} task container(s) are still running: {ids}. "
                    "Use --force to kill and remove them."
                )
                raise typer.Exit(1)

        removed = 0
        for record in records:
            _remove_task_record(store, manager, record, force=force)
            removed += 1
        success(f"Removed {removed} task(s)")
        return

    assert task_id is not None
    record = _resolve_task(store, task_id)
    _remove_task_record(store, manager, record, force=force)
    success(f"Removed task {record.id[:12]}")


def _get_task_container(manager: DockerManager, record: TaskRecord) -> Any | None:
    container: Any = None
    try:
        container = manager.get_container(record.container_id)
    except DockerClientError:
        container = None
    return container


def _running_task_records(records: list[TaskRecord], manager: DockerManager) -> list[TaskRecord]:
    running: list[TaskRecord] = []
    for record in records:
        container = _get_task_container(manager, record)
        if container is None:
            continue
        container.reload()
        if getattr(container, "status", "") == "running":
            running.append(record)
    return running


def _remove_task_record(
    store: TaskStore,
    manager: DockerManager,
    record: TaskRecord,
    force: bool,
) -> None:
    container = _get_task_container(manager, record)
    if container is not None:
        container.reload()
        if getattr(container, "status", "") == "running" and not force:
            error(
                f"Task {record.id[:12]} is still running. "
                "Use --force to kill and remove, or wait for it to finish."
            )
            raise typer.Exit(1)
        try:
            container.remove(force=True)
        except Exception as exc:  # docker SDK raises APIError / DockerException
            error(f"Failed to remove container '{record.container_name}': {exc}")
            raise typer.Exit(1) from exc

    store.delete(record.id)

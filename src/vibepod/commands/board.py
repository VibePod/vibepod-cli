"""`vp board work`: let an agent work through the planned tasks of a vibepod-board project."""

from __future__ import annotations

import signal
import socket
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import typer
from rich.prompt import Confirm

from vibepod.commands import task as task_cmd
from vibepod.core.agents import AGENT_SPECS, get_agent_spec, resolve_agent_name
from vibepod.core.allowed_dirs import add_allowed_dir, is_dir_allowed, is_protected_dir
from vibepod.core.board_client import BoardApiError, BoardClient, resolve_board_settings
from vibepod.core.board_worker import (
    DEFAULT_BRANCH_TEMPLATE,
    BoardWorker,
    FileProfileLock,
    RunnerError,
    WorkerError,
    WorkOptions,
    validate_branch_template,
)
from vibepod.core.config import get_config, get_config_root
from vibepod.core.docker import DockerClientError
from vibepod.core.profiles import resolve_profile
from vibepod.core.tasks import TASK_STATUS_CANCELLED, TERMINAL_TASK_STATUSES
from vibepod.core.worktrees import GitError, repository_root
from vibepod.utils.console import error, info, success, warning

app = typer.Typer(
    name="board",
    help="Work through vibepod-board projects with agents",
    no_args_is_help=True,
)


class ExistingMode(str, Enum):
    CONTINUE = "continue"
    REFUSE = "refuse"


class OnFail(str, Enum):
    PLANNED = "planned"
    BLOCKED = "blocked"


class DockerAgentRun:
    """A `vp task create` container the worker waits for; `vp task logs` shows it too."""

    def __init__(self, launched: task_cmd.LaunchedTask) -> None:
        self.launched = launched
        self.record = launched.record

    @property
    def task_id(self) -> str:
        return self.record.id

    def poll(self) -> int | None:
        container = self.launched.container
        try:
            container.reload()
        except Exception:  # docker SDK raises NotFound / APIError when the container is gone
            return 1
        state = container.attrs.get("State", {}) or {}
        if not isinstance(state, dict):
            state = {}
        self.record = task_cmd._record_with_container_state(self.launched.store, self.record, state)
        if self.record.status in TERMINAL_TASK_STATUSES:
            return self.record.exit_code if self.record.exit_code is not None else 1
        return None

    def stop(self) -> None:
        try:
            self.launched.container.stop(timeout=10)
        except Exception as exc:  # docker SDK raises APIError / DockerException
            warning(f"Failed to stop task {self.record.id[:12]}: {exc}")
        self.launched.store.update(
            self.record.id,
            status=TASK_STATUS_CANCELLED,
            exit_code=self.record.exit_code,
            started_at=self.record.started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    def logs(self) -> str:
        try:
            raw = self.launched.container.logs()
        except Exception:  # docker SDK raises APIError / DockerException
            return ""
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)


class DockerAgentRunner:
    """Starts the agent headless through `vp task create`'s launch, with the saved login of
    the chosen profile. The board token is kept out of the container."""

    def __init__(
        self,
        agent: str,
        *,
        env: list[str] | None = None,
        network: str | None = None,
        no_overlay: bool = False,
        ikwid: bool = False,
        profile: str | None = None,
        provider_names: list[str] | None = None,
        forbidden_env_values: tuple[str, ...] = (),
    ) -> None:
        self.agent = agent
        self.env = env
        self.network = network
        self.no_overlay = no_overlay
        self.ikwid = ikwid
        self.profile = profile
        self.provider_names = provider_names
        self.forbidden_env_values = forbidden_env_values

    def start(
        self,
        prompt: str,
        workspace: Path,
        *,
        mounts: list[tuple[str, str, str]],
        allow_check_path: Path,
    ) -> DockerAgentRun:
        try:
            launched = task_cmd.launch_task(
                agent=self.agent,
                prompt=prompt,
                workspace=workspace,
                env=self.env,
                network=self.network,
                no_overlay=self.no_overlay,
                ikwid=self.ikwid,
                profile=self.profile,
                provider_names=self.provider_names,
                extra_mounts=mounts,
                allow_check_path=allow_check_path,
                forbidden_env_values=self.forbidden_env_values,
            )
        except typer.Exit as exc:
            raise RunnerError(f"{self.agent} could not be started (exit {exc.exit_code})") from exc
        except (typer.BadParameter, DockerClientError, ValueError) as exc:
            raise RunnerError(str(exc)) from exc
        return DockerAgentRun(launched)


def _duration(value: str, option: str) -> int | None:
    try:
        return task_cmd._parse_task_timeout(value)
    except typer.BadParameter as exc:
        raise typer.BadParameter(str(exc), param_hint=option) from exc


def _say(level: str, message: str) -> None:
    {"success": success, "warning": warning, "error": error}.get(level, info)(message)


def _ensure_allowed(repo: Path) -> None:
    """The repository must be on the allow list, like any `vp task` workspace; the task
    worktrees made from it inherit its permission."""
    if is_protected_dir(repo):
        error(f"'{repo}' is a protected directory (home or root); use a project repository.")
        raise typer.Exit(1)
    if is_dir_allowed(repo):
        return
    if not sys.stdin.isatty():
        error(f"'{repo}' is not in the allowed directories list. Run `vp config allow-dir`.")
        raise typer.Exit(1)
    if not Confirm.ask(f"'{repo}' is not allowed for agents. Would you like to allow it?"):
        error("Directory not allowed. Aborting.")
        raise typer.Exit(1)
    add_allowed_dir(repo)


def _raise_interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


@app.command("work")
def board_work(
    project: Annotated[str, typer.Argument(help="Board project key, such as VP")],
    agent: Annotated[str, typer.Option("--agent", "-a", help="Agent that implements the tasks")],
    board_url: Annotated[
        str | None,
        typer.Option("--board-url", help="Board URL; defaults to board.url or VP_BOARD_URL"),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Worker name on the board; defaults to <agent>@<host>"),
    ] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Only claim tasks with this label; repeat to require more"),
    ] = None,
    min_readiness: Annotated[
        int | None,
        typer.Option(
            "--min-readiness", min=1, max=10, help="Only claim tasks rated at least this ready"
        ),
    ] = None,
    task: Annotated[
        str | None,
        typer.Option("--task", help="Work on this one task, such as VP-12, then exit"),
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="Work on one task, then exit")] = False,
    max_tasks: Annotated[
        int | None, typer.Option("--max", min=1, help="Exit after this many tasks")
    ] = None,
    poll: Annotated[
        str | None,
        typer.Option(
            "--poll",
            help="When no task is left, wait this long (30s, 5m) and look again instead of exiting",
        ),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repository to work in; defaults to the task's local path"),
    ] = None,
    worktree_dir: Annotated[
        Path | None,
        typer.Option(
            "--worktree-dir",
            help="Where task worktrees go; defaults to <repo>-worktrees next to the repository",
        ),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option(
            "--base", help="Where new branches start; defaults to the repository's current branch"
        ),
    ] = None,
    branch_template: Annotated[
        str,
        typer.Option(
            "--branch-template",
            help="Branch name from {issue}, {number}, {key} and {project}; tasks without a "
            "GitHub issue use {key}, such as vp-12",
        ),
    ] = DEFAULT_BRANCH_TEMPLATE,
    existing: Annotated[
        ExistingMode,
        typer.Option(
            "--existing", help="When the task's branch or worktree exists: continue or refuse"
        ),
    ] = ExistingMode.CONTINUE,
    keep_worktree: Annotated[
        bool, typer.Option("--keep-worktree", help="Keep worktrees after the hand-over")
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Credential profile to use (see `vp profile list`)"),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option("--provider", help="Temporary model provider(s) for the agent runs"),
    ] = None,
    ikwid: Annotated[
        bool,
        typer.Option("--ikwid", help="Enable the agent's auto-approval flags"),
    ] = False,
    timeout: Annotated[
        str,
        typer.Option("--timeout", help="Time limit per task (s/m/h), or 'none'"),
    ] = task_cmd.DEFAULT_TASK_TIMEOUT,
    env: Annotated[
        list[str] | None,
        typer.Option("-e", "--env", help="Environment variable KEY=VALUE for the agent"),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option("--network", help="Additional Docker network for the agent container"),
    ] = None,
    no_overlay: Annotated[
        bool, typer.Option("--no-overlay", help="Skip the project overlay image")
    ] = False,
    verify: Annotated[
        str | None,
        typer.Option(
            "--verify", help="Command that must pass in the worktree before the hand-over"
        ),
    ] = None,
    on_fail: Annotated[
        OnFail,
        typer.Option(
            "--on-fail",
            help="Where failed and timed-out tasks go: planned (counts an attempt) or blocked",
        ),
    ] = OnFail.PLANNED,
    max_attempts: Annotated[
        int | None,
        typer.Option("--max-attempts", min=1, help="Failed attempts before a task is blocked"),
    ] = None,
    parallel: Annotated[
        bool,
        typer.Option("--parallel", help="Run alongside other workers on the same profile"),
    ] = False,
    usage_limit_wait: Annotated[
        str,
        typer.Option(
            "--usage-limit-wait",
            help="Pause after a usage limit when the agent names no reset time",
        ),
    ] = "30m",
) -> None:
    """Claim planned tasks of a board project and let an agent implement each one.

    Each task gets its own branch and worktree. The agent runs headless with the task as
    its prompt; after an optional --verify command passes, the task moves to Review with
    its branch. Failed and timed-out tasks go back to Planned (or blocked) with a note.
    """
    config = get_config()
    try:
        settings = resolve_board_settings(config, board_url)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    selected = resolve_agent_name(agent)
    if selected is None:
        error(f"Unknown agent '{agent}'.")
        raise typer.Exit(1)
    spec = get_agent_spec(selected)
    if not spec.headless_prefix and not spec.headless_command:
        supported = ", ".join(
            name
            for name, agent_spec in AGENT_SPECS.items()
            if agent_spec.headless_prefix or agent_spec.headless_command
        )
        error(f"Agent '{selected}' cannot run headless. Supported: {supported}.")
        raise typer.Exit(1)

    timeout_seconds = _duration(timeout, "--timeout")
    poll_seconds = _duration(poll, "--poll") if poll else None
    usage_wait = _duration(usage_limit_wait, "--usage-limit-wait") or 0
    try:
        validate_branch_template(branch_template)
        active_profile = resolve_profile(profile, config)
    except ValueError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    repo_path: Path | None = None
    if repo is not None:
        try:
            repo_path = repository_root(repo.expanduser().resolve())
        except GitError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        _ensure_allowed(repo_path)

    host = socket.gethostname().split(".")[0] or "localhost"
    options = WorkOptions(
        project=project,
        agent=selected,
        name=name or f"{selected}@{host}",
        machine=host,
        repo=repo_path,
        worktree_dir=worktree_dir.expanduser().resolve() if worktree_dir else None,
        base=base,
        branch_template=branch_template,
        existing=existing.value,
        keep_worktree=keep_worktree,
        labels=tuple(label or ()),
        min_readiness=min_readiness,
        task=task,
        once=once,
        max_tasks=max_tasks,
        poll_seconds=float(poll_seconds) if poll_seconds else None,
        timeout_seconds=timeout_seconds,
        verify=verify,
        on_fail=on_fail.value,
        max_attempts=max_attempts,
        usage_limit_wait_seconds=usage_wait,
    )
    runner = DockerAgentRunner(
        selected,
        env=env,
        network=network,
        no_overlay=no_overlay,
        ikwid=ikwid,
        profile=profile,
        provider_names=provider,
        forbidden_env_values=(settings.token,),
    )
    lock = (
        None
        if parallel
        else FileProfileLock(
            get_config_root() / "locks" / f"board-work-{active_profile}.lock", active_profile
        )
    )
    worker = BoardWorker(
        BoardClient(settings.url, settings.token), runner, options, lock=lock, say=_say
    )

    previous: Any = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        summary = worker.run()
    except (WorkerError, BoardApiError) as exc:
        error(getattr(exc, "message", str(exc)))
        raise typer.Exit(1) from exc
    except KeyboardInterrupt as exc:
        warning("Interrupted; the task in progress went back to Planned.")
        raise typer.Exit(130) from exc
    finally:
        signal.signal(signal.SIGTERM, previous)

    info(f"Handed over {len(summary.handed_over)} task(s), gave back {len(summary.returned)}.")
    if summary.ended_because.startswith("The agent could not be started"):
        raise typer.Exit(1)

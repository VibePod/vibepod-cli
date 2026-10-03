"""`vp board work`: an agent works through the planned tasks of a board project.

For each task the worker claims it on the board, checks its branch out in a worktree of its
own, runs the agent headless with the task as the prompt, runs an optional verify command, and
hands the task over to Review, or gives it back to Planned (or blocks it) with a note. Every run
leaves a report on its task.

The worker is registered with the board while it runs and reports what it does in heartbeats.
Their replies carry the board's instructions: pause (take no new task), stop (end the run, give
the task back and sign off) and cancel (end the run of a task the worker no longer holds).
Heartbeats are sent from the worker's own wait loops, so it stays single-threaded.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any, Protocol

from vibepod.core import worktrees
from vibepod.core.board_client import BoardApiError, BoardClient

STEP_PREPARING = "preparing_workspace"
STEP_AGENT = "agent_running"
STEP_VERIFYING = "verifying"
STEP_HANDING_OVER = "handing_over"

# How much verify output is read and sent, in bytes: its head and tail.
REPORT_OUTPUT_HEAD = 4_000
REPORT_OUTPUT_TAIL = 60_000
SUMMARY_LINES = 40
SUMMARY_CHARS = 4_000
DEFAULT_BRANCH_TEMPLATE = "issue-{issue}"
# The board takes up to this many tasks to pass over in one claim.
PASS_OVER_LIMIT = 50
# How long a stopped verify command gets to exit before it is killed.
TERMINATE_GRACE_SECONDS = 10.0
# Board writes that fail on the board's side (5xx) or on the way there are tried this often
# before they wait for the next round of the loop.
DELIVERY_ATTEMPTS = 5
DEFAULT_USAGE_LIMIT_WAIT = 30 * 60
MAX_USAGE_LIMIT_WAIT = 24 * 60 * 60

# Messages agents print when a subscription or API limit stops them. Only the end of the
# output of a failed run is searched, so an agent working *on* rate limiting is not mistaken
# for one that hit a limit.
USAGE_LIMIT_PATTERNS = (
    re.compile(r"usage[ _-]limit", re.IGNORECASE),
    re.compile(r"\b(?:5-hour|five-hour|weekly|daily|session) limit reached", re.IGNORECASE),
    re.compile(r"hit your (?:usage )?limit", re.IGNORECASE),
    re.compile(r"rate[ _-]limit[ _-](?:reached|exceeded)", re.IGNORECASE),
    re.compile(r"quota (?:exceeded|exhausted)", re.IGNORECASE),
    re.compile(r"\b429\b.*too many requests", re.IGNORECASE),
)
# Claude Code: "Claude AI usage limit reached|1759140000" names when the limit resets.
USAGE_LIMIT_RESET = re.compile(r"limit reached\|(\d{10})\b", re.IGNORECASE)


class AgentRun(Protocol):
    @property
    def task_id(self) -> str: ...

    def poll(self) -> int | None:
        """The exit code once the agent finished, else None."""
        ...

    def stop(self) -> None: ...

    def logs(self) -> str: ...


class AgentRunner(Protocol):
    def start(
        self,
        prompt: str,
        workspace: Path,
        *,
        mounts: list[tuple[str, str, str]],
        allow_check_path: Path,
    ) -> AgentRun: ...


# Environment variables the verify command never gets: it runs code the agent wrote.
PRIVATE_ENV = ("VP_BOARD_TOKEN",)


class RunnerError(Exception):
    """The agent could not be started, such as with Docker unavailable. Every task would fail
    the same way, so the worker gives the task back and stops."""


class WorkerError(Exception):
    """The worker cannot go on, such as a named task that cannot be claimed."""


class TaskProblem(Exception):
    """A task that cannot run as it stands, such as one without a repository; it is blocked
    with the message as the reason."""


class ProfileLock(Protocol):
    waiting_reason: str

    def try_acquire(self) -> bool: ...

    def release(self) -> None: ...


class FileProfileLock:
    """One task at a time per credential profile, across `vp board work` processes: agents
    sharing a subscription login share its limits."""

    def __init__(self, path: Path, profile: str) -> None:
        self.path = path
        self.waiting_reason = f"Waiting for another run on profile {profile} to finish"
        self._handle: IO[str] | None = None

    def try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            _lock_file(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        with contextlib.suppress(OSError):
            _unlock_file(self._handle)
        self._handle.close()
        self._handle = None


def _lock_file(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class WorkOptions:
    project: str
    agent: str
    name: str
    machine: str = field(default_factory=socket.gethostname)
    # Workspace: the repository (else the task's local path), where worktrees go, the base
    # of new branches, the branch name template, and what to do with an existing branch.
    repo: Path | None = None
    worktree_dir: Path | None = None
    base: str | None = None
    branch_template: str = DEFAULT_BRANCH_TEMPLATE
    existing: str = "continue"
    keep_worktree: bool = False
    # Selection.
    labels: tuple[str, ...] = ()
    min_readiness: int | None = None
    task: str | None = None
    once: bool = False
    max_tasks: int | None = None
    # Wait this long for new work when none is left; None ends the worker instead.
    poll_seconds: float | None = None
    # After the run.
    timeout_seconds: int | None = 2 * 60 * 60
    verify: str | None = None
    on_fail: str = "planned"
    max_attempts: int | None = None
    lease_seconds: int = 10 * 60
    usage_limit_wait_seconds: int = DEFAULT_USAGE_LIMIT_WAIT


@dataclass
class WorkSummary:
    handed_over: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    ended_because: str = ""


@dataclass
class VerifyResult:
    command: str
    exit_code: int | None
    output: str
    ended: str


@dataclass
class TaskResult:
    # done, failed, timed_out, cancelled or usage_limit.
    outcome: str = "failed"
    reason: str | None = None
    # How the task goes back when it is not handed over: failed, blocked, released, or None
    # when it is no longer held (a run cancelled from the board).
    release: str | None = "failed"
    summary: str = ""
    # The commits this run made, and all the work the branch holds.
    commits: list[dict[str, str]] = field(default_factory=list)
    branch_commits: list[dict[str, str]] = field(default_factory=list)
    branch: str | None = None
    verify: VerifyResult | None = None


def branch_name(template: str, task: dict[str, Any]) -> str:
    """The task's branch from the template. `{issue}` is its GitHub issue number, `{number}`
    its task number, `{key}` its lowercase key such as `vp-12` and `{project}` its lowercase
    project key. A task without the issue the template needs gets `{key}` instead."""
    key = str(task.get("key") or f"task-{task.get('taskNumber', '')}").lower()
    values = {
        "issue": task.get("githubIssueNumber"),
        "number": task.get("taskNumber"),
        "key": key,
        "project": key.rpartition("-")[0],
    }
    if "{issue}" in template and not values["issue"]:
        return key
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return key


def validate_branch_template(template: str) -> None:
    try:
        template.format(issue=1, number=1, key="vp-1", project="vp")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"Invalid branch template {template!r}: use {{issue}}, {{number}}, {{key}} or "
            "{project}",
        ) from exc


def build_prompt(task: dict[str, Any], branch: str) -> str:
    """The agent's instructions: the task's title, description and acceptance criteria, and
    how to leave the work."""
    key = task.get("key") or "the task"
    description = str(task.get("details") or task.get("summary") or "").strip()
    criteria = [str(item).strip() for item in task.get("acceptanceCriteria") or []]
    criteria = [item for item in criteria if item]
    lines = [
        f"Implement task {key} from the project board: {task.get('title', '').strip()}",
        "",
        "## Description",
        "",
        description or "No description was given.",
        "",
        "## Acceptance criteria",
        "",
        *([f"- {item}" for item in criteria] or ["None were given."]),
        "",
        "## How to work",
        "",
        f"- You are in a git worktree on the branch `{branch}`. Make every change here.",
        "- Commit your work with clear messages as you go. Do not push, and do not switch or "
        "create branches.",
        "- Run the relevant tests or checks before you finish.",
        "- End with a short summary of what you changed.",
    ]
    return "\n".join(lines)


def usage_limit_in(logs: str) -> bool:
    tail = "\n".join(logs.splitlines()[-40:])
    return any(pattern.search(tail) for pattern in USAGE_LIMIT_PATTERNS)


def usage_limit_reset(logs: str) -> float | None:
    """When the limit resets, as epoch seconds, if the agent said so."""
    matches = USAGE_LIMIT_RESET.findall(logs)
    return float(matches[-1]) if matches else None


def summarize_logs(logs: str) -> str:
    """The end of the agent's output, where it summarises what it did."""
    lines = [line.rstrip() for line in logs.splitlines() if line.strip()]
    return "\n".join(lines[-SUMMARY_LINES:])[-SUMMARY_CHARS:]


def read_output(file: IO[bytes]) -> str:
    """The head and tail of a command's output file, read without loading all of it: a
    runaway command can write gigabytes."""
    size = file.seek(0, os.SEEK_END)
    file.seek(0)
    if size <= REPORT_OUTPUT_HEAD + REPORT_OUTPUT_TAIL:
        return file.read().decode("utf-8", errors="replace")
    head = file.read(REPORT_OUTPUT_HEAD).decode("utf-8", errors="replace")
    file.seek(size - REPORT_OUTPUT_TAIL)
    tail = file.read(REPORT_OUTPUT_TAIL).decode("utf-8", errors="replace")
    omitted = size - REPORT_OUTPUT_HEAD - REPORT_OUTPUT_TAIL
    return f"{head}\n[… {omitted} bytes omitted …]\n{tail}"


def format_duration(seconds: float) -> str:
    total = int(seconds)
    if total % 3600 == 0 and total >= 3600:
        return f"{total // 3600}h"
    if total % 60 == 0 and total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _commit_message(task: dict[str, Any], subject: str | None = None) -> str:
    key = task.get("key") or task.get("id")
    subject = subject or str(task.get("title") or "Board task").strip()
    return f"{subject}\n\nCommitted for {key} by vp board work."


Say = Callable[[str, str], None]


def _print(level: str, message: str) -> None:
    print(f"[{level}] {message}")


class BoardWorker:
    def __init__(
        self,
        client: BoardClient,
        runner: AgentRunner,
        options: WorkOptions,
        *,
        lock: ProfileLock | None = None,
        say: Say = _print,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        poll_seconds: float = 2.0,
        allow_repo: Callable[[Path], bool] | None = None,
    ) -> None:
        self.client = client
        self.runner = runner
        self.options = options
        self.lock = lock
        self.say = say
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self.poll_seconds = poll_seconds
        # Whether a repository may be handed to an agent; checked before git touches it.
        self.allow_repo = allow_repo
        self.worker_id: str | None = None
        self.heartbeat_seconds = 15.0
        self._last_heartbeat = float("-inf")
        self.status = "idle"
        self.status_reason: str | None = None
        self.task: dict[str, Any] | None = None
        self.step: str | None = None
        self.board_pause: str | None = None
        self.stop_requested = False
        self.cancel_reason: str | None = None
        self.usage_pause_until: float | None = None
        self.usage_resume_at: datetime | None = None
        self.summary = WorkSummary()
        # Tasks whose run was cancelled from the board: planned again, but passed over by
        # this worker so it does not restart what it was told to stop.
        self.passed_over: deque[str] = deque(maxlen=PASS_OVER_LIMIT)
        # Board writes that kept failing, tried again on every round of the loop.
        self.undelivered: list[tuple[str, Callable[[], object]]] = []
        self._heartbeat_lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------------------

    def run(self) -> WorkSummary:
        reply = self.client.register_worker(
            self.options.project,
            self.options.name,
            self.options.agent,
            self.options.machine,
        )
        self.worker_id = str(reply["item"]["id"])
        self.heartbeat_seconds = float(reply.get("heartbeatSeconds") or 15)
        self._last_heartbeat = self.clock()
        self.say("info", f"Connected to the board as {self.options.name}")
        self._apply(reply.get("instructions") or [])
        try:
            self._loop()
        finally:
            self._redeliver()
            with contextlib.suppress(BoardApiError):
                self.client.sign_off(self.worker_id)
            self.say("info", f"Signed off: {self.summary.ended_because or 'stopped'}")
        return self.summary

    def _loop(self) -> None:
        processed = 0
        while True:
            self._redeliver()
            if self.stop_requested:
                self.summary.ended_because = self.summary.ended_because or "Stopped from the board"
                return
            if self.options.max_tasks is not None and processed >= self.options.max_tasks:
                self.summary.ended_because = f"Worked on {processed} tasks (--max)"
                return
            if self.board_pause:
                if not self._wait_or_end(
                    self.board_pause,
                    f"Automation is paused: {self.board_pause}",
                ):
                    return
                continue
            if self.usage_pause_until is not None:
                if self.clock() < self.usage_pause_until:
                    if not self._wait_or_end(
                        self._usage_reason(),
                        "The agent reached its usage limit",
                        self.usage_pause_until,
                    ):
                        return
                    continue
                self.usage_pause_until = None
                self.say("info", "Resuming after the usage limit pause")
            self._set("idle")
            with self._profile_lock() as acquired:
                if not acquired:
                    continue
                item = self._claim()
                if item is not None:
                    processed += 1
                    self._work_on(item)
            if item is None:
                if self.board_pause:
                    continue
                if self.options.task:
                    self.summary.ended_because = f"Task {self.options.task} could not be claimed"
                    return
                if self.options.poll_seconds is None:
                    self.summary.ended_because = "No planned task left to claim"
                    return
                self._idle_wait(self.options.poll_seconds)
                continue
            if self.options.once or self.options.task:
                self.summary.ended_because = "Worked on one task"
                return

    def _wait_or_end(self, reason: str, ending: str, until: float | None = None) -> bool:
        """Waits paused while polling for work, else ends the worker. Returns whether to go
        on."""
        self._set("paused", reason)
        self._heartbeat(force=True)
        if self.options.poll_seconds is None or self.options.once or self.options.task:
            self.summary.ended_because = ending
            return False
        wait = self.options.poll_seconds
        if until is not None:
            wait = max(1.0, min(wait, until - self.clock()))
        self._idle_wait(wait)
        return True

    def _usage_reason(self) -> str:
        if self.usage_resume_at is None:
            return "Usage limit reached"
        return f"Usage limit reached; resuming at {self.usage_resume_at.astimezone():%H:%M}"

    @contextlib.contextmanager
    def _profile_lock(self) -> Iterator[bool]:
        if self.lock is None:
            yield True
            return
        announced = False
        while not self.lock.try_acquire():
            if self.stop_requested:
                yield False
                return
            if not announced:
                self.say("info", self.lock.waiting_reason)
                announced = True
            self._set("idle", self.lock.waiting_reason)
            self._idle_wait(5)
        try:
            yield True
        finally:
            self.lock.release()

    def _claim(self) -> dict[str, Any] | None:
        try:
            result = self.client.claim(
                self.options.project,
                self.options.name,
                task=self.options.task,
                labels=self.options.labels,
                min_readiness=self.options.min_readiness,
                exclude=list(self.passed_over),
                lease_seconds=self.options.lease_seconds,
            )
        except BoardApiError as exc:
            if self.options.task and exc.status in {400, 404, 409}:
                raise WorkerError(exc.message) from exc
            if _transient(exc) and self.options.poll_seconds is not None:
                self.say("warning", f"{exc.message}; retrying")
                return None
            raise
        if result.get("claimed"):
            item: dict[str, Any] = result["item"]
            self.say("info", f"Claimed {item['task'].get('key')}: {item['task'].get('title')}")
            return item
        if result.get("paused"):
            self.board_pause = str(result.get("reason") or "Automation is paused")
        self.say("info", str(result.get("reason") or "Nothing to claim"))
        return None

    # --- heartbeats and instructions ------------------------------------------------

    def _set(self, status: str, reason: str | None = None, step: str | None = None) -> None:
        changed = (status, reason, step) != (self.status, self.status_reason, self.step)
        self.status, self.status_reason, self.step = status, reason, step
        if changed:
            self._heartbeat(force=True)

    def _heartbeat(self, force: bool = False) -> None:
        with self._heartbeat_lock:
            self._send_heartbeat(force)

    def _send_heartbeat(self, force: bool) -> None:
        if self.worker_id is None:
            return
        if not force and self.clock() - self._last_heartbeat < self.heartbeat_seconds:
            return
        self._last_heartbeat = self.clock()
        working = self.status == "working" and self.task is not None
        try:
            reply = self.client.heartbeat(
                self.worker_id,
                self.status,
                reason=self.status_reason,
                task=str(self.task["id"]) if working and self.task else None,
                step=self.step if working else None,
                lease_seconds=self.options.lease_seconds,
            )
        except BoardApiError as exc:
            if exc.is_conflict or exc.is_not_found:
                # Signed off, or replaced by a worker registered under the same name.
                self.say("error", f"The board dropped this worker: {exc.message}")
                self.stop_requested = True
                self.summary.ended_because = "Dropped by the board"
            else:
                self.say("warning", f"Heartbeat failed: {exc.message}")
            return
        self._apply(reply.get("instructions") or [])

    def _apply(self, instructions: Sequence[dict[str, Any]]) -> None:
        pause = next((item for item in instructions if item.get("type") == "pause"), None)
        board_pause = str(pause.get("reason") or "Automation is paused") if pause else None
        if board_pause and not self.board_pause:
            self.say("info", f"Automation paused from the board: {board_pause}")
        elif self.board_pause and not board_pause:
            self.say("info", "Automation resumed from the board")
        self.board_pause = board_pause
        if any(item.get("type") == "stop" for item in instructions) and not self.stop_requested:
            self.say("info", "Stop requested from the board")
            self.stop_requested = True
        for item in instructions:
            if (
                item.get("type") == "cancel"
                and self.task is not None
                and item.get("taskId") == self.task.get("id")
                and self.cancel_reason is None
            ):
                self.cancel_reason = str(item.get("reason") or "Cancelled from the board")
                self.say("info", f"Run cancelled from the board: {self.cancel_reason}")

    def _idle_wait(self, seconds: float) -> None:
        deadline = self.clock() + seconds
        while self.clock() < deadline and not self.stop_requested:
            self._heartbeat()
            self.sleep(min(self.poll_seconds, max(0.0, deadline - self.clock())))

    def _wait_for(
        self,
        poll: Callable[[], int | None],
        stop: Callable[[], None],
        deadline: float | None,
    ) -> tuple[int | None, str]:
        """Waits for a process while heartbeating. Ends it on a stop or cancel from the board
        or at the deadline, and says why it ended: exit, stop, cancel or timeout. An
        interruption of the worker itself, such as Ctrl+C, ends the process too."""
        try:
            waited = 0
            while True:
                code = poll()
                if code is not None:
                    # A stop or cancel that came while the process was ending still counts.
                    return code, self._interruption(None) or "exit"
                ended = self._interruption(deadline)
                if ended is not None:
                    stop()
                    return None, ended
                self._heartbeat()
                # Quick commands finish without a full poll interval of waiting.
                self.sleep(min(self.poll_seconds, 0.1 * 2 ** min(waited, 5)))
                waited += 1
        except BaseException:
            with contextlib.suppress(Exception):
                stop()
            raise

    @contextlib.contextmanager
    def _keepalive(self) -> Iterator[None]:
        """Keeps heartbeats going while the main thread is busy, such as while an image is
        pulled or an overlay built for the agent, so the claim does not lapse meanwhile."""
        done = threading.Event()

        def beat() -> None:
            while not done.wait(1.0):
                self._heartbeat()

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join(timeout=5)

    def _interruption(self, deadline: float | None) -> str | None:
        if self.stop_requested:
            return "stop"
        if self.cancel_reason is not None:
            return "cancel"
        if deadline is not None and self.clock() >= deadline:
            return "timeout"
        return None

    # --- one task -------------------------------------------------------------------

    def _work_on(self, item: dict[str, Any]) -> None:
        task: dict[str, Any] = item["task"]
        card: dict[str, Any] = item["card"]
        key = str(task.get("key") or task["id"])
        self.task = task
        self.cancel_reason = None
        started_at = self.now()
        started = self.clock()
        self._set("working", step=STEP_PREPARING)
        result = TaskResult()
        repo: Path | None = None
        worktree: worktrees.Worktree | None = None
        try:
            try:
                repo = self._repository(task)
                result.branch = branch_name(self.options.branch_template, task)
                worktree = worktrees.prepare_worktree(
                    repo,
                    self._worktree_dir(repo),
                    result.branch,
                    self.options.base or worktrees.current_branch(repo) or "HEAD",
                    self.options.existing,
                )
                if worktree.continued:
                    self.say("info", f"Continuing on branch {worktree.branch} in {worktree.path}")
                else:
                    self.say("info", f"Working on branch {worktree.branch} in {worktree.path}")
                self._run(task, repo, worktree, result, started)
            except worktrees.BranchExistsError as exc:
                raise TaskProblem(f"{exc}; not continuing on it (--existing refuse)") from exc
            except worktrees.GitError as exc:
                raise TaskProblem(str(exc)) from exc
        except TaskProblem as problem:
            result.outcome, result.reason, result.release = "failed", str(problem), "blocked"
        except RunnerError as exc:
            result.outcome, result.reason, result.release = "failed", str(exc), "released"
            self.stop_requested = True
            self.summary.ended_because = f"The agent could not be started: {exc}"
        except KeyboardInterrupt:
            # A running agent or verify command was ended where it was waited for.
            result.outcome, result.reason, result.release = (
                "cancelled",
                "The worker was interrupted",
                "released",
            )
            self._finish(key, card, result, started_at, started, repo, worktree)
            raise
        except Exception as exc:
            # Something the worker did not expect: the task goes back before the error ends
            # the worker, instead of staying claimed until its lease runs out.
            result.outcome, result.reason, result.release = (
                "failed",
                f"The worker failed: {exc}",
                "released",
            )
            self._finish(key, card, result, started_at, started, repo, worktree)
            raise
        self._finish(key, card, result, started_at, started, repo, worktree)

    def _repository(self, task: dict[str, Any]) -> Path:
        path = self.options.repo
        if path is None and task.get("repositoryLocalPath"):
            path = Path(str(task["repositoryLocalPath"])).expanduser()
        if path is None:
            raise TaskProblem(
                "No repository to work in: pass --repo, or set the task's local repository path",
            )
        # Checked before git runs in it, since the path may come from the board, and again
        # for the repository it belongs to, which may be a parent the user did not allow.
        resolved = path.resolve()
        self._check_allowed(resolved)
        root = worktrees.repository_root(resolved)
        if root != resolved:
            self._check_allowed(root)
        return root

    def _check_allowed(self, path: Path) -> None:
        if self.allow_repo is not None and not self.allow_repo(path):
            raise TaskProblem(
                f"Repository {path} is not allowed for agents: run `vp config allow-dir {path}`",
            )

    def _worktree_dir(self, repo: Path) -> Path:
        return self.options.worktree_dir or repo.parent / f"{repo.name}-worktrees"

    def _failure(self) -> str:
        return "blocked" if self.options.on_fail == "blocked" else "failed"

    def _run(
        self,
        task: dict[str, Any],
        repo: Path,
        worktree: worktrees.Worktree,
        result: TaskResult,
        started: float,
    ) -> None:
        deadline = started + self.options.timeout_seconds if self.options.timeout_seconds else None
        self._set("working", step=STEP_AGENT)
        mounts = worktrees.agent_mounts(repo, worktree.path)
        pointers = worktrees.pointers(worktree.path)
        refs = worktrees.branch_refs(repo)
        with self._keepalive():
            run = self.runner.start(
                build_prompt(task, worktree.branch),
                worktree.path,
                mounts=mounts,
                allow_check_path=repo,
            )
        self.say(
            "info",
            f"Agent running as task {run.task_id[:12]} (vp task logs {run.task_id[:12]})",
        )
        code, ended = self._wait_for(run.poll, run.stop, deadline)
        logs = run.logs()
        result.summary = summarize_logs(logs)
        self._check_after_run(repo, worktree, pointers, refs)
        if self._ended_early(ended, result):
            return
        # Some agents exit cleanly when a limit stops them, so a run that left no work is
        # searched too.
        limited = usage_limit_in(logs) and (code != 0 or not self._did_work(worktree))
        if limited:
            result.outcome, result.reason, result.release = (
                "usage_limit",
                "The agent reached its usage limit",
                "released",
            )
            self._pause_for_usage_limit(logs)
            return
        if code != 0:
            result.outcome, result.reason, result.release = (
                "failed",
                f"The agent exited with code {code}",
                self._failure(),
            )
            return
        worktrees.commit_all(worktree.path, _commit_message(task))
        result.commits = worktrees.commits_since(worktree.path, worktree.start)
        # A continued branch may already hold the work, such as after a flaky verify.
        result.branch_commits = worktrees.commits_since(worktree.path, worktree.base)
        if not result.branch_commits:
            result.outcome, result.reason, result.release = (
                "failed",
                "The agent made no changes",
                self._failure(),
            )
            return
        if self.options.verify:
            self._set("working", step=STEP_VERIFYING)
            result.verify = self._verify(worktree.path, deadline)
            if self._ended_early(result.verify.ended, result):
                return
            if result.verify.exit_code != 0:
                result.outcome, result.reason, result.release = (
                    "failed",
                    f"The verify command exited with code {result.verify.exit_code}",
                    self._failure(),
                )
                return
            # What the verify command changed or committed, such as formatted files or
            # updated snapshots, goes with the work instead of being lost with the worktree.
            worktrees.commit_all(
                worktree.path,
                _commit_message(task, "Changes left by the verify command"),
            )
            result.commits = worktrees.commits_since(worktree.path, worktree.start)
            result.branch_commits = worktrees.commits_since(worktree.path, worktree.base)
        result.outcome, result.reason, result.release = "done", None, None

    def _did_work(self, worktree: worktrees.Worktree) -> bool:
        return worktrees.has_changes(worktree.path) or bool(
            worktrees.commits_since(worktree.path, worktree.start),
        )

    def _check_after_run(
        self,
        repo: Path,
        worktree: worktrees.Worktree,
        pointers: dict[str, str],
        refs: dict[str, str],
    ) -> None:
        """What the agent must not have done, checked before git on the host touches the
        worktree: redirect it to another git directory, move other branches or tags, or leave
        its own branch."""
        worktrees.verify_pointers(worktree.path, pointers)
        restored = worktrees.restore_refs(repo, refs, worktree.branch)
        if restored:
            raise TaskProblem(
                f"The agent moved {', '.join(restored)}; restored them, and the work on "
                f"{worktree.branch} needs a look",
            )
        on = worktrees.current_branch(worktree.path)
        if on != worktree.branch:
            raise TaskProblem(
                f"The agent left the branch {worktree.branch} (now on {on or 'a detached HEAD'}); "
                f"the worktree at {worktree.path} needs a look",
            )

    def _ended_early(self, ended: str, result: TaskResult) -> bool:
        if ended == "stop":
            result.outcome, result.reason, result.release = (
                "cancelled",
                "The worker was stopped from the board",
                "released",
            )
        elif ended == "cancel":
            # The board already put the task back; it is no longer ours to release.
            result.outcome, result.reason, result.release = "cancelled", self.cancel_reason, None
        elif ended == "timeout":
            limit = format_duration(self.options.timeout_seconds or 0)
            result.outcome, result.reason, result.release = (
                "timed_out",
                f"Timed out after {limit}",
                self._failure(),
            )
        else:
            return False
        return True

    def _verify(self, path: Path, deadline: float | None) -> VerifyResult:
        command = str(self.options.verify)
        self.say("info", f"Verifying with: {command}")
        # It runs code the agent wrote: the board token stays out of its environment.
        token = str(getattr(self.client, "token", "") or "")
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in PRIVATE_ENV and not (token and value == token)
        }
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=path,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=sys.platform != "win32",
            )

            def stop() -> None:
                _terminate(process)

            code, ended = self._wait_for(process.poll, stop, deadline)
            text = read_output(output)
        return VerifyResult(command=command, exit_code=code, output=text, ended=ended)

    def _pause_for_usage_limit(self, logs: str) -> None:
        wait = float(self.options.usage_limit_wait_seconds)
        reset = usage_limit_reset(logs)
        if reset is not None:
            wait = max(60.0, min(reset - time.time(), float(MAX_USAGE_LIMIT_WAIT)))
        self.usage_pause_until = self.clock() + wait
        self.usage_resume_at = self.now() + timedelta(seconds=wait)
        self.say("warning", f"{self._usage_reason()}; the task goes back to Planned")

    def _finish(
        self,
        key: str,
        card: dict[str, Any],
        result: TaskResult,
        started_at: datetime,
        started: float,
        repo: Path | None,
        worktree: worktrees.Worktree | None,
    ) -> None:
        if result.outcome == "done":
            # The board's last word before the hand-over: a stop or cancel still wins.
            self._set("working", step=STEP_HANDING_OVER)
            self._ended_early(self._interruption(None) or "exit", result)
        if self.cancel_reason is not None:
            # The board already put the task back; it is no longer ours to hand over or
            # release, whatever the run came to.
            if result.outcome == "done":
                result.outcome, result.reason = "cancelled", self.cancel_reason
            result.release = None
        card_ref = str(card["id"])
        branch, note = result.branch, self._handover_note(result)
        if result.outcome == "done":
            delivered = self._deliver(
                f"Handing {key} over",
                lambda: self.client.hand_over(card_ref, self.options.name, branch, note),
            )
            if delivered is True:
                self.summary.handed_over.append(key)
                self.say("success", f"Handed {key} over to Review on branch {result.branch}")
            elif isinstance(delivered, BoardApiError) and (
                delivered.is_conflict or delivered.is_not_found
            ):
                # The claim was taken back meanwhile, such as by a cancel from the board.
                result.outcome, result.reason, result.release = (
                    "cancelled",
                    delivered.message,
                    None,
                )
            elif isinstance(delivered, BoardApiError):
                # Refused for another reason, such as a branch name the board does not take:
                # the task is still held, and is blocked for a look rather than left claimed.
                result.outcome, result.reason, result.release = (
                    "failed",
                    f"The board refused the hand-over: {delivered.message}",
                    "blocked",
                )
        if result.outcome != "done" and result.release is not None:
            outcome = result.release
            reason = result.reason
            max_attempts = self.options.max_attempts if outcome == "failed" else None
            self._deliver(
                f"Giving {key} back",
                lambda: self.client.release(
                    card_ref,
                    self.options.name,
                    outcome,
                    reason,
                    max_attempts,
                ),
            )
            self.summary.returned.append(key)
            self.say("warning", f"{key}: {result.reason} ({outcome})")
        elif result.outcome != "done":
            self.summary.returned.append(key)
            self.say("warning", f"{key}: {result.reason}")
        if result.outcome != "done" and result.release is None:
            self.passed_over.append(self._task_id())
        self._report(key, result, started_at, started)
        if (
            result.outcome == "done"
            and worktree is not None
            and repo is not None
            and not self.options.keep_worktree
        ):
            try:
                worktrees.remove_worktree(repo, self._worktree_dir(repo), worktree.path)
            except worktrees.GitError as exc:
                self.say("warning", f"Could not remove the worktree: {exc}")
        self.task = None
        self.cancel_reason = None
        self._set("idle")

    def _deliver(self, what: str, write: Callable[[], object]) -> bool | BoardApiError | None:
        """A board write that must not get lost: tried again while the board is unreachable
        or failing, then kept for the next round of the loop. Says True once it went through,
        the error when the board refused it, and None when it is kept for later."""
        delay = 1.0
        for attempt in range(DELIVERY_ATTEMPTS):
            try:
                write()
                return True
            except BoardApiError as exc:
                if not _transient(exc):
                    self.say("warning", f"{what}: {exc.message}")
                    return exc
                if attempt == DELIVERY_ATTEMPTS - 1:
                    self.say("warning", f"{what} failed ({exc.message}); trying again later")
                    self.undelivered.append((what, write))
                    return None
                self.say("warning", f"{what} failed ({exc.message}); retrying")
                self._idle_wait(delay)
                delay = min(delay * 2, 30.0)
        return None

    def _redeliver(self) -> None:
        pending, self.undelivered = self.undelivered, []
        for what, write in pending:
            try:
                write()
                self.say("info", f"{what}: delivered")
            except BoardApiError as exc:
                if _transient(exc):
                    self.undelivered.append((what, write))
                else:
                    self.say("warning", f"{what}: {exc.message}")

    def _handover_note(self, result: TaskResult) -> str:
        commits = len(result.branch_commits or result.commits)
        note = f"{commits} commit{'s' if commits != 1 else ''}"
        if result.verify is not None:
            note += f"; verify passed ({result.verify.command})"
        return note

    def _report(self, key: str, result: TaskResult, started_at: datetime, started: float) -> None:
        verify = result.verify
        report: dict[str, Any] = {
            "outcome": result.outcome,
            "agent": self.options.agent,
            "workerId": self.worker_id,
            "summary": result.summary,
            "commits": result.commits,
            "branchName": result.branch,
            "verifyCommand": verify.command if verify else None,
            "verifyExitCode": verify.exit_code if verify else None,
            "verifyOutput": verify.output if verify else None,
            "durationSeconds": max(0, int(self.clock() - started)),
            "failureReason": result.reason if result.outcome != "done" else None,
            "startedAt": _iso(started_at),
            "finishedAt": _iso(self.now()),
        }
        task_id = self._task_id()
        self._deliver(
            f"Adding the run report to {key}",
            lambda: self.client.add_run_report(task_id, report),
        )

    def _task_id(self) -> str:
        assert self.task is not None
        return str(self.task["id"])


def _transient(exc: BoardApiError) -> bool:
    """Unreachable, or failing on its side: worth trying again."""
    return exc.status is None or exc.status >= 500


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stops a verify command and everything it started: politely first, then for good,
    since test runners leave children behind that outlive their shell."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        with contextlib.suppress(OSError):
            process.kill()
        return
    with contextlib.suppress(OSError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    # The group outlives its shell when a child ignored the signal.
    with contextlib.suppress(OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_GRACE_SECONDS)

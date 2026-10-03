"""`vp board work`: an agent works through the planned tasks of a board project.

For each task the worker claims it on the board, checks its branch out in a worktree of its
own, runs the agent headless with the task as the prompt, runs an optional verify command, and
hands the task over to Review, or gives it back to Planned (or blocks it) with a note. Every run
leaves a report on its task.

In review mode the worker claims tasks in Review instead. It checks the commit the hand-over
named out, detached, in a worktree of its own, has the agent judge the work without changing
anything, and sends the verdict: approve, rework with feedback, or a question. The review
must leave the repository as it found it; whatever the agent changed is thrown away.

The worker is registered with the board while it runs and reports what it does in heartbeats.
Their replies carry the board's instructions: pause (take no new task), stop (end the run, give
the task back and sign off) and cancel (end the run of a task the worker no longer holds).
Heartbeats are sent from the worker's own wait loops, so it stays single-threaded.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import string
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

MODE_IMPLEMENT = "implement"
MODE_REVIEW = "review"

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
# The share of the claim's lease that may pass without a renewal before the run is stopped:
# the board gives the task to another worker once the lease runs out.
LEASE_SAFETY = 0.75
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


class AgentStopError(RunnerError):
    """The agent could not be stopped and may still be working in the worktree. Its task
    stays claimed until the lease runs out instead of going to another worker meanwhile."""


class WorkerError(Exception):
    """The worker cannot go on, such as a named task that cannot be claimed."""


class TaskProblem(Exception):
    """A task that cannot run as it stands, such as one without a repository; it is blocked
    with the message as the reason."""


class ReviewProblem(Exception):
    """A review that went wrong, such as one whose agent changed the repository; it ends as
    failed, with the message as the reason, and the task stays in Review for others."""


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
    # implement claims planned tasks and does the work; review judges tasks in Review.
    mode: str = MODE_IMPLEMENT
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
    # Review mode: the tasks approved and those sent back for rework; `returned` has the
    # reviews that ended otherwise.
    approved: list[str] = field(default_factory=list)
    reworked: list[str] = field(default_factory=list)
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
    # How the task goes back when it is not handed over: failed, blocked, needs_input,
    # released, or None when it is no longer held (a run cancelled from the board).
    release: str | None = "failed"
    summary: str = ""
    # The commits this run made, and all the work the branch holds.
    commits: list[dict[str, str]] = field(default_factory=list)
    branch_commits: list[dict[str, str]] = field(default_factory=list)
    branch: str | None = None
    # The commit the branch ends at once the work is done: what the reviews judge.
    head: str | None = None
    verify: VerifyResult | None = None


@dataclass
class ReviewResult:
    # The run report's outcome: done (the review came to a verdict on the work), failed,
    # needs_input, timed_out, cancelled or usage_limit.
    outcome: str = "failed"
    reason: str | None = None
    # The verdict sent to the board: approve, rework, needs_input, failed or released; None
    # when there is none to send, such as for a review that already ended on the board.
    verdict: str | None = "failed"
    # The rework feedback, the question, or why the review failed.
    note: str | None = None
    summary: str = ""
    branch: str | None = None
    # The commit the claim named for the review, and the one checked out: the branch's tip
    # when the claim named none.
    head_sha: str | None = None
    commit: str | None = None
    verify: VerifyResult | None = None
    # Kept for a look rather than removed: git must not run in it.
    keep_worktree: bool = False


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
    if "issue" in _template_fields(template) and not values["issue"]:
        return key
    try:
        return template.format(**values)
    except (KeyError, IndexError, TypeError, ValueError):
        return key


def _template_fields(template: str) -> set[str]:
    """The names a template uses, such as `issue` for both `{issue}` and `{issue:04d}`."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return set()
    return {re.split(r"[.\[]", name, maxsplit=1)[0] for _, name, _, _ in parsed if name}


def validate_branch_template(template: str) -> None:
    try:
        template.format(issue=1, number=1, key="vp-1", project="vp")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(
            f"Invalid branch template {template!r}: use {{issue}}, {{number}}, {{key}} or "
            "{project}",
        ) from exc


RESULT_TAG = "vibepod-result"
# A block never spans another opening tag, so a mention of the tag in the agent's prose
# cannot swallow the real block after it.
RESULT_BLOCK = re.compile(
    rf"<{RESULT_TAG}>((?:(?!<{RESULT_TAG}>).)*?)</{RESULT_TAG}>",
    re.DOTALL | re.IGNORECASE,
)
RESULT_STATUSES = ("done", "needs_input", "failed")
# How much of the earlier conversation of a task goes into the prompt.
CONVERSATION_LIMIT = 20

RESULT_INSTRUCTIONS = f"""## How to finish

End your final message with a result block in this form, and nothing after it:

<{RESULT_TAG}>
{{"status": "<done | needs_input | failed>", "summary": "<what you did, in a sentence or two>"}}
</{RESULT_TAG}>

- `done`: the task is implemented and committed.
- `needs_input`: the task is unclear and you cannot go on without an answer. Add
  `"question"` with one precise question, and do not guess instead.
- `failed`: you cannot complete the task. Add `"reason"` with why."""


REVIEW_STATUSES = ("approve", "rework", "needs_input", "failed")

REVIEW_INSTRUCTIONS = f"""## How to finish

End your final message with a result block in this form, and nothing after it:

<{RESULT_TAG}>
{{"status": "<approve | rework | needs_input | failed>", "summary": "<your verdict, briefly>"}}
</{RESULT_TAG}>

- `approve`: the work does what the task asks and meets its acceptance criteria; a pull
  request can be opened.
- `rework`: the work needs changes. Add `"feedback"` with concrete, actionable points, as a
  list of strings: the next implementation run gets them as they are.
- `needs_input`: you cannot judge the work without an answer from a person. Add
  `"question"` with one precise question.
- `failed`: you cannot review the work. Add `"reason"` with why."""


@dataclass(frozen=True)
class AgentResult:
    """How the agent says its run ended."""

    status: str
    summary: str = ""
    question: str = ""
    reason: str = ""
    # A reviewer's points for the rework.
    feedback: str = ""


def _text(value: Any) -> str:
    """A string field of the result; a list, such as of feedback points, becomes a list."""
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item.removeprefix('- ')}" for item in items)
    return str(value or "").strip()


def parse_result(logs: str, statuses: Sequence[str] = RESULT_STATUSES) -> AgentResult | None:
    """The last well-formed result block in the agent's output, or None. The instructions in
    the prompt may be echoed, but their placeholder status never parses. A question needs
    its question, and a rework its feedback."""
    found: AgentResult | None = None
    for block in RESULT_BLOCK.findall(logs):
        text = block.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("status") not in statuses:
            continue
        result = AgentResult(
            status=str(data["status"]),
            summary=_text(data.get("summary")),
            question=_text(data.get("question")),
            reason=_text(data.get("reason")),
            feedback=_text(data.get("feedback")),
        )
        if result.status == "needs_input" and not result.question:
            continue
        if result.status == "rework" and not result.feedback:
            continue
        found = result
    return found


def conversation(history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The questions, answers and review feedback of a task, oldest first."""
    kept = [event for event in history if event.get("kind") in {"question", "answer", "feedback"}]
    return list(reversed(kept))[-CONVERSATION_LIMIT:]


# What a reviewer gets of the task history: the conversation and the earlier verdicts.
REVIEW_HISTORY = {
    "question": "Question",
    "answer": "Answer",
    "feedback": "Review feedback",
    "approved": "Review",
    "rework_requested": "Review",
}


def review_history(history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The questions, answers, review feedback and earlier verdicts of a task, oldest
    first."""
    kept = [event for event in history if event.get("kind") in REVIEW_HISTORY]
    return list(reversed(kept))[-CONVERSATION_LIMIT:]


def _task_lines(task: dict[str, Any]) -> list[str]:
    description = str(task.get("details") or task.get("summary") or "").strip()
    criteria = [str(item).strip() for item in task.get("acceptanceCriteria") or []]
    criteria = [item for item in criteria if item]
    return [
        "## Description",
        "",
        description or "No description was given.",
        "",
        "## Acceptance criteria",
        "",
        *([f"- {item}" for item in criteria] or ["None were given."]),
    ]


def build_review_prompt(
    task: dict[str, Any],
    branch: str,
    commit: str,
    base: str,
    base_commit: str,
    earlier: Sequence[dict[str, Any]] = (),
) -> str:
    """A reviewer's instructions: the task, its history, where the work is and what it
    branched off, that the review only reads, and how to give the verdict."""
    key = task.get("key") or "the task"
    lines = [
        f"Review the work on task {key} from the project board: "
        f"{str(task.get('title') or '').strip()}",
        "",
        "Your review answers one question: does the work need rework, or can a pull request "
        "be opened?",
        "",
        *_task_lines(task),
    ]
    if earlier:
        lines += ["", "## Task history", ""]
        for event in earlier:
            kind = str(event.get("kind"))
            actor = (
                f" from {event['actor']}"
                if event.get("actor") and kind in {"question", "answer", "feedback"}
                else ""
            )
            message = str(event.get("message") or "").strip()
            lines.append(f"- {REVIEW_HISTORY.get(kind, 'Note')}{actor}: {message}")
    lines += [
        "",
        "## How to review",
        "",
        f"- You are in a git worktree with commit `{commit}` of the branch `{branch}` checked "
        "out, detached.",
        f"- The work branched off `{base}`: read it with `git diff {base_commit}...HEAD` and "
        f"`git log {base_commit}..HEAD`.",
        "- Check it against the description and each acceptance criterion, and run the "
        "relevant tests or checks where you can.",
        "- Only read: do not change, create or delete files, do not commit, and do not switch "
        "or create branches. Any change is thrown away, and the review then counts as failed.",
        "",
        REVIEW_INSTRUCTIONS,
    ]
    return "\n".join(lines)


def build_prompt(
    task: dict[str, Any],
    branch: str,
    earlier: Sequence[dict[str, Any]] = (),
    rework: bool = False,
) -> str:
    """The agent's instructions: the task's title, description and acceptance criteria, what
    was asked, answered and fed back in earlier runs, how to leave the work, and how to say
    how the run ended."""
    key = task.get("key") or "the task"
    lines = [
        f"Implement task {key} from the project board: {task.get('title', '').strip()}",
        "",
        *_task_lines(task),
    ]
    if earlier:
        lines += ["", "## Earlier questions, answers and review feedback", ""]
        if rework:
            lines += [
                "This task was reviewed and sent back. The branch already holds the earlier "
                "work: continue it and address the review feedback.",
                "",
            ]
        labels = {"question": "Your question", "answer": "Answer", "feedback": "Review feedback"}
        for event in earlier:
            label = labels.get(str(event.get("kind")), "Note")
            actor = (
                f" from {event['actor']}"
                if event.get("actor") and event["kind"] != "question"
                else ""
            )
            lines.append(f"- {label}{actor}: {str(event.get('message') or '').strip()}")
    lines += [
        "",
        "## How to work",
        "",
        f"- You are in a git worktree on the branch `{branch}`. Make every change here.",
        "- Commit your work with clear messages as you go. Do not push, and do not switch or "
        "create branches.",
        "- Run the relevant tests or checks before you finish.",
        "",
        RESULT_INSTRUCTIONS,
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
Write = Callable[[], object]


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
        # Board writes that kept failing, tried again on every round of the loop, each with
        # the write that replaces it once the worker was stopped, if any.
        self.undelivered: list[tuple[str, Write, tuple[str, Write] | None]] = []
        self._heartbeat_lock = threading.Lock()
        # Why the agent of the current task could not be stopped, if it could not.
        self.stop_failed: str | None = None
        # When the claim on the current task was last claimed or renewed.
        self.renewed: float | None = None

    @property
    def reviewing(self) -> bool:
        return self.options.mode == MODE_REVIEW

    # --- lifecycle ------------------------------------------------------------------

    def run(self) -> WorkSummary:
        reply = self.client.register_worker(
            self.options.project,
            self.options.name,
            self.options.agent,
            self.options.machine,
            mode=MODE_REVIEW if self.reviewing else None,
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
                    self.summary.ended_because = (
                        "No task in review left to claim"
                        if self.reviewing
                        else "No planned task left to claim"
                    )
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
                mode=MODE_REVIEW if self.reviewing else None,
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
            if self.reviewing:
                item = {**item, "review": self._review_claim(item, result.get("review"))}
            self.say("info", f"Claimed {item['task'].get('key')}: {item['task'].get('title')}")
            return item
        if result.get("paused"):
            self.board_pause = str(result.get("reason") or "Automation is paused")
        self.say("info", str(result.get("reason") or "Nothing to claim"))
        return None

    def _review_claim(self, item: dict[str, Any], review: Any) -> dict[str, Any]:
        """The review a review claim carries. A board that does not know review workers
        ignores the mode and claims a planned task to implement instead: that one goes back
        right away."""
        if isinstance(review, dict):
            return review
        card = str(item["card"]["id"])
        with contextlib.suppress(BoardApiError):
            self.client.release(
                card,
                self.options.name,
                "released",
                "Claimed by a review worker on a board without reviews",
            )
        raise WorkerError(
            "The board claimed a task to implement instead of one to review: it does not "
            "support review workers yet, so update it to use --mode review",
        )

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
        sent = self._last_heartbeat = self.clock()
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
        if working:
            self.renewed = sent
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
                    self._stop(stop)
                    return None, ended
                self._heartbeat()
                # Quick commands finish without a full poll interval of waiting.
                self.sleep(min(self.poll_seconds, 0.1 * 2 ** min(waited, 5)))
                waited += 1
        except BaseException:
            if self.stop_failed is None:
                with contextlib.suppress(Exception):
                    self._stop(stop)
            raise

    def _stop(self, stop: Callable[[], None]) -> None:
        try:
            stop()
        except AgentStopError as exc:
            self.stop_failed = str(exc)
            raise

    @contextlib.contextmanager
    def _keepalive(self) -> Iterator[None]:
        """Keeps heartbeats going while the main thread is busy, such as while git checks a
        branch out or an image is pulled for the agent, so the claim does not lapse
        meanwhile."""
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
        if self._lease_lapsing():
            return "lease"
        if deadline is not None and self.clock() >= deadline:
            return "timeout"
        return None

    def _lease_lapsing(self) -> bool:
        """Whether the claim went unrenewed for so long, such as while the board cannot be
        reached, that the board may soon give the task to another worker."""
        if self.task is None or self.renewed is None:
            return False
        return self.clock() - self.renewed >= self.options.lease_seconds * LEASE_SAFETY

    # --- one task -------------------------------------------------------------------

    def _work_on(self, item: dict[str, Any]) -> None:
        if self.reviewing:
            self._review(item)
            return
        task: dict[str, Any] = item["task"]
        card: dict[str, Any] = item["card"]
        key = str(task.get("key") or task["id"])
        self.task = task
        self.cancel_reason = None
        self.stop_failed = None
        started_at = self.now()
        started = self.renewed = self.clock()
        self._set("working", step=STEP_PREPARING)
        result = TaskResult()
        repo: Path | None = None
        worktree: worktrees.Worktree | None = None
        # A task sent back from Review keeps the branch it was handed over on, and a task that
        # asked a question keeps the branch its draft is on: both continue there, whatever
        # --existing says.
        rework = bool(card.get("branchName"))
        earlier = self._conversation(task)
        answered = any(event.get("kind") in {"question", "answer"} for event in earlier)
        try:
            try:
                repo = self._repository(task)
                result.branch = (
                    str(card["branchName"])
                    if rework
                    else branch_name(self.options.branch_template, task)
                )
                if rework and not worktrees.branch_exists(repo, result.branch):
                    raise TaskProblem(
                        f"The rework needs the branch {result.branch} it was handed over on, "
                        f"and it is not in {repo}",
                    )
                # Checking a branch out can take long in a large repository.
                with self._keepalive():
                    worktree = worktrees.prepare_worktree(
                        repo,
                        self._worktree_dir(repo),
                        result.branch,
                        self.options.base or worktrees.current_branch(repo) or "HEAD",
                        "continue" if rework or answered else self.options.existing,
                    )
                if worktree.continued:
                    self.say("info", f"Continuing on branch {worktree.branch} in {worktree.path}")
                else:
                    self.say("info", f"Working on branch {worktree.branch} in {worktree.path}")
                self._run(task, repo, worktree, result, started, earlier, rework)
            except worktrees.BranchExistsError as exc:
                raise TaskProblem(f"{exc}; not continuing on it (--existing refuse)") from exc
            except worktrees.GitError as exc:
                raise TaskProblem(str(exc)) from exc
        except TaskProblem as problem:
            result.outcome, result.reason, result.release = "failed", str(problem), "blocked"
        except AgentStopError as exc:
            result.outcome, result.reason, result.release = "failed", str(exc), None
            self.stop_requested = True
            self.summary.ended_because = f"The agent could not be stopped: {exc}"
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
        earlier: Sequence[dict[str, Any]] = (),
        rework: bool = False,
    ) -> None:
        deadline = started + self.options.timeout_seconds if self.options.timeout_seconds else None
        self._set("working", step=STEP_AGENT)
        with self._keepalive():
            mounts = worktrees.agent_mounts(repo, worktree.path)
            pointers = worktrees.pointers(worktree.path)
            refs = worktrees.branch_refs(repo)
            checked_out = worktrees.checkouts(repo)
            # A stop or cancel that came while the worktree was prepared ends the run before
            # the agent starts, rather than once it already writes to the worktree.
            if self._ended_early(self._interruption(deadline) or "exit", result):
                return
            run = self.runner.start(
                build_prompt(task, worktree.branch, earlier, rework),
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
        with self._keepalive():
            self._check_after_run(repo, worktree, pointers, refs, checked_out)
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
        said = parse_result(logs)
        if said is None:
            result.outcome, result.reason, result.release = (
                "failed",
                "The run ended without a readable result",
                self._failure(),
            )
            return
        result.summary = said.summary or result.summary
        if said.status == "needs_input":
            # The work so far stays on the branch; the answered task continues there.
            with self._keepalive():
                worktrees.commit_all(worktree.path, _commit_message(task))
                result.commits = worktrees.commits_since(worktree.path, worktree.start)
            result.outcome, result.reason, result.release = (
                "needs_input",
                said.question,
                "needs_input",
            )
            return
        if said.status == "failed":
            result.outcome, result.reason, result.release = (
                "failed",
                f"The agent could not finish: {said.reason or 'no reason given'}",
                self._failure(),
            )
            return
        with self._keepalive():
            worktrees.commit_all(worktree.path, _commit_message(task))
            result.commits = worktrees.commits_since(worktree.path, worktree.start)
            # A continued branch may already hold the work, such as after a flaky verify.
            result.branch_commits = worktrees.commits_since(worktree.path, worktree.base)
        if not result.branch_commits:
            result.outcome, result.reason, result.release = (
                "failed",
                "The agent reported done but made no changes",
                self._failure(),
            )
            return
        if self.options.verify:
            self._set("working", step=STEP_VERIFYING)
            result.verify = self._verify(worktree.path, deadline)
            # The verify command ran code the agent wrote, which may have changed what the
            # agent itself must not: checked again before git on the host goes on.
            with self._keepalive():
                self._check_after_run(repo, worktree, pointers, refs, checked_out)
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
            with self._keepalive():
                worktrees.commit_all(
                    worktree.path,
                    _commit_message(task, "Changes left by the verify command"),
                )
                result.commits = worktrees.commits_since(worktree.path, worktree.start)
                result.branch_commits = worktrees.commits_since(worktree.path, worktree.base)
        result.head = worktrees.git(worktree.path, "rev-parse", "HEAD")
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
        checked_out: dict[str, Path],
    ) -> None:
        """What the agent must not have done, checked before git on the host touches the
        worktree: redirect it to another git directory, move other branches or tags, or leave
        its own branch."""
        worktrees.verify_pointers(worktree.path, pointers)
        restored, left = worktrees.restore_refs(
            repo,
            refs,
            worktree.branch,
            checked_out,
        )
        if restored:
            raise TaskProblem(
                f"The agent moved {', '.join(restored)}; restored them, and the work on "
                f"{worktree.branch} needs a look",
            )
        if left:
            raise TaskProblem(
                f"{', '.join(left)} moved during the run, in a checkout with changes staged; "
                f"left as they are: check whether the agent moved them, and the work on "
                f"{worktree.branch}",
            )
        on = worktrees.current_branch(worktree.path)
        if on != worktree.branch:
            raise TaskProblem(
                f"The agent left the branch {worktree.branch} (now on {on or 'a detached HEAD'}); "
                f"the worktree at {worktree.path} needs a look",
            )

    def _conversation(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        return self._history(task, conversation)

    def _history(
        self,
        task: dict[str, Any],
        keep: Callable[[Sequence[dict[str, Any]]], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        try:
            return keep(self.client.task_history(str(task["id"])))
        except BoardApiError as exc:
            self.say("warning", f"Could not read the task history: {exc.message}")
            return []

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
        elif ended == "lease":
            unrenewed = format_duration(self.clock() - (self.renewed or 0))
            result.outcome, result.reason, result.release = (
                "failed",
                f"The claim could not be renewed for {unrenewed}; stopped before it runs out",
                "released",
            )
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
        if self.stop_failed is not None:
            # The agent may still be writing to the worktree: the claim stays until its lease
            # runs out, so that no other worker starts on the task meanwhile.
            result.release = None
        card_ref = str(card["id"])
        branch, note, head = result.branch, self._handover_note(result), result.head
        if result.outcome == "done":
            stopped = "The worker was stopped from the board"
            delivered = self._deliver(
                f"Handing {key} over",
                lambda: self.client.hand_over(card_ref, self.options.name, branch, note, head),
                instead=(
                    f"Giving {key} back",
                    lambda: self.client.release(card_ref, self.options.name, "released", stopped),
                ),
            )
            if delivered is False:
                # A stop or cancel came while the hand-over was tried again: it still wins.
                self._ended_early(self._interruption(None) or "stop", result)
            elif delivered is True:
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

    def _deliver(
        self,
        what: str,
        write: Write,
        instead: tuple[str, Write] | None = None,
    ) -> bool | BoardApiError | None:
        """A board write that must not get lost: tried again while the board is unreachable
        or failing, then kept for the next round of the loop. Says True once it went through,
        the error when the board refused it, and None when it is kept for later.

        A write with one to make `instead` is given up once a stop or cancel comes between
        its tries, which says False, and is replaced by that one if the worker is stopped
        while it is kept."""
        delay = 1.0
        for attempt in range(DELIVERY_ATTEMPTS):
            if attempt and instead is not None and self._interruption(None) is not None:
                return False
            try:
                write()
                return True
            except BoardApiError as exc:
                if not _transient(exc):
                    self.say("warning", f"{what}: {exc.message}")
                    return exc
                if attempt == DELIVERY_ATTEMPTS - 1:
                    self.say("warning", f"{what} failed ({exc.message}); trying again later")
                    self.undelivered.append((what, write, instead))
                    return None
                self.say("warning", f"{what} failed ({exc.message}); retrying")
                self._idle_wait(delay)
                delay = min(delay * 2, 30.0)
        return None

    def _redeliver(self) -> None:
        pending, self.undelivered = self.undelivered, []
        for what, write, instead in pending:
            if instead is not None and self.stop_requested:
                (what, write), instead = instead, None
            try:
                write()
                self.say("info", f"{what}: delivered")
            except BoardApiError as exc:
                if _transient(exc):
                    self.undelivered.append((what, write, instead))
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
            "failureReason": (
                f"Needs input: {result.reason}"
                if result.outcome == "needs_input"
                else result.reason
                if result.outcome != "done"
                else None
            ),
            "startedAt": _iso(started_at),
            "finishedAt": _iso(self.now()),
        }
        task_id = self._task_id()
        self._deliver(
            f"Adding the run report to {key}",
            lambda: self.client.add_run_report(task_id, report),
        )

    # --- one review -----------------------------------------------------------------

    def _review(self, item: dict[str, Any]) -> None:
        task: dict[str, Any] = item["task"]
        card: dict[str, Any] = item["card"]
        review: dict[str, Any] = item["review"]
        key = str(task.get("key") or task["id"])
        self.task = task
        self.cancel_reason = None
        self.stop_failed = None
        started_at = self.now()
        started = self.clock()
        self._set("working", step=STEP_PREPARING)
        result = ReviewResult(
            branch=str(card.get("branchName") or "") or None,
            head_sha=str(review.get("headSha") or "") or None,
        )
        repo: Path | None = None
        path: Path | None = None
        try:
            try:
                repo = self._repository(task)
                # Checking a commit out can take long in a large repository.
                with self._keepalive():
                    path, base, base_commit = self._prepare_review(task, repo, result)
                self.say(
                    "info",
                    f"Reviewing {result.branch} at {(result.commit or '')[:12]} in {path}",
                )
                self._run_review(task, repo, path, base, base_commit, result, started)
            except worktrees.GitError as exc:
                raise TaskProblem(str(exc)) from exc
        except TaskProblem as problem:
            # Blocked for a person, as a task that cannot run is: the card stays in Review
            # with the reason as its question.
            reason = str(problem)
            result.outcome, result.reason, result.verdict, result.note = (
                "failed",
                reason,
                "needs_input",
                f"The review cannot run: {reason}",
            )
        except ReviewProblem as problem:
            reason = str(problem)
            result.outcome, result.reason, result.verdict, result.note = (
                "failed",
                reason,
                "failed",
                reason,
            )
        except AgentStopError as exc:
            result.outcome, result.reason, result.verdict = "failed", str(exc), None
            self.stop_requested = True
            self.summary.ended_because = f"The agent could not be stopped: {exc}"
        except RunnerError as exc:
            result.outcome, result.reason, result.verdict = "failed", str(exc), "released"
            self.stop_requested = True
            self.summary.ended_because = f"The agent could not be started: {exc}"
        except KeyboardInterrupt:
            result.outcome, result.reason, result.verdict = (
                "cancelled",
                "The worker was interrupted",
                "released",
            )
            self._finish_review(key, card, result, started_at, started, repo, path)
            raise
        except Exception as exc:
            result.outcome, result.reason, result.verdict = (
                "failed",
                f"The worker failed: {exc}",
                "released",
            )
            self._finish_review(key, card, result, started_at, started, repo, path)
            raise
        self._finish_review(key, card, result, started_at, started, repo, path)

    def _prepare_review(
        self,
        task: dict[str, Any],
        repo: Path,
        result: ReviewResult,
    ) -> tuple[Path, str, str]:
        """Checks the commit under review out in a worktree of the reviewer's own. Says
        where, and the base the work is diffed against, by name and commit."""
        branch = result.branch
        if not branch:
            raise TaskProblem("The card names no branch to review")
        if not worktrees.branch_exists(repo, branch):
            raise TaskProblem(f"The branch {branch} to review is not in {repo}")
        if result.head_sha:
            if not worktrees.commit_exists(repo, result.head_sha):
                raise TaskProblem(
                    f"The commit {result.head_sha[:12]} of {branch} to review is not in {repo}",
                )
            result.commit = worktrees.git(repo, "rev-parse", f"{result.head_sha}^{{commit}}")
        else:
            # Handed over without naming its commit: the branch as it is now.
            result.commit = worktrees.git(repo, "rev-parse", f"refs/heads/{branch}^{{commit}}")
        base = self.options.base or worktrees.current_branch(repo) or "HEAD"
        base_commit = worktrees.resolve_commit(repo, base)
        key = str(task.get("key") or task["id"]).lower()
        path = worktrees.prepare_review_worktree(
            repo,
            self._worktree_dir(repo),
            f"review-{key}-{self.options.name}",
            result.commit,
        )
        return path, base, base_commit

    def _run_review(
        self,
        task: dict[str, Any],
        repo: Path,
        path: Path,
        base: str,
        base_commit: str,
        result: ReviewResult,
        started: float,
    ) -> None:
        assert result.branch is not None and result.commit is not None
        deadline = started + self.options.timeout_seconds if self.options.timeout_seconds else None
        earlier = self._history(task, review_history)
        self._set("working", step=STEP_AGENT)
        with self._keepalive():
            mounts = worktrees.agent_mounts(repo, path, read_only=True)
            pointers = worktrees.pointers(path)
            refs = worktrees.branch_refs(repo)
            checked_out = worktrees.checkouts(repo)
            run = self.runner.start(
                build_review_prompt(task, result.branch, result.commit, base, base_commit, earlier),
                path,
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
        with self._keepalive():
            self._check_review(repo, path, result, pointers, refs, checked_out)
        if self._review_ended_early(ended, result):
            return
        said = parse_result(logs, REVIEW_STATUSES)
        if usage_limit_in(logs) and (code != 0 or said is None):
            result.outcome, result.reason, result.verdict = (
                "usage_limit",
                "The agent reached its usage limit",
                "released",
            )
            self._pause_for_usage_limit(logs)
            return
        if code != 0:
            self._review_failed(result, f"The agent exited with code {code}")
            return
        if self.options.verify:
            self._set("working", step=STEP_VERIFYING)
            result.verify = self._verify(path, deadline)
            with self._keepalive():
                self._check_refs(repo, refs, checked_out, "The verify command")
            if self._review_ended_early(result.verify.ended, result):
                return
            if result.verify.exit_code != 0:
                # Failing checks need rework, whatever the agent made of the work.
                points = said.feedback if said is not None and said.status == "rework" else ""
                result.outcome, result.reason, result.verdict, result.note = (
                    "done",
                    None,
                    "rework",
                    _verify_feedback(result.verify, points),
                )
                result.summary = f"The verify command failed. {said.summary if said else ''}"
                result.summary = result.summary.strip()
                return
        if said is None:
            self._review_failed(result, "The run ended without a readable result")
            return
        result.summary = said.summary or result.summary
        if said.status == "approve":
            result.outcome, result.reason, result.verdict, result.note = (
                "done",
                None,
                "approve",
                said.summary or None,
            )
        elif said.status == "rework":
            result.outcome, result.reason, result.verdict, result.note = (
                "done",
                None,
                "rework",
                said.feedback,
            )
        elif said.status == "needs_input":
            result.outcome, result.reason, result.verdict, result.note = (
                "needs_input",
                said.question,
                "needs_input",
                said.question,
            )
        else:
            self._review_failed(
                result,
                f"The agent could not review: {said.reason or 'no reason given'}",
            )

    def _review_failed(self, result: ReviewResult, reason: str) -> None:
        result.outcome, result.reason, result.verdict, result.note = (
            "failed",
            reason,
            "failed",
            reason,
        )

    def _check_review(
        self,
        repo: Path,
        path: Path,
        result: ReviewResult,
        pointers: dict[str, str],
        refs: dict[str, str],
        checked_out: dict[str, Path],
    ) -> None:
        """The review must leave the repository as it found it. What the agent committed,
        switched to or left changed in the worktree is thrown away, refs it moved are put
        back, and the review fails. Nothing of git runs in a worktree whose pointers into
        the git directory changed."""
        assert result.commit is not None
        try:
            worktrees.verify_pointers(path, pointers)
        except worktrees.GitError as exc:
            result.keep_worktree = True
            reason = f"{exc}; the worktree at {path} needs a look"
            try:
                self._check_refs(repo, refs, checked_out, "The agent")
            except ReviewProblem as problem:
                reason += f". {problem}"
            raise ReviewProblem(reason) from exc
        changed: list[str] = []
        on = worktrees.current_branch(path)
        if on is not None:
            changed.append(f"switched to the branch {on}")
        if worktrees.git(path, "rev-parse", "HEAD") != result.commit:
            changed.append("committed")
        if worktrees.has_changes(path):
            changed.append("left uncommitted changes")
        if changed:
            worktrees.discard_changes(path, result.commit)
            created = f"refs/heads/{on}"
            if on is not None and created not in refs:
                # A branch the agent made for its commits; nobody else knows it.
                worktrees.git(repo, "update-ref", "-d", created)
        try:
            self._check_refs(repo, refs, checked_out, "The agent")
        except ReviewProblem as problem:
            if not changed:
                raise
            raise ReviewProblem(
                f"The agent {_and(changed)} in the review worktree; threw that away. {problem}",
            ) from problem
        if changed:
            raise ReviewProblem(
                f"The agent {_and(changed)} in the review worktree, which a review must not "
                "do; threw that away",
            )

    def _check_refs(
        self,
        repo: Path,
        refs: dict[str, str],
        checked_out: dict[str, Path],
        who: str,
    ) -> None:
        """Puts back the branches and tags moved during a review: none of them is the
        reviewer's to move, the reviewed branch least of all."""
        restored, left = worktrees.restore_refs(
            repo,
            refs,
            "",
            self._worktree_dir(repo),
            checked_out,
        )
        if restored:
            raise ReviewProblem(f"{who} moved {', '.join(restored)}; restored them")
        if left:
            raise ReviewProblem(
                f"{', '.join(left)} moved during the review, in a checkout with changes "
                "staged; left as they are: check whether the review moved them",
            )

    def _review_ended_early(self, ended: str, result: ReviewResult) -> bool:
        if ended == "stop":
            result.outcome, result.reason, result.verdict = (
                "cancelled",
                "The worker was stopped from the board",
                "released",
            )
        elif ended == "cancel":
            # The review already ended on the board, such as after another reviewer's rework
            # verdict: there is no verdict left to give.
            result.outcome, result.reason, result.verdict = "cancelled", self.cancel_reason, None
        elif ended == "timeout":
            self._review_failed(
                result,
                f"Timed out after {format_duration(self.options.timeout_seconds or 0)}",
            )
            result.outcome = "timed_out"
        else:
            return False
        return True

    def _finish_review(
        self,
        key: str,
        card: dict[str, Any],
        result: ReviewResult,
        started_at: datetime,
        started: float,
        repo: Path | None,
        path: Path | None,
    ) -> None:
        if result.verdict in {"approve", "rework", "needs_input"}:
            # The board's last word before the verdict: a stop or cancel still wins.
            self._set("working", step=STEP_HANDING_OVER)
            self._review_ended_early(self._interruption(None) or "exit", result)
        if self.cancel_reason is not None:
            if result.outcome != "cancelled":
                result.outcome, result.reason = "cancelled", self.cancel_reason
            result.verdict = None
        if self.stop_failed is not None:
            # The agent may still be running: the review stays held until its lease runs out.
            result.verdict = None
        verdict, note, head_sha = result.verdict, result.note, result.head_sha
        delivered: bool | BoardApiError | None = None
        if verdict is not None:
            delivered = self._deliver(
                f"Sending the review of {key}",
                lambda: self.client.submit_review(
                    str(card["id"]),
                    self.options.name,
                    verdict,
                    head_sha,
                    note,
                ),
            )
        if isinstance(delivered, BoardApiError) and (
            delivered.is_conflict or delivered.is_not_found
        ):
            # The review ended on the board meanwhile, or the branch moved on: nothing to
            # judge any more.
            result.outcome, result.reason, result.verdict = "cancelled", delivered.message, None
        elif isinstance(delivered, BoardApiError):
            # Refused for another reason: the review is still held, and ends as failed rather
            # than staying held while this worker's heartbeats keep it alive.
            reason = f"The board refused the verdict {verdict}: {delivered.message}"
            result.outcome, result.reason, result.verdict = "failed", reason, None
            if verdict != "failed":
                self._deliver(
                    f"Ending the review of {key}",
                    lambda: self.client.submit_review(
                        str(card["id"]),
                        self.options.name,
                        "failed",
                        head_sha,
                        reason,
                    ),
                )
        if verdict == "approve" and result.verdict is not None:
            self.summary.approved.append(key)
            self.say("success", f"Approved {key} at {(result.commit or '')[:12]}")
        elif verdict == "rework" and result.verdict is not None:
            self.summary.reworked.append(key)
            self.say("success", f"Sent {key} back for rework")
        else:
            self.summary.returned.append(key)
            self.say("warning", f"{key}: {result.reason or result.note}")
        if result.verdict is None:
            self.passed_over.append(self._task_id())
        self._report_review(key, result, started_at, started)
        if (
            path is not None
            and repo is not None
            and not self.options.keep_worktree
            and not result.keep_worktree
        ):
            try:
                worktrees.remove_worktree(repo, self._worktree_dir(repo), path)
            except worktrees.GitError as exc:
                self.say("warning", f"Could not remove the review worktree: {exc}")
        self.task = None
        self.cancel_reason = None
        self._set("idle")

    def _report_review(
        self,
        key: str,
        result: ReviewResult,
        started_at: datetime,
        started: float,
    ) -> None:
        verify = result.verify
        verdict = REVIEW_VERDICTS.get(str(result.verdict), "no verdict")
        commit = result.commit or result.head_sha
        at = f" at {commit[:12]}" if commit else ""
        lines = [f"Review of {result.branch or key}{at}: {verdict}"]
        if result.summary:
            lines += ["", result.summary]
        if result.verdict == "rework" and result.note:
            lines += ["", "Feedback:", result.note]
        if result.verdict == "needs_input" and result.note:
            lines += ["", f"Question: {result.note}"]
        report: dict[str, Any] = {
            # Not a field of the board's run reports yet; it ignores unknown fields.
            "kind": MODE_REVIEW,
            "outcome": result.outcome,
            "agent": self.options.agent,
            "workerId": self.worker_id,
            "summary": "\n".join(lines),
            "commits": [],
            "branchName": result.branch,
            "verifyCommand": verify.command if verify else None,
            "verifyExitCode": verify.exit_code if verify else None,
            "verifyOutput": verify.output if verify else None,
            "durationSeconds": max(0, int(self.clock() - started)),
            "failureReason": (
                None
                if result.outcome == "done"
                else f"Needs input: {result.reason}"
                if result.outcome == "needs_input"
                else result.reason
            ),
            "startedAt": _iso(started_at),
            "finishedAt": _iso(self.now()),
        }
        task_id = self._task_id()
        self._deliver(
            f"Adding the review report to {key}",
            lambda: self.client.add_run_report(task_id, report),
        )

    def _task_id(self) -> str:
        assert self.task is not None
        return str(self.task["id"])


# How a review's verdict reads in its run report.
REVIEW_VERDICTS = {
    "approve": "approved",
    "rework": "sent back for rework",
    "needs_input": "asked for input",
    "failed": "failed",
    "released": "given up",
}
# How much of a failed verify command's output goes into the rework feedback.
FEEDBACK_OUTPUT_CHARS = 4_000


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _verify_feedback(verify: VerifyResult, points: str = "") -> str:
    """Rework feedback for a failed verify command: the reviewer's points, then the end of
    the command's output, where test runners say what failed."""
    output = verify.output.strip()[-FEEDBACK_OUTPUT_CHARS:]
    lines = [points, ""] if points else []
    lines.append(f"The verify command `{verify.command}` failed with exit code {verify.exit_code}.")
    if output:
        lines += ["", "Its output ends with:", "", output]
    return "\n".join(lines)


def _transient(exc: BoardApiError) -> bool:
    """Unreachable, or failing on its side: worth trying again."""
    return exc.status is None or exc.status >= 500


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stops a verify command and everything it started: politely first, then for good,
    since test runners leave children behind that outlive their shell."""
    if sys.platform == "win32":
        # Ending the shell leaves what it started running: taskkill ends the whole tree.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=TERMINATE_GRACE_SECONDS,
                check=False,
            )
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
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

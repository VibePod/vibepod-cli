"""`vp board work` end to end: a fake agent in real git worktrees, against a fake board over
HTTP. No agent subscription and no container runtime are involved."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

import pytest
from board_fake import TOKEN, FakeBoard, FakeBoardServer
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import board as board_cmd
from vibepod.core import worktrees
from vibepod.core.board_client import BoardApiError, BoardClient, resolve_board_settings
from vibepod.core.board_worker import (
    AgentResult,
    AgentStopError,
    BoardWorker,
    FileProfileLock,
    RunnerError,
    WorkerError,
    WorkOptions,
    branch_name,
    build_prompt,
    build_review_prompt,
    parse_result,
    usage_limit_in,
    usage_limit_reset,
)

# --- helpers -------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=worktrees.git_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def python(code: str) -> str:
    """A verify command that runs the same in sh and in cmd.exe."""
    return f'"{sys.executable}" -c "{code}"'


# Verify commands for the shell of any platform.
CHECKS_THE_FEATURE = python("assert 'implemented' in open('feature.txt').read()")
PASSES = python("pass")
FAILS = python("raise SystemExit(1)")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "app"
    path.mkdir()
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("# App\n")
    git(path, "add", "README.md")
    git(path, "commit", "--quiet", "-m", "Initial commit")
    return path


class Clock:
    """Time that passes only when the worker sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


Behaviour = Callable[[Path, str], tuple[int, str]]


def result_block(status: str = "done", **fields: Any) -> str:
    """The structured result an agent ends its run with."""
    import json

    return f"<vibepod-result>\n{json.dumps({'status': status, **fields})}\n</vibepod-result>"


def commits_a_feature(path: Path, prompt: str) -> tuple[int, str]:
    (path / "feature.txt").write_text("implemented\n")
    git(path, "add", "feature.txt")
    git(path, "commit", "--quiet", "-m", "Add the feature")
    return 0, "Working...\n" + result_block(summary="Added feature.txt and committed it.")


@dataclass
class FakeRun:
    task_id: str
    behaviour: Behaviour
    workspace: Path
    prompt: str
    # How many polls the agent takes; None runs until stopped.
    polls: int | None = 2
    stopped: bool = False
    code: int | None = None
    output: str = ""
    polled: int = 0

    def poll(self) -> int | None:
        self.polled += 1
        if self.stopped:
            return self.code
        if self.polls is not None and self.polled >= self.polls:
            if self.code is None:
                self.code, self.output = self.behaviour(self.workspace, self.prompt)
            return self.code
        return None

    def stop(self) -> None:
        self.stopped = True
        self.code = 143

    def logs(self) -> str:
        return self.output


@dataclass
class FakeRunner:
    behaviour: Behaviour = commits_a_feature
    polls: int | None = 2
    runs: list[FakeRun] = field(default_factory=list)
    starts: list[dict[str, Any]] = field(default_factory=list)
    fail_start: bool = False

    def start(
        self,
        prompt: str,
        workspace: Path,
        *,
        mounts: list[tuple[str, str, str]],
        allow_check_path: Path,
    ) -> FakeRun:
        if self.fail_start:
            raise RunnerError("Docker is not running")
        self.starts.append(
            {"prompt": prompt, "workspace": workspace, "mounts": mounts, "allow": allow_check_path},
        )
        run = FakeRun(f"task{len(self.runs):012d}", self.behaviour, workspace, prompt, self.polls)
        self.runs.append(run)
        return run


Instructions = Callable[[], list[dict[str, Any]]]


def cancel(board: FakeBoard, task_id: str) -> Instructions:
    """Cancels the task's run on the board, which puts the task back to Planned."""

    def cancelled() -> list[dict[str, Any]]:
        board.cards[task_id].update(column="planned", assignee=None, claimedAt=None)
        return [{"type": "cancel", "taskId": task_id, "reason": "Run cancelled by admin"}]

    return cancelled


def once_the_agent_runs(instructions: Instructions) -> Callable[[dict[str, Any]], Any]:
    """Heartbeat replies that carry the instructions once the agent runs: from the second
    heartbeat of that step on, since the first is sent before the agent starts."""
    seen: list[dict[str, Any]] = []

    def on_heartbeat(beat: dict[str, Any]) -> list[dict[str, Any]]:
        if beat.get("step") != "agent_running":
            return []
        seen.append(beat)
        return instructions() if len(seen) > 1 else []

    return on_heartbeat


@pytest.fixture
def board() -> FakeBoard:
    return FakeBoard()


@pytest.fixture
def server(board: FakeBoard):
    with FakeBoardServer(board) as running:
        yield running


def work(
    server: FakeBoardServer,
    runner: FakeRunner,
    repo: Path | None,
    clock: Clock | None = None,
    allow_repo: Callable[[Path], bool] | None = None,
    **options: Any,
) -> tuple[BoardWorker, list[tuple[str, str]]]:
    messages: list[tuple[str, str]] = []
    clock = clock or Clock()
    worker = BoardWorker(
        BoardClient(server.url, TOKEN),
        runner,
        WorkOptions(
            project="VP",
            agent="claude",
            name="claude@laptop",
            machine="laptop",
            repo=repo,
            **options,
        ),
        say=lambda level, message: messages.append((level, message)),
        clock=clock,
        sleep=clock.sleep,
        allow_repo=allow_repo,
    )
    worker.run()
    return worker, messages


# --- the happy path ------------------------------------------------------------------


def test_hands_a_task_over_after_the_agent_committed_and_verify_passed(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task(
        "Add the feature",
        details="Build the feature described here.",
        acceptanceCriteria=["feature.txt exists", "It says implemented"],
        githubIssueNumber=12,
    )
    runner = FakeRunner()

    worker, _ = work(server, runner, repo, verify=CHECKS_THE_FEATURE)

    assert worker.summary.handed_over == ["VP-1"]
    card = board.card("VP-1")
    assert (card["column"], card["branchName"]) == ("review", "issue-12")
    [handover] = board.requests("POST", "/api/board/card-1/handover")
    assert handover == {
        "assignee": "claude@laptop",
        "branchName": "issue-12",
        "note": f"1 commit; verify passed ({CHECKS_THE_FEATURE})",
        "headSha": git(repo, "rev-parse", "issue-12"),
    }
    assert card["headSha"] == git(repo, "rev-parse", "issue-12")
    # The branch holds the work, the worktree is gone.
    assert git(repo, "log", "--format=%s", "main..issue-12") == "Add the feature"
    assert not (repo.parent / "app-worktrees" / "issue-12").exists()
    [report] = board.runs
    assert report["outcome"] == "done"
    assert report["commits"][0]["subject"] == "Add the feature"
    assert (report["verifyCommand"], report["verifyExitCode"]) == (
        CHECKS_THE_FEATURE,
        0,
    )
    assert report["branchName"] == "issue-12"
    assert report["summary"] == "Added feature.txt and committed it."
    assert report["workerId"] == "worker-1"
    assert "failureReason" not in report
    assert board.requests("POST", "/api/workers/worker-1/sign-off") == [None]


def test_the_prompt_carries_the_task_and_the_git_dir_is_mounted(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task(
        "Add the feature",
        details="Build the feature described here.",
        acceptanceCriteria=["feature.txt exists"],
    )
    runner = FakeRunner()

    work(server, runner, repo)

    [start] = runner.starts
    assert "Add the feature" in start["prompt"]
    assert "Build the feature described here." in start["prompt"]
    assert "- feature.txt exists" in start["prompt"]
    assert "`vp-1`" in start["prompt"]
    assert "<vibepod-result>" in start["prompt"]
    git_dir = (repo / ".git").resolve()
    worktree = (repo.parent / "app-worktrees" / "vp-1").resolve()
    admin = git_dir / "worktrees" / "vp-1"
    # The git directory is writable for commits; what makes git run programs, the
    # worktree's pointers into it, and the HEAD and index of the user's checkout are not.
    # On Windows, the container gets its own `.git` file naming the git directory's path there.
    pointer = admin / worktrees.CONTAINER_POINTER if sys.platform == "win32" else worktree / ".git"
    in_container = worktrees.container_path
    assert start["mounts"] == [
        (str(git_dir), in_container(git_dir), "rw"),
        (str(git_dir / "hooks"), in_container(git_dir / "hooks"), "ro"),
        (str(git_dir / "info"), in_container(git_dir / "info"), "ro"),
        (str(git_dir / "config"), in_container(git_dir / "config"), "ro"),
        (str(admin / "commondir"), in_container(admin / "commondir"), "ro"),
        (str(admin / "gitdir"), in_container(admin / "gitdir"), "ro"),
        (str(git_dir / "HEAD"), in_container(git_dir / "HEAD"), "ro"),
        (str(git_dir / "index"), in_container(git_dir / "index"), "ro"),
        (str(pointer), "/workspace/.git", "ro"),
    ]
    assert start["allow"] == repo.resolve()
    assert start["workspace"] == worktree


def test_reports_its_status_steps_and_task_in_heartbeats(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Add the feature")

    work(server, FakeRunner(), repo, verify=PASSES)

    steps = [beat.get("step") for beat in board.heartbeats if beat["status"] == "working"]
    # Long steps repeat in the regular heartbeats; the order is what counts.
    assert list(dict.fromkeys(steps)) == [
        "preparing_workspace",
        "agent_running",
        "verifying",
        "handing_over",
    ]
    assert {beat.get("task") for beat in board.heartbeats if beat["status"] == "working"} == {
        "idea-1",
    }
    assert board.heartbeats[-1]["status"] == "idle"
    assert all(beat["leaseSeconds"] == 600 for beat in board.heartbeats)


def test_commits_what_the_agent_left_uncommitted(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Leave changes")

    def writes_only(path: Path, prompt: str) -> tuple[int, str]:
        (path / "notes.txt").write_text("forgot to commit\n")
        return 0, result_block(summary="Wrote notes.txt.")

    work(server, FakeRunner(writes_only), repo)

    assert board.card("VP-1")["column"] == "review"
    assert git(repo, "show", "vp-1:notes.txt") == "forgot to commit"
    assert board.runs[0]["commits"][0]["subject"] == "Leave changes"


def test_the_worker_commit_is_not_signed(repo: Path, tmp_path: Path) -> None:
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", str(tmp_path / "no-such-gpg"))
    (repo / "notes.txt").write_text("left by the agent\n")

    sha = worktrees.commit_all(repo, "Leave changes")

    assert sha == git(repo, "rev-parse", "HEAD")
    assert git(repo, "show", "HEAD:notes.txt") == "left by the agent"


def test_changes_the_verify_command_made_are_committed_too(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Verify formats")

    work(
        server,
        FakeRunner(),
        repo,
        verify=python("open('feature.txt', 'w').write('formatted')"),
        once=True,
    )

    assert board.card("VP-1")["column"] == "review"
    assert git(repo, "show", "vp-1:feature.txt") == "formatted"
    subjects = [commit["subject"] for commit in board.runs[0]["commits"]]
    assert subjects == ["Add the feature", "Changes left by the verify command"]
    [handover] = board.requests("POST", "/api/board/card-1/handover")
    assert handover["note"].startswith("2 commits; verify passed")


def test_heartbeats_go_on_while_host_git_works(
    monkeypatch,
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    import contextlib

    board.add_task("Large repository")
    alive: list[bool] = []
    outside: list[str] = []
    keepalive = BoardWorker._keepalive

    @contextlib.contextmanager
    def tracked(self: BoardWorker):
        with keepalive(self):
            alive.append(True)
            try:
                yield
            finally:
                alive.pop()

    def checked(name: str) -> Callable[..., Any]:
        original = getattr(worktrees, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if not alive:
                outside.append(name)
            return original(*args, **kwargs)

        return call

    monkeypatch.setattr(BoardWorker, "_keepalive", tracked)
    for name in ("prepare_worktree", "branch_refs", "restore_refs", "commit_all"):
        monkeypatch.setattr(worktrees, name, checked(name))

    work(server, FakeRunner(), repo, verify=PASSES, once=True)

    assert board.card("VP-1")["column"] == "review"
    assert outside == []


def test_works_through_every_planned_task_then_exits(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("First", githubIssueNumber=1)
    board.add_task("Second", githubIssueNumber=2)
    board.add_task("Not planned", column="ready")

    worker, _ = work(server, FakeRunner(), repo)

    assert worker.summary.handed_over == ["VP-1", "VP-2"]
    assert worker.summary.ended_because == "No planned task left to claim"


def test_once_and_max_limit_the_number_of_tasks(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    for number in range(3):
        board.add_task(f"Task {number}")

    once, _ = work(server, FakeRunner(), repo, once=True)
    assert once.summary.handed_over == ["VP-1"]

    capped, _ = work(server, FakeRunner(), repo, max_tasks=1)
    assert capped.summary.handed_over == ["VP-2"]


def test_claims_only_labelled_tasks_and_a_named_task(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Unmarked")
    board.add_task("Marked", labels=["agent"])
    board.add_task("Named")

    labelled, _ = work(server, FakeRunner(), repo, labels=("agent",))
    assert labelled.summary.handed_over == ["VP-2"]
    assert board.requests("POST", "/api/board/claim")[0]["labels"] == ["agent"]

    named, _ = work(server, FakeRunner(), repo, task="VP-3")
    assert named.summary.handed_over == ["VP-3"]
    assert board.card("VP-1")["column"] == "planned"


def test_a_named_task_that_cannot_be_claimed_ends_with_an_error(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Already in review", column="review")

    with pytest.raises(WorkerError, match="VP-1 cannot be claimed"):
        work(server, FakeRunner(), repo, task="VP-1")
    # It still signed off.
    assert board.requests("POST", "/api/workers/worker-1/sign-off") == [None]


# --- failures ------------------------------------------------------------------------


def test_a_failed_verify_returns_the_task_to_planned_with_a_note(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Add the feature")

    worker, _ = work(
        server,
        FakeRunner(),
        repo,
        verify=python("print('2 tests failed'); raise SystemExit(1)"),
        max_attempts=5,
        once=True,
    )

    assert worker.summary.returned == ["VP-1"]
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release == {
        "assignee": "claude@laptop",
        "outcome": "failed",
        "note": "The verify command exited with code 1",
        "maxAttempts": 5,
    }
    [report] = board.runs
    assert (report["outcome"], report["verifyExitCode"]) == ("failed", 1)
    assert "2 tests failed" in report["verifyOutput"]
    assert report["failureReason"] == "The verify command exited with code 1"
    # A failed run keeps its worktree for a look, and the next attempt continues on it.
    assert (repo.parent / "app-worktrees" / "vp-1").is_dir()


def test_a_failing_task_is_retried_until_the_board_blocks_it(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Always fails")

    def fails(path: Path, prompt: str) -> tuple[int, str]:
        return 1, "Error: cannot do this"

    worker, _ = work(server, FakeRunner(fails), repo, max_attempts=2)

    assert [body["outcome"] for body in board.requests("POST", "/api/board/card-1/release")] == [
        "failed",
        "failed",
    ]
    assert board.card("VP-1")["blockedReason"] == "The agent exited with code 1"
    assert worker.summary.ended_because == "No planned task left to claim"


def test_failed_tasks_can_be_blocked_instead(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Add the feature")

    work(server, FakeRunner(), repo, verify=FAILS, on_fail="blocked", once=True)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert board.card("VP-1")["blockedReason"] == "The verify command exited with code 1"


def test_an_agent_that_fails_or_changes_nothing_fails_the_run(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Crashes")
    board.add_task("Does nothing")

    def crashes(path: Path, prompt: str) -> tuple[int, str]:
        return 2, "Traceback: boom"

    worker, _ = work(server, FakeRunner(crashes), repo, once=True)
    assert board.runs[-1]["failureReason"] == "The agent exited with code 2"

    def idles(path: Path, prompt: str) -> tuple[int, str]:
        return 0, result_block(summary="Nothing to do.")

    work(server, FakeRunner(idles), repo, task="VP-2")
    assert board.runs[-1]["failureReason"] == "The agent reported done but made no changes"
    assert [body["outcome"] for body in board.requests("POST", "/api/board/card")] == [
        "failed",
        "failed",
    ]


def test_a_timed_out_agent_is_stopped_and_the_task_returned(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Endless")
    runner = FakeRunner(polls=None)

    work(server, runner, repo, timeout_seconds=60, once=True)

    assert runner.runs[0].stopped is True
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert (release["outcome"], release["note"]) == ("failed", "Timed out after 1m")
    assert board.runs[0]["outcome"] == "timed_out"
    # Heartbeats kept the claim alive while the agent ran.
    assert sum(beat.get("step") == "agent_running" for beat in board.heartbeats) >= 4


@pytest.mark.parametrize("interrupted", [False, True])
def test_an_agent_that_cannot_be_stopped_keeps_its_claim(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    interrupted: bool,
) -> None:
    board.add_task("Unstoppable")

    class Unstoppable(FakeRunner):
        def start(self, prompt: str, workspace: Path, **kwargs: Any) -> FakeRun:
            run = super().start(prompt, workspace, **kwargs)
            polls = 0

            def poll() -> int | None:
                nonlocal polls
                polls += 1
                if interrupted and polls == 3:
                    raise KeyboardInterrupt
                return None

            def stop() -> None:
                raise AgentStopError("Task abc could not be stopped and may still be running")

            run.poll = poll  # type: ignore[method-assign]
            run.stop = stop  # type: ignore[method-assign]
            return run

    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            work(server, Unstoppable(polls=None), repo, timeout_seconds=60, once=True)
    else:
        worker, _ = work(server, Unstoppable(polls=None), repo, timeout_seconds=60, poll_seconds=30)
        assert worker.summary.ended_because.startswith("The agent could not be stopped")

    assert board.requests("POST", "/api/board/card-1/release") == []
    assert board.card("VP-1")["column"] == "in_progress"
    assert board.card("VP-1")["assignee"] == "claude@laptop"
    assert board.runs[0]["outcome"] == ("cancelled" if interrupted else "failed")


def test_a_task_without_a_repository_is_blocked(board: FakeBoard, server: FakeBoardServer) -> None:
    board.add_task("Nowhere to work")

    work(server, FakeRunner(), None)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"].startswith("No repository to work in")


def test_uses_the_repository_path_of_the_task(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Has a path", repositoryLocalPath=str(repo))

    worker, _ = work(server, FakeRunner(), None)

    assert worker.summary.handed_over == ["VP-1"]


def test_a_start_failure_gives_the_task_back_and_stops_the_worker(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("First")
    board.add_task("Second")

    worker, _ = work(server, FakeRunner(fail_start=True), repo)

    assert board.requests("POST", "/api/board/card-1/release")[0]["outcome"] == "released"
    assert board.card("VP-2")["column"] == "planned"
    assert worker.summary.ended_because.startswith("The agent could not be started")


# --- existing branches and worktrees -------------------------------------------------


def test_continues_on_an_existing_branch(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Resumed")
    git(repo, "branch", "vp-1")
    git(repo, "checkout", "--quiet", "vp-1")
    (repo / "earlier.txt").write_text("from an earlier run\n")
    git(repo, "add", "earlier.txt")
    git(repo, "commit", "--quiet", "-m", "Earlier work")
    git(repo, "checkout", "--quiet", "main")

    work(server, FakeRunner(), repo)

    assert board.card("VP-1")["column"] == "review"
    assert git(repo, "log", "--format=%s", "main..vp-1").splitlines() == [
        "Add the feature",
        "Earlier work",
    ]
    # Only this run's commits are reported.
    assert [commit["subject"] for commit in board.runs[0]["commits"]] == ["Add the feature"]


def test_refuses_an_existing_branch_when_asked(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Taken")
    git(repo, "branch", "vp-1")
    runner = FakeRunner()

    work(server, runner, repo, existing="refuse")

    assert runner.starts == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert "Branch vp-1 already exists" in release["note"]


def test_keeps_the_worktree_when_asked(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Keep it")

    work(server, FakeRunner(), repo, keep_worktree=True)

    assert (repo.parent / "app-worktrees" / "vp-1" / "feature.txt").is_file()


# --- instructions from the board -----------------------------------------------------


def test_a_run_cancelled_from_the_board_stops_without_giving_the_task_back(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Cancelled")

    board.on_heartbeat = once_the_agent_runs(cancel(board, "idea-1"))
    board.add_task("Next in line")

    class CancelledThenDone(FakeRunner):
        """The first run goes on until it is stopped; the next one finishes."""

        def start(self, prompt: str, workspace: Path, **kwargs: Any) -> FakeRun:
            run = super().start(prompt, workspace, **kwargs)
            run.polls = None if len(self.runs) == 1 else 2
            return run

    runner = CancelledThenDone()

    worker, _ = work(server, runner, repo, max_tasks=2)

    assert runner.runs[0].stopped is True
    assert board.requests("POST", "/api/board/card-1/release") == []
    assert board.requests("POST", "/api/board/card-1/handover") == []
    report = board.runs[0]
    assert (report["outcome"], report["failureReason"]) == ("cancelled", "Run cancelled by admin")
    assert worker.summary.returned == ["VP-1"]
    # VP-1 is planned again, but this worker passes over what it was told to stop.
    assert board.requests("POST", "/api/board/claim")[1]["exclude"] == ["idea-1"]
    assert worker.summary.handed_over == ["VP-2"]
    assert board.card("VP-1")["column"] == "planned"


def test_a_cancel_that_comes_while_preparing_never_starts_the_agent(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Cancelled before it started")

    def cancel_as_it_prepares(beat: dict[str, Any]) -> list[dict[str, Any]]:
        # The reply to the heartbeat sent once the worktree is ready, before the agent starts.
        return cancel(board, "idea-1")() if beat.get("step") == "agent_running" else []

    board.on_heartbeat = cancel_as_it_prepares
    runner = FakeRunner()

    worker, _ = work(server, runner, repo, once=True)

    assert runner.starts == []
    assert board.requests("POST", "/api/board/card-1/release") == []
    assert board.requests("POST", "/api/board/card-1/handover") == []
    report = board.runs[0]
    assert (report["outcome"], report["failureReason"]) == ("cancelled", "Run cancelled by admin")
    assert list(worker.passed_over) == ["idea-1"]


def test_a_cancelled_run_that_also_failed_is_not_given_back(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Cancelled and failing")

    board.on_heartbeat = once_the_agent_runs(cancel(board, "idea-1"))

    class DetachesWhenStopped(FakeRunner):
        """Leaves its branch on the way out, which the check after the run catches."""

        def start(self, prompt: str, workspace: Path, **kwargs: Any) -> FakeRun:
            run = super().start(prompt, workspace, **kwargs)
            stop = run.stop

            def detach_and_stop() -> None:
                git(workspace, "checkout", "--quiet", "--detach")
                stop()

            run.stop = detach_and_stop  # type: ignore[method-assign]
            return run

    worker, _ = work(server, DetachesWhenStopped(polls=None), repo, once=True)

    assert board.runs[0]["failureReason"].startswith("The agent left the branch")
    assert board.requests("POST", "/api/board/card-1/release") == []
    assert list(worker.passed_over) == ["idea-1"]


def test_an_unexpected_error_gives_the_task_back_before_ending_the_worker(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Breaks the worker")

    class BrokenRunner(FakeRunner):
        def start(self, prompt: str, workspace: Path, **kwargs: Any) -> FakeRun:
            raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        work(server, BrokenRunner(), repo)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert (release["outcome"], release["note"]) == ("released", "The worker failed: disk full")
    assert board.card("VP-1")["column"] == "planned"
    assert board.runs[0]["outcome"] == "failed"


def test_a_stop_from_the_board_ends_the_run_and_the_worker(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Stopped")
    board.add_task("Never started")

    board.on_heartbeat = once_the_agent_runs(lambda: [{"type": "stop"}])
    runner = FakeRunner(polls=None)

    worker, _ = work(server, runner, repo, poll_seconds=30)

    assert runner.runs[0].stopped is True
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "released"
    assert board.runs[0]["outcome"] == "cancelled"
    assert board.card("VP-2")["column"] == "planned"
    assert worker.summary.ended_because == "Stopped from the board"
    assert board.requests("POST", "/api/workers/worker-1/sign-off") == [None]


@pytest.mark.parametrize("step", ["agent_running", "handing_over"])
def test_a_stop_that_comes_as_the_agent_finishes_still_gives_the_task_back(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    step: str,
) -> None:
    board.add_task("Stopped as it finished")

    def stop_at(beat: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "stop"}] if beat.get("step") == step else []

    board.on_heartbeat = stop_at
    # The agent is done by the first look at it, right after the stop arrived.
    worker, _ = work(server, FakeRunner(polls=1), repo, poll_seconds=30)

    assert board.requests("POST", "/api/board/card-1/handover") == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "released"
    assert board.runs[0]["outcome"] == "cancelled"
    assert worker.summary.ended_because == "Stopped from the board"


def test_a_run_stops_before_its_unrenewed_claim_runs_out(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Runs while the board is unreachable")
    board.failures = {"POST /api/workers/worker-1/heartbeat": 10**6}
    runner = FakeRunner(polls=None)

    worker, _ = work(server, runner, repo, once=True, lease_seconds=120)

    assert runner.runs[0].stopped is True
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "released"
    assert release["note"].startswith("The claim could not be renewed for")
    assert board.requests("POST", "/api/board/card-1/handover") == []
    assert board.runs[0]["outcome"] == "failed"
    assert board.card("VP-1")["column"] == "planned"


def test_a_stop_that_comes_while_the_hand_over_is_retried_gives_the_task_back(
    monkeypatch,
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    from vibepod.core import board_worker

    monkeypatch.setattr(board_worker, "DELIVERY_ATTEMPTS", 8)
    board.add_task("Stopped while handing over")
    board.failures = {"POST /api/board/card-1/handover": 6}
    beats: list[dict[str, Any]] = []

    def stop_while_retrying(beat: dict[str, Any]) -> list[dict[str, Any]]:
        if beat.get("step") == "handing_over":
            beats.append(beat)
        # The first comes before the hand-over, the next ones between its tries.
        return [{"type": "stop"}] if len(beats) > 1 else []

    board.on_heartbeat = stop_while_retrying

    worker, _ = work(server, FakeRunner(), repo, poll_seconds=30)

    assert len(beats) > 1
    assert board.card("VP-1")["column"] == "planned"
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "released"
    assert board.runs[0]["outcome"] == "cancelled"
    assert worker.summary.handed_over == []
    assert worker.summary.ended_because == "Stopped from the board"


def test_a_kept_hand_over_becomes_a_release_when_the_worker_is_stopped(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Handed over late")
    board.failures = {"POST /api/board/card-1/handover": 5}

    def stop_once_kept(beat: dict[str, Any]) -> list[dict[str, Any]]:
        kept = len(board.requests("POST", "/api/board/card-1/handover")) == 5
        return [{"type": "stop"}] if kept and beat.get("status") == "idle" else []

    board.on_heartbeat = stop_once_kept

    work(server, FakeRunner(), repo, once=True)

    assert len(board.requests("POST", "/api/board/card-1/handover")) == 5
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert (release["outcome"], release["note"]) == (
        "released",
        "The worker was stopped from the board",
    )
    assert board.card("VP-1")["column"] == "planned"


def test_a_refused_hand_over_blocks_the_task_instead_of_leaving_it_claimed(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Refused")
    board.refused_branch = "vp-1"

    worker, _ = work(server, FakeRunner(), repo, once=True)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"] == "The board refused the hand-over: Invalid request body"
    assert board.card("VP-1")["column"] == "planned"
    assert board.card("VP-1")["blockedReason"] == release["note"]
    assert board.runs[0]["outcome"] == "failed"
    assert worker.summary.returned == ["VP-1"]
    # The work stays on its branch, in the worktree, for the look.
    assert git(repo, "show", "vp-1:feature.txt") == "implemented"


def test_a_paused_project_takes_no_new_task_until_resumed(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Waiting")
    board.paused = "Release freeze"
    clock = Clock()
    resume_at = clock.now + 120

    def resume_later(beat: dict[str, Any]) -> list[dict[str, Any]]:
        if clock.now >= resume_at:
            board.paused = None
        return []

    board.on_heartbeat = resume_later

    worker, messages = work(server, FakeRunner(), repo, clock, poll_seconds=30, max_tasks=1)

    paused = [beat for beat in board.heartbeats if beat["status"] == "paused"]
    assert paused and paused[0]["statusReason"] == "Release freeze"
    assert worker.summary.handed_over == ["VP-1"]
    assert ("info", "Automation resumed from the board") in messages


def test_without_polling_a_paused_project_ends_the_worker(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Waiting")
    board.paused = "Release freeze"

    worker, _ = work(server, FakeRunner(), repo)

    assert worker.summary.ended_because == "Automation is paused: Release freeze"
    assert board.requests("POST", "/api/board/claim") == []


def test_a_usage_limit_returns_the_task_and_pauses_the_worker(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Too expensive")
    board.add_task("Next")

    def hits_the_limit(path: Path, prompt: str) -> tuple[int, str]:
        return 1, "Working...\nClaude AI usage limit reached|1759140000\n"

    clock = Clock()
    worker, _ = work(server, FakeRunner(hits_the_limit), repo, clock, poll_seconds=60, max_tasks=1)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert (release["outcome"], release["note"]) == (
        "released",
        "The agent reached its usage limit",
    )
    assert board.runs[0]["outcome"] == "usage_limit"
    # It stopped at --max 1; without --max it would wait out the pause.
    assert board.card("VP-2")["column"] == "planned"
    paused = [beat for beat in board.heartbeats if beat["status"] == "paused"]
    assert paused == [] or paused[0]["statusReason"].startswith("Usage limit reached")
    assert worker.usage_pause_until is not None


def test_after_a_usage_limit_the_worker_waits_then_resumes(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Too expensive")
    board.add_task("Next")
    outcomes = iter([(1, "You've hit your usage limit. Try again later."), None])

    def limited_then_fine(path: Path, prompt: str) -> tuple[int, str]:
        scripted = next(outcomes)
        return scripted if scripted else commits_a_feature(path, prompt)

    clock = Clock()
    worker, _ = work(
        server,
        FakeRunner(limited_then_fine),
        repo,
        clock,
        poll_seconds=60,
        max_tasks=2,
        usage_limit_wait_seconds=600,
    )

    paused = [beat for beat in board.heartbeats if beat["status"] == "paused"]
    assert paused and paused[0]["statusReason"].startswith("Usage limit reached; resuming at")
    # The pause lasted about ten minutes of worker time.
    assert clock.now - 1000.0 >= 600
    assert worker.summary.handed_over == ["VP-1"]


def test_a_usage_limit_is_detected_when_the_agent_exits_cleanly_without_work(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Limited quietly")
    board.add_task("Mentions limits")

    def quietly_limited(path: Path, prompt: str) -> tuple[int, str]:
        return 0, "Claude AI usage limit reached|1759140000"

    worker, _ = work(server, FakeRunner(quietly_limited), repo, once=True)
    assert board.runs[0]["outcome"] == "usage_limit"
    assert board.requests("POST", "/api/board/card-1/release")[0]["outcome"] == "released"

    def works_on_limits(path: Path, prompt: str) -> tuple[int, str]:
        commits_a_feature(path, prompt)
        return 0, result_block(summary="Added a usage limit reached banner.")

    worker, _ = work(server, FakeRunner(works_on_limits), repo, task="VP-2")
    assert worker.summary.handed_over == ["VP-2"]


def test_detects_usage_limits_only_at_the_end_of_the_output() -> None:
    assert usage_limit_in("Claude AI usage limit reached|1759140000")
    assert usage_limit_in("ERROR: You've hit your usage limit. Upgrade to Pro")
    assert usage_limit_in("stream error: 429 Too Many Requests")
    assert not usage_limit_in("Implemented rate limiting in the API.\n" + "ok\n" * 50)
    assert usage_limit_reset("Claude AI usage limit reached|1759140000") == 1759140000.0
    assert usage_limit_reset("usage limit reached") is None


# --- what the agent must not do, and what the worker must not do to it ----------------


def test_an_interrupt_stops_the_agent_and_gives_the_task_back(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Interrupted")

    class InterruptedRun(FakeRun):
        def poll(self) -> int | None:
            if self.polled == 1 and not self.stopped:
                self.polled += 1
                raise KeyboardInterrupt
            return super().poll()

    class Runner(FakeRunner):
        def start(self, prompt: str, workspace: Path, **kwargs: Any) -> FakeRun:
            run = InterruptedRun("task-interrupted", self.behaviour, workspace, prompt, None)
            self.runs.append(run)
            return run

    runner = Runner()
    with pytest.raises(KeyboardInterrupt):
        work(server, runner, repo)

    assert runner.runs[0].stopped is True
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert (release["outcome"], release["note"]) == ("released", "The worker was interrupted")
    assert board.runs[0]["outcome"] == "cancelled"
    assert board.requests("POST", "/api/workers/worker-1/sign-off") == [None]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX only")
def test_stopping_a_verify_command_kills_its_whole_group(monkeypatch, tmp_path: Path) -> None:
    import time

    from vibepod.core import board_worker

    monkeypatch.setattr(board_worker, "TERMINATE_GRACE_SECONDS", 0.3)
    child_file = tmp_path / "child"
    process = subprocess.Popen(
        f"trap '' TERM; sleep 30 & echo $! > {child_file}; wait",
        shell=True,
        start_new_session=True,
    )
    for _ in range(100):
        if child_file.exists() and child_file.read_text().strip():
            break
        time.sleep(0.05)
    child = int(child_file.read_text())

    board_worker._terminate(process)

    assert process.poll() is not None
    time.sleep(0.2)
    state = Path(f"/proc/{child}/stat")
    assert not state.exists() or state.read_text().split()[2] == "Z"


def test_stopping_a_verify_command_on_windows_ends_its_whole_tree(monkeypatch) -> None:
    from vibepod.core import board_worker

    class Process:
        pid = 4242
        killed = False

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            return 1

    calls: list[list[str]] = []
    monkeypatch.setattr(board_worker.sys, "platform", "win32")
    monkeypatch.setattr(
        board_worker.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args),
    )
    process = Process()

    board_worker._terminate(process)  # type: ignore[arg-type]

    assert calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
    assert process.killed


def test_host_git_never_runs_repository_hooks(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    tmp_path: Path,
) -> None:
    board.add_task("Leaves changes behind")
    marker = tmp_path / "hook-ran"
    for hook in ("post-checkout", "post-commit", "pre-commit"):
        path = repo / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\necho {hook} >> {marker}\n")
        path.chmod(0o755)

    def writes_only(path: Path, prompt: str) -> tuple[int, str]:
        (path / "notes.txt").write_text("uncommitted\n")
        return 0, result_block(summary="Wrote notes.txt.")

    work(server, FakeRunner(writes_only), repo)

    assert board.card("VP-1")["column"] == "review"
    assert not marker.exists()


def test_a_worktree_pointing_elsewhere_is_left_alone(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Redirected")

    def redirects(path: Path, prompt: str) -> tuple[int, str]:
        # Git for Windows hides .git, and Windows refuses to overwrite a hidden file.
        (path / ".git").unlink()
        (path / ".git").write_text("gitdir: /tmp/somewhere-else\n")
        return 0, result_block(summary="Done.")

    work(server, FakeRunner(redirects), repo, once=True)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert "changed the worktree's git metadata" in release["note"]
    assert board.requests("POST", "/api/board/card-1/handover") == []


def test_other_branches_the_agent_moved_are_restored(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Overreaches")
    main_before = git(repo, "rev-parse", "main")

    def moves_main(path: Path, prompt: str) -> tuple[int, str]:
        commits_a_feature(path, prompt)
        git(path, "update-ref", "refs/heads/main", "HEAD")
        return 0, result_block(summary="Done.")

    work(server, FakeRunner(moves_main), repo, once=True)

    assert git(repo, "rev-parse", "main") == main_before
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"].startswith("The agent moved main; restored them")


def test_refs_others_move_during_the_run_are_left_alone(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Runs while others work")
    # Another task's branch, in a worktree of another worker, from an earlier run.
    other = repo.parent / "app-worktrees" / "vp-9"
    git(repo, "worktree", "add", "--quiet", "-b", "vp-9", str(other))

    def others_commit_meanwhile(path: Path, prompt: str) -> tuple[int, str]:
        (repo / "mine.txt").write_text("the user's own work\n")
        git(repo, "add", "mine.txt")
        git(repo, "commit", "--quiet", "-m", "The user commits on main")
        git(other, "commit", "--quiet", "--allow-empty", "-m", "The other worker's agent")
        return commits_a_feature(path, prompt)

    worker, _ = work(server, FakeRunner(others_commit_meanwhile), repo, once=True)

    assert worker.summary.handed_over == ["VP-1"]
    assert git(repo, "log", "-1", "--format=%s", "main") == "The user commits on main"
    assert git(repo, "log", "-1", "--format=%s", "vp-9") == "The other worker's agent"


@pytest.mark.parametrize("verify", [PASSES, FAILS], ids=["passing", "failing"])
def test_what_the_verify_command_moved_is_restored_too(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    verify: str,
) -> None:
    board.add_task("Verified by code that overreaches")
    main_before = git(repo, "rev-parse", "main")
    moves_main = "import subprocess; subprocess.run('git update-ref refs/heads/main HEAD'.split())"
    command = f"{python(moves_main)} && {verify}"

    work(server, FakeRunner(), repo, once=True, verify=command)

    assert git(repo, "rev-parse", "main") == main_before
    assert board.requests("POST", "/api/board/card-1/handover") == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"].startswith("The agent moved main; restored them")


def test_another_tasks_branch_the_agent_moved_is_restored(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Overreaches into another task")
    other = repo.parent / "app-worktrees" / "vp-9"
    git(repo, "worktree", "add", "--quiet", "-b", "vp-9", str(other))
    other_before = git(repo, "rev-parse", "vp-9")

    def moves_the_other_branch(path: Path, prompt: str) -> tuple[int, str]:
        commits_a_feature(path, prompt)
        git(path, "update-ref", "refs/heads/vp-9", "HEAD")
        return 0, result_block(summary="Done.")

    work(server, FakeRunner(moves_the_other_branch), repo, once=True)

    assert git(repo, "rev-parse", "vp-9") == other_before
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"].startswith("The agent moved vp-9; restored them")


def test_a_moved_branch_that_cannot_be_told_from_the_users_work_is_left_and_blocks(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Ambiguous")

    def moves_main_while_the_user_stages(path: Path, prompt: str) -> tuple[int, str]:
        commits_a_feature(path, prompt)
        git(path, "update-ref", "refs/heads/main", "HEAD")
        (repo / "staged.txt").write_text("staged by the user\n")
        git(repo, "add", "staged.txt")
        return 0, result_block(summary="Done.")

    work(server, FakeRunner(moves_main_while_the_user_stages), repo, once=True)

    assert git(repo, "log", "-1", "--format=%s", "main") == "Add the feature"
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"].startswith("main moved during the run, in a checkout with changes")


def test_an_agent_that_left_its_branch_blocks_the_task(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Wanders off")

    def detaches(path: Path, prompt: str) -> tuple[int, str]:
        commits_a_feature(path, prompt)
        git(path, "checkout", "--quiet", "--detach")
        return 0, result_block(summary="Done.")

    work(server, FakeRunner(detaches), repo, once=True)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert "left the branch vp-1 (now on a detached HEAD)" in release["note"]


def test_a_branch_checked_out_in_someone_elses_worktree_is_not_taken_over(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    tmp_path: Path,
) -> None:
    board.add_task("Mine, actually", githubIssueNumber=12)
    mine = tmp_path / "my-checkout"
    git(repo, "worktree", "add", "--quiet", "-b", "issue-12", str(mine))
    (mine / "wip.txt").write_text("work in progress\n")
    runner = FakeRunner()

    work(server, runner, repo, once=True)

    assert runner.starts == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert f"checked out in {mine.resolve()}" in release["note"]
    assert (mine / "wip.txt").read_text() == "work in progress\n"
    assert git(mine, "status", "--porcelain") == "?? wip.txt"


def test_a_repository_that_is_not_allowed_is_never_touched(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Somewhere private", repositoryLocalPath=str(repo))
    runner = FakeRunner()

    work(server, runner, None, allow_repo=lambda path: False, once=True)

    assert runner.starts == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert "is not allowed for agents: run `vp config allow-dir" in release["note"]
    assert not (repo.parent / "app-worktrees").exists()


def test_an_allowed_folder_inside_a_repository_does_not_allow_the_repository(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    (repo / "docs").mkdir()
    board.add_task("Inside a larger repository", repositoryLocalPath=str(repo / "docs"))
    runner = FakeRunner()
    allowed = (repo / "docs").resolve()

    work(server, runner, None, allow_repo=lambda path: path == allowed, once=True)

    assert runner.starts == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert f"Repository {repo.resolve()} is not allowed for agents" in release["note"]
    assert git(repo, "branch", "--list", "vp-1") == ""
    assert not (repo.parent / "app-worktrees").exists()


def test_a_continued_branch_that_already_holds_the_work_is_handed_over(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Done last time")
    git(repo, "checkout", "--quiet", "-b", "vp-1")
    (repo / "feature.txt").write_text("from the earlier run\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "--quiet", "-m", "Earlier work")
    git(repo, "checkout", "--quiet", "main")

    def nothing_left(path: Path, prompt: str) -> tuple[int, str]:
        return 0, result_block(summary="Everything was already done.")

    work(server, FakeRunner(nothing_left), repo, once=True)

    [handover] = board.requests("POST", "/api/board/card-1/handover")
    assert handover["note"] == "1 commit"
    assert board.runs[0]["commits"] == []


def test_board_writes_survive_a_board_restart(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Fails while the board restarts")
    board.add_task("Handed over after the restart")
    board.failures = {
        "POST /api/board/card-1/release": 2,
        "POST /api/board/card-2/handover": 5,
    }

    def first_fails(path: Path, prompt: str) -> tuple[int, str]:
        if "Fails while" in prompt:
            return 1, "Error"
        return commits_a_feature(path, prompt)

    work(server, FakeRunner(first_fails), repo, max_tasks=2, max_attempts=1)

    assert board.card("VP-1")["blockedReason"] == "The agent exited with code 1"
    assert len(board.requests("POST", "/api/board/card-1/release")) == 3
    # Kept after five tries and delivered on the next round, before signing off.
    assert board.card("VP-2")["column"] == "review"
    assert len(board.requests("POST", "/api/board/card-2/handover")) == 6


def test_claims_ride_out_a_board_restart_while_polling(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Waiting for the board")
    board.failures = {"POST /api/board/claim": 1}

    worker, messages = work(server, FakeRunner(), repo, poll_seconds=30, max_tasks=1)

    assert worker.summary.handed_over == ["VP-1"]
    assert ("warning", "Board restarting; retrying") in messages


def test_the_verify_command_never_sees_the_board_token(
    monkeypatch,
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    tmp_path: Path,
) -> None:
    board.add_task("Verified")
    monkeypatch.setenv("VP_BOARD_TOKEN", TOKEN)
    monkeypatch.setenv("COPIED_TOKEN", TOKEN)
    monkeypatch.setenv("HARMLESS", "kept")
    seen = tmp_path / "env.txt"

    dump = f"import os; open({str(seen)!r}, 'w').write(repr(dict(os.environ)))"

    work(server, FakeRunner(), repo, verify=python(dump))

    environment = seen.read_text()
    assert TOKEN not in environment
    assert "VP_BOARD_TOKEN" not in environment
    assert "'HARMLESS': 'kept'" in environment


def test_a_worker_passes_over_at_most_as_many_tasks_as_a_claim_takes() -> None:
    from vibepod.core.board_worker import PASS_OVER_LIMIT

    worker = BoardWorker(
        BoardClient("http://127.0.0.1:9", TOKEN),
        FakeRunner(),
        WorkOptions(project="VP", agent="claude", name="n"),
    )
    worker.passed_over.extend(f"idea-{number}" for number in range(PASS_OVER_LIMIT + 10))
    assert len(worker.passed_over) == PASS_OVER_LIMIT
    assert worker.passed_over[-1] == f"idea-{PASS_OVER_LIMIT + 9}"


# --- the structured result, questions and rework ------------------------------------


def test_a_run_without_a_readable_result_counts_as_failed(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Silent agent")

    def says_nothing(path: Path, prompt: str) -> tuple[int, str]:
        commits_a_feature(path, prompt)
        return 0, "I did it, trust me."

    work(server, FakeRunner(says_nothing), repo, once=True)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert (release["outcome"], release["note"]) == (
        "failed",
        "The run ended without a readable result",
    )
    assert board.runs[0]["outcome"] == "failed"


def test_an_agent_that_says_it_failed_gives_its_reason(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Impossible")

    def gives_up(path: Path, prompt: str) -> tuple[int, str]:
        return 0, result_block("failed", reason="The API it needs does not exist.")

    work(server, FakeRunner(gives_up), repo, once=True)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["note"] == "The agent could not finish: The API it needs does not exist."


def test_needs_input_blocks_the_task_with_the_agents_question(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Add a cache")
    question = "Should the cache live in Redis or in Postgres?"

    def asks(path: Path, prompt: str) -> tuple[int, str]:
        (path / "draft.txt").write_text("started\n")
        return 0, "Thinking...\n" + result_block(
            "needs_input",
            summary="Drafted the interface.",
            question=question,
        )

    worker, _ = work(server, FakeRunner(asks), repo)

    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release == {"assignee": "claude@laptop", "outcome": "needs_input", "note": question}
    assert board.card("VP-1")["question"] == question
    [report] = board.runs
    assert (report["outcome"], report["failureReason"], report["summary"]) == (
        "needs_input",
        f"Needs input: {question}",
        "Drafted the interface.",
    )
    # The draft is kept on the branch for the run that gets the answer.
    assert git(repo, "show", "vp-1:draft.txt") == "started"
    assert worker.summary.ended_because == "No planned task left to claim"


def test_the_next_run_gets_the_question_and_its_answer(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Add a cache")
    board.history["idea-1"] = [
        {"kind": "claimed", "message": "Claimed by claude@laptop (attempt 2)"},
        {"kind": "answer", "actor": "admin", "message": "Postgres, no new services."},
        {"kind": "question", "actor": "claude@laptop", "message": "Redis or Postgres?"},
        {"kind": "claimed", "message": "Claimed by claude@laptop"},
    ]
    runner = FakeRunner()

    work(server, runner, repo)

    prompt = runner.starts[0]["prompt"]
    assert "## Earlier questions, answers and review feedback" in prompt
    assert prompt.index("Your question: Redis or Postgres?") < prompt.index(
        "Answer from admin: Postgres, no new services.",
    )
    assert "Claimed by" not in prompt
    assert board.card("VP-1")["column"] == "review"


def test_a_rework_continues_on_its_branch_with_the_feedback(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Add a cache", githubIssueNumber=99)
    board.card("VP-1")["branchName"] = "issue-7"
    board.history["idea-1"] = [
        {"kind": "feedback", "actor": "admin", "message": "Invalidate the cache on delete."},
    ]
    git(repo, "checkout", "--quiet", "-b", "issue-7")
    (repo / "cache.txt").write_text("first version\n")
    git(repo, "add", "cache.txt")
    git(repo, "commit", "--quiet", "-m", "Add the cache")
    git(repo, "checkout", "--quiet", "main")
    runner = FakeRunner()

    # Even --existing refuse continues a rework on its branch.
    work(server, runner, repo, existing="refuse")

    prompt = runner.starts[0]["prompt"]
    assert "This task was reviewed and sent back" in prompt
    assert "Review feedback from admin: Invalidate the cache on delete." in prompt
    assert "`issue-7`" in prompt
    card = board.card("VP-1")
    assert (card["column"], card["branchName"]) == ("review", "issue-7")
    assert git(repo, "log", "--format=%s", "main..issue-7").splitlines() == [
        "Add the feature",
        "Add the cache",
    ]
    assert [commit["subject"] for commit in board.runs[0]["commits"]] == ["Add the feature"]


def test_an_answered_question_continues_on_its_draft_even_when_refusing_branches(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Add a cache")
    board.history["idea-1"] = [
        {"kind": "answer", "actor": "admin", "message": "Postgres."},
        {"kind": "question", "actor": "claude@laptop", "message": "Redis or Postgres?"},
    ]
    git(repo, "checkout", "--quiet", "-b", "vp-1")
    (repo / "draft.txt").write_text("started\n")
    git(repo, "add", "draft.txt")
    git(repo, "commit", "--quiet", "-m", "Draft")
    git(repo, "checkout", "--quiet", "main")

    work(server, FakeRunner(), repo, existing="refuse")

    assert board.card("VP-1")["column"] == "review"
    assert git(repo, "log", "--format=%s", "main..vp-1").splitlines() == [
        "Add the feature",
        "Draft",
    ]


def test_a_rework_without_its_branch_is_blocked(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Handed over elsewhere")
    board.card("VP-1")["branchName"] = "issue-7"
    runner = FakeRunner()

    work(server, runner, repo, once=True)

    assert runner.starts == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"].startswith("The rework needs the branch issue-7")


def test_reads_the_last_well_formed_result() -> None:
    echoed = build_prompt({"key": "VP-1", "title": "Echoed"}, "vp-1")
    assert parse_result(echoed) is None
    fenced = (
        "<vibepod-result>\n```json\n"
        + '{"status": "done", "summary": "Fine"}'
        + "\n```\n</vibepod-result>"
    )
    assert parse_result(echoed + fenced) == AgentResult("done", summary="Fine")
    assert parse_result(fenced + result_block("failed", reason="Later")).status == "failed"
    assert parse_result(result_block("needs_input")) is None
    assert parse_result("<vibepod-result>{not json}</vibepod-result>") is None
    assert parse_result(result_block("maybe")) is None
    mentioned = "I will finish with a <vibepod-result> block as asked.\n" + result_block(
        summary="Real one",
    )
    assert parse_result(mentioned) == AgentResult("done", summary="Real one")


# --- review mode ---------------------------------------------------------------------

REVIEWER = "claude-review@laptop"


def handed_over(board: FakeBoard, repo: Path, title: str = "Add the feature", **fields: Any) -> str:
    """A task in Review: its branch holds the work, and the card names the commit handed
    over. Returns that commit."""
    task = board.add_task(title, **fields)
    branch = f"issue-{task['taskNumber'] + 6}"
    git(repo, "checkout", "--quiet", "-b", branch)
    (repo / f"feature-{task['taskNumber']}.txt").write_text("implemented\n")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", f"Implement {title}")
    git(repo, "checkout", "--quiet", "main")
    sha = git(repo, "rev-parse", branch)
    board.put_in_review(task["key"], branch, sha)
    return sha


def says(status: str, **fields: Any) -> Behaviour:
    """A reviewing agent that only looks, then gives its verdict."""

    def behaviour(path: Path, prompt: str) -> tuple[int, str]:
        git(path, "log", "--oneline")
        return 0, "Reviewing...\n" + result_block(status, **fields)

    return behaviour


def review(
    server: FakeBoardServer,
    runner: FakeRunner,
    repo: Path | None,
    name: str = REVIEWER,
    **options: Any,
) -> tuple[BoardWorker, list[tuple[str, str]]]:
    messages: list[tuple[str, str]] = []
    clock = options.pop("clock", None) or Clock()
    worker = BoardWorker(
        BoardClient(server.url, TOKEN),
        runner,
        WorkOptions(
            project="VP",
            agent="claude",
            name=name,
            machine="laptop",
            repo=repo,
            mode="review",
            **options,
        ),
        say=lambda level, message: messages.append((level, message)),
        clock=clock,
        sleep=clock.sleep,
    )
    worker.run()
    return worker, messages


def review_path(repo: Path, task: str = "vp-1", name: str = REVIEWER) -> Path:
    return repo.parent / "app-worktrees" / worktrees.worktree_folder(f"review-{task}-{name}")


def test_a_review_judges_the_handed_over_commit_in_a_detached_worktree(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    sha = handed_over(
        board,
        repo,
        details="Build the feature described here.",
        acceptanceCriteria=["feature.txt exists"],
    )
    branches = worktrees.branch_refs(repo)
    seen: dict[str, Any] = {}

    def looks(path: Path, prompt: str) -> tuple[int, str]:
        seen["head"] = git(path, "rev-parse", "HEAD")
        seen["branch"] = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "HEAD"],
            cwd=path,
            capture_output=True,
        ).returncode
        return 0, result_block("approve", summary="Meets the criteria.")

    runner = FakeRunner(looks)

    worker, _ = review(server, runner, repo)

    claim = board.requests("POST", "/api/board/claim")[0]
    assert (claim["mode"], claim["assignee"]) == ("review", REVIEWER)
    [registration] = [body for _, path, body in board.calls if path == "/api/workers"]
    assert registration is not None and registration["mode"] == "review"
    # The commit under review, detached, in a worktree of the reviewer's own.
    start = runner.starts[0]
    assert start["workspace"] == review_path(repo)
    assert (seen["head"], seen["branch"]) == (sha, 1)
    # The agent gets all of the git directory read-only: it has nothing to commit.
    git_dir = (repo / ".git").resolve()
    assert (str(git_dir), str(git_dir), "ro") in start["mounts"]
    prompt = start["prompt"]
    assert "Review the work on task VP-1" in prompt
    assert "Build the feature described here." in prompt
    assert "- feature.txt exists" in prompt
    main = git(repo, "rev-parse", "main")
    assert f"git diff {main}...HEAD" in prompt
    assert f"`{sha}` of the branch `issue-7`" in prompt
    assert '"status": "<approve | rework | needs_input | failed>"' in prompt
    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict == {
        "assignee": REVIEWER,
        "verdict": "approve",
        "headSha": sha,
        "note": "Meets the criteria.",
    }
    assert board.card("VP-1")["column"] == "pr_ready"
    assert board.approvers("VP-1") == [REVIEWER]
    [report] = board.runs
    assert report["kind"] == "review"
    assert report["outcome"] == "done"
    assert report["summary"] == (
        f"Review of issue-7 at {sha[:12]}: approved\n\nMeets the criteria."
    )
    assert report["branchName"] == "issue-7"
    assert report["commits"] == []
    assert "failureReason" not in report
    # The worktree is gone, and no branch was made or moved.
    assert not review_path(repo).exists()
    assert worktrees.branch_refs(repo) == branches
    assert worker.summary.approved == ["VP-1"]
    assert worker.summary.ended_because == "No task in review left to claim"


def test_a_rework_verdict_sends_the_feedback_and_the_task_back(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    sha = handed_over(board, repo)
    runner = FakeRunner(
        says(
            "rework",
            summary="The tests are missing.",
            feedback=["Add a test for the empty input.", "- Handle a missing file."],
        ),
    )

    worker, _ = review(server, runner, repo)

    feedback = "- Add a test for the empty input.\n- Handle a missing file."
    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert (verdict["verdict"], verdict["headSha"], verdict["note"]) == ("rework", sha, feedback)
    card = board.card("VP-1")
    assert (card["column"], card["branchName"], card["reviewRounds"]) == ("planned", "issue-7", 1)
    assert board.history["idea-1"][0] == {
        "kind": "feedback",
        "message": feedback,
        "actor": REVIEWER,
    }
    assert board.runs[0]["summary"].endswith(f"The tests are missing.\n\nFeedback:\n{feedback}")
    assert worker.summary.reworked == ["VP-1"]


def test_reviewers_with_different_names_each_approve_in_their_own_worktree(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.required_approvals = 2
    sha = handed_over(board, repo)

    first, _ = review(
        server,
        FakeRunner(says("approve", summary="Good.")),
        repo,
        keep_worktree=True,
    )
    assert board.card("VP-1")["column"] == "review"
    # The same reviewer does not get the same commit again.
    again, _ = review(server, FakeRunner(), repo)
    assert again.summary.approved == []
    second, _ = review(
        server,
        FakeRunner(says("approve", summary="Fine by me.")),
        repo,
        name="codex-review-2",
        keep_worktree=True,
    )

    assert first.summary.approved == second.summary.approved == ["VP-1"]
    assert board.card("VP-1")["column"] == "pr_ready"
    state = BoardClient(server.url, TOKEN).review_state("card-1")
    assert (state["approvals"], state["requiredApprovals"]) == (2, 2)
    assert state["approvedBy"] == [REVIEWER, "codex-review-2"]
    for name in (REVIEWER, "codex-review-2"):
        assert git(review_path(repo, name=name), "rev-parse", "HEAD") == sha


def test_a_failing_verify_is_always_a_rework_with_its_output(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)
    failing = python("print('2 tests failed'); raise SystemExit(1)")

    review(server, FakeRunner(says("approve", summary="Looks fine.")), repo, verify=failing)

    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict["verdict"] == "rework"
    assert f"The verify command `{failing}` failed with exit code 1." in verdict["note"]
    assert verdict["note"].endswith("2 tests failed")
    assert board.card("VP-1")["column"] == "planned"
    report = board.runs[0]
    assert (report["verifyCommand"], report["verifyExitCode"]) == (failing, 1)
    assert "2 tests failed" in report["verifyOutput"]


def test_a_passing_verify_leaves_the_verdict_to_the_agent(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)

    review(server, FakeRunner(says("approve", summary="Good.")), repo, verify=PASSES)

    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict["verdict"] == "approve"
    assert board.runs[0]["verifyExitCode"] == 0


@pytest.mark.parametrize(
    ("doing", "said"),
    [
        ("commits", "committed"),
        ("writes", "left uncommitted changes"),
        ("switches", "switched to the branch issue-7 and committed"),
    ],
)
def test_a_review_that_changes_the_repository_is_thrown_away_and_fails(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    doing: str,
    said: str,
) -> None:
    sha = handed_over(board, repo)

    def meddles(path: Path, prompt: str) -> tuple[int, str]:
        if doing == "switches":
            git(path, "checkout", "--quiet", "issue-7")
        (path / "fix.txt").write_text("fixed\n")
        if doing != "writes":
            git(path, "add", "fix.txt")
            git(path, "commit", "--quiet", "-m", "Fix it myself")
        return 0, result_block("approve", summary="Fixed and approved.")

    review(server, FakeRunner(meddles), repo, keep_worktree=True)

    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict["verdict"] == "failed"
    assert verdict["note"].startswith(f"The agent {said} in the review worktree")
    assert board.card("VP-1")["column"] == "review"
    assert git(repo, "rev-parse", "issue-7") == sha
    # Kept on request, but back at the reviewed commit without the changes.
    path = review_path(repo)
    assert git(path, "rev-parse", "HEAD") == sha
    assert git(path, "status", "--porcelain") == ""
    assert board.runs[0]["outcome"] == "failed"


def test_a_review_that_moves_other_refs_has_them_restored_and_fails(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    sha = handed_over(board, repo)
    main = git(repo, "rev-parse", "main")

    def moves_refs(path: Path, prompt: str) -> tuple[int, str]:
        git(path, "update-ref", "refs/heads/issue-7", main)
        return 0, result_block("approve", summary="Approved.")

    review(server, FakeRunner(moves_refs), repo)

    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict["verdict"] == "failed"
    assert verdict["note"] == "The agent moved issue-7; restored them"
    assert git(repo, "rev-parse", "issue-7") == sha


def test_a_verify_command_that_commits_or_redirects_git_fails_the_review(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    sha = handed_over(board, repo)
    handed_over(board, repo, "Second")
    commits = python(
        "import subprocess as s; open('cache.txt', 'w').write('x');"
        " s.run(['git', 'checkout', '-q', 'issue-7']);"
        " s.run(['git', 'commit', '-qam', 'Sneaky', '--allow-empty'])",
    )
    redirects = python("import os; os.remove('.git'); open('.git', 'w').write('gitdir: /x')")

    review(server, FakeRunner(says("approve", summary="Good.")), repo, verify=commits, once=True)
    review(server, FakeRunner(says("approve", summary="Good.")), repo, verify=redirects, once=True)

    [first] = board.requests("POST", "/api/board/card-1/review")
    assert first["verdict"] == "failed"
    assert first["note"].startswith(
        "The verify command switched to the branch issue-7 and committed in the review worktree",
    )
    assert git(repo, "rev-parse", "issue-7") == sha
    [second] = board.requests("POST", "/api/board/card-2/review")
    assert second["verdict"] == "failed"
    assert "changed the worktree's git metadata" in second["note"]
    # Kept for a look: git does not run in it again.
    assert review_path(repo, "vp-2").exists()


def test_needs_input_blocks_the_task_in_review_with_the_question(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)
    question = "Should an empty file count as implemented?"

    review(server, FakeRunner(says("needs_input", question=question)), repo)

    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert (verdict["verdict"], verdict["note"]) == ("needs_input", question)
    card = board.card("VP-1")
    assert (card["column"], card["question"]) == ("review", question)
    report = board.runs[0]
    assert (report["outcome"], report["failureReason"]) == (
        "needs_input",
        f"Needs input: {question}",
    )


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ("Looks fine to me.", "The run ended without a readable result"),
        (result_block("rework", summary="Needs work."), "The run ended without a readable result"),
        (result_block("done", summary="Done."), "The run ended without a readable result"),
        (
            result_block("failed", reason="Cannot build it."),
            "The agent could not review: Cannot build it.",
        ),
    ],
)
def test_a_review_without_a_verdict_fails(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    output: str,
    reason: str,
) -> None:
    sha = handed_over(board, repo)

    review(server, FakeRunner(lambda path, prompt: (0, output)), repo)

    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict == {"assignee": REVIEWER, "verdict": "failed", "headSha": sha, "note": reason}
    assert board.card("VP-1")["column"] == "review"
    assert board.runs[0]["failureReason"] == reason


@pytest.mark.parametrize("missing", ["branch", "commit"])
def test_a_review_of_a_missing_branch_or_commit_is_blocked(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    missing: str,
) -> None:
    handed_over(board, repo)
    if missing == "branch":
        board.card("VP-1")["branchName"] = "issue-99"
        expected = f"The branch issue-99 to review is not in {repo.resolve()}"
    else:
        board.card("VP-1")["headSha"] = "0123456789abcdef0123456789abcdef01234567"
        expected = f"The commit 0123456789ab of issue-7 to review is not in {repo.resolve()}"
    runner = FakeRunner()

    review(server, runner, repo)

    assert runner.starts == []
    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert (verdict["verdict"], verdict["note"]) == (
        "needs_input",
        f"The review cannot run: {expected}",
    )
    assert board.card("VP-1")["blockedReason"] == f"Needs input: The review cannot run: {expected}"
    assert board.runs[0]["failureReason"] == expected


def test_a_review_whose_task_was_handed_over_again_ends_quietly(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)

    def handed_over_meanwhile(path: Path, prompt: str) -> tuple[int, str]:
        board.card("VP-1")["headSha"] = "f" * 40
        return 0, result_block("approve", summary="Good.")

    worker, messages = review(server, FakeRunner(handed_over_meanwhile), repo)

    assert len(board.requests("POST", "/api/board/card-1/review")) == 1
    assert board.approvers("VP-1") == []
    report = board.runs[0]
    assert report["outcome"] == "cancelled"
    assert "was handed over again" in report["failureReason"]
    assert worker.summary.approved == []
    assert not [message for level, message in messages if level == "error"]


def test_a_refused_verdict_ends_the_review_as_failed(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    sha = handed_over(board, repo)
    board.refused_verdict = "approve"

    worker, _ = review(server, FakeRunner(says("approve", summary="Good.")), repo)

    approve, failed = board.requests("POST", "/api/board/card-1/review")
    assert approve is not None and approve["verdict"] == "approve"
    reason = "The board refused the verdict approve: Invalid request body"
    assert failed == {"assignee": REVIEWER, "verdict": "failed", "headSha": sha, "note": reason}
    assert board.runs[0]["failureReason"] == reason
    assert board.reviews[0]["verdict"] == "failed"
    assert worker.summary.approved == []


def test_another_reviewers_rework_cancels_the_review_without_a_verdict(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.required_approvals = 2
    sha = handed_over(board, repo)

    def other_reviewer_sends_it_back() -> list[dict[str, Any]]:
        if board.card("VP-1")["column"] == "review":
            board._claim_review({"assignee": "codex-review", "mode": "review"})
            board._submit_review(
                "card-1",
                {"assignee": "codex-review", "verdict": "rework", "headSha": sha, "note": "No."},
            )
        return []

    board.on_heartbeat = once_the_agent_runs(other_reviewer_sends_it_back)
    runner = FakeRunner(polls=None)

    worker, _ = review(server, runner, repo)

    assert runner.runs[0].stopped is True
    assert board.requests("POST", "/api/board/card-1/review") == []
    assert [(r["reviewer"], r.get("verdict")) for r in board.reviews] == [
        (REVIEWER, None),
        ("codex-review", "rework"),
    ]
    report = board.runs[0]
    assert (report["outcome"], report["failureReason"]) == ("cancelled", "No.")
    assert board.card("VP-1")["column"] == "planned"
    assert worker.passed_over[-1] == "idea-1"
    assert not review_path(repo).exists()


def test_a_review_cancelled_from_the_board_ends_without_a_verdict(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)

    def cancelled() -> list[dict[str, Any]]:
        if board.reviews[0]["open"]:
            board._cancel_review("card-1", "review-1", {"reason": "Not now"})
        return []

    board.on_heartbeat = once_the_agent_runs(cancelled)
    runner = FakeRunner(polls=None)

    review(server, runner, repo)

    assert runner.runs[0].stopped is True
    assert board.requests("POST", "/api/board/card-1/review") == []
    assert board.runs[0]["outcome"] == "cancelled"
    assert board.card("VP-1")["column"] == "review"


def test_a_stop_gives_the_review_up_and_ends_the_worker(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)
    board.on_heartbeat = once_the_agent_runs(lambda: [{"type": "stop"}])
    runner = FakeRunner(polls=None)

    worker, _ = review(server, runner, repo, poll_seconds=30)

    assert runner.runs[0].stopped is True
    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict["verdict"] == "released"
    assert board.runs[0]["outcome"] == "cancelled"
    assert worker.summary.ended_because == "Stopped from the board"


def test_a_review_cancelled_while_preparing_never_starts_the_agent(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)

    def cancel_while_preparing(beat: dict[str, Any]) -> list[dict[str, Any]]:
        if beat.get("step") == "preparing_workspace" and board.reviews[0]["open"]:
            board._cancel_review("card-1", "review-1", {"reason": "Not now"})
        return []

    board.on_heartbeat = cancel_while_preparing
    runner = FakeRunner()

    review(server, runner, repo)

    assert runner.starts == []
    assert board.requests("POST", "/api/board/card-1/review") == []
    assert board.runs[0]["outcome"] == "cancelled"
    assert not review_path(repo).exists()


def test_a_review_stops_before_its_unrenewed_lease_runs_out(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    sha = handed_over(board, repo)
    board.failures = {"POST /api/workers/worker-1/heartbeat": 10**6}
    runner = FakeRunner(polls=None)

    review(server, runner, repo, once=True, lease_seconds=120)

    assert runner.runs[0].stopped is True
    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert (verdict["verdict"], verdict["headSha"]) == ("released", sha)
    assert verdict["note"].startswith("The review could not be renewed for")
    assert board.runs[0]["outcome"] == "failed"
    assert board.card("VP-1")["column"] == "review"


def test_a_stop_that_comes_while_the_verdict_is_retried_gives_the_review_up(
    monkeypatch,
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    from vibepod.core import board_worker

    monkeypatch.setattr(board_worker, "DELIVERY_ATTEMPTS", 8)
    sha = handed_over(board, repo)
    board.failures = {"POST /api/board/card-1/review": 6}
    beats: list[dict[str, Any]] = []

    def stop_while_retrying(beat: dict[str, Any]) -> list[dict[str, Any]]:
        if beat.get("step") == "handing_over":
            beats.append(beat)
        return [{"type": "stop"}] if len(beats) > 1 else []

    board.on_heartbeat = stop_while_retrying

    worker, _ = review(server, FakeRunner(says("approve", summary="Good.")), repo, poll_seconds=30)

    sent = [body["verdict"] for body in board.requests("POST", "/api/board/card-1/review")]
    assert sent[0] == "approve" and set(sent[1:]) <= {"approve", "released"}
    assert sent[-1] == "released"
    assert board.reviews[0]["verdict"] == "released"
    assert board.approvers("VP-1") == []
    assert board.runs[0]["outcome"] == "cancelled"
    assert worker.summary.approved == []
    assert worker.summary.ended_because == "Stopped from the board"
    assert board.requests("POST", "/api/board/card-1/review")[-1] == {
        "assignee": REVIEWER,
        "verdict": "released",
        "headSha": sha,
        "note": "The worker was stopped from the board",
    }


def test_a_kept_verdict_becomes_a_release_when_the_worker_is_stopped(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)
    board.failures = {"POST /api/board/card-1/review": 5}

    def stop_once_kept(beat: dict[str, Any]) -> list[dict[str, Any]]:
        kept = len(board.requests("POST", "/api/board/card-1/review")) == 5
        return [{"type": "stop"}] if kept and beat.get("status") == "idle" else []

    board.on_heartbeat = stop_once_kept

    review(server, FakeRunner(says("approve", summary="Good.")), repo, once=True)

    verdicts = [body["verdict"] for body in board.requests("POST", "/api/board/card-1/review")]
    assert verdicts == ["approve"] * 5 + ["released"]
    assert board.reviews[0]["verdict"] == "released"
    assert board.card("VP-1")["column"] == "review"


def test_a_timed_out_review_fails(board: FakeBoard, server: FakeBoardServer, repo: Path) -> None:
    handed_over(board, repo)
    runner = FakeRunner(polls=None)

    review(server, runner, repo, timeout_seconds=60)

    assert runner.runs[0].stopped is True
    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert (verdict["verdict"], verdict["note"]) == ("failed", "Timed out after 1m")
    assert board.runs[0]["outcome"] == "timed_out"


def test_a_usage_limit_gives_the_review_up_and_pauses(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)

    def limited(path: Path, prompt: str) -> tuple[int, str]:
        return 1, "Claude AI usage limit reached|1759140000"

    worker, _ = review(server, FakeRunner(limited), repo)

    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert verdict["verdict"] == "released"
    assert board.runs[0]["outcome"] == "usage_limit"
    assert worker.usage_pause_until is not None
    assert board.card("VP-1")["column"] == "review"


def test_the_review_prompt_carries_the_task_history(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    handed_over(board, repo)
    board.history["idea-1"] = [
        {"kind": "review_started", "message": "Review by claude-review@laptop started"},
        {"kind": "handed_over", "message": "Handed over on issue-7"},
        {"kind": "feedback", "actor": "codex-review", "message": "Handle a missing file."},
        {
            "kind": "rework_requested",
            "actor": "codex-review",
            "message": "Rework requested by codex-review",
        },
        {"kind": "answer", "actor": "admin", "message": "Plain text."},
        {"kind": "question", "actor": "claude@laptop", "message": "Which format?"},
    ]
    runner = FakeRunner(says("approve", summary="Good."))

    review(server, runner, repo)

    prompt = runner.starts[0]["prompt"]
    lines = [
        "- Question from claude@laptop: Which format?",
        "- Answer from admin: Plain text.",
        "- Review: Rework requested by codex-review",
        "- Review feedback from codex-review: Handle a missing file.",
    ]
    assert "## Task history" in prompt
    assert [prompt.index(line) for line in lines] == sorted(prompt.index(line) for line in lines)
    assert "Handed over on" not in prompt


def test_a_board_without_reviews_gets_its_implementation_claim_back(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.reviews_supported = False
    board.add_task("Planned")
    runner = FakeRunner()

    with pytest.raises(WorkerError, match="does not support review workers"):
        review(server, runner, repo)

    assert runner.starts == []
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "released"
    assert board.card("VP-1")["column"] == "planned"


def test_reads_a_reviewers_verdict() -> None:
    reviews = ("approve", "rework", "needs_input", "failed")
    prompt = build_review_prompt(
        {"key": "VP-1", "title": "Echoed"},
        "issue-7",
        "abc",
        "main",
        "def",
    )
    assert parse_result(prompt, reviews) is None
    assert parse_result(result_block("approve"), reviews) == AgentResult("approve")
    assert parse_result(result_block("approve")) is None
    assert parse_result(result_block("rework", summary="Bad."), reviews) is None
    assert parse_result(result_block("rework", feedback="Fix X."), reviews) == AgentResult(
        "rework",
        feedback="Fix X.",
    )
    assert parse_result(result_block("rework", feedback=["A", "", "B"]), reviews) == AgentResult(
        "rework",
        feedback="- A\n- B",
    )


# --- branch names, prompt, lock ------------------------------------------------------


def test_only_the_head_and_tail_of_the_verify_output_are_read(monkeypatch) -> None:
    import tempfile

    from vibepod.core import board_worker

    monkeypatch.setattr(board_worker, "REPORT_OUTPUT_HEAD", 5)
    monkeypatch.setattr(board_worker, "REPORT_OUTPUT_TAIL", 4)
    with tempfile.TemporaryFile() as output:
        output.write(b"short")
        assert board_worker.read_output(output) == "short"
        output.write(b" and then a lot more output-TAIL")
        reads: list[int] = []
        read = output.read
        monkeypatch.setattr(output, "read", lambda size=-1: reads.append(size) or read(size))

        assert board_worker.read_output(output) == "short\n[… 28 bytes omitted …]\nTAIL"
        assert reads == [5, 4]


def test_branch_names_follow_the_template() -> None:
    task = {"key": "VP-12", "taskNumber": 12, "githubIssueNumber": 201}
    assert branch_name("issue-{issue}", task) == "issue-201"
    assert branch_name("issue-{issue}", {"key": "VP-12", "taskNumber": 12}) == "vp-12"
    assert branch_name("feature/{project}-{number}", task) == "feature/vp-12"
    assert branch_name("{key}", task) == "vp-12"
    assert branch_name("issue-{issue:04d}", task) == "issue-0201"
    assert branch_name("issue-{issue:04d}", {"key": "VP-12", "taskNumber": 12}) == "vp-12"


def test_the_prompt_says_so_when_details_are_missing() -> None:
    prompt = build_prompt({"key": "VP-1", "title": "Bare"}, "vp-1")
    assert "No description was given." in prompt
    assert "None were given." in prompt


def test_one_run_at_a_time_per_profile(tmp_path: Path) -> None:
    first = FileProfileLock(tmp_path / "locks" / "board-work-default.lock", "default")
    second = FileProfileLock(tmp_path / "locks" / "board-work-default.lock", "default")

    assert first.try_acquire() is True
    assert second.try_acquire() is False
    first.release()
    assert second.try_acquire() is True
    second.release()


def test_a_worker_waits_for_the_profile_lock(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    tmp_path: Path,
) -> None:
    board.add_task("Waits its turn")
    held = FileProfileLock(tmp_path / "profile.lock", "default")
    assert held.try_acquire()
    clock = Clock()
    release_at = clock.now + 30

    class ReleasingLock(FileProfileLock):
        def try_acquire(self) -> bool:
            if clock.now >= release_at:
                held.release()
            return super().try_acquire()

    messages: list[tuple[str, str]] = []
    worker = BoardWorker(
        BoardClient(server.url, TOKEN),
        FakeRunner(),
        WorkOptions(project="VP", agent="claude", name="claude@laptop", repo=repo),
        lock=ReleasingLock(tmp_path / "profile.lock", "default"),
        say=lambda level, message: messages.append((level, message)),
        clock=clock,
        sleep=clock.sleep,
    )
    worker.run()

    assert worker.summary.handed_over == ["VP-1"]
    assert ("info", "Waiting for another run on profile default to finish") in messages
    assert any(
        beat.get("statusReason") == "Waiting for another run on profile default to finish"
        for beat in board.heartbeats
    )


# --- the board client ----------------------------------------------------------------


def test_the_client_names_the_board_error(board: FakeBoard, server: FakeBoardServer) -> None:
    board.add_task("Held", column="review")
    client = BoardClient(server.url, TOKEN)

    with pytest.raises(BoardApiError) as refused:
        client.claim("VP", "someone", task="VP-1")
    assert refused.value.status == 409
    assert refused.value.message == "Task VP-1 cannot be claimed"

    with pytest.raises(BoardApiError) as unauthorised:
        BoardClient(server.url, "wrong").claim("VP", "someone")
    assert unauthorised.value.status == 401


def test_the_client_refuses_redirects_and_keeps_the_token(board: FakeBoard) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    received: list[str | None] = []

    class Elsewhere(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_POST = do_GET

        def log_message(self, *args: Any) -> None:
            pass

    elsewhere = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    host, port = elsewhere.server_address[:2]
    board.redirect = f"http://{host}:{port}/steal"
    thread = threading.Thread(target=elsewhere.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        with FakeBoardServer(board) as server, pytest.raises(BoardApiError) as refused:
            BoardClient(server.url, TOKEN).claim("VP", "someone")
    finally:
        elsewhere.shutdown()
        elsewhere.server_close()
        thread.join(timeout=5)
    assert refused.value.status == 302
    assert received == []


def test_the_client_reports_an_unreachable_board() -> None:
    with pytest.raises(BoardApiError, match="Cannot reach the board") as unreachable:
        BoardClient("http://127.0.0.1:9", TOKEN, timeout=2).claim("VP", "someone")
    assert unreachable.value.status is None


def test_board_settings_come_from_config_and_environment(monkeypatch, tmp_path: Path) -> None:
    from vibepod.core.config import get_config

    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yaml").write_text(
        "board:\n  url: http://board.local:3000/\n  token: vbp_from-config\n",
    )
    assert resolve_board_settings(get_config()) == resolve_board_settings(
        {"board": {"url": "http://board.local:3000", "token": "vbp_from-config"}},
    )

    monkeypatch.setenv("VP_BOARD_URL", "https://board.example.com")
    monkeypatch.setenv("VP_BOARD_TOKEN", "vbp_from-env")
    settings = resolve_board_settings(get_config())
    assert (settings.url, settings.token) == ("https://board.example.com", "vbp_from-env")

    with pytest.raises(ValueError, match="No board token"):
        resolve_board_settings({"board": {"url": "http://board.local"}})
    with pytest.raises(ValueError, match="No board URL"):
        resolve_board_settings({})


# --- the Docker runner ---------------------------------------------------------------


class _Container:
    id = "cid123456789012"
    name = "vibepod-task-abc"
    status = "running"

    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {"State": {"Status": "running"}}
        self.stopped = False
        self.log_requests: list[dict[str, Any]] = []

    def reload(self) -> None:
        pass

    def logs(self, **kwargs: Any) -> bytes:
        self.log_requests.append(kwargs)
        return b"All done.\n"

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True


class _Manager:
    def __init__(self) -> None:
        self.run_kwargs: dict[str, Any] | None = None
        self.container = _Container()

    def ensure_network(self, name: str) -> None:
        pass

    def pull_image(self, image: str, auto_clean: bool = False) -> None:
        pass

    def resolve_launch_command(self, image: str, command: list[str] | None) -> list[str]:
        return command or []

    def run_agent(self, **kwargs: Any) -> _Container:
        self.run_kwargs = kwargs
        return self.container


def test_the_docker_runner_keeps_the_board_token_from_the_agent(
    monkeypatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    from vibepod.commands import task as task_cmd
    from vibepod.core.tasks import TaskStore

    manager = _Manager()
    checked: list[Path] = []
    monkeypatch.setattr(
        task_cmd,
        "get_config",
        lambda: {
            "network": "vibepod-network",
            "agents": {"claude": {"env": {"LEAK": TOKEN}, "init": []}},
            "proxy": {"enabled": False},
        },
    )
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: manager)
    monkeypatch.setattr(task_cmd, "is_dir_allowed", lambda path: checked.append(path) or True)
    monkeypatch.setattr(task_cmd, "_task_store", lambda: TaskStore(tmp_path / "tasks.db"))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    runner = board_cmd.DockerAgentRunner(
        "claude",
        env=[f"ALSO={TOKEN}", "KEEP=1"],
        forbidden_env_values=(TOKEN,),
    )

    run = runner.start(
        "Do it",
        worktree,
        mounts=[("/repo/.git", "/repo/.git", "rw")],
        allow_check_path=repo,
    )

    assert manager.run_kwargs is not None
    env = manager.run_kwargs["env"]
    assert TOKEN not in env.values()
    assert env["KEEP"] == "1"
    assert ("/repo/.git", "/repo/.git", "rw") in manager.run_kwargs["extra_volumes"]
    assert manager.run_kwargs["workspace"] == worktree.resolve()
    assert checked == [repo.resolve()]
    assert manager.run_kwargs["command"][-1] == "Do it"

    assert run.poll() is None
    manager.container.attrs["State"] = {"Status": "exited", "ExitCode": 0}
    assert run.poll() == 0
    assert run.logs() == "All done.\n"
    run.stop()
    assert manager.container.stopped is True


def test_the_docker_runner_backs_the_time_limit_and_tells_hiccups_from_exits(
    monkeypatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    import docker.errors

    from vibepod.commands import task as task_cmd
    from vibepod.core.tasks import TaskStore

    manager = _Manager()
    watchers: list[tuple[str, int]] = []
    monkeypatch.setattr(
        task_cmd,
        "get_config",
        lambda: {"agents": {"claude": {"env": {}, "init": []}}, "proxy": {"enabled": False}},
    )
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: manager)
    monkeypatch.setattr(task_cmd, "is_dir_allowed", lambda path: True)
    monkeypatch.setattr(task_cmd, "_task_store", lambda: TaskStore(tmp_path / "tasks.db"))
    monkeypatch.setattr(
        task_cmd,
        "_start_timeout_watcher",
        lambda task_id, seconds: watchers.append((task_id, seconds)),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    run = board_cmd.DockerAgentRunner("claude", timeout_seconds=600).start(
        "Do it",
        worktree,
        mounts=[],
        allow_check_path=repo,
    )

    assert watchers == [(run.task_id, 600 + board_cmd.WATCHER_SLACK_SECONDS)]
    # Only the end of the output is read, however much the agent wrote.
    assert run.logs() == "All done.\n"
    assert manager.container.log_requests[-1] == {"tail": board_cmd.AGENT_LOG_LINES}

    def engine_hiccup() -> None:
        raise docker.errors.APIError("engine busy")

    manager.container.reload = engine_hiccup  # type: ignore[method-assign]
    assert run.poll() is None

    def gone() -> None:
        raise docker.errors.NotFound("no such container")

    manager.container.reload = gone  # type: ignore[method-assign]
    assert run.poll() == 1


def test_the_docker_runner_says_when_the_agent_could_not_be_stopped(
    monkeypatch,
    tmp_path: Path,
    repo: Path,
) -> None:
    import docker.errors

    from vibepod.commands import task as task_cmd
    from vibepod.core.tasks import TaskStore

    manager = _Manager()
    store = TaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(
        task_cmd,
        "get_config",
        lambda: {"agents": {"claude": {"env": {}, "init": []}}, "proxy": {"enabled": False}},
    )
    monkeypatch.setattr(task_cmd, "DockerManager", lambda: manager)
    monkeypatch.setattr(task_cmd, "is_dir_allowed", lambda path: True)
    monkeypatch.setattr(task_cmd, "_task_store", lambda: store)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run = board_cmd.DockerAgentRunner("claude").start(
        "Do it",
        worktree,
        mounts=[],
        allow_check_path=repo,
    )

    def refuses(timeout: int = 10) -> None:
        raise docker.errors.APIError("engine busy")

    manager.container.stop = refuses  # type: ignore[method-assign]
    with pytest.raises(AgentStopError, match="could not be stopped and may still be running"):
        run.stop()
    assert store.get(run.task_id).status != "cancelled"

    # Stopped after all, such as by its own time limit: nothing is left running.
    manager.container.attrs["State"] = {"Status": "exited", "ExitCode": 137}
    run.stop()
    assert store.get(run.task_id).status == "cancelled"


def test_the_docker_runner_turns_launch_failures_into_runner_errors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from vibepod.commands import task as task_cmd

    monkeypatch.setattr(task_cmd, "get_config", lambda: {"agents": {}, "proxy": {"enabled": False}})
    monkeypatch.setattr(task_cmd, "is_dir_allowed", lambda path: False)
    monkeypatch.setattr(task_cmd.sys.stdin, "isatty", lambda: False)

    with pytest.raises(RunnerError, match="could not be started"):
        board_cmd.DockerAgentRunner("claude").start(
            "Do it",
            tmp_path,
            mounts=[],
            allow_check_path=tmp_path,
        )


# --- the command ---------------------------------------------------------------------


def test_vp_board_work_runs_a_task_end_to_end(
    monkeypatch,
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    tmp_path: Path,
) -> None:
    board.add_task("From the command line", githubIssueNumber=7)
    runner = FakeRunner()
    created: list[dict[str, Any]] = []

    def fake_runner(agent: str, **kwargs: Any) -> FakeRunner:
        created.append({"agent": agent, **kwargs})
        return runner

    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("VP_BOARD_URL", server.url)
    monkeypatch.setenv("VP_BOARD_TOKEN", TOKEN)
    monkeypatch.setattr(board_cmd, "DockerAgentRunner", fake_runner)
    monkeypatch.setattr(board_cmd, "is_dir_allowed", lambda path: True)

    result = CliRunner().invoke(
        app,
        [
            "board",
            "work",
            "VP",
            "--agent",
            "claude",
            "--repo",
            str(repo),
            "--once",
            "--name",
            "ci-worker",
            "--profile",
            "default",
            "--ikwid",
            "--timeout",
            "30m",
            "--verify",
            python("open('feature.txt')"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert board.card("VP-1")["column"] == "review"
    assert board.card("VP-1")["branchName"] == "issue-7"
    assert board.workers["worker-1"]["name"] == "ci-worker"
    assert created[0]["ikwid"] is True
    assert created[0]["forbidden_env_values"] == (TOKEN,)
    assert "Handed over 1 task(s)" in result.output


def test_vp_board_work_needs_a_board_token(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("VP_BOARD_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("VP_BOARD_TOKEN", raising=False)

    result = CliRunner().invoke(app, ["board", "work", "VP", "--agent", "claude"])

    assert result.exit_code == 1
    assert "No board token" in result.output


def test_vp_board_work_rejects_agents_without_headless_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("VP_BOARD_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("VP_BOARD_TOKEN", TOKEN)

    result = CliRunner().invoke(app, ["board", "work", "VP", "--agent", "gemini"])

    assert result.exit_code == 1
    assert "cannot run headless" in result.output


def test_vp_board_work_reviews_under_a_reviewer_name(
    monkeypatch,
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
    tmp_path: Path,
) -> None:
    sha = handed_over(board, repo)
    runner = FakeRunner(says("approve", summary="Good."))
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("VP_BOARD_URL", server.url)
    monkeypatch.setenv("VP_BOARD_TOKEN", TOKEN)
    monkeypatch.setattr(board_cmd, "DockerAgentRunner", lambda agent, **kwargs: runner)
    monkeypatch.setattr(board_cmd, "is_dir_allowed", lambda path: True)
    monkeypatch.setattr(board_cmd.socket, "gethostname", lambda: "laptop.local")

    result = CliRunner().invoke(
        app,
        ["board", "work", "VP", "--agent", "claude", "--mode", "review", "--repo", str(repo)],
    )

    assert result.exit_code == 0, result.output
    assert board.workers["worker-1"]["name"] == "claude-review@laptop"
    assert board.workers["worker-1"]["mode"] == "review"
    [verdict] = board.requests("POST", "/api/board/card-1/review")
    assert (verdict["verdict"], verdict["headSha"]) == ("approve", sha)
    assert "Approved 1 task(s), sent 0 back for rework" in result.output


@pytest.mark.parametrize(
    "option",
    [
        ["--branch-template", "feature-{key}"],
        ["--existing", "refuse"],
        ["--on-fail", "blocked"],
        ["--max-attempts", "2"],
    ],
)
def test_vp_board_work_refuses_implementation_options_in_review_mode(
    monkeypatch,
    tmp_path: Path,
    option: list[str],
) -> None:
    monkeypatch.setenv("VP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("VP_BOARD_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("VP_BOARD_TOKEN", TOKEN)

    result = CliRunner().invoke(
        app,
        ["board", "work", "VP", "--agent", "claude", "--mode", "review", *option],
    )

    assert result.exit_code == 1
    assert f"{option[0]} cannot be used with --mode review" in result.output


def test_host_git_ignores_a_repository_named_in_the_environment(
    monkeypatch,
    repo: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet", "--initial-branch=main")
    git(other, "-c", "user.name=T", "-c", "user.email=t@x", "commit", "--allow-empty", "-qm", "O")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))

    assert worktrees.repository_root(repo) == repo.resolve()
    worktree = worktrees.prepare_worktree(repo, tmp_path / "worktrees", "issue-1", "main")

    assert worktrees.branch_exists(repo, "issue-1")
    assert not worktrees.branch_exists(other, "issue-1")
    assert worktree.path == (tmp_path / "worktrees" / "issue-1").resolve()


def test_the_agent_cannot_switch_or_stage_in_other_checkouts(repo: Path, tmp_path: Path) -> None:
    git(repo, "worktree", "add", "--quiet", "-b", "mine", str(tmp_path / "mine"))
    task = worktrees.prepare_worktree(repo, tmp_path / "worktrees", "issue-1", "main")
    git_dir = (repo / ".git").resolve()

    readonly = {host for host, _, mode in worktrees.agent_mounts(repo, task.path) if mode == "ro"}

    assert {str(git_dir / "HEAD"), str(git_dir / "index")} <= readonly
    assert {str(git_dir / "worktrees" / "mine" / name) for name in ("HEAD", "index")} <= readonly
    # The task's own HEAD and index stay writable for its commits.
    own = worktrees.admin_dir(task.path)
    assert not {str(own / "HEAD"), str(own / "index")} & readonly


def test_the_agent_cannot_write_per_worktree_configuration(repo: Path, tmp_path: Path) -> None:
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "worktree", "add", "--quiet", "-b", "mine", str(tmp_path / "mine"))
    task = worktrees.prepare_worktree(repo, tmp_path / "worktrees", "issue-1", "main")
    git_dir = (repo / ".git").resolve()
    own = worktrees.admin_dir(task.path)

    readonly = {host for host, _, mode in worktrees.agent_mounts(repo, task.path) if mode == "ro"}

    configs = [git_dir, git_dir / "worktrees" / "mine", own]
    assert {str(path / "config.worktree") for path in configs} <= readonly
    assert all((path / "config.worktree").read_text() == "" for path in configs)


def test_windows_git_paths_are_translated_for_the_linux_container(
    monkeypatch,
    repo: Path,
    tmp_path: Path,
) -> None:
    from pathlib import PurePosixPath, PureWindowsPath

    assert worktrees.container_path(PureWindowsPath(r"C:\Users\me\app\.git")) == (
        "/c/Users/me/app/.git"
    )
    assert worktrees.container_path(PurePosixPath("/home/me/app/.git")) == "/home/me/app/.git"
    with pytest.raises(worktrees.GitError, match="use a local drive"):
        worktrees.container_path(PureWindowsPath(r"\\server\share\app\.git"))

    task = worktrees.prepare_worktree(repo, tmp_path / "worktrees", "issue-1", "main")
    own_pointer = (task.path / ".git").read_text()
    # As on a Windows host, where no host path is a container path.
    monkeypatch.setattr(worktrees, "container_path", lambda path: f"/c{PurePath(path).as_posix()}")

    mounts = worktrees.agent_mounts(repo, task.path)

    git_dir = (repo / ".git").resolve()
    admin = worktrees.admin_dir(task.path)
    assert (str(git_dir), f"/c{git_dir.as_posix()}", "rw") in mounts
    host, target, mode = mounts[-1]
    assert (target, mode) == ("/workspace/.git", "ro")
    assert Path(host).read_text() == f"gitdir: /c{admin.as_posix()}\n"
    # The worktree's own pointer, which git on the host reads, is left as it was.
    assert (task.path / ".git").read_text() == own_pointer


def test_the_users_checkout_is_not_taken_over_from_a_worktree_folder_around_it(
    repo: Path,
    tmp_path: Path,
) -> None:
    git(repo, "checkout", "--quiet", "-b", "vp-1")
    elsewhere = tmp_path / "elsewhere"
    git(repo, "worktree", "add", "--quiet", "-b", "vp-2", str(elsewhere))

    # The worktree folder holds the user's checkout, and a worktree it did not make.
    with pytest.raises(worktrees.GitError, match="not working in someone else's checkout"):
        worktrees.prepare_worktree(repo, tmp_path, "vp-1", "main")
    with pytest.raises(worktrees.GitError, match="not working in someone else's checkout"):
        worktrees.prepare_worktree(repo, tmp_path, "vp-2", "main")
    assert worktrees.current_branch(repo) == "vp-1"


def test_worktree_helpers_prepare_and_clean_up(repo: Path, tmp_path: Path) -> None:
    fresh = worktrees.prepare_worktree(repo, tmp_path / "trees", "vp-9", "main")
    assert fresh.continued is False
    assert worktrees.current_branch(fresh.path) == "vp-9"
    (fresh.path / "change.txt").write_text("x\n")
    assert worktrees.has_changes(fresh.path)
    worktrees.commit_all(fresh.path, "Change")
    assert [commit["subject"] for commit in worktrees.commits_since(fresh.path, fresh.start)] == [
        "Change",
    ]

    again = worktrees.prepare_worktree(repo, tmp_path / "trees", "vp-9", "main")
    assert (again.path, again.continued) == (fresh.path, True)
    with pytest.raises(worktrees.BranchExistsError):
        worktrees.prepare_worktree(repo, tmp_path / "trees", "vp-9", "main", "refuse")
    with pytest.raises(worktrees.GitError, match="Invalid branch name"):
        worktrees.prepare_worktree(repo, tmp_path / "trees", "bad..name", "main")

    with pytest.raises(worktrees.GitError, match="outside"):
        worktrees.remove_worktree(repo, tmp_path / "elsewhere", fresh.path)
    worktrees.remove_worktree(repo, tmp_path / "trees", fresh.path)
    assert not fresh.path.exists()
    assert worktrees.branch_exists(repo, "vp-9")


def test_review_worktrees_are_detached_and_replaced(repo: Path, tmp_path: Path) -> None:
    sha = git(repo, "rev-parse", "main")
    branches = worktrees.branch_refs(repo)

    path = worktrees.prepare_review_worktree(repo, tmp_path / "trees", "review-vp-1-a@b", sha)

    assert path == (tmp_path / "trees" / "review-vp-1-a-b").resolve()
    assert worktrees.current_branch(path) is None
    assert git(path, "rev-parse", "HEAD") == sha
    (path / "left.txt").write_text("left\n")
    # A review worktree left behind is replaced, and no branch is ever made.
    again = worktrees.prepare_review_worktree(repo, tmp_path / "trees", "review-vp-1-a@b", sha)
    assert again == path and not (path / "left.txt").exists()
    assert worktrees.branch_refs(repo) == branches
    (tmp_path / "trees" / "taken").mkdir()
    (tmp_path / "trees" / "taken" / "file").write_text("mine\n")
    with pytest.raises(worktrees.GitError, match="in use"):
        worktrees.prepare_review_worktree(repo, tmp_path / "trees", "taken", sha)

    git(path, "checkout", "--quiet", "-b", "agent-branch")
    (path / "README.md").write_text("changed\n")
    worktrees.discard_changes(path, sha)
    assert worktrees.current_branch(path) is None
    assert git(path, "status", "--porcelain") == ""
    assert worktrees.commit_exists(repo, sha)
    assert not worktrees.commit_exists(repo, "0" * 40)

    mounts = worktrees.agent_mounts(repo, path, read_only=True)
    git_dir = (repo / ".git").resolve()
    assert mounts[0] == (str(git_dir), str(git_dir), "ro")

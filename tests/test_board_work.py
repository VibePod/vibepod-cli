"""`vp board work` end to end: a fake agent in real git worktrees, against a fake board over
HTTP. No agent subscription and no container runtime are involved."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from board_fake import TOKEN, FakeBoard, FakeBoardServer
from typer.testing import CliRunner

from vibepod.cli import app
from vibepod.commands import board as board_cmd
from vibepod.core import worktrees
from vibepod.core.board_client import BoardApiError, BoardClient, resolve_board_settings
from vibepod.core.board_worker import (
    BoardWorker,
    FileProfileLock,
    RunnerError,
    WorkerError,
    WorkOptions,
    branch_name,
    build_prompt,
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


def commits_a_feature(path: Path, prompt: str) -> tuple[int, str]:
    (path / "feature.txt").write_text("implemented\n")
    git(path, "add", "feature.txt")
    git(path, "commit", "--quiet", "-m", "Add the feature")
    return 0, "Working...\nAdded feature.txt and committed it."


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
    }
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
    assert report["summary"].endswith("Added feature.txt and committed it.")
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
    git_dir = (repo / ".git").resolve()
    worktree = (repo.parent / "app-worktrees" / "vp-1").resolve()
    admin = git_dir / "worktrees" / "vp-1"
    # The git directory is writable for commits; what makes git run programs, and the
    # worktree's pointers into it, are not.
    assert start["mounts"] == [
        (str(git_dir), str(git_dir), "rw"),
        (str(git_dir / "hooks"), str(git_dir / "hooks"), "ro"),
        (str(git_dir / "info"), str(git_dir / "info"), "ro"),
        (str(git_dir / "config"), str(git_dir / "config"), "ro"),
        (str(admin / "commondir"), str(admin / "commondir"), "ro"),
        (str(admin / "gitdir"), str(admin / "gitdir"), "ro"),
        (str(worktree / ".git"), "/workspace/.git", "ro"),
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
        return 0, "Done."

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
        return 0, "Nothing to do."

    work(server, FakeRunner(idles), repo, task="VP-2")
    assert board.runs[-1]["failureReason"] == "The agent made no changes"
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

    def cancel_once_running(beat: dict[str, Any]) -> list[dict[str, Any]]:
        if beat.get("step") == "agent_running":
            board.cards["idea-1"].update(column="planned", assignee=None, claimedAt=None)
            return [{"type": "cancel", "taskId": "idea-1", "reason": "Run cancelled by admin"}]
        return []

    board.on_heartbeat = cancel_once_running
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


def test_a_cancelled_run_that_also_failed_is_not_given_back(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Cancelled and failing")

    def cancel_once_running(beat: dict[str, Any]) -> list[dict[str, Any]]:
        if beat.get("step") == "agent_running":
            board.cards["idea-1"].update(column="planned", assignee=None, claimedAt=None)
            return [{"type": "cancel", "taskId": "idea-1", "reason": "Run cancelled by admin"}]
        return []

    board.on_heartbeat = cancel_once_running

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

    def stop_once_running(beat: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "stop"}] if beat.get("step") == "agent_running" else []

    board.on_heartbeat = stop_once_running
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
        return 0, "Added a usage limit reached banner."

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
        return 0, "Done."

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
        return 0, "Done."

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
        return 0, "Done."

    work(server, FakeRunner(moves_main), repo, once=True)

    assert git(repo, "rev-parse", "main") == main_before
    [release] = board.requests("POST", "/api/board/card-1/release")
    assert release["outcome"] == "blocked"
    assert release["note"].startswith("The agent moved main; restored them")


def test_an_agent_that_left_its_branch_blocks_the_task(
    board: FakeBoard,
    server: FakeBoardServer,
    repo: Path,
) -> None:
    board.add_task("Wanders off")

    def detaches(path: Path, prompt: str) -> tuple[int, str]:
        commits_a_feature(path, prompt)
        git(path, "checkout", "--quiet", "--detach")
        return 0, "Done."

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
        return 0, "Everything was already done."

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

    def reload(self) -> None:
        pass

    def logs(self, **kwargs: Any) -> bytes:
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

    def engine_hiccup() -> None:
        raise docker.errors.APIError("engine busy")

    manager.container.reload = engine_hiccup  # type: ignore[method-assign]
    assert run.poll() is None

    def gone() -> None:
        raise docker.errors.NotFound("no such container")

    manager.container.reload = gone  # type: ignore[method-assign]
    assert run.poll() == 1


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

"""Git worktrees for `vp board work`: one branch and one worktree per board task.

The agent works in the worktree while the repository's own checkout stays untouched. A branch
or worktree left by an earlier run of the same task is continued, or refused on request.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The committer used when the repository has no identity configured.
FALLBACK_IDENTITY = ("VibePod", "vibepod@users.noreply.github.com")


class GitError(Exception):
    pass


class BranchExistsError(GitError):
    """The task's branch or worktree exists and continuing was not allowed."""


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def repository_root(path: Path) -> Path:
    if not path.is_dir():
        raise GitError(f"Repository not found: {path}")
    try:
        return Path(git(path, "rev-parse", "--show-toplevel")).resolve()
    except GitError as exc:
        raise GitError(f"Not a git repository: {path}") from exc


def common_git_dir(repo: Path) -> Path:
    """The `.git` directory shared by the repository and its worktrees. A worktree points
    into it by absolute path, so the agent container mounts it at the same path."""
    value = Path(git(repo, "rev-parse", "--git-common-dir"))
    return (value if value.is_absolute() else repo / value).resolve()


def current_branch(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


def resolve_commit(repo: Path, ref: str) -> str:
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError as exc:
        raise GitError(f"Base not found: {ref}") from exc


def branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def is_valid_branch_name(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def worktree_of_branch(repo: Path, branch: str) -> Path | None:
    """Where the branch is checked out as a worktree, if anywhere."""
    current: Path | None = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree ") :])
        elif line == f"branch refs/heads/{branch}" and current is not None:
            return current.resolve()
    return None


def worktree_folder(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "worktree"


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    # The commit the run started from: commits after it are the ones this run made.
    start: str
    # True when an earlier run left the branch or the worktree.
    continued: bool


def prepare_worktree(
    repo: Path,
    worktrees_dir: Path,
    branch: str,
    base: str,
    existing: str = "continue",
) -> Worktree:
    """Checks the task's branch out in its own worktree, branching from `base` when the
    branch is new. `existing` decides what happens to a branch or worktree left by an earlier
    run: `continue` works on in it, `refuse` raises `BranchExistsError`."""
    if not is_valid_branch_name(repo, branch):
        raise GitError(f"Invalid branch name: {branch}")
    git(repo, "worktree", "prune")
    checked_out = worktree_of_branch(repo, branch)
    if checked_out is not None or branch_exists(repo, branch):
        if existing != "continue":
            where = f" in {checked_out}" if checked_out else ""
            raise BranchExistsError(f"Branch {branch} already exists{where}")
        if checked_out is not None and checked_out == repo:
            raise GitError(
                f"Branch {branch} is checked out in the repository itself; switch it to "
                "another branch first"
            )
        path = checked_out or _add(repo, worktrees_dir, branch)
        return Worktree(
            path=path, branch=branch, start=git(path, "rev-parse", "HEAD"), continued=True
        )
    start = resolve_commit(repo, base)
    path = _add(repo, worktrees_dir, branch, start)
    return Worktree(path=path, branch=branch, start=start, continued=False)


def _add(repo: Path, worktrees_dir: Path, branch: str, start: str | None = None) -> Path:
    path = (worktrees_dir / worktree_folder(branch)).resolve()
    if path.exists() and any(path.iterdir()):
        raise GitError(f"Worktree folder is in use: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if start is None:
        git(repo, "worktree", "add", str(path), branch)
    else:
        git(repo, "worktree", "add", "-b", branch, str(path), start)
    return path


def commits_since(path: Path, start: str) -> list[dict[str, str]]:
    """The commits on the worktree's branch after `start`, oldest first."""
    output = git(path, "log", "--reverse", "--format=%H%x1f%s", f"{start}..HEAD")
    commits = []
    for line in output.splitlines():
        sha, _, subject = line.partition("\x1f")
        if sha:
            commits.append({"sha": sha, "subject": subject})
    return commits


def has_changes(path: Path) -> bool:
    return bool(git(path, "status", "--porcelain"))


def commit_all(path: Path, message: str) -> str | None:
    """Commits everything the agent left uncommitted; None when there was nothing."""
    if not has_changes(path):
        return None
    git(path, "add", "-A")
    identity: list[str] = []
    if not _has_identity(path):
        name, email = FALLBACK_IDENTITY
        identity = ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    git(path, *identity, "commit", "--quiet", "--no-verify", "-m", message)
    return git(path, "rev-parse", "HEAD")


def _has_identity(path: Path) -> bool:
    for key in ("user.name", "user.email"):
        result = subprocess.run(
            ["git", "config", key], cwd=path, capture_output=True, text=True, check=False
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
    return True


def remove_worktree(repo: Path, path: Path) -> None:
    """Removes the worktree; the branch and its commits stay."""
    git(repo, "worktree", "remove", "--force", str(path))

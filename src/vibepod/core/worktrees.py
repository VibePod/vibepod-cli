"""Git worktrees for `vp board work`: one branch and one worktree per board task.

The agent works in the worktree while the repository's own checkout stays untouched. A branch
or worktree left by an earlier run of the same task is continued, or refused on request. Only
worktrees in the worker's own folder are ever reused or removed.

Git runs here on the host, in repositories an agent has worked in, so it never runs code from
the repository: hooks and fsmonitors are off for every call, the agent container mounts the
parts of the git directory that make git run programs read-only, and the worktree's pointers
into the git directory are checked before git touches the worktree after a run.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The committer used when the repository has no identity configured.
FALLBACK_IDENTITY = ("VibePod", "vibepod@users.noreply.github.com")
# No hooks and no fsmonitor: git on the host must not run programs the repository names.
SAFE_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")


class GitError(Exception):
    pass


class BranchExistsError(GitError):
    """The task's branch or worktree exists and continuing was not allowed."""


# Variables that point git at another repository than the one in `cwd`, as set for a hook
# or by a wrapper script that `vp` may run from (`git rev-parse --local-env-vars`).
GIT_LOCATION_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    },
)


def git_env() -> dict[str, str]:
    """The environment without the variables that would make git work elsewhere."""
    return {key: value for key, value in os.environ.items() if key not in GIT_LOCATION_ENV}


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *SAFE_CONFIG, *args],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def git(cwd: Path, *args: str) -> str:
    result = _run(cwd, *args)
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
    result = _run(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return result.stdout.strip() or None if result.returncode == 0 else None


def resolve_commit(repo: Path, ref: str) -> str:
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError as exc:
        raise GitError(f"Base not found: {ref}") from exc


def branch_exists(repo: Path, branch: str) -> bool:
    return _run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def is_valid_branch_name(repo: Path, branch: str) -> bool:
    return _run(repo, "check-ref-format", "--branch", branch).returncode == 0


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


def _inside(path: Path, folder: Path) -> bool:
    return path.resolve().is_relative_to(folder.resolve())


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    # The commit the run started from: commits after it are the ones this run made.
    start: str
    # The commit the branch grew from: commits after it are the work the branch holds.
    base: str
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
    run: `continue` works on in it, `refuse` raises `BranchExistsError`. A branch checked out
    outside the worktree folder, such as in the user's own worktree, is never taken over."""
    if not is_valid_branch_name(repo, branch):
        raise GitError(f"Invalid branch name: {branch}")
    git(repo, "worktree", "prune")
    base_commit = resolve_commit(repo, base)
    checked_out = worktree_of_branch(repo, branch)
    if checked_out is not None or branch_exists(repo, branch):
        if existing != "continue":
            where = f" in {checked_out}" if checked_out else ""
            raise BranchExistsError(f"Branch {branch} already exists{where}")
        if checked_out is not None and not _inside(checked_out, worktrees_dir):
            raise GitError(
                f"Branch {branch} is checked out in {checked_out}, outside {worktrees_dir}; "
                "not working in someone else's checkout",
            )
        path = checked_out or _add(repo, worktrees_dir, branch)
        return Worktree(
            path=path,
            branch=branch,
            start=git(path, "rev-parse", "HEAD"),
            base=base_commit,
            continued=True,
        )
    path = _add(repo, worktrees_dir, branch, base_commit)
    return Worktree(path=path, branch=branch, start=base_commit, base=base_commit, continued=False)


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


def admin_dir(worktree: Path) -> Path:
    """The worktree's own directory inside the git directory, named by its `.git` file."""
    pointer = worktree / ".git"
    if not pointer.is_file():
        raise GitError(f"Not a linked worktree: {worktree}")
    content = pointer.read_text(encoding="utf-8").strip()
    if not content.startswith("gitdir:"):
        raise GitError(f"Unreadable worktree pointer: {pointer}")
    value = Path(content[len("gitdir:") :].strip())
    return (value if value.is_absolute() else worktree / value).resolve()


def agent_mounts(
    repo: Path,
    worktree: Path,
    workspace_mount: str = "/workspace",
) -> list[tuple[str, str, str]]:
    """The volumes the agent container needs besides the worktree: the git directory the
    worktree points into, at the same path so git works in the container. It stays writable
    for commits, but what makes git run programs (config, hooks, info) and the worktree's
    pointers into it are read-only, since git on the host reads them after the run."""
    common = common_git_dir(repo)
    mounts = [(str(common), str(common), "rw")]
    for name in ("hooks", "info"):
        (common / name).mkdir(exist_ok=True)
        mounts.append((str(common / name), str(common / name), "ro"))
    admin = admin_dir(worktree)
    for file in (common / "config", admin / "commondir", admin / "gitdir"):
        if file.is_file():
            mounts.append((str(file), str(file), "ro"))
    mounts.append((str(worktree / ".git"), f"{workspace_mount}/.git", "ro"))
    return mounts


def pointers(worktree: Path) -> dict[str, str]:
    """The files that tell git where the worktree's repository is."""
    admin = admin_dir(worktree)
    files = {"worktree .git": worktree / ".git"}
    files.update(commondir=admin / "commondir", gitdir=admin / "gitdir")
    return {
        name: path.read_text(encoding="utf-8") if path.is_file() else ""
        for name, path in files.items()
    }


def verify_pointers(worktree: Path, before: dict[str, str]) -> None:
    """Refuses a worktree whose git pointers changed: git would then read a configuration
    the agent wrote, and could run programs from it on the host."""
    try:
        after = pointers(worktree)
    except (GitError, OSError) as exc:
        raise GitError(f"The agent changed the worktree's git metadata: {exc}") from exc
    changed = [name for name, value in before.items() if after.get(name) != value]
    if changed:
        raise GitError(f"The agent changed the worktree's git metadata ({', '.join(changed)})")


def branch_refs(repo: Path) -> dict[str, str]:
    output = git(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        "refs/tags",
    )
    refs = {}
    for line in output.splitlines():
        name, _, sha = line.partition(" ")
        refs[name] = sha
    return refs


def restore_refs(repo: Path, before: dict[str, str], own_branch: str) -> list[str]:
    """Puts back every branch and tag other than the task's own that moved or vanished during
    the run, and names them."""
    after = branch_refs(repo)
    restored = []
    for ref, sha in before.items():
        if ref != f"refs/heads/{own_branch}" and after.get(ref) != sha:
            git(repo, "update-ref", ref, sha)
            restored.append(ref.removeprefix("refs/heads/"))
    return restored


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
    # Unsigned: a background worker has no TTY or agent to unlock a signing key with.
    git(path, *identity, "commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", message)
    return git(path, "rev-parse", "HEAD")


def _has_identity(path: Path) -> bool:
    for key in ("user.name", "user.email"):
        result = _run(path, "config", key)
        if result.returncode != 0 or not result.stdout.strip():
            return False
    return True


def remove_worktree(repo: Path, worktrees_dir: Path, path: Path) -> None:
    """Removes a worktree the worker made; the branch and its commits stay."""
    if not _inside(path, worktrees_dir):
        raise GitError(f"Not removing {path}: it is outside {worktrees_dir}")
    git(repo, "worktree", "remove", "--force", str(path))

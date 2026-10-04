"""Git worktrees for `vp board work`: one branch and one worktree per board task.

The agent works in the worktree while the repository's own checkout stays untouched. A branch
or worktree left by an earlier run of the same task is continued, or refused on request. Only
worktrees in the worker's own folder are ever reused or removed.

Git runs here on the host, in repositories an agent has worked in, so it never runs code from
the repository: hooks and fsmonitors are off for every call, nested repositories are never
looked into, the agent container mounts the parts of the git directory that make git run
programs read-only, and the worktree's pointers into the git directory are checked before git
touches the worktree after a run.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

# The committer used when the repository has no identity configured.
FALLBACK_IDENTITY = ("VibePod", "vibepod@users.noreply.github.com")
# The agent container's `.git` file on a Windows host, kept in the worktree's own directory
# inside the git directory.
CONTAINER_POINTER = "vibepod-container-gitdir"
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


def checkouts(repo: Path) -> dict[str, Path]:
    """The branches checked out in the repository's worktrees, by ref, and where."""
    found: dict[str, Path] = {}
    current: Path | None = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and current is not None:
            found[line[len("branch ") :]] = current.resolve()
    return found


def worktree_of_branch(repo: Path, branch: str) -> Path | None:
    """Where the branch is checked out as a worktree, if anywhere."""
    return checkouts(repo).get(f"refs/heads/{branch}")


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
    anywhere but the task's own folder in the worktree folder, such as in the user's own
    checkout, is never taken over, even when the worktree folder holds that checkout."""
    if not is_valid_branch_name(repo, branch):
        raise GitError(f"Invalid branch name: {branch}")
    git(repo, "worktree", "prune")
    base_commit = resolve_commit(repo, base)
    checked_out = worktree_of_branch(repo, branch)
    if checked_out is not None or branch_exists(repo, branch):
        if existing != "continue":
            where = f" in {checked_out}" if checked_out else ""
            raise BranchExistsError(f"Branch {branch} already exists{where}")
        if checked_out is not None and (
            checked_out == repo.resolve() or checked_out != _task_path(worktrees_dir, branch)
        ):
            raise GitError(
                f"Branch {branch} is checked out in {checked_out}, not in the worker's "
                f"{_task_path(worktrees_dir, branch)}; not working in someone else's checkout",
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


def _task_path(worktrees_dir: Path, branch: str) -> Path:
    """Where the worker checks the branch out."""
    return (worktrees_dir / worktree_folder(branch)).resolve()


def _add(repo: Path, worktrees_dir: Path, branch: str, start: str | None = None) -> Path:
    path = _task_path(worktrees_dir, branch)
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


def container_path(path: PurePath) -> str:
    """Where a host path is mounted in the Linux agent container: at the same path, and for a
    Windows path such as `C:\\repo\\.git` at `/c/repo/.git`, since a Linux container cannot
    resolve a drive path."""
    text = str(path)
    if text.startswith("/"):
        return text
    windows = PureWindowsPath(text)
    drive = windows.drive
    if len(drive) != 2 or drive[1] != ":" or not windows.is_absolute():
        raise GitError(f"Cannot mount {text} into the agent container: use a local drive")
    return "/".join(["", drive[0].lower(), *windows.parts[1:]])


def agent_mounts(
    repo: Path,
    worktree: Path,
    workspace_mount: str = "/workspace",
) -> list[tuple[str, str, str]]:
    """The volumes the agent container needs besides the worktree: the git directory the
    worktree points into, at the same path so git works in the container. It stays writable
    for commits, but what makes git run programs (config, hooks, info) and the worktree's
    pointers into it are read-only, since git on the host reads them after the run. So are
    the per-worktree configurations where the repository uses them, and the HEAD and index
    of the other checkouts, such as the user's own, which the agent has no business
    switching or staging in."""
    common = common_git_dir(repo)
    mounts = [(str(common), container_path(common), "rw")]
    for name in ("hooks", "info"):
        (common / name).mkdir(exist_ok=True)
        mounts.append((str(common / name), container_path(common / name), "ro"))
    admin = admin_dir(worktree)
    others = [common]
    if (common / "worktrees").is_dir():
        others += sorted(
            path
            for path in (common / "worktrees").iterdir()
            if path.is_dir() and path.resolve() != admin
        )
    files = [common / "config", admin / "commondir", admin / "gitdir"]
    files += [other / name for other in others for name in ("HEAD", "index")]
    if _worktree_config(common):
        # Git reads each checkout's `config.worktree` too: made where missing, so that the
        # agent cannot write one, such as with a filter that `git add` on the host would run.
        for directory in (*others, admin):
            config = directory / "config.worktree"
            if not config.exists():
                config.touch()
            files.append(config)
    for file in files:
        if file.is_file():
            mounts.append((str(file), container_path(file), "ro"))
    pointer = worktree / ".git"
    if container_path(admin) != str(admin):
        # The worktree's `.git` names the git directory by its Windows path: the container
        # gets one that names it by its path there.
        pointer = admin / CONTAINER_POINTER
        pointer.write_text(f"gitdir: {container_path(admin)}\n", encoding="utf-8")
    mounts.append((str(pointer), f"{workspace_mount}/.git", "ro"))
    return mounts


def _worktree_config(common: Path) -> bool:
    """Whether the repository reads a configuration of each worktree's own."""
    result = _run(
        common,
        "config",
        "--file",
        str(common / "config"),
        "--bool",
        "extensions.worktreeConfig",
    )
    return result.stdout.strip() == "true"


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


def _index_matches(checkout: Path, sha: str) -> bool:
    """Whether the checkout's staging index holds the commit's tree: it does after the
    user committed there, and it does not when its branch was moved behind its back."""
    return _run(checkout, "diff-index", "--cached", "--quiet", sha, "--").returncode == 0


def restore_refs(
    repo: Path,
    before: dict[str, str],
    own_branch: str,
    checked_out_before: dict[str, Path],
) -> tuple[list[str], list[str]]:
    """Puts back the branches and tags other than the task's own that the agent moved or
    removed during the run. Says which it restored, and which moved in a checkout but
    could not be told apart from the user's own work there, so were left as they are.

    Others may move refs meanwhile too: a checked-out branch, such as another task's in the
    worktree folder or the user's in their checkout, moved with its checkout's index when
    it was committed to there, and is not touched. Each ref is put back only if it did not
    move again since it was read."""
    after = branch_refs(repo)
    checked_out = {**checked_out_before, **checkouts(repo)}
    restored: list[str] = []
    left: list[str] = []
    for ref, sha in before.items():
        now = after.get(ref, "")
        if ref == f"refs/heads/{own_branch}" or now == sha:
            continue
        name = ref.removeprefix("refs/heads/")
        checkout = checked_out.get(ref)
        if checkout is not None and checkout.is_dir():
            if now and _index_matches(checkout, now):
                continue
            if not _index_matches(checkout, sha):
                left.append(name)
                continue
        git(repo, "update-ref", ref, sha, now)
        restored.append(name)
    return restored, left


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
    # Not into nested repositories: git would run `git status` in them, with a configuration
    # the agent wrote, which can name a filter for git to run on the host.
    return bool(git(path, "status", "--porcelain", "--ignore-submodules=dirty"))


def _gitlinks(path: Path) -> list[str]:
    """The paths of the nested repositories (submodules) the index holds, read from the index
    without looking into them."""
    output = _run(path, "ls-files", "--stage", "-z").stdout
    return [entry.partition("\t")[2] for entry in output.split("\0") if entry.startswith("160000 ")]


def commit_all(path: Path, message: str) -> str | None:
    """Commits everything the agent left uncommitted; None when there was nothing. Nested
    repositories are left out, since git would run `git status` in them to add them: the
    agent commits a submodule change itself."""
    if not has_changes(path):
        return None
    git(path, "add", "-A", "--", ".", *(f":(exclude,literal){link}" for link in _gitlinks(path)))
    if _run(path, "diff", "--cached", "--quiet").returncode == 0:
        # Only a nested repository changed.
        return None
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

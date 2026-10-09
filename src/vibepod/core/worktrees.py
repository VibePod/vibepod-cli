"""Clones for `vp board work`: one branch and one clone of the repository per board task.

Each run gets a local clone of its own that shares the repository's objects (`git clone
--shared`), checked out on the task's branch; a review gets one with the reviewed commit
checked out detached. The agent works in its clone only, so whatever it does to branches,
tags or configuration stays there: the repository, the user's checkout and the other
workers' clones are out of its reach. After the run the worker brings the task's branch back
into the repository with a fetch that only ever moves it forward; a review brings nothing
back. Only clones in the worker's own folder are ever reused or removed.

Git runs here on the host, in clones an agent has worked in, so it never runs code from them:
hooks, fsmonitors and submodule recursion are off for every call, nested repositories are never
looked into, the agent container mounts the clone's configuration, hooks and object pointers
read-only, and those are checked before git touches the clone after a run.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

# The committer used when the repository has no identity configured.
FALLBACK_IDENTITY = ("VibePod", "vibepod@users.noreply.github.com")
# The agent container's list of the repository's object directories on a Windows host, by
# their paths there; kept in the clone's git directory.
CONTAINER_ALTERNATES = "vibepod-container-alternates"
# Where the agent container mounts the clone.
WORKSPACE = "/workspace"
# No hooks, no fsmonitor and no submodules: git on the host must not run programs a clone
# names.
SAFE_CONFIG = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "submodule.recurse=false",
)
# The settings `git clone` writes into a clone's configuration. One that has others was not
# left as the worker made it, and git on the host does not run in it again.
CLONE_SETTINGS = re.compile(
    r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|symlinks|ignorecase"
    r"|precomposeunicode)"
    r"|remote\.origin\.(url|fetch)"
    r"|branch\..+\.(remote|merge)"
    r"|extensions\.(objectformat|refstorage)"
    r"|user\.(name|email)",
    re.IGNORECASE,
)


class GitError(Exception):
    pass


class BranchExistsError(GitError):
    """The task's branch exists and continuing was not allowed."""


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


def current_branch(repo: Path) -> str | None:
    result = _run(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return result.stdout.strip() or None if result.returncode == 0 else None


def resolve_commit(repo: Path, ref: str) -> str:
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError as exc:
        raise GitError(f"Base not found: {ref}") from exc


def commit_exists(repo: Path, sha: str) -> bool:
    return _run(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def branch_exists(repo: Path, branch: str) -> bool:
    return branch_commit(repo, branch) is not None


def branch_commit(repo: Path, branch: str) -> str | None:
    """The commit the branch is at, or None when there is no such branch."""
    result = _run(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
    return result.stdout.strip() or None if result.returncode == 0 else None


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


def worktree_paths(repo: Path) -> list[Path]:
    """Where the repository's worktrees are, its own checkout first."""
    return [
        Path(line[len("worktree ") :]).resolve()
        for line in git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def worktree_of_branch(repo: Path, branch: str) -> Path | None:
    """Where the branch is checked out as a worktree, if anywhere."""
    return checkouts(repo).get(f"refs/heads/{branch}")


def worktree_folder(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "worktree"


def _inside(path: Path, folder: Path) -> bool:
    return path.resolve().is_relative_to(folder.resolve())


def branch_refs(repo: Path) -> dict[str, str]:
    """The repository's branches and tags, by ref, with their commits."""
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


# --- clones --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskClone:
    path: Path
    branch: str
    # The commit the run started from: commits after it are the ones this run made.
    start: str
    # The commit the branch grew from: commits after it are the work the branch holds.
    base: str
    # True when the branch existed in the repository: the run continues it.
    continued: bool
    # The branch's commit in the repository when the run started, None for a new branch:
    # the write-back only moves the branch on from there.
    tip: str | None


def prepare_clone(
    repo: Path,
    worktrees_dir: Path,
    branch: str,
    base: str,
    existing: str = "continue",
) -> TaskClone:
    """Clones the repository into the task's own folder in the worktree folder, checked out
    on the task's branch: as the repository has it, or new from `base`. `existing` decides
    what happens to a branch an earlier run left: `continue` works on from it, `refuse` raises
    `BranchExistsError`. A branch checked out in a worktree of the repository, such as the
    user's own checkout, is never taken over.

    A clone an earlier run left in the folder is worked on where its branch is where the
    repository has it, replaced where nothing in it would be lost, and refused otherwise. A
    worktree an earlier version of the worker left there is removed when it holds no
    changes, its branch staying, and refused otherwise."""
    if not is_valid_branch_name(repo, branch):
        raise GitError(f"Invalid branch name: {branch}")
    base_commit = resolve_commit(repo, base)
    path = _task_path(worktrees_dir, branch)
    _check_place(repo, path)
    _retire_worktree(repo, path)
    tip = branch_commit(repo, branch)
    checked_out = worktree_of_branch(repo, branch)
    if (checked_out is not None or tip is not None) and existing != "continue":
        where = f" in {checked_out}" if checked_out else ""
        raise BranchExistsError(f"Branch {branch} already exists{where}")
    if checked_out is not None:
        raise GitError(
            f"Branch {branch} is checked out in {checked_out}, not in the worker's {path}; "
            "not working in someone else's checkout",
        )
    if not _reusable(repo, worktrees_dir, path, branch, tip):
        _clone(repo, path)
        git(path, "checkout", "--quiet", "--no-track", "-B", branch, tip or base_commit)
    return TaskClone(
        path=path,
        branch=branch,
        start=git(path, "rev-parse", "HEAD"),
        base=base_commit,
        continued=tip is not None,
        tip=tip,
    )


def _task_path(worktrees_dir: Path, branch: str) -> Path:
    """Where the worker clones the task's branch."""
    return (worktrees_dir / worktree_folder(branch)).resolve()


def _check_place(repo: Path, path: Path) -> None:
    if path == repo.resolve() or _inside(repo, path):
        raise GitError(f"Not using {path}: it holds the repository's own checkout")


def _retire_worktree(repo: Path, path: Path) -> None:
    """Removes the linked worktree an earlier version of the worker made at `path`, if any:
    its branch and commits stay in the repository. Git refuses where the worktree holds
    changes, and so does this."""
    if path not in worktree_paths(repo):
        return
    # Checked here rather than by `git worktree remove`, which looks into nested
    # repositories for changes, with a configuration the agent may have written.
    if has_changes(path):
        raise GitError(
            f"{path} is a worktree an earlier version of vp board work left, and it holds "
            f"changes; keep what you need, then remove it with "
            f"`git worktree remove --force {path}`",
        )
    git(repo, "worktree", "remove", "--force", str(path))


def _reusable(
    repo: Path,
    worktrees_dir: Path,
    path: Path,
    branch: str,
    tip: str | None,
) -> bool:
    """Whether the clone an earlier run left at `path` is worked on as it is: it is on the
    branch, at the commit the repository has it at, and may hold changes left uncommitted. A
    clone that holds nothing the repository lacks is removed instead, and one that holds
    more is refused."""
    if not path.exists() or not any(path.iterdir()):
        return False
    if not is_clone_of(path, repo):
        raise GitError(f"Worktree folder is in use: {path}")
    look = "look at what it holds, then remove it to start the task over in a new clone"
    try:
        _check_settings(path)
    except GitError as exc:
        raise GitError(f"The clone at {path} was changed ({exc}); {look}") from exc
    head = _run(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").stdout.strip()
    own = branch_commit(path, branch)
    if current_branch(path) == branch and head and head == tip:
        return True
    unsaved = [
        sha for sha in dict.fromkeys(filter(None, (head, own))) if not _in_repository(repo, sha)
    ]
    if unsaved:
        raise GitError(
            f"The clone at {path} holds commits that are not in {repo} "
            f"({', '.join(sha[:12] for sha in unsaved)}); {look}",
        )
    if has_changes(path):
        raise GitError(f"The clone at {path} holds uncommitted changes; {look}")
    remove_clone(worktrees_dir, path)
    return False


def _in_repository(repo: Path, sha: str) -> bool:
    """Whether a branch or tag of the repository holds the commit."""
    if not commit_exists(repo, sha):
        return False
    result = _run(repo, "for-each-ref", "--count=1", "--contains", sha, "refs/heads", "refs/tags")
    return result.returncode == 0 and bool(result.stdout.strip())


def _clone(repo: Path, path: Path) -> None:
    """A clone of the repository that shares its objects, with nothing checked out. It
    commits as the repository does: with the identity the repository sets, if any."""
    if path.exists() and any(path.iterdir()):
        raise GitError(f"Worktree folder is in use: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    identity: list[str] = []
    for key in ("user.name", "user.email"):
        value = _run(repo, "config", "--local", key).stdout.strip()
        if value:
            identity += ["--config", f"{key}={value}"]
    git(
        path.parent,
        "clone",
        "--quiet",
        "--shared",
        "--no-checkout",
        "--no-recurse-submodules",
        *identity,
        str(repo),
        str(path),
    )


def is_clone_of(path: Path, repo: Path) -> bool:
    """Whether `path` is a clone of the repository, read without running git there."""
    config = path / ".git" / "config"
    if (path / ".git").is_symlink() or not config.is_file() or config.is_symlink():
        return False
    try:
        url = dict(_settings(path)).get("remote.origin.url")
    except GitError:
        return False
    if not url:
        return False
    with_repo = Path(url) if Path(url).is_absolute() else path / url
    try:
        return with_repo.resolve() == repo.resolve()
    except OSError:
        return False


def _settings(path: Path) -> list[tuple[str, str]]:
    """The settings in the clone's own configuration, read as a file: includes are not
    followed and nothing in it runs."""
    output = git(path.parent, "config", "--file", str(path / ".git" / "config"), "-z", "--list")
    return [
        (key.lower(), value)
        for key, _, value in (entry.partition("\n") for entry in output.split("\0") if entry)
    ]


def _check_settings(path: Path) -> None:
    """Refuses a clone whose git directory is not as the worker made it, before git runs in
    it."""
    state = pointers(path)
    if state[".git"] != "directory" or state["objects"] != "directory" or state["commondir"]:
        raise GitError("its git directory was replaced")
    unknown = sorted({key for key, _ in _settings(path) if not CLONE_SETTINGS.fullmatch(key)})
    if unknown:
        raise GitError(f"its configuration sets {', '.join(unknown)}")


def prepare_review_clone(repo: Path, worktrees_dir: Path, name: str, commit: str) -> Path:
    """Clones the repository for a review with `commit` checked out, detached: no branch of
    the repository is created, checked out or moved, and several reviewers of one task each
    get a clone of their own. A review clone an earlier run left at the same place in the
    worktree folder is replaced, as is a review worktree of an earlier version."""
    path = (worktrees_dir / worktree_folder(name)).resolve()
    if path == repo.resolve():
        raise GitError(f"Not replacing {path}: it is the repository's own checkout")
    _check_place(repo, path)
    if path in worktree_paths(repo):
        if not _inside(path, worktrees_dir):
            raise GitError(f"Not replacing {path}: it is outside {worktrees_dir}")
        git(repo, "worktree", "remove", "--force", str(path))
    elif path.exists() and any(path.iterdir()) and is_clone_of(path, repo):
        remove_clone(worktrees_dir, path)
    _clone(repo, path)
    git(path, "checkout", "--quiet", "--detach", commit)
    return path


def remove_clone(worktrees_dir: Path, path: Path) -> None:
    """Removes a clone the worker made; what it brought back stays in the repository."""
    if not _inside(path, worktrees_dir) or path.resolve() == worktrees_dir.resolve():
        raise GitError(f"Not removing {path}: it is outside {worktrees_dir}")
    try:
        _remove_tree(path)
    except OSError as exc:
        raise GitError(f"Could not remove {path}: {exc}") from exc


def _remove_tree(path: Path) -> None:
    """Removes a folder, with the read-only files git makes of its objects on Windows."""

    def writable(function: Callable[..., Any], name: str, _: Any) -> None:
        os.chmod(name, stat.S_IWRITE)
        function(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def write_back(repo: Path, clone: TaskClone) -> str:
    """Brings the task's branch from the clone into the repository and says its commit. The
    branch only moves forward: where it moved in the repository during the run, to where the
    clone's branch does not continue from, it is left as it is and this fails. So is a branch
    checked out in a worktree of the repository meanwhile. No tag or other branch of the
    clone comes along."""
    tip = git(clone.path, "rev-parse", "--verify", f"refs/heads/{clone.branch}^{{commit}}")
    if branch_commit(repo, clone.branch) == tip:
        return tip
    ref = f"refs/heads/{clone.branch}"
    result = _run(
        repo,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "--no-recurse-submodules",
        str(clone.path),
        f"{ref}:{ref}",
    )
    if result.returncode == 0:
        return tip
    now = branch_commit(repo, clone.branch)
    if now != clone.tip:
        was = clone.tip[:12] if clone.tip else "no branch"
        raise GitError(
            f"The branch {clone.branch} moved in {repo} during the run (from {was} to "
            f"{now[:12] if now else 'deleted'}), and the run's work does not continue from "
            f"there; not overwriting it. The work is in {clone.path}",
        )
    detail = (result.stderr or result.stdout).strip()
    raise GitError(
        f"Could not bring the branch {clone.branch} back into {repo}: {detail}. The work is "
        f"in {clone.path}",
    )


# --- the agent container ---------------------------------------------------------------


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


def alternates(clone: Path) -> list[Path]:
    """The object directories the clone borrows from: the repository's."""
    objects = clone / ".git" / "objects"
    file = objects / "info" / "alternates"
    if not file.is_file():
        return []
    found = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            found.append((objects / line).resolve())
    return found


def agent_mounts(
    clone: Path,
    workspace_mount: str = WORKSPACE,
    read_only: bool = False,
) -> list[tuple[str, str, str]]:
    """The volumes the agent container needs besides the clone: the repository's objects
    the clone borrows, read-only, at the paths the clone names them by. In the clone, what
    makes git run programs (config, hooks, info) and the pointer to the borrowed objects are
    read-only, since git on the host reads them after the run. A reviewing agent, which must
    not commit, gets all of the clone's git directory read-only.

    On a Windows host the clone names the borrowed objects by their Windows paths: the
    container gets a pointer that names them by their paths there."""
    dot_git = clone / ".git"
    inside = f"{workspace_mount}/.git"
    borrowed = alternates(clone)
    mounts = [(str(path), container_path(path), "ro") for path in borrowed]
    if read_only:
        mounts.append((str(dot_git), inside, "ro"))
    else:
        for name in ("hooks", "info"):
            (dot_git / name).mkdir(exist_ok=True)
            mounts.append((str(dot_git / name), f"{inside}/{name}", "ro"))
        mounts.append((str(dot_git / "config"), f"{inside}/config", "ro"))
    if borrowed:
        pointer = dot_git / "objects" / "info" / "alternates"
        if any(container_path(path) != str(path) for path in borrowed):
            pointer = dot_git / CONTAINER_ALTERNATES
            pointer.write_text(
                "".join(f"{container_path(path)}\n" for path in borrowed),
                encoding="utf-8",
            )
        mounts.append((str(pointer), f"{inside}/objects/info/alternates", "ro"))
    return mounts


def _kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    return "file" if path.exists() else "missing"


def _content(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def pointers(clone: Path) -> dict[str, str]:
    """What tells git on the host where the clone's repository is and how to work in it:
    its git directory, configuration, and the objects it borrows."""
    dot_git = clone / ".git"
    return {
        ".git": _kind(dot_git),
        "objects": _kind(dot_git / "objects"),
        "commondir": _content(dot_git / "commondir"),
        "config": _content(dot_git / "config"),
        "alternates": _content(dot_git / "objects" / "info" / "alternates"),
    }


def verify_pointers(clone: Path, before: dict[str, str]) -> None:
    """Refuses a clone whose git directory or configuration changed: git would then read a
    configuration the agent wrote, and could run programs from it on the host."""
    try:
        after = pointers(clone)
    except OSError as exc:
        raise GitError(f"The agent changed the clone's git metadata: {exc}") from exc
    changed = [name for name, value in before.items() if after.get(name) != value]
    if changed:
        raise GitError(f"The agent changed the clone's git metadata ({', '.join(changed)})")


# --- work in a clone -------------------------------------------------------------------


def commits_since(path: Path, start: str) -> list[dict[str, str]]:
    """The commits on the clone's branch after `start`, oldest first."""
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


# --- pushing a branch ------------------------------------------------------------------

# How long a push may take before it is given up.
PUSH_TIMEOUT_SECONDS = 120
# Variables that would make git or ssh ask someone for a password: a background worker has
# nobody to answer, so it fails instead of waiting for an answer.
PROMPT_ENV = frozenset({"GIT_ASKPASS", "SSH_ASKPASS"})
# The longest error a failed push reports.
PUSH_ERROR_CHARS = 300
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", re.IGNORECASE)
# Credentials in a URL, such as https://user:token@host/…, kept out of what is reported.
_URL_CREDENTIALS = re.compile(r"(?<=://)[^/@\s]+@")


class PushError(GitError):
    """A push that did not go through, with a short reason fit to report: non-fast-forward,
    authentication, network, missing commit or another refusal."""


def normalize_remote_url(url: str) -> str:
    """A remote URL reduced to host and path, so that the https and ssh forms of one GitHub
    repository compare equal: `https://github.com/Owner/Repo.git`,
    `git@github.com:owner/repo` and `ssh://git@github.com/owner/repo` all become
    `github.com/owner/repo`."""
    text = url.strip().lower()
    scheme, sep, rest = text.partition("://")
    if sep:
        authority, _, path = rest.partition("/")
        host = authority.rpartition("@")[2]
        # A port belongs to the transport, not to the repository.
        if scheme in {"ssh", "git+ssh", "https", "http", "git"}:
            host = host.split(":", 1)[0]
    elif ":" in text and not re.match(r"[a-z]:[\\/]", text):
        # scp-like syntax: [user@]host:path.
        authority, _, path = text.partition(":")
        host = authority.rpartition("@")[2]
    else:
        return text.rstrip("/\\").removesuffix(".git")
    path = path.strip("/").removesuffix(".git").rstrip("/")
    return f"{host}/{path}"


def remotes(repo: Path) -> dict[str, str]:
    """The repository's remotes and their URLs."""
    result = _run(repo, "config", "-z", "--get-regexp", r"^remote\..*\.url$")
    found: dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        key, _, url = entry.partition("\n")
        name = key[len("remote.") : -len(".url")] if key.startswith("remote.") else ""
        if name and url:
            found.setdefault(name, url)
    return found


def push_remote(repo: Path, remote_url: str | None) -> str:
    """The remote to push to: the one whose URL is the given one, else `origin`."""
    if remote_url:
        wanted = normalize_remote_url(remote_url)
        for name, url in remotes(repo).items():
            if normalize_remote_url(url) == wanted:
                return name
    return "origin"


def push_env(hidden: tuple[str, ...] = (), ssh_configured: bool = False) -> dict[str, str]:
    """The environment for a push: no variable that points git elsewhere or would make it
    prompt, no board setting, and no value in `hidden`, such as the board token. Terminal
    prompts are off, and ssh fails rather than asking for a passphrase or host key, unless
    the environment or the repository (`ssh_configured`) says how to run it."""
    env = {
        key: value
        for key, value in git_env().items()
        if key not in PROMPT_ENV
        and not key.startswith("VP_BOARD_")
        and not (value and value in hidden)
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["SSH_ASKPASS_REQUIRE"] = "never"
    if not ssh_configured and "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def push_branch(
    repo: Path,
    branch: str,
    commit: str | None,
    remote_url: str | None = None,
    *,
    timeout: float = PUSH_TIMEOUT_SECONDS,
    hidden: tuple[str, ...] = (),
) -> str:
    """Pushes `commit`, or the branch's tip when None, to `refs/heads/<branch>` on the remote
    whose URL is `remote_url`, else `origin`, and says the commit pushed. Never forced: a
    remote branch the commit does not continue is left as it is, and the push fails as
    non-fast-forward. Pushed from the repository itself, with its remotes and the user's git
    credentials; hooks do not run, and nothing prompts. Raises `PushError` with a short
    reason."""
    if branch.startswith("-") or not is_valid_branch_name(repo, branch):
        raise PushError(f"invalid branch name: {branch}")
    if commit:
        if not _SHA.fullmatch(commit) or not commit_exists(repo, commit):
            raise PushError(f"missing commit: {commit[:12]} is not in the repository")
        sha = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    else:
        found = branch_commit(repo, branch)
        if found is None:
            raise PushError(f"missing commit: there is no branch {branch} in the repository")
        sha = found
    remote = push_remote(repo, remote_url)
    ssh_configured = bool(_run(repo, "config", "core.sshCommand").stdout.strip())
    try:
        result = subprocess.run(
            [
                "git",
                *SAFE_CONFIG,
                # No askpass program either: git then fails where it would ask.
                "-c",
                "core.askPass=",
                "-c",
                "push.recurseSubmodules=no",
                "-c",
                "push.followTags=false",
                "push",
                "--porcelain",
                "--no-verify",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "--",
                remote,
                f"{sha}:refs/heads/{branch}",
            ],
            cwd=repo,
            env=push_env(hidden, ssh_configured),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PushError(
            f"network: the push to {remote} timed out after {int(timeout)}s",
        ) from exc
    except OSError as exc:
        raise PushError(f"git could not run: {exc}") from exc
    if result.returncode != 0:
        raise PushError(push_failure(remote, branch, f"{result.stdout}\n{result.stderr}"))
    return sha


# What git and the transports say when a push fails, by the kind of failure.
_NON_FAST_FORWARD = re.compile(r"non-fast-forward|\(fetch first\)|\[rejected\]", re.IGNORECASE)
_AUTH = re.compile(
    r"authentication failed|could not read (?:username|password)|terminal prompts disabled"
    r"|permission denied|permission to .* denied|invalid username or password|\b403\b|\b401\b"
    r"|host key verification failed|requested url returned error: 40[13]",
    re.IGNORECASE,
)
_NETWORK = re.compile(
    r"could not resolve host|could not resolve hostname|connection (?:refused|reset|timed out)"
    r"|timed out|network is unreachable|failed to connect|unable to access|empty reply"
    r"|could not read from remote repository|the remote end hung up",
    re.IGNORECASE,
)
_NO_REMOTE = re.compile(r"does not appear to be a git repository|no such remote", re.IGNORECASE)
_REMOTE_REFUSED = re.compile(r"\[remote rejected\]\s*\(([^)]*)\)", re.IGNORECASE)


def push_failure(remote: str, branch: str, output: str) -> str:
    """A short reason for a failed push from git's output, without credentials."""
    text = _URL_CREDENTIALS.sub("***@", output)
    if _NON_FAST_FORWARD.search(text) and "remote rejected" not in text.lower():
        reason = (
            f"non-fast-forward: {branch} on {remote} has commits the pushed commit does not "
            "contain; not forcing it"
        )
    elif _AUTH.search(text):
        reason = (
            f"authentication: {remote} refused the credentials, or none are set up for git "
            "on this host"
        )
    elif _NO_REMOTE.search(text):
        reason = f"no remote: the repository has no remote {remote}"
    elif _NETWORK.search(text):
        reason = f"network: {remote} could not be reached"
    elif match := _REMOTE_REFUSED.search(text):
        reason = f"refused by {remote}: {match.group(1).strip()}"
    else:
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith(("To ", "Done"))
        ]
        reason = f"push failed: {lines[-1] if lines else 'git gave no reason'}"
    detail = "" if reason.startswith("non-fast-forward") else _first_error(text)
    if detail and detail not in reason:
        reason += f" ({detail})"
    return reason[:PUSH_ERROR_CHARS]


def _first_error(text: str) -> str:
    """git's own first error line, such as `fatal: …`, if any."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("fatal:", "error:", "remote:", "ERROR:")):
            return line
    return ""

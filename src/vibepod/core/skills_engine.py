"""Driver that calls the vibepod-skills-engine container."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from vibepod.constants import (
    PROJECT_SKILLS_DIR,
    SKILLS_CACHE_DIR,
    SKILLS_ENGINE_IMAGE,
    USER_SKILLS_DIR,
)
from vibepod.core.config import get_config
from vibepod.core.docker import DockerClientError, DockerManager, NotFound

Scope = Literal["local", "user"]

# Two or more characters before the colon: every locator scheme we support is
# longer than one character, so a single letter is a Windows drive (``C:\...``).
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")

# scp-style git remote: user@host:path. A bare "git@something" with no remote
# separator is a directory name, not a locator.
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:")

_SAFE_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_skills_engine_checked = False
_manager: DockerManager | None = None


class SkillsEngineError(RuntimeError):
    """Raised when the driver cannot return an engine result."""


def _get_manager() -> DockerManager:
    """Shared DockerManager: connects to Docker or a discovered Podman socket."""
    global _manager
    if _manager is None:
        try:
            _manager = DockerManager()
        except DockerClientError as exc:
            raise SkillsEngineError(str(exc)) from exc
        except Exception as exc:
            raise SkillsEngineError(f"Docker initialization failed: {exc}") from exc
    return _manager


@dataclass(frozen=True)
class EngineResult:
    exit_code: int
    stdout: str
    stderr: str
    data: Any | None  # parsed --json payload, when present


def detect_scope_default(cwd: Path | None = None) -> Scope:
    """Local when invoked from inside a `.vibepod` project, else user."""
    return "local" if _project_root(cwd) is not None else "user"


def _project_root(cwd: Path | None = None) -> Path | None:
    here = Path(cwd or Path.cwd()).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".vibepod").is_dir():
            return parent
    return None


def local_skills_dir(cwd: Path | None = None) -> Path:
    root = _project_root(cwd)
    if root is not None:
        return root / PROJECT_SKILLS_DIR
    return Path(cwd or Path.cwd()).resolve() / PROJECT_SKILLS_DIR


def user_skills_dir() -> Path:
    return USER_SKILLS_DIR


def cache_dir() -> Path:
    return SKILLS_CACHE_DIR


def _local_mount_dir(cwd: Path | None, *, local_required: bool) -> Path:
    if _project_root(cwd) is not None or local_required:
        return local_skills_dir(cwd)
    return cache_dir() / "empty-local-skills"


def _ensure_dirs(
    cwd: Path | None = None,
    *,
    local_required: bool = False,
) -> tuple[Path, Path, Path]:
    local = _local_mount_dir(cwd, local_required=local_required)
    user = user_skills_dir()
    cache = cache_dir()
    for d in (local, user, cache):
        d.mkdir(parents=True, exist_ok=True)
    return local, user, cache


def is_safe_skill_id(skill_id: str) -> bool:
    """Return True for skill IDs safe to use as one path segment."""
    return bool(_SAFE_SKILL_ID_RE.fullmatch(skill_id))


@dataclass(frozen=True)
class InstalledSkill:
    scope: Scope
    path: Path  # absolute host path to the skill folder


def _string_keyed_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _read_lock(path: Path) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"skills": {}}
    return _string_keyed_dict(raw) or {"skills": {}}


def _safe_skill_path(scope_root: Path, skill_id: str, path_value: object) -> Path | None:
    rel = path_value if isinstance(path_value, str) and path_value else f"installed/{skill_id}"
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        rel_path = Path("installed") / skill_id
    abs_path = (scope_root / rel_path).resolve(strict=False)
    if not abs_path.is_relative_to(scope_root) or not abs_path.is_dir():
        return None
    return abs_path


def installed_skills(workspace: Path, scope: Scope | None = None) -> dict[str, InstalledSkill]:
    """Installed skills from the local + user lockfiles (local wins), or one scope.

    Reads the lockfiles directly so this stays cheap during `vp run` (no engine
    container call).
    """
    roots: list[tuple[Scope, Path]] = [
        ("user", user_skills_dir().resolve()),
        ("local", local_skills_dir(workspace).resolve()),  # processed second → wins
    ]
    merged: dict[str, InstalledSkill] = {}
    for root_scope, scope_root in roots:
        if scope is not None and root_scope != scope:
            continue
        skills = _string_keyed_dict(_read_lock(scope_root / "skills-lock.json").get("skills"))
        if skills is None:
            continue
        for sid, raw_entry in skills.items():
            if not is_safe_skill_id(sid):
                continue
            entry = _string_keyed_dict(raw_entry)
            if entry is None:
                continue
            abs_path = _safe_skill_path(scope_root, sid, entry.get("path"))
            if abs_path is not None:
                merged[sid] = InstalledSkill(scope=root_scope, path=abs_path)
    return merged


def _installed_source_paths(workspace: Path) -> set[Path]:
    """Every installed skill folder in either scope, as resolved and as installed.

    The installed path (``<scope>/installed/<id>``) matters on its own for
    linked installs, where it is a symlink to the real source elsewhere.
    """
    paths: set[Path] = set()
    roots: list[tuple[Scope, Path]] = [
        ("user", user_skills_dir().resolve()),
        ("local", local_skills_dir(workspace).resolve()),
    ]
    for root_scope, scope_root in roots:
        for sid, skill in installed_skills(workspace, root_scope).items():
            paths.add(skill.path)
            paths.add(scope_root / "installed" / sid)
    return paths


def export(
    dest: Path,
    *,
    scope: Scope | None = None,
    skill_ids: list[str] | None = None,
    force: bool = False,
    cwd: Path | None = None,
) -> dict[str, InstalledSkill]:
    """Copy installed skills as plain folders to ``dest/<id>/``.

    Without *scope* this exports what an agent would see (local shadows user).
    Files are copied as they are installed; symlinks inside a skill stay
    symlinks so a link never pulls host files from outside the skill into the
    export. Nothing is written until every requested skill is known to fit.
    """
    workspace = Path(cwd or Path.cwd()).resolve()
    skills = installed_skills(workspace, scope)
    if skill_ids:
        missing = [sid for sid in skill_ids if sid not in skills]
        if missing:
            where = f"in scope {scope}" if scope else "in local or user scope"
            raise SkillsEngineError(f"Skill(s) not installed {where}: {', '.join(missing)}")
        skills = {sid: skills[sid] for sid in skill_ids}

    if not skills:
        return skills

    dest = dest.expanduser().resolve()
    for skill in skills.values():
        if dest.is_relative_to(skill.path):
            raise SkillsEngineError(f"Export destination {dest} is inside skill {skill.path}")
    if dest.exists() and not dest.is_dir():
        raise SkillsEngineError(f"Export destination is not a directory: {dest}")
    existing = [sid for sid in skills if (dest / sid).exists() or (dest / sid).is_symlink()]
    if existing and not force:
        raise SkillsEngineError(
            f"Already present in {dest}: {', '.join(existing)} (use --force to overwrite)",
        )

    protected = _installed_source_paths(workspace)
    for sid in skills:
        target = dest / sid
        for source in protected:
            if target == source or target.is_relative_to(source) or source.is_relative_to(target):
                raise SkillsEngineError(
                    f"Export target {target} overlaps installed skill {source}",
                )

    dest.mkdir(parents=True, exist_ok=True)
    for sid, skill in skills.items():
        target = dest / sid
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill.path, target, symlinks=True)
    return skills


def _is_local_locator(locator: str) -> bool:
    """Local when the locator carries no scheme.

    Covers ``./skills/foo``, ``../foo``, ``/abs/path``, ``skills/foo``, ``.``,
    ``..`` and ``~/skills/foo``. Anything scheme-like (``github:``, ``npm:``,
    ``https://``, ``ftp://``) is left to the engine, which rejects the ones it
    does not support. scp-style git remotes (``git@host:org/repo.git``) are the
    one scheme-less exception; a directory named ``git@foo`` stays local.
    """
    if _SCP_RE.match(locator):
        return False
    return not _SCHEME_RE.match(locator)


def _normalize_locator(locator: str) -> str:
    """Accept common GitHub web URLs by converting them to skill locators.

    Also expands a leading ``~`` on the host: the engine container has a
    different home directory, so ``~`` cannot survive the boundary.
    """
    if locator == "~" or locator.startswith("~/"):
        return str(Path(locator).expanduser())

    parsed = urlparse(locator)
    if parsed.scheme not in {"http", "https"}:
        return locator
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return locator

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "tree":
        return locator

    owner, repo, _, ref, *subpath = parts
    repo = repo.removesuffix(".git")
    normalized = f"github:{owner}/{repo}"
    if subpath:
        normalized += f"//{'/'.join(subpath)}"
    return f"{normalized}#{ref}"


def run_engine(
    args: list[str],
    *,
    json_output: bool = True,
    cwd: Path | None = None,
    extra_mounts: list[tuple[Path, str, str]] | None = None,
    local_required: bool = False,
    working_dir: Path | None = None,
) -> EngineResult:
    """Invoke the engine container with the standard mount layout.

    Returns the parsed JSON payload if ``json_output`` is True. Stderr from the
    engine (human-readable progress) is always captured but never parsed.
    """
    global _skills_engine_checked
    manager = _get_manager()
    if not _skills_engine_checked:
        try:
            image_exists = False
            try:
                manager.client.images.get(SKILLS_ENGINE_IMAGE)
                image_exists = True
            except NotFound:
                pass

            config = get_config()
            auto_pull_enabled = bool(config.get("auto_pull", True))
            is_latest = ":" not in SKILLS_ENGINE_IMAGE.split("/")[
                -1
            ] or SKILLS_ENGINE_IMAGE.endswith(":latest")

            if not image_exists:
                manager.pull_image(
                    SKILLS_ENGINE_IMAGE,
                    auto_clean=bool(config.get("auto_clean", True)),
                )
            elif auto_pull_enabled and is_latest:
                try:
                    from vibepod.utils.console import info

                    info("Checking for skills-engine image updates…")
                    manager.pull_if_newer(
                        SKILLS_ENGINE_IMAGE,
                        auto_clean=bool(config.get("auto_clean", True)),
                    )
                except Exception:
                    pass
            _skills_engine_checked = True
        except DockerClientError as exc:
            raise SkillsEngineError(str(exc)) from exc
        except Exception as exc:
            raise SkillsEngineError(f"Docker initialization failed: {exc}") from exc

    local, user, cache = _ensure_dirs(cwd, local_required=local_required)

    volumes: dict[str, dict[str, str]] = {
        str(local): {"bind": "/vibepod/local-skills", "mode": "rw"},
        str(user): {"bind": "/vibepod/user-skills", "mode": "rw"},
        str(cache): {"bind": "/vibepod/cache", "mode": "rw"},
    }
    for host_path, container_path, mode in extra_mounts or []:
        volumes[str(host_path)] = {"bind": container_path, "mode": mode}

    # Pass through trusted-source allowlist if set on host.
    environment: dict[str, str] | None = None
    if "VIBEPOD_TRUSTED_SOURCES" in os.environ:
        environment = {"VIBEPOD_TRUSTED_SOURCES": os.environ["VIBEPOD_TRUSTED_SOURCES"]}

    command: list[str] = []
    if json_output:
        command.append("--json")
    command.extend(args)

    try:
        container = manager.client.containers.create(
            SKILLS_ENGINE_IMAGE,
            command=command,
            volumes=volumes,
            working_dir=str(working_dir) if working_dir is not None else None,
            environment=environment,
        )
    except Exception as exc:
        raise SkillsEngineError(f"Failed to create skills-engine container: {exc}") from exc

    try:
        container.start()
        status = container.wait()
        exit_code = int(status.get("StatusCode", 1)) if isinstance(status, dict) else int(status)
        stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
        stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
    except Exception as exc:
        raise SkillsEngineError(f"skills-engine container failed: {exc}") from exc
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass

    payload: Any | None = None
    if json_output and stdout.strip():
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SkillsEngineError(
                f"Engine returned non-JSON output (exit={exit_code}): {stdout!r}",
            ) from exc

    return EngineResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        data=payload,
    )


def add(
    locator: str,
    *,
    scope: Scope,
    skill_id: str | None = None,
    link: bool = False,
    cwd: Path | None = None,
) -> EngineResult:
    locator = _normalize_locator(locator)
    args = ["add", locator, "--scope", scope]
    if skill_id:
        args.extend(["--id", skill_id])
    if link:
        args.append("--link")

    extra: list[tuple[Path, str, str]] = []
    working_dir: Path | None = None
    if _is_local_locator(locator):
        locator_path = Path(locator)
        base = Path(cwd) if cwd is not None else Path.cwd()
        host = (locator_path if locator_path.is_absolute() else base / locator_path).resolve()
        if not host.exists():
            raise SkillsEngineError(f"Local skill locator not found: {host}")
        extra.append((host, str(host), "ro"))
        working_dir = base.resolve()
    return run_engine(
        args,
        cwd=cwd,
        extra_mounts=extra,
        local_required=scope == "local",
        working_dir=working_dir,
    )


def delete(skill_id: str, *, scope: Scope, cwd: Path | None = None) -> EngineResult:
    return run_engine(
        ["delete", skill_id, "--scope", scope],
        cwd=cwd,
        local_required=scope == "local",
    )


def list_skills(scope: Scope | None = None, *, cwd: Path | None = None) -> EngineResult:
    args = ["list"]
    if scope:
        args.extend(["--scope", scope])
    return run_engine(args, cwd=cwd, local_required=scope == "local")


def sync(scope: Scope, *, cwd: Path | None = None) -> EngineResult:
    return run_engine(["sync", "--scope", scope], cwd=cwd, local_required=scope == "local")


def update(scope: Scope, skill_id: str | None = None, *, cwd: Path | None = None) -> EngineResult:
    args = ["update"]
    if skill_id:
        args.append(skill_id)
    args.extend(["--scope", scope])
    return run_engine(args, cwd=cwd, local_required=scope == "local")


def resolve(scope: Scope | None = None, *, cwd: Path | None = None) -> EngineResult:
    args = ["resolve"]
    if scope:
        args.extend(["--scope", scope])
    return run_engine(args, cwd=cwd, local_required=scope == "local")


def cache_clear() -> EngineResult:
    return run_engine(["cache", "clear"])

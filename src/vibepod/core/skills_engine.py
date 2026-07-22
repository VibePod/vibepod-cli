"""Driver that calls the vibepod-skills-engine container."""

from __future__ import annotations

import json
import os
import subprocess
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

_skills_engine_checked = False


class SkillsEngineError(RuntimeError):
    """Raised when the driver cannot return an engine result."""


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
    cwd: Path | None = None, *, local_required: bool = False
) -> tuple[Path, Path, Path]:
    local = _local_mount_dir(cwd, local_required=local_required)
    user = user_skills_dir()
    cache = cache_dir()
    for d in (local, user, cache):
        d.mkdir(parents=True, exist_ok=True)
    return local, user, cache


def _is_local_locator(locator: str) -> bool:
    return locator.startswith("./") or locator.startswith("../") or locator.startswith("/")


def _normalize_locator(locator: str) -> str:
    """Accept common GitHub web URLs by converting them to skill locators."""
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
    if not _skills_engine_checked:
        try:
            manager = DockerManager()
            image_exists = False
            try:
                manager.client.images.get(SKILLS_ENGINE_IMAGE)
                image_exists = True
            except NotFound:
                pass

            config = get_config()
            auto_pull_enabled = bool(config.get("auto_pull", True))
            is_latest = (
                ":" not in SKILLS_ENGINE_IMAGE.split("/")[-1]
                or SKILLS_ENGINE_IMAGE.endswith(":latest")
            )

            if not image_exists:
                manager.pull_image(SKILLS_ENGINE_IMAGE)
            elif auto_pull_enabled and is_latest:
                try:
                    from vibepod.utils.console import info
                    info("Checking for skills-engine image updates…")
                    manager.pull_if_newer(
                        SKILLS_ENGINE_IMAGE,
                        remove_previous=bool(config.get("auto_clean", True)),
                    )
                except Exception:
                    pass
            _skills_engine_checked = True
        except DockerClientError as exc:
            raise SkillsEngineError(str(exc)) from exc
        except Exception as exc:
            raise SkillsEngineError(f"Docker initialization failed: {exc}") from exc

    local, user, cache = _ensure_dirs(cwd, local_required=local_required)

    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{local}:/vibepod/local-skills",
        "-v",
        f"{user}:/vibepod/user-skills",
        "-v",
        f"{cache}:/vibepod/cache",
    ]
    for host_path, container_path, mode in extra_mounts or []:
        cmd.extend(["-v", f"{host_path}:{container_path}:{mode}"])
    if working_dir is not None:
        cmd.extend(["-w", str(working_dir)])

    # Pass through trusted-source allowlist if set on host.
    if "VIBEPOD_TRUSTED_SOURCES" in os.environ:
        cmd.extend(["-e", f"VIBEPOD_TRUSTED_SOURCES={os.environ['VIBEPOD_TRUSTED_SOURCES']}"])

    cmd.append(SKILLS_ENGINE_IMAGE)
    if json_output:
        cmd.append("--json")
    cmd.extend(args)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise SkillsEngineError(f"docker not found on PATH: {exc}") from exc

    payload: Any | None = None
    if json_output and proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise SkillsEngineError(
                f"Engine returned non-JSON output (exit={proc.returncode}): {proc.stdout!r}"
            ) from exc

    return EngineResult(
        exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, data=payload
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
        ["delete", skill_id, "--scope", scope], cwd=cwd, local_required=scope == "local"
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

"""Driver that calls the vibepod-skills-engine container."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vibepod.constants import (
    PROJECT_SKILLS_DIR,
    SKILLS_CACHE_DIR,
    SKILLS_ENGINE_IMAGE,
    USER_SKILLS_DIR,
)

Scope = Literal["local", "user"]


class SkillsEngineError(RuntimeError):
    """Raised when the skills engine container exits non-zero or output is malformed."""


@dataclass(frozen=True)
class EngineResult:
    exit_code: int
    stdout: str
    stderr: str
    data: Any | None  # parsed --json payload, when present


def detect_scope_default(cwd: Path | None = None) -> Scope:
    """Local when invoked from inside a `.vibepod` project, else user."""
    here = Path(cwd or Path.cwd()).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".vibepod").is_dir():
            return "local"
    return "user"


def local_skills_dir(cwd: Path | None = None) -> Path:
    here = Path(cwd or Path.cwd()).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".vibepod").is_dir():
            return parent / PROJECT_SKILLS_DIR
    return here / PROJECT_SKILLS_DIR


def user_skills_dir() -> Path:
    return USER_SKILLS_DIR


def cache_dir() -> Path:
    return SKILLS_CACHE_DIR


def _ensure_dirs(scope: Scope, cwd: Path | None = None) -> tuple[Path, Path, Path]:
    local = local_skills_dir(cwd)
    user = user_skills_dir()
    cache = cache_dir()
    # Only the relevant scope must exist, but mounting both is fine.
    for d in (local, user, cache):
        d.mkdir(parents=True, exist_ok=True)
    return local, user, cache


def _is_local_locator(locator: str) -> bool:
    return locator.startswith("./") or locator.startswith("../") or locator.startswith("/")


def run_engine(
    args: list[str],
    *,
    json_output: bool = True,
    cwd: Path | None = None,
    extra_mounts: list[tuple[Path, str]] | None = None,
) -> EngineResult:
    """Invoke the engine container with the standard mount layout.

    Returns the parsed JSON payload if ``json_output`` is True. Stderr from the
    engine (human-readable progress) is always captured but never parsed.
    """
    local, user, cache = _ensure_dirs("local", cwd)

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
    for host_path, container_path in extra_mounts or []:
        cmd.extend(["-v", f"{host_path}:{container_path}"])

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
    args = ["add", locator, "--scope", scope]
    if skill_id:
        args.extend(["--id", skill_id])
    if link:
        args.append("--link")

    extra: list[tuple[Path, str]] = []
    if _is_local_locator(locator):
        host = Path(locator).resolve()
        extra.append((host, "/vibepod/source-in"))
        # Replace user-facing locator with the in-container mount path so the
        # engine sees a bare absolute path it can read.
        args[1] = "/vibepod/source-in"
    return run_engine(args, cwd=cwd, extra_mounts=extra)


def delete(skill_id: str, *, scope: Scope, cwd: Path | None = None) -> EngineResult:
    return run_engine(["delete", skill_id, "--scope", scope], cwd=cwd)


def list_skills(scope: Scope | None = None, *, cwd: Path | None = None) -> EngineResult:
    args = ["list"]
    if scope:
        args.extend(["--scope", scope])
    return run_engine(args, cwd=cwd)


def sync(scope: Scope, *, cwd: Path | None = None) -> EngineResult:
    return run_engine(["sync", "--scope", scope], cwd=cwd)


def update(scope: Scope, skill_id: str | None = None, *, cwd: Path | None = None) -> EngineResult:
    args = ["update"]
    if skill_id:
        args.append(skill_id)
    args.extend(["--scope", scope])
    return run_engine(args, cwd=cwd)


def resolve(scope: Scope | None = None, *, cwd: Path | None = None) -> EngineResult:
    args = ["resolve"]
    if scope:
        args.extend(["--scope", scope])
    return run_engine(args, cwd=cwd)

"""Opt-in process sandbox for agent containers, backed by Sydbox (``syd``).

The container stays the primary isolation boundary. With ``sandbox.type: syd``
the agent process tree additionally runs under syd, which intercepts its
system calls (seccomp user notification) and enforces a path/network policy
rendered by VibePod for each launch.

syd wraps the agent *command*, not the container: the image entrypoint still
runs as root (UID mapping, ``su`` to the runtime user) and then execs
``syd -P <policy> -- <agent argv>``. Sandboxing the entrypoint as well breaks
that privilege drop, because syd blocks setgroups/setregid transitions.
"""

from __future__ import annotations

import hashlib
import os
import struct
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vibepod.core.config import get_config_root

SANDBOX_TYPES = ("none", "syd")
SANDBOX_LABEL = "vibepod.sandbox"
SANDBOX_HELP = (
    "Process sandbox for the agent: none or syd (Sydbox); overrides sandbox.type for this run"
)

SYD_MOUNT_DIR = "/opt/vibepod-sandbox"
SYD_BINARY_MOUNT = f"{SYD_MOUNT_DIR}/syd"
SYD_POLICY_MOUNT = f"{SYD_MOUNT_DIR}/vibepod.syd-3"
SYD_LOG_LEVELS = ("emerg", "alert", "crit", "error", "warn", "notice", "info", "debug")
# syd needs seccomp user-notify with SECCOMP_USER_NOTIF_FLAG_CONTINUE plus
# pidfd_getfd/pidfd_send_signal; upstream documents 5.19 as the floor.
SYD_MIN_KERNEL = (5, 19)

# Every write-like category: write, create, delete, truncate, metadata changes
# and symlink creation. Rename and hard links are covered by create/delete.
_WRITE_CATEGORIES = "wpath,cpath,dpath,tpath,fattr,symlink"
# Always writable: temp dirs and the device/proc nodes every program opens for
# writing (/dev/null, /dev/tty, /proc/self/fd/*). Homes inside the image are
# writable so agents can keep caches there; the persisted homes are mounts.
_BASE_WRITABLE = ("/dev", "/proc", "/tmp", "/var/tmp", "/root", "/home")
# Files in a workspace that the *host* executes or obeys later: an agent able
# to write them could run code outside the container (git hooks, or
# core.hooksPath / core.fsmonitor in .git/config). The .git entry itself is
# protected too, so it cannot be renamed away and recreated.
_PROTECTED_WORKSPACE_ENTRIES = (".git", ".git/config", ".git/hooks/***")

_ELF_MAGIC = b"\x7fELF"
_PT_INTERP = 3
_ELF_MACHINES = {3: "x86", 40: "arm", 62: "x86_64", 183: "aarch64", 243: "riscv64"}


class SandboxError(ValueError):
    """Raised when the requested sandbox cannot be set up."""


@dataclass(frozen=True)
class SydSandbox:
    """A prepared syd launch: host files to mount and the command prefix."""

    binary: Path
    policy_path: Path

    def volumes(self) -> list[tuple[str, str, str]]:
        return [
            (str(self.binary), SYD_BINARY_MOUNT, "ro"),
            (str(self.policy_path), SYD_POLICY_MOUNT, "ro"),
        ]

    def command_prefix(self) -> list[str]:
        # No spaces or shell syntax: some image entrypoints re-parse argv
        # through `sh -c "$*"`.
        return [SYD_BINARY_MOUNT, "-P", SYD_POLICY_MOUNT, "--"]


def resolve_sandbox_type(config: Mapping[str, Any], requested: str | None = None) -> str:
    """Return the sandbox type for this launch: the flag wins over config."""
    raw = requested if requested is not None else _sandbox_cfg(config).get("type", "none")
    value = str(raw or "none").strip().lower()
    if value not in SANDBOX_TYPES:
        raise SandboxError(
            f"Unknown sandbox type '{raw}'. Supported: {', '.join(SANDBOX_TYPES)}.",
        )
    return value


def _sandbox_cfg(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = config.get("sandbox")
    return section if isinstance(section, Mapping) else {}


def syd_settings(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the validated ``sandbox.syd`` settings."""
    raw = _sandbox_cfg(config).get("syd")
    section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    binary = str(section.get("binary") or "").strip()
    log_level = str(section.get("log_level") or "warn").strip().lower()
    if log_level not in SYD_LOG_LEVELS:
        raise SandboxError(
            f"Invalid sandbox.syd.log_level '{log_level}'. "
            f"Use one of: {', '.join(SYD_LOG_LEVELS)}.",
        )
    rules = section.get("rules") or []
    if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
        raise SandboxError("sandbox.syd.rules must be a list of syd rule strings.")
    for rule in rules:
        if "\n" in rule or "\r" in rule:
            raise SandboxError(f"sandbox.syd.rules entry must be a single line: {rule!r}")
    return {"binary": binary, "log_level": log_level, "rules": [r.strip() for r in rules]}


def _elf_info(path: Path) -> tuple[str, bool]:
    """Return (architecture, is_static) for an ELF executable."""
    with path.open("rb") as handle:
        header = handle.read(64)
        if len(header) < 52 or header[:4] != _ELF_MAGIC:
            raise SandboxError(f"{path} is not a Linux ELF executable.")
        is_64 = header[4] == 2
        endian = "<" if header[5] == 1 else ">"
        machine = struct.unpack_from(f"{endian}H", header, 18)[0]
        if is_64:
            phoff = struct.unpack_from(f"{endian}Q", header, 32)[0]
            phentsize, phnum = struct.unpack_from(f"{endian}HH", header, 54)
        else:
            phoff = struct.unpack_from(f"{endian}I", header, 28)[0]
            phentsize, phnum = struct.unpack_from(f"{endian}HH", header, 42)
        has_interp = False
        for index in range(phnum):
            handle.seek(phoff + index * phentsize)
            entry = handle.read(4)
            if len(entry) == 4 and struct.unpack(f"{endian}I", entry)[0] == _PT_INTERP:
                has_interp = True
                break
    return _ELF_MACHINES.get(machine, f"machine {machine}"), not has_interp


def validate_syd_binary(value: str) -> tuple[Path, str]:
    """Check the configured syd binary and return (path, architecture)."""
    if not value:
        raise SandboxError(
            "sandbox type 'syd' needs a static syd binary. Download a release from "
            "https://distfiles.exherbo.org/#sydbox/ (verify the .sha512sum), then set "
            "`sandbox.syd.binary: /path/to/syd` in the VibePod config.",
        )
    path = Path(value).expanduser()
    if not path.is_file():
        raise SandboxError(
            f"syd binary not found at {path} (sandbox.syd.binary). "
            "Point it at the `bin/syd` file of a static syd release.",
        )
    if not os.access(path, os.X_OK):
        raise SandboxError(f"syd binary {path} is not executable. Run `chmod +x {path}`.")
    try:
        arch, static = _elf_info(path)
    except OSError as exc:
        raise SandboxError(f"Cannot read syd binary {path}: {exc}") from exc
    if not static:
        raise SandboxError(
            f"syd binary {path} is dynamically linked, so it would not start inside agent "
            "images. Use the static build from a syd release tarball "
            "(https://distfiles.exherbo.org/#sydbox/), not a distribution package.",
        )
    return path.resolve(), arch


def check_host_kernel() -> None:
    """Reject Linux hosts whose kernel is too old for syd.

    Off Linux the engine runs a VM whose kernel the host cannot see, so the
    check is left to syd at container start.
    """
    if not sys.platform.startswith("linux"):
        return
    release = os.uname().release
    parts: list[int] = []
    for piece in release.split(".")[:2]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 2:
        parts.append(0)
    if tuple(parts) < SYD_MIN_KERNEL:
        minimum = ".".join(str(part) for part in SYD_MIN_KERNEL)
        raise SandboxError(
            f"syd needs Linux {minimum} or newer; this host runs {release}. "
            "Upgrade the kernel or run without --sandbox.",
        )


def _glob_escape(path: str) -> str:
    """Escape syd glob metacharacters so a path matches only itself."""
    return "".join(f"\\{ch}" if ch in "*?[]\\" else ch for ch in path)


def _normalized(paths: Iterable[str | None]) -> list[str]:
    seen: list[str] = []
    for raw in paths:
        if not raw:
            continue
        path = os.path.normpath(raw) if raw.startswith("/") else ""
        if path and path != "/" and path not in seen:
            seen.append(path)
    return seen


def render_syd_policy(
    *,
    agent: str,
    workspace_paths: Iterable[str | None],
    writable_paths: Iterable[str | None],
    log_level: str = "warn",
    rules: Iterable[str] = (),
) -> str:
    """Render the syd policy for one launch.

    *workspace_paths* are the container paths the workspace is mounted at
    (``/workspace`` plus the ACP host-path binds); *writable_paths* are the
    other container paths the agent must be able to write (config mount,
    rw volume targets, HOME).
    """
    workspaces = _normalized(workspace_paths)
    writable = _normalized([*_BASE_WRITABLE, *writable_paths, *workspaces])
    lines = [
        "# VibePod syd policy, generated for this launch by `vp`. Do not edit:",
        "# add rules with `sandbox.syd.rules` instead. Preview: `vp sandbox policy`.",
        f"# Agent: {agent}",
        "include_profile linux",
        "include_profile tty",
        "# Agent runtimes JIT-compile (Node/V8, Bun/JavaScriptCore) and some ship",
        "# non-PIE binaries, so W^X memory and the PIE requirement are relaxed.",
        "include_profile nopie",
        "include_profile nomem",
        "# Landlock rules are not configured; the path rules below are enforced",
        "# through seccomp instead.",
        "sandbox/lock:off",
        f"log/level:{log_level}",
        "# VibePod sets the container environment itself (credentials, proxy, CA);",
        "# keep all of it instead of syd's default scrubbing.",
        "passenv+*",
        "# Reads, exec and network are allowed; writes are confined below.",
        "allow/all+/***",
        "allow/net/connect+any!0-65535",
        "allow/net/bind+any!0-65535",
        "allow/net/connect+/***",
        f"deny/{_WRITE_CATEGORIES}+/***",
    ]
    lines.extend(f"allow/{_WRITE_CATEGORIES}+{_glob_escape(path)}/***" for path in writable)
    lines.append("# Host-executed git metadata in the workspace stays read-only.")
    for workspace in workspaces:
        for entry in _PROTECTED_WORKSPACE_ENTRIES:
            suffix = entry.removesuffix("/***")
            glob = "/***" if entry.endswith("/***") else ""
            lines.append(f"deny/{_WRITE_CATEGORIES}+{_glob_escape(f'{workspace}/{suffix}')}{glob}")
    extra = [rule for rule in rules if rule]
    if extra:
        lines.append("# sandbox.syd.rules")
        lines.extend(extra)
    return "\n".join(lines) + "\n"


def write_policy(text: str, root: Path | None = None) -> Path:
    """Store *text* content-addressed under the config root and return its path.

    syd refuses policy files that are writable by group or others, so the
    file is 0644. Content addressing keeps the file stable for detached
    containers and bounds the directory to one file per distinct policy.
    """
    directory = (root or get_config_root()) / "sandbox" / "policies"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    path = directory / f"{digest}.syd-3"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o644)
        tmp.replace(path)
    path.chmod(0o644)
    return path


def writable_targets(
    *,
    config_mount_path: str,
    env: Mapping[str, str],
    volumes: Iterable[tuple[str, str, str]],
) -> list[str]:
    """Collect the container paths a launch must keep writable."""
    targets = [config_mount_path, env.get("HOME")]
    targets.extend(target for _, target, mode in volumes if mode != "ro")
    return [target for target in targets if target and not target.startswith(SYD_MOUNT_DIR)]


@dataclass(frozen=True)
class SydPrerequisites:
    """Validated syd settings, checked before a launch has side effects."""

    binary: Path
    arch: str
    log_level: str
    rules: list[str]


def check_syd_prerequisites(config: Mapping[str, Any]) -> SydPrerequisites:
    """Validate the syd settings, binary and host kernel."""
    settings = syd_settings(config)
    binary, arch = validate_syd_binary(settings["binary"])
    check_host_kernel()
    return SydPrerequisites(
        binary=binary,
        arch=arch,
        log_level=settings["log_level"],
        rules=settings["rules"],
    )


def prepare_syd_sandbox(
    prerequisites: SydPrerequisites,
    *,
    agent: str,
    workspace_paths: Iterable[str | None],
    writable_paths: Iterable[str | None],
) -> SydSandbox:
    """Write the launch policy and return the mounts and command prefix."""
    policy = render_syd_policy(
        agent=agent,
        workspace_paths=workspace_paths,
        writable_paths=writable_paths,
        log_level=prerequisites.log_level,
        rules=prerequisites.rules,
    )
    return SydSandbox(binary=prerequisites.binary, policy_path=write_policy(policy))


def wrap_command(command: list[str], prefix_len: int, sandbox: SydSandbox) -> list[str]:
    """Insert the syd prefix after the image-entrypoint part of *command*."""
    return [*command[:prefix_len], *sandbox.command_prefix(), *command[prefix_len:]]

"""microsandbox runtime: run agent images in local microVMs through the `msb` CLI.

microsandbox (https://microsandbox.dev, npm ``microsandbox``, formerly
``@superradcompany/microsandbox``) boots OCI images as hardware-isolated
microVMs. VibePod drives its ``msb`` CLI instead of a Docker daemon when
``runtime: microsandbox`` is selected.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RUNTIME_DOCKER = "docker"
RUNTIME_MICROSANDBOX = "microsandbox"
RUNTIMES = (RUNTIME_DOCKER, RUNTIME_MICROSANDBOX)
DEFAULT_RUNTIME = RUNTIME_DOCKER
DEFAULT_BINARY = "msb"

INSTALL_HINT = (
    "Install it with `curl -fsSL https://install.microsandbox.dev | sh` "
    "(or `brew install superradcompany/tap/microsandbox`, `npm i -g microsandbox`), "
    "or set microsandbox.binary to the msb path."
)

# msb sizes: a number with an optional K/M/G/T suffix (512M, 2G); bare
# numbers are MiB, as in the SDKs.
_MEMORY_RE = re.compile(r"[1-9][0-9]*(\.[0-9]+)?[KMGT]?(i?B)?", re.IGNORECASE)
_DOCKER_ONLY_MODE_OPTIONS = frozenset({"z", "Z"})


class MicrosandboxError(RuntimeError):
    """Raised when microsandbox is unavailable, misconfigured, or fails to launch."""


@dataclass(frozen=True)
class MicrosandboxSettings:
    """Validated ``microsandbox:`` config section."""

    binary: str = DEFAULT_BINARY
    cpus: int | None = None
    memory: str | None = None
    args: list[str] = field(default_factory=list)


def resolve_runtime(config: dict[str, Any], override: str | None = None) -> str:
    """Return the selected runtime (flag > config/VP_RUNTIME), validated."""
    raw = override if override is not None else config.get("runtime", DEFAULT_RUNTIME)
    if raw is None:
        return DEFAULT_RUNTIME
    value = str(raw).strip().lower()
    if value not in RUNTIMES:
        source = "--runtime" if override is not None else "runtime"
        raise ValueError(
            f"Invalid {source} value '{raw}'. Supported runtimes: {', '.join(RUNTIMES)}.",
        )
    return value


def normalize_memory(value: Any, *, source: str = "microsandbox.memory") -> str | None:
    """Validate a memory size; bare integers mean MiB."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid {source} value {value!r}: expected a size such as '2G'.")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError(f"Invalid {source} value {value}: must be positive.")
        return f"{value}M"
    text = str(value).strip()
    if not _MEMORY_RE.fullmatch(text):
        raise ValueError(
            f"Invalid {source} value '{value}': expected a size such as '512M' or '2G'.",
        )
    return text


def normalize_cpus(value: Any, *, source: str = "microsandbox.cpus") -> int | None:
    """Validate a vCPU count."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid {source} value {value!r}: expected a positive integer.")
    try:
        cpus = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(
            f"Invalid {source} value '{value}': expected a positive integer.",
        ) from exc
    if cpus <= 0:
        raise ValueError(f"Invalid {source} value '{value}': expected a positive integer.")
    return cpus


def load_settings(config: dict[str, Any]) -> MicrosandboxSettings:
    """Read and validate the ``microsandbox`` config section."""
    section = config.get("microsandbox")
    if section is None:
        return MicrosandboxSettings()
    if not isinstance(section, dict):
        raise ValueError("Invalid microsandbox config: expected a mapping.")
    known = {"binary", "cpus", "memory", "args"}
    unknown = sorted(str(key) for key in section if key not in known)
    if unknown:
        raise ValueError(
            f"Unknown microsandbox config key(s): {', '.join(unknown)}. "
            f"Supported: {', '.join(sorted(known))}.",
        )

    binary = section.get("binary") or DEFAULT_BINARY
    if not isinstance(binary, str) or not binary.strip():
        raise ValueError("Invalid microsandbox.binary value: expected a command name or path.")

    raw_args = section.get("args") or []
    if isinstance(raw_args, str):
        raw_args = [raw_args]
    if not isinstance(raw_args, list) or not all(isinstance(arg, str) for arg in raw_args):
        raise ValueError("Invalid microsandbox.args value: expected a list of strings.")

    return MicrosandboxSettings(
        binary=binary.strip(),
        cpus=normalize_cpus(section.get("cpus")),
        memory=normalize_memory(section.get("memory")),
        args=list(raw_args),
    )


def find_binary(settings: MicrosandboxSettings) -> str:
    """Resolve the msb executable or raise with install instructions."""
    candidate = os.path.expanduser(settings.binary)
    if os.sep in candidate or (os.altsep and os.altsep in candidate):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        raise MicrosandboxError(
            f"microsandbox binary '{settings.binary}' is not an executable file. {INSTALL_HINT}",
        )
    found = shutil.which(candidate)
    if found is None:
        # The installer links into ~/.local/bin but does not edit PATH.
        fallback = Path.home() / ".local" / "bin" / candidate
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return str(fallback)
        raise MicrosandboxError(f"microsandbox CLI '{candidate}' not found on PATH. {INSTALL_HINT}")
    return found


def host_support_problem() -> str | None:
    """Return why this host cannot run local microVMs, or None when it can."""
    if sys.platform.startswith("linux"):
        kvm = Path("/dev/kvm")
        if not kvm.exists():
            return (
                "/dev/kvm is missing: microsandbox needs KVM. Enable virtualization "
                "in the BIOS/hypervisor (nested virtualization inside a VM) and load "
                "the kvm module."
            )
        if not os.access(kvm, os.R_OK | os.W_OK):
            return (
                "/dev/kvm is not accessible: add your user to the 'kvm' group "
                "(`sudo usermod -aG kvm $USER`, then log in again)."
            )
        return None
    if sys.platform == "darwin":
        if platform.machine().lower() not in ("arm64", "aarch64"):
            return "microsandbox on macOS requires Apple Silicon."
        return None
    if sys.platform.startswith("win"):
        return None
    return f"microsandbox does not support this platform ({sys.platform})."


def binary_version(binary: str) -> str | None:
    """Return ``msb --version`` output, or None when it cannot be run."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or result.stderr).strip() or None


def ensure_available(settings: MicrosandboxSettings) -> str:
    """Return the msb path after checking the binary and host prerequisites."""
    binary = find_binary(settings)
    problem = host_support_problem()
    if problem is not None:
        raise MicrosandboxError(f"{problem} Run `msb doctor` for details.")
    return binary


def port_args(ports: dict[str, Any] | None) -> list[str]:
    """Convert docker-py port bindings into ``msb -p`` arguments."""
    args: list[str] = []
    for internal, binds in (ports or {}).items():
        container_port, _, proto = str(internal).partition("/")
        if proto and proto != "tcp":
            raise MicrosandboxError(
                f"Port {internal}: the microsandbox runtime publishes TCP ports only.",
            )
        for bind in binds if isinstance(binds, list) else [binds]:
            if isinstance(bind, tuple):
                host_ip, host_port = (bind + (None,))[:2]
            else:
                host_ip, host_port = None, bind
            if host_port in (None, "", "0", 0):
                raise MicrosandboxError(
                    f"Port {container_port}: the microsandbox runtime needs an explicit "
                    "host port (ephemeral ports are not supported).",
                )
            prefix = f"{host_ip}:" if host_ip else ""
            args.extend(["-p", f"{prefix}{host_port}:{container_port}"])
    return args


def mount_args(volumes: list[tuple[str, str, str]]) -> list[str]:
    """Convert (source, target, mode) mounts into msb mount arguments."""
    args: list[str] = []
    for source, target, mode in volumes:
        options = [opt for opt in mode.split(",") if opt not in _DOCKER_ONLY_MODE_OPTIONS]
        suffix = ":ro" if "ro" in options else ""
        if os.sep in source or (os.altsep and os.altsep in source) or source.startswith("~"):
            path = Path(source).expanduser()
            flag = "--mount-file" if path.is_file() else "-v"
            args.extend([flag, f"{path}:{target}{suffix}"])
        else:
            args.extend(["--mount-named", f"{source}:{target}{suffix}"])
    return args


def write_runtime_conf(
    *,
    env: dict[str, str],
    labels: dict[str, str],
    workdir: str,
) -> Path:
    """Write env, labels and workdir to a private ``--runtime-conf`` file.

    Keeps credentials (API keys, OAuth tokens) out of the msb argv, where any
    local user could read them from the process list. The caller deletes it.
    """
    document = {
        "workdir": workdir,
        "env": {str(key): str(value) for key, value in env.items()},
        "labels": {str(key): str(value) for key, value in labels.items()},
    }
    fd, path = tempfile.mkstemp(prefix="vibepod-msb-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # default_style quotes every scalar: the msb loader rejects bare
            # YAML 1.1 booleans and wants typed strings quoted.
            yaml.safe_dump(document, handle, sort_keys=False, default_style='"')
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return Path(path)


def build_run_argv(
    *,
    binary: str,
    settings: MicrosandboxSettings,
    image: str,
    runtime_conf: Path,
    volumes: list[tuple[str, str, str]],
    ports: dict[str, Any] | None,
    command: list[str] | None,
    name: str | None,
    detach: bool,
) -> list[str]:
    """Build the ``msb run`` argv for an agent sandbox."""
    argv = [binary, "run", "--runtime-conf", str(runtime_conf)]
    if name:
        argv.extend(["--name", name])
    if detach:
        argv.append("--detach")
    if settings.cpus is not None:
        argv.extend(["--cpus", str(settings.cpus)])
    if settings.memory is not None:
        argv.extend(["--memory", settings.memory])
    argv.extend(mount_args(volumes))
    argv.extend(port_args(ports))
    argv.extend(settings.args)
    argv.append(image)
    if command:
        # msb keeps the image ENTRYPOINT and replaces CMD with these, the same
        # contract as Docker's `command`.
        argv.extend(["--", *command])
    return argv


def run_foreground(argv: list[str]) -> int:
    """Run msb attached to this terminal and return its exit code.

    msb shares the terminal's process group, so it receives Ctrl+C itself and
    shuts the sandbox down; VibePod only waits for it to finish.
    """
    try:
        process = subprocess.Popen(argv)
    except OSError as exc:
        raise MicrosandboxError(f"Failed to start microsandbox: {exc}") from exc
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            continue


def pull_image(binary: str, image: str) -> None:
    """Refresh *image* in the microsandbox image cache."""
    try:
        result = subprocess.run([binary, "pull", image], check=False)
    except OSError as exc:
        raise MicrosandboxError(f"Failed to run `{binary} pull {image}`: {exc}") from exc
    if result.returncode != 0:
        raise MicrosandboxError(f"`msb pull {image}` failed with exit code {result.returncode}.")


def remove_sandbox(binary: str, name: str) -> None:
    """Remove a named sandbox, ignoring failures (it may already be gone)."""
    try:
        subprocess.run(
            [binary, "rm", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        pass

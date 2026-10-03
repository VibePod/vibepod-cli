"""Opt-in agent sandboxes that swap the agent container's OCI runtime.

``sandbox.mode: gvisor`` runs the agent container under gVisor's ``runsc``
runtime. Images, mounts, the proxy sidecar and published ports stay the same;
only the agent container gets the runtime (see docs/sandbox.md).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vibepod.core.config import load_project_config

SANDBOX_MODES = ("none", "gvisor")
DEFAULT_GVISOR_RUNTIME = "runsc"
SANDBOX_LABEL = "vibepod.sandbox"

SANDBOX_HELP = (
    "Agent sandbox for this run: none or gvisor (overrides the `sandbox` config; "
    "see https://vibepod.dev/docs/sandbox/)"
)

_DOCS_HINT = "See https://vibepod.dev/docs/sandbox/ for setup."


class SandboxError(ValueError):
    """Raised when the requested sandbox cannot be used on this engine."""


@dataclass(frozen=True)
class SandboxSettings:
    mode: str = "none"
    runtime: str = DEFAULT_GVISOR_RUNTIME


@dataclass(frozen=True)
class SandboxPlan:
    """What a launch has to pass to ``run_agent`` for the chosen sandbox."""

    mode: str = "none"
    runtime: str | None = None
    security_opt: list[str] = field(default_factory=list)
    host_socket_mounts: bool = True
    socket_skip_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def suffix(self) -> str:
        """Fragment appended to the "Starting ..." line."""
        return f" in the {self.mode} sandbox (runtime {self.runtime})" if self.runtime else ""

    @property
    def labels(self) -> dict[str, str]:
        return {SANDBOX_LABEL: self.mode} if self.mode != "none" else {}


NO_SANDBOX = SandboxPlan()


def _normalize_mode(value: Any, source: str) -> str:
    mode = str(value).strip().lower()
    if mode in {"", "off", "false"}:
        mode = "none"
    if mode not in SANDBOX_MODES:
        raise SandboxError(
            f"Invalid sandbox '{value}' from {source}; expected one of: {', '.join(SANDBOX_MODES)}",
        )
    return mode


def _apply_section(settings: dict[str, Any], section: Any, source: str) -> None:
    if section is None:
        return
    if isinstance(section, str):
        # Shorthand: `sandbox: gvisor`.
        settings["mode"] = _normalize_mode(section, source)
        return
    if not isinstance(section, dict):
        raise SandboxError(f"Config key 'sandbox' in {source} must be a mapping or a mode name")
    if "mode" in section and section["mode"] is not None:
        settings["mode"] = _normalize_mode(section["mode"], source)
    gvisor = section.get("gvisor")
    if isinstance(gvisor, dict) and gvisor.get("runtime"):
        settings["runtime"] = str(gvisor["runtime"]).strip()


def resolve_sandbox(
    config: dict[str, Any],
    workspace: Path,
    cli_value: str | None = None,
) -> SandboxSettings:
    """Return the sandbox for this launch.

    Precedence: ``--sandbox`` > ``VP_SANDBOX`` > the launched workspace's
    ``.vibepod/config.yaml`` > the merged global/current-directory config.
    The workspace file is read again because get_config() merges the project
    file of the *current* directory, not the one ``-w`` points at.
    """
    settings: dict[str, Any] = {"mode": "none", "runtime": DEFAULT_GVISOR_RUNTIME}
    _apply_section(settings, config.get("sandbox"), "config")
    _apply_section(settings, load_project_config(workspace).get("sandbox"), "project config")
    env_value = os.environ.get("VP_SANDBOX")
    if env_value is not None:
        settings["mode"] = _normalize_mode(env_value, "VP_SANDBOX")
    if cli_value is not None:
        settings["mode"] = _normalize_mode(cli_value, "--sandbox")
    return SandboxSettings(mode=settings["mode"], runtime=settings["runtime"] or "runsc")


def runtime_flag(args: list[str], name: str) -> str | None:
    """Return the value of ``--name=value`` / ``--name value`` in runtime args."""
    for index, arg in enumerate(args):
        stripped = arg.lstrip("-")
        if arg == stripped:
            continue
        if stripped.startswith(f"{name}="):
            return stripped.split("=", 1)[1]
        if stripped == name and index + 1 < len(args):
            return args[index + 1]
    return None


def _install_hint(runtime: str) -> str:
    return (
        f"Register it with `sudo runsc install --runtime {runtime} -- --network=host "
        "--host-uds=open` and restart Docker (`sudo systemctl restart docker`)."
    )


def _probe(manager: Any, name: str, default: Any) -> Any:
    probe = getattr(manager, name, None)
    return probe() if callable(probe) else default


def prepare_sandbox(manager: Any, settings: SandboxSettings) -> SandboxPlan:
    """Validate the engine for *settings* and return the launch plan.

    Raises SandboxError with an actionable message when a prerequisite is
    missing, before any network, proxy or image is provisioned.
    """
    if settings.mode == "none":
        return NO_SANDBOX
    runtime = settings.runtime
    if not sys.platform.startswith("linux"):
        raise SandboxError(
            "The gVisor sandbox needs a Linux host: runsc is a Linux-only OCI runtime and "
            "Docker Desktop / Podman machine VMs do not ship it. Run without --sandbox. "
            + _DOCS_HINT,
        )
    if _probe(manager, "is_rootless_engine", False):
        raise SandboxError(
            "The gVisor sandbox is not supported on rootless Docker or rootless Podman "
            "(runsc needs the user namespace and network setup only a rootful engine "
            "provides). Use a rootful engine or run without --sandbox. " + _DOCS_HINT,
        )

    podman = bool(_probe(manager, "is_podman", False))
    runtimes: dict[str, dict[str, Any]] = _probe(manager, "runtimes", {}) or {}
    entry = runtimes.get(runtime)
    notes: list[str] = []
    args: list[str] | None
    if podman:
        # Podman lists runtimes from containers.conf whether or not the binary
        # exists, and never their flags: a missing runsc surfaces at create.
        args = None
    else:
        if entry is None:
            registered = ", ".join(sorted(runtimes)) or "none reported"
            raise SandboxError(
                f"The OCI runtime '{runtime}' is not registered with Docker "
                f"(registered: {registered}). Install gVisor "
                "(https://gvisor.dev/docs/user_guide/install/), then "
                + _install_hint(runtime)
                + " Set `sandbox.gvisor.runtime` if you registered it under another name.",
            )
        raw_args = entry.get("runtimeArgs")
        if raw_args is None and entry.get("runtimeType"):
            # containerd shim runtime: its flags live in a runsc.toml we cannot read.
            args = None
            notes.append(
                f"cannot read the flags of shim runtime '{runtime}'; it needs "
                "network=host for DNS on the VibePod network (see the sandbox docs)",
            )
        else:
            args = [str(arg) for arg in (raw_args or [])]
            if runtime_flag(args, "network") != "host":
                # Docker's embedded DNS (127.0.0.11) on user-defined networks is
                # reached through netfilter rules gVisor's netstack never sees
                # (google/gvisor#7469), so the agent could resolve nothing.
                raise SandboxError(
                    f"The Docker runtime '{runtime}' uses gVisor's own network stack, which "
                    "cannot reach Docker's embedded DNS on the VibePod network "
                    "(google/gvisor#7469): the agent would resolve no hostnames. "
                    + _install_hint(runtime).replace("Register it", "Re-register it")
                    + (
                        " Or register a second runtime (e.g. `--runtime runsc-vibepod`) and "
                        "set `sandbox.gvisor.runtime` to it."
                        if runtime == DEFAULT_GVISOR_RUNTIME
                        else ""
                    ),
                )

    host_uds = runtime_flag(args, "host-uds") if args is not None else None
    host_socket_mounts = host_uds in {"open", "all"}
    socket_skip_reason: str | None = None
    if not host_socket_mounts:
        why = (
            "Podman does not report whether it allows host unix sockets"
            if podman
            else "it does not allow host unix sockets (register it with --host-uds=open)"
        )
        socket_skip_reason = (
            f"not mounting the herdr socket into the gVisor runtime '{runtime}': {why}; "
            "reporting pane identity from the host only"
        )

    security_opt: list[str] = []
    if _probe(manager, "selinux_enabled", False):
        # runsc cannot apply SELinux process labels and refuses to start with one.
        security_opt.append("label=disable")

    return SandboxPlan(
        mode=settings.mode,
        runtime=runtime,
        security_opt=security_opt,
        host_socket_mounts=host_socket_mounts,
        socket_skip_reason=socket_skip_reason,
        notes=notes,
    )

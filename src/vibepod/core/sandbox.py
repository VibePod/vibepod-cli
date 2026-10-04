"""Unified sandbox selection, capabilities and three backend integration contracts.

OCI backends contribute engine options, separate backends replace the engine
launch, and process backends wrap the agent command. See docs/sandbox.md.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from vibepod.core.config import deep_merge, load_project_config
from vibepod.utils.console import deprecation_warning

if TYPE_CHECKING:
    from vibepod.core.microsandbox import MicrosandboxSettings

SANDBOX_TYPES = ("none", "gvisor", "microsandbox")
SANDBOX_MODES = SANDBOX_TYPES
DEFAULT_GVISOR_RUNTIME = "runsc"
SANDBOX_LABEL = "vibepod.sandbox"

SANDBOX_HELP = (
    "Agent sandbox for this run: none, gvisor or microsandbox (overrides the `sandbox` config; "
    "see https://vibepod.dev/docs/sandbox/)"
)

_DOCS_HINT = "See https://vibepod.dev/docs/sandbox/ for setup."


class SandboxError(ValueError):
    """Raised when the requested sandbox cannot be used on this engine."""


@dataclass(frozen=True, init=False)
class SandboxSettings:
    type: str = "none"
    runtime: str = DEFAULT_GVISOR_RUNTIME

    def __init__(
        self,
        type: str = "none",
        runtime: str = DEFAULT_GVISOR_RUNTIME,
        *,
        mode: str | None = None,
    ) -> None:
        object.__setattr__(self, "type", mode if mode is not None else type)
        object.__setattr__(self, "runtime", runtime)

    @property
    def mode(self) -> str:
        """Compatibility accessor for the original gVisor integration."""
        return self.type


@dataclass(frozen=True)
class SandboxPlan:
    """What a launch has to pass to ``run_agent`` for the chosen sandbox."""

    type: str = "none"
    runtime: str | None = None
    security_opt: list[str] = field(default_factory=list)
    host_socket_mounts: bool = True
    socket_skip_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return self.type

    @property
    def suffix(self) -> str:
        """Fragment appended to the "Starting ..." line."""
        return f" in the {self.type} sandbox (runtime {self.runtime})" if self.runtime else ""

    @property
    def labels(self) -> dict[str, str]:
        return {SANDBOX_LABEL: self.type} if self.type != "none" else {}


NO_SANDBOX = SandboxPlan()


def _normalize_type(value: Any, source: str) -> str:
    mode = str(value).strip().lower()
    if mode in {"", "off", "false"}:
        mode = "none"
    if mode not in SANDBOX_TYPES:
        raise SandboxError(
            f"Invalid sandbox '{value}' from {source}; expected one of: {', '.join(SANDBOX_TYPES)}",
        )
    return mode


def _apply_section(
    settings: dict[str, Any],
    section: Any,
    source: str,
    *,
    warn_deprecated: bool = True,
) -> None:
    if section is None:
        return
    if isinstance(section, str):
        # Shorthand: `sandbox: gvisor`.
        settings["type"] = _normalize_type(section, source)
        return
    if not isinstance(section, dict):
        raise SandboxError(f"Config key 'sandbox' in {source} must be a mapping or a mode name")
    if "mode" in section and section["mode"] is not None:
        if warn_deprecated:
            deprecation_warning("sandbox.mode is deprecated; use sandbox.type.")
        settings["type"] = _normalize_type(section["mode"], source)
    if "type" in section and section["type"] is not None:
        settings["type"] = _normalize_type(section["type"], source)
    gvisor = section.get("gvisor")
    if isinstance(gvisor, dict) and gvisor.get("runtime"):
        settings["runtime"] = str(gvisor["runtime"]).strip()


def resolve_sandbox(
    config: dict[str, Any],
    workspace: Path,
    cli_value: str | None = None,
    legacy_runtime: str | None = None,
) -> SandboxSettings:
    """Return the sandbox for this launch.

    Precedence: ``--sandbox`` > ``VP_SANDBOX`` > the launched workspace's
    ``.vibepod/config.yaml`` > the merged global/current-directory config.
    The workspace file is read again because get_config() merges the project
    file of the *current* directory, not the one ``-w`` points at.
    """
    settings: dict[str, Any] = {"type": "none", "runtime": DEFAULT_GVISOR_RUNTIME}
    for source, layer in (("config", config), ("project config", load_project_config(workspace))):
        warn_deprecated = source == "config" or workspace.resolve() != Path.cwd().resolve()
        legacy = layer.get("runtime")
        if legacy is not None:
            if warn_deprecated:
                deprecation_warning("runtime is deprecated; use sandbox.type.")
            settings["type"] = _legacy_type(legacy, source)
        _apply_section(settings, layer.get("sandbox"), source, warn_deprecated=warn_deprecated)
    env_runtime = os.environ.get("VP_RUNTIME")
    if env_runtime is not None:
        deprecation_warning("VP_RUNTIME is deprecated; use VP_SANDBOX.")
        settings["type"] = _legacy_type(env_runtime, "VP_RUNTIME")
    env_value = os.environ.get("VP_SANDBOX")
    if env_value is not None:
        settings["type"] = _normalize_type(env_value, "VP_SANDBOX")
    if legacy_runtime is not None:
        deprecation_warning("--runtime is deprecated; use --sandbox.")
        settings["type"] = _legacy_type(legacy_runtime, "--runtime")
    if cli_value is not None:
        settings["type"] = _normalize_type(cli_value, "--sandbox")
    return SandboxSettings(type=settings["type"], runtime=settings["runtime"] or "runsc")


def _legacy_type(value: Any, source: str) -> str:
    value = str(value).strip().lower()
    if value not in {"docker", "microsandbox"}:
        raise SandboxError(f"Invalid {source} runtime '{value}'; expected docker or microsandbox")
    return "none" if value == "docker" else value


def sandbox_options_config(config: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Merge backend options from the launched workspace, preserving global options.

    Selection is resolved separately so environment and CLI precedence stay intact.
    The legacy top-level microsandbox options remain accepted.
    """
    project = load_project_config(workspace)
    section = project.get("sandbox")
    options: dict[str, Any] = {}
    if isinstance(section, dict):
        options = {name: section[name] for name in SANDBOX_TYPES if name in section}
    if "microsandbox" in project and "microsandbox" not in options:
        options["microsandbox"] = project["microsandbox"]
    base = config.get("sandbox")
    if isinstance(base, str):
        config = {**config, "sandbox": {"type": base}}
    return deep_merge(config, {"sandbox": options})


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


def _prepare_gvisor(manager: Any, settings: SandboxSettings) -> SandboxPlan:
    """Validate the engine for *settings* and return the launch plan.

    Raises SandboxError with an actionable message when a prerequisite is
    missing, before any network, proxy or image is provisioned.
    """
    if settings.type == "none":
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
        type=settings.type,
        runtime=runtime,
        security_opt=security_opt,
        host_socket_mounts=host_socket_mounts,
        socket_skip_reason=socket_skip_reason,
        notes=notes,
    )


@dataclass(frozen=True)
class SandboxCapabilities:
    workspace_mount: bool = True
    proxy_metrics: bool = True
    session_logging: bool = True
    ports: bool = True
    attach: bool = True
    task: bool = True
    acp: bool = True
    overlays: bool = True
    init_commands: bool = True
    herdr_socket: bool = True


class OCIRuntimeSandbox(Protocol):
    """Contribute engine create options after checking the OCI runtime."""

    def prepare(self, manager: Any, settings: SandboxSettings) -> SandboxPlan: ...


BackendSettings = TypeVar("BackendSettings", covariant=True)


class SeparateBackendSandbox(Protocol[BackendSettings]):
    """Check and launch through a backend independent of Docker/Podman."""

    def prepare(self, config: dict[str, Any]) -> tuple[BackendSettings, str]: ...

    def launch(self, argv: list[str]) -> int: ...


class ProcessSandbox(Protocol):
    """Validate a process sandbox and wrap the final agent command."""

    def check(self, config: dict[str, Any]) -> None: ...

    def wrap(self, command: list[str]) -> list[str]: ...


class GVisorSandbox:
    def prepare(self, manager: Any, settings: SandboxSettings) -> SandboxPlan:
        return _prepare_gvisor(manager, settings)


class MicrosandboxBackend:
    def prepare(self, config: dict[str, Any]) -> tuple[MicrosandboxSettings, str]:
        from vibepod.core import microsandbox

        settings = microsandbox.load_settings(config)
        return settings, microsandbox.ensure_available(settings)

    def launch(self, argv: list[str]) -> int:
        from vibepod.core import microsandbox

        return microsandbox.run_foreground(argv)


CAPABILITIES = {
    "none": SandboxCapabilities(),
    "gvisor": SandboxCapabilities(),  # host socket support is checked in the launch plan
    "microsandbox": SandboxCapabilities(
        proxy_metrics=False,
        session_logging=False,
        attach=False,
        task=False,
        acp=False,
        overlays=False,
        init_commands=False,
        herdr_socket=False,
    ),
}
OCI_SANDBOXES: dict[str, OCIRuntimeSandbox] = {"gvisor": GVisorSandbox()}
SEPARATE_BACKENDS: dict[str, SeparateBackendSandbox[Any]] = {"microsandbox": MicrosandboxBackend()}
PROCESS_SANDBOXES: dict[str, ProcessSandbox] = {}


def check_capabilities(settings: SandboxSettings, **requested: bool) -> None:
    """Reject requested unsupported features before launch side effects."""
    capabilities = CAPABILITIES[settings.type]
    unsupported = [
        name for name, used in requested.items() if used and not getattr(capabilities, name)
    ]
    if unsupported:
        features = ", ".join(name.replace("_", " ") for name in unsupported)
        hints = {
            "proxy_metrics": "set proxy.enabled: false",
            "session_logging": "set logging.enabled: false",
            "overlays": "pass --no-overlay",
            "herdr_socket": "pass --no-herdr",
        }
        remedies = "; ".join(hints[name] for name in unsupported if name in hints)
        remedy = f"{remedies}, or " if remedies else "disable these features or "
        raise SandboxError(
            f"The {settings.type} sandbox does not support {features}; {remedy}use --sandbox none.",
        )


def prepare_sandbox(manager: Any, settings: SandboxSettings) -> SandboxPlan:
    if settings.type == "none":
        return NO_SANDBOX
    backend = OCI_SANDBOXES.get(settings.type)
    if backend is None:
        raise SandboxError(f"{settings.type} uses a separate backend, not an OCI runtime")
    return backend.prepare(manager, settings)

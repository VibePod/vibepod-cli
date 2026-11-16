"""Remote control of a running agent from another device.

Codex is the only agent wired here. Its experimental ``codex remote-control``
command connects Codex's background app-server to the Codex tab of the
ChatGPT app through OpenAI's relay. The connection is outbound only, so no
ports are published. VibePod runs the command inside the started container as
the agent user, so the daemon lives and dies with the container. The bare
command turns remote control on only for that daemon's lifetime
(``remoteControl/enable`` with ``ephemeral: true``): unlike
``codex remote-control start``, nothing is persisted that would make later
``vp run codex`` sessions on the same profile reachable from the phone.

Claude Code, Copilot CLI and Antigravity have their own switches, which
already pass through ``vp run <agent> -- <flag>`` unchanged.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REMOTE_CONTROL_AGENTS = frozenset({"codex"})

# Agents whose own CLI has a remote-control switch: point there instead.
_NATIVE_SWITCH_HINTS = {
    "claude": (
        "use Claude Code's own switch: `vp run claude -- --remote-control` "
        "(it needs a full `/login`; the stored setup-token cannot start Remote Control)"
    ),
    "copilot": "use Copilot CLI's own switch: `vp run copilot -- --remote`",
    "agy": "use Antigravity's own switch: `vp run agy -- --remote-control`",
}

# The first start prepares the daemon's managed package in CODEX_HOME and
# waits for the relay connection, so allow for a slow first run.
ENABLE_TIMEOUT_SECONDS = 120.0
PAIR_TIMEOUT_SECONDS = 60.0

_STATUS_PREFIXES = ("This machine is available", "Remote control is enabled")
_MAX_HOSTNAME_LENGTH = 63


class RemoteControlError(RuntimeError):
    """Remote control could not be turned on, or no pairing code was created."""


@dataclass(frozen=True)
class RemoteControlStatus:
    """Outcome of turning remote control on."""

    message: str
    # True when Codex was still busy after the timeout. The bare command
    # falls back to a foreground server when its background daemon is not
    # eligible (e.g. `features.daemon_auto_start = false`), and that server
    # never returns. It still serves remote control, but cannot pair.
    timed_out: bool = False


@dataclass(frozen=True)
class PairingCode:
    """A short-lived manual pairing code for the ChatGPT app."""

    code: str
    expires_at: datetime | None


@dataclass(frozen=True)
class _ExecOutcome:
    exit_code: int | None  # None: still running when the timeout expired
    output: str


def unsupported_agent_message(agent: str) -> str:
    """Explain why `--remote-control` is refused for ``agent``."""
    supported = ", ".join(sorted(REMOTE_CONTROL_AGENTS))
    hint = _NATIVE_SWITCH_HINTS.get(agent)
    if hint is not None:
        return f"--remote-control currently wires {supported} only. For {agent}, {hint}."
    return f"--remote-control is not supported for '{agent}'. Supported: {supported}."


def conflicting_option_message(*, acp: bool, provider: bool, llm_enabled: bool) -> str | None:
    """Return why remote control cannot be combined with this launch, if it can't."""
    if acp:
        return "--remote-control cannot be combined with --acp: the editor owns the session."
    if provider or llm_enabled:
        source = "--provider" if provider else "llm wiring (llm.enabled)"
        return (
            f"--remote-control cannot be combined with {source}: Codex remote control "
            "needs a ChatGPT sign-in and refuses API-key and custom-provider auth."
        )
    return None


def remote_control_failure_hint(message: str) -> str | None:
    """Return a next step for a known `codex remote-control` failure, if any."""
    lowered = message.lower()
    if "chatgpt authentication" in lowered or "not logged in" in lowered:
        return "Sign in with ChatGPT first: `vp run codex login` (API keys are refused)."
    if "unrecognized subcommand" in lowered or "unexpected argument" in lowered:
        return (
            "This Codex image predates `codex remote-control`; pull a newer one with "
            "`vp run codex --pull --remote-control`."
        )
    return None


def container_hostname(workspace: Path) -> str:
    """Return an RFC 1123 hostname that names the project.

    Codex lists the host on the phone under the container hostname, which
    is otherwise a random container ID.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-")
    name = f"vibepod-{slug}" if slug else "vibepod"
    return name[:_MAX_HOSTNAME_LENGTH].rstrip("-")


def exec_user(env: dict[str, str], *, rootless_podman: bool) -> str | None:
    """Return the user that runs the agent, so exec'd commands match it.

    The image entrypoint drops to USER_UID/USER_GID (default 1000). Under
    rootless Podman (keep-id) VibePod sets both to 0 and the container's
    default user already maps to the host user.
    """
    if rootless_podman:
        return None
    return f"{env.get('USER_UID', '1000')}:{env.get('USER_GID', '1000')}"


def _exec_with_timeout(
    container: Any,
    command: list[str],
    *,
    user: str | None,
    timeout: float,
) -> _ExecOutcome:
    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            kwargs: dict[str, Any] = {"user": user} if user else {}
            exec_result = container.exec_run(command, **kwargs)
            result["exit_code"] = exec_result.exit_code
            result["output"] = exec_result.output
        except Exception as exc:  # engine errors are reported like a failed exec
            result["error"] = exc

    worker = threading.Thread(target=_target, name="vibepod-remote-control", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return _ExecOutcome(exit_code=None, output="")
    if "error" in result:
        return _ExecOutcome(exit_code=1, output=str(result["error"]))
    output = result.get("output") or b""
    text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
    exit_code = result.get("exit_code")
    return _ExecOutcome(exit_code=1 if exit_code is None else int(exit_code), output=text)


def _lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def _failure_detail(output: str) -> str:
    lines = _lines(output)
    return " ".join(lines[-3:]) if lines else "no output"


def enable_codex_remote_control(
    container: Any,
    *,
    user: str | None,
    timeout: float = ENABLE_TIMEOUT_SECONDS,
) -> RemoteControlStatus:
    """Start Codex's background app-server in ``container`` with remote control on."""
    outcome = _exec_with_timeout(container, ["codex", "remote-control"], user=user, timeout=timeout)
    if outcome.exit_code is None:
        return RemoteControlStatus(
            message=(
                f"`codex remote-control` was still running after {int(timeout)}s. Codex "
                "may have fallen back to a foreground server, which can't create pairing "
                "codes; check that features.daemon_auto_start is not disabled in its "
                "config.toml."
            ),
            timed_out=True,
        )
    if outcome.exit_code != 0:
        raise RemoteControlError(
            f"`codex remote-control` failed: {_failure_detail(outcome.output)}",
        )
    status = next(
        (line for line in _lines(outcome.output) if line.startswith(_STATUS_PREFIXES)),
        "Codex remote control is on.",
    )
    return RemoteControlStatus(message=status)


def start_codex_pairing(
    container: Any,
    *,
    user: str | None,
    timeout: float = PAIR_TIMEOUT_SECONDS,
) -> PairingCode:
    """Create a manual pairing code through the container's Codex daemon."""
    outcome = _exec_with_timeout(
        container,
        ["codex", "remote-control", "pair", "--json"],
        user=user,
        timeout=timeout,
    )
    if outcome.exit_code is None:
        raise RemoteControlError(
            f"`codex remote-control pair` did not answer within {int(timeout)}s",
        )
    if outcome.exit_code != 0:
        raise RemoteControlError(
            f"`codex remote-control pair` failed: {_failure_detail(outcome.output)}",
        )
    payload: dict[str, Any] | None = None
    for line in reversed(_lines(outcome.output)):
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    code = payload.get("manualPairingCode") if payload else None
    if not isinstance(code, str) or not code:
        raise RemoteControlError(
            f"`codex remote-control pair` returned no pairing code: "
            f"{_failure_detail(outcome.output)}",
        )
    expires_at: datetime | None = None
    raw_expiry = payload.get("expiresAt") if payload else None
    if isinstance(raw_expiry, (int, float)) and raw_expiry > 0:
        try:
            expires_at = datetime.fromtimestamp(raw_expiry)
        except (OverflowError, OSError, ValueError):
            expires_at = None
    return PairingCode(code=code, expires_at=expires_at)

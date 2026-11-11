"""Subscription quotas, deliberately separate from traffic logs and API cost estimates.

Only numeric quota fields and locally assigned labels cross the storage boundary.
This module never reads provider credentials, cookies, transcripts, or HTTP captures.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from vibepod.core.agents import agent_config_dir
from vibepod.core.config import get_config_root
from vibepod.core.docker import DockerManager
from vibepod.core.profiles import validate_profile_name

PROVIDERS = ("codex", "claude", "agy")
SOURCES = {"codex": "codex-app-server", "claude": "claude-statusline", "agy": "agy-cli"}
MAX_BYTES = 1024 * 1024
STALE_SECONDS = 300
CLAUDE_COMMAND = "node /claude/vibepod-usage/statusline.cjs"
ERRORS = {
    "not_running": "Start the signed-in agent with vp run.",
    "multiple_containers": "Select a container with --container.",
    "probe_failed": "Usage check failed; check sign-in in the official client.",
    "unsupported_version": "Update Antigravity CLI to 1.1.11 or later.",
    "unavailable": "The client did not report subscription quotas.",
    "no_snapshot": "Run vp usage setup-claude, then use Claude Code normally.",
    "runtime_unavailable": "The container runtime is unavailable.",
    "proxy_capture_enabled": "Use an agent started with proxy.enabled=false and no explicit "
    "proxy variables so quota authentication is not captured by a traffic proxy.",
}


def number(value: Any, minimum: float = 0, maximum: float = 1e15) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def reset_time(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            value = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    return number(value, 1, 253402300799)


@dataclass(frozen=True)
class Window:
    bucket: str
    used_percent: float | None
    resets_at: float | None = None
    window_minutes: float | None = None
    remaining_amount: float | None = None
    disabled: bool = False


def parse_windows(provider: str, payload: Any) -> list[Window]:
    """Read known provider schemas. Unknown/free-text fields are never retained."""
    if not isinstance(payload, dict):
        return []
    result: list[Window] = []
    if provider == "codex":
        data = payload.get("result", payload)
        if not isinstance(data, dict):
            return []
        buckets = data.get("rateLimitsByLimitId")
        if not isinstance(buckets, dict):
            buckets = {"codex": data.get("rateLimits")}
        for i, (key, bucket) in enumerate(list(buckets.items())[:32]):
            if not isinstance(bucket, dict):
                continue
            label = "Codex" if key == "codex" else f"Additional limit {i + 1}"
            for name in ("primary", "secondary"):
                window = bucket.get(name)
                if not isinstance(window, dict):
                    continue
                result.append(
                    Window(
                        f"{label} {name}",
                        number(window.get("usedPercent"), 0, 100),
                        reset_time(window.get("resetsAt")),
                        number(window.get("windowDurationMins"), 1, 525600),
                    ),
                )
    elif provider == "claude":
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            return []
        for key, label, minutes in (("five_hour", "5-hour", 300), ("seven_day", "Weekly", 10080)):
            window = limits.get(key)
            if isinstance(window, dict):
                result.append(
                    Window(
                        label,
                        number(window.get("used_percentage"), 0, 100),
                        reset_time(window.get("resets_at")),
                        minutes,
                    ),
                )
    elif provider == "agy":
        command = payload.get("command")
        if payload.get("status") != "SUCCESS" or not isinstance(command, dict):
            return []
        if command.get("name") not in ("usage", "quota"):
            return []
        data = command.get("data")
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            return []
        for i, group in enumerate(groups[:16]):
            if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
                continue
            # Provider-supplied descriptions/names can contain identities. Map
            # only known labels, and number unfamiliar model groups.
            group_name = group.get("name")
            label = {"Gemini Models": "Gemini", "Claude and GPT models": "Claude/GPT"}.get(
                group_name if isinstance(group_name, str) else "",
                f"Model group {i + 1}",
            )
            for j, window in enumerate(group["buckets"][:16]):
                if not isinstance(window, dict):
                    continue
                cadence = window.get("window")
                cadences: dict[str, tuple[str, int | None]] = {
                    "5h": ("5-hour", 300),
                    "weekly": ("Weekly", 10080),
                }
                title, duration = cadences.get(
                    cadence if isinstance(cadence, str) else "",
                    (f"Limit {j + 1}", None),
                )
                remaining = number(window.get("remaining_fraction"), 0, 1)
                result.append(
                    Window(
                        f"{label} {title}",
                        None if remaining is None else round(100 * (1 - remaining), 6),
                        reset_time(window.get("reset_time")),
                        duration,
                        number(window.get("remaining_amount")),
                        window.get("disabled") is True,
                    ),
                )
    return result


def usage_dir() -> Path:
    path = get_config_root() / "usage"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


class UsageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or usage_dir() / "subscriptions.db"

    def record(
        self,
        provider: str,
        profile: str,
        windows: list[Window],
        *,
        observed_at: float | None = None,
        error: str | None = None,
    ) -> None:
        if provider not in PROVIDERS or (error is not None and error not in ERRORS):
            raise ValueError("Invalid usage source or error code")
        validate_profile_name(profile)
        now = time.time()
        if observed_at is None:
            observed_at = now if error is None else None
        if observed_at is not None and number(observed_at, 1, now + 5) is None:
            raise ValueError("Invalid observation timestamp")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create privately before SQLite opens the file. Do not change an
        # existing file's permissions or follow a planted symlink.
        if self.path.is_symlink():
            raise ValueError("Usage database must not be a symlink")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions ("
                "provider TEXT, profile TEXT, windows TEXT, observed_at REAL, "
                "checked_at REAL NOT NULL, error TEXT, PRIMARY KEY(provider, profile))",
            )
            if error is not None:
                db.execute(
                    "INSERT INTO subscriptions VALUES (?, ?, '[]', NULL, ?, ?) "
                    "ON CONFLICT(provider, profile) DO UPDATE SET "
                    "checked_at=excluded.checked_at, error=excluded.error",
                    (provider, profile, now, error),
                )
            else:
                db.execute(
                    "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, NULL) "
                    "ON CONFLICT(provider, profile) DO UPDATE SET "
                    "windows=excluded.windows, observed_at=excluded.observed_at, "
                    "checked_at=excluded.checked_at, error=NULL "
                    "WHERE excluded.observed_at >= subscriptions.observed_at "
                    "OR subscriptions.observed_at IS NULL",
                    (provider, profile, json.dumps([asdict(w) for w in windows]), observed_at, now),
                )

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        rows: list[dict[str, Any]] = []
        if self.path.is_file():
            uri = self.path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscriptions'",
                ).fetchone():
                    db.row_factory = sqlite3.Row
                    for record in db.execute(
                        "SELECT * FROM subscriptions ORDER BY profile, provider",
                    ):
                        row = dict(record)
                        row["windows"] = json.loads(row["windows"])
                        age = now - row["observed_at"] if row["observed_at"] else None
                        row["stale"] = age is None or age > STALE_SECONDS or bool(row["error"])
                        row["message"] = ERRORS.get(row["error"], "")
                        row["source"] = SOURCES.get(row["provider"], "unknown")
                        for window in row["windows"]:
                            reset = window.get("resets_at")
                            window["stale"] = row["stale"] or (reset is not None and reset <= now)
                        rows.append(row)
        return {"schema_version": 1, "providers": rows}


class ProbeError(Exception):
    def __init__(self, code: str = "probe_failed") -> None:
        super().__init__(code)
        self.code = code


def _exec(manager: DockerManager, container: Any, command: list[str]) -> bytes:
    """Bounded official CLI probe. Never include stdout/stderr in an error."""
    api = manager.client.api
    config = container.attrs.get("Config", {})
    user = config.get("User", "")
    # Match the image entrypoint's host-UID mapping without invoking it again.
    for value in config.get("Env", []) or []:
        if value.startswith("USER_UID=") and value[9:].isdigit() and value[9:] != "0":
            user = value[9:]
    stream = None
    try:
        created = api.exec_create(
            container.id,
            ["timeout", "--signal=TERM", "--kill-after=2s", "30s", *command],
            stdout=True,
            stderr=False,
            stdin=False,
            tty=False,
            workdir="/tmp",
            user=user,
            environment={"AGY_CLI_DISABLE_AUTO_UPDATE": "true", "BROWSER": "/bin/false"},
        )
        stream = api.exec_start(created["Id"], stream=True)
        output = bytearray()
        for chunk in stream:
            if len(output) + len(chunk) > MAX_BYTES:
                raise ProbeError()
            output.extend(chunk)
        if api.exec_inspect(created["Id"]).get("ExitCode") != 0:
            raise ProbeError()
        return bytes(output)
    except Exception as exc:
        raise ProbeError() from exc
    finally:
        if stream is not None and hasattr(stream, "close"):
            stream.close()


def probe(manager: DockerManager, container: Any, provider: str) -> list[Window]:
    # Respect the user's network policy: never silently bypass an intercepting
    # proxy, and never add authenticated quota traffic to its capture database.
    proxy_env = any(
        key.lower() in ("http_proxy", "https_proxy", "all_proxy") and bool(value)
        for entry in container.attrs.get("Config", {}).get("Env", []) or []
        for key, _, value in [entry.partition("=")]
    )
    if (container.labels or {}).get("vibepod.proxy-policy") or proxy_env:
        raise ProbeError("proxy_capture_enabled")
    if provider == "codex":
        script = files("vibepod.resources").joinpath("codex-quota.cjs").read_text()
        output = _exec(manager, container, ["node", "-e", script])
    elif provider == "agy":
        version = _exec(manager, container, ["agy", "--version"])
        match = re.search(rb"\b(\d+)\.(\d+)\.(\d+)\b", version)
        if match is None or tuple(int(n) for n in match.groups()) < (1, 1, 11):
            raise ProbeError("unsupported_version")
        # agy may invoke open/xdg-open directly instead of honoring BROWSER.
        # A private, empty working directory also avoids project startup config.
        script = """set -eu
agy_binary=$(command -v agy)
probe_dir=$(mktemp -d /tmp/vibepod-quota.XXXXXX)
trap 'rm -rf "$probe_dir"' EXIT
printf '#!/bin/sh\\nexit 1\\n' > "$probe_dir/open"
cp "$probe_dir/open" "$probe_dir/xdg-open"
chmod 700 "$probe_dir/open" "$probe_dir/xdg-open"
cd "$probe_dir"
PATH="$probe_dir:$PATH" BROWSER="$probe_dir/open" \\
    "$agy_binary" -p /usage --output-format json
"""
        output = _exec(manager, container, ["sh", "-c", script])
    else:
        raise ProbeError("unavailable")
    try:
        windows = parse_windows(provider, json.loads(output))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProbeError() from exc
    if not any(
        not w.disabled and (w.used_percent is not None or w.remaining_amount is not None)
        for w in windows
    ):
        raise ProbeError("unavailable")
    return windows


def refresh(
    profile: str,
    providers: tuple[str, ...] = PROVIDERS,
    container_name: str | None = None,
    store: UsageStore | None = None,
) -> None:
    store = store or UsageStore()
    manager = None
    containers: list[Any] = []
    runtime_failed = False
    if any(p != "claude" for p in providers):
        try:
            manager = DockerManager()
            containers = manager.list_managed()
        except Exception:
            runtime_failed = True
    for provider in providers:
        try:
            if provider == "claude":
                path = agent_config_dir("claude", profile) / "vibepod-usage" / "snapshot.json"
                if not path.is_file() or path.is_symlink():
                    raise ProbeError("no_snapshot")
                with path.open("rb") as handle:
                    raw = handle.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ProbeError()
                payload = json.loads(raw)
                observed = number(payload.get("observed_at"), 1, time.time() + 5)
                windows = parse_windows("claude", payload)
                if observed is None or not any(w.used_percent is not None for w in windows):
                    raise ProbeError("unavailable")
                store.record(provider, profile, windows, observed_at=observed)
                continue
            if runtime_failed:
                raise ProbeError("runtime_unavailable")
            matches = [
                c
                for c in containers
                if c.status == "running"
                and (c.labels or {}).get("vibepod.managed") == "true"
                and (c.labels or {}).get("vibepod.agent") == provider
                and (c.labels or {}).get("vibepod.profile", "default") == profile
                and not (c.labels or {}).get("vibepod.provider")
                and (container_name is None or c.name == container_name or c.id == container_name)
            ]
            if not matches:
                raise ProbeError("not_running")
            if len(matches) > 1:
                raise ProbeError("multiple_containers")
            assert manager is not None
            store.record(provider, profile, probe(manager, matches[0], provider))
        except ProbeError as exc:
            store.record(provider, profile, [], error=exc.code)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            store.record(provider, profile, [], error="probe_failed")


def setup_claude(profile: str, *, disable: bool = False) -> None:
    """Install a passive status-line wrapper, preserving the user's original command."""
    root = agent_config_dir("claude", profile)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings_path = root / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be a JSON object")
    directory = root / "vibepod-usage"
    if directory.is_symlink() or settings_path.is_symlink():
        raise ValueError("Usage setup does not modify symlinked settings or directories")
    directory.mkdir(mode=0o700, exist_ok=True)
    original_path = directory / "original.json"
    status = settings.get("statusLine")
    installed = isinstance(status, dict) and status.get("command") == CLAUDE_COMMAND
    if disable:
        if installed:
            original = json.loads(original_path.read_text())
            if original is None:
                settings.pop("statusLine", None)
            else:
                settings["statusLine"] = original
    else:
        if not installed:
            _atomic_json(original_path, status)
        script = files("vibepod.resources").joinpath("claude-quota-statusline.cjs").read_text()
        script_path = directory / "statusline.cjs"
        if script_path.is_symlink():
            raise ValueError("Status-line script must not be a symlink")
        script_path.write_text(script)
        settings["statusLine"] = {
            **(status if isinstance(status, dict) else {}),
            "type": "command",
            "command": CLAUDE_COMMAND,
        }
    _atomic_json(settings_path, settings)


def _atomic_json(path: Path, payload: Any) -> None:
    import tempfile

    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".usage-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

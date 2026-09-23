"""Container-local configuration lifetime: no host-side secret files or leases.

The wrapper argv is deliberately free of whitespace and shell metacharacters.
Older agent images hand the launch argv to ``sh -c "$*"``, which joins and
re-parses it as shell source; the real agent command therefore travels in an
environment variable, and the bootstrap script is bind-mounted, never inlined.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from importlib.resources import files
from pathlib import Path

from vibepod.core.config import get_config_root

#: Container paths of the mounted bootstrap scripts (read-only bind mounts).
BOOTSTRAP_MOUNT = "/opt/vibepod/provider-bootstrap.cjs"
BOOTSTRAP_MOUNT_PY = "/opt/vibepod/provider-bootstrap.py"
BOOTSTRAP_MOUNT_SH = "/opt/vibepod/provider-bootstrap.sh"
#: JSON array with the real agent argv, consumed and unset by the bootstrap.
COMMAND_ENV = "VIBEPOD_PROVIDER_COMMAND"
#: The same argv for the sh bootstrap: one base64 word per argument, each
#: prefixed with "b" so empty arguments survive shell word splitting.
COMMAND_B64_ENV = "VIBEPOD_PROVIDER_COMMAND_B64"

#: agent -> (packaged bootstrap resource, container interpreter)
BOOTSTRAPS: dict[str, tuple[str, str]] = {
    "pi": ("provider-bootstrap.cjs", "node"),
    "codex": ("provider-bootstrap.cjs", "node"),
    "opencode": ("provider-bootstrap.cjs", "node"),
    "tau": ("provider-bootstrap.py", "python3"),
    # The jcode image ships neither Node nor Python: POSIX sh plus coreutils.
    "jcode": ("provider-bootstrap.sh", "sh"),
}

WRAPPED_AGENTS = frozenset(BOOTSTRAPS)

_BOOTSTRAP_MOUNTS = {
    "provider-bootstrap.cjs": BOOTSTRAP_MOUNT,
    "provider-bootstrap.py": BOOTSTRAP_MOUNT_PY,
    "provider-bootstrap.sh": BOOTSTRAP_MOUNT_SH,
}


def bootstrap_source(filename: str) -> str:
    return files("vibepod.resources").joinpath(filename).read_text()


def bootstrap_volume(agent: str) -> tuple[str, str, str]:
    """Install the agent's packaged bootstrap under the config root; return its mount.

    The config root (not site-packages) is used as the bind source so the file
    lives in a directory Docker Desktop shares with containers by default. The
    scripts hold no secrets and are rewritten only when the packaged copy changes.
    """
    filename = BOOTSTRAPS[agent][0]
    source = bootstrap_source(filename)
    target = get_config_root() / "runtime" / filename
    try:
        current = target.read_text()
    except OSError:
        current = None
    if current != source:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".bootstrap-")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(source)
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return str(target), _BOOTSTRAP_MOUNTS[filename], "ro"


def wrap_provider_command(agent: str, command: list[str]) -> tuple[list[str], dict[str, str]]:
    """Return shell-safe argv and the environment carrying the real command.

    Image entrypoints still run first for host UID mapping. Detached containers
    own the bootstrap and its private files, independently of the host process.
    """
    bootstrap = BOOTSTRAPS.get(agent)
    if bootstrap is None:
        return command, {}
    filename, interpreter = bootstrap
    if interpreter == "sh":
        words = ["b" + base64.b64encode(part.encode()).decode() for part in command]
        return [interpreter, _BOOTSTRAP_MOUNTS[filename]], {COMMAND_B64_ENV: " ".join(words)}
    return [interpreter, _BOOTSTRAP_MOUNTS[filename]], {COMMAND_ENV: json.dumps(command)}

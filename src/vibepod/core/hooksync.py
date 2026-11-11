"""Shared plumbing for the hook-file integrations (herdr, dash).

Both integrations do the same thing with different payloads: copy VibePod-owned
scripts into an agent's config directory, then let the user add their own via
``<integration>.integrations.<agent>`` config entries. The copy is idempotent —
VibePod-owned destinations are overwritten on every run, everything else is
left alone — and never raises: a broken integration must not block a run.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from vibepod.utils.console import warning


def copy_into(
    config_dir: Path,
    dest_rel: str,
    content: bytes,
    *,
    executable: bool,
    label: str,
) -> bool:
    """Write *content* to ``config_dir/dest_rel``; False when it was refused."""
    dest = (config_dir / dest_rel).resolve()
    if config_dir.resolve() not in dest.parents:
        warning(f"{label}: destination '{dest_rel}' escapes the agent config dir, skipping")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    if executable:
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return True


def sync_integration_files(
    *,
    label: str,
    root: Path,
    builtin: list[tuple[str, str]],
    config_dir: Path,
    custom: list[Any],
    agent: str,
) -> int:
    """Copy the built-in files for *agent*, then the user's own. Returns the count.

    *builtin* maps packaged resource paths (relative to *root*) to destinations
    relative to the agent config dir; *custom* holds ``{source, dest}`` entries
    read from the user's config.
    """
    synced = 0
    for resource_rel, dest_rel in builtin:
        source = root / resource_rel
        if not source.is_file():
            warning(f"{label}: packaged resource missing: {resource_rel}")
            continue
        if copy_into(
            config_dir,
            dest_rel,
            source.read_bytes(),
            executable=resource_rel.endswith(".sh"),
            label=label,
        ):
            synced += 1

    for entry in custom or []:
        if not isinstance(entry, dict) or "source" not in entry or "dest" not in entry:
            warning(f"{label}: invalid integration entry for '{agent}': {entry!r}")
            continue
        source = Path(str(entry["source"])).expanduser()
        if not source.is_file():
            warning(f"{label}: integration source not found: {source}")
            continue
        if copy_into(
            config_dir,
            str(entry["dest"]),
            source.read_bytes(),
            executable=os.access(source, os.X_OK),
            label=label,
        ):
            synced += 1
    return synced

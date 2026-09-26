#!/usr/bin/env python3
"""Fail when docs/import.md drifts from IMPORT_SPECS."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vibepod.constants import SUPPORTED_AGENTS  # noqa: E402
from vibepod.core.agent_import import agent_import_entries  # noqa: E402


def main() -> int:
    docs = (Path(__file__).resolve().parents[1] / "docs" / "import.md").read_text()
    problems: list[str] = []
    for agent in SUPPORTED_AGENTS:
        if f"`{agent}`" not in docs:
            problems.append(f"docs/import.md is missing agent '{agent}'")
        for entry in agent_import_entries(agent):
            root = entry.source.split("/")[0]
            if root not in docs:
                problems.append(f"docs/import.md is missing source root '{root}' ({agent})")
    for problem in sorted(set(problems)):
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

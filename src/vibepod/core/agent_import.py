"""Import an existing agent configuration into a VibePod profile.

Most agents persist a container ``HOME`` (``HOME=/config``; agy uses
``/home/agy``), so the mapping from a host home is the identity on the path
relative to ``HOME``: ``~/.tau/providers.json`` becomes
``<agent dir>/.tau/providers.json``. Four agents mount their own config
directory instead of a home -- claude (``CLAUDE_CONFIG_DIR=/claude``), qwen
(``QWEN_CONFIG_DIR=/qwen``), freebuff (``FREEBUFF_CONFIG_DIR=/freebuff``) and
hermes (``HERMES_HOME=/opt/data``, baked into the image) -- so their host
directory maps onto the destination root.
"""

from __future__ import annotations

import fnmatch
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

Category = Literal[
    "settings",
    "models",
    "mcp",
    "hooks",
    "skills",
    "memory",
    "sessions",
    "credentials",
    "other",
]

ALL_CATEGORIES: frozenset[Category] = frozenset(get_args(Category))
#: Copied unless the user narrows the selection.
DEFAULT_CATEGORIES: frozenset[Category] = frozenset(
    {"settings", "models", "mcp", "hooks", "skills", "memory"},
)
#: Never copied without an explicit flag.
OPT_IN_CATEGORIES: frozenset[Category] = frozenset({"sessions", "credentials", "other"})

#: Flag that opts each opt-in category in.
CATEGORY_FLAGS: dict[Category, str] = {
    "credentials": "--with-credentials",
    "sessions": "--with-sessions",
    "other": "--with-other",
}


@dataclass(frozen=True)
class ImportEntry:
    """One source path mapped onto the destination agent directory.

    ``source`` is relative to the source home (host import) and ``dest`` is
    relative to the destination agent config directory; ``""`` means the
    destination root. ``exclude`` holds glob patterns, matched against the
    path relative to ``source``, that are never copied.
    """

    source: str
    dest: str
    category: Category
    exclude: tuple[str, ...] = ()
    note: str | None = None


IMPORT_SPECS: dict[str, tuple[ImportEntry, ...]] = {
    "claude": (
        ImportEntry(".claude/settings.json", "settings.json", "settings"),
        ImportEntry(".claude/CLAUDE.md", "CLAUDE.md", "memory"),
        ImportEntry(".claude/agents", "agents", "skills"),
        ImportEntry(".claude/commands", "commands", "skills"),
        ImportEntry(".claude/skills", "skills", "skills"),
        ImportEntry(".claude/plugins", "plugins", "skills"),
        ImportEntry(".claude/projects", "projects", "sessions"),
        ImportEntry(".claude/todos", "todos", "sessions"),
        ImportEntry(".claude/shell-snapshots", "shell-snapshots", "sessions"),
        ImportEntry(".claude/statsig", "statsig", "sessions"),
        ImportEntry(".claude/history.jsonl", "history.jsonl", "sessions"),
        ImportEntry(
            ".claude/.credentials.json",
            ".credentials.json",
            "credentials",
            note=(
                "On macOS Claude Code keeps its OAuth token in the Keychain, "
                "so this file usually does not exist; log in inside the pod."
            ),
        ),
    ),
    "opencode": (
        ImportEntry(
            ".config/opencode/opencode.json",
            ".config/opencode/opencode.json",
            "settings",
        ),
        ImportEntry(
            ".config/opencode/opencode.jsonc",
            ".config/opencode/opencode.jsonc",
            "settings",
        ),
        ImportEntry(".config/opencode/AGENTS.md", ".config/opencode/AGENTS.md", "memory"),
        ImportEntry(".config/opencode/command", ".config/opencode/command", "skills"),
        ImportEntry(".config/opencode/agent", ".config/opencode/agent", "skills"),
        ImportEntry(".config/opencode/plugin", ".config/opencode/plugin", "hooks"),
        ImportEntry(
            ".local/share/opencode/auth.json",
            ".local/share/opencode/auth.json",
            "credentials",
        ),
    ),
    "codex": (
        ImportEntry(".codex/config.toml", ".codex/config.toml", "settings"),
        ImportEntry(".codex/AGENTS.md", ".codex/AGENTS.md", "memory"),
        ImportEntry(".codex/prompts", ".codex/prompts", "skills"),
        ImportEntry(".codex/auth.json", ".codex/auth.json", "credentials"),
    ),
    # unverified -- replaced by researched entries once the host inventory runs
    "gemini": (ImportEntry(".gemini", ".gemini", "settings"),),
    "devstral": (ImportEntry(".config/mistral", ".config/mistral", "settings"),),
    "auggie": (ImportEntry(".augment", ".augment", "settings"),),
    "copilot": (ImportEntry(".copilot", ".copilot", "settings"),),
    "pi": (
        ImportEntry(".pi/agent/models.json", ".pi/agent/models.json", "models"),
        ImportEntry(".pi/agent/auth.json", ".pi/agent/auth.json", "credentials"),
        ImportEntry(
            ".pi",
            ".pi",
            "settings",
            exclude=("agent/models.json", "agent/auth.json"),
        ),
    ),
    "agy": (ImportEntry(".agy", ".agy", "settings"),),
    "tau": (
        ImportEntry(".tau/providers.json", ".tau/providers.json", "models"),
        ImportEntry(".tau/catalog.toml", ".tau/catalog.toml", "models"),
        ImportEntry(".tau/credentials.json", ".tau/credentials.json", "credentials"),
        ImportEntry(
            ".tau",
            ".tau",
            "settings",
            exclude=("providers.json", "catalog.toml", "credentials.json"),
        ),
    ),
    "jcode": (
        ImportEntry(".jcode", ".jcode", "settings"),
        ImportEntry(".config/jcode", ".config/jcode", "models"),
    ),
    "freebuff": (ImportEntry(".config/manicode", "", "settings"),),
    "qwen": (ImportEntry(".qwen", "", "settings"),),
    "dsh": (ImportEntry(".dsh", ".dsh", "settings"),),
    # ~/.hermes also holds the installer's hermes-agent checkout, so only the
    # state HERMES_HOME documents is listed; the rest is reported as other.
    "hermes": (
        ImportEntry(".hermes/config.yaml", "config.yaml", "settings"),
        ImportEntry(".hermes/SOUL.md", "SOUL.md", "memory"),
        ImportEntry(".hermes/memories", "memories", "memory"),
        ImportEntry(".hermes/skills", "skills", "skills"),
        ImportEntry(".hermes/sessions", "sessions", "sessions"),
        ImportEntry(".hermes/.env", ".env", "credentials"),
        ImportEntry(".hermes/auth.json", "auth.json", "credentials"),
    ),
}


def agent_import_entries(agent: str) -> tuple[ImportEntry, ...]:
    """Return the import entries for *agent*."""
    if agent not in IMPORT_SPECS:
        raise ValueError(f"Unsupported agent: {agent}")
    return IMPORT_SPECS[agent]


@dataclass(frozen=True)
class PlannedFile:
    source: Path
    dest: Path
    category: Category


@dataclass(frozen=True)
class SkippedPath:
    source: Path
    reason: str


@dataclass
class ImportPlan:
    agent: str
    source_root: Path
    dest_root: Path
    files: list[PlannedFile]
    skipped: list[SkippedPath]
    conflicts: list[PlannedFile]
    unclassified: list[Path]

    @property
    def is_empty(self) -> bool:
        return not self.files


def _iter_files(root: Path) -> list[Path]:
    """Every regular file under *root*, or *root* itself when it is a file."""
    if root.is_symlink():
        return [root]
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_symlink() or p.is_file())


def _is_excluded(relative: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def plan_import(
    agent: str,
    source_root: Path,
    dest_root: Path,
    categories: frozenset[Category] | set[Category],
    entries: tuple[ImportEntry, ...] | None = None,
) -> ImportPlan:
    """Resolve *agent*'s entries under *source_root* into a concrete plan.

    Pure: reads the filesystem, writes nothing. *entries* overrides the table,
    which is how a profile-to-profile copy reuses the same classification.
    """
    resolved_entries = entries if entries is not None else agent_import_entries(agent)
    files: list[PlannedFile] = []
    skipped: list[SkippedPath] = []
    conflicts: list[PlannedFile] = []
    claimed: set[Path] = set()

    for entry in resolved_entries:
        entry_root = source_root / entry.source
        entry_root_is_dir = entry_root.is_dir() and not entry_root.is_symlink()
        for path in _iter_files(entry_root):
            relative = path.relative_to(entry_root).as_posix() if entry_root_is_dir else path.name
            if _is_excluded(relative, entry.exclude):
                continue
            if path in claimed:
                continue
            claimed.add(path)
            if path.is_symlink() or _has_symlink_component(path, source_root):
                skipped.append(SkippedPath(path, "symlink, not followed"))
                continue
            if entry.category not in categories:
                flag = CATEGORY_FLAGS.get(entry.category)
                reason = f"category '{entry.category}' not selected"
                skipped.append(SkippedPath(path, f"{reason} ({flag})" if flag else reason))
                continue
            dest = (
                dest_root / entry.dest / relative if entry_root_is_dir else dest_root / entry.dest
            )
            planned = PlannedFile(path, dest, entry.category)
            files.append(planned)
            if dest.exists():
                conflicts.append(planned)

    unclassified = _unclassified(agent, source_root, claimed)
    return ImportPlan(agent, source_root, dest_root, files, skipped, conflicts, unclassified)


CREDENTIAL_FILE_MODE = 0o600
CREDENTIAL_DIR_MODE = 0o700


class ImportConflictError(RuntimeError):
    """Raised when a plan would overwrite existing files and force is off."""

    def __init__(self, conflicts: list[PlannedFile]) -> None:
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} destination file(s) already exist")


@dataclass
class ImportResult:
    copied: int
    failed: list[tuple[Path, str]]


def apply_import(plan: ImportPlan, *, force: bool) -> ImportResult:
    """Copy every file in *plan*. Raises ImportConflictError unless *force*.

    ``shutil.copyfile`` rather than ``copy2``: host mtimes and modes carry no
    meaning inside the pod, and credential files get their mode set explicitly.
    """
    if plan.conflicts and not force:
        raise ImportConflictError(plan.conflicts)

    copied = 0
    failed: list[tuple[Path, str]] = []
    for planned in plan.files:
        try:
            planned.dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(planned.source, planned.dest)
            if planned.category == "credentials":
                planned.dest.chmod(CREDENTIAL_FILE_MODE)
                planned.dest.parent.chmod(CREDENTIAL_DIR_MODE)
            copied += 1
        except OSError as exc:
            failed.append((planned.source, str(exc)))
    return ImportResult(copied, failed)


def _unclassified(agent: str, source_root: Path, claimed: set[Path]) -> list[Path]:
    """Files under the agent's source roots that no entry claimed."""
    roots: set[Path] = set()
    for entry in agent_import_entries(agent):
        top = entry.source.split("/")[0]
        roots.add(source_root / top)
    found: list[Path] = []
    for root in sorted(roots):
        for path in _iter_files(root):
            if path not in claimed and not path.is_symlink():
                found.append(path)
    return sorted(found)


#: Config files worth linting for host paths that will not resolve in a pod.
_LINTED_SUFFIXES = {".json", ".jsonc", ".toml", ".yaml", ".yml", ".md"}
_HOST_PATH_RE = re.compile(r"(/Users/[^\"'\s,:]+|/home/[^\"'\s,:]+)")


def scan_host(home: Path) -> dict[str, list[Path]]:
    """Map each agent to the source roots that exist and hold files under *home*."""
    found: dict[str, list[Path]] = {}
    for agent in IMPORT_SPECS:
        roots: list[Path] = []
        for top in sorted({entry.source.split("/")[0] for entry in agent_import_entries(agent)}):
            candidate = home / top
            if _iter_files(candidate):
                roots.append(candidate)
        if roots:
            found[agent] = roots
    return found


def host_path_warnings(paths: list[Path]) -> list[tuple[Path, str]]:
    """Flag copied config files that embed absolute host paths.

    The pod mounts the project at /workspace and has its own home, so a host
    path baked into a hook command or an MCP server entry will not resolve.
    Reported, never rewritten.
    """
    warnings: list[tuple[Path, str]] = []
    for path in paths:
        if path.suffix.lower() not in _LINTED_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        matches = sorted(set(_HOST_PATH_RE.findall(text)))
        if matches:
            warnings.append((path, ", ".join(matches[:3])))
    return warnings

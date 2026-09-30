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
import os
import re
import secrets
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
    # Known token files are listed as credentials; anything else matching
    # CREDENTIAL_NAME_PATTERNS under a directory entry is reclassified too.
    "gemini": (
        ImportEntry(".gemini/oauth_creds.json", ".gemini/oauth_creds.json", "credentials"),
        ImportEntry(
            ".gemini/mcp-oauth-tokens.json",
            ".gemini/mcp-oauth-tokens.json",
            "credentials",
        ),
        ImportEntry(".gemini/.env", ".gemini/.env", "credentials"),
        ImportEntry(".gemini", ".gemini", "settings"),
    ),
    "devstral": (ImportEntry(".config/mistral", ".config/mistral", "settings"),),
    "auggie": (
        ImportEntry(".augment/session.json", ".augment/session.json", "credentials"),
        ImportEntry(".augment", ".augment", "settings"),
    ),
    "copilot": (
        ImportEntry(
            ".copilot/config.json",
            ".copilot/config.json",
            "credentials",
            note=(
                "Without a system keychain Copilot CLI stores its token in this file, "
                "so it is treated as a credential."
            ),
        ),
        ImportEntry(".copilot", ".copilot", "settings"),
    ),
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
        ImportEntry(".jcode/auth.json", ".jcode/auth.json", "credentials"),
        ImportEntry(".jcode", ".jcode", "settings"),
        ImportEntry(".config/jcode", ".config/jcode", "models"),
    ),
    "freebuff": (
        ImportEntry(".config/manicode/credentials.json", "credentials.json", "credentials"),
        ImportEntry(".config/manicode", "", "settings"),
    ),
    "qwen": (
        ImportEntry(".qwen/oauth_creds.json", "oauth_creds.json", "credentials"),
        ImportEntry(".qwen/.env", ".env", "credentials"),
        ImportEntry(".qwen", "", "settings"),
    ),
    "dsh": (
        ImportEntry(".dsh/.credentials.yaml", ".dsh/.credentials.yaml", "credentials"),
        ImportEntry(".dsh", ".dsh", "settings"),
    ),
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


@dataclass(frozen=True)
class AgentRoot:
    """A directory the agent owns on the host, and where it lands in the destination.

    Used to detect an installation, to find files no entry claims, and to map
    those files onto the destination. ``exclude`` holds glob patterns, relative
    to ``source``, that are neither reported nor copied.
    """

    source: str
    dest: str
    exclude: tuple[str, ...] = ()


#: The agent-specific directories under the source home. Generic parents such
#: as ``~/.config`` are never listed: they hold every other tool's config too.
AGENT_ROOTS: dict[str, tuple[AgentRoot, ...]] = {
    "claude": (AgentRoot(".claude", ""),),
    "opencode": (
        AgentRoot(".config/opencode", ".config/opencode"),
        AgentRoot(".local/share/opencode", ".local/share/opencode"),
    ),
    "codex": (AgentRoot(".codex", ".codex"),),
    "gemini": (AgentRoot(".gemini", ".gemini"),),
    "devstral": (AgentRoot(".config/mistral", ".config/mistral"),),
    "auggie": (AgentRoot(".augment", ".augment"),),
    "copilot": (AgentRoot(".copilot", ".copilot"),),
    "pi": (AgentRoot(".pi", ".pi"),),
    "agy": (AgentRoot(".agy", ".agy"),),
    "tau": (AgentRoot(".tau", ".tau"),),
    "jcode": (AgentRoot(".jcode", ".jcode"), AgentRoot(".config/jcode", ".config/jcode")),
    "freebuff": (AgentRoot(".config/manicode", ""),),
    "qwen": (AgentRoot(".qwen", ""),),
    "dsh": (AgentRoot(".dsh", ".dsh"),),
    # The installer clones its hermes-agent checkout into ~/.hermes.
    "hermes": (AgentRoot(".hermes", "", exclude=("hermes-agent", "hermes-agent/*")),),
}


def agent_roots(agent: str) -> tuple[AgentRoot, ...]:
    """Return the agent-specific source roots for *agent*."""
    if agent not in AGENT_ROOTS:
        raise ValueError(f"Unsupported agent: {agent}")
    return AGENT_ROOTS[agent]


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


#: File names treated as credentials wherever a directory entry finds them, so
#: an agent's unlisted token file is never copied by a default-on category.
CREDENTIAL_NAME_PATTERNS: tuple[str, ...] = (
    "*oauth*",
    "*credential*",
    "*token*",
    "*secret*",
    "auth.json",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
)


def _looks_like_credential(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in CREDENTIAL_NAME_PATTERNS)


def _is_excluded(relative: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


#: The agent directory is mounted read-write into containers, so a symlink in
#: it may have been planted to redirect a host-side write.
_DEST_SYMLINK_REASON = "destination path contains a symlink, not written"


def plan_import(
    agent: str,
    source_root: Path,
    dest_root: Path,
    categories: frozenset[Category] | set[Category],
    entries: tuple[ImportEntry, ...] | None = None,
    unclassified_roots: tuple[AgentRoot, ...] | None = None,
) -> ImportPlan:
    """Resolve *agent*'s entries under *source_root* into a concrete plan.

    Pure: reads the filesystem, writes nothing. *entries* overrides the table,
    which is how a profile-to-profile copy reuses the same classification;
    *unclassified_roots* overrides where unmapped files are looked for, which a
    profile copy needs because its entries no longer name the host dotdirs.
    """
    resolved_entries = entries if entries is not None else agent_import_entries(agent)
    files: list[PlannedFile] = []
    skipped: list[SkippedPath] = []
    conflicts: list[PlannedFile] = []
    claimed: set[Path] = set()

    def add(path: Path, dest: Path, category: Category) -> None:
        if category not in categories:
            flag = CATEGORY_FLAGS.get(category)
            reason = f"category '{category}' not selected"
            skipped.append(SkippedPath(path, f"{reason} ({flag})" if flag else reason))
            return
        if _has_symlink_component(dest, dest_root):
            skipped.append(SkippedPath(path, _DEST_SYMLINK_REASON))
            return
        planned = PlannedFile(path, dest, category)
        files.append(planned)
        if dest.exists():
            conflicts.append(planned)

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
            category = entry.category
            if entry_root_is_dir and _looks_like_credential(path.name):
                category = "credentials"
            dest = (
                dest_root / entry.dest / relative if entry_root_is_dir else dest_root / entry.dest
            )
            add(path, dest, category)

    roots = agent_roots(agent) if unclassified_roots is None else unclassified_roots
    unclassified: list[Path] = []
    for path, root in _unclassified(source_root, roots, claimed):
        if "other" not in categories:
            unclassified.append(path)
            continue
        # A file no entry claims is still a credential when its name says so.
        category = "credentials" if _looks_like_credential(path.name) else "other"
        root_relative = path.relative_to(source_root / root.source)
        add(path, dest_root / root.dest / root_relative, category)
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

    Each file is written to a temporary sibling created with its final mode
    (``0600`` for credentials) and renamed into place, so a destination is
    either the old file or the complete new one and never a symlink target.
    Host mtimes and modes carry no meaning inside the pod, so they are not kept.
    """
    if plan.conflicts and not force:
        raise ImportConflictError(plan.conflicts)

    copied = 0
    failed: list[tuple[Path, str]] = []
    for planned in plan.files:
        try:
            _copy_file(planned, plan.dest_root)
            copied += 1
        except OSError as exc:
            failed.append((planned.source, str(exc)))
    return ImportResult(copied, failed)


def _copy_file(planned: PlannedFile, dest_root: Path) -> None:
    if _has_symlink_component(planned.dest, dest_root):
        raise OSError(f"{planned.dest}: {_DEST_SYMLINK_REASON}")
    credential = planned.category == "credentials"
    parent = planned.dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(parent, dest_root):
        raise OSError(f"{planned.dest}: {_DEST_SYMLINK_REASON}")
    if credential:
        parent.chmod(CREDENTIAL_DIR_MODE)
    mode = CREDENTIAL_FILE_MODE if credential else 0o666
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    temp = parent / f".{planned.dest.name}.vp-import-{secrets.token_hex(4)}"
    fd = os.open(temp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as out, planned.source.open("rb") as src:
            shutil.copyfileobj(src, out)
        if credential:
            temp.chmod(CREDENTIAL_FILE_MODE)
        os.replace(temp, planned.dest)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _unclassified(
    source_root: Path,
    roots: tuple[AgentRoot, ...],
    claimed: set[Path],
) -> list[tuple[Path, AgentRoot]]:
    """Files under *roots* that no entry claimed, each with the root it was found in."""
    found: dict[Path, AgentRoot] = {}
    for root in roots:
        base = source_root / root.source
        for path in _iter_files(base):
            if path in claimed or path in found or path.is_symlink():
                continue
            if _is_excluded(path.relative_to(base).as_posix(), root.exclude):
                continue
            found[path] = root
    return sorted(found.items())


#: Config files worth linting for host paths that will not resolve in a pod.
_LINTED_SUFFIXES = {".json", ".jsonc", ".toml", ".yaml", ".yml", ".md"}
_HOST_PATH_RE = re.compile(r"(/Users/[^\"'\s,:]+|/home/[^\"'\s,:]+)")


def scan_host(home: Path) -> dict[str, list[Path]]:
    """Map each agent to the source roots that exist and hold files under *home*."""
    found: dict[str, list[Path]] = {}
    for agent in IMPORT_SPECS:
        roots: list[Path] = []
        for root in agent_roots(agent):
            candidate = home / root.source
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

"""Detect agent-printed resume hints and map them to VibePod commands."""

from __future__ import annotations

import re
from collections.abc import Callable

from vibepod.utils.console import console, info

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI sequences (colors, cursor movement)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (titles, hyperlinks)
    r"|\x1b[@-Z\\-_]",  # other single-character escapes
)

# Session identifiers / names / file names as agents print them. May contain
# dots (file names) but never end with one, so sentence punctuation around the
# hint is not captured.
_TOKEN = r"[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9_/-])?"

# The binary name must not be preceded by a word/path character, so hints in
# prose match but occurrences inside paths or our own `vp run <agent> -- ...`
# output do not.
_BOUNDARY = r"(?<![\w./-])"


def _fixed(args: str) -> Callable[[re.Match[str]], str]:
    def _format(match: re.Match[str]) -> str:
        del match
        return args

    return _format


def _with_id(prefix: str) -> Callable[[re.Match[str]], str]:
    def _format(match: re.Match[str]) -> str:
        return f"{prefix} {match.group(1)}"

    return _format


def _verbatim(match: re.Match[str]) -> str:
    return match.group(1)


# Per-agent resume hint patterns. Hints must appear on a single line: patterns
# only allow spaces/tabs between tokens so unrelated text on following lines is
# never mistaken for a session identifier. Each formatter rebuilds the agent's
# own suggested arguments, which are then passed through after `--`.
_HINT_PATTERNS: dict[str, tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...]] = {
    "claude": (
        (
            re.compile(rf"{_BOUNDARY}claude[ \t]+(?:-r|--resume)[ \t]+({_TOKEN})"),
            _with_id("--resume"),
        ),
        (re.compile(rf"{_BOUNDARY}claude[ \t]+--continue(?![\w-])"), _fixed("--continue")),
    ),
    "codex": (
        (re.compile(rf"{_BOUNDARY}codex[ \t]+resume[ \t]+({_TOKEN})"), _with_id("resume")),
        (
            re.compile(rf"{_BOUNDARY}codex[ \t]+resume[ \t]+--last(?![\w-])"),
            _fixed("resume --last"),
        ),
    ),
    # Pi prints "To resume this session: pi [--session-dir <dir>] --session
    # <id>"; its -r/--resume and -c/--continue flags are bare toggles (session
    # picker / last session) and never carry an identifier.
    "pi": (
        (
            re.compile(
                rf"{_BOUNDARY}pi[ \t]+"
                rf"((?:--session-dir[ \t]+/?{_TOKEN}[ \t]+)?--session[ \t]+{_TOKEN})",
            ),
            _verbatim,
        ),
        (re.compile(rf"{_BOUNDARY}pi[ \t]+(?:-c|--continue)(?![\w-])"), _fixed("--continue")),
    ),
    "copilot": (
        (
            re.compile(rf"{_BOUNDARY}copilot[ \t]+(?:-r|--resume)[ \t]+({_TOKEN})"),
            _with_id("--resume"),
        ),
        (re.compile(rf"{_BOUNDARY}copilot[ \t]+--continue(?![\w-])"), _fixed("--continue")),
    ),
    "jcode": (
        (
            re.compile(rf"{_BOUNDARY}jcode[ \t]+(?:-r|--resume)[ \t]+({_TOKEN})"),
            _with_id("--resume"),
        ),
    ),
    "freebuff": (
        (
            re.compile(rf"{_BOUNDARY}freebuff[ \t]+--continue[ \t]+({_TOKEN})"),
            _with_id("--continue"),
        ),
        (re.compile(rf"{_BOUNDARY}freebuff[ \t]+--continue(?![\w-])"), _fixed("--continue")),
    ),
}


def _clean(output: str) -> str:
    text = _ANSI_RE.sub("", output)
    # Carriage returns act as line resets in raw TTY output; treat them as
    # line breaks so redrawn fragments never merge into one line.
    return text.replace("\r", "\n")


def build_resume_hint(agent: str, output: str) -> str | None:
    """Return the ``vp`` command resuming the session suggested in *output*.

    Scans the (ANSI-stripped) session output for the agent's own resume
    instruction and wraps its arguments in ``vp run <agent> -- ...``. Returns
    ``None`` when no recognizable hint is present, so exiting stays silent
    rather than risking an incorrect command.
    """
    patterns = _HINT_PATTERNS.get(agent)
    if not patterns:
        return None
    text = _clean(output)
    best: tuple[int, str] | None = None
    for pattern, formatter in patterns:
        for match in pattern.finditer(text):
            if best is None or match.end() > best[0]:
                best = (match.end(), formatter(match))
    if best is None:
        return None
    return f"vp run {agent} -- {best[1]}"


def show_resume_hint(agent: str, output_tail: bytes | None) -> None:
    """Print the VibePod resume command for a finished session, if detectable."""
    if not output_tail:
        return
    hint = build_resume_hint(agent, output_tail.decode("utf-8", errors="replace"))
    if hint is None:
        return
    info("Resume this session with:")
    console.print(f"  {hint}", markup=False, highlight=False)

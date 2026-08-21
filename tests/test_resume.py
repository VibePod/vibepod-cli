"""Resume-hint detection tests."""

from __future__ import annotations

import pytest

from vibepod.core.resume import build_resume_hint

CLAUDE_ID = "39bf1a93-ea1f-4b0a-a894-0a662e5a1d4e"


def test_detects_claude_resume_hint() -> None:
    output = f"Some transcript...\nResume this session with:\nclaude --resume {CLAUDE_ID}\n"
    assert build_resume_hint("claude", output) == f"vp run claude -- --resume {CLAUDE_ID}"


def test_detects_claude_short_flag_hint() -> None:
    output = f"claude -r {CLAUDE_ID}\n"
    assert build_resume_hint("claude", output) == f"vp run claude -- --resume {CLAUDE_ID}"


def test_detects_claude_continue_hint() -> None:
    output = "Run claude --continue to resume.\n"
    assert build_resume_hint("claude", output) == "vp run claude -- --continue"


def test_detects_codex_resume_hint() -> None:
    session = "0198a2c3-1111-7222-8333-444455556666"
    output = f"To continue this session, run codex resume {session}.\n"
    assert build_resume_hint("codex", output) == f"vp run codex -- resume {session}"


def test_detects_codex_resume_last_hint() -> None:
    output = "Tip: codex resume --last\n"
    assert build_resume_hint("codex", output) == "vp run codex -- resume --last"


def test_detects_pi_session_hint() -> None:
    # Pi prints "To resume this session: pi --session <id>" on exit; its
    # --resume flag is a bare session-picker toggle and never carries an id.
    session = "0198a2c3-1111-7222-8333-444455556666"
    output = f"To resume this session: pi --session {session}\n"
    assert build_resume_hint("pi", output) == f"vp run pi -- --session {session}"


def test_detects_pi_session_hint_with_session_dir() -> None:
    output = "To resume this session: pi --session-dir /workspace/.pi-sessions --session my-id\n"
    assert (
        build_resume_hint("pi", output)
        == "vp run pi -- --session-dir /workspace/.pi-sessions --session my-id"
    )


def test_pi_resume_flag_with_argument_is_not_a_hint() -> None:
    # "pi --resume <word>" is a picker toggle plus a prompt word, not a
    # resumable session reference — emitting it would be an incorrect command.
    assert build_resume_hint("pi", "pi --resume session-2026-08-18.jsonl\n") is None


def test_detects_pi_continue_hint() -> None:
    output = "Continue with pi --continue\n"
    assert build_resume_hint("pi", output) == "vp run pi -- --continue"


def test_detects_jcode_resume_hint() -> None:
    output = "jcode --resume my-session\n"
    assert build_resume_hint("jcode", output) == "vp run jcode -- --resume my-session"


def test_detects_freebuff_continue_hint() -> None:
    output = "freebuff --continue conv-42\n"
    assert build_resume_hint("freebuff", output) == "vp run freebuff -- --continue conv-42"


def test_strips_ansi_escape_sequences() -> None:
    output = f"\x1b[1mResume:\x1b[0m \x1b[36mclaude --resume {CLAUDE_ID}\x1b[39m\r\n"
    assert build_resume_hint("claude", output) == f"vp run claude -- --resume {CLAUDE_ID}"


def test_uses_last_hint_in_output() -> None:
    other = "11111111-2222-3333-4444-555555555555"
    output = f"claude --resume {other}\n...\nclaude --resume {CLAUDE_ID}\n"
    assert build_resume_hint("claude", output) == f"vp run claude -- --resume {CLAUDE_ID}"


def test_returns_none_without_hint() -> None:
    assert build_resume_hint("claude", "Goodbye!\n") is None


def test_returns_none_for_agent_without_patterns() -> None:
    assert build_resume_hint("gemini", f"claude --resume {CLAUDE_ID}\n") is None


def test_returns_none_for_unknown_agent() -> None:
    assert build_resume_hint("not-an-agent", "whatever") is None


def test_does_not_match_other_agents_binary() -> None:
    # A stray mention of another binary must not produce a command for this agent.
    assert build_resume_hint("codex", f"claude --resume {CLAUDE_ID}\n") is None


def test_does_not_rematch_own_vibepod_hint() -> None:
    # Our own printed hint replayed in scrollback must not create a match:
    # "claude -- --resume <id>" has "--" between binary and flag.
    output = f"vp run claude -- --resume {CLAUDE_ID}\n"
    assert build_resume_hint("claude", output) is None


@pytest.mark.parametrize("agent", ["claude", "codex", "pi"])
def test_requires_identifier_for_resume_flag(agent: str) -> None:
    # Bare "--resume" / "resume" without an identifier is not a usable hint.
    assert build_resume_hint(agent, f"{agent} --resume\n{agent} resume\n") is None


def test_show_resume_hint_prints_command(capsys: pytest.CaptureFixture[str]) -> None:
    from vibepod.core.resume import show_resume_hint

    show_resume_hint("claude", b"Resume this session with:\r\nclaude --resume abc-123\r\n")
    out = capsys.readouterr().out
    assert "Resume this session" in out
    assert "vp run claude -- --resume abc-123" in out


def test_show_resume_hint_silent_without_hint(capsys: pytest.CaptureFixture[str]) -> None:
    from vibepod.core.resume import show_resume_hint

    show_resume_hint("claude", b"Goodbye!\r\n")
    assert capsys.readouterr().out == ""


def test_show_resume_hint_handles_undecodable_bytes(capsys: pytest.CaptureFixture[str]) -> None:
    from vibepod.core.resume import show_resume_hint

    show_resume_hint("claude", b"\xff\xfe claude --resume abc-123\r\n")
    assert "vp run claude -- --resume abc-123" in capsys.readouterr().out

"""Windows console input via ReadConsoleInputW records (issue #242).

Everything here runs off Windows: the decoder is pure, and the record reader is
driven through a fake ``kernel32`` that fills real ``INPUT_RECORD`` arrays.
"""

from __future__ import annotations

import ctypes
import types
from typing import Any

import pytest

from vibepod.core import docker as docker_mod

_D = docker_mod
ENHANCED = 0x0100


def key(
    char: str | int = 0,
    vk: int = 0,
    state: int = 0,
    down: bool = True,
    repeat: int = 1,
    scan: int = 0,
) -> docker_mod._KeyEvent:
    code = ord(char) if isinstance(char, str) else char
    return docker_mod._KeyEvent(down, repeat, vk, scan, code, state)


def typed(text: str, state: int = 0) -> list[docker_mod._KeyEvent]:
    """Key-down records for *text*, one per UTF-16 code unit, plus releases."""
    units = text.encode("utf-16-le")
    events = []
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i : i + 2], "little")
        events += [key(code, state=state), key(code, state=state, down=False)]
    return events


def decode(*events: docker_mod._KeyEvent | list[docker_mod._KeyEvent]) -> bytes:
    decoder = docker_mod._ConsoleKeyDecoder()
    flat: list[docker_mod._KeyEvent] = []
    for event in events:
        flat += event if isinstance(event, list) else [event]
    return b"".join(decoder.feed(event) for event in flat) + decoder.flush()


# --- the remaining problems listed in #242 -----------------------------------


def test_a_grave_is_text_not_an_extended_key_prefix() -> None:
    # getwch() returned \xe0 for both; `à` then `x` dropped both keys.
    assert decode(typed("àx")) == "àx".encode()
    assert decode(typed("àH")) == "àH".encode()
    assert decode(typed("à")) == "à".encode()


def test_emoji_surrogate_pair_is_joined_into_one_utf8_character() -> None:
    assert decode(typed("a😀b")) == "a😀b".encode()


def test_lone_surrogate_halves_become_a_replacement_not_garbage() -> None:
    assert decode(key(0xD83D)) == b"?"
    assert decode(key(0xD83D), typed("x")) == b"?x"
    assert decode(key(0xDE00)) == b"?"


@pytest.mark.parametrize("alt", [_D._LEFT_ALT_PRESSED, _D._RIGHT_ALT_PRESSED])
def test_alt_key_is_sent_as_meta(alt: int) -> None:
    assert decode(key("x", vk=0x58, state=alt)) == b"\x1bx"
    assert decode(key("\r", vk=0x0D, state=alt)) == b"\x1b\r"  # Alt+Enter
    assert decode(key("\x08", vk=_D._VK_BACK, state=alt)) == b"\x1b\x7f"


def test_altgr_text_is_not_meta() -> None:
    altgr = _D._RIGHT_ALT_PRESSED | _D._LEFT_CTRL_PRESSED
    assert decode(key("@", vk=0x51, state=altgr)) == b"@"  # AltGr+Q, German layout
    assert decode(key("€", vk=0x45, state=altgr)) == "€".encode()
    # Ctrl+Alt with a control character is still Meta + that control character.
    ctrl_alt = _D._LEFT_ALT_PRESSED | _D._LEFT_CTRL_PRESSED
    assert decode(key("\x01", vk=0x41, state=ctrl_alt)) == b"\x1b\x01"


def test_escape_then_arrow_is_two_keys() -> None:
    # The arrow arrives as a VK record, so it cannot continue the ESC sequence.
    assert decode(key("\x1b", vk=0x1B), key(vk=0x26, state=ENHANCED)) == b"\x1b\x1b[A"


@pytest.mark.parametrize(
    "report",
    ["\x1b[97:65:97;2:3u", "\x1b[97;1:3;97u", "\x1b[97::99;1:3u", "\x1b[97;1:3u"],
)
def test_kitty_release_reports_with_alternates_or_text_are_dropped(report: str) -> None:
    assert decode(typed(report)) == b""
    assert docker_mod._KITTY_KEY_REPORT.fullmatch(report.encode())


def test_long_kitty_release_with_a_text_field_is_dropped() -> None:
    # 36 characters: more than the general 32-character escape bound.
    report = "\x1b[97:65:97;2:3;97:769:128512:128513u"
    assert len(report) > docker_mod._MAX_ESCAPE_SEQUENCE_CHARS
    assert decode(typed(report)) == b""


def test_long_kitty_press_is_forwarded_whole() -> None:
    report = "\x1b[97:65:97;2:1;97:769:128512:128513u"
    assert decode(typed(report)) == report.encode()


def _feed_until_output(sequence: str) -> tuple[int, bytes, bool]:
    """Feed *sequence* one record at a time; where output first appears."""
    decoder = docker_mod._ConsoleKeyDecoder()
    for count, event in enumerate(typed(sequence)[::2], start=1):  # key-downs
        out = decoder.feed(event)
        if out:
            return count, out, decoder.pending
    return 0, b"", decoder.pending


@pytest.mark.parametrize(
    ("sequence", "limit"),
    [
        # A parameter-only CSI that is not a release (no ``:3`` event type).
        ("\x1b[" + "1;" * 100, docker_mod._MAX_CSI_PARAMETER_SEQUENCE_CHARS),
        # Anything else keeps the short bound.
        ("\x1b[?" + "1" * 60, docker_mod._MAX_ESCAPE_SEQUENCE_CHARS),
    ],
)
def test_held_sequence_is_forwarded_exactly_at_its_bound(sequence: str, limit: int) -> None:
    count, out, pending = _feed_until_output(sequence)
    assert count == limit  # nothing before, the whole prefix at the bound
    assert out == sequence[:limit].encode()
    assert not pending


def test_release_longer_than_the_parameter_bound_is_dropped() -> None:
    report = "\x1b[97;1:3;" + "97:" * 40 + "97u"
    assert len(report) > docker_mod._MAX_CSI_PARAMETER_SEQUENCE_CHARS
    assert docker_mod._KITTY_KEY_REPORT.fullmatch(report.encode())
    assert decode(typed(report), typed("x")) == b"x"


def test_press_longer_than_the_parameter_bound_is_forwarded_unchanged() -> None:
    report = "\x1b[97;1:1;" + "97:" * 40 + "97u"
    assert decode(typed(report)) == report.encode()


def test_dropped_release_whose_end_never_comes_stops_at_the_flush() -> None:
    decoder = docker_mod._ConsoleKeyDecoder()
    prefix = "\x1b[97;1:3;" + "97:" * 45
    assert b"".join(decoder.feed(event) for event in typed(prefix)) == b""
    assert decoder.pending and not decoder.holding_bare_escape
    assert decoder.flush() == b""  # the grace period ended it
    assert b"".join(decoder.feed(event) for event in typed("12")) == b"12"


def test_only_a_lone_escape_counts_as_the_bare_escape_key() -> None:
    decoder = docker_mod._ConsoleKeyDecoder()
    decoder.feed(key("\x1b", vk=0x1B))
    assert decoder.holding_bare_escape  # strict deadline
    decoder.feed(key("[", vk=0xDB))
    assert not decoder.holding_bare_escape  # ESC[ is a sequence under way
    decoder.flush()
    decoder.feed(key(0xD83D))
    assert decoder.pending and not decoder.holding_bare_escape  # half an emoji


@pytest.mark.parametrize("report", ["\x1b[97:65:97;2:2u", "\x1b[97;1;97u", "\x1b[97u"])
def test_kitty_press_and_repeat_reports_are_forwarded(report: str) -> None:
    assert decode(typed(report)) == report.encode()


def test_kernel32_prototypes_keep_handles_pointer_sized() -> None:
    names = [
        "GetStdHandle",
        "GetConsoleMode",
        "SetConsoleMode",
        "WaitForSingleObject",
        "GetNumberOfConsoleInputEvents",
        "ReadConsoleInputW",
    ]
    fake = types.SimpleNamespace(**{name: types.SimpleNamespace() for name in names})

    docker_mod._declare_kernel32_prototypes(fake)

    assert fake.GetStdHandle.restype is ctypes.c_void_p
    for name in names[1:]:
        assert fake.__dict__[name].argtypes[0] is ctypes.c_void_p, name
    assert fake.SetConsoleMode.argtypes[1] is ctypes.c_uint32
    assert fake.ReadConsoleInputW.argtypes[1] is ctypes.POINTER(docker_mod._INPUT_RECORD)


# --- key translation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("vk", "state", "expected"),
    [
        (0x26, ENHANCED, b"\x1b[A"),
        (0x28, ENHANCED, b"\x1b[B"),
        (0x27, ENHANCED, b"\x1b[C"),
        (0x25, ENHANCED, b"\x1b[D"),
        (0x24, ENHANCED, b"\x1b[H"),
        (0x23, ENHANCED, b"\x1b[F"),
        (0x21, ENHANCED, b"\x1b[5~"),
        (0x22, ENHANCED, b"\x1b[6~"),
        (0x2D, ENHANCED, b"\x1b[2~"),
        (0x2E, ENHANCED, b"\x1b[3~"),
        (0x70, 0, b"\x1bOP"),
        (0x73, 0, b"\x1bOS"),
        (0x74, 0, b"\x1b[15~"),
        (0x7B, 0, b"\x1b[24~"),
        (0x25, _D._LEFT_CTRL_PRESSED, b"\x1b[1;5D"),
        (0x27, _D._RIGHT_CTRL_PRESSED, b"\x1b[1;5C"),
        (0x26, _D._SHIFT_PRESSED, b"\x1b[1;2A"),
        (0x28, _D._LEFT_ALT_PRESSED | ENHANCED, b"\x1b[1;3B"),
        (0x24, _D._LEFT_CTRL_PRESSED | _D._SHIFT_PRESSED, b"\x1b[1;6H"),
        (0x2E, _D._LEFT_CTRL_PRESSED, b"\x1b[3;5~"),
        (0x70, _D._SHIFT_PRESSED, b"\x1b[1;2P"),
        (0x74, _D._LEFT_CTRL_PRESSED, b"\x1b[15;5~"),
    ],
)
def test_navigation_and_function_keys(vk: int, state: int, expected: bytes) -> None:
    assert decode(key(vk=vk, state=state)) == expected


def test_special_text_keys() -> None:
    assert decode(key("\x08", vk=_D._VK_BACK)) == b"\x7f"
    assert decode(key("\x7f", vk=_D._VK_BACK, state=_D._LEFT_CTRL_PRESSED)) == b"\x08"
    assert decode(key("\t", vk=_D._VK_TAB, state=_D._SHIFT_PRESSED)) == b"\x1b[Z"
    assert decode(key("\t", vk=_D._VK_TAB)) == b"\t"
    assert decode(key(" ", vk=_D._VK_SPACE, state=_D._LEFT_CTRL_PRESSED)) == b"\x00"
    assert decode(key("\x03", vk=0x43, state=_D._LEFT_CTRL_PRESSED)) == b"\x03"  # Ctrl+C
    assert decode(key("\r", vk=0x0D)) == b"\r"


def test_shift_enter_is_a_plain_enter_like_on_a_linux_terminal() -> None:
    # GNOME Terminal, Konsole and xterm send CR for Shift+Enter too; only
    # Alt+Enter (ESC CR) is a distinct key.
    assert decode(key("\r", vk=0x0D, state=_D._SHIFT_PRESSED)) == b"\r"


def test_key_releases_and_keys_without_text_send_nothing() -> None:
    assert decode(key("a", vk=0x41, down=False)) == b""
    assert decode(key(vk=0x10, state=_D._SHIFT_PRESSED)) == b""  # Shift alone
    assert decode(key(vk=0xDC)) == b""  # a dead key


def test_alt_numpad_character_arrives_with_the_alt_release() -> None:
    numpad = [key(vk=0x60 + digit, state=_D._LEFT_ALT_PRESSED) for digit in (2, 2, 4)]
    assert decode(numpad, key("à", vk=_D._VK_MENU, down=False)) == "à".encode()


def test_alt_numpad_with_numlock_off_sends_only_the_character() -> None:
    # NumLock off: the digits 0, 2, 2, 4 arrive as Insert, Down, Down, Left
    # without ENHANCED_KEY; they must not go out as Alt+navigation keys.
    alt = _D._LEFT_ALT_PRESSED
    digits = [key(vk=vk, state=alt) for vk in (0x2D, 0x28, 0x28, 0x25)]
    assert decode(digits, key("à", vk=_D._VK_MENU, down=False)) == "à".encode()


def test_alt_with_the_dedicated_navigation_keys_still_sends_them() -> None:
    alt = _D._LEFT_ALT_PRESSED | ENHANCED
    assert decode(key(vk=0x26, state=alt), key(vk=0x2D, state=alt)) == b"\x1b[1;3A\x1b[2;3~"
    # Without Alt the numpad navigation keys are ordinary navigation keys.
    assert decode(key(vk=0x26)) == b"\x1b[A"


def test_repeated_surrogate_halves_still_make_one_emoji() -> None:
    assert decode(key(0xD83D, repeat=2), key(0xDE00, repeat=2)) == "😀".encode()


def test_progress_moves_with_every_held_record_only() -> None:
    decoder = docker_mod._ConsoleKeyDecoder()
    decoder.feed(key("\x1b", vk=0x1B))
    assert decoder.progress == 1
    decoder.feed(key(vk=0x10, state=_D._SHIFT_PRESSED))  # Shift extends nothing
    assert decoder.progress == 1
    decoder.feed(key("[", vk=0xDB))
    assert decoder.progress == 2
    decoder.feed(key("\x1b", vk=0x1B))  # forwards ESC[, holds the new ESC
    assert decoder.progress == 3
    decoder.flush()
    decoder.feed(key(0xD83D))
    assert decoder.progress == 4


def test_repeat_count_repeats_the_key() -> None:
    assert decode(key("a", vk=0x41, repeat=3)) == b"aaa"
    assert decode(key(vk=0x26, repeat=2)) == b"\x1b[A\x1b[A"


# --- escape sequence assembly ------------------------------------------------


def test_lone_escape_waits_for_flush() -> None:
    decoder = docker_mod._ConsoleKeyDecoder()
    assert decoder.feed(key("\x1b", vk=0x1B)) == b""
    assert decoder.pending
    assert decoder.flush() == b"\x1b"
    assert not decoder.pending


def test_sequence_split_across_reads_is_assembled() -> None:
    decoder = docker_mod._ConsoleKeyDecoder()
    out = b"".join(decoder.feed(event) for event in typed("\x1b[97;1"))
    assert out == b""
    assert decoder.pending
    assert b"".join(decoder.feed(event) for event in typed(":3u")) == b""
    assert not decoder.pending


@pytest.mark.parametrize(
    "sequence",
    ["\x1b[?1;2c", "\x1b[201~", "\x1bOP", "\x1bd", "\x1b]"],
)
def test_terminal_sequences_pass_through(sequence: str) -> None:
    assert decode(typed(sequence)) == sequence.encode()


def test_double_escape_forwards_the_first_at_once() -> None:
    decoder = docker_mod._ConsoleKeyDecoder()
    assert decoder.feed(key("\x1b", vk=0x1B)) == b""
    assert decoder.feed(key("\x1b", vk=0x1B)) == b"\x1b"
    assert decoder.flush() == b"\x1b"


def test_alt_key_after_held_escape_flushes_it_first() -> None:
    assert decode(key("\x1b", vk=0x1B), key("x", state=_D._LEFT_ALT_PRESSED)) == b"\x1b\x1bx"


# --- record reader -----------------------------------------------------------


class _RecordConsole:
    """kernel32 stand-in that serves queued INPUT_RECORDs."""

    def __init__(self, records: list[tuple[int, docker_mod._KeyEvent | None]], wait: int = 0):
        self.records = list(records)
        self.wait = wait
        self.read_sizes: list[int] = []

    def WaitForSingleObject(self, handle: Any, timeout_ms: int) -> int:
        del handle, timeout_ms
        return self.wait if self.records else docker_mod._WAIT_TIMEOUT

    def GetNumberOfConsoleInputEvents(self, handle: Any, count: Any) -> int:
        del handle
        count._obj.value = len(self.records)
        return 1

    def ReadConsoleInputW(self, handle: Any, buf: Any, size: int, read: Any) -> int:
        del handle
        self.read_sizes.append(size)
        taken, self.records = self.records[:size], self.records[size:]
        for slot, (event_type, event) in zip(buf, taken, strict=False):
            slot.EventType = event_type
            if event is not None:
                k = slot.Event.KeyEvent
                k.bKeyDown = int(event.key_down)
                k.wRepeatCount = event.repeat
                k.wVirtualKeyCode = event.vk
                k.wVirtualScanCode = event.scan
                k.uChar = event.char
                k.dwControlKeyState = event.state
        read._obj.value = len(taken)
        return 1


def test_input_record_has_the_windows_layout() -> None:
    assert ctypes.sizeof(docker_mod._INPUT_RECORD) == 20
    assert docker_mod._INPUT_RECORD.Event.offset == 4


def test_read_console_key_events_returns_key_records_only() -> None:
    a = key("a", vk=0x41, scan=0x1E, state=ENHANCED)
    focus = (0x0010, None)
    console = _RecordConsole([(docker_mod._KEY_EVENT, a), focus])

    assert docker_mod._read_console_key_events(console, 7, 10) == [a]
    assert docker_mod._read_console_key_events(console, 7, 10) == []  # timed out


def test_read_console_key_events_reads_in_bounded_batches() -> None:
    a = key("a", vk=0x41)
    console = _RecordConsole([(docker_mod._KEY_EVENT, a)] * 300)

    events = docker_mod._read_console_key_events(console, 7, 10)

    assert events is not None and len(events) == docker_mod._INPUT_RECORD_BATCH
    assert console.read_sizes == [docker_mod._INPUT_RECORD_BATCH]


def test_read_console_key_events_reports_a_failed_wait() -> None:
    console = _RecordConsole([(docker_mod._KEY_EVENT, key("a"))], wait=0xFFFFFFFF)
    assert docker_mod._read_console_key_events(console, 7, 10) is None

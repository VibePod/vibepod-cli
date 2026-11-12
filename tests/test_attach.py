"""Attach command tests."""

from __future__ import annotations

import io
import socket
import threading
import time
import types
from typing import Any

import pytest
import typer

from vibepod.commands import attach as attach_cmd
from vibepod.constants import CONTAINER_LABEL_MANAGED, EXIT_DOCKER_NOT_RUNNING
from vibepod.core import docker as docker_mod
from vibepod.core.docker import DockerClientError, DockerManager

_UNSET: dict[str, str] = {"__unset__": ""}


class _FakeContainer:
    def __init__(
        self,
        name: str,
        status: str = "running",
        labels: dict[str, str] = _UNSET,
    ) -> None:
        self.name = name
        self.status = status
        if labels is _UNSET:
            labels = {CONTAINER_LABEL_MANAGED: "true", "vibepod.agent": "claude"}
        self.labels = labels


def _managed_container(name: str = "vibepod-claude-abc", status: str = "running") -> _FakeContainer:
    return _FakeContainer(
        name,
        status=status,
        labels={CONTAINER_LABEL_MANAGED: "true", "vibepod.agent": "claude"},
    )


def test_attach_exits_when_docker_unavailable(monkeypatch) -> None:
    def _raise() -> None:
        raise DockerClientError("Docker is not available")

    class _UnavailableManager:
        def __init__(self) -> None:
            _raise()

    monkeypatch.setattr(attach_cmd, "DockerManager", _UnavailableManager)

    with pytest.raises(typer.Exit) as exc:
        attach_cmd.attach(container=None)
    assert exc.value.exit_code == EXIT_DOCKER_NOT_RUNNING


def test_attach_no_arg_errors_when_no_running_containers(monkeypatch) -> None:
    class _Manager:
        def list_managed(self):
            return []

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        attach_cmd.attach(container=None)
    assert exc.value.exit_code == 1


def test_attach_no_arg_errors_on_multiple_running(monkeypatch) -> None:
    class _Manager:
        def list_managed(self):
            return [
                _managed_container("vibepod-claude-1"),
                _managed_container("vibepod-claude-2"),
            ]

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        attach_cmd.attach(container=None)
    assert exc.value.exit_code == 1


def test_attach_no_arg_auto_picks_single_running(monkeypatch) -> None:
    attached: list[_FakeContainer] = []
    only = _managed_container("vibepod-claude-solo")

    class _Manager:
        def list_managed(self):
            return [only]

        def attach_interactive(self, container, logger=None):  # noqa: ARG002
            attached.append(container)

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    attach_cmd.attach(container=None)

    assert attached == [only]


def test_attach_ignores_non_running_when_auto_selecting(monkeypatch) -> None:
    running = _managed_container("vibepod-claude-running", status="running")
    exited = _managed_container("vibepod-claude-exited", status="exited")
    attached: list[_FakeContainer] = []

    class _Manager:
        def list_managed(self):
            return [exited, running]

        def attach_interactive(self, container, logger=None):  # noqa: ARG002
            attached.append(container)

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    attach_cmd.attach(container=None)

    assert attached == [running]


def test_attach_by_name_succeeds(monkeypatch) -> None:
    target = _managed_container("vibepod-claude-named")
    attached: list[_FakeContainer] = []

    class _Manager:
        def get_container(self, name_or_id: str):
            assert name_or_id == "vibepod-claude-named"
            return target

        def attach_interactive(self, container, logger=None):  # noqa: ARG002
            attached.append(container)

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    attach_cmd.attach(container="vibepod-claude-named")

    assert attached == [target]


def test_attach_by_name_rejects_unmanaged(monkeypatch) -> None:
    unmanaged = _FakeContainer("random-container", labels={})

    class _Manager:
        def get_container(self, name_or_id: str):  # noqa: ARG002
            return unmanaged

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        attach_cmd.attach(container="random-container")
    assert exc.value.exit_code == 1


def test_attach_by_name_rejects_stopped(monkeypatch) -> None:
    stopped = _managed_container("vibepod-claude-stopped", status="exited")

    class _Manager:
        def get_container(self, name_or_id: str):  # noqa: ARG002
            return stopped

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        attach_cmd.attach(container="vibepod-claude-stopped")
    assert exc.value.exit_code == 1


def test_attach_by_name_not_found(monkeypatch) -> None:
    class _Manager:
        def get_container(self, name_or_id: str):
            raise DockerClientError(f"Container '{name_or_id}' not found")

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        attach_cmd.attach(container="does-not-exist")
    assert exc.value.exit_code == 1


def test_attach_interactive_forwards_windows_console_input_without_termios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = bytearray()
    sent_event = threading.Event()
    sock_reader, sock_writer = socket.socketpair()

    class _FakeSocket:
        def fileno(self) -> int:
            return sock_reader.fileno()

        def recv(self, size: int) -> bytes:
            data = sock_reader.recv(size)
            if sent_event.is_set():
                return b""
            return data

        def sendall(self, data: bytes) -> None:
            sent.extend(data)
            sent_event.set()
            sock_writer.sendall(b"x")

    class _FakeSocketWrapper:
        def __init__(self) -> None:
            self._sock = _FakeSocket()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _FakeApi:
        def __init__(self) -> None:
            self.socket_wrapper = _FakeSocketWrapper()

        def attach_socket(self, container_id: str, params: dict[str, int]) -> _FakeSocketWrapper:
            assert container_id == "container-1"
            assert params["stdin"] == 1
            return self.socket_wrapper

        def resize(self, container_id: str, height: int, width: int) -> None:
            del container_id, height, width

    class _FakeClient:
        def __init__(self) -> None:
            self.api = _FakeApi()

    class _FakeStdin:
        def isatty(self) -> bool:
            return True

        @property
        def encoding(self) -> str:
            return "utf-8"

    pressed = iter(["h", KeyboardInterrupt])

    def getwch() -> str:
        value = next(pressed)
        if value is KeyboardInterrupt:
            raise KeyboardInterrupt
        return value

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    monkeypatch.setattr(docker_mod, "termios", None)
    monkeypatch.setattr(docker_mod, "tty", None)
    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace(getwch=getwch))
    monkeypatch.setattr(docker_mod.sys, "stdin", _FakeStdin())

    try:
        manager.attach_interactive(types.SimpleNamespace(id="container-1"))
    finally:
        sock_reader.close()
        sock_writer.close()

    assert sent == b"h"


class _NpipeLikeSocket:
    """Mimics docker-py's NpipeSocket: no fileno(), so select() rejects it.

    Only the overlapped ``recv_into`` is offered, not ``recv``: a synchronous
    ``ReadFile`` on the overlapped pipe handle may complete early.
    """

    def __init__(self, chunks: list[bytes], end: BaseException | None = None) -> None:
        self._chunks = list(chunks)
        self._end = end
        self.sent = bytearray()
        self.timeouts: list[float | None] = []

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)

    def recv_into(self, buf: bytearray) -> int:
        if self._chunks:
            chunk = self._chunks.pop(0)
            buf[: len(chunk)] = chunk
            return len(chunk)
        if self._end is not None:
            raise self._end
        return 0

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)


class _PyWinTypesError(Exception):
    """pywintypes.error is not an OSError subclass."""


def _attach_windows_npipe(
    monkeypatch: pytest.MonkeyPatch,
    sock: _NpipeLikeSocket,
) -> tuple[bytes, bytes]:
    class _FakeSocketWrapper:
        _sock = sock

        def close(self) -> None:
            pass

    class _FakeApi:
        def attach_socket(self, container_id: str, params: dict[str, int]) -> _FakeSocketWrapper:
            del container_id, params
            return _FakeSocketWrapper()

        def resize(self, container_id: str, height: int, width: int) -> None:
            del container_id, height, width

    class _FakeStdin:
        encoding = "utf-8"

        def isatty(self) -> bool:
            return True

    console_done = threading.Event()

    def getwch() -> str:
        # Block like a console waiting for a key until the session ends.
        console_done.wait(5)
        raise EOFError

    def _no_select(*args: object, **kwargs: object) -> None:
        raise AssertionError("select() must not be used on the Windows attach path")

    manager = object.__new__(DockerManager)
    manager.client = types.SimpleNamespace(api=_FakeApi())  # type: ignore[assignment]
    fake_stdout = types.SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(docker_mod, "termios", None)
    monkeypatch.setattr(docker_mod, "tty", None)
    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace(getwch=getwch))
    monkeypatch.setattr(docker_mod.select, "select", _no_select)
    monkeypatch.setattr(docker_mod.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(docker_mod.sys, "stdout", fake_stdout)

    try:
        result = manager.attach_interactive(types.SimpleNamespace(id="container-1"))
    finally:
        console_done.set()
    return result, fake_stdout.buffer.getvalue()


def test_attach_interactive_windows_npipe_socket_without_fileno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #230: Podman/Docker on Windows attach via a named pipe."""
    sock = _NpipeLikeSocket([b"hello ", b"from pi\r\n"])
    tail, written = _attach_windows_npipe(monkeypatch, sock)
    assert written == b"hello from pi\r\n"
    assert tail == b"hello from pi\r\n"
    # docker-py's 60 s client timeout would end an idle session.
    assert sock.timeouts == [None]


@pytest.mark.parametrize(
    "error",
    [
        _PyWinTypesError(64, "ReadFile", "The specified network name is no longer available."),
        ConnectionResetError("connection reset"),
        TimeoutError(),
    ],
)
def test_attach_interactive_windows_pipe_errors_are_not_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """Only the end-of-stream pipe codes mean EOF; anything else must reach run.py."""
    sock = _NpipeLikeSocket([b"partial"], end=error)
    with pytest.raises(type(error)):
        _attach_windows_npipe(monkeypatch, sock)


@pytest.mark.parametrize("code", [109, 232, 233, 995])
def test_attach_interactive_windows_pipe_end_codes_end_the_session(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Broken pipe, closing, disconnected and aborted reads are a normal end."""
    sock = _NpipeLikeSocket([b"bye\r\n"], end=_PyWinTypesError(code, "ReadFile", "ended"))
    tail, written = _attach_windows_npipe(monkeypatch, sock)
    assert written == tail == b"bye\r\n"


def test_attach_interactive_windows_broken_pipe_ends_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = _NpipeLikeSocket([b"bye\r\n"], end=_PyWinTypesError(109, "ReadFile", "broken pipe"))
    tail, written = _attach_windows_npipe(monkeypatch, sock)
    assert written == b"bye\r\n"
    assert tail == b"bye\r\n"


def test_attach_interactive_windows_enters_and_restores_console_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console must not echo input locally while a container TTY echoes it."""
    calls: list[str] = []

    def _enter() -> int:
        calls.append("enter")
        return 0x187

    monkeypatch.setattr(docker_mod, "_enter_console_raw_input", _enter)
    monkeypatch.setattr(
        docker_mod, "_leave_console_raw_input", lambda mode: calls.append(hex(mode))
    )

    _attach_windows_npipe(monkeypatch, _NpipeLikeSocket([b"hi\r\n"]))

    assert calls == ["enter", "0x187"]


def test_attach_interactive_windows_restores_console_input_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored: list[int] = []
    monkeypatch.setattr(docker_mod, "_enter_console_raw_input", lambda: 0x187)
    monkeypatch.setattr(docker_mod, "_leave_console_raw_input", restored.append)

    def _boom(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("attach output failed")

    monkeypatch.setattr(docker_mod, "_ThreadedReader", _boom)

    with pytest.raises(RuntimeError, match="attach output failed"):
        _attach_windows_npipe(monkeypatch, _NpipeLikeSocket([]))

    assert restored == [0x187]


class _FakeKernel32:
    """GetConsoleMode/SetConsoleMode over one mutable console input mode."""

    def __init__(self, mode: int, ok: bool = True) -> None:
        self.mode = mode
        self.ok = ok
        self.set_modes: list[int] = []

    def GetStdHandle(self, which: int) -> int:
        assert which == docker_mod._STD_INPUT_HANDLE
        return 7

    def GetConsoleMode(self, handle: int, mode_ptr: Any) -> int:
        # BOOL GetConsoleMode(HANDLE, LPDWORD): the mode comes back through a pointer.
        del handle
        mode_ptr.contents.value = self.mode
        return 1 if self.ok else 0

    def SetConsoleMode(self, handle: int, mode: Any) -> int:
        # BOOL SetConsoleMode(HANDLE, DWORD): the mode is passed by value. A
        # pointer would be read as mode bits on real Windows and fail.
        del handle
        assert not hasattr(mode, "contents"), "SetConsoleMode takes the mode by value"
        value = int(getattr(mode, "value", mode))
        self.set_modes.append(value)
        self.mode = value
        return 1 if self.ok else 0


#: The default console input mode: processed, line-buffered, with local echo.
_DEFAULT_CONSOLE_MODE = (
    docker_mod._ENABLE_PROCESSED_INPUT
    | docker_mod._ENABLE_LINE_INPUT
    | docker_mod._ENABLE_ECHO_INPUT
    | docker_mod._ENABLE_WINDOW_INPUT
    | docker_mod._ENABLE_MOUSE_INPUT
    | docker_mod._ENABLE_QUICK_EDIT_MODE
)


def test_raw_console_input_mode_disables_local_echo_and_line_input() -> None:
    mode = docker_mod._raw_console_input_mode(_DEFAULT_CONSOLE_MODE)

    for flag in (
        docker_mod._ENABLE_ECHO_INPUT,
        docker_mod._ENABLE_LINE_INPUT,
        docker_mod._ENABLE_PROCESSED_INPUT,
        docker_mod._ENABLE_QUICK_EDIT_MODE,
    ):
        assert not mode & flag


def test_raw_console_input_mode_keeps_console_key_reporting_legacy() -> None:
    """Regression: VT input mode made one ``a`` arrive as ``a`` + ``ESC[97;1:3u``.

    With per-key-event byte generation the CRT's mode flip around each
    ``msvcrt.getwch()`` split press and release into different encodings, and the
    kitty key-release report ended up as stray text in the agent prompt. Navigation
    keys must instead keep arriving as scan codes for ``_EXTENDED_KEY_SEQUENCES``.
    """
    with_vt_input = _DEFAULT_CONSOLE_MODE | docker_mod._ENABLE_VIRTUAL_TERMINAL_INPUT

    for start in (_DEFAULT_CONSOLE_MODE, with_vt_input):
        assert not docker_mod._raw_console_input_mode(start) & (
            docker_mod._ENABLE_VIRTUAL_TERMINAL_INPUT
        )

    # Anything outside the raw-input bits survives, to be restored verbatim.
    assert docker_mod._raw_console_input_mode(0x8000 | _DEFAULT_CONSOLE_MODE) == 0x8000


def test_enter_console_raw_input_applies_mode_and_leave_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32(_DEFAULT_CONSOLE_MODE)
    monkeypatch.setattr(docker_mod, "_kernel32", lambda: kernel32)

    old_mode = docker_mod._enter_console_raw_input()

    assert old_mode == _DEFAULT_CONSOLE_MODE
    assert kernel32.mode == docker_mod._raw_console_input_mode(_DEFAULT_CONSOLE_MODE)

    docker_mod._leave_console_raw_input(old_mode or 0)

    assert kernel32.set_modes[-1] == _DEFAULT_CONSOLE_MODE


def test_enter_console_raw_input_is_noop_without_console_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_mod, "_kernel32", lambda: None)

    assert docker_mod._enter_console_raw_input() is None
    docker_mod._leave_console_raw_input(0)


def test_enter_console_raw_input_is_noop_when_console_rejects_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32(_DEFAULT_CONSOLE_MODE, ok=False)
    monkeypatch.setattr(docker_mod, "_kernel32", lambda: kernel32)

    assert docker_mod._enter_console_raw_input() is None
    assert kernel32.set_modes == []


@pytest.mark.parametrize(
    ("prefix", "scan_code", "expected"),
    [
        ("\xe0", "H", b"\x1b[A"),  # Arrow Up
        ("\xe0", "P", b"\x1b[B"),  # Arrow Down
        ("\xe0", "K", b"\x1b[D"),  # Arrow Left
        ("\xe0", "M", b"\x1b[C"),  # Arrow Right
        ("\xe0", "G", b"\x1b[H"),  # Home
        ("\xe0", "O", b"\x1b[F"),  # End
        ("\xe0", "I", b"\x1b[5~"),  # Page Up (not ESC[I, the focus-in report)
        ("\xe0", "Q", b"\x1b[6~"),  # Page Down
        ("\xe0", "R", b"\x1b[2~"),  # Insert
        ("\xe0", "S", b"\x1b[3~"),  # Delete
        ("\x00", ";", b"\x1bOP"),  # F1
        ("\x00", "?", b"\x1b[15~"),  # F5
        ("\x00", "D", b"\x1b[21~"),  # F10
        ("\x00", chr(133), b"\x1b[23~"),  # F11
        ("\x00", chr(134), b"\x1b[24~"),  # F12
        ("\x00", chr(15), b"\x1b[Z"),  # Shift+Tab (Claude Code mode switch)
        ("\xe0", chr(115), b"\x1b[1;5D"),  # Ctrl+Left
        ("\xe0", chr(116), b"\x1b[1;5C"),  # Ctrl+Right
        ("\xe0", chr(147), b"\x1b[3;5~"),  # Ctrl+Delete
        ("\xe0", "\x13", b""),  # no terminal equivalent: dropped
    ],
)
def test_console_key_bytes_translates_extended_keys(
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    scan_code: str,
    expected: bytes,
) -> None:
    """Scan codes must not reach the container as (mojibake) text."""
    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace(getwch=lambda: scan_code))

    assert docker_mod._console_key_bytes(prefix) == expected


def test_console_key_bytes_encodes_text_as_utf8_and_backspace_as_del(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_mod.sys, "stdin", types.SimpleNamespace(encoding="cp1252"))

    assert docker_mod._console_key_bytes("\u00e4") == b"\xc3\xa4"
    assert docker_mod._console_key_bytes("\x08") == b"\x7f"
    assert docker_mod._console_key_bytes("\r") == b"\r"


class _FakeConsole:
    """msvcrt stand-in: one queued burst of key reads plus ``kbhit``."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)

    def getwch(self) -> str:
        if not self._keys:
            raise EOFError
        return self._keys.pop(0)

    def kbhit(self) -> bool:
        return bool(self._keys)


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        # Regression: a terminal in kitty keyboard mode reported the release of
        # ``a`` next to the key itself, and the agent inserted the report.
        ("\x1b[97;1:3u", b""),
        ("\x1b[57427;3:3u", b""),
        ("\x1b[97;1u", b"\x1b[97;1u"),  # key press stays
        ("\x1b[97;1:2u", b"\x1b[97;1:2u"),  # auto-repeat stays
        ("\x1b[97u", b"\x1b[97u"),
        ("\x1b[201~", b"\x1b[201~"),  # bracketed paste end
        ("\x1b[A", b"\x1b[A"),  # arrow key from a terminal in VT input mode
        ("\x1bOP", b"\x1bOP"),  # SS3 (F1)
        ("\x1bd", b"\x1bd"),  # Alt/Meta key
    ],
)
def test_console_escape_bytes_screens_kitty_key_releases(
    monkeypatch: pytest.MonkeyPatch,
    sequence: str,
    expected: bytes,
) -> None:
    console = _FakeConsole(list(sequence[1:]))
    monkeypatch.setattr(docker_mod, "msvcrt", console)

    assert docker_mod._console_escape_bytes() == expected


class _TricklingConsole(_FakeConsole):
    """Console that only hands out its queued keys after a few polls."""

    def __init__(self, keys: list[str], reveal_after_polls: int) -> None:
        super().__init__(keys)
        self._polls = 0
        self._reveal_after = reveal_after_polls

    def kbhit(self) -> bool:
        self._polls += 1
        return self._polls > self._reveal_after and bool(self._keys)


def test_console_escape_bytes_catches_a_sequence_split_across_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pseudo-console may release the report bytes one by one, not as a burst."""
    console = _TricklingConsole(list("[97;1:3u"), reveal_after_polls=3)
    monkeypatch.setattr(docker_mod, "msvcrt", console)

    assert docker_mod._console_escape_bytes() == b""


def test_console_escape_bytes_gives_up_on_a_real_escape_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grace period must end, or vim escapes and TUI interrupts would stall."""
    console = _TricklingConsole([], reveal_after_polls=10**6)
    monkeypatch.setattr(docker_mod, "msvcrt", console)

    started = time.monotonic()
    assert docker_mod._console_escape_bytes() == b"\x1b"
    elapsed = time.monotonic() - started

    assert docker_mod._ESCAPE_SEQUENCE_GRACE_SECONDS <= elapsed < 2.0


def test_console_escape_bytes_forwards_lone_esc_without_queued_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lone ESC (vim escape, TUI interrupt) must never wait for a next key."""
    monkeypatch.setattr(docker_mod, "msvcrt", _FakeConsole([]))

    assert docker_mod._console_escape_bytes() == b"\x1b"


def test_console_key_bytes_without_kbhit_forwards_esc_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consoles without a usable ``kbhit`` keep the previous byte-per-read flow."""
    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace(getwch=lambda: ""))

    assert docker_mod._console_key_bytes("\x1b") == b"\x1b"


def test_forward_windows_console_input_drops_key_release_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One typed ``a`` arriving as ``a`` + ``ESC[97;1:3u`` forwards just ``a``."""
    monkeypatch.setattr(docker_mod, "msvcrt", _FakeConsole(list("a\x1b[97;1:3u\x1b[A")))
    sent = bytearray()

    class _Sock:
        def sendall(self, data: bytes) -> None:
            sent.extend(data)

    docker_mod._forward_windows_console_input(_Sock(), None, threading.Event())

    assert sent == b"a\x1b[A"


def test_forward_windows_console_input_forwards_typed_escape_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console in VT input mode delivers arrow keys as plain escape bytes."""
    keys = iter(["\x1b", "[", "A", "a", "\x08"])

    def getwch() -> str:
        try:
            return next(keys)
        except StopIteration:
            raise EOFError from None

    sent = bytearray()

    class _Sock:
        def sendall(self, data: bytes) -> None:
            sent.extend(data)

    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace(getwch=getwch))
    docker_mod._forward_windows_console_input(_Sock(), None, threading.Event())

    assert sent == b"\x1b[Aa\x7f"


def test_forward_windows_console_input_drops_key_after_session_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key read after the session ended must not reach the closed socket."""
    stop = threading.Event()

    def getwch() -> str:
        stop.set()  # the session ends while getwch() waits for a key
        return "a"

    class _ClosedSock:
        def sendall(self, data: bytes) -> None:
            raise AssertionError("sent to a closed socket")

    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace(getwch=getwch))
    docker_mod._forward_windows_console_input(_ClosedSock(), None, stop)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Can not reuse socket after connection was closed."),
        _PyWinTypesError(232, "WriteFile", "The pipe is being closed."),
    ],
)
def test_forward_windows_console_input_stops_quietly_on_send_failure(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """A closed NpipeSocket raises non-OSError errors; no thread traceback."""
    keys = iter(["a", "b"])

    def getwch() -> str:
        return next(keys)

    class _Sock:
        calls = 0

        def sendall(self, data: bytes) -> None:
            _Sock.calls += 1
            raise error

    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace(getwch=getwch))
    docker_mod._forward_windows_console_input(_Sock(), None, threading.Event())
    assert _Sock.calls == 1


def test_attach_interactive_non_tty_outlives_the_socket_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docker-py sets a client timeout on the attach socket; an agent that is
    quiet for longer must not end a session without a terminal (CI, < /dev/null)."""
    sock_reader, sock_writer = socket.socketpair()
    sock_reader.settimeout(0.2)

    class _FakeSocketWrapper:
        _sock = sock_reader

        def close(self) -> None:
            pass

    class _FakeApi:
        def attach_socket(self, container_id: str, params: dict[str, int]) -> _FakeSocketWrapper:
            del container_id, params
            return _FakeSocketWrapper()

        def resize(self, container_id: str, height: int, width: int) -> None:
            del container_id, height, width

    class _FakeStdin:
        def isatty(self) -> bool:
            return False

    manager = object.__new__(DockerManager)
    manager.client = types.SimpleNamespace(api=_FakeApi())  # type: ignore[assignment]
    fake_stdout = types.SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(docker_mod.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(docker_mod.sys, "stdout", fake_stdout)
    monkeypatch.setattr(docker_mod, "msvcrt", None)

    def _feed() -> None:
        time.sleep(0.6)  # three socket timeouts of silence
        sock_writer.sendall(b"still here\r\n")
        sock_writer.close()

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        tail = manager.attach_interactive(types.SimpleNamespace(id="container-1"))
    finally:
        feeder.join(timeout=5)
        sock_reader.close()

    assert tail == b"still here\r\n"
    assert fake_stdout.buffer.getvalue() == b"still here\r\n"


def test_is_selectable_distinguishes_named_pipes_from_sockets() -> None:
    a, b = socket.socketpair()
    try:
        assert docker_mod._is_selectable(a)
    finally:
        a.close()
        b.close()
    assert not docker_mod._is_selectable(_NpipeLikeSocket([]))


def _attach_and_capture(
    monkeypatch: pytest.MonkeyPatch,
    payload_chunks: list[bytes],
) -> tuple[bytes | None, bytes]:
    sock_reader, sock_writer = socket.socketpair()

    class _FakeSocketWrapper:
        _sock = sock_reader

        def close(self) -> None:
            pass

    class _FakeApi:
        def attach_socket(self, container_id: str, params: dict[str, int]) -> _FakeSocketWrapper:
            del container_id, params
            return _FakeSocketWrapper()

        def resize(self, container_id: str, height: int, width: int) -> None:
            del container_id, height, width

    manager = object.__new__(DockerManager)
    manager.client = types.SimpleNamespace(api=_FakeApi())  # type: ignore[assignment]

    class _FakeStdin:
        def isatty(self) -> bool:
            return False

    fake_stdout = types.SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(docker_mod.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(docker_mod.sys, "stdout", fake_stdout)
    monkeypatch.setattr(docker_mod, "msvcrt", None)

    def _feed() -> None:
        for chunk in payload_chunks:
            sock_writer.sendall(chunk)
        sock_writer.close()

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        result = manager.attach_interactive(types.SimpleNamespace(id="container-1"))
    finally:
        feeder.join(timeout=5)
        sock_reader.close()
    return result, fake_stdout.buffer.getvalue()


def test_attach_interactive_returns_output_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [b"transcript...\r\n", b"claude --resume abc-123\r\n"]
    tail, written = _attach_and_capture(monkeypatch, payload)
    assert tail == b"transcript...\r\nclaude --resume abc-123\r\n"
    assert written == b"".join(payload)


def test_attach_interactive_bounds_output_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = b"claude --resume abc-123\r\n"
    payload = [b"x" * 8192 for _ in range(20)] + [marker]
    tail, written = _attach_and_capture(monkeypatch, payload)
    assert tail is not None
    assert tail.endswith(marker)
    assert len(tail) <= docker_mod.ATTACH_TAIL_LIMIT
    assert len(written) == 20 * 8192 + len(marker)


def test_attach_prints_resume_hint_after_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = _managed_container("vibepod-claude-hint")

    class _Manager:
        def get_container(self, name_or_id: str):
            return target

        def attach_interactive(self, container, logger=None):  # noqa: ARG002
            return b"Resume this session with:\r\nclaude --resume abc-123\r\n"

    monkeypatch.setattr(attach_cmd, "DockerManager", lambda: _Manager())

    attach_cmd.attach(container="vibepod-claude-hint")

    assert "vp run claude -- --resume abc-123" in capsys.readouterr().out

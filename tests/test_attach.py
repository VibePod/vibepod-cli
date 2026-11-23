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

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    monkeypatch.setattr(docker_mod, "termios", None)
    monkeypatch.setattr(docker_mod, "tty", None)
    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace())
    _fake_console_keys(monkeypatch, [[_key("h", vk=0x48)]])
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


@pytest.fixture(autouse=True)
def _fake_npipe_socket_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker-py defines NpipeSocket only on Windows; stand in the fake for it."""
    monkeypatch.setattr(docker_mod, "_NpipeSocket", _NpipeLikeSocket)


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

    def read(timeout_ms: int) -> list[docker_mod._KeyEvent] | None:
        # An idle console until the session ends.
        return None if console_done.wait(timeout_ms / 1000) else []

    def _no_select(*args: object, **kwargs: object) -> None:
        raise AssertionError("select() must not be used on the Windows attach path")

    manager = object.__new__(DockerManager)
    manager.client = types.SimpleNamespace(api=_FakeApi())  # type: ignore[assignment]
    fake_stdout = types.SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(docker_mod, "termios", None)
    monkeypatch.setattr(docker_mod, "tty", None)
    monkeypatch.setattr(docker_mod, "msvcrt", types.SimpleNamespace())
    monkeypatch.setattr(docker_mod, "_console_key_reader", lambda: read)
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
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    """Only the end-of-stream pipe codes mean EOF; anything else must reach run.py."""
    sock = _NpipeLikeSocket([b"partial"], end=error)
    with pytest.raises(type(error)):
        _attach_windows_npipe(monkeypatch, sock)


@pytest.mark.parametrize("code", [109, 232, 233, 995])
def test_attach_interactive_windows_pipe_end_codes_end_the_session(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
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
        docker_mod,
        "_leave_console_raw_input",
        lambda mode: calls.append(hex(mode)),
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
        assert which == docker_mod._STD_INPUT_HANDLE & 0xFFFFFFFF  # a DWORD
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
    """VT input mode would turn keys into escape text without virtual-key codes.

    ``_ConsoleKeyDecoder`` works from the virtual-key code and modifier state of
    legacy key records; with the old ``getwch()`` reader VT mode also made one
    ``a`` arrive as ``a`` + a kitty ``ESC[97;1:3u`` release report.
    """
    with_vt_input = _DEFAULT_CONSOLE_MODE | docker_mod._ENABLE_VIRTUAL_TERMINAL_INPUT

    for start in (_DEFAULT_CONSOLE_MODE, with_vt_input):
        assert not docker_mod._raw_console_input_mode(start) & (
            docker_mod._ENABLE_VIRTUAL_TERMINAL_INPUT
        )

    # Anything outside the raw-input bits survives, to be restored verbatim.
    assert docker_mod._raw_console_input_mode(0x8000 | _DEFAULT_CONSOLE_MODE) == (
        0x8000 | docker_mod._ENABLE_EXTENDED_FLAGS
    )


def test_raw_console_input_mode_sets_extended_flags_so_quick_edit_turns_off() -> None:
    """SetConsoleMode ignores the quick-edit bit unless ENABLE_EXTENDED_FLAGS is set;
    with quick-edit left on, a mouse click freezes the agent's output."""
    mode = docker_mod._raw_console_input_mode(_DEFAULT_CONSOLE_MODE)
    assert mode & docker_mod._ENABLE_EXTENDED_FLAGS
    assert not mode & docker_mod._ENABLE_QUICK_EDIT_MODE


def test_enter_console_raw_input_applies_mode_and_leave_restores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _FakeKernel32(_DEFAULT_CONSOLE_MODE)
    monkeypatch.setattr(docker_mod, "_kernel32", lambda: kernel32)

    old_mode = docker_mod._enter_console_raw_input()

    assert old_mode == _DEFAULT_CONSOLE_MODE
    assert kernel32.mode == docker_mod._raw_console_input_mode(_DEFAULT_CONSOLE_MODE)

    docker_mod._leave_console_raw_input(old_mode or 0)

    # Restored with ENABLE_EXTENDED_FLAGS, or the saved quick-edit bit is ignored.
    assert kernel32.set_modes[-1] == _DEFAULT_CONSOLE_MODE | docker_mod._ENABLE_EXTENDED_FLAGS


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


def _key(char: str | int = 0, vk: int = 0, state: int = 0, down: bool = True) -> Any:
    code = ord(char) if isinstance(char, str) else char
    return docker_mod._KeyEvent(down, 1, vk, 0, code, state)


def _typed(text: str) -> list[Any]:
    return [_key(ch) for ch in text]


def _fake_console_keys(
    monkeypatch: pytest.MonkeyPatch,
    batches: list[list[Any]],
    on_idle: Any = None,
) -> list[int]:
    """Serve *batches* as successive console reads, then end the console.

    Returns the read timeouts the forwarder asked for. *on_idle*, if given, is
    called once all batches are served and decides what the reads return then
    (``[]`` keeps the console idle, ``None`` ends it).
    """
    queue = list(batches)
    timeouts: list[int] = []

    def read(timeout_ms: int) -> list[Any] | None:
        timeouts.append(timeout_ms)
        if queue:
            return queue.pop(0)
        if on_idle is not None:
            return on_idle(timeout_ms)
        return None

    monkeypatch.setattr(docker_mod, "_console_key_reader", lambda: read)
    return timeouts


class _RecordingSock:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.calls = 0

    def sendall(self, data: bytes) -> None:
        self.calls += 1
        self.sent.extend(data)


def test_forward_windows_console_input_translates_key_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alt = docker_mod._LEFT_ALT_PRESSED
    _fake_console_keys(
        monkeypatch,
        [
            [_key("\u00e0", vk=0x41), _key("x", vk=0x58)],  # à then x (#242)
            [_key(vk=0x26)],  # Arrow Up
            [_key(0xD83D), _key(0xDE00)],  # an emoji as a surrogate pair
            [_key("\r", vk=0x0D, state=alt)],  # Alt+Enter
            [_key("\x08", vk=docker_mod._VK_BACK)],
        ],
    )
    sock = _RecordingSock()

    docker_mod._forward_windows_console_input(sock, None, threading.Event())

    assert bytes(sock.sent) == "àx\x1b[A😀\x1b\r\x7f".encode()


def test_forward_windows_console_input_drops_key_release_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One typed ``a`` arriving as ``a`` + ``ESC[97;1:3u`` forwards just ``a``."""
    _fake_console_keys(monkeypatch, [_typed("a\x1b[97;1:3u"), [_key(vk=0x26)]])
    sock = _RecordingSock()

    docker_mod._forward_windows_console_input(sock, None, threading.Event())

    assert bytes(sock.sent) == b"a\x1b[A"


def test_forward_windows_console_input_assembles_a_sequence_across_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_console_keys(monkeypatch, [_typed("\x1b[97;1"), _typed(":3u"), _typed("b")])
    sock = _RecordingSock()

    docker_mod._forward_windows_console_input(sock, None, threading.Event())

    assert bytes(sock.sent) == b"b"


def test_forward_windows_console_input_flushes_a_lone_escape_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    sock = _RecordingSock()

    def idle(timeout_ms: int) -> list[Any]:
        if sock.sent:
            stop.set()
        time.sleep(timeout_ms / 1000)
        return []

    timeouts = _fake_console_keys(monkeypatch, [[_key("\x1b", vk=0x1B)]], on_idle=idle)
    started = time.monotonic()

    docker_mod._forward_windows_console_input(sock, None, stop)

    assert bytes(sock.sent) == b"\x1b"
    assert time.monotonic() - started >= docker_mod._ESCAPE_SEQUENCE_GRACE_SECONDS
    # While the ESC is held, reads never wait past its flush deadline.
    assert max(timeouts) <= docker_mod._KEY_WAIT_POLL_SECONDS * 1000


def test_forward_windows_console_input_flushes_escape_despite_other_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held Shift auto-repeating must not keep a lone ESC from going out."""
    stop = threading.Event()
    sock = _RecordingSock()
    shift = _key(vk=0x10, state=docker_mod._SHIFT_PRESSED)

    def repeating_shift(timeout_ms: int) -> list[Any]:
        if sock.sent:
            stop.set()
        time.sleep(0.005)
        return [shift]

    _fake_console_keys(monkeypatch, [[_key("\x1b", vk=0x1B)]], on_idle=repeating_shift)

    docker_mod._forward_windows_console_input(sock, None, stop)

    assert bytes(sock.sent) == b"\x1b"


def test_forward_windows_console_input_restarts_grace_for_a_new_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ESC arriving while another is held gets its own full grace period.

    The new ESC forwards the held one at once and is held itself; it must not
    inherit the first one's deadline and go out only a few ms after it came.
    """
    stop = threading.Event()
    sock = _RecordingSock()
    grace = docker_mod._ESCAPE_SEQUENCE_GRACE_SECONDS
    esc = _key("\x1b", vk=0x1B)
    first_at: list[float] = []
    second_at: list[float] = []
    flushed_at: list[float] = []

    def read(timeout_ms: int) -> list[Any] | None:
        now = time.monotonic()
        if not first_at:
            first_at.append(now)
            return [esc]
        if not second_at and now - first_at[0] >= grace * 0.8:
            second_at.append(now)
            return [esc]
        if len(sock.sent) == 2:
            flushed_at.append(now)
            stop.set()
            return []
        time.sleep(min(timeout_ms, 2) / 1000)
        return []

    monkeypatch.setattr(docker_mod, "_console_key_reader", lambda: read)
    docker_mod._forward_windows_console_input(sock, None, stop)

    assert bytes(sock.sent) == b"\x1b\x1b"
    assert flushed_at[0] - second_at[0] >= grace * 0.9


def test_forward_windows_console_input_logs_what_it_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_console_keys(monkeypatch, [[_key(vk=0x10)], _typed("hi")])
    logged: list[bytes] = []
    logger = types.SimpleNamespace(log_input=logged.append)
    sock = _RecordingSock()

    docker_mod._forward_windows_console_input(sock, logger, threading.Event())

    assert logged == [b"hi"]
    assert sock.calls == 1  # Shift alone sends nothing


def test_forward_windows_console_input_drops_key_after_session_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key read after the session ended must not reach the closed socket."""
    stop = threading.Event()

    def read(timeout_ms: int) -> list[Any]:
        stop.set()  # the session ends while the read waits for a key
        return [_key("a", vk=0x41)]

    class _ClosedSock:
        def sendall(self, data: bytes) -> None:
            raise AssertionError("sent to a closed socket")

    monkeypatch.setattr(docker_mod, "_console_key_reader", lambda: read)
    docker_mod._forward_windows_console_input(_ClosedSock(), None, stop)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Can not reuse socket after connection was closed."),
        _PyWinTypesError(232, "WriteFile", "The pipe is being closed."),
    ],
)
def test_forward_windows_console_input_stops_quietly_on_send_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    """A closed NpipeSocket raises non-OSError errors; no thread traceback."""
    _fake_console_keys(monkeypatch, [_typed("a"), _typed("b")])

    class _Sock:
        calls = 0

        def sendall(self, data: bytes) -> None:
            _Sock.calls += 1
            raise error

    docker_mod._forward_windows_console_input(_Sock(), None, threading.Event())
    assert _Sock.calls == 1


def test_forward_windows_console_input_ends_soon_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle reader thread must notice the session end within one short wait,
    or it would take the next keystroke away from the restored prompt."""
    stop = threading.Event()

    def idle(timeout_ms: int) -> list[Any]:
        stop.set()  # the session ends while the thread is idle
        time.sleep(timeout_ms / 1000)
        return []

    timeouts = _fake_console_keys(monkeypatch, [], on_idle=idle)
    thread = threading.Thread(
        target=docker_mod._forward_windows_console_input,
        args=(object(), None, stop),
    )
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert timeouts == [int(docker_mod._KEY_WAIT_POLL_SECONDS * 1000)]


def test_forward_windows_console_input_is_a_noop_without_a_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_mod, "_console_key_reader", lambda: None)
    docker_mod._forward_windows_console_input(object(), None, threading.Event())


def test_threaded_reader_buffers_a_bounded_amount_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow consumer must apply back-pressure, not grow the queue without end."""
    monkeypatch.setattr(docker_mod, "_READER_QUEUE_CHUNKS", 4)
    reads = 0

    class _Firehose(_NpipeLikeSocket):
        def recv_into(self, buf: bytearray) -> int:
            nonlocal reads
            reads += 1
            buf[:1] = b"x"
            return 1

    reader = docker_mod._ThreadedReader(_Firehose([]), 16)
    time.sleep(0.3)
    # Four queued chunks plus the one the reader holds while waiting for room.
    assert reads <= 5
    assert reader.get(0.1) == b"x"
    reader.close()
    reader._thread.join(timeout=2)
    assert not reader._thread.is_alive()


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

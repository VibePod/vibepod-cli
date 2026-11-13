"""Thin Docker SDK wrapper used by CLI commands."""

from __future__ import annotations

import os
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from vibepod.constants import CONTAINER_LABEL_MANAGED, agent_ids

docker: Any | None
APIError: type[Exception]
DockerException: type[Exception]
NotFound: type[Exception]
termios: Any | None
tty: Any | None
msvcrt: Any | None

try:
    import termios as _termios
    import tty as _tty
except ImportError:  # pragma: no cover - exercised on Windows
    termios = None
    tty = None
else:
    termios = _termios
    tty = _tty

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised off Windows
    msvcrt = None
else:
    msvcrt = _msvcrt

ctypes: Any | None
try:
    import ctypes as _ctypes
except ImportError:  # pragma: no cover - exercised on ctypes-less runtimes
    ctypes = None
else:
    ctypes = _ctypes

try:
    import docker as _docker
    from docker.errors import APIError as _APIError
    from docker.errors import DockerException as _DockerException
    from docker.errors import NotFound as _NotFound
except ImportError:  # pragma: no cover - handled at runtime
    docker = None
    APIError = Exception
    DockerException = Exception
    NotFound = Exception
else:
    docker = _docker
    APIError = _APIError
    DockerException = _DockerException
    NotFound = _NotFound


class DockerClientError(RuntimeError):
    """Raised for Docker availability or lifecycle errors."""


_PODMAN_HINT = (
    "If you use Podman, make sure the machine is running (`podman machine start`) "
    "or point DOCKER_HOST at the Podman socket."
)

#: Image namespace owned by vibepod; the only one auto_clean ever sweeps.
IMAGE_NAMESPACE = "vibepod"

#: ACP mode: seconds without any adapter output before attach_stdio explains
#: the silence on stderr.
_ACP_FIRST_OUTPUT_WARN_SECONDS = 30

# How much trailing container output attach_interactive keeps for post-exit
# inspection (resume hints appear in the last few lines of a session).
ATTACH_TAIL_LIMIT = 64 * 1024
PROXY_POLICY_SCHEMA_LABEL = "io.vibepod.proxy.policy-schema"
# The per-source policy schema this CLI speaks; a proxy image must carry the
# matching PROXY_POLICY_SCHEMA_LABEL value.
PROXY_POLICY_SCHEMA = "2"
PROXY_VERSION_LABEL = "io.vibepod.proxy.version"
# The oldest proxy release this CLI works with (streamed gRPC/Connect bodies);
# a proxy image must carry a PROXY_VERSION_LABEL value at least this high.
PROXY_MIN_VERSION = "0.6.0"
_VERSION_PREFIX = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _parse_version(value: object) -> tuple[int, int, int] | None:
    """Return the leading ``major.minor.patch`` of *value*, or None if absent."""
    if not isinstance(value, str):
        return None
    match = _VERSION_PREFIX.match(value.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


_SELINUX_ENFORCE_PATH = "/sys/fs/selinux/enforce"
# Engines refuse to relabel these (and the home dir or its ancestors): doing so
# would rewrite the label the host itself depends on.
_SELINUX_PROTECTED_DIRS = frozenset(
    "/ /bin /boot /dev /etc /home /lib /lib64 /media /mnt /opt /proc /root /run /sbin /srv "
    "/sys /tmp /usr /var".split(),
)
# System trees whose descendants are host-owned too (say `/etc/ssl/certs` as a
# proxy CA dir): relabeling anything under them could break host services.
# `/home`, `/tmp`, `/opt`, `/srv`, `/var`, `/mnt` and friends hold user projects.
_SELINUX_PROTECTED_TREES = tuple(
    Path(root) for root in "/bin /boot /dev /etc /lib /lib64 /proc /sbin /sys /usr".split()
)
_SELINUX_HINT = (
    "SELinux is enforcing: containers may be denied access to VibePod's bind mounts "
    "(the workspace, ~/.config/vibepod). To let VibePod relabel them, set "
    "`selinux_relabel: true` in config.yaml or VP_SELINUX_RELABEL=true. Relabeling "
    "permanently changes the SELinux label of those host directories."
)


class _SelinuxNotices:
    """What this process has already told the user about SELinux relabeling."""

    def __init__(self) -> None:
        self.hint_shown = False
        self.relabeled: list[str] = []
        self.reported: set[str] = set()


_selinux_notices = _SelinuxNotices()


def _selinux_enforcing() -> bool:
    try:
        return Path(_SELINUX_ENFORCE_PATH).read_text().strip() == "1"
    except OSError:
        return False


def _on_read_only_fs(path: Path) -> bool:
    try:
        flags = os.statvfs(path).f_flag
    except (OSError, AttributeError):  # missing path, or no statvfs (Windows)
        return False
    return bool(flags & getattr(os, "ST_RDONLY", 1))


def selinux_relabel_enabled() -> bool:
    """Whether the user opted into relabeling (`selinux_relabel` / VP_SELINUX_RELABEL)."""
    from vibepod.core.config import get_config

    return get_config().get("selinux_relabel") is True


def bind_mode(host_path: str | Path, mode: str = "rw") -> str:
    """Return `mode`, plus SELinux's `z` flag when the user opted into relabeling.

    On an enforcing SELinux host (Fedora and friends) a bind mount keeps its
    host label -- `config_home_t` for the config dir, `user_home_t` for a
    workspace -- and `container_t` may write neither: the proxy dies on
    startup with `Permission denied: /data/mitmproxy/mitmproxy-ca.pem`, and
    agents see an unwritable workspace. `z` relabels the source to the shared
    `container_file_t`, which stays readable across containers (the agent
    reads the proxy's CA dir, datasette shares the proxy db dir) where the
    private `Z` would not. Non-Linux engines have no such file and stay
    unflagged -- Podman's macOS VM cannot relabel a virtiofs share anyway.

    The relabel is permanent and can break host services that read the same
    files (a workspace under `/var/www`, say), so it only happens when
    `selinux_relabel` is enabled; `report_selinux_relabel` tells the user
    either way. Only call this for mounts VibePod owns: relabeling a user's own
    volume (say `~/.ssh`) would break the host services that read it. Named
    volumes, an explicit `z`/`Z`, the home or system dirs, anything under
    system trees such as `/etc` or `/usr`, and paths on a read-only filesystem
    are left as they are.
    """
    if "/" not in str(host_path) or {"z", "Z"} & set(mode.split(",")):
        return mode
    if not _selinux_enforcing() or not selinux_relabel_enabled():
        return mode
    # Check the path as given too: on macOS /home, /tmp and /etc are symlinks
    # into /System/Volumes/Data or /private, so only the unresolved form matches.
    given = Path(host_path).absolute()
    path = given.resolve()
    home = Path.home().resolve()
    if {str(given), str(path)} & _SELINUX_PROTECTED_DIRS or path == home or path in home.parents:
        return mode
    for candidate in (given, path):
        if any(root in candidate.parents for root in _SELINUX_PROTECTED_TREES):
            return mode
    if _on_read_only_fs(path):
        # The engine cannot write the label there (a VM's read-only host share, say) and
        # refuses to start the container: mount it as it is.
        return mode
    if str(given) not in _selinux_notices.relabeled:
        _selinux_notices.relabeled.append(str(given))
    return f"{mode},z"


def report_selinux_relabel() -> None:
    """Tell the user what SELinux relabeling does for the containers being started.

    Call it once the binds of a VibePod container are built. On an enforcing
    host with relabeling off it prints a hint on how to opt in (once per
    process); with relabeling on it lists the host paths `bind_mode` relabeled
    that were not reported yet. Elsewhere it prints nothing. Both go to stderr.
    """
    if not _selinux_enforcing():
        return
    # stderr: stdout may carry JSON (`vp skills list --json`) or JSON-RPC (ACP).
    from rich.console import Console
    from rich.markup import escape

    notices = _selinux_notices
    if not selinux_relabel_enabled():
        if not notices.hint_shown:
            notices.hint_shown = True
            Console(stderr=True).print(f"[yellow]{_SELINUX_HINT}[/yellow]", highlight=False)
        return
    new = [path for path in notices.relabeled if path not in notices.reported]
    if new:
        notices.reported.update(new)
        message = escape(f"Relabeling for SELinux (container_file_t): {', '.join(new)}")
        Console(stderr=True).print(f"[cyan]{message}[/cyan]", highlight=False)


def _run_podman(podman: str, args: list[str]) -> str | None:
    """Run a Podman subcommand, returning its trimmed stdout on success."""
    try:
        result = subprocess.run([podman, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _podman_machine_sockets(podman: str) -> list[str]:
    """Socket paths of every configured Podman machine, not just the default one."""
    listing = _run_podman(podman, ["machine", "list", "--format", "{{.Name}}"])
    if listing is None:
        return []
    # `machine list` suffixes the default machine's name with "*".
    names = [stripped.rstrip("*") for line in listing.splitlines() if (stripped := line.strip())]
    if not names:
        return []
    inspected = _run_podman(
        podman,
        ["machine", "inspect", *names, "--format", "{{.ConnectionInfo.PodmanSocket.Path}}"],
    )
    if inspected is None:
        return []
    return [stripped for line in inspected.splitlines() if (stripped := line.strip())]


def _discover_podman_socket() -> str | None:
    """Locate a Podman socket to use when the default Docker socket is unavailable.

    Podman machines on macOS expose the Docker-compatible API on a socket whose
    path depends on $TMPDIR at machine-start time, so it cannot be hardcoded.
    Ask Podman itself, then fall back to the rootless default on Linux.
    """
    if os.environ.get("DOCKER_HOST"):
        return None

    candidates: list[str] = []

    podman = shutil.which("podman")
    if podman is not None:
        machine_sockets = _podman_machine_sockets(podman)
        candidates.extend(machine_sockets)
        if not machine_sockets:
            # With a machine in play `podman info` reports the socket path as seen
            # from inside the VM, which does not exist on the host — only trust it
            # for native installs, where no machine is configured.
            remote_socket = _run_podman(podman, ["info", "--format", "{{.Host.RemoteSocket.Path}}"])
            if remote_socket is not None:
                candidates.append(remote_socket)

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        candidates.append(os.path.join(runtime_dir, "podman", "podman.sock"))

    for candidate in candidates:
        path = candidate.removeprefix("unix://")
        if Path(path).is_socket():
            return f"unix://{path}"
    return None


#: ``STD_INPUT_HANDLE`` and the console input mode flags (wincon.h) needed to put
#: the Windows console into the raw, no-echo input mode a POSIX TTY gets from
#: ``tty.setraw()``. Defined unconditionally so the helpers below stay testable
#: off Windows.
_STD_INPUT_HANDLE = -10
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_WINDOW_INPUT = 0x0008
_ENABLE_MOUSE_INPUT = 0x0010
_ENABLE_INSERT_MODE = 0x0020
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

#: Console input flags cleared while attached. ``ENABLE_ECHO_INPUT`` is the one
#: that duplicates keystrokes (local echo plus the container TTY echo); the rest
#: stop line editing, Ctrl+C-as-signal, resize/mouse records and click-to-select
#: from interfering with the agent's own line editing.
#:
#: ``ENABLE_VIRTUAL_TERMINAL_INPUT`` is cleared rather than set, although it would
#: hand us ready-made VT sequences: it switches the pseudo-console to generating
#: bytes per key *event*, and because the CRT flips the console mode around every
#: single ``msvcrt.getwch()`` read, press and release then get encoded
#: inconsistently -- one ``a`` arrives as ``a`` plus a kitty ``ESC[97;1:3u``
#: key-release report that the agent inserts as text. Key encoding therefore
#: stays legacy and ``_EXTENDED_KEY_SEQUENCES`` does the translating.
_RAW_CONSOLE_INPUT_CLEAR = (
    _ENABLE_ECHO_INPUT
    | _ENABLE_LINE_INPUT
    | _ENABLE_PROCESSED_INPUT
    | _ENABLE_WINDOW_INPUT
    | _ENABLE_MOUSE_INPUT
    | _ENABLE_INSERT_MODE
    | _ENABLE_QUICK_EDIT_MODE
    | _ENABLE_VIRTUAL_TERMINAL_INPUT
)

#: Scan codes reported after the ``\x00``/``\xe0`` console prefix, mapped to the VT
#: sequences a Linux TTY expects. This is how navigation keys reach the agent:
#: the console is kept in its legacy key reporting (see
#: :func:`_raw_console_input_mode` for why), so every arrow/Home/End/Delete/F key
#: arrives as a scan code pair.
_EXTENDED_KEY_SEQUENCES: dict[int, bytes] = {
    59: b"\x1bOP",  # F1
    60: b"\x1bOQ",  # F2
    61: b"\x1bOR",  # F3
    62: b"\x1bOS",  # F4
    63: b"\x1b[15~",  # F5
    64: b"\x1b[17~",  # F6
    65: b"\x1b[18~",  # F7
    66: b"\x1b[19~",  # F8
    67: b"\x1b[20~",  # F9
    68: b"\x1b[21~",  # F10
    71: b"\x1b[H",  # Home
    72: b"\x1b[A",  # Arrow Up
    73: b"\x1b[5~",  # Page Up
    75: b"\x1b[D",  # Arrow Left
    77: b"\x1b[C",  # Arrow Right
    79: b"\x1b[F",  # End
    80: b"\x1b[B",  # Arrow Down
    81: b"\x1b[6~",  # Page Down
    82: b"\x1b[2~",  # Insert
    83: b"\x1b[3~",  # Delete
    133: b"\x1b[23~",  # F11
    134: b"\x1b[24~",  # F12
    15: b"\x1b[Z",  # Shift+Tab
    115: b"\x1b[1;5D",  # Ctrl+Left
    116: b"\x1b[1;5C",  # Ctrl+Right
    141: b"\x1b[1;5A",  # Ctrl+Up
    145: b"\x1b[1;5B",  # Ctrl+Down
    119: b"\x1b[1;5H",  # Ctrl+Home
    117: b"\x1b[1;5F",  # Ctrl+End
    147: b"\x1b[3;5~",  # Ctrl+Delete
}


def _encode_console_character(ch: str) -> bytes:
    """Encode one console key for the container TTY.

    Always UTF-8: the container side is a Linux PTY, while the Windows console
    hands us the character in its own codepage (cp850/cp1252 for a legacy
    console), whose bytes are invalid UTF-8 as soon as a non-ASCII key is typed.
    """
    return ch.encode("utf-8", errors="replace")


#: A kitty keyboard protocol key report (CSI u): a codepoint, then optional
#: shifted/base alternates and a modifier field, any of which may carry a
#: ``:value`` sub-field. Group 1 is the event type of the modifier field, where
#: ``2`` is auto-repeat and ``3`` is a key *release*.
_KITTY_KEY_REPORT = re.compile(rb"\x1b\[\d+(?::\d+)?(?:;\d+(?::(\d+))?)?u")

#: CSI/SS3 final bytes (0x40-0x7E per ECMA-48); ``[`` opens a CSI and ``O`` opens
#: SS3, so both fall in that range but act as intermediates here.
_CSI_FINAL_FIRST, _CSI_FINAL_LAST = 0x40, 0x7E
_CSI_INTERMEDIATES = "[O"
_MAX_ESCAPE_SEQUENCE_CHARS = 32

#: How long a bare ``ESC`` waits for the rest of its sequence before it is
#: forwarded as the Escape key itself. Bytes of one sequence normally arrive as a
#: single burst; the grace period only covers a pseudo-console that delivers that
#: burst trickling, and stays far below anything a user would notice.
_ESCAPE_SEQUENCE_GRACE_SECONDS = 0.05
_KEY_POLL_INTERVAL = 0.002
#: How often the console input thread checks for a key while idle. It bounds
#: both the typing latency and how long the thread outlives the session.
_KEY_WAIT_POLL_SECONDS = 0.01


def _console_key_available(deadline: float | None = None) -> bool:
    """True once the console has further key events queued.

    With a *deadline* the answer is waited for (polled, not blocked) until then,
    so a sequence split across several reads is still recognized.
    """
    kbhit = getattr(msvcrt, "kbhit", None) if msvcrt is not None else None
    if kbhit is None:
        return False
    while True:
        try:
            if kbhit():
                return True
        except OSError:
            return False
        if deadline is None or time.monotonic() >= deadline:
            return False
        time.sleep(_KEY_POLL_INTERVAL)


def _console_escape_bytes() -> bytes:
    """Forward the escape sequence queued behind a bare ``ESC`` key read.

    A terminal (or the pseudo-console replaying its output as key events for a
    legacy console app) reports one key as a burst of events, so what follows
    belongs to this sequence -- even when the pseudo-console hands the burst out
    a few bytes at a time, which the grace period in ``_console_key_available``
    covers. Kitty key *release* reports -- one ``a`` arriving as ``a`` plus an
    ``ESC[97;1:3u`` -- are dropped, since a Linux PTY has no such thing and a
    prompt that never enabled the protocol inserts them as stray text. Press and
    auto-repeat reports, Alt-as-Meta (``ESC`` plus a char), bracketed paste
    markers and SS3 keys are forwarded verbatim.
    """
    if msvcrt is None or getattr(msvcrt, "kbhit", None) is None:
        # No way to look ahead: keep the one-key-read-per-byte behaviour.
        return b"\x1b"
    deadline = time.monotonic() + _ESCAPE_SEQUENCE_GRACE_SECONDS
    chars: list[str] = []
    while len(chars) < _MAX_ESCAPE_SEQUENCE_CHARS and _console_key_available(deadline):
        try:
            ch = msvcrt.getwch()
        except (EOFError, KeyboardInterrupt, OSError):
            break
        chars.append(ch)
        if ch not in _CSI_INTERMEDIATES and _CSI_FINAL_FIRST <= ord(ch) <= _CSI_FINAL_LAST:
            break
    if not chars:
        # Nothing arrived: it really was the Escape key (vim escape, TUI
        # interrupt), so hand it over instead of holding it back.
        return b"\x1b"
    sequence = b"\x1b" + _encode_console_character("".join(chars))
    report = _KITTY_KEY_REPORT.fullmatch(sequence)
    if report is not None and (report.group(1) or b"1") == b"3":
        return b""
    return sequence


def _console_key_bytes(ch: str) -> bytes:
    """Bytes to forward for one console key read, ``b""`` to forward nothing.

    Navigation and function keys arrive from the Windows console as a
    ``\\x00``/``\\xe0`` prefix plus a scan code, which a Linux TTY cannot make
    sense of (once UTF-8 encoded it turns into mojibake such as ``\\xc3\\xa0H``
    for Arrow Up, i.e. stray characters in the agent prompt), so they are mapped
    to the VT sequences a terminal would send. Unmapped codes are dropped rather
    than sent as garbage. Backspace is normalized to ``\\x7f``, which is what a
    Linux terminal sends for that key (``\\x08`` shows up as ``^H`` in prompts
    that only bind DEL). A bare ``ESC`` hands over to
    :func:`_console_escape_bytes`, which assembles and screens the rest of the
    sequence.
    """
    if ch in ("\x00", "\xe0") and msvcrt is not None:
        try:
            code = msvcrt.getwch()
        except (EOFError, KeyboardInterrupt, OSError):
            return b""
        return _EXTENDED_KEY_SEQUENCES.get(ord(code), b"")
    if ch == "\x1b":
        return _console_escape_bytes()
    if ch == "\x08":
        return b"\x7f"
    return _encode_console_character(ch)


def _raw_console_input_mode(old_mode: int) -> int:
    """Console input mode with local echo and line editing off.

    Only ``_RAW_CONSOLE_INPUT_CLEAR`` bits are touched, so anything the console
    was configured with beyond raw input (and the mode the caller restores) is
    preserved; see that constant for why VT input mode is cleared, not enabled.
    """
    return old_mode & ~_RAW_CONSOLE_INPUT_CLEAR


def _kernel32() -> Any | None:
    """``ctypes.windll.kernel32`` on Windows, ``None`` everywhere else."""
    if ctypes is None or sys.platform != "win32":
        return None
    try:
        return ctypes.windll.kernel32
    except Exception:  # pragma: no cover - exotic CPython builds
        return None


def _get_console_input_mode(kernel32: Any) -> int | None:
    """Current console input mode of our stdin, ``None`` if it is not a console."""
    if ctypes is None:
        return None
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(kernel32.GetStdHandle(_STD_INPUT_HANDLE), ctypes.pointer(mode)):
        return None
    return int(mode.value)


def _set_console_input_mode(kernel32: Any, mode: int) -> bool:
    if ctypes is None:
        return False
    handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    # SetConsoleMode takes the mode by value (DWORD), unlike GetConsoleMode's
    # LPDWORD; a pointer here is read as mode bits and the call fails.
    return bool(kernel32.SetConsoleMode(handle, ctypes.c_uint32(mode)))


def _enter_console_raw_input() -> int | None:
    """Switch the Windows console input to raw, no-echo mode.

    The POSIX attach path gets this from ``tty.setraw()`` for free. Without the
    equivalent console request the console host keeps handling input itself:
    keystrokes are echoed locally on top of the container TTY echo (typed text
    shows up doubled — ``msvcrt.getwch()`` clears echo only inside its own read),
    input is line-buffered, and Ctrl+C becomes a local ``KeyboardInterrupt``
    instead of byte ``0x03`` for the agent. Returns the previous mode to restore,
    or ``None`` when stdin is not a console window or the mode cannot be changed.
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    try:
        old_mode = _get_console_input_mode(kernel32)
        if old_mode is None:
            return None
        if not _set_console_input_mode(kernel32, _raw_console_input_mode(old_mode)):
            return None
        return old_mode
    except Exception:  # pragma: no cover - console API failure
        return None


def _leave_console_raw_input(old_mode: int) -> None:
    """Restore a mode saved by :func:`_enter_console_raw_input`."""
    kernel32 = _kernel32()
    if kernel32 is None:
        return
    try:
        _set_console_input_mode(kernel32, old_mode)
    except Exception:  # pragma: no cover - console API failure
        pass


def _forward_windows_console_input(sock: Any, logger: Any, stop_event: threading.Event) -> None:
    if msvcrt is None:
        return
    kbhit = getattr(msvcrt, "kbhit", None)
    while not stop_event.is_set():
        if kbhit is not None:
            # Only read once a key is queued: a getwch() left blocked after the
            # session ended would steal the next keystroke, and on return the
            # CRT puts back the raw console mode we have already restored.
            try:
                if not kbhit():
                    time.sleep(_KEY_WAIT_POLL_SECONDS)
                    continue
            except OSError:
                return
        try:
            data = _console_key_bytes(msvcrt.getwch())
        except (EOFError, KeyboardInterrupt, OSError):
            return
        if not data:
            continue
        if stop_event.is_set():
            # The session ended while getwch() waited; the socket is closed.
            return
        if logger is not None:
            logger.log_input(data)
        try:
            sock.sendall(data)
        except Exception:
            # A closed NpipeSocket raises RuntimeError, a broken pipe
            # pywintypes.error; neither is an OSError.
            return


#: ``pywintypes.error`` codes a named-pipe read raises when the stream is over
#: rather than broken: ``ERROR_BROKEN_PIPE`` (109, the daemon closed its end;
#: ``docker.utils.socket.read`` maps it to EOF the same way), ``ERROR_NO_DATA``
#: (232, pipe being closed), ``ERROR_PIPE_NOT_CONNECTED`` (233, the server
#: disconnected) and ``ERROR_OPERATION_ABORTED`` (995, the read was cancelled
#: because the handle was closed).
_NPIPE_EOF_ERRORS = frozenset({109, 232, 233, 995})

#: Exit codes of a container ended by ``container.stop()``: SIGTERM (143), or
#: SIGKILL (137) once the stop timeout ran out.
_STOP_EXIT_CODES = frozenset({137, 143})

#: How often the main thread wakes while a :class:`_ThreadedReader` is waiting, so
#: Ctrl+C and signal handlers still run promptly.
_PIPE_POLL_SECONDS = 0.1

#: Chunks a :class:`_ThreadedReader` buffers ahead of a slow consumer (a busy
#: terminal or ACP client) before it stops reading: back-pressure, as the
#: select() loop gets for free, instead of unbounded memory.
_READER_QUEUE_CHUNKS = 64


def _is_selectable(sock: Any) -> bool:
    """True when ``select()`` accepts *sock*.

    On Windows over ``npipe://`` (Docker Desktop, Podman Desktop) the attach
    socket is a docker-py ``NpipeSocket``, which has no ``fileno()``, and
    Windows ``select()`` only takes Winsock sockets anyway. TCP and SSH
    connections hand back real sockets, also on Windows.
    """
    fileno = getattr(sock, "fileno", None)
    if not callable(fileno):
        return False
    try:
        return isinstance(fileno(), int)
    except Exception:
        return False


def _recv_chunk(sock: Any, buf: bytearray, pipe: bool) -> bytes:
    """One blocking read from the attach socket into *buf*; ``b""`` at EOF.

    A named pipe (*pipe*) is read with the overlapped ``recv_into`` rather than
    ``recv``: its handle is opened for overlapped I/O, and a synchronous
    ``ReadFile`` on it may report completion early while the input thread
    writes to the same handle. Only the codes in ``_NPIPE_EOF_ERRORS`` count as
    EOF; any other error propagates, so a broken connection is not mistaken for
    the agent exiting.
    """
    if not pipe:
        return bytes(sock.recv(len(buf)))
    try:
        count = sock.recv_into(buf)
    except Exception as exc:
        if exc.args and exc.args[0] in _NPIPE_EOF_ERRORS:
            return b""
        raise
    return bytes(memoryview(buf)[:count])


def _can_select_stdin() -> bool:
    """False on Windows, where ``select()`` takes only Winsock sockets."""
    return sys.platform != "win32"


class _ThreadedReader:
    """Read the attach socket on a helper thread instead of ``select()``.

    The main thread collects chunks with :meth:`get`, which returns after at
    most *timeout* seconds, so it keeps handling Ctrl+C, signals and its own
    deadlines while the read blocks. The queue is bounded; :meth:`close` lets a
    reader waiting for room exit.
    """

    def __init__(self, sock: Any, bufsize: int) -> None:
        # docker-py gives the socket the client's 60 s timeout, which a
        # blocking read honours: an agent idle for a minute would end the
        # session. select() never needed it, so turn it off.
        try:
            sock.settimeout(None)
        except Exception:  # pragma: no cover - best effort
            pass
        self._sock = sock
        self._buf = bytearray(bufsize)
        self._pipe = not _is_selectable(sock)
        self._closed = threading.Event()
        self._chunks: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=_READER_QUEUE_CHUNKS)
        self._thread = threading.Thread(target=self._run, name="vibepod-attach-output", daemon=True)
        self._thread.start()

    def _put(self, item: bytes | BaseException) -> bool:
        """Queue *item*, waiting for room; False once the reader was closed."""
        while not self._closed.is_set():
            try:
                self._chunks.put(item, timeout=_PIPE_POLL_SECONDS)
            except queue.Full:
                continue
            return True
        return False

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                data = _recv_chunk(self._sock, self._buf, self._pipe)
            except BaseException as exc:
                self._put(exc)
                return
            if not self._put(data) or not data:
                return

    def close(self) -> None:
        """Stop the reader once its current read returns; queued data is dropped."""
        self._closed.set()

    def get(self, timeout: float) -> bytes | None:
        """Next chunk, ``b""`` at EOF, ``None`` when *timeout* passed first.

        Re-raises the error that ended the reader thread.
        """
        try:
            item = self._chunks.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            raise item
        return item


def _forward_stdin(
    stdin_fd: int,
    sock: Any,
    container: Any,
    eof_stop: threading.Event,
    stop_failures: list[BaseException],
) -> None:
    """Copy ACP client input from *stdin_fd* to the attach socket on a thread.

    On stdin EOF a real socket is half-closed so the adapter sees EOF and exits
    while its output keeps draining. A named pipe cannot be half-closed
    (``NpipeSocket.shutdown`` closes the whole handle, cutting off the output
    still to come), so the container is stopped instead, which ends the stream;
    *eof_stop* records that, so the caller does not report the resulting
    SIGTERM exit as a failure. A stop that fails is appended to
    *stop_failures*, so the caller ends the session instead of waiting on a
    container that never sees EOF.
    """
    while True:
        try:
            data = os.read(stdin_fd, 65536)
        except OSError:
            data = b""
        if not data:
            if _is_selectable(sock):
                try:
                    sock.shutdown(socket.SHUT_WR)
                except OSError:  # pragma: no cover - already closed
                    pass
                return
            eof_stop.set()
            try:
                container.stop(timeout=5)
            except Exception as exc:
                stop_failures.append(exc)
            return
        try:
            sock.sendall(data)
        except Exception:
            return


def _is_latest_tag(image: str) -> bool:
    """Return True when *image* uses the ``latest`` tag (explicitly or by omission)."""
    name = image.split("/")[-1]
    return ":" not in name or name.endswith(":latest")


def _normalize_command(value: Any) -> list[str]:
    """Normalize Docker command/entrypoint values to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(part) for part in value]
    return [str(value)]


def _version_is_podman(version: Any) -> bool:
    """Return True when Docker-compatible API version metadata belongs to Podman."""
    if not isinstance(version, dict):
        return False

    components = version.get("Components", [])
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            name = str(component.get("Name", "")).lower()
            if "podman" in name:
                return True

    platform = version.get("Platform")
    if isinstance(platform, dict) and "podman" in str(platform.get("Name", "")).lower():
        return True

    return "podman" in str(version.get("Name", "")).lower()


def _reference_namespace(reference: str) -> str | None:
    """Return the namespace of an image *reference*, ignoring registry and digest.

    ``vibepod/claude@sha256:…`` and ``ghcr.io/vibepod/claude@sha256:…`` both
    yield ``vibepod``. A bare ``python@sha256:…`` has no namespace, and neither
    has a deeper path such as ``ghcr.io/acme/vibepod/tool`` — that repository
    belongs to ``acme``, not to us.
    """
    repository = reference.split("@", 1)[0]
    parts = repository.split("/")
    if len(parts) > 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        # Drop the registry host; what remains must be exactly <namespace>/<image>.
        parts = parts[1:]
    if len(parts) != 2:
        return None
    return parts[0]


def parse_image_name(image: str) -> tuple[str, str | None]:
    """Parse a full image string into repository and tag/digest."""
    if "@" in image:
        repository, tag = image.split("@", 1)
        return repository, tag
    elif ":" in image:
        parts = image.rsplit(":", 1)
        if "/" not in parts[1]:
            return parts[0], parts[1]
    return image, None


class DockerManager:
    """Manager for all Docker operations."""

    def __init__(self) -> None:
        if docker is None:
            raise DockerClientError("Docker SDK not installed")
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as exc:
            podman_socket = _discover_podman_socket()
            if podman_socket is None:
                raise DockerClientError(f"Docker is not available: {exc}. {_PODMAN_HINT}") from exc
            try:
                self.client = docker.DockerClient(base_url=podman_socket)
                self.client.ping()
            except DockerException as retry_exc:
                raise DockerClientError(
                    f"Docker is not available: {exc}. "
                    f"Found Podman socket {podman_socket} but could not connect: {retry_exc}",
                ) from retry_exc

        self._rootless_podman: bool | None = None
        self._podman: bool | None = None

    def is_podman(self) -> bool:
        """Return True when the Docker-compatible engine is Podman."""
        cached = getattr(self, "_podman", None)
        if isinstance(cached, bool):
            return cached

        try:
            version = self.client.version()
        except (APIError, DockerException, AttributeError):
            self._podman = False
            return False

        self._podman = _version_is_podman(version)
        return self._podman

    def is_rootless_podman(self) -> bool:
        """Return True for a rootless Podman engine exposed through the Docker API."""
        cached = getattr(self, "_rootless_podman", None)
        if isinstance(cached, bool):
            return cached

        try:
            info = self.client.info()
        except (APIError, DockerException, AttributeError):
            self._rootless_podman = False
            return False

        if not isinstance(info, dict):
            self._rootless_podman = False
            return False

        security_options = info.get("SecurityOptions", [])
        rootless = bool(info.get("Rootless")) or (
            isinstance(security_options, list)
            and any(str(option).lower() == "name=rootless" for option in security_options)
        )
        self._rootless_podman = rootless and self.is_podman()
        return self._rootless_podman

    def supports_host_socket_mounts(self) -> bool:
        """Return True when a host unix socket can be bind-mounted into a container.

        Every engine off Linux (Docker Desktop, Colima, Podman machine, ...) runs
        inside a VM and reaches host paths through a file share that does not
        carry socket inodes: the bind fails with ``mkdir …: operation not
        supported`` (Docker on macOS, issue #203) or ``make volume mountpoint
        for volume …: operation not supported`` (Podman, issue #170) and takes
        the whole run down with it.
        """
        return sys.platform.startswith("linux")

    def _pull_image_with_progress(self, image: str) -> None:
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        from vibepod.utils.console import console

        repository, tag = parse_image_name(image)
        try:
            response = self.client.api.pull(repository, tag=tag, stream=True, decode=True)
        except APIError as exc:
            raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc

        tasks: dict[str, Any] = {}
        try:
            with Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                disable=not console.is_terminal,
            ) as progress:
                for chunk in response:
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:
                        raise DockerClientError(f"Failed to pull image {image}: {chunk['error']}")

                    status = chunk.get("status", "")
                    layer_id = chunk.get("id")
                    progress_detail = chunk.get("progressDetail") or {}

                    if not layer_id:
                        if status and status not in (
                            "Downloading",
                            "Extracting",
                            "Waiting",
                            "Download complete",
                            "Pull complete",
                            "Already exists",
                            "Pulling fs layer",
                        ):
                            progress.console.print(f"[dim]{status}[/dim]")
                        continue

                    status_color = "cyan"
                    if status in ("Download complete", "Pull complete", "Already exists"):
                        status_color = "green"
                    elif status == "Waiting":
                        status_color = "yellow"
                    elif "error" in status.lower() or "fail" in status.lower():
                        status_color = "red"

                    description = f"[{status_color}][{layer_id}][/{status_color}] {status}"

                    if layer_id not in tasks:
                        tasks[layer_id] = progress.add_task(description, total=None)

                    task_id = tasks[layer_id]
                    progress.update(task_id, description=description)

                    total = progress_detail.get("total", 0)
                    current = progress_detail.get("current", 0)

                    if total:
                        progress.update(task_id, total=total, completed=current)

                    if status in ("Download complete", "Pull complete", "Already exists"):
                        if total:
                            progress.update(task_id, completed=total, total=total)
                        else:
                            progress.update(task_id, completed=1, total=1)
        except Exception as exc:
            if isinstance(exc, DockerClientError):
                raise
            if isinstance(exc, APIError):
                raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc
            raise DockerClientError(f"Failed to pull image {image}: {exc}") from exc

    def pull_image(self, image: str, auto_clean: bool = False) -> None:
        self._pull_image_with_progress(image)
        if auto_clean:
            self.clean_untagged_images()

    def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
        """Pull *image* and return True if the local image was updated.

        Returns False when the image is already up to date, when the pull
        fails (e.g. no network / private registry), or when the image only
        exists locally and cannot be found on a registry.

        With *auto_clean*, untagged images left behind by this and earlier
        pulls are swept afterwards (see :meth:`clean_untagged_images`).
        """
        try:
            old_id = self.image_id(image)
            self.pull_image(image)
            new_id = self.image_id(image)
            if new_id is None:
                return False

            if auto_clean:
                self.clean_untagged_images()
            return bool(old_id != new_id)
        except (APIError, DockerClientError):
            return False

    def image_id(self, image: str) -> str | None:
        """Return the local id of *image*, or None when it cannot be determined."""
        try:
            return str(self.client.images.get(image).id)
        except DockerException:
            # Covers NotFound and any transient daemon error: callers treat a
            # missing id as "no confirmed image", never as a failure to report.
            return None

    def require_proxy_image(
        self,
        image: str | Any,
        policy_schema: str = PROXY_POLICY_SCHEMA,
        min_version: str = PROXY_MIN_VERSION,
    ) -> None:
        """Require a proxy image with an exact policy schema and a minimum version."""
        image_name = image if isinstance(image, str) else "the running proxy image"
        try:
            inspected = self.client.images.get(image) if isinstance(image, str) else image
            attrs = inspected.attrs
            config = attrs.get("Config", {}) if isinstance(attrs, dict) else {}
            labels = config.get("Labels", {}) if isinstance(config, dict) else {}
            if not isinstance(labels, dict):
                labels = {}
        except (AttributeError, DockerException) as exc:
            raise DockerClientError(
                f"Proxy image {image_name} could not be inspected for policy schema "
                f"{policy_schema}",
            ) from exc
        actual_schema = labels.get(PROXY_POLICY_SCHEMA_LABEL)
        if actual_schema != policy_schema:
            shown = actual_schema if isinstance(actual_schema, str) and actual_schema else "missing"
            raise DockerClientError(
                f"Proxy image {image_name} is incompatible: required policy schema "
                f"{policy_schema}, found {shown}. Use an updated VibePod proxy image or add "
                f"the label {PROXY_POLICY_SCHEMA_LABEL}={policy_schema} to a compatible "
                "custom image.",
            )
        actual_version = labels.get(PROXY_VERSION_LABEL)
        parsed = _parse_version(actual_version)
        required = _parse_version(min_version)
        if parsed is None or (required is not None and parsed < required):
            shown = (
                actual_version if isinstance(actual_version, str) and actual_version else "missing"
            )
            raise DockerClientError(
                f"Proxy image {image_name} is too old: required version {min_version} or "
                f"newer, found {shown}. Pull an updated VibePod proxy image or add the label "
                f"{PROXY_VERSION_LABEL}=<version> to a compatible custom image.",
            )

    def build_image(
        self,
        context_tar: Any,
        tag: str,
        labels: dict[str, str],
        *,
        nocache: bool = False,
    ) -> None:
        """Build *tag* from an in-memory custom-context tar, streaming output.

        The tar must contain a complete Dockerfile (see
        :func:`vibepod.core.overlay.build_context_tar`); nothing is written to
        the host filesystem. *nocache* disables docker's layer cache so a
        forced rebuild re-runs every instruction.
        """
        try:
            chunks = self.client.api.build(
                fileobj=context_tar,
                custom_context=True,
                tag=tag,
                labels=labels,
                rm=True,
                nocache=nocache,
                decode=True,
            )
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk:
                    raise DockerClientError(f"Failed to build image {tag}: {chunk['error']}")
                stream = chunk.get("stream")
                if stream and stream.strip():
                    # Through the shared console so ACP mode's stderr routing
                    # keeps build logs off the JSON-RPC stream on stdout.
                    from vibepod.utils.console import console

                    console.out(stream, end="" if stream.endswith("\n") else "\n", highlight=False)
        except DockerClientError:
            raise
        except (APIError, DockerException) as exc:
            raise DockerClientError(f"Failed to build image {tag}: {exc}") from exc

    def remove_stale_overlays(self, overlay_key: str, keep_tag: str) -> int:
        """Remove overlay images labeled with *overlay_key* except *keep_tag*.

        Old content-addressed tags accumulate as the overlay evolves; each
        successful build sweeps its predecessors. Images docker refuses to
        remove (still used by a container) stay and are retried next build.
        The label name is ``vibepod.core.overlay.OVERLAY_KEY_LABEL`` (kept as
        a literal here to avoid an import cycle with that module).
        """
        try:
            images = self.client.images.list(
                filters={"label": [f"vibepod.overlay.key={overlay_key}"]},
            )
        except DockerException:
            return 0

        removed = 0
        for image in images:
            if keep_tag in (image.tags or []):
                continue
            try:
                self.client.images.remove(str(image.id))
            except DockerException:
                continue
            removed += 1
        return removed

    def clean_untagged_images(self, namespace: str = IMAGE_NAMESPACE) -> int:
        """Remove untagged *namespace* images and return how many were removed.

        A pull that moves the ``latest`` tag leaves the previous image behind
        with no tag but with its repository digest intact, so it still shows up
        as ``vibepod/claude <none>``. Sweeping the whole namespace — rather
        than only the image just replaced — also clears leftovers from earlier
        pulls whose removal failed because a container still held them.

        Images that are still tagged, that belong to another namespace, or that
        cannot be attributed to one are never touched. Docker refuses to remove
        images used by a container (running or stopped); those stay and are
        retried by the next sweep.
        """
        try:
            images = self.client.images.list()
        except DockerException:
            return 0

        removed = 0
        for image in images:
            if image.tags:
                continue
            digests = image.attrs.get("RepoDigests") or []
            if not any(_reference_namespace(str(digest)) == namespace for digest in digests):
                continue
            try:
                self.client.images.remove(str(image.id))
            except DockerException:
                continue
            removed += 1
        return removed

    def ensure_network(self, name: str) -> None:
        try:
            self.client.networks.get(name)
        except NotFound:
            self.client.networks.create(name, labels={CONTAINER_LABEL_MANAGED: "true"})

    def networks_with_running_containers(self) -> list[str]:
        networks: set[str] = set()
        for container in self.client.containers.list():
            try:
                attached = container.attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
            except AttributeError:
                continue
            networks.update(attached.keys())
        return sorted(networks)

    def connect_network(self, container: Any, network_name: str) -> None:
        try:
            network = self.client.networks.get(network_name)
            network.connect(container)
        except APIError as exc:
            raise DockerClientError(f"Failed to connect to network {network_name}: {exc}") from exc

    def get_container(self, name_or_id: str) -> Any:
        try:
            return self.client.containers.get(name_or_id)
        except NotFound as exc:
            raise DockerClientError(f"Container '{name_or_id}' not found") from exc
        except APIError as exc:
            raise DockerClientError(f"Failed to look up container '{name_or_id}': {exc}") from exc
        except DockerException as exc:
            raise DockerClientError(f"Failed to look up container '{name_or_id}': {exc}") from exc

    def resolve_launch_command(self, image: str, command: list[str] | None) -> list[str]:
        """Resolve the full executable argv for a container start."""
        try:
            image_obj = self.client.images.get(image)
        except NotFound as exc:
            raise DockerClientError(
                f"Image {image} not found locally. Pull the image first (for example with --pull).",
            ) from exc
        except APIError as exc:
            raise DockerClientError(f"Failed to inspect image {image}: {exc}") from exc
        except DockerException as exc:
            raise DockerClientError(f"Failed to inspect image {image}: {exc}") from exc

        image_config = image_obj.attrs.get("Config", {}) if hasattr(image_obj, "attrs") else {}
        if not isinstance(image_config, dict):
            image_config = {}

        image_entrypoint = _normalize_command(image_config.get("Entrypoint"))
        image_cmd = _normalize_command(image_config.get("Cmd"))
        effective_cmd = command if command is not None else image_cmd
        launch = [*image_entrypoint, *effective_cmd]

        if not launch:
            raise DockerClientError(
                f"Could not resolve a startup command for image {image}. "
                "Specify a command in the image or in agent settings.",
            )
        return launch

    def run_agent(
        self,
        *,
        agent: str,
        image: str,
        workspace: Path,
        config_dir: Path,
        config_mount_path: str,
        env: dict[str, str],
        command: list[str] | None,
        auto_remove: bool,
        name: str | None,
        version: str,
        network: str | None = None,
        ports: dict[str, Any] | None = None,
        extra_volumes: list[tuple[str, str, str]] | None = None,
        platform: str | None = None,
        user: str | None = None,
        entrypoint: list[str] | None = None,
        userns_mode: str | None = None,
        extra_labels: dict[str, str] | None = None,
        workspace_mount_path: str | None = None,
        start: bool = True,
        tty: bool = True,
    ) -> Any:
        container_name = name or f"vibepod-{agent}-{uuid4().hex[:8]}"

        labels = {
            CONTAINER_LABEL_MANAGED: "true",
            "vibepod.agent": agent,
            "vibepod.workspace": str(workspace),
            "vibepod.version": version,
            **(extra_labels or {}),
        }

        environment = {**env}

        workspace_mode = bind_mode(workspace)
        config_mode = bind_mode(config_dir)
        report_selinux_relabel()
        if workspace_mode == "rw" and _selinux_enforcing() and selinux_relabel_enabled():
            from vibepod.utils.console import warning

            warning(
                f"Not relabeling {workspace} for SELinux (home, system or read-only dir); "
                "the agent may be unable to access it.",
            )
        volumes: list[str] = [
            f"{workspace}:/workspace:{workspace_mode}",
            f"{config_dir}:{config_mount_path}:{config_mode}",
        ]
        if workspace_mount_path:
            # ACP path parity: bind the workspace a second time onto its own
            # host path so host-side absolute paths (ACP session cwd, @-mentions,
            # diffs) resolve identically inside the container.
            volumes.insert(1, f"{workspace}:{workspace_mount_path}:{workspace_mode}")
        if extra_volumes:
            # Mounted with the mode the caller chose: user volumes and host-owned
            # files (herdr, X11) must keep their SELinux label.
            volumes.extend(f"{host}:{bind}:{mode}" for host, bind, mode in extra_volumes)

        try:
            if userns_mode is not None or not start:
                # Low-level create path: needed for Podman's `keep-id` (docker-py
                # rejects it) and for ACP's create-without-start ordering, and
                # because the high-level containers.create/run does not accept
                # `stdin_once` (the stdin-EOF lifecycle hook).
                host_config = self.client.api.create_host_config(
                    binds=volumes,
                    auto_remove=auto_remove,
                    network_mode=network,
                    port_bindings=ports,
                    extra_hosts={"host.docker.internal": "host-gateway"},
                )
                # docker-py validates userns_mode against Docker's enum and rejects
                # Podman's `keep-id`, so set the Docker-compatible HostConfig field
                # directly for Podman engines.
                if userns_mode is not None:
                    host_config["UsernsMode"] = userns_mode

                create_kwargs: dict[str, Any] = {
                    "image": image,
                    "name": container_name,
                    "command": command,
                    "tty": tty,
                    "stdin_open": True,
                    "labels": labels,
                    "environment": environment,
                    "working_dir": workspace_mount_path or "/workspace",
                    "host_config": host_config,
                }
                # Note: docker-py derives StdinOnce=true itself from
                # detach=False + stdin_open=True (ContainerConfig), which is
                # the ACP lifecycle hook — no explicit flag exists.
                if ports:
                    # (port, proto) tuples: raw "1456/tcp" keys would be
                    # re-suffixed by docker-py's exposed-port normalization
                    # into "1456/tcp/tcp".
                    create_kwargs["ports"] = [tuple(p.split("/", 1)) for p in ports]
                if platform:
                    create_kwargs["platform"] = platform
                if user:
                    create_kwargs["user"] = user
                if entrypoint:
                    create_kwargs["entrypoint"] = entrypoint

                created = self.client.api.create_container(**create_kwargs)
                container_id = created["Id"]
                if start:
                    self.client.api.start(container_id)
                return self.client.containers.get(container_id)

            run_kwargs: dict[str, Any] = {
                "image": image,
                "name": container_name,
                "command": command,
                "detach": True,
                "tty": tty,
                "stdin_open": True,
                "auto_remove": auto_remove,
                "labels": labels,
                "environment": environment,
                "volumes": volumes,
                "working_dir": workspace_mount_path or "/workspace",
                "network": network,
                # Docker Desktop resolves host.docker.internal natively, but Docker
                # Engine on Linux and Podman only do so with an explicit
                # host-gateway mapping. Local LLM servers (Ollama, LM Studio, ...)
                # are documented against this hostname, so map it unconditionally.
                "extra_hosts": {"host.docker.internal": "host-gateway"},
            }
            if ports:
                # Maps "<container_port>/tcp" -> host_port. Used to publish the
                # Codex OAuth callback forwarder during `codex login`.
                run_kwargs["ports"] = ports
            if platform:
                run_kwargs["platform"] = platform
            if user:
                run_kwargs["user"] = user
            if entrypoint:
                run_kwargs["entrypoint"] = entrypoint

            return self.client.containers.run(**run_kwargs)
        except APIError as exc:
            raise DockerClientError(f"Failed to start container: {exc}") from exc

    def stop_agent(self, agent: str, force: bool = False) -> int:
        stopped = 0
        timeout = 0 if force else 10
        # Containers started before a rename still carry the legacy agent label.
        labels = agent_ids(agent)
        for container in self.list_managed(all_containers=True):
            if container.labels.get("vibepod.agent") not in labels:
                continue
            try:
                container.stop(timeout=timeout)
            except APIError as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}",
                ) from exc
            except DockerException as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}",
                ) from exc
            stopped += 1
        return stopped

    def stop_container(self, name_or_id: str, force: bool = False) -> Any:
        container = self.get_container(name_or_id)
        labels = getattr(container, "labels", {}) or {}
        if labels.get(CONTAINER_LABEL_MANAGED) != "true":
            raise DockerClientError(
                f"Container '{name_or_id}' is not managed by VibePod; refusing to stop.",
            )
        try:
            container.stop(timeout=0 if force else 10)
        except APIError as exc:
            raise DockerClientError(
                f"Failed to stop container '{name_or_id}': {exc}",
            ) from exc
        except DockerException as exc:
            raise DockerClientError(
                f"Failed to stop container '{name_or_id}': {exc}",
            ) from exc
        return container

    def stop_all(self, force: bool = False) -> int:
        stopped = 0
        timeout = 0 if force else 10
        for container in self.list_managed(all_containers=True):
            try:
                container.stop(timeout=timeout)
            except APIError as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}",
                ) from exc
            except DockerException as exc:
                raise DockerClientError(
                    f"Failed to stop container '{container.name}': {exc}",
                ) from exc
            stopped += 1
        return stopped

    def list_managed(self, all_containers: bool = False) -> list[Any]:
        filters = {"label": f"{CONTAINER_LABEL_MANAGED}=true"}
        try:
            return list(self.client.containers.list(all=all_containers, filters=filters))
        except APIError as exc:
            raise DockerClientError(f"Failed to list containers: {exc}") from exc
        except DockerException as exc:
            raise DockerClientError(f"Failed to list containers: {exc}") from exc

    def find_datasette(self) -> Any | None:
        containers = self.client.containers.list(
            all=True,
            filters={"label": ["vibepod.managed=true", "vibepod.role=datasette"]},
        )
        return containers[0] if containers else None

    def ensure_datasette(
        self,
        image: str,
        logs_db_path: Path,
        proxy_db_path: Path,
        port: int,
    ) -> Any:
        existing = self.find_datasette()
        if existing:
            existing.reload()
            env_list = existing.attrs.get("Config", {}).get("Env", []) or []
            has_proxy_env = any(env.startswith("PROXY_DB_PATH=") for env in env_list)
            if existing.status == "running" and has_proxy_env:
                return existing
            existing.remove(force=True)

        if hasattr(self.client, "images"):
            try:
                self.client.images.get(image)
            except NotFound:
                self.pull_image(image)

        logs_db_path.parent.mkdir(parents=True, exist_ok=True)
        if not logs_db_path.exists():
            logs_db_path.touch()

        logs_parent = Path(os.path.abspath(str(logs_db_path.parent)))
        proxy_parent = Path(os.path.abspath(str(proxy_db_path.parent)))

        if logs_parent == proxy_parent:
            volumes = {str(logs_parent): {"bind": "/mount/data", "mode": bind_mode(logs_parent)}}
            logs_db_container_path = f"/mount/data/{logs_db_path.name}"
            proxy_db_container_path = f"/mount/data/{proxy_db_path.name}"
        else:
            volumes = {
                str(logs_parent): {"bind": "/mount/logs", "mode": bind_mode(logs_parent)},
                str(proxy_parent): {"bind": "/mount/proxy", "mode": bind_mode(proxy_parent)},
            }
            logs_db_container_path = f"/mount/logs/{logs_db_path.name}"
            proxy_db_container_path = f"/mount/proxy/{proxy_db_path.name}"
        report_selinux_relabel()

        return self.client.containers.run(
            image=image,
            name="vibepod-datasette",
            detach=True,
            labels={"vibepod.managed": "true", "vibepod.role": "datasette"},
            environment={
                "LOGS_DB_PATH": logs_db_container_path,
                "PROXY_DB_PATH": proxy_db_container_path,
                "DATASETTE_PORT": "8001",
            },
            volumes=volumes,
            ports={"8001/tcp": port},
        )

    def find_proxy(self) -> Any | None:
        containers = self.client.containers.list(
            all=True,
            filters={"label": ["vibepod.managed=true", "vibepod.role=proxy"]},
        )
        return containers[0] if containers else None

    def remove_proxy(self, existing: Any, timeout: float = 15.0) -> None:
        """Force-remove a proxy container and wait for it to disappear.

        Concurrent launches (e.g. an editor spawning `vp run` twice) can race
        on the removal; Docker then answers 409 "removal ... is already in
        progress", or 404 if the peer already finished. Treat both as success
        and wait until the container is gone so the caller can create its
        replacement.

        The wait tracks *this* container, not any proxy: a peer's replacement
        carries the same labels, so waiting for `find_proxy()` to go empty
        would never be satisfied once one exists.
        """
        target_id = getattr(existing, "id", None)
        try:
            existing.remove(force=True)
        except NotFound:
            return
        except APIError as exc:
            if "already in progress" not in str(exc):
                raise
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = self.find_proxy()
            if current is None or getattr(current, "id", None) != target_id:
                return
            time.sleep(0.2)
        raise DockerClientError(
            "Timed out waiting for the previous vibepod-proxy container to be removed.",
        )

    def ensure_proxy(
        self,
        image: str,
        db_path: Path,
        ca_dir: Path,
        network: str,
        policy_schema: str | None = None,
    ) -> Any:
        existing = self.find_proxy()
        if existing:
            if existing.status == "running":
                if policy_schema is not None:
                    self.require_proxy_image(existing.image, policy_schema)
                return existing
            self.remove_proxy(existing)

        if hasattr(self.client, "images"):
            try:
                self.client.images.get(image)
            except NotFound:
                self.pull_image(image)

        if policy_schema is not None:
            self.require_proxy_image(image, policy_schema)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        ca_dir.mkdir(parents=True, exist_ok=True)

        volumes = {
            str(db_path.parent): {"bind": "/data", "mode": bind_mode(db_path.parent)},
            str(ca_dir): {"bind": "/data/mitmproxy", "mode": bind_mode(ca_dir)},
        }
        report_selinux_relabel()

        run_kwargs: dict[str, Any] = {
            "image": image,
            "name": "vibepod-proxy",
            "detach": True,
            "labels": {"vibepod.managed": "true", "vibepod.role": "proxy"},
            "environment": {
                "PROXY_DB_PATH": "/data/proxy.db",
                "PROXY_CONF_DIR": "/data/mitmproxy",
            },
            "volumes": volumes,
            "network": network,
            "extra_hosts": {"host.docker.internal": "host-gateway"},
        }

        if not self.is_rootless_podman():
            getuid = getattr(os, "getuid", None)
            getgid = getattr(os, "getgid", None)
            if callable(getuid) and callable(getgid):
                run_kwargs["user"] = f"{getuid()}:{getgid()}"

        return self.client.containers.run(**run_kwargs)

    def attach_interactive(self, container: Any, logger: Any = None) -> bytes:
        """Attach local stdin/stdout to a running container TTY.

        Returns the tail (last ``ATTACH_TAIL_LIMIT`` bytes) of the container
        output, so callers can inspect it after the session ends (e.g. for
        agent resume hints).
        """

        def resize_tty() -> None:
            size = shutil.get_terminal_size(fallback=(120, 40))
            try:
                self.client.api.resize(container.id, height=size.lines, width=size.columns)
            except Exception:
                pass

        try:
            sock_wrapper = self.client.api.attach_socket(
                container.id,
                params={
                    "stdin": 1,
                    "stdout": 1,
                    "stderr": 1,
                    "stream": 1,
                    "logs": 1,
                },
            )
        except Exception as exc:  # pragma: no cover - runtime Docker behavior
            raise DockerClientError(f"Failed to attach to container: {exc}") from exc

        sock = getattr(sock_wrapper, "_sock", sock_wrapper)
        resize_tty()

        output_tail = bytearray()
        stdin_fd = None
        old_tty = None
        old_console_mode: int | None = None
        old_winch_handler = None
        input_stop_event: threading.Event | None = None
        input_thread: threading.Thread | None = None
        interactive_reader: _ThreadedReader | None = None
        sigwinch = getattr(signal, "SIGWINCH", None)
        try:
            if sys.stdin.isatty() and termios is not None and tty is not None:
                stdin_fd = sys.stdin.fileno()
                old_tty = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
                if sigwinch is not None:
                    old_winch_handler = signal.getsignal(sigwinch)

                    def _on_winch(signum: int, frame: Any) -> None:
                        del signum, frame
                        resize_tty()

                    signal.signal(sigwinch, _on_winch)
            elif sys.stdin.isatty() and msvcrt is not None:
                # The console host must stop interpreting/echoing input, see
                # _enter_console_raw_input(); the container TTY is the echo.
                old_console_mode = _enter_console_raw_input()
                input_stop_event = threading.Event()
                input_thread = threading.Thread(
                    target=_forward_windows_console_input,
                    args=(sock, logger, input_stop_event),
                    daemon=True,
                )
                input_thread.start()

            def _emit(data: bytes) -> None:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                output_tail.extend(data)
                if len(output_tail) > ATTACH_TAIL_LIMIT:
                    del output_tail[:-ATTACH_TAIL_LIMIT]

            if not _is_selectable(sock):
                # Windows named pipe: no select(). Only reachable on Windows,
                # where console input is forwarded by its own thread above.
                interactive_reader = reader = _ThreadedReader(sock, 8192)
                while True:
                    data = reader.get(_PIPE_POLL_SECONDS)
                    if data is None:
                        continue
                    if not data:
                        break
                    _emit(data)
                return bytes(output_tail)

            while True:
                readers: list[Any] = [sock]
                if stdin_fd is not None:
                    readers.append(sys.stdin)

                ready, _, _ = select.select(readers, [], [])

                if sock in ready:
                    data = sock.recv(8192)
                    if not data:
                        break
                    _emit(data)

                if stdin_fd is not None and sys.stdin in ready:
                    user_data = os.read(stdin_fd, 1024)
                    if not user_data:
                        continue
                    if logger is not None:
                        logger.log_input(user_data)
                    sock.sendall(user_data)
        finally:
            if interactive_reader is not None:
                interactive_reader.close()
            try:
                sock_wrapper.close()
            except Exception:
                pass
            if input_stop_event is not None:
                input_stop_event.set()
            if input_thread is not None:
                input_thread.join(timeout=0.2)
            if old_console_mode is not None:
                _leave_console_raw_input(old_console_mode)
            if sigwinch is not None and old_winch_handler is not None:
                signal.signal(sigwinch, old_winch_handler)
            if stdin_fd is not None and old_tty is not None and termios is not None:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_tty)
        return bytes(output_tail)

    def attach_stdio(
        self,
        container: Any,
        logger: Any = None,
        on_attached: Any = None,
        auto_remove: bool = False,
        initial_stdin: bytes = b"",
    ) -> int:
        """Attach local stdin/stdout to a container without a TTY (ACP mode).

        Unlike ``attach_interactive`` this performs no raw-mode switching, no
        resize handling and no ``logs`` replay; instead it demultiplexes the
        Docker stream frame protocol so container stdout goes to our stdout
        and stderr to our stderr, keeping the JSON-RPC stream clean.
        Returns the container's exit code, also when the container was
        stopped by the SIGINT/SIGTERM handling. ``auto_remove`` must match the
        container's AutoRemove setting so the exit code survives its removal.
        ``initial_stdin`` is written to the container first: the client bytes
        the ACP handshake already read from our stdin (see ``core.acp``).
        """
        try:
            sock_wrapper = self.client.api.attach_socket(
                container.id,
                params={
                    "stdin": 1,
                    "stdout": 1,
                    "stderr": 1,
                    "stream": 1,
                },
            )
        except Exception as exc:  # pragma: no cover - runtime Docker behavior
            raise DockerClientError(f"Failed to attach to container: {exc}") from exc

        sock = getattr(sock_wrapper, "_sock", sock_wrapper)

        # Register the exit-code wait before the container starts. With
        # AutoRemove the container can be gone by the time the stream closes,
        # so inspecting it afterwards races the daemon; a wait opened up front
        # is answered with the status code even once the container is removed
        # (docker's own CLI waits for "removed" under --rm for the same reason).
        wait_result: dict[str, Any] = {}

        def _wait_for_exit() -> None:
            try:
                result = self.client.api.wait(
                    container.id,
                    timeout=None,
                    condition="removed" if auto_remove else "next-exit",
                )
            except Exception:
                return
            if isinstance(result, dict):
                wait_result.update(result)

        waiter = threading.Thread(target=_wait_for_exit, name="vibepod-acp-wait", daemon=True)
        waiter.start()

        if on_attached is not None:
            # Create-attach-start ordering: the ACP adapter emits its first
            # JSON-RPC frames immediately once the entrypoint runs, so the
            # caller starts the container only after the attach is in place.
            on_attached()
        if initial_stdin:
            # Sent after start so the adapter's stdin pipe exists to hold it;
            # the loop below continues from wherever the handshake stopped
            # reading our stdin.
            sock.sendall(initial_stdin)
        stdin_fd = None
        if sys.stdin is not None and not sys.stdin.closed:
            try:
                stdin_fd = sys.stdin.fileno()
            except (OSError, ValueError):
                stdin_fd = None

        stop_requested = False
        old_sigterm = None
        old_sigint = None

        def _request_stop(signum: int, frame: Any) -> None:
            del signum, frame
            nonlocal stop_requested
            stop_requested = True
            try:
                container.stop(timeout=5)
            except Exception:  # pragma: no cover - best effort
                pass

        try:
            old_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _request_stop)
            old_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _request_stop)
        except ValueError:  # pragma: no cover - not in main thread (tests)
            old_sigterm = old_sigint = None

        from vibepod.utils.console import warning

        buffer = b""

        def _handle_output(data: bytes) -> None:
            nonlocal buffer
            buffer += data
            # Docker (no TTY) frames each stream chunk with an 8-byte header:
            # stream byte, 3 padding bytes, 4-byte big-endian payload length.
            # Frames can split across recv() bounds, so buffer until header
            # and payload are complete.
            while len(buffer) >= 8:
                stream_type = buffer[0]
                length = int.from_bytes(buffer[4:8], "big")
                if len(buffer) < 8 + length:
                    break
                payload = buffer[8 : 8 + length]
                buffer = buffer[8 + length :]
                if stream_type == 1:
                    sys.stdout.buffer.write(payload)
                    sys.stdout.buffer.flush()
                elif stream_type == 2:
                    sys.stderr.buffer.write(payload)
                    sys.stderr.buffer.flush()
                if logger is not None:
                    logger.log_output(payload)

        def _warn_no_output() -> None:
            warning(
                f"ACP: no output from the adapter after {_ACP_FIRST_OUTPUT_WARN_SECONDS}s; "
                "the editor is still waiting on initialize. For claude and codex the "
                "adapter is fetched with npx on every launch, so check "
                f"`docker logs {getattr(container, 'name', container.id)}`.",
            )

        # Until the adapter says anything the editor sits on a pending
        # initialize with no visible reason; after a while, name the usual
        # cause on stderr (the editor's ACP log) once.
        first_output_deadline: float | None = time.monotonic() + _ACP_FIRST_OUTPUT_WARN_SECONDS
        eof_stop = threading.Event()
        stop_failures: list[BaseException] = []
        stdio_reader: _ThreadedReader | None = None
        try:
            if not _is_selectable(sock) or (stdin_fd is not None and not _can_select_stdin()):
                # Windows: select() rejects named pipes and stdin pipes alike
                # (it takes only Winsock sockets), so on npipe:// -- and with
                # stdin to forward, on tcp:// or ssh:// too -- both directions
                # get a thread.
                stdio_reader = reader = _ThreadedReader(sock, 65536)
                if stdin_fd is not None:
                    threading.Thread(
                        target=_forward_stdin,
                        args=(stdin_fd, sock, container, eof_stop, stop_failures),
                        name="vibepod-acp-stdin",
                        daemon=True,
                    ).start()
                while not stop_requested and not stop_failures:
                    wait = _PIPE_POLL_SECONDS
                    if first_output_deadline is not None:
                        wait = min(wait, max(0.0, first_output_deadline - time.monotonic()))
                    data = reader.get(wait)
                    if data is None:
                        if (
                            first_output_deadline is not None
                            and time.monotonic() >= first_output_deadline
                        ):
                            first_output_deadline = None
                            _warn_no_output()
                        continue
                    if not data:
                        break
                    first_output_deadline = None
                    _handle_output(data)
            else:
                while True:
                    if stop_requested:
                        break
                    readers: list[Any] = [sock]
                    if stdin_fd is not None:
                        readers.append(sys.stdin)
                    timeout: float | None = None
                    if first_output_deadline is not None:
                        timeout = max(0.0, first_output_deadline - time.monotonic())
                    ready, _, _ = select.select(readers, [], [], timeout)
                    if not ready:
                        if first_output_deadline is None:
                            continue
                        first_output_deadline = None
                        _warn_no_output()
                        continue

                    if sock in ready:
                        data = sock.recv(65536)
                        if not data:
                            break
                        first_output_deadline = None
                        _handle_output(data)

                    if stdin_fd is not None and sys.stdin in ready:
                        try:
                            user_data = os.read(stdin_fd, 65536)
                        except OSError:
                            user_data = b""
                        if not user_data:
                            # stdin EOF (ACP client went away): close our write
                            # side so the adapter sees EOF and exits, but keep
                            # draining the container stream until it ends.
                            try:
                                sock.shutdown(socket.SHUT_WR)
                            except OSError:  # pragma: no cover - already closed
                                pass
                            stdin_fd = None
                        else:
                            try:
                                sock.sendall(user_data)
                            except OSError:
                                stdin_fd = None
        finally:
            if stdio_reader is not None:
                stdio_reader.close()
            try:
                sock_wrapper.close()
            except Exception:  # pragma: no cover - best effort
                pass
            if old_sigterm is not None:
                signal.signal(signal.SIGTERM, old_sigterm)
            if old_sigint is not None:
                signal.signal(signal.SIGINT, old_sigint)

        if stop_failures:
            # The ACP client is gone but the adapter never got its EOF stand-in;
            # the container would keep running, so say which one and fail.
            name = getattr(container, "name", container.id)
            warning(
                f"ACP: the client closed stdin but stopping {name} failed: "
                f"{stop_failures[0]}; stop it with `vp stop {name}`.",
            )
            return 1
        waiter.join(timeout=10)
        status = wait_result.get("StatusCode")
        if status is None:
            # No wait answer (daemon without wait conditions, or the stream
            # ended without an exit): fall back to an inspect, which only
            # works while the container still exists.
            try:
                container.reload()
                status = container.attrs.get("State", {}).get("ExitCode", 0)
            except Exception:  # pragma: no cover - auto-removed containers
                status = 0
        if eof_stop.is_set() and not stop_requested and status in _STOP_EXIT_CODES:
            # We stopped the container because the ACP client closed stdin
            # over a named pipe, the stand-in for the EOF a socket would have
            # delivered; on POSIX the adapter exits 0 there, so report that.
            status = 0
        return int(status or 0)

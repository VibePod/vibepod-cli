"""Attach command tests."""

from __future__ import annotations

import socket
import threading
import types

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

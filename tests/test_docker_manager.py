"""Docker manager lifecycle tests."""

from __future__ import annotations

import weakref

import pytest

from vibepod.core import docker as docker_mod
from vibepod.core.docker import DockerClientError, DockerManager


class _FakeClient:
    def __init__(self, *, fail_ping: bool = False, fail_close: bool = False) -> None:
        self.close_calls = 0
        self.fail_close = fail_close
        self.fail_ping = fail_ping

    def ping(self) -> None:
        if self.fail_ping:
            raise RuntimeError("docker unavailable")

    def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise ValueError("I/O operation on closed file.")


def test_close_closes_docker_client_once() -> None:
    client = _FakeClient()
    manager = object.__new__(DockerManager)
    manager.client = client
    manager._client_finalizer = weakref.finalize(manager, client.close)  # type: ignore[attr-defined]

    manager.close()
    manager.close()

    assert client.close_calls == 1


def test_init_closes_client_when_ping_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(fail_ping=True)

    class _FakeDockerModule:
        def from_env(self) -> _FakeClient:
            return client

    monkeypatch.setattr(docker_mod, "docker", _FakeDockerModule())
    monkeypatch.setattr(docker_mod, "DockerException", RuntimeError)

    with pytest.raises(DockerClientError):
        DockerManager()

    assert client.close_calls == 1


def test_close_suppresses_docker_client_cleanup_errors() -> None:
    client = _FakeClient(fail_close=True)
    manager = object.__new__(DockerManager)
    manager.client = client
    manager._client_finalizer = weakref.finalize(manager, client.close)  # type: ignore[attr-defined]

    manager.close()

    assert client.close_calls == 1


def test_consume_docker_stream_closes_generator_response() -> None:
    class _Response:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    response = _Response()

    def _stream(response: _Response = response):
        yield "layer 1"
        yield "layer 2"

    stream = _stream()

    docker_mod._consume_docker_stream(stream)

    assert response.close_calls == 1
    assert list(stream) == []


def test_pull_image_consumes_and_closes_low_level_stream() -> None:
    class _Response:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    response = _Response()

    def _stream(response: _Response = response):
        yield {"status": "Pulling"}
        yield {"status": "Done"}

    stream = _stream()

    class _API:
        def __init__(self) -> None:
            self.pull_kwargs: dict[str, object] = {}

        def pull(self, image: str, **kwargs: object):
            self.pull_kwargs = {"image": image, **kwargs}
            return stream

    class _Client:
        def __init__(self) -> None:
            self.api = _API()

    manager = object.__new__(DockerManager)
    manager.client = _Client()

    manager.pull_image("vibepod/copilot:latest")

    assert manager.client.api.pull_kwargs == {
        "image": "vibepod/copilot:latest",
        "stream": True,
    }
    assert response.close_calls == 1


def test_attach_socket_with_response_retains_streaming_response() -> None:
    class _FakeAPI:
        def __init__(self) -> None:
            self._general_configs = {"detachKeys": "ctrl-p,ctrl-q"}
            self.response = object()
            self.socket = object()
            self.post_kwargs: dict[str, object] = {}
            self.attach_params: dict[str, object] = {}

        def _url(self, pathfmt: str, container_id: str) -> str:
            return pathfmt.format(container_id)

        def _attach_params(self, params: dict[str, object]) -> dict[str, object]:
            self.attach_params = params
            return params

        def post(self, url: str, data: object, **kwargs: object) -> object:
            self.post_kwargs = {"url": url, "data": data, **kwargs}
            return self.response

        def _get_raw_response_socket(self, response: object) -> object:
            assert response is self.response
            return self.socket

    api = _FakeAPI()

    sock, response = docker_mod._attach_socket_with_response(
        api,
        "abc123",
        {"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1, "logs": 1},
    )

    assert sock is api.socket
    assert response is api.response
    assert api.attach_params["detachKeys"] == "ctrl-p,ctrl-q"
    assert api.post_kwargs["url"] == "/containers/abc123/attach"
    assert api.post_kwargs["data"] is None
    assert api.post_kwargs["stream"] is True
    assert api.post_kwargs["headers"] == {"Connection": "Upgrade", "Upgrade": "tcp"}


def test_close_attach_resources_closes_response_before_socket() -> None:
    calls: list[str] = []

    class _Response:
        def close(self) -> None:
            calls.append("response")

    class _Socket:
        def close(self) -> None:
            calls.append("socket")

    docker_mod._close_attach_resources(_Socket(), _Response())

    assert calls == ["response", "socket"]


def test_close_attach_resources_suppresses_closed_file_response_errors() -> None:
    class _Raw:
        def __init__(self) -> None:
            self._fp = object()

    class _Response:
        def __init__(self) -> None:
            self.raw = _Raw()

        def close(self) -> None:
            raise ValueError("I/O operation on closed file.")

    class _Socket:
        closed = False

        def close(self) -> None:
            self.closed = True

    response = _Response()
    socket = _Socket()

    docker_mod._close_attach_resources(socket, response)

    assert response.raw._fp is None
    assert socket.closed is True

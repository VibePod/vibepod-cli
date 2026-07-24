"""Tests for Docker manager image pulling and parsing helper functions."""

from __future__ import annotations

import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibepod.core.docker import (
    APIError,
    DockerClientError,
    DockerException,
    DockerManager,
    NotFound,
    _discover_podman_socket,
    _parse_image_name,
)

requires_af_unix = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX sockets are not available on this platform"
)


@pytest.fixture()
def socket_dir() -> Iterator[Path]:
    """Short-path directory for binding AF_UNIX sockets.

    sun_path is limited to ~104 chars on macOS and pytest's tmp_path on CI
    runners can exceed it, so keep bound sockets out of tmp_path.
    """
    with tempfile.TemporaryDirectory(prefix="vp-sock-") as tmp:
        yield Path(tmp)


def _bind_unix_socket(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))
    return sock


def test_parse_image_name() -> None:
    assert _parse_image_name("vibepod/datasette:latest") == ("vibepod/datasette", "latest")
    assert _parse_image_name("vibepod/datasette@sha256:abcd") == (
        "vibepod/datasette",
        "sha256:abcd",
    )
    assert _parse_image_name("localhost:5000/vibepod/datasette:latest") == (
        "localhost:5000/vibepod/datasette",
        "latest",
    )
    assert _parse_image_name("localhost:5000/vibepod/datasette") == (
        "localhost:5000/vibepod/datasette",
        None,
    )
    assert _parse_image_name("ubuntu") == ("ubuntu", None)


@patch("vibepod.core.docker.docker")
def test_pull_image_success(mock_docker) -> None:
    # Set up mocks for DockerManager initialization
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    # Define mock response from api.pull stream
    mock_client.api.pull.return_value = [
        {"status": "Pulling from library/ubuntu", "id": "latest"},
        {"status": "Pulling fs layer", "id": "layer1"},
        {"status": "Downloading", "id": "layer1", "progressDetail": {"current": 50, "total": 100}},
        {"status": "Download complete", "id": "layer1"},
        {"status": "Extracting", "id": "layer1", "progressDetail": {"current": 100, "total": 100}},
        {"status": "Pull complete", "id": "layer1"},
        {"status": "Already exists", "id": "layer2"},
    ]

    manager = DockerManager()
    manager.pull_image("vibepod/datasette:latest")

    mock_client.api.pull.assert_called_once_with(
        "vibepod/datasette", tag="latest", stream=True, decode=True
    )


@patch("vibepod.core.docker.docker")
def test_pull_image_api_error(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    # api.pull raises APIError
    mock_client.api.pull.side_effect = APIError("Failed")

    manager = DockerManager()
    with pytest.raises(DockerClientError) as exc_info:
        manager.pull_image("vibepod/datasette:latest")
    assert "Failed to pull image" in str(exc_info.value)


@patch("vibepod.core.docker.docker")
def test_pull_image_chunk_error(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    # api.pull yields an error chunk
    mock_client.api.pull.return_value = [
        {"status": "Pulling fs layer", "id": "layer1"},
        {"error": "Registry returned 404"},
    ]

    manager = DockerManager()
    with pytest.raises(DockerClientError) as exc_info:
        manager.pull_image("vibepod/datasette:latest")
    assert "Registry returned 404" in str(exc_info.value)


@patch("vibepod.core.docker.docker")
def test_pull_if_newer(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    # Scenario 1: Image exists and gets updated (old_id != new_id)
    mock_image_old = MagicMock()
    mock_image_old.id = "sha256:old"
    mock_image_new = MagicMock()
    mock_image_new.id = "sha256:new"

    mock_client.images.get.side_effect = [mock_image_old, mock_image_new]
    mock_client.api.pull.return_value = [{"status": "Image is up to date"}]

    manager = DockerManager()
    updated = manager.pull_if_newer("vibepod/datasette:latest")
    assert updated is True

    # Scenario 2: Image is already up to date (old_id == new_id)
    mock_client.images.get.side_effect = [mock_image_old, mock_image_old]
    updated = manager.pull_if_newer("vibepod/datasette:latest")
    assert updated is False

    # Scenario 3: Image not found locally before, but pulled successfully
    mock_client.images.get.side_effect = [NotFound("not found"), mock_image_new]
    updated = manager.pull_if_newer("vibepod/datasette:latest")
    assert updated is True

    # Scenario 4: Pull fails
    mock_client.images.get.side_effect = [NotFound("not found")]
    mock_client.api.pull.side_effect = APIError("Failed")
    updated = manager.pull_if_newer("vibepod/datasette:latest")
    assert updated is False


@patch("vibepod.core.docker.docker")
def test_ensure_datasette_pulls_image_when_missing(mock_docker, tmp_path: Path) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.containers.list.return_value = []
    mock_client.images.get.side_effect = NotFound("Image not found")
    mock_client.api.pull.return_value = [{"status": "Pulling"}]

    manager = DockerManager()
    manager.ensure_datasette(
        image="vibepod/datasette:latest",
        logs_db_path=tmp_path / "logs.db",
        proxy_db_path=tmp_path / "proxy.db",
        port=8001,
    )

    mock_client.api.pull.assert_called_once_with(
        "vibepod/datasette", tag="latest", stream=True, decode=True
    )
    mock_client.containers.run.assert_called_once()


@patch("vibepod.core.docker.docker")
def test_ensure_proxy_pulls_image_when_missing(mock_docker, tmp_path: Path) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.containers.list.return_value = []
    mock_client.images.get.side_effect = NotFound("Image not found")
    mock_client.api.pull.return_value = [{"status": "Pulling"}]

    manager = DockerManager()
    manager.is_rootless_podman = MagicMock(return_value=False)

    manager.ensure_proxy(
        image="vibepod/proxy:latest",
        db_path=tmp_path / "proxy.db",
        ca_dir=tmp_path / "ca",
        network="vibepod-network",
    )

    mock_client.api.pull.assert_called_once_with(
        "vibepod/proxy", tag="latest", stream=True, decode=True
    )
    mock_client.containers.run.assert_called_once()


def test_discover_podman_socket_skipped_when_docker_host_set(monkeypatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    assert _discover_podman_socket() is None


@requires_af_unix
def test_discover_podman_socket_uses_machine_inspect(monkeypatch, socket_dir: Path) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    socket_path = socket_dir / "podman.sock"
    sock = _bind_unix_socket(socket_path)
    try:
        with (
            patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
            patch("vibepod.core.docker.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{socket_path}\n")
            assert _discover_podman_socket() == f"unix://{socket_path}"
    finally:
        sock.close()


@requires_af_unix
def test_discover_podman_socket_strips_unix_prefix(monkeypatch, socket_dir: Path) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    socket_path = socket_dir / "podman.sock"
    sock = _bind_unix_socket(socket_path)
    try:
        with (
            patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
            patch("vibepod.core.docker.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=f"unix://{socket_path}\n")
            assert _discover_podman_socket() == f"unix://{socket_path}"
    finally:
        sock.close()


def test_discover_podman_socket_ignores_stale_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with (
        patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
        patch("vibepod.core.docker.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=f"{tmp_path}/gone.sock\n")
        assert _discover_podman_socket() is None


@requires_af_unix
def test_discover_podman_socket_xdg_runtime_dir(monkeypatch, socket_dir: Path) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(socket_dir))
    (socket_dir / "podman").mkdir()
    socket_path = socket_dir / "podman" / "podman.sock"
    sock = _bind_unix_socket(socket_path)
    try:
        with patch("vibepod.core.docker.shutil.which", return_value=None):
            assert _discover_podman_socket() == f"unix://{socket_path}"
    finally:
        sock.close()


def test_discover_podman_socket_none_without_podman(monkeypatch) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with patch("vibepod.core.docker.shutil.which", return_value=None):
        assert _discover_podman_socket() is None


@requires_af_unix
def test_discover_podman_socket_falls_back_to_podman_info(monkeypatch, socket_dir: Path) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    socket_path = socket_dir / "podman.sock"
    sock = _bind_unix_socket(socket_path)
    try:
        with (
            patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
            patch("vibepod.core.docker.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=125, stdout=""),
                MagicMock(returncode=0, stdout=f"{socket_path}\n"),
            ]
            assert _discover_podman_socket() == f"unix://{socket_path}"
        assert mock_run.call_count == 2
    finally:
        sock.close()


@requires_af_unix
def test_discover_podman_socket_survives_subprocess_errors(monkeypatch, socket_dir: Path) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(socket_dir))
    (socket_dir / "podman").mkdir()
    socket_path = socket_dir / "podman" / "podman.sock"
    sock = _bind_unix_socket(socket_path)
    try:
        with (
            patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
            patch("vibepod.core.docker.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                subprocess.TimeoutExpired(cmd="podman", timeout=10),
                OSError("podman exploded"),
            ]
            assert _discover_podman_socket() == f"unix://{socket_path}"
    finally:
        sock.close()


@requires_af_unix
def test_discover_podman_socket_prefers_podman_over_xdg(monkeypatch, socket_dir: Path) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(socket_dir))
    (socket_dir / "podman").mkdir()
    xdg_socket = socket_dir / "podman" / "podman.sock"
    machine_socket = socket_dir / "machine.sock"
    sock_machine = _bind_unix_socket(machine_socket)
    sock_xdg = _bind_unix_socket(xdg_socket)
    try:
        with (
            patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
            patch("vibepod.core.docker.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{machine_socket}\n")
            assert _discover_podman_socket() == f"unix://{machine_socket}"
    finally:
        sock_machine.close()
        sock_xdg.close()


@patch("vibepod.core.docker.docker")
def test_init_falls_back_to_podman_socket(mock_docker) -> None:
    mock_docker.from_env.side_effect = DockerException("no socket")
    mock_client = MagicMock()
    mock_docker.DockerClient.return_value = mock_client

    with patch(
        "vibepod.core.docker._discover_podman_socket",
        return_value="unix:///tmp/podman.sock",
    ):
        manager = DockerManager()

    mock_docker.DockerClient.assert_called_once_with(base_url="unix:///tmp/podman.sock")
    assert manager.client is mock_client
    mock_client.ping.assert_called_once()


@patch("vibepod.core.docker.docker")
def test_init_error_includes_podman_hint(mock_docker) -> None:
    mock_docker.from_env.side_effect = DockerException("no socket")

    with patch("vibepod.core.docker._discover_podman_socket", return_value=None):
        with pytest.raises(DockerClientError) as exc_info:
            DockerManager()

    message = str(exc_info.value)
    assert "podman machine start" in message
    assert "DOCKER_HOST" in message


@patch("vibepod.core.docker.docker")
def test_init_fallback_failure_raises(mock_docker) -> None:
    mock_docker.from_env.side_effect = DockerException("no socket")
    mock_docker.DockerClient.side_effect = DockerException("still broken")

    with patch(
        "vibepod.core.docker._discover_podman_socket",
        return_value="unix:///tmp/podman.sock",
    ):
        with pytest.raises(DockerClientError) as exc_info:
            DockerManager()

    assert "Docker is not available" in str(exc_info.value)

"""Tests for Docker manager image pulling and parsing helper functions."""

from __future__ import annotations

import io
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


def _fake_image(image_id: str, tags: list[str], digests: list[str]):
    """Build a stand-in for a docker-py Image with the attributes the sweep reads."""
    image = MagicMock()
    image.id = image_id
    image.tags = tags
    image.attrs = {"RepoDigests": digests}
    return image


@patch("vibepod.core.docker.docker")
def test_clean_untagged_images_removes_untagged_namespace_images(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.images.list.return_value = [
        _fake_image("sha256:current", ["vibepod/claude:latest"], ["vibepod/claude@sha256:aaa"]),
        _fake_image("sha256:old", [], ["vibepod/claude@sha256:bbb"]),
        _fake_image("sha256:older", [], ["vibepod/codex@sha256:ccc"]),
    ]

    manager = DockerManager()
    removed = manager.clean_untagged_images()

    assert removed == 2
    assert [call.args[0] for call in mock_client.images.remove.call_args_list] == [
        "sha256:old",
        "sha256:older",
    ]


@patch("vibepod.core.docker.docker")
def test_clean_untagged_images_keeps_foreign_and_unattributable_images(mock_docker) -> None:
    """Only images provably in the namespace are touched; anonymous layers stay."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.images.list.return_value = [
        _fake_image("sha256:other", [], ["python@sha256:aaa"]),
        _fake_image("sha256:anonymous", [], []),
        _fake_image("sha256:library", [], ["docker.io/library/python@sha256:bbb"]),
    ]

    manager = DockerManager()
    removed = manager.clean_untagged_images()

    assert removed == 0
    mock_client.images.remove.assert_not_called()


@patch("vibepod.core.docker.docker")
def test_clean_untagged_images_matches_registry_qualified_references(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.images.list.return_value = [
        _fake_image("sha256:old", [], ["ghcr.io/vibepod/claude@sha256:aaa"]),
    ]

    manager = DockerManager()

    assert manager.clean_untagged_images() == 1
    mock_client.images.remove.assert_called_once_with("sha256:old")


@patch("vibepod.core.docker.docker")
def test_clean_untagged_images_keeps_foreign_nested_references(mock_docker) -> None:
    """A third-party repository that merely contains a `vibepod` path segment is not ours."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.images.list.return_value = [
        _fake_image("sha256:foreign", [], ["ghcr.io/acme/vibepod/tool@sha256:aaa"]),
    ]

    manager = DockerManager()

    assert manager.clean_untagged_images() == 0
    mock_client.images.remove.assert_not_called()


@patch("vibepod.core.docker.docker")
def test_clean_untagged_images_skips_images_still_in_use(mock_docker) -> None:
    """A container holding one image must not stop the rest of the sweep."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.images.list.return_value = [
        _fake_image("sha256:in-use", [], ["vibepod/claude@sha256:aaa"]),
        _fake_image("sha256:free", [], ["vibepod/claude@sha256:bbb"]),
    ]
    mock_client.images.remove.side_effect = [
        APIError("conflict: image is being used by stopped container"),
        None,
    ]

    manager = DockerManager()
    removed = manager.clean_untagged_images()

    assert removed == 1
    assert [call.args[0] for call in mock_client.images.remove.call_args_list] == [
        "sha256:in-use",
        "sha256:free",
    ]


@patch("vibepod.core.docker.docker")
def test_clean_untagged_images_survives_list_failure(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.images.list.side_effect = DockerException("connection refused")

    manager = DockerManager()

    assert manager.clean_untagged_images() == 0


@patch("vibepod.core.docker.docker")
def test_pull_image_cleans_untagged_images(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.api.pull.return_value = [{"status": "Downloaded newer image"}]
    mock_client.images.list.return_value = [
        _fake_image("sha256:old", [], ["vibepod/claude@sha256:aaa"]),
    ]

    manager = DockerManager()
    manager.pull_image("vibepod/claude:latest", auto_clean=True)

    mock_client.images.remove.assert_called_once_with("sha256:old")


@patch("vibepod.core.docker.docker")
def test_pull_image_keeps_untagged_images_by_default(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.api.pull.return_value = [{"status": "Downloaded newer image"}]

    manager = DockerManager()
    manager.pull_image("vibepod/claude:latest")

    mock_client.images.list.assert_not_called()
    mock_client.images.remove.assert_not_called()


@patch("vibepod.core.docker.docker")
def test_pull_if_newer_cleans_untagged_images_even_when_up_to_date(mock_docker) -> None:
    """The sweep also clears leftovers from earlier pulls whose removal failed."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_image = MagicMock()
    mock_image.id = "sha256:same"
    mock_client.images.get.side_effect = [mock_image, mock_image]
    mock_client.api.pull.return_value = [{"status": "Image is up to date"}]
    mock_client.images.list.return_value = [
        _fake_image("sha256:leftover", [], ["vibepod/claude@sha256:aaa"]),
    ]

    manager = DockerManager()
    updated = manager.pull_if_newer("vibepod/claude:latest", auto_clean=True)

    assert updated is False
    mock_client.images.remove.assert_called_once_with("sha256:leftover")


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
def test_pull_if_newer_keeps_untagged_images_by_default(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_image_old = MagicMock()
    mock_image_old.id = "sha256:old"
    mock_image_new = MagicMock()
    mock_image_new.id = "sha256:new"

    mock_client.images.get.side_effect = [mock_image_old, mock_image_new]
    mock_client.api.pull.return_value = [{"status": "Downloaded newer image"}]

    manager = DockerManager()
    updated = manager.pull_if_newer("vibepod/claude:latest")

    assert updated is True
    mock_client.images.list.assert_not_called()
    mock_client.images.remove.assert_not_called()


@patch("vibepod.core.docker.docker")
def test_image_id_returns_none_on_daemon_error(mock_docker) -> None:
    """A daemon hiccup while reading an image id is reported as 'no image'."""
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_client.images.get.side_effect = APIError("daemon hiccup")

    manager = DockerManager()

    assert manager.image_id("vibepod/claude:latest") is None


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
def test_discover_podman_socket_enumerates_non_default_machines(
    monkeypatch, socket_dir: Path
) -> None:
    """A running non-default machine is found even when the default one is dead."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    live_path = socket_dir / "dev.sock"
    sock = _bind_unix_socket(live_path)
    stale_path = socket_dir / "gone.sock"
    try:
        with (
            patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
            patch("vibepod.core.docker.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                # `machine list` marks the default machine with a trailing "*".
                MagicMock(returncode=0, stdout="podman-machine-default*\ndev\n"),
                MagicMock(returncode=0, stdout=f"{stale_path}\n{live_path}\n"),
            ]
            assert _discover_podman_socket() == f"unix://{live_path}"
        listed = mock_run.call_args_list[1].args[0]
        assert listed[1:4] == ["machine", "inspect", "podman-machine-default"]
        assert "dev" in listed
    finally:
        sock.close()


def test_discover_podman_socket_skips_podman_info_when_machines_exist(monkeypatch) -> None:
    """`podman info` reports the VM-internal path, so never trust it alongside a machine."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with (
        patch("vibepod.core.docker.shutil.which", return_value="/usr/bin/podman"),
        patch("vibepod.core.docker.subprocess.run") as mock_run,
    ):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="podman-machine-default*\n"),
            MagicMock(returncode=0, stdout="/var/folders/xx/gone/podman.sock\n"),
        ]
        assert _discover_podman_socket() is None
    assert mock_run.call_count == 2
    assert all("info" not in call.args[0] for call in mock_run.call_args_list)


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


@patch("vibepod.core.docker.docker")
def test_build_image_streams_and_succeeds(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.api.build.return_value = iter([{"stream": "Step 1/2\n"}, {"stream": "Done\n"}])

    manager = DockerManager()
    context = io.BytesIO(b"tar")
    manager.build_image(context, tag="vibepod/overlay-claude:abc", labels={"a": "b"})

    mock_client.api.build.assert_called_once_with(
        fileobj=context,
        custom_context=True,
        tag="vibepod/overlay-claude:abc",
        labels={"a": "b"},
        rm=True,
        nocache=False,
        decode=True,
    )


@patch("vibepod.core.docker.docker")
def test_build_image_forwards_nocache(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.api.build.return_value = iter([])

    manager = DockerManager()
    manager.build_image(io.BytesIO(b"tar"), tag="t:1", labels={}, nocache=True)

    assert mock_client.api.build.call_args.kwargs["nocache"] is True


@patch("vibepod.core.docker.docker")
def test_build_image_raises_on_error_chunk(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.api.build.return_value = iter([{"stream": "Step 1/2\n"}, {"error": "boom"}])

    manager = DockerManager()
    with pytest.raises(DockerClientError, match="boom"):
        manager.build_image(io.BytesIO(b"tar"), tag="t:1", labels={})


@patch("vibepod.core.docker.docker")
def test_build_image_skips_non_dict_chunks(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.api.build.return_value = iter(["error: not a dict", {"stream": "Done\n"}])

    manager = DockerManager()
    manager.build_image(io.BytesIO(b"tar"), tag="t:1", labels={})


@patch("vibepod.core.docker.docker")
def test_build_image_wraps_api_error(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.api.build.side_effect = APIError("daemon down")

    manager = DockerManager()
    with pytest.raises(DockerClientError, match="Failed to build image"):
        manager.build_image(io.BytesIO(b"tar"), tag="t:1", labels={})


def _overlay_image(image_id: str, tags: list[str], key: str) -> MagicMock:
    image = MagicMock()
    image.id = image_id
    image.tags = tags
    image.labels = {"vibepod.overlay.key": key}
    return image


@patch("vibepod.core.docker.docker")
def test_remove_stale_overlays_removes_same_key_other_tags(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    keep = _overlay_image("sha256:keep", ["vibepod/overlay-claude:new1"], "k1")
    stale = _overlay_image("sha256:old", ["vibepod/overlay-claude:old1"], "k1")
    mock_client.images.list.return_value = [keep, stale]

    manager = DockerManager()
    removed = manager.remove_stale_overlays("k1", keep_tag="vibepod/overlay-claude:new1")

    assert removed == 1
    mock_client.images.list.assert_called_once_with(filters={"label": ["vibepod.overlay.key=k1"]})
    mock_client.images.remove.assert_called_once_with("sha256:old")


@patch("vibepod.core.docker.docker")
def test_remove_stale_overlays_survives_remove_failure(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    stale = _overlay_image("sha256:old", ["vibepod/overlay-claude:old1"], "k1")
    mock_client.images.list.return_value = [stale]
    mock_client.images.remove.side_effect = DockerException("in use")

    manager = DockerManager()
    assert manager.remove_stale_overlays("k1", keep_tag="vibepod/overlay-claude:new1") == 0


@patch("vibepod.core.docker.docker")
def test_remove_stale_overlays_survives_list_failure(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    mock_client.images.list.side_effect = DockerException("daemon down")

    manager = DockerManager()
    assert manager.remove_stale_overlays("k1", keep_tag="t:1") == 0

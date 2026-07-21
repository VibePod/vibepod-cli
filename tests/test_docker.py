"""Tests for Docker manager image pulling and parsing helper functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vibepod.core.docker import (
    APIError,
    DockerClientError,
    DockerManager,
    NotFound,
    _parse_image_name,
)


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
def test_pull_if_newer_removes_replaced_image(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_image_old = MagicMock()
    mock_image_old.id = "sha256:old"
    mock_image_new = MagicMock()
    mock_image_new.id = "sha256:new"

    mock_client.images.get.side_effect = [mock_image_old, mock_image_new]
    mock_client.api.pull.return_value = [{"status": "Image is up to date"}]

    manager = DockerManager()
    updated = manager.pull_if_newer("vibepod/claude:latest", remove_previous=True)

    assert updated is True
    mock_client.images.remove.assert_called_once_with("sha256:old")


@patch("vibepod.core.docker.docker")
def test_pull_if_newer_keeps_replaced_image_by_default(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_image_old = MagicMock()
    mock_image_old.id = "sha256:old"
    mock_image_new = MagicMock()
    mock_image_new.id = "sha256:new"

    mock_client.images.get.side_effect = [mock_image_old, mock_image_new]
    mock_client.api.pull.return_value = [{"status": "Image is up to date"}]

    manager = DockerManager()
    updated = manager.pull_if_newer("vibepod/claude:latest")

    assert updated is True
    mock_client.images.remove.assert_not_called()


@patch("vibepod.core.docker.docker")
def test_pull_if_newer_ignores_remove_failure(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_image_old = MagicMock()
    mock_image_old.id = "sha256:old"
    mock_image_new = MagicMock()
    mock_image_new.id = "sha256:new"

    mock_client.images.get.side_effect = [mock_image_old, mock_image_new]
    mock_client.api.pull.return_value = [{"status": "Image is up to date"}]
    mock_client.images.remove.side_effect = APIError("image is in use")

    manager = DockerManager()
    updated = manager.pull_if_newer("vibepod/claude:latest", remove_previous=True)

    assert updated is True


@patch("vibepod.core.docker.docker")
def test_pull_image_removes_replaced_image(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_image_old = MagicMock()
    mock_image_old.id = "sha256:old"
    mock_image_new = MagicMock()
    mock_image_new.id = "sha256:new"

    mock_client.images.get.side_effect = [mock_image_old, mock_image_new]
    mock_client.api.pull.return_value = [{"status": "Downloaded newer image"}]

    manager = DockerManager()
    manager.pull_image("vibepod/claude:latest", remove_previous=True)

    mock_client.images.remove.assert_called_once_with("sha256:old")


@patch("vibepod.core.docker.docker")
def test_pull_image_no_remove_without_previous_image(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    mock_image_new = MagicMock()
    mock_image_new.id = "sha256:new"

    mock_client.images.get.side_effect = [NotFound("not found"), mock_image_new]
    mock_client.api.pull.return_value = [{"status": "Downloaded newer image"}]

    manager = DockerManager()
    manager.pull_image("vibepod/claude:latest", remove_previous=True)

    mock_client.images.remove.assert_not_called()


@patch("vibepod.core.docker.docker")
def test_remove_replaced_image_requires_new_id(mock_docker) -> None:
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client

    manager = DockerManager()
    manager.remove_replaced_image("sha256:old", None)

    mock_client.images.remove.assert_not_called()


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

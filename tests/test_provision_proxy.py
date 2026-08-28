"""Tests for launch.provision_proxy: safe image refresh + proxy ensure."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vibepod.core.docker import DockerClientError
from vibepod.core.launch import provision_proxy


class _FakeManager:
    def __init__(self, *, pull_updates: bool = False, bad_new_image: bool = False) -> None:
        self._pull_updates = pull_updates
        self._bad_new_image = bad_new_image
        self.pull_auto_clean: bool | None = None
        self.ensured = False
        self.swept = False
        self.schema_checks: list[tuple[str, str]] = []
        self.running = MagicMock()

    def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
        self.pull_auto_clean = auto_clean
        return self._pull_updates

    def require_proxy_policy_schema(self, image: str, required: str) -> None:
        self.schema_checks.append((image, required))
        if self._bad_new_image:
            raise DockerClientError("incompatible new image")

    def find_proxy(self) -> object:
        return self.running

    def remove_proxy(self, existing: object, timeout: float = 15.0) -> None:
        existing.remove(force=True)  # type: ignore[attr-defined]

    def ensure_proxy(self, **kwargs: object) -> str:
        self.ensured = True
        return "proxy-container"

    def clean_untagged_images(self) -> None:
        self.swept = True


def _provision(manager: _FakeManager, *, auto_clean: bool = True) -> object:
    return provision_proxy(
        manager,
        image="vibepod/proxy:latest",
        db_path=Path("/tmp/proxy.db"),
        ca_dir=Path("/tmp/ca"),
        network="vibepod-network",
        auto_clean=auto_clean,
    )


def test_bad_new_image_does_not_tear_down_running_proxy() -> None:
    manager = _FakeManager(pull_updates=True, bad_new_image=True)
    with pytest.raises(DockerClientError):
        _provision(manager)
    manager.running.remove.assert_not_called()
    assert manager.ensured is False


def test_pull_does_not_sweep_before_old_proxy_is_removed() -> None:
    manager = _FakeManager(pull_updates=True)
    _provision(manager)
    assert manager.pull_auto_clean is False
    manager.running.remove.assert_called_once()
    assert manager.ensured is True
    assert manager.swept is True


def test_no_auto_clean_skips_sweep() -> None:
    manager = _FakeManager(pull_updates=True)
    _provision(manager, auto_clean=False)
    assert manager.swept is False
    assert manager.ensured is True

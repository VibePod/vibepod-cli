"""Logs command tests."""

from __future__ import annotations

from vibepod.commands import logs as logs_cmd


class _FakeContainer:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def remove(self, force: bool = False) -> None:
        self._events.append("container.remove")


class _FakeManager:
    def __init__(self, events: list[str], updated: bool = True) -> None:
        self._events = events
        self._updated = updated
        self._container = _FakeContainer(events)
        self._image_ids = iter(["old-id", "new-id"])

    def image_id(self, image: str) -> str | None:
        return next(self._image_ids)

    def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
        self._events.append(f"pull_if_newer(auto_clean={auto_clean})")
        return self._updated

    def find_datasette(self):
        return self._container

    def clean_untagged_images(self) -> int:
        self._events.append("clean_untagged_images")
        return 0

    def ensure_datasette(self, **kwargs) -> None:
        self._events.append("ensure_datasette")


def _patch_common(monkeypatch, events: list[str], config: dict, updated: bool = True) -> None:
    monkeypatch.setattr(logs_cmd, "DockerManager", lambda: _FakeManager(events, updated))
    monkeypatch.setattr(logs_cmd, "get_config", lambda: config)
    monkeypatch.setattr(logs_cmd, "_wait_for_datasette", lambda port: True)


def test_logs_start_cleans_images_after_container(monkeypatch) -> None:
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": True})

    logs_cmd.logs_start(port=None, no_open=True)

    assert events == [
        "pull_if_newer(auto_clean=False)",
        "container.remove",
        "clean_untagged_images",
        "ensure_datasette",
    ]


def test_logs_start_skips_image_cleanup_without_auto_clean(monkeypatch) -> None:
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": False})

    logs_cmd.logs_start(port=None, no_open=True)

    assert events == [
        "pull_if_newer(auto_clean=False)",
        "container.remove",
        "ensure_datasette",
    ]


def test_logs_start_cleans_leftovers_without_update(monkeypatch) -> None:
    """Leftovers from an earlier failed removal are swept even when nothing changed."""
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": True}, updated=False)

    logs_cmd.logs_start(port=None, no_open=True)

    assert events == [
        "pull_if_newer(auto_clean=False)",
        "clean_untagged_images",
        "ensure_datasette",
    ]

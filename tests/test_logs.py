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

    def pull_if_newer(self, image: str, remove_previous: bool = False) -> bool:
        self._events.append(f"pull_if_newer(remove_previous={remove_previous})")
        return self._updated

    def find_datasette(self):
        return self._container

    def remove_replaced_image(self, old_id: str | None, new_id: str | None) -> None:
        self._events.append(f"remove_replaced_image({old_id}, {new_id})")

    def ensure_datasette(self, **kwargs) -> None:
        self._events.append("ensure_datasette")


def _patch_common(monkeypatch, events: list[str], config: dict, updated: bool = True) -> None:
    monkeypatch.setattr(logs_cmd, "DockerManager", lambda: _FakeManager(events, updated))
    monkeypatch.setattr(logs_cmd, "get_config", lambda: config)
    monkeypatch.setattr(logs_cmd, "_wait_for_datasette", lambda port: True)


def test_logs_start_removes_old_image_after_container(monkeypatch) -> None:
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": True})

    logs_cmd.logs_start(port=None, no_open=True)

    assert events == [
        "pull_if_newer(remove_previous=False)",
        "container.remove",
        "remove_replaced_image(old-id, new-id)",
        "ensure_datasette",
    ]


def test_logs_start_skips_image_cleanup_without_auto_clean(monkeypatch) -> None:
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": False})

    logs_cmd.logs_start(port=None, no_open=True)

    assert events == [
        "pull_if_newer(remove_previous=False)",
        "container.remove",
        "ensure_datasette",
    ]


def test_logs_start_no_update_keeps_container(monkeypatch) -> None:
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": True}, updated=False)

    logs_cmd.logs_start(port=None, no_open=True)

    assert events == [
        "pull_if_newer(remove_previous=False)",
        "ensure_datasette",
    ]

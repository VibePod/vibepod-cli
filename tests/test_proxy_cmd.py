"""Proxy command tests."""

from __future__ import annotations

from vibepod.commands import proxy as proxy_cmd


class _FakeContainer:
    def __init__(self, events: list[str], status: str = "running") -> None:
        self._events = events
        self.status = status

    def remove(self, force: bool = False) -> None:
        self._events.append("container.remove")


class _FakeManager:
    def __init__(self, events: list[str], updated: bool = True) -> None:
        self._events = events
        self._updated = updated
        self._container: _FakeContainer | None = _FakeContainer(events)

    def ensure_network(self, name: str) -> None:
        self._events.append("ensure_network")

    def pull_if_newer(self, image: str, auto_clean: bool = False) -> bool:
        self._events.append(f"pull_if_newer(auto_clean={auto_clean})")
        return self._updated

    def find_proxy(self):
        return self._container

    def clean_untagged_images(self) -> int:
        self._events.append("clean_untagged_images")
        return 0

    def ensure_proxy(self, **kwargs) -> None:
        self._events.append("ensure_proxy")


def _patch_common(monkeypatch, events: list[str], config: dict, updated: bool = True) -> None:
    monkeypatch.setattr(proxy_cmd, "DockerManager", lambda: _FakeManager(events, updated))
    monkeypatch.setattr(proxy_cmd, "get_config", lambda: config)
    monkeypatch.setattr(
        proxy_cmd,
        "materialize_policy_bases",
        lambda config, profile: events.append("materialize_policy_bases"),
    )


def test_proxy_start_recreates_container_before_cleanup(monkeypatch) -> None:
    """The running proxy holds the replaced image, so it must go before the sweep."""
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": True})

    proxy_cmd.proxy_start()

    assert events == [
        "ensure_network",
        "pull_if_newer(auto_clean=False)",
        "container.remove",
        "materialize_policy_bases",
        "ensure_proxy",
        "clean_untagged_images",
    ]


def test_proxy_start_keeps_container_without_update(monkeypatch) -> None:
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": True}, updated=False)

    proxy_cmd.proxy_start()

    assert events == [
        "ensure_network",
        "pull_if_newer(auto_clean=False)",
        "materialize_policy_bases",
        "ensure_proxy",
        "clean_untagged_images",
    ]


def test_proxy_start_skips_cleanup_without_auto_clean(monkeypatch) -> None:
    events: list[str] = []
    _patch_common(monkeypatch, events, {"auto_clean": False})

    proxy_cmd.proxy_start()

    assert events == [
        "ensure_network",
        "pull_if_newer(auto_clean=False)",
        "container.remove",
        "materialize_policy_bases",
        "ensure_proxy",
    ]

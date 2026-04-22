"""Stop command tests."""

from __future__ import annotations

import pytest
import typer

from vibepod.commands import stop as stop_cmd
from vibepod.constants import CONTAINER_LABEL_MANAGED, EXIT_DOCKER_NOT_RUNNING
from vibepod.core.docker import DockerClientError

_UNSET: dict[str, str] = {"__unset__": ""}


class _FakeContainer:
    def __init__(
        self,
        name: str,
        labels: dict[str, str] = _UNSET,
    ) -> None:
        self.name = name
        if labels is _UNSET:
            labels = {CONTAINER_LABEL_MANAGED: "true", "vibepod.agent": "claude"}
        self.labels = labels
        self.stop_timeout: int | None = None

    def stop(self, timeout: int = 10) -> None:
        self.stop_timeout = timeout


def test_stop_requires_target_or_all(monkeypatch) -> None:
    monkeypatch.setattr(stop_cmd, "DockerManager", lambda: pytest.fail("should not be called"))
    with pytest.raises(typer.BadParameter):
        stop_cmd.stop(target=None, all_containers=False, force=False)


def test_stop_docker_unavailable_exits_with_status(monkeypatch) -> None:
    def _unavailable() -> None:
        raise DockerClientError("Docker is not available")

    class _Manager:
        def __init__(self) -> None:
            _unavailable()

    monkeypatch.setattr(stop_cmd, "DockerManager", _Manager)

    with pytest.raises(typer.Exit) as exc:
        stop_cmd.stop(target="claude", all_containers=False, force=False)
    assert exc.value.exit_code == EXIT_DOCKER_NOT_RUNNING


def test_stop_by_agent_name_dispatches_to_stop_agent(monkeypatch) -> None:
    calls: dict = {}

    class _Manager:
        def stop_agent(self, agent: str, force: bool = False) -> int:
            calls["agent"] = agent
            calls["force"] = force
            return 2

        def stop_container(self, name_or_id: str, force: bool = False):  # pragma: no cover
            pytest.fail("stop_container should not be called for a known agent")

    monkeypatch.setattr(stop_cmd, "DockerManager", lambda: _Manager())

    stop_cmd.stop(target="claude", all_containers=False, force=True)

    assert calls == {"agent": "claude", "force": True}


def test_stop_by_agent_shortcut_is_resolved(monkeypatch) -> None:
    calls: dict = {}

    class _Manager:
        def stop_agent(self, agent: str, force: bool = False) -> int:
            calls["agent"] = agent
            calls["force"] = force
            return 1

    monkeypatch.setattr(stop_cmd, "DockerManager", lambda: _Manager())

    stop_cmd.stop(target="c", all_containers=False, force=False)

    assert calls == {"agent": "claude", "force": False}


def test_stop_by_container_name_dispatches_to_stop_container(monkeypatch) -> None:
    calls: dict = {}
    container = _FakeContainer("vibepod-claude-abc12345")

    class _Manager:
        def stop_agent(self, agent: str, force: bool = False) -> int:  # pragma: no cover
            pytest.fail("stop_agent should not be called for a container name")

        def stop_container(self, name_or_id: str, force: bool = False):
            calls["name_or_id"] = name_or_id
            calls["force"] = force
            return container

    monkeypatch.setattr(stop_cmd, "DockerManager", lambda: _Manager())

    stop_cmd.stop(target="vibepod-claude-abc12345", all_containers=False, force=False)

    assert calls == {"name_or_id": "vibepod-claude-abc12345", "force": False}


def test_stop_by_container_id_propagates_errors(monkeypatch) -> None:
    class _Manager:
        def stop_container(self, name_or_id: str, force: bool = False):
            raise DockerClientError(f"Container '{name_or_id}' not found")

    monkeypatch.setattr(stop_cmd, "DockerManager", lambda: _Manager())

    with pytest.raises(typer.Exit) as exc:
        stop_cmd.stop(target="bogus-id", all_containers=False, force=False)
    assert exc.value.exit_code == 1


def test_stop_all_flag_uses_stop_all(monkeypatch) -> None:
    calls: dict = {}

    class _Manager:
        def stop_all(self, force: bool = False) -> int:
            calls["force"] = force
            return 3

    monkeypatch.setattr(stop_cmd, "DockerManager", lambda: _Manager())

    stop_cmd.stop(target=None, all_containers=True, force=True)

    assert calls == {"force": True}


def test_manager_stop_container_rejects_unmanaged() -> None:
    unmanaged = _FakeContainer("random", labels={})

    class _FakeClient:
        class containers:  # noqa: N801
            @staticmethod
            def get(name_or_id: str):  # noqa: ARG004
                return unmanaged

    from vibepod.core.docker import DockerManager

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    with pytest.raises(DockerClientError):
        manager.stop_container("random")
    assert unmanaged.stop_timeout is None


def test_manager_stop_container_stops_managed() -> None:
    managed = _FakeContainer("vibepod-claude-xyz")

    class _FakeClient:
        class containers:  # noqa: N801
            @staticmethod
            def get(name_or_id: str):  # noqa: ARG004
                return managed

    from vibepod.core.docker import DockerManager

    manager = object.__new__(DockerManager)
    manager.client = _FakeClient()  # type: ignore[assignment]

    result = manager.stop_container("vibepod-claude-xyz", force=True)
    assert result is managed
    assert managed.stop_timeout == 0

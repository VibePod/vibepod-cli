"""Container runtime detection tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from rich.prompt import Prompt

from vibepod.core import runtime


def test_probe_socket_uses_default_timeout(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            recorded["base_url"] = base_url
            recorded["timeout"] = timeout

        def ping(self) -> None:
            recorded["pinged"] = True

        def close(self) -> None:
            recorded["closed"] = True

    monkeypatch.delenv("VP_RUNTIME_PROBE_TIMEOUT", raising=False)
    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(DockerClient=_FakeClient))

    assert runtime._probe_socket("unix:///tmp/podman.sock") is True
    assert recorded == {
        "base_url": "unix:///tmp/podman.sock",
        "timeout": 10.0,
        "pinged": True,
        "closed": True,
    }


def test_probe_socket_honors_env_timeout(monkeypatch) -> None:
    recorded: dict[str, float] = {}

    class _FakeClient:
        def __init__(self, *, base_url: str, timeout: float) -> None:
            recorded["timeout"] = timeout

        def ping(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setenv("VP_RUNTIME_PROBE_TIMEOUT", "12.5")
    monkeypatch.setitem(sys.modules, "docker", SimpleNamespace(DockerClient=_FakeClient))

    assert runtime._probe_socket("unix:///tmp/podman.sock") is True
    assert recorded["timeout"] == 12.5


def test_resolve_runtime_prompts_with_detected_choices(monkeypatch) -> None:
    prompted: dict[str, object] = {}

    def _ask(*args, **kwargs) -> str:
        del args
        prompted["choices"] = kwargs["choices"]
        prompted["default"] = kwargs["default"]
        return "podman"

    monkeypatch.setattr(
        runtime,
        "detect_available_runtimes",
        lambda: {
            "docker": "unix:///var/run/docker.sock",
            "podman": "unix:///run/user/1000/podman/podman.sock",
        },
    )
    monkeypatch.setattr(runtime.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(Prompt, "ask", staticmethod(_ask))
    monkeypatch.setattr(
        runtime,
        "save_runtime_preference",
        lambda choice: prompted.setdefault("saved", choice),
    )

    selected = runtime.resolve_runtime()

    assert prompted["choices"] == ["docker", "podman"]
    assert prompted["default"] == "docker"
    assert prompted["saved"] == "podman"
    assert selected == ("podman", "unix:///run/user/1000/podman/podman.sock")


def test_resolve_runtime_rejects_unavailable_prompt_choice(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime,
        "detect_available_runtimes",
        lambda: {
            "podman": "unix:///run/user/1000/podman/podman.sock",
            "alt": "unix:///tmp/alt.sock",
        },
    )
    monkeypatch.setattr(runtime.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(Prompt, "ask", staticmethod(lambda *args, **kwargs: "docker"))

    with pytest.raises(RuntimeError, match="not available"):
        runtime.resolve_runtime()

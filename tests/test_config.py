"""Configuration tests."""

from __future__ import annotations

from vibepod.core.config import deep_merge


def test_deep_merge() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}}
    override = {"nested": {"y": 999, "z": 3}, "b": 2}
    merged = deep_merge(base, override)
    assert merged == {"a": 1, "b": 2, "nested": {"x": 1, "y": 999, "z": 3}}

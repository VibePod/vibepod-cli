"""Proxy policy identity tests."""

from __future__ import annotations

import pytest

from vibepod.core.proxy_identity import (
    identified_proxy_url,
    new_policy_id,
    validate_policy_id,
)


def test_validate_policy_id_rejects_non_string() -> None:
    with pytest.raises(ValueError):
        validate_policy_id(None)
    with pytest.raises(ValueError):
        validate_policy_id(123)


def test_validate_policy_id_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        validate_policy_id("not-a-policy-id")


def test_generated_policy_id_round_trip() -> None:
    policy_id = new_policy_id()
    assert validate_policy_id(policy_id) == policy_id
    assert policy_id in identified_proxy_url(policy_id)

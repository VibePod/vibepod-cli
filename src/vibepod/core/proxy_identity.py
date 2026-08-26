"""Per-launch proxy policy identity helpers."""

from __future__ import annotations

import re
from uuid import uuid4

_POLICY_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def validate_policy_id(policy_id: str) -> str:
    """Return a valid policy identifier or raise ``ValueError``."""
    if not _POLICY_ID_RE.fullmatch(policy_id):
        raise ValueError(f"Invalid proxy policy id '{policy_id}'")
    return policy_id


def new_policy_id() -> str:
    """Generate an opaque identifier for one agent launch policy."""
    return uuid4().hex


def identified_proxy_url(policy_id: str) -> str:
    """Return the shared proxy URL carrying one launch policy identity."""
    validated = validate_policy_id(policy_id)
    return f"http://vp-{validated}:vibepod@vibepod-proxy:8080"

"""Bounded model discovery for compatible APIs, with no redirect credential leaks."""

from __future__ import annotations

import json
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from vibepod import __version__
from vibepod.core.providers import Provider, valid_model_id, validate_key

MAX_BYTES = 2 * 1024 * 1024
#: Some API gateways (Cloudflare, e.g. in front of Groq) reject urllib's default
#: "Python-urllib" User-Agent with 403 before authentication is even checked.
USER_AGENT = f"vibepod/{__version__}"
MAX_PAGES = 20


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def discover_models(
    provider: Provider,
    *,
    key: str = "",
    allow_http_key: bool = False,
) -> list[str]:
    """Read model IDs; listing does not establish model capability compatibility."""
    provider.validate()
    validate_key(key)
    if key and urlsplit(provider.base_url).scheme != "https" and not allow_http_key:
        raise ValueError("Sending an API key over HTTP requires explicit confirmation")
    base = provider.base_url.rstrip("/")
    if provider.protocol == "anthropic":
        # Anthropic clients append /v1 themselves; validation rejects a /v1 suffix.
        base += "/v1"
    endpoint = base + "/models"
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if provider.protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    opener = build_opener(_NoRedirect())
    found: set[str] = set()
    cursors: set[str] = set()
    url = endpoint
    for _ in range(MAX_PAGES):
        try:
            with opener.open(Request(url, headers=headers), timeout=10) as response:
                raw = response.read(MAX_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401:
                raise ValueError("Authentication failed while listing models") from None
            if status == 403:
                raise ValueError(
                    "Access denied while listing models (HTTP 403): the key lacks access "
                    "or the endpoint blocks this client",
                ) from None
            if status in (404, 405, 501):
                raise ValueError("Model listing unsupported; use manual model entry") from None
            if 300 <= status < 400:
                raise ValueError(
                    "Model discovery refuses redirects; configure the final URL",
                ) from None
            raise ValueError(f"Model discovery failed: HTTP {status}") from None
        except (URLError, TimeoutError, OSError):
            raise ValueError(
                "Cannot reach model endpoint; check URL, TLS, and server binding",
            ) from None
        except HTTPException:
            # BadStatusLine, IncompleteRead, LineTooLong: not an OSError subclass.
            raise ValueError("Model endpoint returned an invalid HTTP response") from None
        if len(raw) > MAX_BYTES:
            raise ValueError("Model response exceeds size limit")
        try:
            data = json.loads(raw)
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError
            for row in rows:
                model = row.get("id") if isinstance(row, dict) else None
                if not isinstance(model, str) or not valid_model_id(model):
                    raise ValueError
                found.add(model)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid model-list response") from None
        if not data.get("has_more", False):
            return sorted(found)
        cursor = data.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("Invalid model pagination cursor")
        cursors.add(cursor)
        url = endpoint + "?" + urlencode({"after_id": cursor})
    raise ValueError("Model pagination exceeds page limit")

"""Fetch provider exchange files from local paths or URLs.

Exchange files never contain credentials, so plain HTTP is allowed for local
and internal networks; the caller warns about it. Responses are bounded and
never echoed in errors.
"""

from __future__ import annotations

from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from vibepod.core.provider_discovery import _NoRedirect

MAX_EXCHANGE_BYTES = 256 * 1024


def fetch_exchange(source: str) -> tuple[str, bool]:
    """Return the file text and whether it travelled without TLS.

    ``source`` is a local path or an ``http://``/``https://`` URL.
    """
    parts = urlsplit(source)
    if parts.scheme in ("http", "https") and parts.netloc:
        return _fetch_url(source), parts.scheme == "http"
    if "://" in source:
        raise ValueError("Unsupported source scheme; use a local path or an http(s) URL")
    path = Path(source).expanduser()
    if not path.is_file():
        raise ValueError(f"Provider file not found: {source}")
    if path.stat().st_size > MAX_EXCHANGE_BYTES:
        raise ValueError("Provider file exceeds size limit")
    try:
        return path.read_text(encoding="utf-8"), False
    except UnicodeDecodeError:
        raise ValueError("Provider file is not UTF-8 text") from None


def _fetch_url(url: str) -> str:
    request = Request(url, headers={"Accept": "application/toml, text/plain"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(MAX_EXCHANGE_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if 300 <= status < 400:
            raise ValueError("Provider file fetch refuses redirects; use the final URL") from None
        raise ValueError(f"Fetching the provider file failed: HTTP {status}") from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("Cannot reach the provider file URL") from None
    except HTTPException:
        raise ValueError("Provider file URL returned an invalid HTTP response") from None
    if len(raw) > MAX_EXCHANGE_BYTES:
        raise ValueError("Provider file exceeds size limit")
    try:
        text: str = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Provider file is not UTF-8 text") from None
    return text

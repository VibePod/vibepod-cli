"""Runtime compatibility shims for supported Python versions."""

from __future__ import annotations

import http.client
import sys

_HTTP_RESPONSE_FLUSH_PATCH_ATTR = "_vibepod_python314_closed_file_patch"
_CLOSED_FILE_ERROR = "I/O operation on closed file."


def should_ignore_closed_http_response_flush_error(
    response: object, exc: BaseException
) -> bool:
    """Return True for Python 3.14's closed ``HTTPResponse.fp`` flush cleanup error.

    Python 3.14 can surface a ``ValueError`` while finalizing Docker SDK / urllib3
    HTTP responses if ``HTTPResponse.fp`` is still set but the underlying file is
    already closed. The command has already completed at that point; suppress only
    that exact cleanup edge case.
    """
    if not isinstance(exc, ValueError) or str(exc) != _CLOSED_FILE_ERROR:
        return False
    fp = getattr(response, "fp", None)
    return bool(getattr(fp, "closed", False))


def install_python314_http_client_flush_patch() -> None:
    """Suppress a Python 3.14 stdlib cleanup edge case seen through Docker SDK."""
    if sys.version_info < (3, 14):
        return

    current_flush = http.client.HTTPResponse.flush
    if bool(getattr(current_flush, _HTTP_RESPONSE_FLUSH_PATCH_ATTR, False)):
        return

    original_flush = current_flush

    def _flush(self: http.client.HTTPResponse) -> None:
        try:
            original_flush(self)
        except ValueError as exc:
            if should_ignore_closed_http_response_flush_error(self, exc):
                return
            raise

    setattr(_flush, _HTTP_RESPONSE_FLUSH_PATCH_ATTR, True)
    setattr(http.client.HTTPResponse, "flush", _flush)  # noqa: B010

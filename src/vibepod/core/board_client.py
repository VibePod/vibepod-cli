"""A small client for the vibepod-board REST API, used by `vp board work`.

It speaks JSON over HTTP with the standard library and authenticates with a project-scoped
board API token. A failed request raises `BoardApiError` carrying the board's own message and
the HTTP status, so callers can tell a lost claim (409) from an unreachable board.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses redirects: they would carry the token to another host, and turn a write into a
    body-less GET that looks delivered. urllib then raises the 3xx as an `HTTPError`."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class BoardApiError(Exception):
    """A board request that failed; `status` is None when the board could not be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status

    @property
    def is_conflict(self) -> bool:
        return self.status == 409

    @property
    def is_not_found(self) -> bool:
        return self.status == 404


@dataclass(frozen=True)
class BoardSettings:
    url: str
    token: str


def resolve_board_settings(config: dict[str, Any], url: str | None = None) -> BoardSettings:
    """The board URL and token from `board.url` / `board.token` in the config, which
    `VP_BOARD_URL` / `VP_BOARD_TOKEN` override. An explicit URL wins over both."""
    board = config.get("board") or {}
    if not isinstance(board, dict):
        board = {}
    resolved_url = (url or str(board.get("url") or "")).strip().rstrip("/")
    token = str(board.get("token") or "").strip()
    if not resolved_url:
        raise ValueError("No board URL: set board.url in the config or VP_BOARD_URL.")
    if not resolved_url.startswith(("http://", "https://")):
        raise ValueError(f"Board URL must start with http:// or https://: {resolved_url}")
    if not token:
        raise ValueError("No board token: set board.token in the config or VP_BOARD_TOKEN.")
    return BoardSettings(url=resolved_url, token=token)


def _ref(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _item(result: Any) -> dict[str, Any]:
    item: dict[str, Any] = result["item"]
    return item


def _body(**fields: Any) -> dict[str, Any]:
    """A request body without the fields left unset."""
    return {key: value for key, value in fields.items() if value is not None}


class BoardClient:
    def __init__(self, url: str, token: str, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise BoardApiError(_error_message(exc), exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise BoardApiError(f"Cannot reach the board at {self.url}: {reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BoardApiError(f"The board answered {path} with invalid JSON") from exc

    # --- claims ---------------------------------------------------------------------

    def claim(
        self,
        project: str,
        assignee: str,
        *,
        task: str | None = None,
        labels: Sequence[str] = (),
        min_readiness: int | None = None,
        exclude: Sequence[str] = (),
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        body = _body(
            projectId=project,
            assignee=assignee,
            task=task,
            labels=list(labels) or None,
            minReadiness=min_readiness,
            exclude=list(exclude) or None,
            leaseSeconds=lease_seconds,
        )
        result: dict[str, Any] = self.request("POST", "/api/board/claim", body)
        return result

    def hand_over(
        self,
        card: str,
        assignee: str,
        branch_name: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        body = _body(assignee=assignee, branchName=branch_name, note=note)
        result: dict[str, Any] = self.request("POST", f"/api/board/{_ref(card)}/handover", body)
        return _item(result)

    def release(
        self,
        card: str,
        assignee: str,
        outcome: str,
        note: str | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        body = _body(assignee=assignee, outcome=outcome, note=note, maxAttempts=max_attempts)
        result: dict[str, Any] = self.request("POST", f"/api/board/{_ref(card)}/release", body)
        return _item(result)

    # --- workers --------------------------------------------------------------------

    def register_worker(self, project: str, name: str, agent: str, machine: str) -> dict[str, Any]:
        body = _body(projectId=project, name=name, agent=agent, machine=machine)
        result: dict[str, Any] = self.request("POST", "/api/workers", body)
        return result

    def heartbeat(
        self,
        worker_id: str,
        status: str,
        *,
        reason: str | None = None,
        task: str | None = None,
        step: str | None = None,
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        body = _body(
            status=status,
            statusReason=reason,
            task=task,
            step=step,
            leaseSeconds=lease_seconds,
        )
        result: dict[str, Any] = self.request(
            "POST",
            f"/api/workers/{_ref(worker_id)}/heartbeat",
            body,
        )
        return result

    def sign_off(self, worker_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self.request("POST", f"/api/workers/{_ref(worker_id)}/sign-off")
        return _item(result)

    # --- reports --------------------------------------------------------------------

    def add_run_report(self, task: str, report: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self.request(
            "POST",
            f"/api/ideas/{_ref(task)}/runs",
            _body(**report),
        )
        return _item(result)


def _error_message(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read() or b"{}")
    except (json.JSONDecodeError, OSError):
        payload = {}
    if isinstance(payload, dict) and payload.get("error"):
        message = str(payload["error"])
        details = payload.get("details")
        if isinstance(details, dict) and details.get("fieldErrors"):
            message += f" ({json.dumps(details['fieldErrors'])})"
        return message
    return f"Board request failed with HTTP {exc.code}"

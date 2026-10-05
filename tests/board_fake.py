"""An in-memory vibepod-board for `vp board work` tests, served over real HTTP.

It implements the endpoints the worker uses (claims, hand-over, release, workers, run
reports) with the board's rules in miniature, records every request, and lets a test script
the instructions a heartbeat reply carries.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

TOKEN = "vbp_test-token"


@dataclass
class FakeBoard:
    project: str = "VP"
    tasks: list[dict[str, Any]] = field(default_factory=list)
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    workers: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)
    heartbeats: list[dict[str, Any]] = field(default_factory=list)
    paused: str | None = None
    # Returns extra instructions for a heartbeat body; lets a test cancel or stop a run.
    on_heartbeat: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    # "METHOD /path/prefix" -> how many requests to fail with 503 before answering.
    failures: dict[str, int] = field(default_factory=dict)
    # A branch name the hand-over refuses as invalid.
    refused_branch: str | None = None
    # Answers every request with a redirect to this URL.
    redirect: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    # --- setup ----------------------------------------------------------------------

    def add_task(self, title: str, **fields: Any) -> dict[str, Any]:
        number = len(self.tasks) + 1
        task = {
            "id": f"idea-{number}",
            "key": f"{self.project}-{number}",
            "projectId": "project-1",
            "taskNumber": number,
            "title": title,
            "summary": "",
            "details": fields.pop("details", ""),
            "acceptanceCriteria": fields.pop("acceptanceCriteria", []),
            "labels": fields.pop("labels", []),
            "status": "ready",
            **fields,
        }
        self.tasks.append(task)
        self.cards[task["id"]] = {
            "id": f"card-{number}",
            "ideaId": task["id"],
            "column": fields.get("column", "planned"),
            "attempts": 0,
        }
        return task

    def card(self, key_or_id: str) -> dict[str, Any]:
        task = self.task(key_or_id)
        return self.cards[task["id"]]

    def task(self, ref: str) -> dict[str, Any]:
        for task in self.tasks:
            card = self.cards[task["id"]]
            if ref in {task["id"], task["key"], card["id"]}:
                return task
        raise KeyError(ref)

    def requests(self, method: str, prefix: str) -> list[dict[str, Any] | None]:
        return [body for m, path, body in self.calls if m == method and path.startswith(prefix)]

    # --- endpoints ------------------------------------------------------------------

    def handle(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, Any]:
        with self.lock:
            self.calls.append((method, path, body))
            for prefix, remaining in self.failures.items():
                method_name, _, route = prefix.partition(" ")
                if remaining and method == method_name and path.startswith(route):
                    self.failures[prefix] = remaining - 1
                    return 503, {"error": "Board restarting"}
            parts = [unquote(part) for part in path.strip("/").split("/")]
            body = body or {}
            if parts[:3] == ["api", "board", "claim"]:
                return self._claim(body)
            if parts[:2] == ["api", "board"] and len(parts) == 4:
                return {"handover": self._hand_over, "release": self._release}[parts[3]](
                    parts[2],
                    body,
                )
            if parts == ["api", "workers"]:
                return self._register(body)
            if parts[:2] == ["api", "workers"] and len(parts) == 4:
                if parts[3] == "heartbeat":
                    return self._heartbeat(parts[2], body)
                if parts[3] == "sign-off":
                    return self._sign_off(parts[2])
            if parts[:2] == ["api", "ideas"] and parts[3:] == ["runs"]:
                self.runs.append({"task": parts[2], **body})
                return 201, {"item": {"id": f"run-{len(self.runs)}", **body}}
            return 404, {"error": f"Not found: {method} {path}"}

    def _claimable(self, task: dict[str, Any], labels: list[str]) -> bool:
        card = self.cards[task["id"]]
        wanted = {label.casefold() for label in labels}
        return (
            card["column"] == "planned"
            and not card.get("assignee")
            and not card.get("blockedReason")
            and wanted <= {label.casefold() for label in task.get("labels", [])}
        )

    def _claim(self, body: dict[str, Any]) -> tuple[int, Any]:
        if self.paused:
            return 200, {"claimed": False, "paused": True, "reason": self.paused}
        labels = body.get("labels") or []
        if body.get("task"):
            task = self.task(body["task"])
            if not self._claimable(task, labels):
                return 409, {"error": f"Task {task['key']} cannot be claimed"}
            candidates = [task]
        else:
            skipped = set(body.get("exclude") or [])
            candidates = [
                task
                for task in self.tasks
                if self._claimable(task, labels) and not {task["id"], task["key"]} & skipped
            ]
        if not candidates:
            return 200, {"claimed": False, "reason": "No planned task can be claimed"}
        task = candidates[0]
        card = self.cards[task["id"]]
        card.update(column="in_progress", assignee=body["assignee"], claimedAt="now")
        return 200, {"claimed": True, "item": {"task": dict(task), "card": dict(card)}}

    def _held(self, ref: str, body: dict[str, Any]) -> dict[str, Any] | None:
        card = self.card(ref)
        if card.get("claimedAt") and card.get("assignee") == body.get("assignee"):
            return card
        return None

    def _hand_over(self, ref: str, body: dict[str, Any]) -> tuple[int, Any]:
        card = self._held(ref, body)
        if card is None:
            return 409, {"error": f"Task {ref} is not claimed by {body.get('assignee')}"}
        if self.refused_branch and body.get("branchName") == self.refused_branch:
            return 400, {"error": "Invalid request body"}
        card.update(column="review", assignee=None, claimedAt=None, attempts=0)
        card["branchName"] = body.get("branchName")
        return 200, {"item": dict(card)}

    def _release(self, ref: str, body: dict[str, Any]) -> tuple[int, Any]:
        card = self._held(ref, body)
        if card is None:
            return 409, {"error": f"Task {ref} is not claimed by {body.get('assignee')}"}
        card.update(column="planned", assignee=None, claimedAt=None)
        outcome = body.get("outcome", "failed")
        if outcome == "failed":
            card["attempts"] += 1
            if card["attempts"] >= (body.get("maxAttempts") or 3):
                card["blockedReason"] = body.get("note") or "Failed"
        elif outcome == "blocked":
            card["blockedReason"] = body.get("note")
        return 200, {"item": dict(card)}

    def _register(self, body: dict[str, Any]) -> tuple[int, Any]:
        worker_id = f"worker-{len(self.workers) + 1}"
        self.workers[worker_id] = {"id": worker_id, "status": "idle", **body}
        return 201, {
            "item": self.workers[worker_id],
            "instructions": self._instructions(worker_id, {}),
            "heartbeatSeconds": 15,
        }

    def _sign_off(self, worker_id: str) -> tuple[int, Any]:
        worker = self.workers[worker_id]
        if worker["status"] != "offline":
            # As the board does: claims the worker still holds go back to Planned.
            for card in self.cards.values():
                if card.get("claimedAt") and card.get("assignee") == worker.get("name"):
                    card.update(column="planned", assignee=None, claimedAt=None)
        worker["status"] = "offline"
        return 200, {"item": worker}

    def _heartbeat(self, worker_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        worker = self.workers.get(worker_id)
        if worker is None or worker["status"] == "offline":
            return 409, {"error": "Worker is signed off; register again to reconnect"}
        worker.update(status=body.get("status"), task=body.get("task"), step=body.get("step"))
        self.heartbeats.append(dict(body))
        return 200, {
            "item": worker,
            "instructions": self._instructions(worker_id, body),
            "heartbeatSeconds": 15,
        }

    def _instructions(self, worker_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        instructions: list[dict[str, Any]] = []
        if self.paused:
            instructions.append({"type": "pause", "reason": self.paused})
        if self.on_heartbeat is not None:
            instructions += self.on_heartbeat(body)
        return instructions


class _Handler(BaseHTTPRequestHandler):
    board: FakeBoard

    def _respond(self) -> None:
        # Read the whole request first: answering before the body is read makes Windows
        # reset the connection, and the client then sees a network error instead.
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"error": "Authentication required"})
            return
        if self.board.redirect:
            self.send_response(302)
            self.send_header("Location", self.board.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.loads(raw) if raw else None
        status, payload = self.board.handle(self.command, urlparse(self.path).path, body)
        self._send(status, payload)

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _respond

    def log_message(self, *args: Any) -> None:
        pass


class FakeBoardServer:
    def __init__(self, board: FakeBoard) -> None:
        handler = type("Handler", (_Handler,), {"board": board})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeBoardServer:
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

"""An in-memory vibepod-board for `vp board work` tests, served over real HTTP.

It implements the endpoints the worker uses (claims, hand-over, release, reviews, workers,
run reports) with the board's rules in miniature, records every request, and lets a test
script the instructions a heartbeat reply carries.

Reviews follow the board's review flow (`services/reviews.py`): a review claim takes a task
in Review with the commit its last hand-over named, several reviewers may hold one task, and
a verdict must name that commit. Distinct approvals of it, as many as the project requires,
move the task to PR ready; a rework verdict sends it back to Planned with the feedback and
ends the other open reviews, whose workers are told to cancel with their next heartbeat.
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
    # Task history by task id, newest first, as the board lists it.
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Returns extra instructions for a heartbeat body; lets a test cancel or stop a run.
    on_heartbeat: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    # "METHOD /path/prefix" -> how many requests to fail with 503 before answering.
    failures: dict[str, int] = field(default_factory=dict)
    # A branch name the hand-over refuses as invalid.
    refused_branch: str | None = None
    # The project's review settings.
    required_approvals: int = 1
    max_review_rounds: int = 3
    # Every review claim, open or ended, oldest first.
    reviews: list[dict[str, Any]] = field(default_factory=list)
    # False plays a board from before review workers, which ignores the claim's mode.
    reviews_supported: bool = True
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
            "reviewRounds": 0,
        }
        return task

    def put_in_review(self, key: str, branch: str, head_sha: str | None) -> dict[str, Any]:
        """The card as a hand-over leaves it: in Review on its branch at `head_sha`."""
        card = self.card(key)
        card.update(column="review", branchName=branch, headSha=head_sha, assignee=None)
        card.pop("claimedAt", None)
        return card

    def event(self, task_id: str, kind: str, message: str, actor: str | None = None) -> None:
        """Adds an event to the task history, which lists the newest first."""
        entry: dict[str, Any] = {"kind": kind, "message": message}
        if actor:
            entry["actor"] = actor
        self.history.setdefault(task_id, []).insert(0, entry)

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
            if parts[:2] == ["api", "board"] and parts[3:] == ["reviews"] and method == "GET":
                return 200, self._review_state(self.task(parts[2]))
            if parts[:2] == ["api", "board"] and parts[3:] == ["review", "renew"]:
                return self._renew_review(parts[2], body)
            if parts[:2] == ["api", "board"] and len(parts) == 6 and parts[5] == "cancel":
                return self._cancel_review(parts[2], parts[4], body)
            if parts[:2] == ["api", "board"] and len(parts) == 4:
                return {
                    "handover": self._hand_over,
                    "release": self._release,
                    "review": self._submit_review,
                }[parts[3]](parts[2], body)
            if parts == ["api", "workers"]:
                return self._register(body)
            if parts[:2] == ["api", "workers"] and len(parts) == 4:
                if parts[3] == "heartbeat":
                    return self._heartbeat(parts[2], body)
                if parts[3] == "sign-off":
                    return self._sign_off(parts[2])
            if parts[:2] == ["api", "ideas"] and parts[3:] == ["history"]:
                task = self.task(parts[2])
                return 200, {"items": self.history.get(task["id"], [])}
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
        if body.get("mode") == "review" and self.reviews_supported:
            return self._claim_review(body)
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
        # Reviews and approvals are bound to the commit handed over; a new one starts the
        # approval count over.
        card["headSha"] = (body.get("headSha") or "").strip().lower() or None
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
        elif outcome == "needs_input":
            card["blockedReason"] = f"Needs input: {body.get('note')}"
            card["question"] = body.get("note")
        return 200, {"item": dict(card)}

    # --- reviews ----------------------------------------------------------------------

    def _open_reviews(self, task_id: str) -> list[dict[str, Any]]:
        return [r for r in self.reviews if r["ideaId"] == task_id and r["open"]]

    def _at_head(self, card: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            review
            for review in self.reviews
            if review["ideaId"] == card["ideaId"] and review["headSha"] == card.get("headSha")
        ]

    def approvers(self, key: str) -> list[str]:
        """Distinct reviewers that approved the card's current head commit."""
        card = self.card(key)
        approved = [r["reviewer"] for r in self._at_head(card) if r.get("verdict") == "approve"]
        return list(dict.fromkeys(approved))

    def _review_obstacle(self, task: dict[str, Any], reviewer: str, labels: list[str]) -> str:
        card = self.cards[task["id"]]
        if card["column"] != "review":
            return f"its card is in {card['column']}, not review"
        if card.get("blockedReason"):
            return f"it is blocked: {card['blockedReason']}"
        if not card.get("branchName"):
            return "its card names no branch to review"
        wanted = {label.casefold() for label in labels}
        if not wanted <= {label.casefold() for label in task.get("labels", [])}:
            return "it lacks a label"
        reviews = self._open_reviews(task["id"])
        if any(review["reviewer"] == reviewer for review in reviews):
            return f"{reviewer} is already reviewing it"
        settled = ("approve", "rework", "failed")
        if any(
            r["reviewer"] == reviewer and r.get("verdict") in settled for r in self._at_head(card)
        ):
            return f"{reviewer} already reviewed it"
        if len(self.approvers(task["id"])) + len(reviews) >= self.required_approvals:
            return "it has the approvals and open reviews it needs"
        return ""

    def _claim_review(self, body: dict[str, Any]) -> tuple[int, Any]:
        reviewer = body["assignee"]
        labels = body.get("labels") or []
        if body.get("task"):
            task = self.task(body["task"])
            held = [r for r in self._open_reviews(task["id"]) if r["reviewer"] == reviewer]
            if held:
                return 200, self._review_claimed(task, held[0])
            obstacle = self._review_obstacle(task, reviewer, labels)
            if obstacle:
                return 409, {"error": f"Task {task['key']} cannot be reviewed: {obstacle}"}
        else:
            skipped = set(body.get("exclude") or [])
            candidates = [
                task
                for task in self.tasks
                if not self._review_obstacle(task, reviewer, labels)
                and not {task["id"], task["key"]} & skipped
            ]
            if not candidates:
                return 200, {
                    "claimed": False,
                    "reason": "No task in review can be claimed for a review",
                }
            task = candidates[0]
        card = self.cards[task["id"]]
        worker = next(
            (
                w
                for w in self.workers.values()
                if w.get("name") == reviewer and w["status"] != "offline"
            ),
            None,
        )
        review = {
            "id": f"review-{len(self.reviews) + 1}",
            "ideaId": task["id"],
            "taskKey": task["key"],
            "projectId": "project-1",
            "reviewer": reviewer,
            "headSha": card.get("headSha"),
            "open": True,
        }
        if worker is not None:
            review["workerId"] = worker["id"]
        self.reviews.append(review)
        self.event(task["id"], "review_started", f"Review by {reviewer} started", reviewer)
        return 200, self._review_claimed(task, review)

    def _review_claimed(self, task: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
        card = self.cards[task["id"]]
        shown = {key: value for key, value in review.items() if value is not None}
        return {"claimed": True, "item": {"task": dict(task), "card": dict(card)}, "review": shown}

    def _held_review(self, ref: str, reviewer: str | None) -> dict[str, Any] | None:
        task = self.task(ref)
        held = [r for r in self._open_reviews(task["id"]) if r["reviewer"] == reviewer]
        return held[0] if held else None

    def _end_review(self, review: dict[str, Any], reason: str, verdict: str | None = None) -> None:
        review.update(open=False, endedReason=reason)
        if verdict is not None:
            review["verdict"] = verdict
        self.event(
            review["ideaId"], "review_ended", f"Review by {review['reviewer']} ended: {reason}"
        )

    def _end_other_reviews(self, task_id: str, keep: dict[str, Any], reason: str) -> None:
        for review in self._open_reviews(task_id):
            if review is not keep:
                self._end_review(review, reason)

    def _submit_review(self, ref: str, body: dict[str, Any]) -> tuple[int, Any]:
        reviewer = body.get("assignee")
        verdict = body.get("verdict")
        note = (body.get("note") or "").strip()
        sha = (body.get("headSha") or "").strip().lower() or None
        if verdict not in ("approve", "rework", "needs_input", "failed", "released"):
            return 400, {"error": "Invalid request body"}
        if verdict in ("rework", "needs_input") and not note:
            return 400, {"error": f"A note is required for {verdict}"}
        task = self.task(ref)
        card = self.cards[task["id"]]
        review = self._held_review(ref, reviewer)
        if review is None:
            return 409, {"error": f"Task {task['key']} is not being reviewed by {reviewer}"}
        judging = verdict in ("approve", "rework")
        if judging and review["headSha"] and not sha:
            return 400, {"error": f"headSha is required: the review is of {review['headSha']}"}
        if sha and sha != review["headSha"]:
            return 409, {
                "error": f"Task {task['key']} is reviewed at another commit: the branch moved on"
            }
        if review["headSha"] != card.get("headSha") or card["column"] != "review":
            return 409, {
                "error": f"Task {task['key']} was handed over again since the review started"
            }
        ending = verdict in ("failed", "released")
        if not ending and card.get("blockedReason"):
            return 409, {"error": f"Task {task['key']} is blocked until a human acts"}
        if ending:
            self._end_review(review, verdict + (f": {note}" if note else ""), verdict)
            review["feedback"] = note or None
            return 200, self._review_state(task)
        review.update(open=False, verdict=verdict, feedback=note or None)
        if verdict == "approve":
            approved = self.approvers(task["id"])
            self.event(task["id"], "approved", f"Approved by {reviewer}", reviewer)
            if len(approved) >= self.required_approvals:
                self._end_other_reviews(task["id"], review, "the task is ready for a PR")
                card.update(column="pr_ready", reviewRounds=0)
        elif verdict == "rework":
            self._end_other_reviews(task["id"], review, f"{reviewer} requested rework")
            card["reviewRounds"] += 1
            self.event(task["id"], "rework_requested", f"Rework requested by {reviewer}", reviewer)
            self.event(task["id"], "feedback", note, reviewer)
            if card["reviewRounds"] >= self.max_review_rounds:
                card["blockedReason"] = f"Sent back for rework {card['reviewRounds']} times: {note}"
            else:
                card.update(column="planned", attempts=0)
        else:
            self._end_other_reviews(task["id"], review, f"{reviewer} asked for input")
            card.update(blockedReason=f"Needs input: {note}", question=note)
            self.event(task["id"], "question", note, reviewer)
        return 200, self._review_state(task)

    def _renew_review(self, ref: str, body: dict[str, Any]) -> tuple[int, Any]:
        review = self._held_review(ref, body.get("assignee"))
        if review is None:
            return 409, {"error": f"Task {ref} is not being reviewed by {body.get('assignee')}"}
        return 200, {"item": dict(review)}

    def _cancel_review(self, ref: str, review_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        task = self.task(ref)
        review = next((r for r in self.reviews if r["id"] == review_id), None)
        if review is None or review["ideaId"] != task["id"]:
            return 404, {"error": f"Review not found on task {task['key']}: {review_id}"}
        if not review["open"]:
            return 409, {"error": f"The review by {review['reviewer']} already ended"}
        reason = (body.get("reason") or "").strip()
        self._end_review(review, "cancelled" + (f": {reason}" if reason else ""))
        return 200, self._review_state(task)

    def _review_state(self, task: dict[str, Any]) -> dict[str, Any]:
        card = self.cards[task["id"]]
        items = [
            {key: value for key, value in review.items() if value is not None}
            for review in reversed(self.reviews)
            if review["ideaId"] == task["id"]
        ]
        approved = self.approvers(task["id"])
        state = {
            "taskId": task["id"],
            "taskKey": task["key"],
            "column": card["column"],
            "headSha": card.get("headSha"),
            "requiredApprovals": self.required_approvals,
            "approvals": len(approved),
            "approvedBy": approved,
            "openReviews": len(self._open_reviews(task["id"])),
            "reviewRounds": card["reviewRounds"],
            "maxReviewRounds": self.max_review_rounds,
            "items": items,
        }
        return {key: value for key, value in state.items() if value is not None}

    def _register(self, body: dict[str, Any]) -> tuple[int, Any]:
        worker_id = f"worker-{len(self.workers) + 1}"
        self.workers[worker_id] = {"id": worker_id, "status": "idle", "mode": "implement", **body}
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
        lost = self._lost_task(worker_id, body)
        if lost is not None and not any(item.get("type") == "cancel" for item in instructions):
            instructions.append(lost)
        return instructions

    def _lost_task(self, worker_id: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """A cancel for the task a working worker no longer holds a claim or a review of, such
        as one another reviewer sent back for rework, with the latest word in its history as
        the reason."""
        worker = self.workers[worker_id]
        if body.get("status") != "working" or not body.get("task"):
            return None
        task = self.task(str(body["task"]))
        card = self.cards[task["id"]]
        claimed = bool(card.get("claimedAt")) and card.get("assignee") == worker.get("name")
        if claimed or self._held_review(task["id"], worker.get("name")) is not None:
            return None
        events = self.history.get(task["id"], [])
        reason = events[0]["message"] if events else "The task is no longer claimed"
        return {"type": "cancel", "reason": reason, "taskId": task["id"], "taskKey": task["key"]}


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

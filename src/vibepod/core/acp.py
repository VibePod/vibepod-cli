"""ACP client handshake for ``vp run --acp``.

The editor (the ACP client) speaks newline-delimited JSON-RPC on our stdin
and stdout; the adapter inside the container is the real ACP agent and vp
normally copies bytes between the two. Before the container exists, vp needs
the client for two things the interactive CLI would do on a TTY:

- ask a question (allow the workspace, pick a compose network). ACP ties a
  request-scoped ``elicitation/create`` to a JSON-RPC request the client is
  still waiting on, meant for "auth/configuration phases before any session
  is started" -- exactly the client's pending ``initialize``;
- report a pre-launch failure as a JSON-RPC error on that request, so the
  editor shows the message instead of "process exited with code 1".

Every client byte read here is kept and replayed into the container once it
runs, so the adapter still receives the untouched ``initialize`` request and
answers it itself. Only the responses to vp's own elicitations are consumed.
"""

from __future__ import annotations

import atexit
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# -32000 is ACP's ``auth_required``; pre-launch aborts use the generic
# implementation-defined server error instead.
ACP_INTERNAL_ERROR = -32603

_READ_CHUNK = 65536


@dataclass(frozen=True)
class ElicitationOutcome:
    """Result of one form elicitation.

    ``status`` is ``accepted`` (``content`` holds the submitted fields),
    ``declined`` (the user dismissed the form) or ``unavailable`` (the client
    cannot show forms, answered with an error, or went away). Callers treat
    ``unavailable`` like a missing TTY: fall back to the non-interactive path.
    """

    status: str
    content: dict[str, Any] = field(default_factory=dict)


class AcpClientChannel:
    """Line-oriented JSON-RPC access to the ACP client before the container runs.

    Reads raw bytes from ``read_fd`` (our stdin) and writes frames with
    ``write`` (our stdout). Consumed client lines are queued for replay;
    ``take_replay`` hands them, plus any unconsumed remainder, to whoever
    attaches the container so no byte of the client stream is lost.
    """

    def __init__(self, read_fd: int, write: Callable[[bytes], None]) -> None:
        self._read_fd = read_fd
        self._write = write
        self._buffer = b""
        self._replay: list[bytes] = []
        self._eof = False
        self._next_id = 0
        self._answered = False
        self.initialize_id: Any = None
        self.initialize_params: dict[str, Any] = {}

    # -- reading -----------------------------------------------------------

    def _read_line(self) -> bytes | None:
        while b"\n" not in self._buffer:
            if self._eof:
                return None
            try:
                chunk = os.read(self._read_fd, _READ_CHUNK)
            except OSError:
                chunk = b""
            if not chunk:
                self._eof = True
                return None
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line + b"\n"

    def _next_message(self) -> tuple[bytes, dict[str, Any] | None] | None:
        line = self._read_line()
        if line is None:
            return None
        try:
            parsed = json.loads(line)
        except ValueError:
            parsed = None
        return line, parsed if isinstance(parsed, dict) else None

    def wait_for_initialize(self) -> bool:
        """Consume client frames up to and including ``initialize``.

        Returns False when the client closed the stream first. Frames that
        precede ``initialize`` are kept for replay untouched.
        """
        while True:
            item = self._next_message()
            if item is None:
                return False
            raw, message = item
            self._replay.append(raw)
            if message is not None and message.get("method") == "initialize" and "id" in message:
                self.initialize_id = message["id"]
                params = message.get("params")
                self.initialize_params = params if isinstance(params, dict) else {}
                return True

    @property
    def supports_form_elicitation(self) -> bool:
        """True when the client advertised ``clientCapabilities.elicitation.form``."""
        capabilities = self.initialize_params.get("clientCapabilities")
        if not isinstance(capabilities, dict):
            return False
        elicitation = capabilities.get("elicitation")
        return isinstance(elicitation, dict) and isinstance(elicitation.get("form"), dict)

    # -- writing -----------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        self._write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")

    def elicit(
        self,
        message: str,
        properties: dict[str, Any],
        required: list[str],
    ) -> ElicitationOutcome:
        """Show the client a form tied to the pending ``initialize`` request.

        Client frames that arrive meanwhile and are not the answer belong to
        the adapter and are queued for replay in order.
        """
        if self.initialize_id is None or self._answered or not self.supports_form_elicitation:
            return ElicitationOutcome("unavailable")
        self._next_id += 1
        request_id = f"vp-{self._next_id}"
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "requestId": self.initialize_id,
                    "message": message,
                    "requestedSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            },
        )
        while True:
            item = self._next_message()
            if item is None:
                return ElicitationOutcome("unavailable")
            raw, reply = item
            if reply is None or "method" in reply or reply.get("id") != request_id:
                self._replay.append(raw)
                continue
            result = reply.get("result")
            if not isinstance(result, dict):
                return ElicitationOutcome("unavailable")
            if result.get("action") != "accept":
                return ElicitationOutcome("declined")
            content = result.get("content")
            return ElicitationOutcome("accepted", content if isinstance(content, dict) else {})

    def fail_initialize(self, message: str) -> bool:
        """Answer the pending ``initialize`` with a JSON-RPC error.

        Returns False when there is nothing to answer: no ``initialize`` seen
        yet, already failed, or already handed over to the container.
        """
        if self.initialize_id is None or self._answered:
            return False
        self._answered = True
        self.send(
            {
                "jsonrpc": "2.0",
                "id": self.initialize_id,
                "error": {"code": ACP_INTERNAL_ERROR, "message": message},
            },
        )
        return True

    # -- hand-over ---------------------------------------------------------

    def take_replay(self) -> bytes:
        """Return every client byte the adapter has not seen, in order.

        That is the consumed frames (minus answers to vp's own requests) and
        the unconsumed remainder of the last read. Written to the container's
        stdin once it starts; afterwards the caller owns the stdin fd.
        """
        data = b"".join(self._replay) + self._buffer
        self._replay = []
        self._buffer = b""
        return data

    def mark_handed_over(self) -> None:
        """Record that the container will answer ``initialize`` from here on."""
        self._answered = True

    def install_exit_guard(self, message_provider: Callable[[], str]) -> None:
        """Answer a still-pending ``initialize`` with an error at interpreter exit.

        Every pre-launch abort in ``vp run`` is a ``typer.Exit`` after an
        ``error()`` line on stderr; this turns that line into a JSON-RPC error
        the editor renders, without threading the channel through every
        abort site. A no-op once the container took over.
        """

        def _guard() -> None:
            try:
                self.fail_initialize(message_provider())
            except OSError:  # pragma: no cover - client already gone
                pass

        atexit.register(_guard)

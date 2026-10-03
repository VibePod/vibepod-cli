"""ACP client channel: the pre-launch handshake of `vp run --acp`."""

from __future__ import annotations

import json
import os
from typing import Any

from vibepod.core import acp as acp_mod
from vibepod.core.acp import ACP_INTERNAL_ERROR, AcpClientChannel


def _line(message: dict[str, Any]) -> bytes:
    return json.dumps(message).encode() + b"\n"


def _initialize(request_id: Any = 7, *, form: bool = True) -> bytes:
    capabilities: dict[str, Any] = {"fs": {"readTextFile": True, "writeTextFile": True}}
    if form:
        capabilities["elicitation"] = {"form": {}, "url": {}}
    return _line(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": capabilities},
        },
    )


def _channel(script: bytes) -> tuple[AcpClientChannel, list[bytes]]:
    """A channel whose client already wrote *script* and then hung up."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, script)
    os.close(write_fd)
    written: list[bytes] = []
    return AcpClientChannel(read_fd, written.append), written


def _sent(written: list[bytes]) -> list[dict[str, Any]]:
    return [json.loads(item) for item in written]


def test_wait_for_initialize_records_id_and_client_capabilities() -> None:
    channel, written = _channel(_initialize(7))

    assert channel.wait_for_initialize() is True
    assert channel.initialize_id == 7
    assert channel.supports_form_elicitation is True
    assert written == []


def test_wait_for_initialize_without_form_capability() -> None:
    channel, _ = _channel(_initialize(form=False))
    assert channel.wait_for_initialize() is True
    assert channel.supports_form_elicitation is False


def test_wait_for_initialize_is_false_when_the_client_hangs_up_first() -> None:
    channel, _ = _channel(b"")
    assert channel.wait_for_initialize() is False
    assert channel.initialize_id is None


def test_frames_before_initialize_are_kept_for_replay_in_order() -> None:
    noise = b"not json\n"
    channel, _ = _channel(noise + _initialize(1))

    assert channel.wait_for_initialize() is True
    assert channel.take_replay() == noise + _initialize(1)


def test_elicit_ties_the_form_to_the_pending_initialize_and_returns_content() -> None:
    answer = _line(
        {
            "jsonrpc": "2.0",
            "id": "vp-1",
            "result": {"action": "accept", "content": {"decision": "allow_once"}},
        },
    )
    channel, written = _channel(_initialize("init-9") + answer)
    channel.wait_for_initialize()

    outcome = channel.elicit("Allow?", {"decision": {"type": "string"}}, ["decision"])

    assert outcome.status == "accepted"
    assert outcome.content == {"decision": "allow_once"}
    request = _sent(written)[0]
    assert request["method"] == "elicitation/create"
    assert request["id"] == "vp-1"
    assert request["params"]["mode"] == "form"
    assert request["params"]["requestId"] == "init-9"
    assert request["params"]["requestedSchema"] == {
        "type": "object",
        "properties": {"decision": {"type": "string"}},
        "required": ["decision"],
    }
    # The answer was vp's to consume; the adapter only ever sees initialize.
    assert channel.take_replay() == _initialize("init-9")


def test_elicit_reports_declined_and_cancelled_forms() -> None:
    for action in ("decline", "cancel"):
        answer = _line({"jsonrpc": "2.0", "id": "vp-1", "result": {"action": action}})
        channel, _ = _channel(_initialize() + answer)
        channel.wait_for_initialize()
        assert channel.elicit("q", {}, []).status == "declined"


def test_elicit_is_unavailable_without_form_support_or_on_client_error() -> None:
    channel, written = _channel(_initialize(form=False))
    channel.wait_for_initialize()
    assert channel.elicit("q", {}, []).status == "unavailable"
    assert written == []

    refused = _line({"jsonrpc": "2.0", "id": "vp-1", "error": {"code": -32601, "message": "no"}})
    channel, _ = _channel(_initialize() + refused)
    channel.wait_for_initialize()
    assert channel.elicit("q", {}, []).status == "unavailable"

    channel, _ = _channel(_initialize())  # hangs up while the form is open
    channel.wait_for_initialize()
    assert channel.elicit("q", {}, []).status == "unavailable"


def test_unrelated_client_frames_during_a_form_are_replayed_after_initialize() -> None:
    notification = _line({"jsonrpc": "2.0", "method": "session/cancel", "params": {}})
    answer = _line({"jsonrpc": "2.0", "id": "vp-1", "result": {"action": "accept"}})
    partial = b'{"jsonrpc":"2.0","id":3,"method":"session/new"'
    channel, _ = _channel(_initialize() + notification + answer + partial)
    channel.wait_for_initialize()

    assert channel.elicit("q", {}, []).status == "accepted"
    assert channel.take_replay() == _initialize() + notification + partial
    assert channel.take_replay() == b""


def test_fail_initialize_answers_the_pending_request_once() -> None:
    channel, written = _channel(_initialize(4))
    channel.wait_for_initialize()

    assert channel.fail_initialize("no dice") is True
    assert channel.fail_initialize("again") is False
    assert _sent(written) == [
        {"jsonrpc": "2.0", "id": 4, "error": {"code": ACP_INTERNAL_ERROR, "message": "no dice"}},
    ]
    assert channel.elicit("q", {}, []).status == "unavailable"


def test_fail_initialize_is_a_no_op_before_initialize_or_after_hand_over() -> None:
    channel, written = _channel(b"")
    assert channel.fail_initialize("x") is False

    channel, written = _channel(_initialize())
    channel.wait_for_initialize()
    channel.mark_handed_over()
    assert channel.fail_initialize("x") is False
    assert written == []


def test_exit_guard_reports_a_pending_initialize_at_exit(monkeypatch) -> None:
    guards: list[Any] = []
    monkeypatch.setattr(acp_mod.atexit, "register", guards.append)
    channel, written = _channel(_initialize(2))
    channel.wait_for_initialize()
    channel.install_exit_guard(lambda: "allow-dir first")

    assert len(guards) == 1
    guards[0]()
    assert _sent(written)[0]["error"]["message"] == "allow-dir first"

    # Once the container owns the request the guard stays silent.
    channel, written = _channel(_initialize(3))
    channel.wait_for_initialize()
    channel.install_exit_guard(lambda: "late")
    channel.mark_handed_over()
    guards[-1]()
    assert written == []

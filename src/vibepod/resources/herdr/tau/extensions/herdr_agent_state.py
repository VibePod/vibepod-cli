"""Managed by VibePod — report Tau lifecycle events to Herdr."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class TauExtensionAPI(Protocol):
    """Subset of Tau's extension API used by this integration."""

    def on(
        self,
        event: str,
        handler: Callable[[object, object], Awaitable[None]],
    ) -> object: ...


async def _report(state: str, context: object) -> None:
    sock_path = os.environ.get("HERDR_SOCKET_PATH")
    pane = os.environ.get("HERDR_PANE_ID")
    if not sock_path or not pane:
        return

    params: dict[str, Any] = {
        "pane_id": pane,
        "source": "vibepod",
        "agent": "tau",
        "display_agent": "vp:tau",
        "state": state,
    }
    session_id = getattr(context, "session_id", None)
    if isinstance(session_id, str) and session_id:
        params["agent_session_id"] = session_id

    request = {
        "id": f"vibepod:{os.getpid()}:{time.time_ns()}",
        "method": "pane.report_agent",
        "params": params,
    }
    writer: asyncio.StreamWriter | None = None
    try:
        reader, connected_writer = await asyncio.wait_for(
            asyncio.open_unix_connection(sock_path),
            timeout=3,
        )
        writer = connected_writer
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        await asyncio.wait_for(reader.readline(), timeout=3)
    except (OSError, TimeoutError, asyncio.TimeoutError):
        # Herdr must never disturb Tau's event loop.
        return
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


def setup(tau: TauExtensionAPI) -> None:
    """Register working/idle handlers with Tau's public extension API."""

    async def working(_event: object, context: object) -> None:
        await _report("working", context)

    async def idle(_event: object, context: object) -> None:
        await _report("idle", context)

    for event in ("agent_start", "turn_start"):
        tau.on(event, working)
    for event in ("agent_end", "agent_settled", "turn_end"):
        tau.on(event, idle)

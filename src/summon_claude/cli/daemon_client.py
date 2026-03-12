"""Typed async client for the daemon Unix socket control API.

All public functions open a fresh connection, send one request, receive one
response, and close the connection.  This keeps the client stateless and
safe to call from any asyncio context.

Raises ``DaemonError`` when the daemon returns ``{"type": "error", ...}``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
from typing import Any

from summon_claude.sessions.session import SessionOptions

logger = logging.getLogger(__name__)


class DaemonError(Exception):
    """Raised when the daemon returns an error response."""


async def _request(msg: dict[str, Any]) -> dict[str, Any]:
    """Send *msg* to the daemon and return the response dict.

    Opens a fresh Unix socket connection, sends the message via ``send_msg``,
    reads the response via ``recv_msg``, closes the connection, then returns
    the parsed response.

    Raises ``DaemonError`` if the daemon responds with ``type == "error"``.
    """
    from summon_claude.daemon import (  # noqa: PLC0415
        connect_to_daemon,
        recv_msg,
        send_msg,
    )

    reader, writer = await connect_to_daemon()
    try:
        await send_msg(writer, msg)
        response = await recv_msg(reader)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    if response.get("type") == "error":
        raise DaemonError(response.get("message", "Unknown daemon error"))

    return response


async def create_session(options: SessionOptions) -> str:
    """Send a ``create_session`` request to the daemon.

    The daemon generates the session ID and auth token internally.

    Returns:
        The short code for the user to authenticate via ``/summon <code>`` in Slack.
    """
    response = await _request({"type": "create_session", "options": dataclasses.asdict(options)})
    if response.get("type") != "session_created":
        raise DaemonError(f"Unexpected daemon response: {response}")
    short_code: str = response["short_code"]
    logger.debug("Session created (code: %s)", short_code)
    return short_code


async def create_session_with_spawn_token(options: SessionOptions, spawn_token: str) -> str:
    """Send a create_session_with_spawn_token request to the daemon.

    Returns the session_id of the spawned session.
    """
    response = await _request(
        {
            "type": "create_session_with_spawn_token",
            "options": dataclasses.asdict(options),
            "spawn_token": spawn_token,
        }
    )
    if response.get("type") != "session_created_spawned":
        raise DaemonError(f"Unexpected daemon response: {response}")
    return response["session_id"]


async def stop_session(session_id: str) -> bool:
    """Send a ``stop_session`` request to the daemon.

    Returns:
        ``True`` if the daemon found and signalled the session, ``False`` if
        the session was not found.
    """
    response = await _request({"type": "stop_session", "session_id": session_id})
    found: bool = response.get("found", False)
    return found


async def get_status() -> dict[str, Any]:
    """Request the daemon status and return the raw response dict."""
    return await _request({"type": "status"})


async def list_sessions() -> list[dict[str, Any]]:
    """Return the list of active sessions from the daemon status response.

    Returns sparse dicts from the daemon (session_id, channel_id only),
    not full registry records.
    """
    status = await get_status()
    sessions: list[dict[str, Any]] = status.get("sessions", [])
    return sessions


async def stop_all_sessions() -> list[tuple[str, bool]]:
    """Stop every active session via a single ``stop_all`` IPC message.

    Returns:
        List of ``(session_id, was_found)`` tuples — one per session.
    """
    response = await _request({"type": "stop_all"})
    if response.get("type") != "all_stopped":
        raise DaemonError(f"Unexpected daemon response: {response}")
    results: list[dict[str, Any]] = response.get("results", [])
    return [(r["session_id"], r["found"]) for r in results]

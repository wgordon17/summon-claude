"""Typed async client for the daemon Unix socket control API.

All public functions open a fresh connection, send one request, receive one
response, and close the connection.  This keeps the client stateless and
safe to call from any asyncio context.

Raises ``DaemonError`` when the daemon returns ``{"type": "error", ...}``.
"""

from __future__ import annotations

import contextlib
import logging

logger = logging.getLogger(__name__)


class DaemonError(Exception):
    """Raised when the daemon returns an error response."""


async def _request(msg: dict) -> dict:  # type: ignore[type-arg]
    """Send *msg* to the daemon and return the response dict.

    Opens a fresh Unix socket connection, sends the message via ``send_msg``,
    reads the response via ``recv_msg``, closes the connection, then returns
    the parsed response.

    Raises ``DaemonError`` if the daemon responds with ``type == "error"``.
    """
    from summon_claude.daemon import connect_to_daemon  # noqa: PLC0415
    from summon_claude.ipc import recv_msg, send_msg  # noqa: PLC0415

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


async def create_session(options: dict) -> tuple[str, str]:  # type: ignore[type-arg]
    """Send a ``create_session`` request to the daemon.

    The daemon generates the session ID and auth token internally.

    Args:
        options: Serialised ``SessionOptions`` dict (cwd, name, model, resume).

    Returns:
        ``(session_id, short_code)`` — the short code is shown to the user so
        they can authenticate via ``/summon <code>`` in Slack.
    """
    response = await _request({"type": "create_session", "options": options})
    if response.get("type") != "session_created":
        raise DaemonError(f"Unexpected daemon response: {response}")
    session_id: str = response["session_id"]
    short_code: str = response["short_code"]
    logger.debug("Session created: %s (code: %s)", session_id, short_code)
    return session_id, short_code


async def stop_session(session_id: str) -> bool:
    """Send a ``stop_session`` request to the daemon.

    Returns:
        ``True`` if the daemon found and signalled the session, ``False`` if
        the session was not found.
    """
    response = await _request({"type": "stop_session", "session_id": session_id})
    found: bool = response.get("found", False)
    return found


async def get_status() -> dict:  # type: ignore[type-arg]
    """Request the daemon status and return the raw response dict."""
    return await _request({"type": "status"})


async def list_sessions() -> list[dict]:  # type: ignore[type-arg]
    """Return the list of active sessions from the daemon status response."""
    status = await get_status()
    sessions: list[dict] = status.get("sessions", [])  # type: ignore[type-arg]
    return sessions


async def stop_all_sessions() -> list[tuple[str, bool]]:
    """Stop every active session reported by the daemon.

    Queries ``get_status()`` for the current session list, then calls
    ``stop_session()`` for each one.

    Returns:
        List of ``(session_id, was_found)`` tuples — one per session.
    """
    sessions = await list_sessions()
    results: list[tuple[str, bool]] = []
    for sess in sessions:
        sid = sess.get("session_id", "")
        if not sid:
            continue
        found = await stop_session(sid)
        results.append((sid, found))
    return results

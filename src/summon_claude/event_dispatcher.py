"""Event routing layer for the single-Bolt daemon architecture.

The ``EventDispatcher`` maintains a registry of active sessions (keyed by
``channel_id``) and routes incoming Slack events to the correct session.

Design note: asyncio is single-threaded, so all dict mutations and reads are
safe without explicit locking.  ``register`` and ``unregister`` must be called
from the same event loop as the dispatch methods.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from summon_claude.permissions import PermissionHandler

logger = logging.getLogger(__name__)

# action_id pattern used to recognise AskUserQuestion button clicks
_ASK_USER_RE = re.compile(r"ask_user_\d+_.+")


@dataclass
class SessionHandle:
    """Lightweight reference to a running session held by the dispatcher.

    Attributes:
        session_id: Unique session identifier.
        channel_id: Slack channel ID this session is bound to.
        message_queue: Queue that the session task reads user messages from.
        permission_handler: Handles Slack button click actions for the session.
        abort_callback: Zero-argument callable that cancels the current turn.
        authenticated_user_id: Slack user ID that owns the session.
    """

    session_id: str
    channel_id: str
    message_queue: asyncio.Queue  # type: ignore[type-arg]
    permission_handler: PermissionHandler
    abort_callback: Callable[[], None]
    authenticated_user_id: str


class EventDispatcher:
    """Routes Slack events from Bolt handlers to the correct session.

    Sessions are registered by ``channel_id``.  Events whose channel maps to
    no registered session are silently dropped — this is intentional behaviour
    for channels from previous sessions, bot DMs, and unrelated activity.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionHandle] = {}  # channel_id → handle

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def register(self, channel_id: str, handle: SessionHandle) -> None:
        """Register *handle* for *channel_id*, replacing any existing entry."""
        self._sessions[channel_id] = handle
        logger.debug(
            "EventDispatcher: registered session %s on channel %s",
            handle.session_id,
            channel_id,
        )

    def unregister(self, channel_id: str) -> None:
        """Remove the session registered for *channel_id* (no-op if absent)."""
        handle = self._sessions.pop(channel_id, None)
        if handle is not None:
            logger.debug(
                "EventDispatcher: unregistered session %s from channel %s",
                handle.session_id,
                channel_id,
            )

    def all_channel_ids(self) -> list[str]:
        """Return a snapshot of all registered channel IDs."""
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch_message(self, event: dict) -> None:  # type: ignore[type-arg]
        """Route an incoming Slack message event to the appropriate session queue.

        The channel is extracted from ``event["channel"]``.  Events for unknown
        channels are silently ignored.
        """
        channel_id = event.get("channel", "")
        handle = self._sessions.get(channel_id)
        if handle is not None:
            try:
                handle.message_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "EventDispatcher: session %s queue full — message dropped for channel %s",
                    handle.session_id,
                    channel_id,
                )
        else:
            logger.debug("EventDispatcher: no session for channel %s — message dropped", channel_id)

    async def dispatch_action(self, action: dict, body: dict) -> None:  # type: ignore[type-arg]
        """Route a Slack interactive action to the session's permission handler.

        Distinguishes between:
        - ``permission_approve`` / ``permission_deny`` → ``handle_action``
        - ``ask_user_*`` → ``handle_ask_user_action``

        The channel is extracted from ``body["channel"]["id"]``.  Actions for
        unknown channels are silently ignored.
        """
        channel_id = body.get("channel", {}).get("id", "")
        handle = self._sessions.get(channel_id)
        if handle is None:
            logger.debug("EventDispatcher: no session for channel %s — action dropped", channel_id)
            return

        action_id: str = action.get("action_id", "")
        value: str = action.get("value", "")
        user_id: str = body.get("user", {}).get("id", "")
        response_url: str = body.get("response_url", "")

        if _ASK_USER_RE.fullmatch(action_id):
            await handle.permission_handler.handle_ask_user_action(
                value=value,
                user_id=user_id,
                response_url=response_url,
            )
        else:
            # permission_approve / permission_deny (and any future variants)
            await handle.permission_handler.handle_action(
                value=value,
                user_id=user_id,
                channel_id=channel_id,
                response_url=response_url,
            )

    async def dispatch_reaction(self, event: dict) -> None:  # type: ignore[type-arg]
        """Route a ``reaction_added`` event to the session's abort callback.

        The channel is extracted from ``event["item"]["channel"]``.  Reactions
        on unknown channels are silently ignored.
        """
        channel_id = event.get("item", {}).get("channel", "")
        handle = self._sessions.get(channel_id)
        if handle is not None:
            handle.abort_callback()
        else:
            logger.debug(
                "EventDispatcher: no session for channel %s — reaction dropped",
                channel_id,
            )

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.slack.client import SlackClient

# Callback type for session resume (injected by daemon to break circular import).
# Arguments: (channel_id, user_id, target_session_id)
ResumeHandler = Callable[[str, str, str | None], Awaitable[None]]

if TYPE_CHECKING:
    from summon_claude.sessions.permissions import PermissionHandler

logger = logging.getLogger(__name__)

# Callback signature for /summon command handling.
# Arguments: (user_id: str, code: str, respond: Callable)
CommandHandler = Callable[..., Awaitable[None]]

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

    The dispatcher also holds an optional *command handler* callback for
    ``/summon`` slash commands.  This allows ``SessionManager`` to register
    itself without introducing a direct dependency from ``BoltRouter``.
    """

    def __init__(self, web_client: AsyncWebClient | None = None) -> None:
        self._sessions: dict[str, SessionHandle] = {}  # channel_id → handle
        self._command_handler: CommandHandler | None = None
        self._resume_handler: ResumeHandler | None = None
        self._web_client = web_client

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

    def has_handler(self, channel_id: str) -> bool:
        """Return True if a session is registered for this channel."""
        return channel_id in self._sessions

    def has_active_sessions(self) -> bool:
        """Return True if any sessions are currently registered."""
        return bool(self._sessions)

    # ------------------------------------------------------------------
    # Command handler (for /summon slash commands)
    # ------------------------------------------------------------------

    def set_command_handler(self, handler: CommandHandler) -> None:
        """Register a callback for ``/summon`` slash commands."""
        self._command_handler = handler

    def set_resume_handler(self, handler: ResumeHandler) -> None:
        """Register a callback for ``!summon resume`` in unrouted channels."""
        self._resume_handler = handler

    async def dispatch_command(
        self, user_id: str, code: str, respond: Callable[..., Awaitable[None]]
    ) -> None:
        """Route a ``/summon`` slash command to the registered handler."""
        if self._command_handler is not None:
            await self._command_handler(user_id=user_id, code=code, respond=respond)
        else:
            logger.warning("EventDispatcher: /summon received but no command handler set")
            await respond(
                text=":x: Service not ready. Please try again shortly.",
                response_type="ephemeral",
            )

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
            # Fallback: check for !summon resume in unrouted channels
            await self._handle_unrouted_message(event)

    async def _handle_unrouted_message(self, event: dict[str, Any]) -> None:
        """Handle messages in channels with no active session.

        Checks for ``!summon resume`` command and triggers session resume
        if the channel has a completed session.
        """
        text = event.get("text", "")
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")

        match = re.match(r"^!summon\s+resume(?:\s+(\S+))?\s*$", text, re.IGNORECASE)
        if not match:
            return

        target_session_id = match.group(1)
        await self._handle_resume_request(channel_id, user_id, target_session_id)

    async def _handle_resume_request(
        self, channel_id: str, user_id: str, target_session_id: str | None
    ) -> None:
        """Process a !summon resume request from an unrouted channel.

        Delegates all validation and DB access to the resume handler
        (``SessionManager.resume_from_channel``).
        """
        if not self._resume_handler:
            logger.warning("Cannot resume: no resume handler registered")
            return
        try:
            await self._resume_handler(channel_id, user_id, target_session_id)
        except ValueError as e:
            await self._post_error(channel_id, str(e))
        except Exception as e:
            await self._post_error(channel_id, f":x: Failed to resume: {e}")

    async def _post_error(self, channel_id: str, text: str) -> None:
        """Best-effort error message to a channel via SlackClient (redacted)."""
        if not self._web_client:
            logger.warning("Cannot post error to %s: no web_client", channel_id)
            return
        try:
            client = SlackClient(self._web_client, channel_id)
            await client.post(text)
        except Exception as e:
            logger.warning("Failed to post error to %s: %s", channel_id, e)

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
                response_url=response_url,
            )

    async def dispatch_reaction(self, event: dict) -> None:  # type: ignore[type-arg]
        """Route a ``reaction_added`` event to the session's abort callback.

        The channel is extracted from ``event["item"]["channel"]``.  Reactions
        on unknown channels are silently ignored.  Only the authenticated
        session owner can trigger an abort — reactions from other users are
        dropped to prevent cross-user interference.
        """
        channel_id = event.get("item", {}).get("channel", "")
        handle = self._sessions.get(channel_id)
        if handle is not None:
            reactor = event.get("user", "")
            if reactor != handle.authenticated_user_id:
                logger.debug(
                    "EventDispatcher: reaction from %s ignored — session owned by %s",
                    reactor,
                    handle.authenticated_user_id,
                )
                return
            handle.abort_callback()
        else:
            logger.debug(
                "EventDispatcher: no session for channel %s — reaction dropped",
                channel_id,
            )

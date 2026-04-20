"""Event routing layer for the single-Bolt daemon architecture.

The ``EventDispatcher`` maintains a registry of active sessions (keyed by
``channel_id``) and routes incoming Slack events to the correct session.

Design note: asyncio is single-threaded, so all dict mutations and reads are
safe without explicit locking.  ``register`` and ``unregister`` must be called
from the same event loop as the dispatch methods.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.file_handler import (
    MAX_FILE_SIZE,
    WARN_FILE_SIZE,
    classify_file,
    download_file,
    prepare_image_content,
    prepare_text_content,
)
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

# Callback for App Home opened events.
# Arguments: (user_id: str)
AppHomeHandler = Callable[[str], Awaitable[None]]

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
    pending_turns: asyncio.Queue  # type: ignore[type-arg]  # _PendingTurn queue for direct injection


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
        self._app_home_handler: AppHomeHandler | None = None
        self._web_client = web_client
        self._bot_user_id: str | None = None

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

    def set_app_home_handler(self, handler: AppHomeHandler) -> None:
        """Register a callback for ``app_home_opened`` events."""
        self._app_home_handler = handler

    async def dispatch_app_home(self, user_id: str) -> None:
        """Route an app_home_opened event to the registered handler."""
        if self._app_home_handler is not None:
            try:
                await self._app_home_handler(user_id)
            except Exception as e:
                logger.warning("EventDispatcher: app_home handler error for %s: %s", user_id, e)
        else:
            logger.debug("EventDispatcher: app_home_opened received but no handler set")

    def set_bot_user_id(self, bot_user_id: str) -> None:
        """Set the bot's own Slack user ID (for self-upload filtering)."""
        self._bot_user_id = bot_user_id

    async def dispatch_file_shared(self, event: dict) -> None:  # type: ignore[type-arg]
        """Route a file_shared event to the correct session's message queue.

        Security: filters self-uploads and verifies user ownership before
        delegating to _process_file_shared for download and enqueue.
        """
        if not self._web_client:
            logger.debug("dispatch_file_shared: no web_client, dropping event")
            return

        user_id: str = event.get("user_id", "")
        channel_id: str = event.get("channel_id", "")
        file_id: str = event.get("file_id", "")

        # Filter self-uploads (bot-created files: diffs, Write uploads, etc.)
        if self._bot_user_id and user_id == self._bot_user_id:
            logger.debug("dispatch_file_shared: dropping bot self-upload %s", file_id)
            return

        handle = self._sessions.get(channel_id)
        if handle is None:
            logger.debug(
                "dispatch_file_shared: no session for channel %s — file dropped", channel_id
            )
            return

        # Security: verify uploader is the authenticated session owner
        if user_id != handle.authenticated_user_id:
            logger.warning(
                "dispatch_file_shared: file from %s rejected — session owned by %s",
                user_id,
                handle.authenticated_user_id,
            )
            return

        await self._process_file_shared(file_id, handle)

    async def _process_file_shared(self, file_id: str, handle: SessionHandle) -> None:
        """Fetch, classify, download, and enqueue a file for the session.

        Security:
        - File size is checked from files.info metadata BEFORE downloading.
        - Filenames are sanitized by file_handler.sanitize_filename.
        - Download URLs are never logged or stored.
        """
        if not self._web_client:  # guaranteed by dispatch_file_shared, but guard for safety
            return

        # Fetch file metadata before downloading
        try:
            resp = await self._web_client.files_info(file=file_id)
            file_info: dict[str, Any] = resp.get("file", {})
        except Exception as e:
            logger.warning("dispatch_file_shared: files.info failed for %s: %s", file_id, e)
            return

        filename: str = file_info.get("name", "unknown")
        mimetype: str = file_info.get("mimetype", "")
        file_size: int = file_info.get("size", 0)
        url_private: str = file_info.get("url_private_download", "") or file_info.get(
            "url_private", ""
        )

        if not url_private:
            logger.warning("dispatch_file_shared: no download URL for file %s", file_id)
            return

        # Size check BEFORE downloading
        if file_size > MAX_FILE_SIZE:
            logger.warning(
                "dispatch_file_shared: file %s too large (%d > %d bytes), skipping",
                filename,
                file_size,
                MAX_FILE_SIZE,
            )
            return
        if file_size > WARN_FILE_SIZE:
            logger.warning(
                "dispatch_file_shared: large file %s (%d bytes) — downloading",
                filename,
                file_size,
            )

        kind = classify_file(filename, mimetype)
        if kind == "unsupported":
            logger.debug("dispatch_file_shared: unsupported file type %s (%s)", filename, mimetype)
            return

        # Download file content (token from web_client, never logged)
        token: str = self._web_client.token or ""
        try:
            content_bytes = await download_file(url_private, token, max_size=MAX_FILE_SIZE)
        except Exception as e:
            logger.warning(
                "dispatch_file_shared: download failed for %s: %s", filename, type(e).__name__
            )
            return

        # Prepare and enqueue the turn
        from summon_claude.sessions.session import _PendingTurn  # noqa: PLC0415

        if kind == "text":
            text_content = prepare_text_content(filename, content_bytes)
            pending: _PendingTurn = _PendingTurn(message=text_content, pre_sent=False)
        else:  # image
            content_blocks = prepare_image_content(filename, content_bytes, mimetype)
            safe_name = filename.replace("\n", "")[:200]
            pending = _PendingTurn(
                message=f"User shared image: {safe_name}",
                pre_sent=False,
                content_blocks=tuple(content_blocks),
            )

        try:
            handle.pending_turns.put_nowait(pending)
            logger.info(
                "dispatch_file_shared: enqueued %s file %s for session %s",
                kind,
                filename,
                handle.session_id,
            )
        except Exception:
            logger.warning("dispatch_file_shared: queue full for session %s", handle.session_id)

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
        - ``turn_overflow`` → turn-level actions (stop, copy session ID, view cost)
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
        user_id: str = body.get("user", {}).get("id", "")
        trigger_id: str | None = body.get("trigger_id")

        if action_id == "turn_overflow":
            await self._dispatch_turn_overflow(action, handle, channel_id, user_id)
        elif _ASK_USER_RE.fullmatch(action_id):
            action_type: str = action.get("type", "")
            if action_type == "static_select":
                # Single select menu: value is in selected_option.value
                value = (action.get("selected_option") or {}).get("value", "")
                await handle.permission_handler.handle_ask_user_action(
                    value=value,
                    user_id=user_id,
                    trigger_id=trigger_id,
                )
            elif action_type == "multi_static_select":
                # Multi select menu: full current selection list in selected_options
                selected_values = [
                    opt.get("value", "") for opt in (action.get("selected_options") or [])
                ]
                await handle.permission_handler.handle_ask_user_multiselect_action(
                    action_id=action_id,
                    selected_values=selected_values,
                    user_id=user_id,
                )
            else:
                # Button actions (existing behaviour)
                value = action.get("value", "")
                await handle.permission_handler.handle_ask_user_action(
                    value=value,
                    user_id=user_id,
                    trigger_id=trigger_id,
                )
        else:
            # permission_approve / permission_approve_session / permission_deny
            value = action.get("value", "")
            await handle.permission_handler.handle_action(
                value=value,
                user_id=user_id,
            )

    async def _dispatch_turn_overflow(
        self,
        action: dict,
        handle: SessionHandle,
        channel_id: str,
        user_id: str,
    ) -> None:
        """Handle turn overflow menu actions.

        Security: only the authenticated session owner may trigger these actions.
        """
        if user_id != handle.authenticated_user_id:
            logger.warning(
                "EventDispatcher: turn_overflow from %s rejected — session owned by %s",
                user_id,
                handle.authenticated_user_id,
            )
            return

        value: str = action.get("selected_option", {}).get("value", "")

        if value == "turn_stop":
            handle.abort_callback()
        elif value == "turn_copy_sid":
            await self._post_ephemeral(
                channel_id=channel_id,
                user_id=user_id,
                text=f"Session ID: `{handle.session_id}`",
            )
        elif value == "turn_view_cost":
            await self._post_ephemeral(
                channel_id=channel_id,
                user_id=user_id,
                text=f"Session ID: `{handle.session_id}` — use `!cost` for details.",
            )
        else:
            logger.warning("EventDispatcher: unknown turn_overflow value %r", value)

    async def _post_ephemeral(self, channel_id: str, user_id: str, text: str) -> None:
        """Post an ephemeral message to a user in a channel (best-effort)."""
        if not self._web_client:
            logger.warning("Cannot post ephemeral to %s: no web_client", channel_id)
            return
        try:
            await self._web_client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=text,
            )
        except Exception as e:
            logger.warning("Failed to post ephemeral to %s: %s", channel_id, e)

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

    async def dispatch_view_submission(self, view: dict, body: dict) -> None:  # type: ignore[type-arg]
        """Route a modal view submission to the correct session's permission handler.

        Extracts the channel_id from view private_metadata, looks up the session,
        verifies the submitting user, and delegates to handle_ask_user_view_submission.
        """
        user_id: str = body.get("user", {}).get("id", "")

        try:
            meta = json.loads(view.get("private_metadata", "{}"))
            channel_id: str = meta["channel_id"]
        except (KeyError, ValueError, json.JSONDecodeError):
            logger.warning("dispatch_view_submission: malformed private_metadata — dropped")
            return

        handle = self._sessions.get(channel_id)
        if handle is None:
            logger.debug(
                "EventDispatcher: no session for channel %s — view submission dropped", channel_id
            )
            return

        # Security: verify user is the authenticated session owner before processing
        if user_id != handle.authenticated_user_id:
            logger.warning(
                "EventDispatcher: view submission from %s rejected — session owned by %s",
                user_id,
                handle.authenticated_user_id,
            )
            return

        await handle.permission_handler.handle_ask_user_view_submission(view=view, user_id=user_id)

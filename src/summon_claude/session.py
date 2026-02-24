"""Session orchestrator — ties Claude SDK + Slack + permissions + streaming together."""

# pyright: reportArgumentType=false, reportReturnType=false
# slack_sdk and claude_agent_sdk don't ship type stubs

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from datetime import UTC, datetime

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude._formatting import format_file_references
from summon_claude.auth import SessionAuth, generate_session_token, verify_short_code
from summon_claude.channel_manager import ChannelManager
from summon_claude.config import SummonConfig, discover_installed_plugins
from summon_claude.content_display import ContentDisplay
from summon_claude.mcp_tools import create_summon_mcp_server
from summon_claude.permissions import PermissionHandler
from summon_claude.providers.slack import SlackChatProvider
from summon_claude.rate_limiter import RateLimiter
from summon_claude.registry import SessionRegistry
from summon_claude.streamer import ResponseStreamer
from summon_claude.thread_router import ThreadRouter

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_S = 30
_AUTH_POLL_INTERVAL_S = 1.0
_AUTH_TIMEOUT_S = 300  # 5 minutes to authenticate
_QUEUE_POLL_INTERVAL_S = 1.0
_MAX_USER_MESSAGE_CHARS = 10_000

_SYSTEM_PROMPT = {
    "type": "preset",
    "preset": "claude_code",
    "append": (
        "You are running via summon-claude, bridged to a Slack channel. "
        "The user interacts through Slack messages. Format responses for Slack mrkdwn."
    ),
}


@dataclass(frozen=True, slots=True)
class SessionOptions:
    """Options for creating a SummonSession.

    All fields are resolved by the CLI layer before reaching the session.
    """

    session_id: str
    cwd: str
    name: str
    model: str | None = None
    resume: str | None = None


class SummonSession:
    """Orchestrates a Claude Code session bridged to a Slack channel.

    Lifecycle:
        1. Register session in SQLite (status: pending_auth)
        2. Generate auth token + short code, print to terminal
        3. Start Slack Socket Mode handler
        4. Wait for /summon <code> command from an authorized user
        5. Create session channel, post header
        6. Enter message loop: Slack messages -> Claude -> Slack responses
        7. Graceful shutdown on SIGINT/SIGTERM or session end
    """

    def __init__(
        self,
        config: SummonConfig,
        options: SessionOptions,
    ) -> None:
        self._config = config
        self._session_id = options.session_id
        self._cwd = options.cwd
        self._name = options.name
        self._model = options.model
        self._resume = options.resume

        # Will be set during start()
        self._registry: SessionRegistry | None = None
        self._app: AsyncApp | None = None
        self._socket_handler: AsyncSocketModeHandler | None = None
        self._client: AsyncWebClient | None = None
        self._permission_handler: PermissionHandler | None = None
        self._channel_id: str | None = None
        self._auth: SessionAuth | None = None

        # Message queue: Slack user messages -> Claude
        self._message_queue: asyncio.Queue[str] = asyncio.Queue()

        # Shutdown signal
        self._shutdown_event = asyncio.Event()
        self._authenticated_event = asyncio.Event()
        self._authenticated_user_id: str | None = None

        # Session stats
        self._total_cost: float = 0.0
        self._total_turns: int = 0

        # Rate limiter for /summon slash command
        self._rate_limiter = RateLimiter()

    async def prepare_auth(self) -> str:
        """Register session in SQLite and generate auth token.

        Opens and closes its own registry connection (safe to fork after this returns).
        Must be called before start(). Returns the short_code string.
        """
        async with SessionRegistry() as registry:
            await registry.register(
                session_id=self._session_id,
                pid=os.getpid(),
                cwd=self._cwd,
                name=self._name,
                model=self._model,
            )
            await registry.log_event(
                "session_created",
                session_id=self._session_id,
                details={"cwd": self._cwd, "name": self._name, "model": self._model},
            )
            auth = await generate_session_token(registry, self._session_id, self._cwd)
            self._auth = auth
            short_code = auth.short_code
        return short_code

    async def start(self) -> bool:
        """Main entry point. Runs the full session lifecycle."""
        async with SessionRegistry() as registry:
            self._registry = registry

            # Set up Slack app
            self._client = AsyncWebClient(token=self._config.slack_bot_token)
            self._app = self._build_slack_app()
            self._socket_handler = AsyncSocketModeHandler(self._app, self._config.slack_app_token)

            # Install signal handlers
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(sig, self._handle_signal)

            logger.info("Waiting for Slack authentication...")

            auth_task = asyncio.create_task(self._wait_for_auth())
            socket_task = asyncio.create_task(self._socket_handler.start_async())
            done, pending = await asyncio.wait(
                {auth_task, socket_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Cancel any still-running tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            # Re-raise unexpected exceptions from auth_task
            for task in done:
                if task is auth_task and not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        raise exc

            if not self._authenticated_event.is_set():
                logger.error("Authentication failed or timed out")
                await registry.update_status(
                    self._session_id, "errored", error_message="Authentication timed out"
                )
                await registry.log_event(
                    "session_errored",
                    session_id=self._session_id,
                    details={"reason": "Authentication timed out"},
                )
                return False

            # Auth succeeded — create channel and run session
            await self._run_session()
            return True

    async def _wait_for_auth(self) -> None:
        """Poll until auth is confirmed, timed out, or shutdown is requested."""
        elapsed = 0.0
        next_countdown = 30.0
        while elapsed < _AUTH_TIMEOUT_S:
            if self._authenticated_event.is_set():
                return
            if self._shutdown_event.is_set():
                logger.info("Shutdown requested. Cancelling authentication.")
                if self._auth:
                    try:
                        assert self._registry is not None
                        await self._registry.delete_pending_token(self._auth.short_code)
                    except Exception:
                        pass
                return
            await asyncio.sleep(_AUTH_POLL_INTERVAL_S)
            elapsed += _AUTH_POLL_INTERVAL_S
            if elapsed >= next_countdown:
                remaining = int(_AUTH_TIMEOUT_S - elapsed)
                if remaining > 0:
                    logger.info("Waiting for authentication... %ds remaining", remaining)
                next_countdown += 30.0

        logger.info("Authentication timed out. No /summon command received within 5 minutes.")
        if self._auth and self._registry:
            try:
                await self._registry.delete_pending_token(self._auth.short_code)
            except Exception:
                pass
        logger.warning("Auth timeout after %.0f seconds", elapsed)
        self._shutdown_event.set()

    async def _run_session(self) -> None:
        """Create channel, connect Claude, run message loop."""
        assert self._registry is not None
        assert self._client is not None

        provider = SlackChatProvider(self._client)
        channel_manager = ChannelManager(provider, self._config.channel_prefix)
        channel_id, channel_name = await channel_manager.create_session_channel(self._name)
        self._channel_id = channel_id
        logger.info("Authenticated! Session channel: #%s", channel_name)

        await self._registry.update_status(
            self._session_id,
            "active",
            slack_channel_id=channel_id,
            slack_channel_name=channel_name,
            authenticated_at=datetime.now(UTC).isoformat(),
        )
        await self._registry.log_event(
            "session_active",
            session_id=self._session_id,
            user_id=self._authenticated_user_id,
            details={"channel_id": channel_id},
        )

        await channel_manager.post_session_header(
            channel_id,
            {
                "cwd": self._cwd,
                "model": self._model,
                "session_id": self._session_id,
            },
        )

        # Notify the authenticating user
        if self._authenticated_user_id:
            try:
                await self._client.chat_postEphemeral(
                    channel=channel_id,
                    user=self._authenticated_user_id,
                    text=f"Session ready! Welcome to <#{channel_id}>.",
                )
            except Exception as e:
                logger.debug("Failed to post ephemeral welcome: %s", e)

        logger.info("Connected to channel (id=%s)", channel_id)

        router = ThreadRouter(provider, channel_id)
        self._permission_handler = PermissionHandler(router, self._config)

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._run_message_loop(router, provider)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

            await self._shutdown(channel_manager, channel_id)

    async def _run_message_loop(self, router: ThreadRouter, provider: SlackChatProvider) -> None:
        """Listen for Slack messages and forward them to Claude."""
        assert self._registry is not None
        assert self._client is not None
        assert self._permission_handler is not None
        assert self._channel_id is not None

        slack_mcp = create_summon_mcp_server(router)

        agent_kwargs: dict = {
            "cwd": self._cwd,
            "resume": self._resume,
            "system_prompt": _SYSTEM_PROMPT,
            "include_partial_messages": True,
            "setting_sources": ["user", "project"],
            "plugins": discover_installed_plugins(),
            "can_use_tool": self._permission_handler.handle,
            "mcp_servers": {"summon-slack": slack_mcp},
        }
        if self._model is not None:
            agent_kwargs["model"] = self._model
        options = ClaudeAgentOptions(**agent_kwargs)

        display = ContentDisplay(self._config.max_inline_chars)
        streamer = ResponseStreamer(router=router, display=display)

        async with ClaudeSDKClient(options) as claude:
            # Store Claude session ID if available
            try:
                server_info = await claude.get_server_info()
                if server_info:
                    claude_session_id = server_info.get("session_id", "")
                    if claude_session_id:
                        await self._registry.update_status(
                            self._session_id, "active", claude_session_id=claude_session_id
                        )
            except Exception as e:
                logger.debug("Could not retrieve Claude session ID: %s", e)

            while not self._shutdown_event.is_set():
                try:
                    user_message = await asyncio.wait_for(
                        self._message_queue.get(), timeout=_QUEUE_POLL_INTERVAL_S
                    )
                except TimeoutError:
                    continue

                if not user_message:
                    continue

                await self._handle_user_message(claude, router, streamer, provider, user_message)

    async def _handle_user_message(
        self,
        claude: ClaudeSDKClient,
        router: ThreadRouter,
        streamer: ResponseStreamer,
        provider: SlackChatProvider,
        message: str,
    ) -> None:
        """Forward a single user message to Claude and stream the response."""
        assert self._registry is not None
        assert self._channel_id is not None

        logger.info("Forwarding message to Claude (%d chars)", len(message))

        try:
            self._total_turns += 1
            await router.start_turn(self._total_turns)
            await claude.query(message)
            result = await streamer.stream_with_flush(claude.receive_response())
            if result:
                cost = result.total_cost_usd or 0.0
                self._total_cost += cost
                await self._registry.record_turn(self._session_id, cost)
                summary = router.generate_turn_summary()
                await router.update_turn_summary(summary)
        except Exception as e:
            logger.exception("Error during Claude response: %s", e)
            error_type = type(e).__name__
            await self._registry.log_event(
                "session_errored",
                session_id=self._session_id,
                details={
                    "error_type": error_type,
                    "error": f"{error_type}: {str(e)[:200]}",
                },
            )
            try:
                await provider.post_message(
                    self._channel_id,
                    ":warning: An error occurred while processing your request.",
                )
            except Exception as e2:
                logger.warning("Failed to post error notification: %s", e2)

    async def _heartbeat_loop(self) -> None:
        """Update registry heartbeat every 30 seconds."""
        assert self._registry is not None
        while not self._shutdown_event.is_set():
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                await self._registry.heartbeat(self._session_id)
            except Exception as e:
                logger.warning("Heartbeat failed: %s", e)

    async def _shutdown(self, channel_manager: ChannelManager, channel_id: str) -> None:
        """Gracefully shut down the session."""
        assert self._registry is not None
        assert self._client is not None

        logger.info(
            "Session ended. Turns: %d, Total cost: $%.4f", self._total_turns, self._total_cost
        )

        # Post session summary to channel
        try:
            provider = SlackChatProvider(self._client)
            await provider.post_message(
                channel_id,
                (
                    f":wave: Session ending.\n"
                    f"Turns: {self._total_turns} | "
                    f"Cost: ${self._total_cost:.4f}"
                ),
            )
        except Exception as e:
            logger.warning("Failed to post session summary: %s", e)

        # Archive channel
        await channel_manager.archive_session_channel(channel_id)

        # Update registry
        await self._registry.update_status(
            self._session_id,
            "completed",
            ended_at=datetime.now(UTC).isoformat(),
        )
        await self._registry.log_event(
            "session_ended",
            session_id=self._session_id,
            details={"total_turns": self._total_turns, "total_cost_usd": self._total_cost},
        )

        # Disconnect Socket Mode
        if self._socket_handler:
            try:
                await self._socket_handler.close_async()
            except Exception as e:
                logger.debug("Socket Mode cleanup error: %s", e)

    def _build_slack_app(self) -> AsyncApp:
        """Build and configure the Slack Bolt async app."""
        app = AsyncApp(
            token=self._config.slack_bot_token,
            signing_secret=self._config.slack_signing_secret,
        )
        app.command("/summon")(self._on_summon_command)
        app.event("message")(self._on_message_event)
        app.action("permission_approve")(self._on_permission_action)
        app.action("permission_deny")(self._on_permission_action)
        return app

    async def _on_summon_command(self, ack, command, respond) -> None:
        """Handle the /summon slash command."""
        await ack()

        if self._authenticated_event.is_set():
            await respond(
                text="This session is already active.",
                response_type="ephemeral",
            )
            return

        user_id = command.get("user_id", "")

        if not self._rate_limiter.check(user_id):
            await respond(text="Please wait before trying again.", response_type="ephemeral")
            return

        if self._config.allowed_user_ids and user_id not in self._config.allowed_user_ids:
            await respond(text="You are not authorized to use summon.", response_type="ephemeral")
            return

        text = command.get("text", "").strip()
        if not text:
            await respond(
                text="Usage: `/summon <code>` — enter the 6-character code shown in terminal.",
                response_type="ephemeral",
            )
            return

        if self._registry is None:
            return

        await self._registry.log_event(
            "auth_attempted",
            user_id=user_id,
        )

        auth_result = await verify_short_code(self._registry, text)
        if not auth_result:
            await self._registry.log_event("auth_failed", user_id=user_id)
            await respond(
                text=":x: Invalid or expired code. Run `summon start` to get a new code.",
                response_type="ephemeral",
            )
            return

        self._authenticated_user_id = user_id
        self._authenticated_event.set()
        self._auth = None  # clear token from memory after successful auth

        await self._registry.log_event(
            "auth_succeeded", session_id=self._session_id, user_id=user_id
        )
        await respond(
            text=":rocket: Authenticated! Creating your session channel...",
            response_type="ephemeral",
        )

    async def _on_message_event(self, event, say) -> None:
        """Handle incoming Slack messages."""
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        text = event.get("text", "")
        subtype = event.get("subtype")

        if subtype or not text or not user_id:
            return
        if channel_id != self._channel_id:
            return

        if self._config.allowed_user_ids and user_id not in self._config.allowed_user_ids:
            logger.debug("Ignoring message from unauthorized user %s", user_id)
            return

        if len(text) > _MAX_USER_MESSAGE_CHARS:
            logger.warning("Message from %s truncated (%d chars)", user_id, len(text))
            text = text[:_MAX_USER_MESSAGE_CHARS] + "\n[message truncated]"

        files = event.get("files", [])
        full_text = text
        if files:
            file_context = format_file_references(files)
            if file_context:
                full_text = f"{text}\n\n{file_context}"

        await self._message_queue.put(full_text)

    async def _on_permission_action(self, ack, action, body) -> None:
        """Handle permission approve/deny button clicks."""
        await ack()
        if not (self._permission_handler and self._channel_id):
            return
        user_id = body.get("user", {}).get("id", "")
        message = body.get("message", {})
        channel_id = body.get("channel", {}).get("id", self._channel_id)
        action_id = action.get("action_id", "")
        await self._permission_handler.handle_action(
            action_id=action_id,
            value=action.get("value", ""),
            user_id=user_id,
            channel_id=channel_id,
            message_ts=message.get("ts", ""),
        )

    def _handle_signal(self) -> None:
        """Signal handler for SIGINT/SIGTERM — triggers graceful shutdown."""
        logger.info("Received shutdown signal")
        self._shutdown_event.set()
        # Put a sentinel to unblock the message queue
        self._message_queue.put_nowait("")


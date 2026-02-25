"""Session orchestrator — ties Claude SDK + Slack + permissions + streaming together."""

# pyright: reportArgumentType=false, reportReturnType=false
# slack_sdk and claude_agent_sdk don't ship type stubs

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude._formatting import format_file_references
from summon_claude.auth import SessionAuth, verify_short_code
from summon_claude.channel_manager import ChannelManager
from summon_claude.commands import CommandContext, CommandRegistry, build_registry
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
_CLEANUP_TIMEOUT_S = 10.0

# Patterns that may appear in exception messages and should not be stored in the audit log
_SECRET_PATTERN = re.compile(r"xox[a-z]-[A-Za-z0-9\-]+|sk-ant-[A-Za-z0-9\-]+")

_SYSTEM_PROMPT = {
    "type": "preset",
    "preset": "claude_code",
    "append": (
        "You are running via summon-claude, bridged to a Slack channel. "
        "The user interacts through Slack messages. Format responses for Slack mrkdwn."
    ),
}

AuthResult = Literal["authenticated", "timed_out", "shutdown"]


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


@dataclass(frozen=True, slots=True)
class _SessionRuntime:
    registry: SessionRegistry
    client: AsyncWebClient
    provider: SlackChatProvider
    permission_handler: PermissionHandler
    channel_id: str
    socket_handler: AsyncSocketModeHandler


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
        auth: SessionAuth,
    ) -> None:
        self._config = config
        self._session_id = options.session_id
        self._cwd = options.cwd
        self._name = options.name
        self._model = options.model
        self._resume = options.resume

        self._auth: SessionAuth | None = auth
        self._command_registry: CommandRegistry = build_registry()
        self._session_start_time: datetime = datetime.now(UTC)

        # Message queue: Slack user messages -> Claude
        self._message_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        # Shutdown signal
        self._shutdown_event = asyncio.Event()
        self._authenticated_event = asyncio.Event()
        self._authenticated_user_id: str | None = None
        # Tracks whether _shutdown() completed successfully
        self._shutdown_completed: bool = False

        # Session stats
        self._total_cost: float = 0.0
        self._total_turns: int = 0

        # Rate limiter for /summon slash command
        self._rate_limiter = RateLimiter()

    async def start(self) -> bool:  # noqa: PLR0912, PLR0915
        """Main entry point. Runs the full session lifecycle."""
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

            try:
                # Set up Slack app
                client = AsyncWebClient(token=self._config.slack_bot_token)
                app = AsyncApp(
                    token=self._config.slack_bot_token,
                    signing_secret=self._config.slack_signing_secret,
                )
                socket_handler = AsyncSocketModeHandler(app, self._config.slack_app_token)

                # Register /summon handler before socket start
                async def _on_summon_command(ack, command, respond) -> None:
                    await ack()

                    if self._authenticated_event.is_set():
                        await respond(
                            text="This session is already active.",
                            response_type="ephemeral",
                        )
                        return

                    user_id = command.get("user_id", "")

                    if not self._rate_limiter.check(user_id):
                        await respond(
                            text="Please wait before trying again.", response_type="ephemeral"
                        )
                        return

                    allowed = self._config.allowed_user_ids
                    if allowed and user_id not in allowed:
                        await respond(
                            text="You are not authorized to use summon.",
                            response_type="ephemeral",
                        )
                        return

                    text = command.get("text", "").strip()
                    if not text:
                        await respond(
                            text="Usage: `/summon <code>` — enter the code shown in terminal.",
                            response_type="ephemeral",
                        )
                        return

                    await registry.log_event(
                        "auth_attempted",
                        user_id=user_id,
                    )

                    auth_result = await verify_short_code(registry, text)
                    if not auth_result:
                        await registry.log_event("auth_failed", user_id=user_id)
                        await respond(
                            text=(
                                ":x: Invalid or expired code."
                                " Run `summon start` to get a new code."
                            ),
                            response_type="ephemeral",
                        )
                        return

                    self._authenticated_user_id = user_id
                    self._authenticated_event.set()
                    self._auth = None  # clear token from memory after successful auth

                    await registry.log_event(
                        "auth_succeeded", session_id=self._session_id, user_id=user_id
                    )
                    await respond(
                        text=":rocket: Authenticated! Creating your session channel...",
                        response_type="ephemeral",
                    )

                app.command("/summon")(_on_summon_command)

                # Install signal handlers
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    loop.add_signal_handler(sig, self._handle_signal)

                logger.info("Waiting for Slack authentication...")

                auth_task = asyncio.create_task(self._wait_for_auth())
                socket_task = asyncio.create_task(socket_handler.start_async())
                done, pending = await asyncio.wait(
                    {auth_task, socket_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Cancel any still-running tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception) as e:
                        logger.debug("Pending task cleanup: %s", e)
                # Re-raise unexpected exceptions from auth_task
                for task in done:
                    if task is auth_task and not task.cancelled():
                        exc = task.exception()
                        if exc is not None:
                            raise exc

                auth_status: AuthResult = (
                    auth_task.result() if not auth_task.cancelled() else "shutdown"
                )

                if auth_status == "authenticated":
                    await self._run_session(registry, client, app, socket_handler)
                    return True
                if auth_status == "timed_out":
                    if self._auth:
                        try:
                            await registry.delete_pending_token(self._auth.short_code)
                        except Exception as e:
                            logger.debug("Failed to delete pending token on timeout: %s", e)
                    logger.error("Authentication timed out")
                    await registry.update_status(
                        self._session_id, "errored", error_message="Authentication timed out"
                    )
                    await registry.log_event(
                        "session_errored",
                        session_id=self._session_id,
                        details={"reason": "Authentication timed out"},
                    )
                    self._shutdown_completed = True
                    return False
                # shutdown
                if self._auth:
                    try:
                        await registry.delete_pending_token(self._auth.short_code)
                    except Exception as e:
                        logger.debug("Failed to delete pending token on shutdown: %s", e)
                return False

            except asyncio.CancelledError:
                logger.info("Session task cancelled during startup")
                if self._auth:
                    try:
                        await registry.delete_pending_token(self._auth.short_code)
                    except Exception as e:
                        logger.debug("Failed to delete pending token on cancel: %s", e)
                raise
            finally:
                if not self._shutdown_completed:
                    try:
                        await registry.update_status(
                            self._session_id,
                            "errored",
                            error_message="Session terminated unexpectedly",
                            ended_at=datetime.now(UTC).isoformat(),
                        )
                    except Exception as e:
                        logger.warning("Failed to update registry on unexpected termination: %s", e)

    async def _wait_for_auth(self) -> AuthResult:
        """Poll until auth is confirmed, timed out, or shutdown is requested."""
        elapsed = 0.0
        next_countdown = 30.0
        while elapsed < _AUTH_TIMEOUT_S:
            if self._authenticated_event.is_set():
                return "authenticated"
            if self._shutdown_event.is_set():
                logger.info("Shutdown requested. Cancelling authentication.")
                return "shutdown"
            await asyncio.sleep(_AUTH_POLL_INTERVAL_S)
            elapsed += _AUTH_POLL_INTERVAL_S
            if elapsed >= next_countdown:
                remaining = int(_AUTH_TIMEOUT_S - elapsed)
                if remaining > 0:
                    logger.info("Waiting for authentication... %ds remaining", remaining)
                next_countdown += 30.0

        logger.info("Authentication timed out. No /summon command received within 5 minutes.")
        logger.warning("Auth timeout after %.0f seconds", elapsed)
        self._shutdown_event.set()
        return "timed_out"

    async def _run_session(  # noqa: PLR0915
        self,
        registry: SessionRegistry,
        client: AsyncWebClient,
        app: AsyncApp,
        socket_handler: AsyncSocketModeHandler,
    ) -> None:
        """Create channel, connect Claude, run message loop."""
        provider = SlackChatProvider(client)
        channel_manager = ChannelManager(provider, self._config.channel_prefix)
        channel_id, channel_name = await channel_manager.create_session_channel(self._name)
        logger.info("Authenticated! Session channel: #%s", channel_name)

        # Invite the authenticating user to the private channel
        if self._authenticated_user_id:
            await channel_manager.invite_user_to_channel(channel_id, self._authenticated_user_id)

        await registry.update_status(
            self._session_id,
            "active",
            slack_channel_id=channel_id,
            slack_channel_name=channel_name,
            authenticated_at=datetime.now(UTC).isoformat(),
        )
        await registry.log_event(
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
                await client.chat_postEphemeral(
                    channel=channel_id,
                    user=self._authenticated_user_id,
                    text=f"Session ready! Welcome to <#{channel_id}>.",
                )
            except Exception as e:
                logger.debug("Failed to post ephemeral welcome: %s", e)

        logger.info("Connected to channel (id=%s)", channel_id)

        router = ThreadRouter(provider, channel_id)
        permission_handler = PermissionHandler(router, self._config)

        rt = _SessionRuntime(
            registry=registry,
            client=client,
            provider=provider,
            permission_handler=permission_handler,
            channel_id=channel_id,
            socket_handler=socket_handler,
        )

        # Register post-auth callbacks as closures capturing rt
        async def _on_message_event(event, say) -> None:
            event_channel_id = event.get("channel", "")
            user_id = event.get("user", "")
            text = event.get("text", "")
            subtype = event.get("subtype")

            if subtype or not text or not user_id:
                return
            if event_channel_id != rt.channel_id:
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

            thread_ts = event.get("ts")

            # Check for command prefix
            parsed = self._command_registry.parse(full_text)
            if parsed is not None:
                await self._dispatch_command(rt, parsed[0], parsed[1], user_id, thread_ts)
                return

            await self._message_queue.put((full_text, None))

        async def _on_permission_action(ack, action, body) -> None:
            await ack()
            user_id = body.get("user", {}).get("id", "")
            message = body.get("message", {})
            action_channel_id = body.get("channel", {}).get("id", rt.channel_id)
            action_id = action.get("action_id", "")
            await rt.permission_handler.handle_action(
                action_id=action_id,
                value=action.get("value", ""),
                user_id=user_id,
                channel_id=action_channel_id,
                message_ts=message.get("ts", ""),
            )

        app.event("message")(_on_message_event)
        app.action("permission_approve")(_on_permission_action)
        app.action("permission_deny")(_on_permission_action)

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(rt))
        try:
            await self._run_message_loop(rt, router, provider)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception) as e:
                logger.debug("Heartbeat task cleanup: %s", e)

            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                current_task.uncancel()

            await self._shutdown(rt, channel_manager)

    async def _run_message_loop(
        self, rt: _SessionRuntime, router: ThreadRouter, provider: SlackChatProvider
    ) -> None:
        """Listen for Slack messages and forward them to Claude."""
        slack_mcp = create_summon_mcp_server(router)

        options = ClaudeAgentOptions(
            cwd=self._cwd,
            resume=self._resume,
            system_prompt=_SYSTEM_PROMPT,
            include_partial_messages=True,
            setting_sources=["user", "project"],
            plugins=discover_installed_plugins(),
            can_use_tool=rt.permission_handler.handle,
            mcp_servers={"summon-slack": slack_mcp},
            model=self._model,
        )

        display = ContentDisplay(self._config.max_inline_chars)
        streamer = ResponseStreamer(router=router, display=display)

        async with ClaudeSDKClient(options) as claude:
            # Store Claude session ID if available
            server_info = None
            try:
                server_info = await claude.get_server_info()
                if server_info:
                    claude_session_id = server_info.get("session_id", "")
                    if claude_session_id:
                        await rt.registry.update_status(
                            self._session_id, "active", claude_session_id=claude_session_id
                        )
            except Exception as e:
                logger.debug("Could not retrieve Claude session ID: %s", e)

            # Initialize command registry with passthrough commands from SDK
            self._command_registry = build_registry()
            if server_info:
                commands = server_info.get("commands", [])
                if commands:
                    self._command_registry.set_passthrough_commands(commands)

            while not self._shutdown_event.is_set():
                try:
                    item = await asyncio.wait_for(
                        self._message_queue.get(), timeout=_QUEUE_POLL_INTERVAL_S
                    )
                except TimeoutError:
                    continue

                user_message, thread_ts = item
                if not user_message:
                    continue

                await self._handle_user_message(
                    rt, claude, router, streamer, provider, user_message, thread_ts
                )

    async def _handle_user_message(
        self,
        rt: _SessionRuntime,
        claude: ClaudeSDKClient,
        router: ThreadRouter,
        streamer: ResponseStreamer,
        provider: SlackChatProvider,
        message: str,
        _thread_ts: str | None = None,
    ) -> None:
        """Forward a single user message to Claude and stream the response."""
        logger.info("Forwarding message to Claude (%d chars)", len(message))

        try:
            self._total_turns += 1
            await router.start_turn(self._total_turns)
            await claude.query(message)
            result = await streamer.stream_with_flush(claude.receive_response())
            if result:
                cost = result.total_cost_usd or 0.0
                self._total_cost += cost
                await rt.registry.record_turn(self._session_id, cost)
                summary = router.generate_turn_summary()
                await router.update_turn_summary(summary)
        except Exception as e:
            logger.exception("Error during Claude response: %s", e)
            error_type = type(e).__name__
            await rt.registry.log_event(
                "session_errored",
                session_id=self._session_id,
                details={
                    "error_type": error_type,
                    "error": _SECRET_PATTERN.sub("***", f"{error_type}: {str(e)[:200]}"),
                },
            )
            try:
                await provider.post_message(
                    rt.channel_id,
                    ":warning: An error occurred while processing your request.",
                )
            except Exception as e2:
                logger.warning("Failed to post error notification: %s", e2)

    async def _heartbeat_loop(self, rt: _SessionRuntime) -> None:
        """Update registry heartbeat every 30 seconds."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                await rt.registry.heartbeat(self._session_id)
            except Exception as e:
                logger.warning("Heartbeat failed: %s", e)

    async def _shutdown(
        self, rt: _SessionRuntime, channel_manager: ChannelManager
    ) -> None:
        """Gracefully shut down the session."""
        logger.info(
            "Session ended. Turns: %d, Total cost: $%.4f", self._total_turns, self._total_cost
        )

        # Post session summary to channel
        try:
            await asyncio.wait_for(
                rt.provider.post_message(
                    rt.channel_id,
                    (
                        f":wave: Session ending.\n"
                        f"Turns: {self._total_turns} | "
                        f"Cost: ${self._total_cost:.4f}"
                    ),
                ),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("Failed to post session summary: %s", e)

        # Archive channel
        try:
            await asyncio.wait_for(
                channel_manager.archive_session_channel(rt.channel_id),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("Failed to archive session channel: %s", e)

        # Update registry
        try:
            await asyncio.wait_for(
                rt.registry.update_status(
                    self._session_id,
                    "completed",
                    ended_at=datetime.now(UTC).isoformat(),
                ),
                timeout=_CLEANUP_TIMEOUT_S,
            )
            self._shutdown_completed = True
            await asyncio.wait_for(
                rt.registry.log_event(
                    "session_ended",
                    session_id=self._session_id,
                    details={"total_turns": self._total_turns, "total_cost_usd": self._total_cost},
                ),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("Failed to update registry on shutdown: %s", e)

        # Disconnect Socket Mode
        try:
            await asyncio.wait_for(
                rt.socket_handler.close_async(),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception as e:
            logger.debug("Socket Mode cleanup error: %s", e)

    async def _dispatch_command(
        self,
        rt: _SessionRuntime,
        name: str,
        args: list[str],
        user_id: str,
        thread_ts: str | None,
    ) -> None:
        """Dispatch a !-prefixed command and post the result as a threaded reply."""
        ctx = CommandContext(
            channel_id=rt.channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
            provider=rt.provider,
            turns=self._total_turns,
            cost_usd=self._total_cost,
            start_time=self._session_start_time,
            model=self._model,
            session_id=self._session_id,
            metadata={"registry": self._command_registry},
        )

        try:
            result = await self._command_registry.dispatch(name, args, ctx)
        except Exception as e:
            logger.exception("Command dispatch error for !%s: %s", name, e)
            try:
                await rt.provider.post_message(
                    rt.channel_id,
                    f":warning: Error executing `!{name}`.",
                    thread_ts=thread_ts,
                )
            except Exception as e2:
                logger.warning("Failed to post command error: %s", e2)
            return

        # Handle model change from !model
        if "new_model" in result.metadata:
            self._model = result.metadata["new_model"]

        # Handle shutdown signal from !end/!quit/!exit/!logout
        if result.metadata.get("shutdown"):
            if result.text:
                try:
                    await rt.provider.post_message(
                        rt.channel_id, result.text, thread_ts=thread_ts
                    )
                except Exception as e:
                    logger.warning("Failed to post shutdown message: %s", e)
            self._shutdown_event.set()
            self._message_queue.put_nowait(("", None))
            return

        # Pass-through: translate !cmd args -> /cmd args and enqueue
        if not result.suppress_queue:
            slash_message = f"/{name}" + (" " + " ".join(args) if args else "")
            if len(slash_message) > _MAX_USER_MESSAGE_CHARS:
                slash_message = slash_message[:_MAX_USER_MESSAGE_CHARS]
            try:
                await rt.provider.post_message(
                    rt.channel_id,
                    f":gear: Running `{slash_message}`...",
                    thread_ts=thread_ts,
                )
            except Exception as e:
                logger.warning("Failed to post passthrough ack: %s", e)
            await self._message_queue.put((slash_message, None))
            return

        # Post the response text in thread
        if result.text:
            try:
                await rt.provider.post_message(
                    rt.channel_id, result.text, thread_ts=thread_ts
                )
            except Exception as e:
                logger.warning("Failed to post command response: %s", e)

    def _handle_signal(self) -> None:
        """Signal handler for SIGINT/SIGTERM — triggers graceful shutdown."""
        if self._shutdown_event.is_set():
            logger.warning("Received second shutdown signal — forcing exit")
            # Best-effort flush before hard exit (os._exit skips finally/atexit)
            import sys  # noqa: PLC0415

            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)
        logger.info("Received shutdown signal")
        self._shutdown_event.set()
        # Put a sentinel to unblock the message queue
        self._message_queue.put_nowait(("", None))

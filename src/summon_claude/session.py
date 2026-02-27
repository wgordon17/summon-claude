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

import click
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude._formatting import format_file_references
from summon_claude.auth import SessionAuth, verify_short_code
from summon_claude.channel_manager import ChannelManager, _get_git_branch
from summon_claude.commands import CommandContext, CommandRegistry, build_registry
from summon_claude.config import SummonConfig, discover_installed_plugins
from summon_claude.content_display import ContentDisplay, _split_text
from summon_claude.context import ContextUsage
from summon_claude.mcp_tools import create_summon_mcp_server
from summon_claude.permissions import PermissionHandler
from summon_claude.providers.slack import SlackChatProvider
from summon_claude.rate_limiter import RateLimiter
from summon_claude.registry import SessionRegistry
from summon_claude.socket_health import SocketHealthMonitor
from summon_claude.streamer import ResponseStreamer
from summon_claude.thread_router import ThreadRouter

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_S = 30
_AUTH_POLL_INTERVAL_S = 1.0
_AUTH_TIMEOUT_S = 300  # 5 minutes to authenticate
_QUEUE_POLL_INTERVAL_S = 1.0
_MAX_USER_MESSAGE_CHARS = 10_000
_CLEANUP_TIMEOUT_S = 10.0
_WATCHDOG_CHECK_INTERVAL_S = 15
_WATCHDOG_THRESHOLD_S = 90
_OS_WATCHDOG_TIMEOUT_S = 120

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
    channel_manager: ChannelManager


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
        self._last_context: ContextUsage | None = None
        self._last_model_seen: str | None = None

        # Turn abort infrastructure
        self._current_turn_task: asyncio.Task | None = None
        self._abort_event = asyncio.Event()

        # Rate limiter for /summon slash command
        self._rate_limiter = RateLimiter()

        # Message dispatch — early handler delegates to this once registered
        self._message_handler: list = []  # holds [handler_fn] once set

        # Socket resilience state
        self._active_socket_handler: AsyncSocketModeHandler | None = None
        self._disconnect_reason: str | None = None  # one-way latch: set once, read in _shutdown
        self._claude_session_id: str | None = None
        self._last_heartbeat_time: float = 0.0
        self._health_monitor: SocketHealthMonitor | None = None

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

            socket_handler: AsyncSocketModeHandler | None = None
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
                                ":x: Invalid or expired code. Run `summon start` to get a new code."
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

                # Register a catch-all message handler early so Bolt doesn't
                # warn about "Unhandled request" for message subtypes like
                # channel_join that fire before post-auth handlers are registered.
                # The real message processing logic is added after auth via
                # _register_event_handlers which populates self._message_handler.
                async def _on_message_event_early(event, say) -> None:
                    if self._message_handler:
                        await self._message_handler[0](event, say)

                app.event("message")(_on_message_event_early)

                # Install signal handlers
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    loop.add_signal_handler(sig, self._handle_signal)

                logger.info("Waiting for Slack authentication...")
                assert socket_handler is not None
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
                    click.echo("Authenticated! Setting up session...")
                    await self._run_session(registry, client, app, socket_handler)
                    return True
                if auth_status == "timed_out":
                    if self._auth:
                        try:
                            await registry.delete_pending_token(self._auth.short_code)
                        except Exception as e:
                            logger.debug("Failed to delete pending token on timeout: %s", e)
                    if socket_handler is not None:
                        try:
                            await socket_handler.close_async()
                        except Exception as e:
                            logger.debug("Socket Mode cleanup error on timeout: %s", e)
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
                if socket_handler is not None:
                    try:
                        await socket_handler.close_async()
                    except Exception as e:
                        logger.debug("Socket Mode cleanup error on shutdown: %s", e)
                return False

            except asyncio.CancelledError:
                logger.info("Session task cancelled during startup")
                if self._auth:
                    try:
                        await registry.delete_pending_token(self._auth.short_code)
                    except Exception as e:
                        logger.debug("Failed to delete pending token on cancel: %s", e)
                if socket_handler is not None:
                    try:
                        await socket_handler.close_async()
                    except Exception as e:
                        logger.debug("Socket Mode cleanup error on cancel: %s", e)
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
        next_countdown = 15.0
        while elapsed < _AUTH_TIMEOUT_S:
            if self._authenticated_event.is_set():
                return "authenticated"
            if self._shutdown_event.is_set():
                logger.info("Shutdown requested. Cancelling authentication.")
                return "shutdown"
            await asyncio.sleep(_AUTH_POLL_INTERVAL_S)
            elapsed += _AUTH_POLL_INTERVAL_S
            if elapsed >= next_countdown:
                remaining = _AUTH_TIMEOUT_S - elapsed
                if remaining > 0:
                    click.echo(f"Waiting for authentication... {remaining:.0f}s remaining")
                next_countdown += 15.0

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

        git_branch = _get_git_branch(self._cwd)
        await channel_manager.set_session_topic(
            channel_id,
            model=self._model,
            cwd=self._cwd,
            git_branch=git_branch,
            context=None,
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
        permission_handler = PermissionHandler(
            router, self._config, self._authenticated_user_id or ""
        )

        rt = _SessionRuntime(
            registry=registry,
            client=client,
            provider=provider,
            permission_handler=permission_handler,
            channel_id=channel_id,
            socket_handler=socket_handler,
            channel_manager=channel_manager,
        )
        self._active_socket_handler = socket_handler

        # Register post-auth event handlers
        self._register_event_handlers(app, rt)

        # Reconnect callback — uses nonlocal rt so closures see the updated runtime
        async def _on_reconnect_needed() -> None:
            nonlocal rt
            new_rt = await self._reconnect_socket(rt)
            rt = new_rt
            health_monitor.update_handler(new_rt.socket_handler)

        # Exhaustion callback — sets shutdown event + reason (runs synchronously in health task)
        def _on_exhausted() -> None:
            self._disconnect_reason = "reconnect_exhausted"
            self._shutdown_event.set()
            self._message_queue.put_nowait(("", None))

        health_monitor = SocketHealthMonitor(
            socket_handler=socket_handler,
            on_reconnect_needed=_on_reconnect_needed,
            on_exhausted=_on_exhausted,
        )
        self._health_monitor = health_monitor

        # Start OS-level watchdog
        self._start_os_watchdog()
        self._last_heartbeat_time = asyncio.get_running_loop().time()

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(rt))
        health_task = asyncio.create_task(health_monitor.run())
        watchdog_task = asyncio.create_task(self._watchdog_loop())
        try:
            await self._run_message_loop(rt, router, provider)
        finally:
            health_task.cancel()
            heartbeat_task.cancel()
            watchdog_task.cancel()
            for task in (health_task, heartbeat_task, watchdog_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception) as e:
                    logger.debug("Task cleanup: %s", e)

            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling() > 0:
                # uncancel() allows the _shutdown() awaits below to complete even if
                # this task was cancelled (e.g., by signal handling or asyncio cleanup).
                # Without this, awaiting _shutdown() would immediately re-raise
                # CancelledError before cleanup can finish.
                current_task.uncancel()

            # Use latest rt (may have been updated by reconnect callback)
            await self._shutdown(rt)

    def _register_event_handlers(self, app: AsyncApp, rt: _SessionRuntime) -> None:  # noqa: PLR0915
        """Register all Slack event handlers on the given app."""

        async def _on_message_event(event, say) -> None:
            event_channel_id = event.get("channel", "")
            user_id = event.get("user", "")
            text = event.get("text", "")
            subtype = event.get("subtype")

            if subtype or not text or not user_id:
                return
            if event_channel_id != rt.channel_id:
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

            # Check for pending AskUserQuestion "Other" text input
            if rt.permission_handler.has_pending_text_input():
                await rt.permission_handler.receive_text_input(text)
                return

            # Check for command prefix
            parsed = self._command_registry.parse(full_text)
            if parsed is not None:
                await self._dispatch_command(rt, parsed[0], parsed[1], user_id, thread_ts)
                return

            await self._message_queue.put((full_text, None))
            # A successfully queued message means the socket is alive — reset failure counter
            if self._health_monitor is not None:
                self._health_monitor.mark_healthy()

        async def _on_permission_action(ack, action, body) -> None:
            await ack()
            user_id = body.get("user", {}).get("id", "")
            action_channel_id = body.get("channel", {}).get("id", rt.channel_id)
            response_url = body.get("response_url", "")
            await rt.permission_handler.handle_action(
                value=action.get("value", ""),
                user_id=user_id,
                channel_id=action_channel_id,
                response_url=response_url,
            )

        async def _on_ask_user_action(ack, action, body) -> None:
            await ack()
            value = action.get("value", "")
            action_user_id = body.get("user", {}).get("id", "")
            response_url = body.get("response_url", "")
            await rt.permission_handler.handle_ask_user_action(
                value=value,
                user_id=action_user_id,
                response_url=response_url,
            )

        async def _on_reaction_added(event) -> None:
            reaction = event.get("reaction", "")
            reaction_user = event.get("user", "")
            item = event.get("item", {})
            item_channel = item.get("channel", "")
            if (
                reaction == "octagonal_sign"
                and reaction_user == self._authenticated_user_id
                and item_channel == rt.channel_id
            ):
                self._abort_current_turn()
                try:
                    await rt.provider.post_message(
                        rt.channel_id, ":octagonal_sign: Turn cancelled via reaction."
                    )
                except Exception as e:
                    logger.debug("Failed to post cancellation message: %s", e)

        # Wire the real message handler into the early-registered dispatch slot
        # (app.event("message") was registered pre-auth to prevent Bolt warnings)
        self._message_handler.clear()
        self._message_handler.append(_on_message_event)

        app.event("reaction_added")(_on_reaction_added)
        app.action("permission_approve")(_on_permission_action)
        app.action("permission_deny")(_on_permission_action)
        app.action(re.compile(r"ask_user_\d+_.+"))(_on_ask_user_action)

    async def _reconnect_socket(self, rt: _SessionRuntime) -> _SessionRuntime:
        """Replace the socket handler with a fresh connection.

        Returns a new _SessionRuntime with the updated socket_handler.
        """
        # Close old socket (best-effort)
        try:
            await asyncio.wait_for(rt.socket_handler.close_async(), timeout=5.0)
        except Exception as e:
            logger.debug("Old socket cleanup failed (expected on dead connection): %s", e)

        # Create new app + socket handler (no /summon — already authenticated)
        new_app = AsyncApp(
            token=self._config.slack_bot_token,
            signing_secret=self._config.slack_signing_secret,
        )
        new_socket_handler = AsyncSocketModeHandler(new_app, self._config.slack_app_token)

        # Build new runtime preserving all non-socket state
        new_rt = _SessionRuntime(
            registry=rt.registry,
            client=rt.client,
            provider=rt.provider,
            permission_handler=rt.permission_handler,
            channel_id=rt.channel_id,
            socket_handler=new_socket_handler,
            channel_manager=rt.channel_manager,
        )

        # Re-register event handlers on the new app
        self._register_event_handlers(new_app, new_rt)

        # Start new socket connection
        await new_socket_handler.connect_async()
        self._active_socket_handler = new_socket_handler

        logger.info("Socket reconnected successfully")
        return new_rt

    async def _run_message_loop(  # noqa: PLR0912
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
        streamer = ResponseStreamer(
            router=router, display=display, user_id=self._authenticated_user_id
        )

        async with ClaudeSDKClient(options) as claude:
            try:
                # Initialize from server info: session ID, command registry
                self._command_registry = build_registry()
                try:
                    server_info = await claude.get_server_info()
                    if server_info:
                        claude_session_id = server_info.get("session_id", "")
                        if claude_session_id:
                            self._claude_session_id = claude_session_id
                            await rt.registry.update_status(
                                self._session_id, "active", claude_session_id=claude_session_id
                            )
                            try:
                                await rt.provider.post_message(
                                    rt.channel_id,
                                    f"Claude session: `{claude_session_id[:16]}...`",
                                    blocks=[
                                        {
                                            "type": "context",
                                            "elements": [
                                                {
                                                    "type": "mrkdwn",
                                                    "text": (
                                                        f":brain: Claude session ID:"
                                                        f" `{claude_session_id[:16]}...`"
                                                    ),
                                                }
                                            ],
                                        }
                                    ],
                                )
                            except Exception as e2:
                                logger.debug("Failed to post Claude session ID: %s", e2)
                        commands = server_info.get("commands", [])
                        if commands:
                            self._command_registry.set_passthrough_commands(commands)
                except Exception as e:
                    logger.debug("Could not retrieve server info: %s", e)

                # Discover resolved model name via /model query
                try:
                    await claude.query("/model")
                    async for msg in claude.receive_response():
                        if isinstance(msg, AssistantMessage) and (
                            not self._model or self._model == "default"
                        ):
                            self._model = msg.model
                        # Consume but don't display the response
                except Exception as e:
                    logger.debug("Could not discover model name: %s", e)

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
            finally:
                # Post session summary while the client is still open
                if self._total_turns > 0:
                    try:
                        await asyncio.wait_for(self._post_session_summary(rt, claude), timeout=30.0)
                    except TimeoutError:
                        logger.debug("Session summary timed out")
                    except Exception as e:
                        logger.debug("Session summary failed: %s", e)

    async def _handle_user_message(  # noqa: PLR0915
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

        # Reset abort event for this turn
        self._abort_event.clear()

        async def _do_turn() -> None:
            self._total_turns += 1
            await router.start_turn(self._total_turns)
            await claude.query(message)
            stream_result = await streamer.stream_with_flush(claude.receive_response())
            if stream_result:
                if stream_result.model:
                    self._model = stream_result.model
                cost = stream_result.result.total_cost_usd or 0.0
                self._total_cost += cost
                await rt.registry.record_turn(self._session_id, cost)
                summary = router.generate_turn_summary()
                await router.update_turn_summary(summary)
                if stream_result.context is not None:
                    self._last_context = stream_result.context
                if stream_result.model is not None:
                    self._last_model_seen = stream_result.model
                try:
                    git_branch = _get_git_branch(self._cwd)
                    await rt.channel_manager.set_session_topic(
                        rt.channel_id,
                        model=self._last_model_seen or self._model,
                        cwd=self._cwd,
                        git_branch=git_branch,
                        context=self._last_context,
                    )
                except Exception:
                    logger.debug("Post-turn topic update failed")

        self._current_turn_task = asyncio.create_task(_do_turn())
        abort_wait = asyncio.create_task(self._abort_event.wait())
        try:
            done, pending = await asyncio.wait(
                {self._current_turn_task, abort_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception) as _e:
                    logger.debug("Pending task cancelled: %s", _e)

            if self._current_turn_task in done and not self._current_turn_task.cancelled():
                exc = self._current_turn_task.exception()
                if exc is not None:
                    raise exc
            elif self._abort_event.is_set():
                logger.info("Turn aborted by user")
        except asyncio.CancelledError:
            if self._current_turn_task and not self._current_turn_task.done():
                self._current_turn_task.cancel()
                try:
                    await self._current_turn_task
                except (asyncio.CancelledError, Exception) as _e:
                    logger.debug("Turn task cancelled: %s", _e)
            raise
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
        finally:
            self._current_turn_task = None

    async def _heartbeat_loop(self, rt: _SessionRuntime) -> None:
        """Update registry heartbeat every 30 seconds."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                await rt.registry.heartbeat(self._session_id)
                self._last_heartbeat_time = asyncio.get_running_loop().time()
                self._pet_os_watchdog()
            except Exception as e:
                logger.warning("Heartbeat failed: %s", e)

    async def _watchdog_loop(self) -> None:
        """Detect stuck event loop. If heartbeat hasn't updated in 90s, force recovery."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(_WATCHDOG_CHECK_INTERVAL_S)
            if self._shutdown_event.is_set():
                break
            elapsed = asyncio.get_running_loop().time() - self._last_heartbeat_time
            if elapsed > _WATCHDOG_THRESHOLD_S:
                logger.critical(
                    "Watchdog: event loop appears stuck (%.0fs since last heartbeat). "
                    "Forcing shutdown.",
                    elapsed,
                )
                self._disconnect_reason = "watchdog"
                self._shutdown_event.set()
                self._message_queue.put_nowait(("", None))
                break

    def _start_os_watchdog(self) -> None:
        """Set a SIGALRM timer as last-resort watchdog for fully stuck event loops."""
        if not hasattr(signal, "SIGALRM"):
            return  # Windows does not support SIGALRM

        def _alarm_handler(signum, frame) -> None:
            logger.critical("OS watchdog fired — event loop unresponsive. Forcing exit.")
            os._exit(2)

        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(_OS_WATCHDOG_TIMEOUT_S)

    def _pet_os_watchdog(self) -> None:
        """Reset the OS watchdog timer. Called from heartbeat on each successful beat."""
        if hasattr(signal, "SIGALRM"):
            signal.alarm(_OS_WATCHDOG_TIMEOUT_S)

    async def _post_session_summary(self, rt: _SessionRuntime, claude: ClaudeSDKClient) -> None:
        """Generate and post a session summary via Claude."""
        try:
            await claude.query(
                "In 2-3 sentences, summarize what was accomplished in this session. "
                "Be concise and factual."
            )
            summary_parts: list[str] = []
            async for msg in claude.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            summary_parts.append(block.text)
            summary = "".join(summary_parts).strip()
            if summary:
                # Strip dangerous Slack mention patterns from Claude output
                summary = re.sub(r"<!(?:channel|here|everyone)>", "", summary)
                summary = re.sub(r"<@[A-Z0-9]+>", "", summary)
                summary = summary[:3000]
                await rt.provider.post_message(
                    rt.channel_id,
                    f":memo: *Session Summary*\n{summary}",
                )
        except Exception as e:
            logger.debug("Failed to generate session summary: %s", e)

    async def _shutdown(self, rt: _SessionRuntime) -> None:
        """Gracefully shut down the session."""
        logger.info(
            "Session ended. Turns: %d, Total cost: $%.4f", self._total_turns, self._total_cost
        )

        # Post disconnect message (channel is preserved, not archived)
        await self._post_disconnect_message(rt, reason=self._disconnect_reason or "ended")

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

        # Disconnect Socket Mode (use most recent handler — may have been replaced by reconnect)
        handler_to_close = self._active_socket_handler or rt.socket_handler
        try:
            await asyncio.wait_for(
                handler_to_close.close_async(),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception as e:
            logger.debug("Socket Mode cleanup error: %s", e)

    async def _post_disconnect_message(self, rt: _SessionRuntime, reason: str = "ended") -> None:
        """Post a clear disconnect notice to the channel."""
        if reason == "reconnect_exhausted":
            text = (
                ":x: *Claude session disconnected*\n"
                "Connection to Slack lost and could not be re-established.\n"
                f"Turns: {self._total_turns} | Cost: ${self._total_cost:.4f}\n"
                f"Claude session: `{self._claude_session_id or 'unknown'}`\n"
                "Resume with: `claude --resume <session-id>`"
            )
        elif reason == "watchdog":
            text = (
                ":rotating_light: *Claude session terminated by watchdog*\n"
                "The session process became unresponsive and was terminated.\n"
                f"Turns: {self._total_turns} | Cost: ${self._total_cost:.4f}"
            )
        else:
            text = (
                ":wave: *Claude session ended*\n"
                f"Turns: {self._total_turns} | Cost: ${self._total_cost:.4f}\n"
                "Channel preserved — you can review the conversation history."
            )

        blocks = [
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
        ]
        try:
            await asyncio.wait_for(
                rt.provider.post_message(rt.channel_id, text, blocks=blocks),
                timeout=_CLEANUP_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("Failed to post disconnect message: %s", e)

    async def _post_clear_delineation(self, rt: _SessionRuntime) -> None:
        """Post a visual delineation block to mark conversation history cleared."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        blocks = [
            {"type": "divider"},
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Conversation Cleared", "emoji": True},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Cleared at {timestamp}"}],
            },
        ]
        try:
            await rt.provider.post_message(rt.channel_id, "Conversation cleared.", blocks=blocks)
        except Exception as e:
            logger.warning("Failed to post clear delineation: %s", e)

    async def _dispatch_command(  # noqa: PLR0912
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

        # Handle shutdown signal from !end/!quit/!exit/!logout
        if result.metadata.get("shutdown"):
            if result.text:
                try:
                    await rt.provider.post_message(rt.channel_id, result.text, thread_ts=thread_ts)
                except Exception as e:
                    logger.warning("Failed to post shutdown message: %s", e)
            self._shutdown_event.set()
            self._message_queue.put_nowait(("", None))
            return

        # Handle !stop — abort the current Claude turn
        if result.metadata.get("stop"):
            if result.text:
                try:
                    await rt.provider.post_message(rt.channel_id, result.text, thread_ts=thread_ts)
                except Exception as e:
                    logger.warning("Failed to post stop message: %s", e)
            self._abort_current_turn()
            return

        # Handle !clear — post visual delineation then fall through to passthrough
        if result.metadata.get("clear"):
            await self._post_clear_delineation(rt)

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

        # Post the response text in thread (with splitting for long responses)
        if result.text:
            chunks = _split_text(result.text, _MAX_USER_MESSAGE_CHARS)
            for chunk in chunks:
                try:
                    await rt.provider.post_message(rt.channel_id, chunk, thread_ts=thread_ts)
                except Exception as e:
                    logger.warning("Failed to post command response: %s", e)
                    break

    def _abort_current_turn(self) -> None:
        """Signal the current Claude turn to abort."""
        self._abort_event.set()
        if self._current_turn_task and not self._current_turn_task.done():
            self._current_turn_task.cancel()

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

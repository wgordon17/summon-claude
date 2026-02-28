"""Session orchestrator — ties Claude SDK + Slack + permissions + streaming together."""

# pyright: reportArgumentType=false, reportReturnType=false
# slack_sdk and claude_agent_sdk don't ship type stubs

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude._formatting import format_file_references
from summon_claude.auth import SessionAuth
from summon_claude.channel_manager import ChannelManager, _get_git_branch
from summon_claude.commands import CommandContext, CommandRegistry, build_registry
from summon_claude.config import SummonConfig, discover_installed_plugins, get_data_dir
from summon_claude.content_display import ContentDisplay, _split_text
from summon_claude.context import ContextUsage
from summon_claude.mcp_tools import create_summon_mcp_server
from summon_claude.permissions import PermissionHandler
from summon_claude.providers.slack import SlackChatProvider
from summon_claude.registry import SessionRegistry
from summon_claude.streamer import ResponseStreamer
from summon_claude.thread_router import ThreadRouter

if TYPE_CHECKING:
    from summon_claude.event_dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

# Per-session log correlation: set when a session starts so all log records
# within that asyncio task carry the session_id in their context.
_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="")


class SessionIdFilter(logging.Filter):
    """Injects the current ``session_id`` contextvar into every log record.

    Install on the root logger (or any handler) so daemon log lines are
    tagged with the session that produced them::

        root = logging.getLogger()
        root.addFilter(SessionIdFilter())

    The filter sets ``record.session_id`` to a bracket-wrapped value like
    ``[abc123]`` when a session is active, or ``""`` when at daemon level.
    Use ``%(session_id)s`` in the log format string.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        sid = _session_id_var.get()
        # Include trailing space so format "%(name)s%(session_id)s: %(message)s"
        # produces "name[sid]: msg" for session records and "name: msg" for daemon records.
        record.session_id = f"[{sid}] " if sid else ""  # type: ignore[attr-defined]
        return True


class _SessionLogFilter(logging.Filter):
    """Passes only log records emitted within a specific session task.

    Attached to a per-session ``FileHandler`` so that each session's log file
    contains only records from that session.  Works by checking the
    ``_session_id_var`` contextvar, which is task-scoped in asyncio.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self._session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:
        return _session_id_var.get() == self._session_id


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
    provider: SlackChatProvider
    permission_handler: PermissionHandler
    channel_id: str
    channel_manager: ChannelManager


class SummonSession:
    """Orchestrates a Claude Code session bridged to a Slack channel.

    In the daemon architecture this class runs as an asyncio task inside the
    daemon process.  Slack events arrive via ``_message_queue`` (populated by
    ``EventDispatcher``) rather than being received directly by a per-session
    ``AsyncSocketModeHandler``.

    Lifecycle:
        1. Register session in SQLite (status: pending_auth)
        2. Wait for authentication signal via ``authenticate()``
        3. Create session channel, register ``SessionHandle`` with dispatcher
        4. Enter message loop: queued Slack messages -> Claude -> Slack responses
        5. Graceful shutdown on ``request_shutdown()`` or session end
    """

    def __init__(
        self,
        config: SummonConfig,
        options: SessionOptions,
        auth: SessionAuth,
        shared_provider: SlackChatProvider | None = None,
        dispatcher: EventDispatcher | None = None,
        bot_user_id: str | None = None,
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

        # Shared provider and dispatcher from the daemon (None for standalone/test use)
        self._shared_provider = shared_provider
        self._dispatcher = dispatcher
        # Pre-cached bot user ID from BoltRouter.start() — avoids a per-session auth_test() call
        self._bot_user_id = bot_user_id

        # Message queue: Slack user messages -> Claude (populated by EventDispatcher)
        # maxsize=100 provides backpressure — EventDispatcher drops events when full
        self._message_queue: asyncio.Queue[dict | tuple[str, str | None]] = asyncio.Queue(
            maxsize=100
        )

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

        # Session state
        self._claude_session_id: str | None = None
        self._last_heartbeat_time: float = 0.0
        self._channel_id: str | None = None  # set after channel creation

    # ------------------------------------------------------------------
    # Public API (called by SessionManager / BoltRouter)
    # ------------------------------------------------------------------

    @property
    def channel_id(self) -> str | None:
        """Slack channel ID for this session, set after channel creation."""
        return self._channel_id

    def request_shutdown(self) -> None:
        """Signal this session to shut down gracefully."""
        if not self._shutdown_event.is_set():
            logger.info("Session %s: shutdown requested", self._session_id)
            self._shutdown_event.set()
            # Unblock the message queue poll
            try:
                self._message_queue.put_nowait(("", None))
            except asyncio.QueueFull:
                logger.debug("Shutdown sentinel dropped (queue full); shutdown_event is set")

    def authenticate(self, user_id: str) -> None:
        """Authenticate the session for *user_id* (called by SessionManager).

        This is the daemon-side equivalent of the old ``/summon`` handler:
        it sets ``_authenticated_event`` and records the user directly,
        without requiring cross-process IPC or SQLite coordination.
        """
        self._authenticated_user_id = user_id
        self._authenticated_event.set()
        self._auth = None  # clear token from memory after successful auth
        logger.info("Session %s: authenticated by user %s", self._session_id, user_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Main entry point. Runs the full session lifecycle.

        In the daemon architecture:
        - No Bolt app or socket handler is created here.
        - Authentication is signalled externally via ``authenticate()``.
        - Events arrive via ``_message_queue`` populated by ``EventDispatcher``.
        """
        # Set contextvar so all log records in this task carry session_id
        _session_id_var.set(self._session_id)

        # Per-session log file — all records from this task are written here
        session_log_handler = self._install_session_log_handler()

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
                logger.info("Session %s: waiting for Slack authentication...", self._session_id)
                auth_status = await self._wait_for_auth()

                if auth_status == "authenticated":
                    logger.info("Authenticated! Setting up session...")
                    await self._run_session(registry)
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
                # shutdown — requested before auth completed
                if self._auth:
                    try:
                        await registry.delete_pending_token(self._auth.short_code)
                    except Exception as e:
                        logger.debug("Failed to delete pending token on shutdown: %s", e)
                return False

            except asyncio.CancelledError:
                logger.info("Session %s: task cancelled during startup", self._session_id)
                if self._auth:
                    try:
                        await registry.delete_pending_token(self._auth.short_code)
                    except Exception as e:
                        logger.debug("Failed to delete pending token on cancel: %s", e)
                raise
            finally:
                self._remove_session_log_handler(session_log_handler)
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
                    logger.info("Waiting for authentication... %.0fs remaining", remaining)
                next_countdown += 15.0

        self._shutdown_event.set()
        return "timed_out"

    async def _run_session(self, registry: SessionRegistry) -> None:
        """Create channel, register with dispatcher, connect Claude, run message loop."""
        # In daemon mode, bot_user_id is pre-cached by BoltRouter.start() — skip auth_test().
        if self._shared_provider is not None and self._bot_user_id is not None:
            provider = self._shared_provider
            bot_user_id = self._bot_user_id
        else:
            # Standalone/test mode: call auth_test() to discover bot_user_id
            client = AsyncWebClient(token=self._config.slack_bot_token)
            provider = self._shared_provider or SlackChatProvider(client)
            bot_user_id = (await client.auth_test())["user_id"]

        channel_manager = ChannelManager(provider, self._config.channel_prefix, bot_user_id)
        channel_id, channel_name = await channel_manager.create_session_channel(self._name)
        logger.info("Authenticated! Session channel: #%s", channel_name)

        # Record channel_id for SessionManager status queries
        self._channel_id = channel_id

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
                await provider.post_ephemeral(
                    channel_id,
                    self._authenticated_user_id,
                    f"Session ready! Welcome to <#{channel_id}>.",
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
            provider=provider,
            permission_handler=permission_handler,
            channel_id=channel_id,
            channel_manager=channel_manager,
        )

        # Register SessionHandle with the EventDispatcher so events are routed here
        if self._dispatcher is not None:
            from summon_claude.event_dispatcher import SessionHandle  # noqa: PLC0415

            handle = SessionHandle(
                session_id=self._session_id,
                channel_id=channel_id,
                message_queue=self._message_queue,
                permission_handler=permission_handler,
                abort_callback=self._abort_current_turn,
                authenticated_user_id=self._authenticated_user_id or "",
            )
            self._dispatcher.register(channel_id, handle)

        self._last_heartbeat_time = asyncio.get_running_loop().time()

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
                # uncancel() allows the _shutdown() awaits below to complete even if
                # this task was cancelled (e.g., by signal handling or asyncio cleanup).
                # Without this, awaiting _shutdown() would immediately re-raise
                # CancelledError before cleanup can finish.
                current_task.uncancel()

            await self._shutdown(rt)

    async def _run_message_loop(  # noqa: PLR0912, PLR0915
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

                    # Items are either raw Slack event dicts (from EventDispatcher)
                    # or internal tuples (sentinel / slash passthrough from _dispatch_command)
                    if isinstance(item, dict):
                        # Raw Slack event — run full preprocessing pipeline
                        result = await self._process_incoming_event(item, rt)
                        if result is None:
                            # Filtered out (subtype, empty, permission input handled, etc.)
                            continue
                        user_message, thread_ts = result
                    else:
                        # Internal tuple: (text, thread_ts) from _dispatch_command passthrough
                        # or ("", None) shutdown sentinel
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
            except Exception as e:
                logger.warning("Heartbeat failed: %s", e)

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
        await self._post_disconnect_message(rt, reason="ended")

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
        # Socket Mode is now managed by BoltRouter — no per-session cleanup needed

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
            try:
                self._message_queue.put_nowait(("", None))
            except asyncio.QueueFull:
                logger.debug("Shutdown sentinel dropped (queue full); shutdown_event is set")
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

    # ------------------------------------------------------------------
    # Per-session logging
    # ------------------------------------------------------------------

    def _install_session_log_handler(self) -> logging.FileHandler | None:
        """Create a per-session log file at ``~/.summon/logs/{session_id}.log``.

        Attaches a ``_SessionLogFilter`` so only records from this session's
        asyncio task (identified by ``_session_id_var``) are written.

        Returns the handler (caller must pass it to ``_remove_session_log_handler``
        on shutdown) or ``None`` if the handler could not be created.
        """
        try:
            log_dir = get_data_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f"{self._session_id}.log"
            fh = logging.FileHandler(log_file)
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            fh.addFilter(_SessionLogFilter(self._session_id))
            logging.getLogger().addHandler(fh)
            return fh
        except Exception as e:
            logger.debug("Failed to set up per-session log file: %s", e)
            return None

    @staticmethod
    def _remove_session_log_handler(handler: logging.FileHandler | None) -> None:
        """Remove and close the per-session log handler."""
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()

    async def _process_incoming_event(
        self,
        event: dict,  # type: ignore[type-arg]
        rt: _SessionRuntime,
    ) -> tuple[str, str | None] | None:
        """Process a raw Slack message event from the EventDispatcher queue.

        Replicates all pre-processing that used to live in the per-session
        ``_on_message_event`` Bolt handler:

        1. Subtype filtering — bot/system messages are ignored.
        2. Empty text / empty user_id filtering.
        3. Message truncation at ``_MAX_USER_MESSAGE_CHARS``.
        4. File reference extraction via ``format_file_references``.
        5. AskUserQuestion free-text capture via ``permission_handler``.
        6. Command prefix routing via ``_command_registry.parse``.

        Returns ``(full_text, thread_ts)`` when the message should be forwarded
        to Claude, or ``None`` when it has been handled/filtered internally.
        """
        subtype = event.get("subtype")
        user_id = event.get("user", "")
        text = event.get("text", "")

        # 1 & 2: Drop bot/system messages and empty content
        if subtype or not text or not user_id:
            return None

        # 3: Truncate oversized messages
        if len(text) > _MAX_USER_MESSAGE_CHARS:
            logger.warning("Message from %s truncated (%d chars)", user_id, len(text))
            text = text[:_MAX_USER_MESSAGE_CHARS] + "\n[message truncated]"

        # 4: Append file references
        files = event.get("files", [])
        full_text = text
        if files:
            file_context = format_file_references(files)
            if file_context:
                full_text = f"{text}\n\n{file_context}"

        thread_ts: str | None = event.get("ts")

        # 5: Route to permission handler's pending free-text input if waiting
        if rt.permission_handler.has_pending_text_input():
            await rt.permission_handler.receive_text_input(text)
            return None

        # 6: Check for !command prefix and dispatch immediately
        parsed = self._command_registry.parse(full_text)
        if parsed is not None:
            await self._dispatch_command(rt, parsed[0], parsed[1], user_id, thread_ts)
            return None

        return full_text, thread_ts

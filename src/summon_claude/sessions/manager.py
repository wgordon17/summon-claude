"""Session lifecycle management for the single-Bolt daemon architecture.

``SessionManager`` owns all running ``SummonSession`` tasks.  It provides:

- **Session creation** — ``asyncio.create_task`` + ``add_done_callback`` tracking
- **Supervision** — per-session auto-restart wrapper with exponential backoff
- **Graceful shutdown** — three-phase: signal → ``asyncio.wait(30 s)`` → cancel
- **Grace timer** — 60-second auto-stop when no sessions remain
- **Unix socket control API** — ``handle_client`` / ``_dispatch_control`` for
  CLI ↔ daemon IPC (create_session, stop_session, status messages)
- **Auth bridging** — ``handle_summon_command`` verifies short codes and calls
  ``authenticate_session`` to unblock the matching session's auth wait
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import logging
import os
import pathlib
import secrets
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING, Any

from slack_sdk.web.async_client import AsyncWebClient

# recv_msg/send_msg imported lazily in handle_client to avoid circular import
# (daemon.py imports SessionManager; SessionManager uses IPC from daemon.py)
from summon_claude.config import get_data_dir
from summon_claude.security import mark_untrusted
from summon_claude.sessions.auth import (
    SessionAuth,
    SpawnAuth,
    generate_session_token,
    verify_short_code,
    verify_spawn_token,
)
from summon_claude.sessions.prompts import format_pm_topic
from summon_claude.sessions.registry import MAX_SPAWN_CHILDREN_PM, SessionRegistry
from summon_claude.sessions.session import SessionOptions, SummonSession
from summon_claude.slack.client import redact_secrets, sanitize_for_slack
from summon_claude.slack.formatting import build_home_view
from summon_claude.summon_cli_mcp import MAX_PROMPT_CHARS

if TYPE_CHECKING:
    from summon_claude.config import SummonConfig
    from summon_claude.event_dispatcher import EventDispatcher
    from summon_claude.slack.bolt import EventProbe

logger = logging.getLogger(__name__)

_GRACE_SECONDS = 60.0
_SHUTDOWN_WAIT_TIMEOUT = 30.0
_MAX_QUEUED_SESSIONS = 50


@dataclasses.dataclass(frozen=True)
class _QueuedSession:
    """A session waiting to start once an active slot opens up."""

    options: SessionOptions
    project_id: str  # project the session belongs to (for dequeue routing)
    pm_session_id: str  # session_id of the PM that queued this session
    authenticated_user_id: str  # for auto-authenticate on dequeue
    queued_at: float  # time.monotonic() when enqueued
    parent_channel_id: str | None = None  # PM channel for spawn notifications


class SessionManager:
    """Manages the lifecycle of all sessions running inside the daemon.

    Constructor is synchronous — call from the daemon's async entry point.
    ``_start_time`` is recorded for uptime reporting in ``status`` responses.
    """

    MAX_SESSION_RESTARTS = 3

    def __init__(  # noqa: PLR0913
        self,
        config: SummonConfig,
        web_client: AsyncWebClient,
        bot_user_id: str,
        dispatcher: EventDispatcher,
        *,
        bot_team_id: str | None = None,
        event_probe: EventProbe | None = None,
        jira_proxy_port: int | None = None,
        jira_proxy_token: str | None = None,
    ) -> None:
        self._config = config
        self._web_client = web_client
        self._bot_user_id = bot_user_id
        self._bot_team_id = bot_team_id
        self._dispatcher = dispatcher
        self._event_probe = event_probe
        self._jira_proxy_port = jira_proxy_port
        self._jira_proxy_token = jira_proxy_token
        self._tasks: dict[str, asyncio.Task] = {}  # session_id → task
        self._sessions: dict[str, SummonSession] = {}  # session_id → session
        self._grace_timer: asyncio.TimerHandle | None = None
        self._shutdown_event = asyncio.Event()
        self._start_time: float = time.monotonic()
        self._project_up_in_flight = False  # guard against concurrent project_up
        self._resuming_channels: set[str] = set()  # guard against concurrent resume
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._pm_topic_cache: dict[str, str] = {}  # project_id → last-set topic
        self._app_home_last_publish: dict[str, float] = {}  # user_id → ts; FIFO cap 500
        self._suspend_on_shutdown: bool = False  # set by health monitor on event pipeline failure
        # FIFO queue: project_id → deque of _QueuedSession
        self._session_queue: dict[str, collections.deque[_QueuedSession]] = {}
        self._queue_lock = asyncio.Lock()

    def set_suspend_on_shutdown(self) -> None:
        """Mark for session suspension on shutdown (called on event pipeline failure)."""
        self._suspend_on_shutdown = True

    def _inject_proxy_options(self, options: SessionOptions) -> SessionOptions:
        """Inject daemon-level proxy config into session options."""
        if self._jira_proxy_port is not None:
            return dataclasses.replace(
                options,
                jira_proxy_port=self._jira_proxy_port,
                jira_proxy_token=self._jira_proxy_token,
            )
        return options

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _channel_has_active_session(self, channel_id: str) -> bool:
        """Return True if any live session is bound to *channel_id*."""
        return any(
            channel_id in (s.channel_id, s.target_channel_id) for s in self._sessions.values()
        )

    def _check_channel_available(self, channel_id: str) -> None:
        """Raise ``ValueError`` if *channel_id* is resuming or has an active session."""
        if channel_id in self._resuming_channels:
            raise ValueError("Resume already in progress for this channel")
        if self._channel_has_active_session(channel_id):
            raise ValueError("Channel already has an active session")

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    @property
    def shutdown_event(self) -> asyncio.Event:
        """Event that is set when the daemon should stop."""
        return self._shutdown_event

    async def create_session(self, options: SessionOptions) -> str:
        """Create and start a supervised session task.

        Generates a new ``session_id`` and auth token internally so the CLI
        does not need to touch the SQLite registry.

        Returns:
            The short code for the user to authenticate via ``/summon`` in Slack.

        Raises:
            ValueError: If ``options.channel_id`` targets a channel that already
                has an active or resuming session.
        """
        # Guard against duplicate sessions on the same channel (resume via CLI)
        if options.channel_id:
            self._check_channel_available(options.channel_id)

        # Cancel grace timer — a new session has arrived
        self._cancel_grace_timer()

        session_id = str(uuid.uuid4())
        auth = await self._generate_auth(session_id)
        options = self._inject_proxy_options(options)

        session = SummonSession(
            config=self._config,
            options=options,
            auth=auth,
            session_id=session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
            bot_team_id=self._bot_team_id,
            ipc_spawn=self.create_session_with_spawn_token,
            ipc_resume=self._ipc_resume,
            ipc_queue=self.queue_session,
        )
        self._sessions[session_id] = session

        task = asyncio.create_task(
            self._supervised_session(session, session_id),
            name=f"session-{session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=session_id))
        self._tasks[session_id] = task
        logger.info("SessionManager: created session %s", session_id)

        # Update PM topic if this is a project-affiliated non-PM session
        if session.project_id and not session.is_pm:
            await self._update_pm_topic(session.project_id)

        return auth.short_code

    @staticmethod
    async def _generate_auth(session_id: str) -> SessionAuth:
        """Open a short-lived registry and generate an auth token.

        Uses a dedicated ``SessionRegistry`` context so the connection is
        closed before the session task is spawned.  The session's ``start()``
        opens its own registry later for ``register()`` / heartbeat.
        """
        auth: SessionAuth | None = None
        async with SessionRegistry() as registry:
            auth = await generate_session_token(registry, session_id)
        if auth is None:  # pragma: no cover — SessionRegistry never suppresses
            raise RuntimeError("Auth generation failed silently")
        return auth

    @staticmethod
    async def _verify_and_log_spawn_token(spawn_token: str, session_id: str) -> SpawnAuth | None:
        """Verify a spawn token and log the result (success or rejection)."""
        async with SessionRegistry() as registry:
            spawn_auth = await verify_spawn_token(registry, spawn_token)
            if spawn_auth is None:
                await registry.log_event(
                    "spawn_token_rejected",
                    session_id=session_id,
                )
                return None
            await registry.log_event(
                "spawn_token_consumed",
                session_id=session_id,
                user_id=spawn_auth.target_user_id,
                details={
                    "parent_session_id": spawn_auth.parent_session_id,
                    "spawn_source": spawn_auth.spawn_source,
                    "cwd": spawn_auth.cwd,
                },
            )
            return spawn_auth

    async def create_session_with_spawn_token(
        self, options: SessionOptions, spawn_token: str
    ) -> str:
        """Create a pre-authenticated session using a spawn token."""
        session_id = str(uuid.uuid4())
        spawn_auth = await self._verify_and_log_spawn_token(spawn_token, session_id)

        if spawn_auth is None:
            raise ValueError("Invalid or expired spawn token")

        # Cancel grace timer only after successful verification — prevents
        # invalid tokens from keeping the daemon alive indefinitely.
        self._cancel_grace_timer()

        # Enforce the authorized working directory from the spawn token
        options = dataclasses.replace(options, cwd=spawn_auth.cwd)
        options = self._inject_proxy_options(options)

        session = SummonSession(
            config=self._config,
            options=options,
            auth=None,
            session_id=session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
            bot_team_id=self._bot_team_id,
            parent_session_id=spawn_auth.parent_session_id,
            parent_channel_id=spawn_auth.parent_channel_id,
            ipc_spawn=self.create_session_with_spawn_token,
            ipc_resume=self._ipc_resume,
            ipc_queue=self.queue_session,
        )
        session.authenticate(spawn_auth.target_user_id)

        self._sessions[session_id] = session
        task = asyncio.create_task(
            self._supervised_session(session, session_id),
            name=f"session-{session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=session_id))
        self._tasks[session_id] = task
        logger.info("SessionManager: created spawned session %s", session_id)

        # Update PM topic now that a new child is tracked
        if session.project_id and not session.is_pm:
            await self._update_pm_topic(session.project_id)

        return session_id

    def stop_session(self, session_id: str) -> bool:
        """Signal a specific session to shut down.  Returns ``True`` if found."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.request_shutdown()
            logger.info("SessionManager: stop requested for %s", session_id)
            return True
        logger.debug("SessionManager: stop_session — session %s not found", session_id)
        return False

    def authenticate_session(self, session_id: str, user_id: str) -> bool:
        """Authenticate the session with *user_id*.  Returns ``True`` if found."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.authenticate(user_id)
            return True
        return False

    async def shutdown(self) -> None:
        """Three-phase graceful shutdown of all sessions.

        If _suspend_on_shutdown, project sessions are pre-set to 'suspended'
        before signalling stop, so they can be resumed via 'summon project up'.
        """
        self._cancel_grace_timer()

        # Clear queue BEFORE signaling shutdown — prevents _on_task_done
        # from starting new sessions as existing ones complete.
        self._session_queue.clear()

        # Suspend project sessions on health failure so they can be resumed
        if self._suspend_on_shutdown and self._sessions:
            async with SessionRegistry() as registry:
                for sid, session in list(self._sessions.items()):
                    try:
                        if session.project_id:
                            await registry.update_status(sid, "suspended")
                        else:
                            await registry.update_status(
                                sid,
                                "errored",
                                error_message="Daemon shutdown: event pipeline failure",
                            )
                    except Exception:
                        logger.debug(
                            "SessionManager: failed to update status for %s on suspend shutdown",
                            sid,
                        )

        # Phase 1 — signal
        for session in list(self._sessions.values()):
            session.request_shutdown()

        # Cancel background tasks (orchestrators, etc.)
        for bg_task in list(self._background_tasks):
            bg_task.cancel()

        # Phase 2 — wait (bounded)
        all_tasks = list(self._tasks.values()) + list(self._background_tasks)
        if all_tasks:
            _done, pending = await asyncio.wait(all_tasks, timeout=_SHUTDOWN_WAIT_TIMEOUT)
            # Phase 3 — force cancel
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        # Intentionally idempotent — safe to call even if event is already set.
        # Ensures shutdown() is self-contained for direct callers outside daemon_main().
        self._shutdown_event.set()
        logger.info("SessionManager: shutdown complete")

    # ------------------------------------------------------------------
    # /summon auth bridging
    # ------------------------------------------------------------------

    async def resume_from_channel(
        self, channel_id: str, user_id: str, target_session_id: str | None
    ) -> None:
        """Resume a stopped session from a Slack channel command.

        Called by EventDispatcher for ``!summon resume`` in unrouted channels.
        Validates channel ownership and session state before resuming.

        Raises:
            ValueError: With a user-facing message on validation failure.
        """
        resolved_session_id = await self._resolve_channel_resume(
            channel_id, user_id, target_session_id
        )
        if not resolved_session_id:
            return  # Not a summon-managed channel — silent

        # Delegate to existing resume orchestration (opens its own registry)
        result = await self._handle_resume_session(
            {"type": "resume_session", "session_id": resolved_session_id}
        )
        if result.get("type") == "error":
            raise ValueError(result["message"])

    async def _resolve_channel_resume(
        self, channel_id: str, user_id: str, target_session_id: str | None
    ) -> str | None:
        """Validate ownership and resolve the session to resume.

        Returns the session ID on success, ``None`` for non-summon channels.

        Raises:
            ValueError: On ownership or state validation failure.
        """
        async with SessionRegistry() as registry:
            channel = await registry.get_channel(channel_id)
            if not channel:
                return None

            if channel.get("authenticated_user_id") != user_id:
                raise ValueError(":x: Only the original session owner can resume.")

            if target_session_id:
                session = await registry.get_session(target_session_id)
                if not session or session.get("slack_channel_id") != channel_id:
                    raise ValueError("Session not found in this channel.")
            else:
                session = await registry.get_latest_session_for_channel(channel_id)
                if not session:
                    # Check if there's an active session — provide a better
                    # error message than "no previous session found"
                    active = await registry.get_active_session_for_channel(channel_id)
                    if active:
                        raise ValueError("Session is still active. Use the existing session.")
                    raise ValueError("No previous session found in this channel.")

            if session["status"] not in ("completed", "errored"):
                raise ValueError("Session is still active. Use the existing session.")

            return session["session_id"]

    async def _ipc_resume(self, session_id: str) -> dict[str, Any]:
        """Resume a stopped session and return the IPC result dict.

        Used as callback for ``SummonSession._handle_resume_from_active``.
        """
        return await self._handle_resume_session(
            {"type": "resume_session", "session_id": session_id}
        )

    async def handle_summon_command(
        self,
        user_id: str,
        code: str,
        respond,  # Bolt respond callable
    ) -> None:
        """Handle a ``/summon <code>`` slash command routed from BoltRouter.

        Verifies the short code against pending tokens, then authenticates
        the matching session directly (same event loop — no IPC required).
        """
        auth_result = None
        try:
            async with SessionRegistry() as registry:
                auth_result = await asyncio.wait_for(verify_short_code(registry, code), timeout=2.0)
        except TimeoutError:
            logger.warning("SessionManager: verify_short_code timed out for code %s", code)
            await respond(
                text=":x: Service temporarily busy. Please try again.",
                response_type="ephemeral",
            )
            return
        except Exception:
            logger.exception("SessionManager: verify_short_code failed for code %s", code)
            await respond(
                text=":x: Service error. Please try again.",
                response_type="ephemeral",
            )
            return

        if auth_result is None:
            await respond(
                text=":x: Invalid or expired code. Run `summon start` to get a new code.",
                response_type="ephemeral",
            )
            return

        # Authenticate the matching session
        found = self.authenticate_session(auth_result.session_id, user_id)
        if not found:
            logger.warning(
                "SessionManager: /summon auth succeeded for %s but session not found",
                auth_result.session_id,
            )
            await respond(
                text=":x: Session not found. It may have already completed.",
                response_type="ephemeral",
            )
            return

        await respond(
            text=":rocket: Authenticated! Creating your session channel...",
            response_type="ephemeral",
        )

    _APP_HOME_DEBOUNCE_S = 60.0

    async def handle_app_home(self, user_id: str) -> None:
        """Publish the App Home dashboard for a user.

        Queries active sessions scoped to user_id (SQL-level scoping),
        builds the home view, and publishes via views.publish.
        Debounces per-user to avoid redundant DB+API calls on rapid tab switches.
        """
        now = time.monotonic()
        last = self._app_home_last_publish.get(user_id, 0.0)
        if now - last < self._APP_HOME_DEBOUNCE_S:
            return
        if len(self._app_home_last_publish) >= 500 and user_id not in self._app_home_last_publish:
            oldest_key = next(iter(self._app_home_last_publish))
            del self._app_home_last_publish[oldest_key]
        self._app_home_last_publish[user_id] = now

        sessions: list[dict] = []
        try:
            async with SessionRegistry() as registry:
                sessions = await registry.list_active_by_user(user_id)
        except Exception:
            logger.exception("SessionManager: registry query failed for app_home user %s", user_id)

        home_view = build_home_view(sessions)
        try:
            await self._web_client.views_publish(user_id=user_id, view=home_view)
        except Exception as e:
            logger.warning("SessionManager: views_publish failed for user %s: %s", user_id, e)

    # ------------------------------------------------------------------
    # Unix socket control API
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_session_options(options: SessionOptions) -> dict | None:
        """Validate session options at the IPC boundary. Returns error dict or None."""
        if options.system_prompt_append and len(options.system_prompt_append) > MAX_PROMPT_CHARS:
            return {
                "type": "error",
                "message": f"system_prompt_append exceeds {MAX_PROMPT_CHARS} chars",
            }
        if options.initial_prompt and len(options.initial_prompt) > MAX_PROMPT_CHARS:
            return {
                "type": "error",
                "message": f"initial_prompt exceeds {MAX_PROMPT_CHARS} chars",
            }
        return None

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one CLI client connection on the Unix control socket."""
        from summon_claude.daemon import recv_msg, send_msg  # noqa: PLC0415

        try:
            msg = await recv_msg(reader)
            response = await self._dispatch_control(msg)
            await send_msg(writer, response)
        except (asyncio.IncompleteReadError, ConnectionResetError) as e:
            logger.debug("SessionManager: control client disconnected early: %s", e)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch_control(self, msg: dict) -> dict:  # type: ignore[type-arg]  # noqa: PLR0912, PLR0915
        """Route a control message to the appropriate handler and return a response."""
        match msg.get("type"):
            case "create_session":
                try:
                    opts = msg["options"]
                    opts.pop("jira_proxy_port", None)
                    opts.pop("jira_proxy_token", None)
                    options = SessionOptions(**opts)
                except (TypeError, KeyError) as e:
                    return {"type": "error", "message": f"Invalid session options: {e}"}
                # Defense-in-depth: validate free-text fields at daemon boundary
                if err := self._validate_session_options(options):
                    return err
                try:
                    short_code = await self.create_session(options)
                except ValueError as e:
                    return {"type": "error", "message": str(e)}
                return {"type": "session_created", "short_code": short_code}

            case "stop_session":
                session_id = msg.get("session_id")
                if not session_id:
                    return {"type": "error", "message": "Missing session_id"}
                found = self.stop_session(session_id)
                return {"type": "session_stopped", "found": found}

            case "stop_all":
                results = [
                    {"session_id": sid, "found": self.stop_session(sid)}
                    for sid in list(self._sessions)
                ]
                return {"type": "all_stopped", "results": results}

            case "status":
                return {
                    "type": "status",
                    "pid": os.getpid(),
                    "uptime": time.monotonic() - self._start_time,
                    "sessions": [
                        {
                            "session_id": sid,
                            "channel_id": s.channel_id,
                            "session_name": s.name,
                            "project_id": s.project_id,
                            "pm_profile": s.is_pm,
                            "status": "active",
                        }
                        for sid, s in self._sessions.items()
                    ],
                    "queued": {pid: len(dq) for pid, dq in self._session_queue.items() if dq},
                }

            case "create_session_with_spawn_token":
                try:
                    opts = msg["options"]
                    opts.pop("jira_proxy_port", None)
                    opts.pop("jira_proxy_token", None)
                    options = SessionOptions(**opts)
                    spawn_token = msg["spawn_token"]
                except (TypeError, KeyError) as e:
                    return {"type": "error", "message": f"Invalid request: {e}"}
                if err := self._validate_session_options(options):
                    return err
                try:
                    session_id = await self.create_session_with_spawn_token(options, spawn_token)
                except ValueError as e:
                    return {"type": "error", "message": str(e)}
                return {"type": "session_created_spawned", "session_id": session_id}

            case "send_message":
                session_id = msg.get("session_id")
                text = msg.get("text")
                sender_info = msg.get("sender_info")
                if not session_id or not text:
                    return {"type": "error", "message": "Missing session_id or text"}
                session = self._sessions.get(session_id)
                if session is None:
                    return {
                        "type": "error",
                        "message": f"Session {session_id} not found or not active",
                    }
                ok = await session.inject_message(text, sender_info=sender_info)
                if not ok:
                    return {
                        "type": "error",
                        "message": "Queue full or session shutting down",
                    }
                return {
                    "type": "message_sent",
                    "session_id": session_id,
                    "channel_id": session.channel_id,
                }

            case "resume_session":
                try:
                    return await self._handle_resume_session(msg)
                except Exception as e:
                    logger.exception("resume_session failed")
                    return {"type": "error", "message": str(e)}

            case "project_up":
                return await self._handle_project_up(msg)

            case "health_check":
                return await self._handle_health_check()

            case "clear_session":
                session_id = msg.get("session_id")
                if not session_id:
                    return {"type": "error", "message": "Missing session_id"}
                session = self._sessions.get(session_id)
                if session is None:
                    return {
                        "type": "error",
                        "message": f"Session {session_id} not found or not active",
                    }
                logger.info("Clearing context for session %s", session_id)
                ok = await session.clear_context()
                if not ok:
                    return {"type": "error", "message": "clear_context() failed"}
                return {"type": "session_cleared", "session_id": session_id}

            case "clear_project_queue":
                project_id = msg.get("project_id")
                if not project_id:
                    return {"type": "error", "message": "Missing project_id"}
                count = self.clear_queue(project_id)
                return {"type": "queue_cleared", "project_id": project_id, "count": count}

            case _:
                return {"type": "error", "message": f"Unknown command: {msg.get('type')}"}

    async def _handle_health_check(self) -> dict[str, Any]:
        """IPC handler for ``health_check``."""
        if self._event_probe is None:
            return {
                "type": "health_check_result",
                "healthy": None,
                "reason": "skipped",
                "details": "Event probe not available.",
                "remediation_url": None,
            }
        try:
            result = await asyncio.wait_for(self._event_probe.run_probe(timeout=5.0), timeout=10.0)
            return {
                "type": "health_check_result",
                "healthy": result.healthy,
                "reason": result.reason,
                "details": result.details,
                "remediation_url": result.remediation_url,
            }
        except TimeoutError:
            return {
                "type": "health_check_result",
                "healthy": None,
                "reason": "timeout",
                "details": "Health check timed out.",
                "remediation_url": None,
            }
        except Exception as e:
            logger.warning("health_check IPC: probe failed with exception: %s", e)
            return {
                "type": "health_check_result",
                "healthy": None,
                "reason": "error",
                "details": f"Probe error: {redact_secrets(str(e))}",
                "remediation_url": None,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _supervised_session(self, session: SummonSession, session_id: str) -> None:
        """Run *session* with auto-restart for recoverable errors.

        Makes up to ``MAX_SESSION_RESTARTS`` attempts.  Exits cleanly on
        ``CancelledError`` (shutdown in progress).  Logs and gives up on
        non-recoverable errors or exhausted retries.
        """
        for attempt in range(self.MAX_SESSION_RESTARTS):
            try:
                await session.start()
                # Set correct PM topic on clean exit (restart recovery is
                # handled by child lifecycle hooks in create_session* / _on_task_done)
                if session.is_pm and session.project_id:
                    await self._update_pm_topic(session.project_id)
                return  # clean exit — session ran to completion
            except asyncio.CancelledError:
                raise  # propagate — shutdown is in progress
            except Exception as e:
                last_attempt = attempt >= self.MAX_SESSION_RESTARTS - 1
                if not last_attempt and self._is_recoverable(e):
                    backoff = 2**attempt
                    logger.warning(
                        "Session %s restarting (attempt %d/%d, backoff %ds): %s",
                        session_id,
                        attempt + 1,
                        self.MAX_SESSION_RESTARTS,
                        backoff,
                        redact_secrets(str(e)),
                    )
                    # Best-effort: alert channel about retry
                    await self._alert_channel(
                        session,
                        f":warning: *Session error* (attempt {attempt + 1}/"
                        f"{self.MAX_SESSION_RESTARTS}), restarting in {backoff}s\u2026\n"
                        f"Error: `{e}`",
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "Session %s failed permanently: %s",
                        session_id,
                        redact_secrets(str(e)),
                    )
                    recovery_hint = "\nRun `summon project up` to restart." if session.is_pm else ""
                    await self._alert_channel(
                        session,
                        f":x: *Session terminated unexpectedly*: `{e}`{recovery_hint}",
                    )
                    # Best-effort: notify global PM of project PM failure
                    if session.is_pm and not session.is_global_pm:
                        for s in self._sessions.values():
                            if s.is_global_pm:
                                with contextlib.suppress(Exception):
                                    await s.inject_message(
                                        mark_untrusted(
                                            f"Project PM session failed: {type(e).__name__}",
                                            source="session-manager-error",
                                        ),
                                        sender_info="session-manager",
                                    )
                                break
                    break

    # ------------------------------------------------------------------
    # session resume — daemon-side orchestration
    # ------------------------------------------------------------------

    async def _handle_resume_session(self, msg: dict[str, Any]) -> dict[str, Any]:
        """IPC handler for ``resume_session``.

        Looks up the old session, validates state, and creates a new session
        connected to the same channel with Claude SDK transcript continuity.
        """
        old_session_id = msg.get("session_id")
        if not old_session_id:
            return {"type": "error", "message": "Missing session_id"}

        # Look up old session and extract resume parameters
        try:
            resume_params = await self._validate_resume_target(old_session_id)
        except ValueError as e:
            return {"type": "error", "message": str(e)}

        channel_id: str = resume_params["channel_id"]
        claude_sid: str | None = resume_params["claude_session_id"]

        # Atomic guard: reject if channel already resuming or has active session
        self._check_channel_available(channel_id)

        self._resuming_channels.add(channel_id)
        try:
            options = SessionOptions(
                cwd=resume_params["cwd"],
                name=resume_params.get("session_name") or "",
                model=msg.get("model") or resume_params.get("model"),
                effort=resume_params.get("effort") or "high",
                resume=claude_sid,
                channel_id=channel_id,
                resume_from_session_id=old_session_id,
            )
            session_id = await self.create_resumed_session(
                options,
                authenticated_user_id=resume_params.get("authenticated_user_id"),
                parent_session_id=resume_params.get("parent_session_id"),
            )
            return {
                "type": "session_resumed",
                "session_id": session_id,
                "channel_id": channel_id,
            }
        finally:
            self._resuming_channels.discard(channel_id)

    async def _validate_resume_target(self, old_session_id: str) -> dict[str, Any]:
        """Validate and extract resume parameters from an old session.

        Returns a dict with resume params on success.

        Note: ``_restart_suspended_sessions`` bypasses this method and builds
        resume params inline from its already-open registry connection.  Keep
        validation logic here in sync with that path.

        Raises:
            ValueError: On validation failure (session not found, wrong status, etc.).
        """
        async with SessionRegistry() as registry:
            old_session = await registry.get_session(old_session_id)
            if not old_session:
                raise ValueError(f"Session {old_session_id} not found")
            status = old_session["status"]
            if status == "suspended":
                raise ValueError("Session is suspended — use project up to restart it")
            if status not in ("completed", "errored"):
                raise ValueError(f"Session is {status} — use session_message instead")
            channel_id = old_session.get("slack_channel_id")
            if not channel_id:
                raise ValueError("Session has no associated channel")

            channel = await registry.get_channel(channel_id)
            claude_session_id = (
                channel["claude_session_id"]
                if channel and channel.get("claude_session_id")
                else old_session.get("claude_session_id")
            )
            # Missing claude_session_id is allowed — caller falls back to channel-reuse-only

            return {
                "channel_id": channel_id,
                "claude_session_id": claude_session_id,
                "cwd": old_session["cwd"],
                "session_name": old_session.get("session_name"),
                "model": old_session.get("model"),
                "effort": old_session.get("effort"),
                "authenticated_user_id": old_session.get("authenticated_user_id"),
                "parent_session_id": old_session.get("parent_session_id"),
            }
        raise AssertionError("unreachable")  # pragma: no cover

    async def create_resumed_session(
        self,
        options: SessionOptions,
        authenticated_user_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> str:
        """Create a pre-authenticated session for resume (no spawn token needed).

        Similar to ``create_session_with_spawn_token`` but uses the old
        session's auth identity directly.

        Raises:
            ValueError: If ``authenticated_user_id`` is None (session would
                hang in auth wait).
        """
        if not authenticated_user_id:
            raise ValueError("Cannot resume session without authenticated_user_id")

        self._cancel_grace_timer()
        session_id = str(uuid.uuid4())
        options = self._inject_proxy_options(options)

        session = SummonSession(
            config=self._config,
            options=options,
            auth=None,
            session_id=session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
            bot_team_id=self._bot_team_id,
            parent_session_id=parent_session_id,
            ipc_spawn=self.create_session_with_spawn_token,
            ipc_resume=self._ipc_resume,
            ipc_queue=self.queue_session,
        )
        session.authenticate(authenticated_user_id)

        self._sessions[session_id] = session
        task = asyncio.create_task(
            self._supervised_session(session, session_id),
            name=f"session-resume-{session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=session_id))
        self._tasks[session_id] = task
        logger.info("SessionManager: created resumed session %s", session_id)

        # Update PM topic if this is a project-affiliated non-PM session
        if options.project_id and not options.pm_profile:
            await self._update_pm_topic(options.project_id)

        return session_id

    # ------------------------------------------------------------------
    # project up — daemon-side orchestration
    # ------------------------------------------------------------------

    async def _handle_project_up(self, msg: dict[str, Any]) -> dict[str, Any]:
        """IPC handler for ``project_up``.

        Checks which projects need a PM, creates an auth session if any do,
        launches a background orchestrator, and returns immediately with the
        short code for the user to authenticate.
        """
        if self._project_up_in_flight:
            return {"type": "error", "message": "A project_up operation is already in progress"}

        # Set the flag before any work — cleared in orchestrator's finally
        # (or in the except block if we fail before the orchestrator starts).
        self._project_up_in_flight = True

        try:
            # Check if any projects need PM before creating an auth session
            projects: list[dict[str, Any]] = []
            async with SessionRegistry() as registry:
                projects = await registry.list_projects()
            needing_pm = [p for p in projects if not p.get("pm_running")]
            gpm_running = any(s.is_global_pm for s in self._sessions.values())
            scribe_running = any(s.is_scribe for s in self._sessions.values())
            if not needing_pm and gpm_running and scribe_running:
                self._project_up_in_flight = False
                return {"type": "project_up_complete"}
            if not needing_pm:
                # All project PMs running but GPM or scribe needs starting.
                # Grab user_id from an existing session to avoid a new auth flow.
                user_id: str | None = None
                for s in self._sessions.values():
                    if s._authenticated_user_id:  # noqa: SLF001
                        user_id = s._authenticated_user_id  # noqa: SLF001
                        break
                if user_id:
                    try:
                        await self._resume_or_start_global_pm(user_id)
                    except Exception as e:
                        logger.error("Global PM: failed to start: %s", e)
                    try:
                        await self._resume_or_start_scribe(user_id)
                    except Exception as e:
                        logger.error("Scribe: failed to start: %s", e)
                self._project_up_in_flight = False
                return {"type": "project_up_complete"}

            cwd = msg.get("cwd", str(pathlib.Path.cwd()))

            # Create auth-only session
            self._cancel_grace_timer()
            session_id = str(uuid.uuid4())
            auth = await self._generate_auth(session_id)
            options = SessionOptions(
                cwd=cwd,
                name=f"project-auth-{secrets.token_hex(3)}",
                auth_only=True,
            )
            options = self._inject_proxy_options(options)
            session = SummonSession(
                config=self._config,
                options=options,
                auth=auth,
                session_id=session_id,
                web_client=self._web_client,
                dispatcher=self._dispatcher,
                bot_user_id=self._bot_user_id,
                bot_team_id=self._bot_team_id,
                ipc_spawn=self.create_session_with_spawn_token,
                ipc_resume=self._ipc_resume,
                ipc_queue=self.queue_session,
            )
            self._sessions[session_id] = session
            task = asyncio.create_task(
                self._supervised_session(session, session_id),
                name=f"session-auth-{session_id}",
            )
            task.add_done_callback(partial(self._on_task_done, session_id=session_id))
            self._tasks[session_id] = task

            bg_task = asyncio.create_task(
                self._project_up_orchestrator(session, needing_pm),
                name=f"project-up-{session_id}",
            )
            self._background_tasks.add(bg_task)
            bg_task.add_done_callback(self._on_background_task_done)

            logger.info(
                "SessionManager: project_up started (auth session %s, %d projects)",
                session_id,
                len(needing_pm),
            )
            return {
                "type": "project_up_auth_required",
                "short_code": auth.short_code,
                "project_count": len(needing_pm),
            }
        except Exception:
            self._project_up_in_flight = False
            raise

    async def _project_up_orchestrator(
        self,
        auth_session: SummonSession,
        needing_pm: list[dict[str, Any]],
    ) -> None:
        """Background task: wait for auth, then resume suspended sessions + start fresh PMs."""
        try:
            async with asyncio.timeout(360):
                await auth_session._authenticated_event.wait()  # noqa: SLF001
            user_id = auth_session._authenticated_user_id  # noqa: SLF001
            if user_id is None:
                raise RuntimeError("Auth session completed without setting user_id")

            # Resume suspended sessions first (including PMs suspended by project down).
            # Returns the set of project_ids that got a suspended PM resumed.
            pm_resumed = await self._restart_suspended_sessions(needing_pm, user_id)

            # Only start fresh PMs for projects that did NOT have a suspended PM to resume.
            for project in needing_pm:
                if project["project_id"] in pm_resumed:
                    continue
                try:
                    self._start_pm_for_project(project, user_id)
                except Exception as e:
                    logger.error(
                        "PM: failed to start session for project %s: %s",
                        project.get("name", "?"),
                        e,
                    )

            # Resume suspended Global PM or start fresh (after PMs, before scribe).
            try:
                await self._resume_or_start_global_pm(user_id)
            except Exception as e:
                logger.error("Global PM: failed to start: %s", e)

            # Resume suspended scribe or start fresh.
            await self._resume_or_start_scribe(user_id)

        except TimeoutError:
            logger.error("project_up orchestrator: authentication timed out")
        except Exception as e:
            logger.error(
                "project_up orchestrator failed: %s", redact_secrets(str(e)), exc_info=True
            )
        finally:
            self._project_up_in_flight = False

    async def _restart_suspended_sessions(
        self,
        projects: list[dict[str, Any]],
        user_id: str,
    ) -> set[str]:
        """Resume sessions suspended by ``project down`` via create_resumed_session.

        Returns a set of project_ids for which a suspended PM was successfully resumed.
        Child sessions are also resumed (not restarted fresh).
        """
        pm_resumed: set[str] = set()
        async with SessionRegistry() as registry:
            for project in projects:
                project_id = project["project_id"]
                sessions = await registry.get_project_sessions(project_id)
                suspended = [s for s in sessions if s.get("status") == "suspended"]
                project_dir = project["directory"]
                project_path = pathlib.Path(project_dir)
                if not project_path.is_dir():  # noqa: ASYNC240
                    logger.error(
                        "PM: project %s directory missing: %s, marking suspended sessions errored",
                        project["name"],
                        project_dir,
                    )
                    for sess in suspended:
                        with contextlib.suppress(Exception):
                            await registry.update_status(
                                sess["session_id"],
                                "errored",
                                error_message=f"Project directory not found: {project_dir}",
                            )
                    continue
                resolved_project_dir = project_path.resolve()  # noqa: ASYNC240
                for sess in suspended:
                    sess_id = sess["session_id"]
                    sess_name = sess.get("session_name", "")
                    is_pm = bool(sess.get("pm_profile"))
                    try:
                        channel_id = sess.get("slack_channel_id")
                        if not channel_id:
                            raise ValueError("Session has no associated channel")
                        channel = await registry.get_channel(channel_id)
                        claude_sid: str | None = (
                            channel["claude_session_id"]
                            if channel and channel.get("claude_session_id")
                            else sess.get("claude_session_id")
                        )
                        old_cwd = sess.get("cwd")
                        if is_pm:
                            cwd = project_dir
                        elif (
                            old_cwd
                            and (old_cwd_path := pathlib.Path(old_cwd)).is_dir()
                            and old_cwd_path.resolve().is_relative_to(resolved_project_dir)  # noqa: ASYNC240
                        ):
                            cwd = old_cwd
                        else:
                            cwd = project_dir
                        if old_cwd and old_cwd != cwd:
                            logger.debug(
                                "project_up: session %s cwd changed: %s -> %s",
                                sess_id[:8],
                                old_cwd,
                                cwd,
                            )
                        options = SessionOptions(
                            cwd=cwd,
                            name=sess_name or "",
                            model=sess.get("model"),
                            effort=sess.get("effort") or "high",
                            resume=claude_sid,  # None → channel-reuse-only fallback
                            channel_id=channel_id,
                            project_id=project_id,
                            pm_profile=is_pm,
                            resume_from_session_id=sess_id,
                        )
                        await self.create_resumed_session(
                            options,
                            authenticated_user_id=user_id,
                            parent_session_id=sess.get("parent_session_id"),
                        )
                        # Mark old suspended record as completed
                        await registry.update_status(sess_id, "completed")
                        logger.info(
                            "PM: resumed suspended %s %s for project %s",
                            "PM" if is_pm else "session",
                            sess_id[:8],
                            project["name"],
                        )
                        if is_pm:
                            pm_resumed.add(project_id)
                    except Exception as e:
                        logger.error(
                            "PM: failed to resume suspended session %s: %s",
                            sess_id[:8],
                            e,
                        )
                        # Mark as errored to prevent infinite retry on next project up
                        with contextlib.suppress(Exception):
                            await registry.update_status(
                                sess_id,
                                "errored",
                                error_message=f"Resume failed: {e}",
                            )
        return pm_resumed

    def _start_pm_for_project(self, project: dict[str, Any], user_id: str) -> None:
        """Create and start a single PM session for *project*."""
        project_dir = project["directory"]
        if not pathlib.Path(project_dir).is_dir():
            raise FileNotFoundError(f"Directory not found: {project_dir}")

        new_session_id = str(uuid.uuid4())
        pm_options = SessionOptions(
            cwd=project_dir,
            name=f"pm-{secrets.token_hex(3)}",
            pm_profile=True,
            project_id=project["project_id"],
        )
        pm_options = self._inject_proxy_options(pm_options)
        new_session = SummonSession(
            config=self._config,
            options=pm_options,
            auth=None,
            session_id=new_session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
            bot_team_id=self._bot_team_id,
            ipc_spawn=self.create_session_with_spawn_token,
            ipc_resume=self._ipc_resume,
            ipc_queue=self.queue_session,
        )
        new_session.authenticate(user_id)

        # Cancel grace timer — it may have been (re)started by
        # _on_task_done after the auth-only session completed.
        self._cancel_grace_timer()
        self._sessions[new_session_id] = new_session
        task = asyncio.create_task(
            self._supervised_session(new_session, new_session_id),
            name=f"session-pm-{new_session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=new_session_id))
        self._tasks[new_session_id] = task

        # Seed the topic cache with the initial value that session.start() will set,
        # so _update_pm_topic on clean exit doesn't make a redundant API call.
        self._pm_topic_cache[project["project_id"]] = format_pm_topic(0)

        logger.info(
            "SessionManager: started PM session %s for project %s",
            new_session_id,
            project["name"],
        )

    async def _resume_or_start_scribe(self, user_id: str) -> None:
        """Resume a suspended scribe or start fresh.

        Follows the same suspend/resume pattern as PM and child sessions:
        ``project down`` marks scribe as ``suspended``; ``project up``
        resumes it with transcript continuity via ``create_resumed_session``.
        Falls back to ``_start_scribe_if_enabled`` if no suspended scribe exists.
        """
        if not self._config.scribe_enabled:
            return

        # Check for already-running scribe
        for sess in self._sessions.values():
            if sess.is_scribe:
                logger.info("SessionManager: scribe already running — skipping")
                return

        # Look for a suspended scribe session to resume
        async with SessionRegistry() as registry:
            async with registry.db.execute(
                "SELECT * FROM sessions"
                " WHERE status = 'suspended'"
                "   AND session_name = 'scribe'"
                "   AND project_id IS NULL"
                " ORDER BY started_at DESC LIMIT 1",
            ) as cursor:
                row = await cursor.fetchone()
            suspended_scribe = dict(row) if row else None
            if suspended_scribe is not None:
                sess_id = suspended_scribe["session_id"]
                channel_id = suspended_scribe.get("slack_channel_id")
                claude_sid: str | None = None
                if channel_id:
                    channel = await registry.get_channel(channel_id)
                    claude_sid = (
                        channel["claude_session_id"]
                        if channel and channel.get("claude_session_id")
                        else suspended_scribe.get("claude_session_id")
                    )
                scribe_cwd = (
                    suspended_scribe.get("cwd")
                    or self._config.scribe_cwd
                    or str(get_data_dir() / "scribe")
                )
                pathlib.Path(scribe_cwd).mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
                options = SessionOptions(
                    cwd=scribe_cwd,
                    name="scribe",
                    model=self._config.scribe_model,
                    scribe_profile=True,
                    scan_interval_s=max(60, self._config.scribe_scan_interval_minutes * 60),
                    resume=claude_sid,
                    channel_id=channel_id,
                )
                try:
                    if channel_id:
                        self._check_channel_available(channel_id)
                    await self.create_resumed_session(
                        options,
                        authenticated_user_id=user_id,
                    )
                    await registry.update_status(sess_id, "completed")
                    logger.info("SessionManager: resumed suspended scribe %s", sess_id[:8])
                    return
                except Exception as e:
                    logger.error("SessionManager: failed to resume scribe %s: %s", sess_id[:8], e)
                    with contextlib.suppress(Exception):
                        await registry.update_status(
                            sess_id,
                            "errored",
                            error_message=f"Resume failed: {e}",
                        )

        # No suspended scribe found — start fresh
        self._start_scribe_if_enabled(user_id)

    def _start_scribe_if_enabled(self, user_id: str) -> None:
        """Spawn the global Scribe session if configured and not already running."""
        if not self._config.scribe_enabled:
            return

        # Check for already-running scribe
        for sess in self._sessions.values():
            if sess.is_scribe:
                logger.info("SessionManager: scribe already running — skipping")
                return

        # Pre-flight: validate dependencies before spawning.
        # workspace-mcp uses bare top-level modules (not a 'workspace_mcp' package),
        # so find_spec('workspace_mcp') always returns None. Use the binary check.
        if self._config.scribe_google_enabled:
            from summon_claude.config import (  # noqa: PLC0415
                discover_google_accounts,
                find_workspace_mcp_bin,
            )

            if not find_workspace_mcp_bin().exists():
                logger.error("Scribe requires Google support: pip install summon-claude[google]")
                return
            accounts = discover_google_accounts()
            if not accounts:
                logger.error(
                    "Scribe Google enabled but no accounts discovered. "
                    "Run 'summon auth google setup' then 'summon auth google login'"
                )
                return

        if self._config.scribe_slack_enabled:
            import importlib.util  # noqa: PLC0415

            if importlib.util.find_spec("playwright") is None:
                logger.error(
                    "Scribe Slack support requires: pip install summon-claude[slack-browser]"
                )
                return
            # Check for Slack auth state
            from summon_claude.config import get_workspace_config_path  # noqa: PLC0415

            ws_config = get_workspace_config_path()
            if not ws_config.is_file():
                logger.error("Run 'summon auth slack login' before enabling scribe Slack")
                return

        # Resolve CWD
        scribe_cwd = self._config.scribe_cwd or str(get_data_dir() / "scribe")
        pathlib.Path(scribe_cwd).mkdir(parents=True, exist_ok=True)

        new_session_id = str(uuid.uuid4())
        scribe_options = SessionOptions(
            cwd=scribe_cwd,
            name="scribe",
            model=self._config.scribe_model,
            scribe_profile=True,
            scan_interval_s=max(60, self._config.scribe_scan_interval_minutes * 60),
        )
        scribe_options = self._inject_proxy_options(scribe_options)
        new_session = SummonSession(
            config=self._config,
            options=scribe_options,
            auth=None,
            session_id=new_session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
            bot_team_id=self._bot_team_id,
            ipc_spawn=self.create_session_with_spawn_token,
            ipc_resume=self._ipc_resume,
            ipc_queue=self.queue_session,
        )
        new_session.authenticate(user_id)

        self._cancel_grace_timer()
        self._sessions[new_session_id] = new_session
        task = asyncio.create_task(
            self._supervised_session(new_session, new_session_id),
            name=f"session-scribe-{new_session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=new_session_id))
        self._tasks[new_session_id] = task

        logger.info("SessionManager: started scribe session %s", new_session_id)

    def _resolve_gpm_cwd(self, suspended_cwd: str | None = None) -> str:
        """Resolve Global PM working directory with mkdir."""
        cwd = suspended_cwd or self._config.global_pm_cwd or str(get_data_dir() / "global-pm")
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        return cwd

    async def _resume_or_start_global_pm(self, user_id: str) -> None:
        """Resume a suspended Global PM or start fresh.

        Called from ``_project_up_orchestrator`` after PMs, before scribe.
        """
        # Check for already-running GPM
        for sess in self._sessions.values():
            if sess.is_global_pm:
                logger.info("SessionManager: Global PM already running — skipping")
                return

        # Look for a suspended GPM session to resume
        prev_channel_id: str | None = None
        async with SessionRegistry() as registry:
            async with registry.db.execute(
                "SELECT * FROM sessions"
                " WHERE status = 'suspended'"
                "   AND session_name = 'global-pm'"
                "   AND project_id IS NULL"
                " ORDER BY started_at DESC LIMIT 1",
            ) as cursor:
                row = await cursor.fetchone()
            suspended_gpm = dict(row) if row else None
            if suspended_gpm is not None:
                sess_id = suspended_gpm["session_id"]
                channel_id = suspended_gpm.get("slack_channel_id")
                claude_sid: str | None = None
                if channel_id:
                    channel = await registry.get_channel(channel_id)
                    claude_sid = (
                        channel["claude_session_id"]
                        if channel and channel.get("claude_session_id")
                        else suspended_gpm.get("claude_session_id")
                    )
                gpm_cwd = self._resolve_gpm_cwd(suspended_gpm.get("cwd"))
                options = SessionOptions(
                    cwd=gpm_cwd,
                    name="global-pm",
                    model=self._config.global_pm_model,
                    pm_profile=True,
                    global_pm_profile=True,
                    scan_interval_s=max(60, self._config.global_pm_scan_interval_minutes * 60),
                    resume=claude_sid,
                    channel_id=channel_id,
                )
                try:
                    if channel_id:
                        self._check_channel_available(channel_id)
                    await self.create_resumed_session(
                        options,
                        authenticated_user_id=user_id,
                    )
                    await registry.update_status(sess_id, "completed")
                    logger.info("SessionManager: resumed suspended Global PM %s", sess_id[:8])
                    return
                except Exception as e:
                    logger.error(
                        "SessionManager: failed to resume Global PM %s: %s", sess_id[:8], e
                    )
                    with contextlib.suppress(Exception):
                        await registry.update_status(
                            sess_id,
                            "errored",
                            error_message=f"Resume failed: {e}",
                        )

            # No suspended GPM — look up previous channel to reuse (perf: skip paginated scan)
            async with registry.db.execute(
                "SELECT slack_channel_id FROM sessions"
                " WHERE session_name = 'global-pm'"
                "   AND project_id IS NULL"
                "   AND slack_channel_id IS NOT NULL"
                " ORDER BY started_at DESC LIMIT 1",
            ) as cursor:
                prev_row = await cursor.fetchone()
            if prev_row:
                prev_channel_id = prev_row["slack_channel_id"]

        # No suspended GPM found — start fresh
        self._start_global_pm(user_id, channel_id=prev_channel_id)

    def _start_global_pm(self, user_id: str, channel_id: str | None = None) -> None:
        """Spawn a fresh Global PM session."""
        gpm_cwd = self._resolve_gpm_cwd()

        new_session_id = str(uuid.uuid4())
        gpm_options = SessionOptions(
            cwd=gpm_cwd,
            name="global-pm",
            model=self._config.global_pm_model,
            pm_profile=True,
            global_pm_profile=True,
            scan_interval_s=max(60, self._config.global_pm_scan_interval_minutes * 60),
            channel_id=channel_id,
        )
        gpm_options = self._inject_proxy_options(gpm_options)
        new_session = SummonSession(
            config=self._config,
            options=gpm_options,
            auth=None,
            session_id=new_session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
            bot_team_id=self._bot_team_id,
            ipc_spawn=self.create_session_with_spawn_token,
            ipc_resume=self._ipc_resume,
            ipc_queue=self.queue_session,
        )
        new_session.authenticate(user_id)

        self._cancel_grace_timer()
        self._sessions[new_session_id] = new_session
        task = asyncio.create_task(
            self._supervised_session(new_session, new_session_id),
            name=f"session-global-pm-{new_session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=new_session_id))
        self._tasks[new_session_id] = task

        logger.info("SessionManager: started Global PM session %s", new_session_id)

    async def _alert_channel(self, session: SummonSession, message: str) -> None:
        """Best-effort Slack notification to a session's channel."""
        if not session.channel_id or not self._web_client:
            logger.debug("_alert_channel skipped (no channel or web_client)")
            return
        with contextlib.suppress(Exception):
            safe_msg = redact_secrets(message)
            await self._web_client.chat_postMessage(
                channel=session.channel_id,
                text=safe_msg,
            )

    def _on_task_done(self, task: asyncio.Task, session_id: str) -> None:  # type: ignore[type-arg]
        """Cleanup callback fired when a session task finishes (any outcome)."""
        session = self._sessions.pop(session_id, None)
        self._tasks.pop(session_id, None)

        if session is not None and session.channel_id:
            self._dispatcher.unregister(session.channel_id)

        # Clear PM topic cache and drain orphaned queue when a PM exits
        if session is not None and session.is_pm and session.project_id:
            self._pm_topic_cache.pop(session.project_id, None)
            self.clear_queue(session.project_id)

        # Update PM topic if a non-PM child with a project finished; also try dequeue
        if session is not None and not session.is_pm and session.project_id:
            # _on_task_done is synchronous (done_callback); schedule async update
            with contextlib.suppress(RuntimeError):
                t = asyncio.get_running_loop().create_task(
                    self._update_pm_topic(session.project_id)
                )
                t.add_done_callback(self._on_background_task_done)
                self._background_tasks.add(t)
            # Dequeue next waiting session for this project (if any)
            if session.project_id in self._session_queue:
                with contextlib.suppress(RuntimeError):
                    t = asyncio.get_running_loop().create_task(
                        self._dequeue_and_start(session.project_id)
                    )
                    t.add_done_callback(self._on_background_task_done)
                    self._background_tasks.add(t)

        # Log unexpected task exceptions (CancelledError is expected on shutdown)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Session %s task raised: %s", session_id, redact_secrets(str(exc)), exc_info=exc
                )

        # Start grace timer when no sessions and no queued sessions remain
        if not self._sessions and not self._session_queue:
            self._start_grace_timer()

    async def _update_pm_topic(self, project_id: str) -> None:
        """Update the PM's channel topic with the current child session count."""
        pm_session = next(
            (s for s in self._sessions.values() if s.is_pm and s.project_id == project_id),
            None,
        )
        if not pm_session or not pm_session.channel_id:
            return

        child_count = sum(
            1 for s in self._sessions.values() if s.project_id == project_id and not s.is_pm
        )
        topic = format_pm_topic(child_count)
        if self._pm_topic_cache.get(project_id) == topic:
            return
        try:
            await self._web_client.conversations_setTopic(
                channel=pm_session.channel_id,
                topic=topic,
            )
            self._pm_topic_cache[project_id] = topic
        except Exception:
            logger.warning("Failed to update PM topic for project %s", project_id, exc_info=True)

    def _on_background_task_done(self, task: asyncio.Task) -> None:  # type: ignore[type-arg]
        """Cleanup callback for background tasks (orchestrators, etc.)."""
        self._background_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Background task %s failed: %s", task.get_name(), redact_secrets(str(exc))
                )

    def _start_grace_timer(self) -> None:
        """Schedule daemon auto-stop after ``_GRACE_SECONDS`` with no sessions."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("SessionManager: _start_grace_timer called outside event loop")
            return

        self._grace_timer = loop.call_later(_GRACE_SECONDS, self._shutdown_event.set)
        logger.info(
            "SessionManager: no active sessions — daemon will stop in %.0fs",
            _GRACE_SECONDS,
        )

    def _cancel_grace_timer(self) -> None:
        """Cancel the daemon auto-stop grace timer if it is running."""
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None

    # ------------------------------------------------------------------
    # Session queue (FIFO per project)
    # ------------------------------------------------------------------

    def queue_session(
        self,
        options: SessionOptions,
        *,
        project_id: str,
        pm_session_id: str,
        authenticated_user_id: str,
        parent_channel_id: str | None = None,
    ) -> int:
        """Enqueue a session for deferred startup.

        Returns the queue position (1-based) for this project's queue,
        or -1 if the queue is full.
        """
        q = self._session_queue.setdefault(project_id, collections.deque())
        if len(q) >= _MAX_QUEUED_SESSIONS:
            logger.warning(
                "SessionManager: session queue full (%d/%d) for project %s",
                len(q),
                _MAX_QUEUED_SESSIONS,
                project_id,
            )
            return -1
        entry = _QueuedSession(
            options=options,
            project_id=project_id,
            pm_session_id=pm_session_id,
            authenticated_user_id=authenticated_user_id,
            queued_at=time.monotonic(),
            parent_channel_id=parent_channel_id,
        )
        q.append(entry)
        position = len(q)
        logger.info(
            "SessionManager: queued session '%s' for project %s (position %d)",
            options.name,
            project_id,
            position,
        )
        return position

    async def _dequeue_and_start(self, project_id: str) -> None:
        """Dequeue the next waiting session for *project_id* and start it.

        The lock serializes cap-check + pop + session-create to prevent
        concurrent dequeues from overshooting the cap.
        """
        # Fast path: bail early if no queue (no lock needed)
        if project_id not in self._session_queue:
            return

        async with self._queue_lock:
            q = self._session_queue.get(project_id)
            if not q:
                return

            # Find the live PM for parent linkage
            live_pm_session = next(
                (s for s in self._sessions.values() if s.is_pm and s.project_id == project_id),
                None,
            )
            if live_pm_session is None:
                return

            # Cap check inside the lock to prevent TOCTOU overshoot
            children_count = sum(
                1
                for sid, s in self._sessions.items()
                if sid != live_pm_session._session_id  # noqa: SLF001
                and s.project_id == project_id
                and not s.is_pm
            )
            if children_count >= MAX_SPAWN_CHILDREN_PM:
                return

            entry = q.popleft()
            if not q:
                self._session_queue.pop(project_id, None)

            live_pm_sid = live_pm_session._session_id  # noqa: SLF001

            # Session creation inside lock to prevent TOCTOU between cap
            # check and _sessions registration
            new_session_id = str(uuid.uuid4())
            entry_opts = self._inject_proxy_options(entry.options)
            try:
                session = SummonSession(
                    config=self._config,
                    options=entry_opts,
                    auth=None,
                    session_id=new_session_id,
                    web_client=self._web_client,
                    dispatcher=self._dispatcher,
                    bot_user_id=self._bot_user_id,
                    bot_team_id=self._bot_team_id,
                    parent_session_id=live_pm_sid,
                    parent_channel_id=entry.parent_channel_id,
                    ipc_spawn=self.create_session_with_spawn_token,
                    ipc_resume=self._ipc_resume,
                    ipc_queue=self.queue_session,
                )
                session.authenticate(entry.authenticated_user_id)
            except Exception as exc:
                logger.error(
                    "Failed to create queued session '%s': %s",
                    entry.options.name,
                    redact_secrets(str(exc)),
                )
                # Don't re-queue: SummonSession() failures are typically
                # deterministic (bad config, deleted cwd). Re-queuing would
                # create an infinite retry loop.
                return

            self._cancel_grace_timer()
            self._sessions[new_session_id] = session
            task = asyncio.create_task(
                self._supervised_session(session, new_session_id),
                name=f"session-dequeued-{new_session_id}",
            )
            task.add_done_callback(partial(self._on_task_done, session_id=new_session_id))
            self._tasks[new_session_id] = task

        wait_s = time.monotonic() - entry.queued_at
        logger.info(
            "SessionManager: dequeued session '%s' (%s) for project %s (waited %.1fs)",
            entry.options.name,
            new_session_id,
            project_id,
            wait_s,
        )

        # Fire-and-forget notifications (outside lock — non-critical)
        t = asyncio.create_task(
            self._update_pm_topic(project_id),
            name=f"dequeue-topic-{new_session_id}",
        )
        t.add_done_callback(self._on_background_task_done)
        self._background_tasks.add(t)

        t = asyncio.create_task(
            self._notify_pm_of_dequeue(entry, new_session_id, live_pm_session=live_pm_session),
            name=f"dequeue-notify-{new_session_id}",
        )
        t.add_done_callback(self._on_background_task_done)
        self._background_tasks.add(t)

    async def _notify_pm_of_dequeue(
        self,
        entry: _QueuedSession,
        new_session_id: str,
        *,
        live_pm_session: SummonSession | None = None,
    ) -> None:
        """Best-effort: notify the PM's channel that a queued session started.

        Uses *live_pm_session* when provided (avoids stale ``pm_session_id``
        lookups if the PM restarted between queue time and dequeue time).
        Falls back to looking up ``entry.pm_session_id`` for callers that
        don't thread the live session through (e.g. direct test calls).
        """
        if live_pm_session is not None:
            pm_session = live_pm_session
        else:
            pm_session = self._sessions.get(entry.pm_session_id)
        if pm_session is None:
            return
        initial_prompt = entry.options.initial_prompt or ""
        sanitized = sanitize_for_slack(initial_prompt)
        snippet = sanitized[:200]
        suffix = "..." if len(sanitized) > 200 else ""
        msg = f"Queued session '{entry.options.name}' has started (session_id: {new_session_id})."
        if snippet:
            msg += f"\nInitial prompt: {snippet}{suffix}"
        with contextlib.suppress(Exception):
            await pm_session.inject_message(msg, sender_info="session-queue")

    def clear_queue(self, project_id: str) -> int:
        """Remove all queued sessions for *project_id*. Returns count cleared."""
        q = self._session_queue.pop(project_id, None)
        count = len(q) if q else 0
        if count:
            logger.info(
                "SessionManager: cleared %d queued session(s) for project %s",
                count,
                project_id,
            )
        return count

    @staticmethod
    def _is_recoverable(exc: Exception) -> bool:
        """Return ``True`` for transient errors worth retrying.

        Recoverable: ``ConnectionError``, ``TimeoutError``, ``OSError``
        (covers socket drops, DNS hiccups, OS resource limits).
        All others are treated as fatal (bad credentials, SDK crash, etc.).
        """
        return isinstance(exc, (ConnectionError, TimeoutError, OSError))

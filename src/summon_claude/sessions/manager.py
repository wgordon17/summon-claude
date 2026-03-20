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
from summon_claude.sessions.auth import (
    SessionAuth,
    SpawnAuth,
    generate_session_token,
    verify_short_code,
    verify_spawn_token,
)
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.sessions.session import (
    _SECRET_PATTERN,
    SessionOptions,
    SummonSession,
    format_pm_topic,
)

if TYPE_CHECKING:
    from summon_claude.config import SummonConfig
    from summon_claude.event_dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

_GRACE_SECONDS = 60.0
_SHUTDOWN_WAIT_TIMEOUT = 30.0


class SessionManager:
    """Manages the lifecycle of all sessions running inside the daemon.

    Constructor is synchronous — call from the daemon's async entry point.
    ``_start_time`` is recorded for uptime reporting in ``status`` responses.
    """

    MAX_SESSION_RESTARTS = 3

    def __init__(
        self,
        config: SummonConfig,
        web_client: AsyncWebClient,
        bot_user_id: str,
        dispatcher: EventDispatcher,
    ) -> None:
        self._config = config
        self._web_client = web_client
        self._bot_user_id = bot_user_id
        self._dispatcher = dispatcher
        self._tasks: dict[str, asyncio.Task] = {}  # session_id → task
        self._sessions: dict[str, SummonSession] = {}  # session_id → session
        self._grace_timer: asyncio.TimerHandle | None = None
        self._shutdown_event = asyncio.Event()
        self._start_time: float = time.monotonic()
        self._project_up_in_flight = False  # guard against concurrent project_up
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._pm_topic_cache: dict[str, str] = {}  # project_id → last-set topic

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
        """
        # Cancel grace timer — a new session has arrived
        self._cancel_grace_timer()

        session_id = str(uuid.uuid4())
        auth = await self._generate_auth(session_id)

        session = SummonSession(
            config=self._config,
            options=options,
            auth=auth,
            session_id=session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
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

        session = SummonSession(
            config=self._config,
            options=options,
            auth=None,
            session_id=session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
            parent_session_id=spawn_auth.parent_session_id,
            parent_channel_id=spawn_auth.parent_channel_id,
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

        Phase 1: Signal every session to stop.
        Phase 2: Wait up to 30 seconds for tasks to drain.
        Phase 3: Force-cancel any remaining tasks.
        """
        self._cancel_grace_timer()

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

    # ------------------------------------------------------------------
    # Unix socket control API
    # ------------------------------------------------------------------

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

    async def _dispatch_control(self, msg: dict) -> dict:  # type: ignore[type-arg]
        """Route a control message to the appropriate handler and return a response."""
        match msg.get("type"):
            case "create_session":
                try:
                    options = SessionOptions(**msg["options"])
                except (TypeError, KeyError) as e:
                    return {"type": "error", "message": f"Invalid session options: {e}"}
                short_code = await self.create_session(options)
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
                            "status": "active",
                        }
                        for sid, s in self._sessions.items()
                    ],
                }

            case "create_session_with_spawn_token":
                try:
                    options = SessionOptions(**msg["options"])
                    spawn_token = msg["spawn_token"]
                except (TypeError, KeyError) as e:
                    return {"type": "error", "message": f"Invalid request: {e}"}
                try:
                    session_id = await self.create_session_with_spawn_token(options, spawn_token)
                except ValueError as e:
                    return {"type": "error", "message": str(e)}
                return {"type": "session_created_spawned", "session_id": session_id}

            case "project_up":
                return await self._handle_project_up(msg)

            case _:
                return {"type": "error", "message": f"Unknown command: {msg.get('type')}"}

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
                        e,
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
                        _SECRET_PATTERN.sub("***", str(e)),
                    )
                    recovery_hint = "\nRun `summon project up` to restart." if session.is_pm else ""
                    await self._alert_channel(
                        session,
                        f":x: *Session terminated unexpectedly*: `{e}`{recovery_hint}",
                    )
                    break

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
            if not needing_pm:
                self._project_up_in_flight = False
                return {"type": "project_up_complete"}

            cwd = msg.get("cwd", str(pathlib.Path.cwd()))

            # Create auth-only session
            self._cancel_grace_timer()
            session_id = str(uuid.uuid4())
            auth = await self._generate_auth(session_id)
            options = SessionOptions(
                cwd=cwd,
                name=f"pm-auth-{secrets.token_hex(3)}",
                auth_only=True,
            )
            session = SummonSession(
                config=self._config,
                options=options,
                auth=auth,
                session_id=session_id,
                web_client=self._web_client,
                dispatcher=self._dispatcher,
                bot_user_id=self._bot_user_id,
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
        """Background task: wait for auth, then create PM + restart suspended sessions."""
        try:
            async with asyncio.timeout(360):
                await auth_session._authenticated_event.wait()  # noqa: SLF001
            user_id = auth_session._authenticated_user_id  # noqa: SLF001
            if user_id is None:
                raise RuntimeError("Auth session completed without setting user_id")

            for project in needing_pm:
                try:
                    self._start_pm_for_project(project, user_id)
                except Exception as e:
                    logger.error(
                        "PM: failed to start session for project %s: %s",
                        project.get("name", "?"),
                        e,
                    )

            # Cascade restart: revive sessions suspended by project down
            await self._restart_suspended_sessions(needing_pm, user_id)

        except TimeoutError:
            logger.error("project_up orchestrator: authentication timed out")
        except Exception as e:
            logger.error("project_up orchestrator failed: %s", e, exc_info=True)
        finally:
            self._project_up_in_flight = False

    async def _restart_suspended_sessions(
        self,
        projects: list[dict[str, Any]],
        user_id: str,
    ) -> None:
        """Restart sessions that were suspended by ``project down``."""
        async with SessionRegistry() as registry:
            for project in projects:
                project_id = project["project_id"]
                sessions = await registry.get_project_sessions(project_id)
                suspended = [s for s in sessions if s.get("status") == "suspended"]
                for sess in suspended:
                    try:
                        self._start_child_session(
                            project=project,
                            user_id=user_id,
                            cwd=sess.get("cwd", project["directory"]),
                            model=sess.get("model"),
                        )
                        # Mark old suspended session as completed
                        await registry.update_status(sess["session_id"], "completed")
                        logger.info(
                            "PM: restarted suspended session %s for project %s",
                            sess["session_id"][:8],
                            project["name"],
                        )
                    except Exception as e:
                        logger.error(
                            "PM: failed to restart suspended session %s: %s",
                            sess.get("session_id", "?")[:8],
                            e,
                        )
                        # Mark as errored to prevent infinite retry on next project up
                        with contextlib.suppress(Exception):
                            await registry.update_status(
                                sess["session_id"],
                                "errored",
                                error_message=f"Restart failed: {e}",
                            )

    def _start_child_session(
        self,
        project: dict[str, Any],
        user_id: str,
        cwd: str,
        model: str | None = None,
    ) -> None:
        """Create and start a regular (non-PM) child session for *project*."""
        if not pathlib.Path(cwd).is_dir():
            raise FileNotFoundError(f"Directory not found: {cwd}")

        new_session_id = str(uuid.uuid4())
        options = SessionOptions(
            cwd=cwd,
            name=f"{project['channel_prefix']}-{secrets.token_hex(3)}",
            model=model,
            project_id=project["project_id"],
        )
        new_session = SummonSession(
            config=self._config,
            options=options,
            auth=None,
            session_id=new_session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
        )
        new_session.authenticate(user_id)

        self._cancel_grace_timer()
        self._sessions[new_session_id] = new_session
        task = asyncio.create_task(
            self._supervised_session(new_session, new_session_id),
            name=f"session-child-{new_session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=new_session_id))
        self._tasks[new_session_id] = task

        logger.info(
            "SessionManager: started child session %s for project %s (cwd=%s)",
            new_session_id,
            project["name"],
            cwd,
        )

        # Update PM topic now that a new child is tracked
        t = asyncio.create_task(self._update_pm_topic(project["project_id"]))
        t.add_done_callback(self._on_background_task_done)
        self._background_tasks.add(t)

    def _start_pm_for_project(self, project: dict[str, Any], user_id: str) -> None:
        """Create and start a single PM session for *project*."""
        project_dir = project["directory"]
        if not pathlib.Path(project_dir).is_dir():
            raise FileNotFoundError(f"Directory not found: {project_dir}")

        new_session_id = str(uuid.uuid4())
        pm_options = SessionOptions(
            cwd=project_dir,
            name=f"{project['channel_prefix']}-pm-{secrets.token_hex(3)}",
            pm_profile=True,
            project_id=project["project_id"],
        )
        new_session = SummonSession(
            config=self._config,
            options=pm_options,
            auth=None,
            session_id=new_session_id,
            web_client=self._web_client,
            dispatcher=self._dispatcher,
            bot_user_id=self._bot_user_id,
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

    async def _alert_channel(self, session: SummonSession, message: str) -> None:
        """Best-effort Slack notification to a session's channel."""
        if not session.channel_id or not self._web_client:
            logger.debug("_alert_channel skipped (no channel or web_client)")
            return
        with contextlib.suppress(Exception):
            safe_msg = _SECRET_PATTERN.sub("***", message)
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

        # Clear PM topic cache when a PM exits so a replacement PM gets a fresh topic
        if session is not None and session.is_pm and session.project_id:
            self._pm_topic_cache.pop(session.project_id, None)

        # Update PM topic if a non-PM child with a project finished
        if session is not None and not session.is_pm and session.project_id:
            # _on_task_done is synchronous (done_callback); schedule async update
            with contextlib.suppress(RuntimeError):
                t = asyncio.get_running_loop().create_task(
                    self._update_pm_topic(session.project_id)
                )
                t.add_done_callback(self._on_background_task_done)
                self._background_tasks.add(t)

        # Log unexpected task exceptions (CancelledError is expected on shutdown)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("Session %s task raised: %s", session_id, exc, exc_info=exc)

        # Start grace timer when no sessions remain
        if not self._sessions:
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
                logger.error("Background task %s failed: %s", task.get_name(), exc)

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

    @staticmethod
    def _is_recoverable(exc: Exception) -> bool:
        """Return ``True`` for transient errors worth retrying.

        Recoverable: ``ConnectionError``, ``TimeoutError``, ``OSError``
        (covers socket drops, DNS hiccups, OS resource limits).
        All others are treated as fatal (bad credentials, SDK crash, etc.).
        """
        return isinstance(exc, (ConnectionError, TimeoutError, OSError))

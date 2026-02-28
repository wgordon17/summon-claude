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
import logging
import os
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING

from summon_claude.auth import generate_session_token, verify_short_code
from summon_claude.ipc import recv_msg, send_msg
from summon_claude.registry import SessionRegistry
from summon_claude.session import SessionOptions, SummonSession

if TYPE_CHECKING:
    from summon_claude.bolt_router import BoltRouter
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
        bolt_router: BoltRouter,
        dispatcher: EventDispatcher,
    ) -> None:
        self._config = config
        self._bolt_router = bolt_router
        self._dispatcher = dispatcher
        self._tasks: dict[str, asyncio.Task] = {}  # session_id → task
        self._sessions: dict[str, SummonSession] = {}  # session_id → session
        self._grace_timer: asyncio.TimerHandle | None = None
        self._shutdown_event = asyncio.Event()
        self._start_time: float = time.monotonic()

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    @property
    def shutdown_event(self) -> asyncio.Event:
        """Event that is set when the daemon should stop."""
        return self._shutdown_event

    async def create_session(self, options: SessionOptions) -> tuple[str, str]:
        """Create and start a supervised session task.

        Generates a new ``session_id`` and auth token internally so the CLI
        does not need to touch the SQLite registry.

        Returns:
            ``(session_id, short_code)`` — the short code is printed by the CLI
            so the user can authenticate via ``/summon <code>`` in Slack.
        """
        # Cancel grace timer — a new session has arrived
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None

        session_id = str(uuid.uuid4())
        # Generate auth token in the daemon (single process owns the registry)
        async with SessionRegistry() as registry:
            auth = await generate_session_token(registry, session_id)

        full_options = SessionOptions(
            session_id=session_id,
            cwd=options.cwd,
            name=options.name,
            model=options.model,
            resume=options.resume,
        )
        session = SummonSession(
            config=self._config,
            options=full_options,
            auth=auth,
            shared_provider=self._bolt_router.provider,
            dispatcher=self._dispatcher,
            bot_user_id=self._bolt_router.bot_user_id,
        )
        self._sessions[session_id] = session

        task = asyncio.create_task(
            self._supervised_session(session, session_id),
            name=f"session-{session_id}",
        )
        task.add_done_callback(partial(self._on_task_done, session_id=session_id))
        self._tasks[session_id] = task
        logger.info("SessionManager: created session %s", session_id)
        return session_id, auth.short_code

    async def stop_session(self, session_id: str) -> bool:
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
        if self._grace_timer is not None:
            self._grace_timer.cancel()
            self._grace_timer = None

        # Phase 1 — signal
        for session in list(self._sessions.values()):
            session.request_shutdown()

        # Phase 2 — wait (bounded)
        if self._tasks:
            _done, pending = await asyncio.wait(
                list(self._tasks.values()), timeout=_SHUTDOWN_WAIT_TIMEOUT
            )
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
                options = SessionOptions(**msg["options"])
                sid, short_code = await self.create_session(options)
                return {
                    "type": "session_created",
                    "session_id": sid,
                    "short_code": short_code,
                }

            case "stop_session":
                found = await self.stop_session(msg["session_id"])
                return {"type": "session_stopped", "found": found}

            case "status":
                return {
                    "type": "status",
                    "pid": os.getpid(),
                    "uptime": time.monotonic() - self._start_time,
                    "sessions": [
                        {
                            "session_id": sid,
                            "channel_id": getattr(s, "channel_id", None),
                        }
                        for sid, s in self._sessions.items()
                    ],
                }

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
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "Session %s failed permanently: %s",
                        session_id,
                        e,
                        exc_info=True,
                    )
                    # Best-effort: post error notice to the session channel
                    channel_id = getattr(session, "channel_id", None)
                    if channel_id:
                        with contextlib.suppress(Exception):
                            await self._bolt_router.provider.post_message(
                                channel_id,
                                f":x: Session terminated unexpectedly: {e}",
                            )
                    break

    def _on_task_done(self, task: asyncio.Task, session_id: str) -> None:  # type: ignore[type-arg]
        """Cleanup callback fired when a session task finishes (any outcome)."""
        session = self._sessions.pop(session_id, None)
        self._tasks.pop(session_id, None)

        if session is not None:
            channel_id = getattr(session, "channel_id", None)
            if channel_id:
                self._dispatcher.unregister(channel_id)

        # Log unexpected task exceptions (CancelledError is expected on shutdown)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("Session %s task raised: %s", session_id, exc, exc_info=exc)

        # Start grace timer when no sessions remain
        if not self._sessions:
            self._start_grace_timer()

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

    @staticmethod
    def _is_recoverable(exc: Exception) -> bool:
        """Return ``True`` for transient errors worth retrying.

        Recoverable: ``ConnectionError``, ``TimeoutError``, ``OSError``
        (covers socket drops, DNS hiccups, OS resource limits).
        All others are treated as fatal (bad credentials, SDK crash, etc.).
        """
        return isinstance(exc, (ConnectionError, TimeoutError, OSError))

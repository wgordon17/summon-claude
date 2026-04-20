"""Daemon process lifecycle for the single-Bolt architecture.

The daemon owns a single ``BoltRouter`` (one Slack WebSocket connection) that
routes events to all concurrent sessions running as asyncio tasks.

Public API
----------
- ``daemon_main(config)``   — async entry point; runs until shutdown.
- ``run_daemon(config)``    — acquire lock, write PID, call asyncio.run().
- ``start_daemon(config)``  — fork daemon in background, wait for socket.
- ``is_daemon_running()``   — True if daemon socket exists and accepts connections.
- ``connect_to_daemon()``   — open Unix socket connection to running daemon.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import logging.handlers
import os
import queue
import signal
import socket
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING

from summon_claude.config import get_data_dir, get_socket_path, is_local_install
from summon_claude.event_dispatcher import EventDispatcher
from summon_claude.sessions.manager import SessionManager
from summon_claude.sessions.registry import SessionRegistry
from summon_claude.slack.bolt import BoltRouter
from summon_claude.slack.client import ZZZ_PREFIX, make_zzz_name

if TYPE_CHECKING:
    from slack_sdk.web.async_client import AsyncWebClient

    from summon_claude.config import SummonConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IPC framing protocol (absorbed from ipc.py)
#
# Messages are framed with a 4-byte big-endian length prefix followed by a
# JSON-encoded payload.  The maximum allowed message size is 64 KiB.
# ---------------------------------------------------------------------------

MAX_MESSAGE_SIZE = 65_536  # 64 KiB

_RECV_TIMEOUT: float = 30.0
"""Maximum seconds to wait for a complete IPC message."""


async def send_msg(writer: asyncio.StreamWriter, data: dict) -> None:  # type: ignore[type-arg]
    """Encode *data* as JSON and write it to *writer* with a 4-byte length prefix."""
    payload: bytes = json.dumps(data).encode()
    writer.write(struct.pack(">I", len(payload)) + payload)
    await writer.drain()


async def recv_msg(
    reader: asyncio.StreamReader,
    *,
    timeout: float | None = None,  # noqa: ASYNC109
) -> dict:  # type: ignore[type-arg]
    """Read a length-prefixed message from *reader* and return the decoded dict.

    Args:
        reader: The stream to read from.
        timeout: Per-read timeout in seconds.  Defaults to ``_RECV_TIMEOUT``.

    Raises:
        ValueError: If the declared message length exceeds MAX_MESSAGE_SIZE.
        asyncio.IncompleteReadError: If the connection closes before a full
            message is received.
        TimeoutError: If the message is not fully received within *timeout*.
    """
    t = timeout if timeout is not None else _RECV_TIMEOUT
    header: bytes = await asyncio.wait_for(reader.readexactly(4), timeout=t)
    length: int = struct.unpack(">I", header)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
    payload: bytes = await asyncio.wait_for(reader.readexactly(length), timeout=t)
    return json.loads(payload)  # type: ignore[no-any-return]


# Watchdog constants — daemon-level event loop health monitoring
_WATCHDOG_CHECK_INTERVAL_S = 15.0  # how often to check event loop progress
_WATCHDOG_THRESHOLD_S = 90.0  # stall threshold before forced shutdown
_SIGALRM_TIMEOUT_S = 120  # OS-level last-resort watchdog (seconds)

# ---------------------------------------------------------------------------
# Path constants (lazy — resolved at call time so tests can patch get_data_dir)
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    return get_data_dir()


def _daemon_socket() -> Path:
    return get_socket_path()


def _validate_socket_path(sock: Path) -> None:
    """Raise ``SocketPathTooLongError`` if *sock* exceeds the Unix limit.

    Called at daemon startup rather than on every path resolution so tests
    using long ``tmp_path`` directories are not affected.
    """
    sock_str = str(sock)
    if len(sock_str) > _UNIX_SOCKET_PATH_MAX:
        if is_local_install():
            hint = (
                "This should not occur with the runtime-directory socket path. "
                "Please file a bug at https://github.com/summon-claude/summon-claude/issues."
            )
        else:
            hint = (
                "Set XDG_DATA_HOME to a shorter path (e.g. export "
                "XDG_DATA_HOME=~/.local/share) or use the default ~/.summon directory."
            )
        raise SocketPathTooLongError(
            f"Daemon socket path is {len(sock_str)} chars, exceeding the "
            f"Unix limit of {_UNIX_SOCKET_PATH_MAX}.\n"
            f"  Path: {sock_str}\n"
            f"{hint}"
        )


def _daemon_pid() -> Path:
    return _data_dir() / "daemon.pid"


def _daemon_lock() -> Path:
    return _data_dir() / "daemon.lock"


def _daemon_log() -> Path:
    return _data_dir() / "logs" / "daemon.log"


def _startup_error_path() -> Path:
    return _data_dir() / "last-startup-error"


# Unix socket path length limit: 104 on macOS, 108 on Linux.
# We use the lower bound so the daemon works on both platforms.
_UNIX_SOCKET_PATH_MAX = 104

_SOCKET_WAIT_TIMEOUT_S = 20.0
_SOCKET_POLL_INTERVAL_S = 0.1


class SocketPathTooLongError(RuntimeError):
    """Raised when the daemon socket path exceeds the Unix limit."""


class DaemonAlreadyRunningError(Exception):
    """Raised when a daemon process is already running."""

    def __init__(self, pid_file: Path) -> None:
        super().__init__(f"Daemon already running (see {pid_file})")


# ---------------------------------------------------------------------------
# Daemon entry points
# ---------------------------------------------------------------------------


def _write_startup_error(message: str) -> None:
    """Write a startup error message to the last-startup-error file (mode 0o600)."""
    import datetime  # noqa: PLC0415

    error_path = _startup_error_path()
    error_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().isoformat(timespec="seconds")  # noqa: DTZ005
    content = f"[{timestamp}]\n{message}\n"
    fd = os.open(str(error_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


async def _cleanup_orphaned_sessions(web_client: AsyncWebClient) -> None:
    """Mark any 'active' or 'pending_auth' sessions as errored on daemon startup.

    At daemon start, ``_sessions`` is empty — any session still marked active
    in SQLite is left over from a previous daemon instance and should be closed.
    Posts a disconnect notice to each orphaned session's Slack channel.
    """
    try:
        async with SessionRegistry() as registry:
            cleaned = await registry.cleanup_active("Orphaned by daemon restart")
            for session in cleaned:
                channel_id = session.get("slack_channel_id")
                channel_name = session.get("slack_channel_name", "")
                if channel_id:
                    # Rename with zzz- prefix BEFORE posting message
                    if channel_name and not channel_name.startswith(ZZZ_PREFIX):
                        zzz_name = make_zzz_name(channel_name)
                        try:
                            await web_client.conversations_rename(channel=channel_id, name=zzz_name)
                            logger.info(
                                "zzz-rename: #%s → #%s for orphaned session",
                                channel_name,
                                zzz_name,
                            )
                        except Exception as e:
                            logger.debug("Could not zzz-rename channel %s: %s", channel_id, e)
                    try:
                        await web_client.chat_postMessage(
                            channel=channel_id,
                            text=(
                                ":warning: *Session disconnected* (daemon restarted)\n"
                                "Channel preserved — review the "
                                "conversation history anytime."
                            ),
                        )
                    except Exception as e:
                        logger.debug("Could not post disconnect to channel %s: %s", channel_id, e)
            if cleaned:
                logger.info("Cleaned up %d orphaned session(s) from previous daemon", len(cleaned))
    except Exception as e:
        logger.warning("Failed to clean up orphaned sessions: %s", e)


async def _run_startup_probe(bolt_router: BoltRouter) -> None:
    """Run a one-time event probe at daemon startup.

    Soft-fails (WARNING + continue) for non-definitive results.
    Hard-fails (SystemExit) for definitive signals (token_revoked, socket_disabled).
    """
    event_probe = bolt_router.event_probe
    if event_probe is None:
        logger.debug("Startup event probe: skipped (probe not available)")
        return

    # Wait for WebSocket event delivery to stabilize after connect_async()
    await asyncio.sleep(2.0)
    try:
        startup_result = await event_probe.run_probe(timeout=5.0)
    except Exception as e:
        logger.warning("Startup event probe: exception during probe (%s) — continuing", e)
        return
    if startup_result.healthy:
        _startup_error_path().unlink(missing_ok=True)
        logger.info("Startup event probe: healthy")
    elif startup_result.reason in ("events_disabled", "unknown"):
        logger.warning(
            "Startup event probe: non-definitive result (%s) — continuing",
            startup_result.reason,
        )
    else:
        diagnostic_msg = event_probe.format_alert(startup_result)
        try:
            _write_startup_error(diagnostic_msg)
        except OSError as e:
            logger.warning("Could not write startup error file: %s", e)
        logger.critical(
            "Startup event probe failed: %s — %s",
            startup_result.reason,
            startup_result.details,
        )
        await bolt_router.stop()
        _daemon_pid().unlink(missing_ok=True)
        raise SystemExit(1)


async def daemon_main(config: SummonConfig) -> None:  # noqa: PLR0912, PLR0915
    """Async daemon entry point.

    Creates BoltRouter, EventDispatcher, and SessionManager, wires them
    together, starts the Unix socket control server and Bolt WebSocket, then
    waits for a shutdown signal or grace-period expiry before cleaning up.

    Also starts:
    - BoltRouter socket health monitor (reconnect on unhealthy, shutdown on exhaustion)
    - Daemon-level event loop watchdog (90s stall threshold)
    - SIGALRM OS watchdog (120s last resort, macOS/Linux only)
    """
    # Refresh path constants in case data dir moved (e.g., in tests)
    socket_path = _daemon_socket()
    pid_path = _daemon_pid()

    dispatcher = EventDispatcher()
    bolt_router = BoltRouter(config, dispatcher)

    await bolt_router.start()
    # Inject web_client now that BoltRouter has started
    dispatcher._web_client = bolt_router.web_client  # noqa: SLF001
    logger.info("BoltRouter started")

    # Mark any sessions left active from a previous daemon as errored
    await _cleanup_orphaned_sessions(bolt_router.web_client)

    # Startup probe runs BEFORE the control socket is created (line ~304).
    # On hard failure, _run_startup_probe raises SystemExit before the socket
    # appears, so _wait_for_socket times out and reads the error file.
    await _run_startup_probe(bolt_router)

    if bolt_router.bot_user_id is None:  # pragma: no cover — start() always sets this
        raise RuntimeError("BoltRouter.start() did not set bot_user_id")

    # Start Jira auth proxy if credentials exist
    jira_proxy = None
    jira_proxy_port = None
    jira_proxy_token = None
    from summon_claude.jira_auth import jira_credentials_exist  # noqa: PLC0415

    if jira_credentials_exist():
        from summon_claude.jira_proxy import JiraAuthProxy  # noqa: PLC0415

        try:
            jira_proxy = JiraAuthProxy()
            jira_proxy_port = await jira_proxy.start()
            jira_proxy_token = jira_proxy.access_token
            logger.info("Jira auth proxy started on port %d", jira_proxy_port)
        except Exception:
            logger.warning(
                "Jira proxy startup failed — sessions use direct token",
                exc_info=True,
            )
            jira_proxy = None
            jira_proxy_port = None
            jira_proxy_token = None

    try:  # try/finally ensures jira_proxy.stop() on any startup/runtime failure
        session_manager = SessionManager(
            config=config,
            web_client=bolt_router.web_client,
            bot_user_id=bolt_router.bot_user_id,
            bot_team_id=bolt_router.bot_team_id,
            dispatcher=dispatcher,
            event_probe=bolt_router.event_probe,
            jira_proxy_port=jira_proxy_port,
            jira_proxy_token=jira_proxy_token,
        )
        dispatcher.set_command_handler(session_manager.handle_summon_command)
        dispatcher.set_resume_handler(session_manager.resume_from_channel)
        dispatcher.set_app_home_handler(session_manager.handle_app_home)
        if bolt_router.bot_user_id:
            dispatcher.set_bot_user_id(bolt_router.bot_user_id)

        # Start Unix socket control server.
        # umask(0o077) ensures the socket file is created with mode 0o600 from
        # the start, closing the brief window where other users could observe it
        # under the DaemonContext's default umask of 0o022.
        old_umask = os.umask(0o077)
        try:
            control_server = await asyncio.start_unix_server(
                session_manager.handle_client,
                path=str(socket_path),
                limit=MAX_MESSAGE_SIZE,
            )
        finally:
            os.umask(old_umask)
        # chmod(0o600) as defense-in-depth — socket was already created with 0o600.
        try:
            socket_path.chmod(0o600)
        except FileNotFoundError:
            logger.debug("daemon.sock not found for chmod (abstract socket or test mock)")
        logger.info("Control socket listening at %s", socket_path)

        # Signal handling — SIGTERM/SIGINT trigger graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, session_manager.shutdown_event.set)

        # Layer 1: Socket health monitor (BoltRouter-owned)
        # Register shutdown callback so health monitor can trigger daemon shutdown on exhaustion
        bolt_router.shutdown_callback = session_manager.shutdown_event.set

        def _on_event_failure() -> None:
            session_manager.set_suspend_on_shutdown()

        bolt_router.event_failure_callback = _on_event_failure
        health_task = bolt_router.start_health_monitor()

        # Layer 2: Daemon-level event loop watchdog
        watchdog_task = asyncio.create_task(
            _watchdog_loop(session_manager.shutdown_event), name="daemon-watchdog"
        )

        # Layer 3: SIGALRM OS watchdog (last resort — only on Unix)
        _start_sigalrm_watchdog()

        # Warm up Jira proxy token cache in background (non-blocking).
        # The proxy is already functional — warmup avoids a 502 on the first
        # tool call if the cached token is expired, but doesn't block startup.
        warmup_task: asyncio.Task[None] | None = None
        if jira_proxy is not None:

            async def _jira_warmup() -> None:
                try:
                    if not await jira_proxy.warmup():
                        logger.warning(
                            "Jira proxy warmup: token refresh failed — "
                            "Jira tools will return 502 until re-auth"
                        )
                except Exception:
                    logger.warning("Jira proxy warmup failed", exc_info=True)

            warmup_task = asyncio.create_task(_jira_warmup(), name="jira-proxy-warmup")

        logger.info("Daemon started (pid=%d, socket=%s)", os.getpid(), socket_path)

        # Wait until shutdown is requested (by signal, grace timer, or error)
        await session_manager.shutdown_event.wait()

        logger.info("Daemon shutdown initiated")

        # Cancel background tasks before draining sessions
        health_task.cancel()
        watchdog_task.cancel()
        if warmup_task is not None:
            warmup_task.cancel()
        cleanup_tasks = [health_task, watchdog_task]
        if warmup_task is not None:
            cleanup_tasks.append(warmup_task)
        for task in cleanup_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception) as e:
                logger.debug("Background task cleanup: %s", e)

        # Three-phase shutdown: stop accepting → drain sessions → stop Bolt
        control_server.close()
        try:
            await asyncio.wait_for(control_server.wait_closed(), timeout=5.0)
        except TimeoutError:
            logger.debug("Control server close timed out")

        await session_manager.shutdown()

        await bolt_router.stop()

        # Disarm SIGALRM so we don't get killed during clean exit
        _disarm_sigalrm_watchdog()

        # Cleanup filesystem artefacts
        pid_path.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)

        logger.info("Daemon stopped cleanly")
    finally:
        # Stop Jira proxy after sessions drain (sessions may still be making Jira calls)
        if jira_proxy is not None:
            try:
                await jira_proxy.stop()
            except Exception:
                logger.warning("Jira proxy stop failed", exc_info=True)


async def _watchdog_loop(shutdown_event: asyncio.Event) -> None:
    """Daemon-level event loop watchdog.

    Monitors asyncio event loop progress by tracking time between iterations.
    If no progress is detected for ``_WATCHDOG_THRESHOLD_S`` seconds, the
    daemon is considered stuck and shutdown is forced.

    This guards against blocking calls (e.g., a buggy SDK call that never
    returns) that would freeze all sessions.
    """
    last_alive = asyncio.get_running_loop().time()

    while not shutdown_event.is_set():
        await asyncio.sleep(_WATCHDOG_CHECK_INTERVAL_S)
        now = asyncio.get_running_loop().time()
        elapsed = now - last_alive
        last_alive = now

        # If the event loop was blocked (sleep returned late), elapsed will be
        # much larger than _WATCHDOG_CHECK_INTERVAL_S.
        if elapsed > _WATCHDOG_THRESHOLD_S:
            logger.critical(
                "Daemon watchdog: event loop appears stuck (%.0fs since last check). "
                "Forcing shutdown.",
                elapsed,
            )
            shutdown_event.set()
            return

        # Rearm SIGALRM on each successful check — proves the event loop is alive
        if hasattr(signal, "SIGALRM"):
            signal.alarm(_SIGALRM_TIMEOUT_S)

    logger.debug("Daemon watchdog: stopping cleanly")


def _start_sigalrm_watchdog() -> None:
    """Install a SIGALRM handler as the last-resort OS-level watchdog.

    Sets a ``_SIGALRM_TIMEOUT_S``-second alarm.  If the process does not
    call ``_disarm_sigalrm_watchdog()`` or reset the alarm before then,
    the alarm handler fires and calls ``os._exit(2)`` to guarantee termination
    even if the event loop is completely frozen.

    No-op on Windows (SIGALRM is not available).
    """
    if not hasattr(signal, "SIGALRM"):
        return

    def _alarm_handler(signum: int, frame: object) -> None:  # noqa: ARG001
        logger.critical(
            "Daemon SIGALRM fired — process unresponsive for %ds. Forcing exit.",
            _SIGALRM_TIMEOUT_S,
        )
        os._exit(2)

    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(_SIGALRM_TIMEOUT_S)
    logger.debug("SIGALRM watchdog armed (%ds)", _SIGALRM_TIMEOUT_S)


def _disarm_sigalrm_watchdog() -> None:
    """Cancel the SIGALRM alarm set by ``_start_sigalrm_watchdog``."""
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)
        logger.debug("SIGALRM watchdog disarmed")


def _setup_daemon_logging(log_file: Path) -> logging.handlers.QueueListener:
    """Configure non-blocking file-based logging for the daemon process.

    Uses ``QueueHandler`` + ``QueueListener`` so log records are enqueued
    instantly (non-blocking) and written to disk in a background thread.
    This prevents synchronous file I/O from ever stalling the asyncio
    event loop — the root cause of the SIGALRM crash.

    Installs a ``SessionIdFilter`` on the ``QueueHandler`` so that every log
    record is tagged with ``session_id`` before enqueuing — including records
    from third-party loggers (Slack SDK, Claude Agent SDK) that propagate to
    the root logger without passing through root-level filters.  The filter
    must be on the QueueHandler (not the FileHandler) because it reads a
    contextvar only available in the calling thread/task.

    Returns the ``QueueListener`` — the caller must call ``.stop()`` on
    shutdown to flush remaining records.
    """
    from summon_claude.sessions.session import RedactingFormatter, SessionIdFilter  # noqa: PLC0415

    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()

    root.setLevel(logging.DEBUG)
    # session_id is "[abc] " when inside a session task, "" at daemon level.
    # Example daemon:  "12:00:00 INFO summon_claude.daemon: message text"
    # Example session: "12:00:00 INFO summon_claude.session: [abc123] message text"
    fmt = RedactingFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(session_id)s%(message)s",
            datefmt="%H:%M:%S",
        )
    )
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5_000_000,
        backupCount=2,
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)

    # Non-blocking: QueueHandler enqueues instantly; QueueListener writes
    # to the FileHandler in a background thread, off the event loop.
    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    qh = logging.handlers.QueueHandler(log_queue)
    qh.setLevel(logging.DEBUG)
    # SessionIdFilter must be on the QueueHandler (not the FileHandler)
    # because it reads a contextvar that is only available in the calling
    # thread/task — the QueueListener thread has its own context.
    qh.addFilter(SessionIdFilter())
    root.addHandler(qh)

    listener = logging.handlers.QueueListener(log_queue, fh, respect_handler_level=True)
    listener.start()

    # Suppress noisy third-party DEBUG logging — these generate high volumes
    # of messages (pings, API calls) that are rarely useful for debugging
    # summon-claude itself.
    for noisy_logger in ("slack_sdk", "slack_bolt", "aiohttp", "urllib3"):
        logging.getLogger(noisy_logger).setLevel(logging.INFO)

    return listener


def run_daemon(config: SummonConfig) -> None:
    """Acquire the file lock, write PID, configure logging, and run the event loop.

    Raises ``DaemonAlreadyRunningError`` if the lock is already held by another
    process.  This function never returns normally — it exits when the asyncio
    event loop completes.

    Uses ``fcntl.flock`` rather than a third-party file-lock library so the
    lock is automatically released by the kernel when the process exits,
    eliminating stale-lock races during fork.
    """
    pid_path = _daemon_pid()
    lock_path = _daemon_lock()
    log_path = _daemon_log()
    sock_path = _daemon_socket()

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise DaemonAlreadyRunningError(pid_path) from exc

    pid_path.write_text(str(os.getpid()))
    pid_path.chmod(0o600)

    log_listener = _setup_daemon_logging(log_path)
    logger.info("Daemon process started (pid=%d)", os.getpid())

    try:
        asyncio.run(daemon_main(config))
    finally:
        # Best-effort cleanup if asyncio.run() exits early (e.g., exception)
        log_listener.stop()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        pid_path.unlink(missing_ok=True)
        sock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Auto-start helpers (called from CLI)
# ---------------------------------------------------------------------------


def is_daemon_running() -> bool:
    """Return True if the daemon socket exists and accepts connections."""
    sock_path = _daemon_socket()
    if not sock_path.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(2.0)
        s.connect(str(sock_path))
        return True
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return False
    finally:
        s.close()


def _clear_stale_daemon_files() -> None:
    """Remove PID and socket files left by a dead daemon.

    The lock file is intentionally preserved — fcntl.flock() locks are
    advisory and tied to the file descriptor, not the file path, so there
    is no risk of a stale lock persisting across daemon restarts.
    """
    for path in (_daemon_pid(), _daemon_socket(), _startup_error_path()):
        path.unlink(missing_ok=True)


def start_daemon(config: SummonConfig) -> None:
    """Fork a daemon process and wait for it to start listening.

    Uses ``python-daemon.DaemonContext`` for a proper double-fork so the
    daemon is fully detached from the terminal.  The parent polls for the
    Unix socket file to appear (up to 10 seconds) to confirm the daemon is
    ready before returning.

    Raises ``RuntimeError`` on unsupported platforms (Windows) or if the
    daemon socket does not appear within the timeout.
    """
    import sys  # noqa: PLC0415

    if sys.platform == "win32":
        raise RuntimeError("Daemon mode is not supported on Windows")

    socket_path = _daemon_socket()
    # Validate socket path length before attempting anything
    _validate_socket_path(socket_path)

    if is_daemon_running():
        logger.debug("Daemon already running — skipping fork")
        return

    if os.getuid() == 0:
        logger.warning(
            "Running as root (uid 0) — /tmp socket dir is shared with all root processes"
        )
    # Remove stale artefacts from a previous (dead) daemon
    _clear_stale_daemon_files()

    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    pid = os.fork()
    if pid > 0:
        # Parent: wait for daemon socket to appear
        _wait_for_socket(socket_path)
        # Clear any stale startup error from a previous failed run
        _startup_error_path().unlink(missing_ok=True)
        return

    # Child: detach from terminal session and run daemon
    os.setsid()  # become session leader — detach from parent's terminal

    # Set up file logging early so exceptions during daemon startup are
    # captured in daemon.log instead of being silently swallowed.
    # run_daemon() sets up its own listener, so stop this one first.
    log_path = _daemon_log()
    early_listener = _setup_daemon_logging(log_path)

    try:
        import daemon  # noqa: PLC0415

        log_fh = log_path.open("a")

        ctx = daemon.DaemonContext(
            working_directory="/",
            umask=0o022,
            stdout=log_fh,
            stderr=log_fh,
            detach_process=False,  # already forked + setsid above — just close fds
        )
        with ctx:
            early_listener.stop()
            run_daemon(config)
    except Exception as exc:
        logger.critical("Daemon child failed: %s", exc, exc_info=True)
    finally:
        os._exit(0)


def _wait_for_socket(socket_path: Path) -> None:
    """Poll until *socket_path* exists (daemon is ready) or timeout expires.

    Raises ``RuntimeError`` if the socket does not appear within
    ``_SOCKET_WAIT_TIMEOUT_S`` seconds.  Includes startup error diagnostic
    from ``last-startup-error`` file if it exists.
    """
    deadline = time.monotonic() + _SOCKET_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(_SOCKET_POLL_INTERVAL_S)

    # Socket never appeared — check for startup error diagnostic
    error_path = _startup_error_path()
    if error_path.exists():
        try:
            error_contents = error_path.read_text().strip()
            error_path.unlink(missing_ok=True)
            raise RuntimeError(f"Daemon startup failed:\n{error_contents}")
        except OSError:
            pass

    raise RuntimeError(
        f"Daemon did not start within {_SOCKET_WAIT_TIMEOUT_S:.0f}s "
        f"(socket {socket_path} never appeared)"
    )


async def connect_to_daemon() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a Unix socket connection to the running daemon.

    Returns ``(reader, writer)`` for use with ``ipc.send_msg`` /
    ``ipc.recv_msg``.

    Raises ``ConnectionRefusedError`` / ``FileNotFoundError`` if the daemon
    socket does not exist or the connection is refused.
    """
    socket_path = _daemon_socket()
    reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=MAX_MESSAGE_SIZE)
    return reader, writer

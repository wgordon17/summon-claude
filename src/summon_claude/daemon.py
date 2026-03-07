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

from summon_claude.config import get_data_dir
from summon_claude.event_dispatcher import EventDispatcher
from summon_claude.sessions.manager import SessionManager
from summon_claude.slack.bolt import BoltRouter

if TYPE_CHECKING:
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


async def recv_msg(reader: asyncio.StreamReader) -> dict:  # type: ignore[type-arg]
    """Read a length-prefixed message from *reader* and return the decoded dict.

    Raises:
        ValueError: If the declared message length exceeds MAX_MESSAGE_SIZE.
        asyncio.IncompleteReadError: If the connection closes before a full
            message is received.
        TimeoutError: If the message is not fully received within ``_RECV_TIMEOUT``.
    """
    header: bytes = await asyncio.wait_for(reader.readexactly(4), timeout=_RECV_TIMEOUT)
    length: int = struct.unpack(">I", header)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} bytes (max {MAX_MESSAGE_SIZE})")
    payload: bytes = await asyncio.wait_for(reader.readexactly(length), timeout=_RECV_TIMEOUT)
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
    return _data_dir() / "daemon.sock"


def _validate_socket_path(sock: Path) -> None:
    """Raise ``SocketPathTooLongError`` if *sock* exceeds the Unix limit.

    Called at daemon startup rather than on every path resolution so tests
    using long ``tmp_path`` directories are not affected.
    """
    sock_str = str(sock)
    if len(sock_str) > _UNIX_SOCKET_PATH_MAX:
        raise SocketPathTooLongError(
            f"Daemon socket path is {len(sock_str)} chars, exceeding the "
            f"Unix limit of {_UNIX_SOCKET_PATH_MAX}.\n"
            f"  Path: {sock_str}\n"
            f"Set XDG_DATA_HOME to a shorter path (e.g. export "
            f"XDG_DATA_HOME=~/.local/share) or use the default ~/.summon directory."
        )


def _daemon_pid() -> Path:
    return _data_dir() / "daemon.pid"


def _daemon_lock() -> Path:
    return _data_dir() / "daemon.lock"


def _daemon_log() -> Path:
    return _data_dir() / "logs" / "daemon.log"


# Unix socket path length limit: 104 on macOS, 108 on Linux.
# We use the lower bound so the daemon works on both platforms.
_UNIX_SOCKET_PATH_MAX = 104

_SOCKET_WAIT_TIMEOUT_S = 10.0
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


async def daemon_main(config: SummonConfig) -> None:
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
    logger.info("BoltRouter started")

    if bolt_router.bot_user_id is None:  # pragma: no cover — start() always sets this
        raise RuntimeError("BoltRouter.start() did not set bot_user_id")
    session_manager = SessionManager(
        config=config,
        web_client=bolt_router.web_client,
        bot_user_id=bolt_router.bot_user_id,
        dispatcher=dispatcher,
    )
    dispatcher.set_command_handler(session_manager.handle_summon_command)

    # Start Unix socket control server
    control_server = await asyncio.start_unix_server(
        session_manager.handle_client,
        path=str(socket_path),
        limit=MAX_MESSAGE_SIZE,
    )
    # Restrict socket to owner-only (mode 600) so other users cannot connect
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
    health_task = bolt_router.start_health_monitor()

    # Layer 2: Daemon-level event loop watchdog
    watchdog_task = asyncio.create_task(
        _watchdog_loop(session_manager.shutdown_event), name="daemon-watchdog"
    )

    # Layer 3: SIGALRM OS watchdog (last resort — only on Unix)
    _start_sigalrm_watchdog()

    logger.info("Daemon started (pid=%d, socket=%s)", os.getpid(), socket_path)

    # Wait until shutdown is requested (by signal, grace timer, or error)
    await session_manager.shutdown_event.wait()

    logger.info("Daemon shutdown initiated")

    # Cancel watchdog tasks before draining sessions
    health_task.cancel()
    watchdog_task.cancel()
    for task in (health_task, watchdog_task):
        try:
            await task
        except (asyncio.CancelledError, Exception) as e:
            logger.debug("Watchdog task cleanup: %s", e)

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

    # Stop the background log listener last — flush remaining records
    _stop_daemon_logging()


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


_daemon_log_listener: logging.handlers.QueueListener | None = None
"""Module-level reference to the daemon log listener for shutdown cleanup."""


def _setup_daemon_logging(log_file: Path) -> None:
    """Configure non-blocking file-based logging for the daemon process.

    Uses ``QueueHandler`` + ``QueueListener`` so log records are enqueued
    instantly (non-blocking) and written to disk in a background thread.
    This prevents synchronous file I/O from ever stalling the asyncio
    event loop — the root cause of the SIGALRM crash.

    Idempotent — safe to call twice (e.g. once in the fork child for early
    diagnostics, then again inside ``run_daemon``).  Skips if a
    ``QueueHandler`` is already attached.

    Installs a ``SessionIdFilter`` on the ``QueueHandler`` so that every log
    record is tagged with ``session_id`` before enqueuing — including records
    from third-party loggers (Slack SDK, Claude Agent SDK) that propagate to
    the root logger without passing through root-level filters.  The filter
    must be on the QueueHandler (not the FileHandler) because it reads a
    contextvar only available in the calling thread/task.
    """
    global _daemon_log_listener  # noqa: PLW0603
    from summon_claude.sessions.session import SessionIdFilter  # noqa: PLC0415

    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()

    # Idempotency: skip if listener already running
    if _daemon_log_listener is not None:
        return

    root.setLevel(logging.DEBUG)
    # session_id is "[abc] " when inside a session task, "" at daemon level.
    # Example daemon:  "12:00:00 INFO summon_claude.daemon: message text"
    # Example session: "12:00:00 INFO summon_claude.session: [abc123] message text"
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(session_id)s%(message)s",
        datefmt="%H:%M:%S",
    )
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
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

    _daemon_log_listener = logging.handlers.QueueListener(log_queue, fh, respect_handler_level=True)
    _daemon_log_listener.start()

    # Suppress noisy third-party DEBUG logging — these generate high volumes
    # of messages (pings, API calls) that are rarely useful for debugging
    # summon-claude itself.
    for noisy_logger in ("slack_sdk", "slack_bolt", "aiohttp", "urllib3"):
        logging.getLogger(noisy_logger).setLevel(logging.INFO)


def _stop_daemon_logging() -> None:
    """Stop the background log listener, flushing remaining records."""
    global _daemon_log_listener  # noqa: PLW0603
    if _daemon_log_listener is not None:
        _daemon_log_listener.stop()
        _daemon_log_listener = None


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

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(lock_fd)
        raise DaemonAlreadyRunningError(pid_path) from exc

    pid_path.write_text(str(os.getpid()))
    pid_path.chmod(0o600)

    _setup_daemon_logging(log_path)
    logger.info("Daemon process started (pid=%d)", os.getpid())

    try:
        asyncio.run(daemon_main(config))
    finally:
        # Best-effort cleanup if asyncio.run() exits early (e.g., exception)
        _stop_daemon_logging()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        pid_path.unlink(missing_ok=True)
        _daemon_socket().unlink(missing_ok=True)


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
    for path in (_daemon_pid(), _daemon_socket()):
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

    # Validate socket path length before attempting anything
    _validate_socket_path(_daemon_socket())

    if is_daemon_running():
        logger.debug("Daemon already running — skipping fork")
        return

    socket_path = _daemon_socket()
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove stale artefacts from a previous (dead) daemon
    _clear_stale_daemon_files()

    pid = os.fork()
    if pid > 0:
        # Parent: wait for daemon socket to appear
        _wait_for_socket(socket_path)
        return

    # Child: detach from terminal session and run daemon
    os.setsid()  # become session leader — detach from parent's terminal

    # Set up file logging early so exceptions during daemon startup are
    # captured in daemon.log instead of being silently swallowed.
    log_path = _daemon_log()
    _setup_daemon_logging(log_path)

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
            run_daemon(config)
    except Exception as exc:
        logger.critical("Daemon child failed: %s", exc, exc_info=True)
    finally:
        os._exit(0)


def _wait_for_socket(socket_path: Path) -> None:
    """Poll until *socket_path* exists (daemon is ready) or timeout expires.

    Raises ``RuntimeError`` if the socket does not appear within
    ``_SOCKET_WAIT_TIMEOUT_S`` seconds.
    """
    deadline = time.monotonic() + _SOCKET_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(_SOCKET_POLL_INTERVAL_S)
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

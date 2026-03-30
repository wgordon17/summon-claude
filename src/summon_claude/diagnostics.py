"""Diagnostic checks for summon doctor command.

Provides the DiagnosticCheck protocol, CheckResult dataclass, Redactor class,
and all check implementations registered in DIAGNOSTIC_REGISTRY.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import os
import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from summon_claude.config import get_config_dir, get_data_dir
from summon_claude.slack.client import redact_secrets

if TYPE_CHECKING:
    from summon_claude.config import SummonConfig


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Result of a single diagnostic check."""

    status: Literal["pass", "fail", "warn", "info", "skip"]
    subsystem: str
    message: str
    details: list[str] = field(default_factory=list)
    suggestion: str | None = None
    collected_logs: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# DiagnosticCheck protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DiagnosticCheck(Protocol):
    """Protocol for a single diagnostic check."""

    name: str
    description: str

    async def run(self, config: SummonConfig | None) -> CheckResult: ...


# ---------------------------------------------------------------------------
# DIAGNOSTIC_REGISTRY and KNOWN_SUBSYSTEMS
# ---------------------------------------------------------------------------

KNOWN_SUBSYSTEMS: frozenset[str] = frozenset(
    {
        "environment",
        "daemon",
        "database",
        "slack",
        "logs",
        "mcp_workspace",
        "mcp_github",
    }
)

DIAGNOSTIC_REGISTRY: dict[str, DiagnosticCheck] = {}


# ---------------------------------------------------------------------------
# Redactor
# ---------------------------------------------------------------------------

_HOME_DIR = str(Path.home())
_DATA_DIR = str(get_data_dir())
_CONFIG_DIR = str(get_config_dir())
_SLACK_USER_ID_RE = re.compile(r"\bU[A-Z0-9]{8,11}\b")
_SLACK_CHANNEL_ID_RE = re.compile(r"\bC[A-Z0-9]{8,11}\b")
_SLACK_TEAM_ID_RE = re.compile(r"\bT[A-Z0-9]{8,11}\b")
_SLACK_BOT_ID_RE = re.compile(r"\bB[A-Z0-9]{8,11}\b")
_SESSION_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


class Redactor:
    """Applies aggressive sanitization to diagnostic output."""

    def redact(self, text: str) -> str:
        """Redact secrets, paths, user IDs, and session UUIDs from text."""
        # 1. Token secrets (xoxb-, xapp-, sk-ant-, ghp_, github_pat_, gho_, ghu_, ghs_, ghr_)
        text = redact_secrets(text)
        # 2. Path normalization — longer specific dirs first, home last
        if _DATA_DIR != _HOME_DIR:
            text = text.replace(_DATA_DIR, "[data_dir]")
        if _CONFIG_DIR != _HOME_DIR:
            text = text.replace(_CONFIG_DIR, "[config_dir]")
        text = text.replace(_HOME_DIR, "~")
        # 3. Slack IDs: user (U), channel (C), team (T), bot (B)
        text = _SLACK_USER_ID_RE.sub("U***", text)
        text = _SLACK_CHANNEL_ID_RE.sub("C***", text)
        text = _SLACK_TEAM_ID_RE.sub("T***", text)
        text = _SLACK_BOT_ID_RE.sub("B***", text)
        # 4. Session UUIDs: truncate to first 8 chars
        return _SESSION_UUID_RE.sub(lambda m: m.group(0)[:8] + "...", text)


# Module-level singleton — import as `from summon_claude.diagnostics import redactor`
redactor = Redactor()


# ---------------------------------------------------------------------------
# EnvironmentCheck
# ---------------------------------------------------------------------------


class EnvironmentCheck:
    name = "environment"
    description = "Python version, CLI tools, and summon-claude package version"

    async def run(self, config: SummonConfig | None) -> CheckResult:  # noqa: ARG002, PLR0912
        details: list[str] = []
        status: Literal["pass", "fail", "warn", "info", "skip"] = "pass"

        # Python version
        vi = sys.version_info
        py_version = f"{vi.major}.{vi.minor}.{vi.micro}"
        if vi < (3, 12):
            details.append(f"Python {py_version} — FAIL (3.12+ required)")
            status = "fail"
        else:
            details.append(f"Python {py_version}")

        # claude CLI
        claude_path = shutil.which("claude")
        if not claude_path:
            details.append("claude CLI — NOT FOUND (install from https://claude.ai/code)")
            status = "fail"
        else:
            claude_version = await _get_version("claude", "--version", max_wait=5)
            if claude_version is None:
                details.append(f"claude CLI — found at {claude_path} (version timed out)")
                if status == "pass":
                    status = "warn"
            else:
                details.append(f"claude CLI {claude_version}")

        # uv
        uv_path = shutil.which("uv")
        if uv_path:
            uv_version = await _get_version("uv", "--version", max_wait=5)
            details.append(f"uv {uv_version or 'found'}")
        else:
            details.append("uv — not found")
            if status == "pass":
                status = "warn"

        # gh CLI (info-level — optional)
        gh_path = shutil.which("gh")
        if gh_path:
            gh_version = await _get_version("gh", "--version", max_wait=5)
            details.append(f"gh {gh_version or 'found'} (optional)")
        else:
            details.append("gh — not found (optional, needed for --submit)")

        # sqlite3 CLI (info-level — optional)
        sqlite3_path = shutil.which("sqlite3")
        if sqlite3_path:
            details.append("sqlite3 CLI found (optional)")
        else:
            details.append("sqlite3 CLI — not found (optional)")

        # Platform
        plat = f"{platform.system()} {platform.release()}"
        details.append(f"Platform: {plat}")

        # summon-claude version
        try:
            pkg_version = importlib.metadata.version("summon-claude")
            details.append(f"summon-claude {pkg_version}")
        except importlib.metadata.PackageNotFoundError:
            details.append("summon-claude — package version not found")

        message = f"Python {py_version}, claude {'found' if claude_path else 'MISSING'}"
        return CheckResult(status=status, subsystem="environment", message=message, details=details)


async def _get_version(cmd: str, *args: str, max_wait: float = 5) -> str | None:
    """Run a command and return first line of stdout, or None on error/timeout."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=max_wait)
        line = stdout.decode(errors="replace").splitlines()[0].strip() if stdout else ""
        return line or None
    except TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.wait()  # reap zombie
        return None
    except (FileNotFoundError, OSError):
        return None


DIAGNOSTIC_REGISTRY["environment"] = EnvironmentCheck()


# ---------------------------------------------------------------------------
# DaemonCheck
# ---------------------------------------------------------------------------


class DaemonCheck:
    name = "daemon"
    description = "Daemon process liveness, socket connectivity, and orphaned sessions"

    async def run(self, config: SummonConfig | None) -> CheckResult:  # noqa: ARG002, PLR0912, PLR0915
        # _daemon_pid/_daemon_socket are private but no public alternative exists;
        # diagnostic check is a legitimate consumer of daemon internals.
        from summon_claude.daemon import (  # noqa: PLC0415
            _daemon_pid,
            _daemon_socket,
            is_daemon_running,
        )
        from summon_claude.sessions.registry import SessionRegistry  # noqa: PLC0415

        details: list[str] = []
        status: Literal["pass", "fail", "warn", "info", "skip"] = "pass"

        # Check daemon running via socket
        running = is_daemon_running()
        sock_path = _daemon_socket()
        pid_path = _daemon_pid()

        if running:
            details.append("Daemon socket: connected")
        # Check for stale socket file
        elif sock_path.exists():
            details.append("Daemon socket: stale file exists (daemon not responding)")
            status = "warn"
        else:
            details.append("Daemon socket: not running")

        # PID file liveness check
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                try:
                    os.kill(pid, 0)
                    details.append(f"PID file: process {pid} alive")
                except ProcessLookupError:
                    details.append(f"PID file: process {pid} is dead (stale PID file)")
                    if status == "pass":
                        status = "warn"
                except PermissionError:
                    details.append(f"PID file: process {pid} exists (no kill permission)")
            except (ValueError, OSError) as e:
                details.append(f"PID file: could not read ({e})")
                if status == "pass":
                    status = "warn"
        else:
            details.append("PID file: not found")

        # Orphaned session check
        active_count = 0
        try:
            async with SessionRegistry() as reg:
                active_sessions = await reg.list_active()
                active_count = len(active_sessions)
            if active_count > 0 and not running:
                details.append(
                    f"Orphaned sessions: {active_count} active"
                    " session(s) in DB but daemon not running"
                )
                if status == "pass":
                    status = "warn"
            elif active_count > 0:
                details.append(f"Active sessions: {active_count}")
            else:
                details.append("Active sessions: none")
        except Exception as e:
            details.append(f"Could not check active sessions: {e}")

        if status == "pass" and running:
            message = "Daemon running and healthy"
        elif status == "warn":
            message = "Daemon issues detected"
        elif not running:
            status = "info"
            message = "Daemon not running (start with `summon start`)"
        else:
            message = "Daemon status checked"

        return CheckResult(status=status, subsystem="daemon", message=message, details=details)


DIAGNOSTIC_REGISTRY["daemon"] = DaemonCheck()


# ---------------------------------------------------------------------------
# DatabaseCheck
# ---------------------------------------------------------------------------

_DB_TABLES = [
    "sessions",
    "audit_log",
    "spawn_tokens",
    "channels",
    "projects",
    "session_tasks",
    "workflow_defaults",
    "pending_auth_tokens",
    "schema_version",
]


class DatabaseCheck:
    name = "database"
    description = "Database file, schema version, integrity, and row counts"

    async def run(self, config: SummonConfig | None) -> CheckResult:  # noqa: ARG002
        from summon_claude.config import get_data_dir  # noqa: PLC0415
        from summon_claude.sessions.migrations import (  # noqa: PLC0415
            CURRENT_SCHEMA_VERSION,
            get_schema_version,
        )
        from summon_claude.sessions.registry import SessionRegistry  # noqa: PLC0415

        details: list[str] = []
        db_path = get_data_dir() / "registry.db"

        if not db_path.exists():
            return CheckResult(
                status="warn",
                subsystem="database",
                message="Database file not found (run `summon start` to create it)",
                details=[f"Expected path: {db_path}"],
            )

        # File size
        size_bytes = db_path.stat().st_size
        size_str = _human_size(size_bytes)
        details.append(f"Database: {db_path} ({size_str})")

        status: Literal["pass", "fail", "warn", "info", "skip"] = "pass"
        message = "Database OK"

        try:
            async with SessionRegistry() as reg:
                db = reg.db
                # Schema version
                version = await get_schema_version(db)
                if version < CURRENT_SCHEMA_VERSION:
                    details.append(
                        f"Schema version: {version} (behind — expected {CURRENT_SCHEMA_VERSION})"
                    )
                    status = "fail"
                    message = f"Schema v{version} is behind current v{CURRENT_SCHEMA_VERSION}"
                elif version > CURRENT_SCHEMA_VERSION:
                    details.append(
                        f"Schema version: {version}"
                        f" (ahead of code — expected {CURRENT_SCHEMA_VERSION})"
                    )
                    if status == "pass":
                        status = "warn"
                    message = f"Schema v{version} is ahead of code v{CURRENT_SCHEMA_VERSION}"
                else:
                    details.append(f"Schema version: {version} (current)")

                # Integrity check
                async with db.execute("PRAGMA integrity_check") as cursor:
                    row = await cursor.fetchone()
                    integrity = row[0] if row else "unknown"
                if integrity != "ok":
                    details.append(f"Integrity check: {integrity}")
                    status = "fail"
                    message = f"Database integrity failure: {integrity}"
                else:
                    details.append("Integrity check: ok")

                # Row counts
                for table in _DB_TABLES:
                    try:
                        # table values are compile-time constants from _DB_TABLES, not user input
                        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
                            row = await cur.fetchone()
                            count = row[0] if row else 0
                            details.append(f"  {table}: {count} rows")
                    except Exception as e:
                        details.append(f"  {table}: error ({e})")

        except Exception as e:
            return CheckResult(
                status="fail",
                subsystem="database",
                message=f"Failed to open database: {e}",
                details=details,
            )

        return CheckResult(
            status=status,
            subsystem="database",
            message=message,
            details=details,
        )


def _human_size(size_bytes: int) -> str:
    """Convert bytes to a human-readable string."""
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


DIAGNOSTIC_REGISTRY["database"] = DatabaseCheck()


# ---------------------------------------------------------------------------
# SlackCheck
# ---------------------------------------------------------------------------


class SlackCheck:
    name = "slack"
    description = "Slack bot token validity and workspace connectivity"

    async def run(self, config: SummonConfig | None) -> CheckResult:
        if config is None or not config.slack_bot_token:
            return CheckResult(
                status="skip",
                subsystem="slack",
                message="Slack tokens not configured",
                suggestion="Run `summon init` to set up.",
            )

        from slack_sdk import WebClient  # noqa: PLC0415
        from slack_sdk.errors import SlackApiError  # noqa: PLC0415

        try:
            client = WebClient(token=config.slack_bot_token)
            # Run sync call in executor to avoid blocking event loop
            loop = asyncio.get_running_loop()
            response = await asyncio.wait_for(
                loop.run_in_executor(None, client.auth_test),
                timeout=10,
            )
            if not response.get("ok"):
                return CheckResult(
                    status="fail",
                    subsystem="slack",
                    message="Slack auth.test returned ok=False",
                    suggestion=(
                        "Check your SUMMON_SLACK_BOT_TOKEN — run `summon config check` for details."
                    ),
                )
            # Don't include workspace/user name per security review SEC-003
            return CheckResult(
                status="pass",
                subsystem="slack",
                message="auth.test passed",
                details=["auth.test: connected successfully"],
            )
        except SlackApiError as e:
            return CheckResult(
                status="fail",
                subsystem="slack",
                message=f"Slack API error: {e.response.get('error', str(e))}",
                suggestion=(
                    "Check your SUMMON_SLACK_BOT_TOKEN — run `summon config check` for details."
                ),
            )
        except TimeoutError:
            return CheckResult(
                status="fail",
                subsystem="slack",
                message="Slack auth.test timed out (>10s)",
                suggestion="Check network connectivity to Slack.",
            )
        except Exception as e:
            return CheckResult(
                status="fail",
                subsystem="slack",
                message=f"Slack connectivity error: {e}",
                suggestion=(
                    "Check your SUMMON_SLACK_BOT_TOKEN — run `summon config check` for details."
                ),
            )


DIAGNOSTIC_REGISTRY["slack"] = SlackCheck()


# ---------------------------------------------------------------------------
# LogsCheck
# ---------------------------------------------------------------------------

_LOG_STALE_DAYS = 7
_LOG_MAX_LINES = 100


class LogsCheck:
    name = "logs"
    description = "Log directory, daemon and session log tails with redaction"

    async def run(self, config: SummonConfig | None) -> CheckResult:  # noqa: ARG002, PLR0912, PLR0915
        from summon_claude.config import get_data_dir  # noqa: PLC0415

        log_dir = get_data_dir() / "logs"
        details: list[str] = []
        collected_logs: dict[str, list[str]] = {}

        if not log_dir.exists():
            return CheckResult(
                status="info",
                subsystem="logs",
                message="Log directory not found (fresh install — normal)",
                details=["No logs directory found"],
            )

        import time  # noqa: PLC0415

        now = time.time()
        stale_threshold = now - (_LOG_STALE_DAYS * 86400)

        # Daemon log
        daemon_log = log_dir / "daemon.log"
        if daemon_log.exists():
            daemon_st = daemon_log.stat()
            age_hours = (now - daemon_st.st_mtime) / 3600
            size_str = _human_size(daemon_st.st_size)
            details.append(f"daemon.log: {size_str}, {age_hours:.1f}h old")
            lines = _tail_file(daemon_log, _LOG_MAX_LINES)
            collected_logs["daemon.log"] = [redactor.redact(ln) for ln in lines]
        else:
            details.append("daemon.log: not found")

        # Most recent session log (any .log except daemon.log)
        def _safe_mtime(f: Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0

        session_logs = sorted(
            [f for f in log_dir.glob("*.log") if f.name != "daemon.log" and f.exists()],
            key=_safe_mtime,
            reverse=True,
        )
        if session_logs:
            latest = session_logs[0]
            try:
                latest_st = latest.stat()
                age_hours = (now - latest_st.st_mtime) / 3600
                size_str = _human_size(latest_st.st_size)
                details.append(f"{latest.name}: {size_str}, {age_hours:.1f}h old")
            except OSError:
                details.append(f"{latest.name}: could not stat")
            lines = _tail_file(latest, _LOG_MAX_LINES)
            collected_logs[latest.name] = [redactor.redact(ln) for ln in lines]
        else:
            details.append("No session logs found")

        # Error/warning counts across all collected lines
        all_lines = [ln for lines in collected_logs.values() for ln in lines]
        error_count = sum(1 for ln in all_lines if "ERROR" in ln)
        warning_count = sum(1 for ln in all_lines if "WARNING" in ln)
        total_collected = len(all_lines)
        details.append(
            f"{total_collected} collected lines: {error_count} errors, {warning_count} warnings"
        )

        # Total disk usage
        total_size = 0
        for f in log_dir.glob("*.log*"):
            try:
                if f.is_file():
                    total_size += f.stat().st_size
            except OSError:
                pass
        details.append(f"Total log size: {_human_size(total_size)}")

        # Staleness check — reuse session_logs from above instead of re-globbing
        all_log_files = ([daemon_log] if daemon_log.exists() else []) + session_logs
        mtimes = []
        for f in all_log_files:
            with contextlib.suppress(OSError):
                mtimes.append(f.stat().st_mtime)
        if mtimes:
            most_recent_mtime = max(mtimes)
            if most_recent_mtime < stale_threshold:
                message = f"All logs are older than {_LOG_STALE_DAYS} days (stale)"
                status: Literal["pass", "fail", "warn", "info", "skip"] = "warn"
            else:
                parts = []
                if daemon_log.exists():
                    parts.append("daemon.log")
                parts.append(f"{len(session_logs)} session log(s)")
                message = " and ".join(parts) + " found"
                status = "info"
        else:
            message = "No log files found"
            status = "info"

        if error_count == 0 and warning_count == 0:
            suggestion = (
                "Logs look clean. For deeper diagnostics, restart with `summon start -v`"
                " and reproduce the issue, then run `summon doctor` again."
            )
        else:
            suggestion = None

        return CheckResult(
            status=status,
            subsystem="logs",
            message=message,
            details=details,
            suggestion=suggestion,
            collected_logs=collected_logs,
        )


def _tail_file(path: Path, n: int) -> list[str]:
    """Return the last n lines of a file."""
    try:
        text = path.read_text(errors="replace")
        lines = text.splitlines()
        return lines[-n:] if len(lines) > n else lines
    except OSError:
        return []


DIAGNOSTIC_REGISTRY["logs"] = LogsCheck()


# ---------------------------------------------------------------------------
# WorkspaceMcpCheck
# ---------------------------------------------------------------------------


class WorkspaceMcpCheck:
    name = "mcp_workspace"
    description = "workspace-mcp binary, Google credentials, and scope validity"

    async def run(self, config: SummonConfig | None) -> CheckResult:  # noqa: PLR0912
        if config is None or not config.scribe_enabled:
            return CheckResult(
                status="skip",
                subsystem="mcp_workspace",
                message="Scribe not enabled (skipping workspace MCP check)",
            )

        from summon_claude.config import (  # noqa: PLC0415
            find_workspace_mcp_bin,
            get_google_credentials_dir,
        )

        details: list[str] = []
        status: Literal["pass", "fail", "warn", "info", "skip"] = "pass"

        # Binary check
        bin_path = find_workspace_mcp_bin()
        if not bin_path.exists():
            return CheckResult(
                status="fail",
                subsystem="mcp_workspace",
                message="workspace-mcp binary not found",
                details=[f"Expected at: {bin_path}"],
                suggestion="Install workspace-mcp: pip install workspace-mcp",
            )
        details.append(f"workspace-mcp binary: {bin_path}")

        # Google credentials directory
        creds_dir = get_google_credentials_dir()
        if not creds_dir.exists():
            details.append("Google credentials directory: not found")
            if status == "pass":
                status = "warn"
        else:
            details.append(f"Google credentials directory: {creds_dir}")

        # client_secret.json
        client_secret = creds_dir / "client_secret.json"
        if not client_secret.exists():
            details.append("client_secret.json: not found")
            if status == "pass":
                status = "warn"
        else:
            details.append("client_secret.json: present")

        # Credential validity via auth.google_auth
        try:
            from auth.google_auth import (  # type: ignore[import-not-found]  # noqa: PLC0415
                has_required_scopes,
            )
            from auth.scopes import (  # type: ignore[import-not-found]  # noqa: PLC0415
                get_scopes_for_tools,
            )

            services = [s.strip() for s in config.scribe_google_services.split(",") if s.strip()]
            required = get_scopes_for_tools(services)
            valid = has_required_scopes(required)  # type: ignore[call-arg]
            if valid:
                details.append("Google credentials: valid scopes")
            else:
                details.append("Google credentials: missing required scopes")
                if status == "pass":
                    status = "warn"
        except ImportError:
            details.append("workspace-mcp package not importable (auth module not found)")
            if status == "pass":
                status = "warn"
        except Exception as e:
            details.append(f"Google credential check failed: {e}")
            if status == "pass":
                status = "warn"

        message = (
            "workspace-mcp configured" if status == "pass" else "workspace-mcp issues detected"
        )
        return CheckResult(
            status=status, subsystem="mcp_workspace", message=message, details=details
        )


DIAGNOSTIC_REGISTRY["mcp_workspace"] = WorkspaceMcpCheck()


# ---------------------------------------------------------------------------
# GitHubMcpCheck
# ---------------------------------------------------------------------------


class GitHubMcpCheck:
    name = "mcp_github"
    description = "GitHub token validity for remote MCP server"

    async def run(self, config: SummonConfig | None) -> CheckResult:  # noqa: ARG002
        from summon_claude.github_auth import (  # noqa: PLC0415
            GitHubAuthError,
            load_token,
            validate_token,
        )

        token = load_token()
        if token is None:
            return CheckResult(
                status="skip",
                subsystem="mcp_github",
                message="No GitHub token stored (run `summon auth github login`)",
            )

        try:
            result = await asyncio.wait_for(validate_token(token), timeout=10)
        except TimeoutError:
            return CheckResult(
                status="warn",
                subsystem="mcp_github",
                message="GitHub token validation timed out (network issue?)",
                suggestion="Check network connectivity to api.github.com.",
            )
        except GitHubAuthError as e:
            return CheckResult(
                status="warn",
                subsystem="mcp_github",
                message=f"GitHub API error: {e}",
                suggestion="Check network connectivity to api.github.com.",
            )
        except Exception as e:
            return CheckResult(
                status="warn",
                subsystem="mcp_github",
                message=f"GitHub token validation failed: {e}",
            )

        if result is None:
            return CheckResult(
                status="fail",
                subsystem="mcp_github",
                message="GitHub token is invalid or expired",
                suggestion="Token is invalid — run `summon auth github login` to re-authenticate.",
            )

        # Don't include login per SEC-003 (same as SlackCheck)
        return CheckResult(
            status="pass",
            subsystem="mcp_github",
            message="GitHub token valid",
            details=["auth: connected successfully"],
        )


DIAGNOSTIC_REGISTRY["mcp_github"] = GitHubMcpCheck()

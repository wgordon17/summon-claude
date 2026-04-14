"""Unit tests for summon_claude.diagnostics module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_test_config

from summon_claude.config import SummonConfig
from summon_claude.diagnostics import (
    DIAGNOSTIC_REGISTRY,
    CheckResult,
    DaemonCheck,
    DatabaseCheck,
    EnvironmentCheck,
    GitHubMcpCheck,
    LogsCheck,
    Redactor,
    SlackCheck,
    WorkspaceMcpCheck,
    redactor,
)

# ---------------------------------------------------------------------------
# CheckResult tests
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_construction_defaults(self) -> None:
        r = CheckResult(status="pass", subsystem="test", message="ok")
        assert r.status == "pass"
        assert r.subsystem == "test"
        assert r.message == "ok"
        assert r.details == []
        assert r.suggestion is None
        assert r.collected_logs == {}

    def test_construction_full(self) -> None:
        r = CheckResult(
            status="fail",
            subsystem="db",
            message="bad",
            details=["detail1"],
            suggestion="fix it",
            collected_logs={"log.txt": ["line1"]},
        )
        assert r.status == "fail"
        assert r.details == ["detail1"]
        assert r.suggestion == "fix it"
        assert r.collected_logs == {"log.txt": ["line1"]}

    def test_frozen(self) -> None:
        r = CheckResult(status="pass", subsystem="test", message="ok")
        with pytest.raises(AttributeError):
            r.status = "fail"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Redactor tests
# ---------------------------------------------------------------------------


class TestRedactor:
    def test_redact_secrets(self) -> None:
        text = "token is xoxb-abc-123-def"
        result = redactor.redact(text)
        assert "xoxb-" not in result
        assert "[REDACTED]" in result

    def test_redact_home_dir(self) -> None:
        home = str(Path.home())
        text = f"path is {home}/projects/test"
        result = redactor.redact(text)
        assert home not in result
        assert "~/projects/test" in result

    def test_redact_slack_user_id(self) -> None:
        text = "user U0123456789 sent message"
        result = redactor.redact(text)
        assert "U0123456789" not in result
        assert "U***" in result

    def test_redact_slack_channel_id(self) -> None:
        text = "channel C0123456789 created"
        result = redactor.redact(text)
        assert "C0123456789" not in result
        assert "C***" in result

    def test_redact_slack_team_id(self) -> None:
        text = "team T0123456789 connected"
        result = redactor.redact(text)
        assert "T0123456789" not in result
        assert "T***" in result

    def test_redact_slack_bot_id(self) -> None:
        text = "bot B0123456789 active"
        result = redactor.redact(text)
        assert "B0123456789" not in result
        assert "B***" in result

    def test_redact_uuid(self) -> None:
        text = "session 12345678-abcd-1234-abcd-1234567890ab running"
        result = redactor.redact(text)
        assert "12345678-abcd-1234-abcd-1234567890ab" not in result
        assert "12345678..." in result

    def test_redact_data_dir(self) -> None:
        from summon_claude.diagnostics import _data_dir_str

        data_dir = _data_dir_str()
        if str(Path.home()) != data_dir:
            text = f"opened {data_dir}/registry.db"
            result = redactor.redact(text)
            assert data_dir not in result
            assert "[data_dir]" in result

    def test_redact_no_sensitive_data(self) -> None:
        text = "all is well"
        assert redactor.redact(text) == "all is well"

    def test_redact_composition(self) -> None:
        home = str(Path.home())
        text = (
            f"xoxb-secret at {home}/proj "
            "for U0123456789 in C0123456789 "
            "session 12345678-abcd-1234-abcd-1234567890ab"
        )
        result = redactor.redact(text)
        assert "xoxb-" not in result
        assert home not in result
        assert "U0123456789" not in result
        assert "C0123456789" not in result
        assert "12345678-abcd-1234-abcd-1234567890ab" not in result

    def test_redact_github_tokens(self) -> None:
        for token in ("ghp_abc123", "github_pat_abc123", "gho_xyz"):
            result = redactor.redact(f"token: {token}")
            assert token not in result

    def test_redact_anthropic_key(self) -> None:
        text = "key is sk-ant-api01-abcdef"
        result = redactor.redact(text)
        assert "sk-ant-" not in result

    def test_redactor_is_instance(self) -> None:
        assert isinstance(redactor, Redactor)

    def test_lazy_data_dir_evaluation_picks_up_patched_value(self, monkeypatch) -> None:
        """Patching get_data_dir after import is picked up by the lazy _data_dir_str cache."""
        from pathlib import Path

        import summon_claude.diagnostics
        from summon_claude.diagnostics import _data_dir_str

        sentinel = Path("/tmp/test-sentinel-data")
        monkeypatch.setattr(summon_claude.diagnostics, "get_data_dir", lambda: sentinel)
        _data_dir_str.cache_clear()

        result = redactor.redact("/tmp/test-sentinel-data/registry.db")

        assert "[data_dir]" in result
        assert "/tmp/test-sentinel-data" not in result


# ---------------------------------------------------------------------------
# EnvironmentCheck tests
# ---------------------------------------------------------------------------


class TestEnvironmentCheck:
    @pytest.fixture()
    def check(self) -> EnvironmentCheck:
        return EnvironmentCheck()

    async def test_all_tools_present(self, check: EnvironmentCheck) -> None:
        with (
            patch("summon_claude.diagnostics.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "summon_claude.diagnostics._get_version",
                return_value="1.0.0",
            ),
        ):
            result = await check.run(None)
        assert result.status == "pass"
        assert result.subsystem == "environment"

    async def test_claude_missing(self, check: EnvironmentCheck) -> None:
        def which_side_effect(name: str) -> str | None:
            return None if name == "claude" else "/usr/bin/" + name

        with (
            patch(
                "summon_claude.diagnostics.shutil.which",
                side_effect=which_side_effect,
            ),
            patch(
                "summon_claude.diagnostics._get_version",
                return_value="1.0.0",
            ),
        ):
            result = await check.run(None)
        assert result.status == "fail"

    async def test_python_version_check(self, check: EnvironmentCheck) -> None:
        """Python >= 3.12 should pass (running tests requires 3.12+)."""
        with (
            patch("summon_claude.diagnostics.shutil.which", return_value="/usr/bin/tool"),
            patch(
                "summon_claude.diagnostics._get_version",
                return_value="1.0.0",
            ),
        ):
            result = await check.run(None)
        assert result.status == "pass"
        assert any("Python" in d for d in result.details)


# ---------------------------------------------------------------------------
# DaemonCheck tests
# ---------------------------------------------------------------------------


def _make_daemon_mocks(  # noqa: PLR0913
    *,
    running: bool = False,
    sock_exists: bool = False,
    pid_exists: bool = False,
    pid_text: str = "12345",
    pid_alive: bool = True,
    active_sessions: list | None = None,
) -> dict:
    """Build a dict of patches for DaemonCheck tests."""
    mock_reg = MagicMock()
    mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
    mock_reg.__aexit__ = AsyncMock(return_value=False)
    mock_reg.list_active = AsyncMock(return_value=active_sessions or [])

    sock_path = MagicMock(spec=Path)
    sock_path.exists.return_value = sock_exists

    pid_path = MagicMock(spec=Path)
    pid_path.exists.return_value = pid_exists
    pid_path.read_text.return_value = pid_text

    patches = {
        "running": patch("summon_claude.daemon.is_daemon_running", return_value=running),
        "socket": patch("summon_claude.daemon._daemon_socket", return_value=sock_path),
        "pid": patch("summon_claude.daemon._daemon_pid", return_value=pid_path),
        "registry": patch("summon_claude.sessions.registry.SessionRegistry", return_value=mock_reg),
    }

    if pid_exists and pid_alive:
        patches["kill"] = patch("summon_claude.diagnostics.os.kill", return_value=None)
    elif pid_exists and not pid_alive:
        patches["kill"] = patch("summon_claude.diagnostics.os.kill", side_effect=ProcessLookupError)

    return patches


class TestDaemonCheck:
    @pytest.fixture()
    def check(self) -> DaemonCheck:
        return DaemonCheck()

    async def test_daemon_running(self, check: DaemonCheck) -> None:
        mocks = _make_daemon_mocks(running=True, active_sessions=[])
        with mocks["running"], mocks["socket"], mocks["pid"], mocks["registry"]:
            result = await check.run(None)
        assert result.status == "pass"
        assert "running and healthy" in result.message

    async def test_daemon_not_running_clean(self, check: DaemonCheck) -> None:
        mocks = _make_daemon_mocks(running=False, sock_exists=False, pid_exists=False)
        with mocks["running"], mocks["socket"], mocks["pid"], mocks["registry"]:
            result = await check.run(None)
        assert result.status == "info"
        assert "not running" in result.message.lower()

    async def test_stale_socket(self, check: DaemonCheck) -> None:
        mocks = _make_daemon_mocks(running=False, sock_exists=True)
        with mocks["running"], mocks["socket"], mocks["pid"], mocks["registry"]:
            result = await check.run(None)
        assert result.status == "warn"
        assert any("stale" in d.lower() for d in result.details)

    async def test_stale_pid_dead_process(self, check: DaemonCheck) -> None:
        mocks = _make_daemon_mocks(
            running=False, pid_exists=True, pid_alive=False, pid_text="99999"
        )
        with (
            mocks["running"],
            mocks["socket"],
            mocks["pid"],
            mocks["registry"],
            mocks["kill"],
        ):
            result = await check.run(None)
        assert result.status == "warn"
        assert any("dead" in d.lower() for d in result.details)

    async def test_pid_alive(self, check: DaemonCheck) -> None:
        mocks = _make_daemon_mocks(running=True, pid_exists=True, pid_alive=True, pid_text="12345")
        with (
            mocks["running"],
            mocks["socket"],
            mocks["pid"],
            mocks["registry"],
            mocks["kill"],
        ):
            result = await check.run(None)
        assert result.status == "pass"
        assert any("alive" in d.lower() for d in result.details)

    async def test_orphaned_sessions(self, check: DaemonCheck) -> None:
        mocks = _make_daemon_mocks(running=False, active_sessions=[{"id": "a"}, {"id": "b"}])
        with mocks["running"], mocks["socket"], mocks["pid"], mocks["registry"]:
            result = await check.run(None)
        assert result.status == "warn"
        assert any("orphan" in d.lower() for d in result.details)

    async def test_unreadable_pid_file(self, check: DaemonCheck) -> None:
        # pid_alive=False: os.kill is never reached (ValueError on int() first),
        # so don't create an unused kill patch
        mocks = _make_daemon_mocks(
            running=False, pid_exists=True, pid_text="not-a-number", pid_alive=False
        )
        with mocks["running"], mocks["socket"], mocks["pid"], mocks["registry"]:
            result = await check.run(None)
        assert result.status == "warn"
        assert any("could not read" in d.lower() for d in result.details)

    async def test_pid_permission_error(self, check: DaemonCheck) -> None:
        mocks = _make_daemon_mocks(running=True, pid_exists=True, pid_text="12345")
        # Override the kill patch to raise PermissionError (process exists, no permission)
        mocks["kill"] = patch("summon_claude.diagnostics.os.kill", side_effect=PermissionError)
        with (
            mocks["running"],
            mocks["socket"],
            mocks["pid"],
            mocks["registry"],
            mocks["kill"],
        ):
            result = await check.run(None)
        assert result.status == "pass"
        assert any("no kill permission" in d.lower() for d in result.details)


# ---------------------------------------------------------------------------
# DatabaseCheck tests
# ---------------------------------------------------------------------------


class TestDatabaseCheck:
    @pytest.fixture()
    def check(self) -> DatabaseCheck:
        return DatabaseCheck()

    async def test_db_missing(self, check: DatabaseCheck, tmp_path: Path) -> None:
        with patch(
            "summon_claude.config.get_data_dir",
            return_value=tmp_path / "nonexistent",
        ):
            result = await check.run(None)
        assert result.status == "warn"
        assert "not found" in result.message.lower()

    async def test_db_schema_current(self, check: DatabaseCheck, tmp_path: Path) -> None:
        """Schema at current version + integrity ok → pass."""
        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION

        db_file = tmp_path / "registry.db"
        db_file.write_bytes(b"")  # create file so exists() passes

        mock_db = MagicMock()

        def _mock_execute(sql, *_args):
            ctx = MagicMock()
            if "PRAGMA integrity_check" in sql:
                ctx.fetchone = AsyncMock(return_value=("ok",))
            elif "SELECT COUNT" in sql:
                ctx.fetchone = AsyncMock(return_value=(42,))
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        mock_db.execute = _mock_execute

        mock_reg = MagicMock()
        mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_reg.__aexit__ = AsyncMock(return_value=False)
        mock_reg.db = mock_db

        with (
            patch("summon_claude.config.get_data_dir", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", return_value=mock_reg),
            patch(
                "summon_claude.sessions.migrations.get_schema_version",
                return_value=CURRENT_SCHEMA_VERSION,
            ),
        ):
            result = await check.run(None)
        assert result.status == "pass"
        assert "ok" in result.message.lower()

    async def test_db_schema_behind(self, check: DatabaseCheck, tmp_path: Path) -> None:
        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION

        db_file = tmp_path / "registry.db"
        db_file.write_bytes(b"")

        mock_db = MagicMock()

        def _mock_execute(sql, *_args):
            ctx = MagicMock()
            if "PRAGMA integrity_check" in sql:
                ctx.fetchone = AsyncMock(return_value=("ok",))
            elif "SELECT COUNT" in sql:
                ctx.fetchone = AsyncMock(return_value=(0,))
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        mock_db.execute = _mock_execute

        mock_reg = MagicMock()
        mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_reg.__aexit__ = AsyncMock(return_value=False)
        mock_reg.db = mock_db

        with (
            patch("summon_claude.config.get_data_dir", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", return_value=mock_reg),
            patch(
                "summon_claude.sessions.migrations.get_schema_version",
                return_value=CURRENT_SCHEMA_VERSION - 1,
            ),
        ):
            result = await check.run(None)
        assert result.status == "fail"
        assert "behind" in result.message.lower()

    async def test_db_schema_ahead(self, check: DatabaseCheck, tmp_path: Path) -> None:
        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION

        db_file = tmp_path / "registry.db"
        db_file.write_bytes(b"")

        mock_db = MagicMock()

        def _mock_execute(sql, *_args):
            ctx = MagicMock()
            if "PRAGMA integrity_check" in sql:
                ctx.fetchone = AsyncMock(return_value=("ok",))
            elif "SELECT COUNT" in sql:
                ctx.fetchone = AsyncMock(return_value=(0,))
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        mock_db.execute = _mock_execute

        mock_reg = MagicMock()
        mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_reg.__aexit__ = AsyncMock(return_value=False)
        mock_reg.db = mock_db

        with (
            patch("summon_claude.config.get_data_dir", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", return_value=mock_reg),
            patch(
                "summon_claude.sessions.migrations.get_schema_version",
                return_value=CURRENT_SCHEMA_VERSION + 1,
            ),
        ):
            result = await check.run(None)
        assert result.status == "warn"
        assert "ahead" in result.message.lower()

    async def test_db_open_failure(self, check: DatabaseCheck, tmp_path: Path) -> None:
        db_file = tmp_path / "registry.db"
        db_file.write_bytes(b"")

        mock_reg = MagicMock()
        mock_reg.__aenter__ = AsyncMock(side_effect=RuntimeError("corrupt"))
        mock_reg.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("summon_claude.config.get_data_dir", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", return_value=mock_reg),
        ):
            result = await check.run(None)
        assert result.status == "fail"
        assert "failed to open" in result.message.lower()

    async def test_db_integrity_failure(self, check: DatabaseCheck, tmp_path: Path) -> None:
        from summon_claude.sessions.migrations import CURRENT_SCHEMA_VERSION

        db_file = tmp_path / "registry.db"
        db_file.write_bytes(b"")

        mock_db = MagicMock()

        def _mock_execute(sql, *_args):
            ctx = MagicMock()
            if "PRAGMA integrity_check" in sql:
                ctx.fetchone = AsyncMock(return_value=("data corruption on page 42",))
            elif "SELECT COUNT" in sql:
                ctx.fetchone = AsyncMock(return_value=(0,))
            ctx.__aenter__ = AsyncMock(return_value=ctx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        mock_db.execute = _mock_execute

        mock_reg = MagicMock()
        mock_reg.__aenter__ = AsyncMock(return_value=mock_reg)
        mock_reg.__aexit__ = AsyncMock(return_value=False)
        mock_reg.db = mock_db

        with (
            patch("summon_claude.config.get_data_dir", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", return_value=mock_reg),
            patch(
                "summon_claude.sessions.migrations.get_schema_version",
                return_value=CURRENT_SCHEMA_VERSION,
            ),
        ):
            result = await check.run(None)
        assert result.status == "fail"
        assert "integrity" in result.message.lower()


# ---------------------------------------------------------------------------
# SlackCheck tests
# ---------------------------------------------------------------------------


class TestSlackCheck:
    @pytest.fixture()
    def check(self) -> SlackCheck:
        return SlackCheck()

    async def test_skip_no_config(self, check: SlackCheck) -> None:
        result = await check.run(None)
        assert result.status == "skip"

    async def test_skip_no_token(self, check: SlackCheck) -> None:
        config = make_test_config(slack_bot_token="")
        result = await check.run(config)
        assert result.status == "skip"

    async def test_auth_test_pass(self, check: SlackCheck) -> None:
        config = make_test_config()
        mock_client = MagicMock()
        mock_client.auth_test.return_value = {
            "ok": True,
            "team": "test-team",
            "user": "test-bot",
        }
        with patch(
            "slack_sdk.WebClient",
            return_value=mock_client,
        ):
            result = await check.run(config)
        assert result.status == "pass"
        # SEC-003: workspace name should NOT be in message
        assert "test-team" not in result.message

    async def test_auth_test_fail(self, check: SlackCheck) -> None:
        config = make_test_config()
        mock_client = MagicMock()
        mock_client.auth_test.return_value = {
            "ok": False,
            "error": "invalid_auth",
        }
        with patch(
            "slack_sdk.WebClient",
            return_value=mock_client,
        ):
            result = await check.run(config)
        assert result.status == "fail"

    async def test_slack_api_error(self, check: SlackCheck) -> None:
        from slack_sdk.errors import SlackApiError

        config = make_test_config()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.get.return_value = "invalid_auth"
        mock_client.auth_test.side_effect = SlackApiError(
            message="invalid_auth", response=mock_response
        )
        with patch("slack_sdk.WebClient", return_value=mock_client):
            result = await check.run(config)
        assert result.status == "fail"
        assert "slack api error" in result.message.lower()

    async def test_slack_timeout(self, check: SlackCheck) -> None:
        config = make_test_config()
        mock_client = MagicMock()
        mock_client.auth_test.side_effect = TimeoutError
        with patch("slack_sdk.WebClient", return_value=mock_client):
            result = await check.run(config)
        assert result.status == "fail"
        assert "timed out" in result.message.lower()

    async def test_slack_generic_exception(self, check: SlackCheck) -> None:
        config = make_test_config()
        mock_client = MagicMock()
        mock_client.auth_test.side_effect = ConnectionError("network down")
        with patch("slack_sdk.WebClient", return_value=mock_client):
            result = await check.run(config)
        assert result.status == "fail"
        assert "connectivity error" in result.message.lower()


# ---------------------------------------------------------------------------
# LogsCheck tests
# ---------------------------------------------------------------------------


class TestLogsCheck:
    @pytest.fixture()
    def check(self) -> LogsCheck:
        return LogsCheck()

    async def test_no_log_directory(self, check: LogsCheck, tmp_path: Path) -> None:
        with patch(
            "summon_claude.config.get_data_dir",
            return_value=tmp_path / "nonexistent",
        ):
            result = await check.run(None)
        assert result.status == "info"
        assert "fresh install" in result.message.lower()

    async def test_logs_with_content(self, check: LogsCheck, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        daemon_log = log_dir / "daemon.log"
        daemon_log.write_text(
            "2025-01-01 ERROR something failed\n"
            "2025-01-01 WARNING something warned\n"
            "2025-01-01 INFO all good\n"
        )
        with patch(
            "summon_claude.config.get_data_dir",
            return_value=tmp_path,
        ):
            result = await check.run(None)
        assert result.status == "info"
        assert "daemon.log" in result.collected_logs
        assert any("1 errors" in d for d in result.details)

    async def test_redaction_applied(self, check: LogsCheck, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        daemon_log = log_dir / "daemon.log"
        home = str(Path.home())
        daemon_log.write_text(f"path is {home}/secret/project\ntoken is xoxb-abc-123-def\n")
        with patch(
            "summon_claude.config.get_data_dir",
            return_value=tmp_path,
        ):
            result = await check.run(None)
        logs = result.collected_logs.get("daemon.log", [])
        for line in logs:
            assert home not in line
            assert "xoxb-" not in line


# ---------------------------------------------------------------------------
# WorkspaceMcpCheck tests
# ---------------------------------------------------------------------------


class TestWorkspaceMcpCheck:
    @pytest.fixture()
    def check(self) -> WorkspaceMcpCheck:
        return WorkspaceMcpCheck()

    async def test_skip_no_config(self, check: WorkspaceMcpCheck) -> None:
        result = await check.run(None)
        assert result.status == "skip"

    async def test_skip_scribe_disabled(self, check: WorkspaceMcpCheck) -> None:
        config = make_test_config(scribe_enabled=False, scribe_google_enabled=False)
        result = await check.run(config)
        assert result.status == "skip"

    async def test_binary_not_found(self, check: WorkspaceMcpCheck) -> None:
        config = make_test_config(scribe_enabled=True, scribe_google_enabled=True)
        mock_bin = MagicMock(spec=Path)
        mock_bin.exists.return_value = False
        with (
            patch("summon_claude.config.find_workspace_mcp_bin", return_value=mock_bin),
            patch(
                "summon_claude.config.get_google_credentials_dir",
                return_value=Path("/tmp/creds"),
            ),
        ):
            result = await check.run(config)
        assert result.status == "fail"
        assert "not found" in result.message.lower()

    async def test_binary_found_no_creds(self, check: WorkspaceMcpCheck, tmp_path: Path) -> None:
        config = make_test_config(scribe_enabled=True, scribe_google_enabled=True)
        mock_bin = MagicMock(spec=Path)
        mock_bin.exists.return_value = True
        creds_dir = tmp_path / "nonexistent_creds"
        with (
            patch("summon_claude.config.find_workspace_mcp_bin", return_value=mock_bin),
            patch("summon_claude.config.get_google_credentials_dir", return_value=creds_dir),
        ):
            result = await check.run(config)
        assert result.status == "warn"

    async def test_binary_found_service_detection_fails(
        self, check: WorkspaceMcpCheck, tmp_path: Path
    ) -> None:
        config = make_test_config(scribe_enabled=True, scribe_google_enabled=True)
        mock_bin = MagicMock(spec=Path)
        mock_bin.exists.return_value = True
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        # Create a valid account subdirectory
        account_dir = creds_dir / "default"
        account_dir.mkdir()
        (account_dir / "client_env").write_text("CLIENT_ID=x\nCLIENT_SECRET=y")
        (account_dir / "user@test.com.json").write_text("{}")

        with (
            patch("summon_claude.config.find_workspace_mcp_bin", return_value=mock_bin),
            patch("summon_claude.config.get_google_credentials_dir", return_value=creds_dir),
            patch("summon_claude.config.detect_account_services", return_value=None),
        ):
            result = await check.run(config)
        assert result.status == "warn"
        assert any("no services detected" in d.lower() for d in result.details)

    async def test_per_account_exception_warns(
        self, check: WorkspaceMcpCheck, tmp_path: Path
    ) -> None:
        config = make_test_config(scribe_enabled=True, scribe_google_enabled=True)
        mock_bin = MagicMock(spec=Path)
        mock_bin.exists.return_value = True
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        account_dir = creds_dir / "default"
        account_dir.mkdir()
        (account_dir / "client_env").write_text("CLIENT_ID=x\nCLIENT_SECRET=y")
        (account_dir / "user@test.com.json").write_text("{}")

        with (
            patch("summon_claude.config.find_workspace_mcp_bin", return_value=mock_bin),
            patch("summon_claude.config.get_google_credentials_dir", return_value=creds_dir),
            patch(
                "summon_claude.config.detect_account_services",
                side_effect=RuntimeError("store corrupt"),
            ),
        ):
            result = await check.run(config)
        assert result.status == "warn"
        assert any("check failed" in d for d in result.details)

    async def test_pass_with_accounts_and_services(
        self, check: WorkspaceMcpCheck, tmp_path: Path
    ) -> None:
        config = make_test_config(scribe_enabled=True, scribe_google_enabled=True)
        mock_bin = MagicMock(spec=Path)
        mock_bin.exists.return_value = True
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        # Create two valid account subdirectories
        for label in ("personal", "work"):
            account_dir = creds_dir / label
            account_dir.mkdir()
            (account_dir / "client_env").write_text("CLIENT_ID=x\nCLIENT_SECRET=y")
            (account_dir / "user@test.com.json").write_text("{}")

        with (
            patch("summon_claude.config.find_workspace_mcp_bin", return_value=mock_bin),
            patch("summon_claude.config.get_google_credentials_dir", return_value=creds_dir),
            patch("summon_claude.config.detect_account_services", return_value="gmail,calendar"),
        ):
            result = await check.run(config)
        assert result.status == "pass"
        assert "2 account(s)" in result.message
        assert "personal" in result.message
        assert "work" in result.message


# ---------------------------------------------------------------------------
# GitHubMcpCheck tests
# ---------------------------------------------------------------------------


class TestGitHubMcpCheck:
    @pytest.fixture()
    def check(self) -> GitHubMcpCheck:
        return GitHubMcpCheck()

    async def test_skip_no_token(self, check: GitHubMcpCheck) -> None:
        """Skip when no GitHub token is stored."""
        with patch("summon_claude.github_auth.load_token", return_value=None):
            result = await check.run(None)
        assert result.status == "skip"
        assert "summon auth github login" in result.message

    async def test_token_valid(self, check: GitHubMcpCheck) -> None:
        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test123"),
            patch(
                "summon_claude.github_auth.validate_token",
                new_callable=AsyncMock,
                return_value={"login": "testuser", "scopes": "repo"},
            ),
        ):
            result = await check.run(None)
        assert result.status == "pass"
        # SEC-003: username should NOT leak into message or details
        assert "testuser" not in result.message
        assert all("testuser" not in d for d in result.details)

    async def test_token_invalid(self, check: GitHubMcpCheck) -> None:
        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_invalid"),
            patch(
                "summon_claude.github_auth.validate_token",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await check.run(None)
        assert result.status == "fail"
        assert "invalid or expired" in result.message.lower()

    async def test_token_api_error(self, check: GitHubMcpCheck) -> None:
        from summon_claude.github_auth import GitHubAuthError

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test123"),
            patch(
                "summon_claude.github_auth.validate_token",
                new_callable=AsyncMock,
                side_effect=GitHubAuthError("Token validation failed: HTTP 500"),
            ),
        ):
            result = await check.run(None)
        assert result.status == "warn"
        assert "500" in result.message

    async def test_token_timeout(self, check: GitHubMcpCheck) -> None:
        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test123"),
            patch(
                "summon_claude.github_auth.validate_token",
                new_callable=AsyncMock,
                side_effect=TimeoutError,
            ),
        ):
            result = await check.run(None)
        assert result.status == "warn"
        assert "timed out" in result.message.lower()

    async def test_token_generic_exception(self, check: GitHubMcpCheck) -> None:
        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test123"),
            patch(
                "summon_claude.github_auth.validate_token",
                new_callable=AsyncMock,
                side_effect=ConnectionError("dns fail"),
            ),
        ):
            result = await check.run(None)
        assert result.status == "warn"
        assert "validation failed" in result.message.lower()


# ---------------------------------------------------------------------------
# CLI doctor tests
# ---------------------------------------------------------------------------


class TestDoctorCli:
    def test_format_status_pass(self) -> None:
        from summon_claude.cli.doctor import _format_status

        result = _format_status("pass", color=False)
        assert result == "[PASS]"

    def test_format_status_fail(self) -> None:
        from summon_claude.cli.doctor import _format_status

        result = _format_status("fail", color=False)
        assert result == "[FAIL]"

    def test_format_status_all_statuses(self) -> None:
        from summon_claude.cli.doctor import _format_status

        for status in ("pass", "fail", "warn", "info", "skip"):
            no_color = _format_status(status, color=False)
            assert no_color == f"[{status.upper()}]"

        with_color = _format_status("skip", color=True)
        assert "SKIP" in with_color

        unknown = _format_status("unknown_status", color=False)
        assert unknown == "[UNKNOWN_STATUS]"

    def test_redact_result(self) -> None:
        from summon_claude.cli.doctor import _redact_result

        home = str(Path.home())
        r = CheckResult(
            status="pass",
            subsystem="test",
            message=f"path is {home}/secret",
            details=[f"detail with {home}"],
            collected_logs={"log": [f"line with {home}"]},
        )
        redacted = _redact_result(r)
        assert home not in redacted.message
        assert home not in redacted.details[0]
        assert home not in redacted.collected_logs["log"][0]

    def test_redact_result_keys(self) -> None:
        """Log filename keys with UUIDs should be redacted."""
        from summon_claude.cli.doctor import _redact_result

        uuid_name = "12345678-abcd-1234-abcd-1234567890ab.log"
        r = CheckResult(
            status="info",
            subsystem="logs",
            message="ok",
            collected_logs={uuid_name: ["line1"]},
        )
        redacted = _redact_result(r)
        # Full UUID should not survive in keys
        assert uuid_name not in redacted.collected_logs
        # Truncated UUID should be present
        assert any("12345678..." in k for k in redacted.collected_logs)

    def test_write_export(self, tmp_path: Path) -> None:
        """--export should write valid JSON with redacted results."""
        from summon_claude.cli.doctor import _write_export

        home = str(Path.home())
        results = [
            CheckResult(
                status="pass",
                subsystem="test",
                message=f"path is {home}/secret",
            ),
            CheckResult(
                status="fail",
                subsystem="bad",
                message="broken",
                suggestion="fix it",
            ),
        ]
        export_path = str(tmp_path / "report.json")
        _write_export(export_path, results)

        import json

        data = json.loads(Path(export_path).read_text())
        assert data["version"] == "1.0"
        assert len(data["checks"]) == 2
        assert data["checks"][0]["status"] == "pass"
        assert data["checks"][1]["suggestion"] == "fix it"
        # Verify redaction is applied
        assert home not in data["checks"][0]["message"]

    def test_build_submit_body(self) -> None:
        """--submit body should contain check results and escape @."""
        from summon_claude.cli.doctor import _build_submit_body

        results = [
            CheckResult(
                status="pass",
                subsystem="test",
                message="ok @user",
            ),
        ]
        body = _build_submit_body(results)
        assert "\\@user" in body
        assert "@user" not in body.replace("\\@", "")
        assert "## summon doctor report" in body

    def test_build_submit_body_logs_in_code_blocks(self) -> None:
        """Log content in submit body should be inside fenced code blocks."""
        from summon_claude.cli.doctor import _build_submit_body

        results = [
            CheckResult(
                status="info",
                subsystem="logs",
                message="logs found",
                collected_logs={"daemon.log": ["ERROR something"]},
            ),
        ]
        body = _build_submit_body(results)
        assert "```" in body

    def test_from_file_sanitizes_pydantic_missing(self, monkeypatch) -> None:
        """from_file should raise ValueError with clean message, not Pydantic's input_value dump."""
        monkeypatch.delenv("SUMMON_SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("SUMMON_SLACK_APP_TOKEN", raising=False)
        monkeypatch.delenv("SUMMON_SLACK_SIGNING_SECRET", raising=False)
        with pytest.raises(ValueError, match=r"required field.*missing") as exc_info:
            SummonConfig.from_file("/dev/null")
        msg = str(exc_info.value)
        assert "input_value" not in msg
        assert "slack_bot_token" in msg

    async def test_run_checks_crash_recovery(self) -> None:
        """A crashing check should produce a synthetic fail result."""
        from summon_claude.cli.doctor import _run_checks

        class CrashingCheck:
            name = "crasher"
            description = "always crashes"

            async def run(self, config):
                raise RuntimeError("kaboom")

        with patch.dict(DIAGNOSTIC_REGISTRY, {"crasher": CrashingCheck()}, clear=True):  # type: ignore[dict-item]
            results: list[CheckResult] = []
            await _run_checks(results, None)

            assert len(results) == 1
            assert results[0].status == "fail"
            assert "crashed" in results[0].message.lower()
            assert results[0].suggestion is not None
            assert "bug" in results[0].suggestion.lower()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_human_size_bytes(self) -> None:
        from summon_claude.diagnostics import _human_size

        assert _human_size(0) == "0.0 B"
        assert _human_size(512) == "512.0 B"

    def test_human_size_kb(self) -> None:
        from summon_claude.diagnostics import _human_size

        assert _human_size(1536) == "1.5 KB"

    def test_human_size_mb(self) -> None:
        from summon_claude.diagnostics import _human_size

        result = _human_size(2 * 1024 * 1024)
        assert result == "2.0 MB"

    def test_tail_file(self, tmp_path: Path) -> None:
        from summon_claude.diagnostics import _tail_file

        f = tmp_path / "test.log"
        f.write_text("\n".join(f"line{i}" for i in range(200)))
        lines = _tail_file(f, 50)
        assert len(lines) == 50
        assert lines[-1] == "line199"

    def test_tail_file_short(self, tmp_path: Path) -> None:
        from summon_claude.diagnostics import _tail_file

        f = tmp_path / "test.log"
        f.write_text("line1\nline2\nline3")
        lines = _tail_file(f, 100)
        assert len(lines) == 3

    def test_tail_file_missing(self) -> None:
        from summon_claude.diagnostics import _tail_file

        lines = _tail_file(Path("/nonexistent/file.log"), 10)
        assert lines == []

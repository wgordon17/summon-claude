"""Tests for summon_claude.cli."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from summon_claude.cli import (
    _print_auth_banner,
    cli,
)
from summon_claude.cli.formatting import format_ts, print_session_detail, print_session_table
from tests.conftest import ACTIVE_SESSION as _ACTIVE_SESSION
from tests.conftest import COMPLETED_SESSION as _COMPLETED_SESSION
from tests.conftest import mock_registry as _mock_registry


class TestCLICommands:
    """Test that CLI commands are properly configured."""

    def test_cli_start_command_exists(self):
        """Test that start command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start a new summon session" in result.output

    def test_session_group_exists(self):
        """Test that session group is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "--help"])
        assert result.exit_code == 0
        assert "Manage summon sessions" in result.output

    def test_session_list_command_exists(self):
        """Test that session list command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "list", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output

    def test_session_info_command_exists(self):
        """Test that session info command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "info", "--help"])
        assert result.exit_code == 0

    def test_top_level_stop_command_exists(self):
        """Test that top-level stop command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0

    def test_session_logs_command_exists(self):
        """Test that session logs command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "logs", "--help"])
        assert result.exit_code == 0

    def test_session_cleanup_command_exists(self):
        """Test that session cleanup command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "cleanup", "--help"])
        assert result.exit_code == 0

    def test_session_alias_s(self):
        """Test that 's' alias works for 'session'."""
        runner = CliRunner()
        result = runner.invoke(cli, ["s", "--help"])
        assert result.exit_code == 0
        assert "Manage summon sessions" in result.output

    def test_session_alias_s_list(self):
        """Test that 's list' works like 'session list'."""
        runner = CliRunner()
        result = runner.invoke(cli, ["s", "list", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output

    def test_session_no_subcommand_shows_usage(self):
        """Test that 'session' with no subcommand shows usage and subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session"])
        # Click groups without invoke_without_command exit with code 2
        assert "list" in result.output
        assert "info" in result.output
        # 'stop' is now a top-level command, not a session subcommand
        assert "stop" not in result.output or "session" in result.output

    def test_start_accepts_cwd_option(self):
        """Test that start command accepts --cwd option."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert "--cwd" in result.output

    def test_start_accepts_name_option(self):
        """Test that start command accepts --name option."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert "--name" in result.output

    def test_start_accepts_model_option(self):
        """Test that start command accepts --model option."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert "--model" in result.output

    def test_start_accepts_resume_option(self):
        """Test that start command accepts --resume option."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert "--resume" in result.output

    def test_start_accepts_effort_option(self):
        """Test that start command accepts --effort option."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert "--effort" in result.output
        assert "low" in result.output
        assert "high" in result.output
        assert "max" in result.output

    def test_session_list_accepts_name_filter(self):
        """Test that session list accepts --name option."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "list", "--help"])
        assert "--name" in result.output

    def test_session_info_accepts_session_arg(self):
        """Test session info help says 'name or ID'."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "info", "--help"])
        assert "name or ID" in result.output

    def test_session_logs_accepts_session_arg(self):
        """Test session logs help says 'name or ID'."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "logs", "--help"])
        assert "name or ID" in result.output

    def test_stop_accepts_session_arg(self):
        """Test stop help says 'name or ID'."""
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--help"])
        assert "name or ID" in result.output

    def test_start_does_not_accept_background_flag(self):
        """Test that --background flag has been removed from start."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert "--background" not in result.output
        assert "-b" not in result.output

    def test_verbose_flag_supported(self):
        """Test that -v/--verbose flag is supported."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert "-v" in result.output or "--verbose" in result.output

    def test_short_help_flag(self):
        """Test that -h works as a shorthand for --help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["-h"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_old_toplevel_commands_removed(self):
        """Test that old top-level status/sessions/logs/cleanup are gone."""
        runner = CliRunner()
        for cmd in ["status", "sessions", "logs", "cleanup"]:
            result = runner.invoke(cli, [cmd])
            assert result.exit_code != 0, f"'{cmd}' should not be a top-level command"

    def test_top_level_stop_command_available(self):
        """Test that 'stop' is now a valid top-level command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0
        assert "session" in result.output.lower() or "stop" in result.output.lower()


class TestPrintAuthBanner:
    def test_auth_banner_contains_code(self, capsys):
        """_print_auth_banner should output the code."""
        _print_auth_banner("ABCDEF")
        captured = capsys.readouterr()
        assert "ABCDEF" in captured.out

    def test_auth_banner_contains_summon_command(self, capsys):
        """_print_auth_banner should show the /summon command."""
        _print_auth_banner("XYZ123")
        captured = capsys.readouterr()
        assert "/summon XYZ123" in captured.out

    def test_auth_banner_mentions_expiry(self, capsys):
        """_print_auth_banner should mention the 5-minute expiry."""
        _print_auth_banner("TTTTTT")
        captured = capsys.readouterr()
        assert "5 minutes" in captured.out or "Expires" in captured.out


class TestPrintSessionTable:
    def test_empty_list(self, capsys):
        sessions = []
        print_session_table(sessions)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_prints_session_data(self, capsys):
        sessions = [
            {
                "session_id": "sess-1",
                "status": "active",
                "session_name": "my-session",
                "slack_channel_name": "summon-my-session-0222",
                "cwd": "/tmp",
            }
        ]
        print_session_table(sessions)
        captured = capsys.readouterr()
        assert "STATUS" in captured.out
        assert "active" in captured.out
        assert "my-session" in captured.out
        assert "/tmp" in captured.out

    def test_handles_none_values(self, capsys):
        sessions = [
            {
                "session_id": "sess-x",
                "status": "pending_auth",
                "session_name": None,
                "slack_channel_name": None,
                "cwd": "/home",
            }
        ]
        print_session_table(sessions)
        captured = capsys.readouterr()
        assert "pending_auth" in captured.out

    def test_show_id_includes_full_session_id(self, capsys):
        sessions = [
            {
                "session_id": "aaaa1111-2222-3333-4444-555566667777",
                "status": "active",
                "session_name": "test",
                "slack_channel_name": "ch",
                "cwd": "/tmp",
            }
        ]
        print_session_table(sessions, show_id=True)
        captured = capsys.readouterr()
        assert "SESSION ID" in captured.out
        assert "aaaa1111-2222-3333-4444-555566667777" in captured.out

    def test_show_id_false_hides_session_id_column(self, capsys):
        sessions = [
            {
                "session_id": "aaaa1111-2222-3333-4444-555566667777",
                "status": "active",
                "session_name": "test",
                "slack_channel_name": "ch",
                "cwd": "/tmp",
            }
        ]
        print_session_table(sessions, show_id=False)
        captured = capsys.readouterr()
        assert "SESSION ID" not in captured.out


class TestPrintSessionDetail:
    def test_prints_session_fields(self, capsys):
        session = {
            "session_id": "sess-123",
            "status": "active",
            "pid": 12345,
            "cwd": "/tmp",
            "model": "claude-opus-4-6",
            "slack_channel_id": "C123",
            "slack_channel_name": "summon-test",
            "started_at": "2025-02-22T10:00:00+00:00",
            "total_turns": 3,
            "total_cost_usd": 0.05,
        }
        print_session_detail(session)
        captured = capsys.readouterr()
        assert "sess-123" in captured.out
        assert "active" in captured.out

    def test_includes_error_message_if_present(self, capsys):
        session = {
            "session_id": "sess-err",
            "status": "errored",
            "pid": 999,
            "cwd": "/tmp",
            "error_message": "Connection failed",
        }
        print_session_detail(session)
        captured = capsys.readouterr()
        assert "Connection failed" in captured.out


class TestFormatTs:
    def test_valid_iso_timestamp(self):
        result = format_ts("2025-02-22T10:30:45+00:00")
        assert isinstance(result, str)
        assert "2025" in result or "10:30" in result

    def test_none_returns_dash(self):
        result = format_ts(None)
        assert result == "-"

    def test_empty_string_returns_dash(self):
        result = format_ts("")
        assert result == "-"

    def test_invalid_format_returns_as_is(self):
        result = format_ts("not-a-timestamp")
        assert result == "not-a-timestamp"


# ---------------------------------------------------------------------------
# Behavior tests — session list / info / stop / logs / cleanup
# ---------------------------------------------------------------------------


class TestSessionList:
    """Behavior tests for 'session list'."""

    def test_list_shows_active_sessions(self):
        mock_ctx = _mock_registry(active=[_ACTIVE_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list"])
        assert result.exit_code == 0
        assert "active" in result.output
        assert "my-proj" in result.output

    def test_list_no_active_sessions(self):
        mock_ctx = _mock_registry(active=[])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list"])
        assert result.exit_code == 0
        assert "No active sessions." in result.output

    def test_list_all_shows_completed(self):
        mock_ctx = _mock_registry(all=[_ACTIVE_SESSION, _COMPLETED_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list", "--all"])
        assert result.exit_code == 0
        assert "active" in result.output
        assert "completed" in result.output
        assert "old-proj" in result.output

    def test_list_all_empty(self):
        mock_ctx = _mock_registry(all=[])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list", "--all"])
        assert result.exit_code == 0
        assert "No sessions found." in result.output

    def test_list_all_via_short_flag(self):
        mock_ctx = _mock_registry(all=[_ACTIVE_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list", "-a"])
        assert result.exit_code == 0
        assert "my-proj" in result.output

    def test_list_via_s_alias(self):
        mock_ctx = _mock_registry(active=[_ACTIVE_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["s", "list"])
        assert result.exit_code == 0
        assert "my-proj" in result.output

    def test_list_shows_table_headers(self):
        mock_ctx = _mock_registry(active=[_ACTIVE_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list"])
        assert "STATUS" in result.output
        assert "NAME" in result.output
        assert "CWD" in result.output

    def test_list_name_filter(self):
        """--name flag filters sessions by name."""
        mock_ctx = _mock_registry(active=[_ACTIVE_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list", "--name", "my-proj"])
        assert "my-proj" in result.output

    def test_list_name_filter_no_match(self):
        """--name flag with no matching session shows empty message."""
        mock_ctx = _mock_registry(active=[_ACTIVE_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list", "--name", "nonexistent"])
        assert "No active sessions." in result.output

    def test_list_all_shows_session_id_column(self):
        """--all flag adds SESSION ID column to output."""
        mock_ctx = _mock_registry(all=[_ACTIVE_SESSION])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "list", "--all"])
        assert "SESSION ID" in result.output
        # Full session ID should appear
        assert "aaaa1111-2222-3333-4444-555566667777" in result.output


class TestSessionInfo:
    """Behavior tests for 'session info'."""

    def test_info_shows_session_detail(self):
        mock_ctx = _mock_registry(session=_ACTIVE_SESSION)
        with patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "info", "aaaa1111-2222-3333-4444-555566667777"])
        assert result.exit_code == 0
        assert "aaaa1111-2222-3333-4444-555566667777" in result.output
        assert "active" in result.output
        assert "my-proj" in result.output
        assert "claude-sonnet-4-20250514" in result.output

    def test_info_not_found(self):
        mock_ctx = _mock_registry(session=None)
        with patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "info", "nonexistent-id"])
        assert result.exit_code == 0
        assert "Session not found: nonexistent-id" in result.output

    def test_info_shows_error_message(self):
        errored = {**_COMPLETED_SESSION, "status": "errored", "error_message": "Process 123 died"}
        mock_ctx = _mock_registry(session=errored)
        with patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "info", "bbbb1111-2222-3333-4444-555566667777"])
        assert "Process 123 died" in result.output

    def test_info_shows_cost(self):
        mock_ctx = _mock_registry(session=_ACTIVE_SESSION)
        with patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "info", "aaaa1111-2222-3333-4444-555566667777"])
        assert "$0.1234" in result.output


class TestSessionStop:
    """Behavior tests for top-level 'stop' command."""

    def test_stop_daemon_not_running(self):
        with patch("summon_claude.cli.stop.is_daemon_running", return_value=False):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop", "some-session-id"])
        assert result.exit_code == 0
        assert "not running" in result.output

    def test_stop_session_found(self):
        mock_ctx = _mock_registry(resolve=_ACTIVE_SESSION)
        with (
            patch("summon_claude.cli.stop.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.stop_session",
                new=AsyncMock(return_value=True),
            ),
            patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop", "aaaa1111"])
        assert result.exit_code == 0
        assert "Stop requested" in result.output

    def test_stop_session_not_found_in_daemon(self):
        mock_ctx = _mock_registry(resolve=_ACTIVE_SESSION)
        with (
            patch("summon_claude.cli.stop.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.stop_session",
                new=AsyncMock(return_value=False),
            ),
            patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop", "aaaa1111"])
        assert result.exit_code == 0
        assert "not owned by running daemon" in result.output
        assert "summon session cleanup" in result.output

    def test_stop_session_not_found_in_registry(self):
        mock_ctx = _mock_registry(resolve=None)
        with (
            patch("summon_claude.cli.stop.is_daemon_running", return_value=True),
            patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop", "nonexistent"])
        assert "Session not found" in result.output

    def test_stop_ambiguous_prefix_prompts(self):
        mock_ctx = _mock_registry(resolve=[_ACTIVE_SESSION, _COMPLETED_SESSION])
        with (
            patch("summon_claude.cli.stop.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.stop_session",
                new=AsyncMock(return_value=True),
            ),
            patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["stop", "ambig"], input="1\n")
        assert "matches 2 sessions" in result.output
        assert "Stop requested" in result.output


class TestSessionLogs:
    """Behavior tests for 'session logs'."""

    def test_logs_no_log_dir(self, tmp_path):
        missing_dir = tmp_path / "nonexistent"
        with patch("summon_claude.cli.session.get_data_dir", return_value=missing_dir):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs"])
        assert result.exit_code == 0
        assert "No log files found." in result.output

    def test_logs_lists_available_files(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "aaaa1111-2222-3333-4444-555566667777.log").write_text("line1\n")
        (log_dir / "bbbb1111-2222-3333-4444-555566667777.log").write_text("line2\n")
        with patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs"])
        assert result.exit_code == 0
        assert "Available session logs:" in result.output
        assert "aaaa1111-2222-3333-4444-555566667777" in result.output
        assert "bbbb1111-2222-3333-4444-555566667777" in result.output

    def test_logs_tails_specific_session(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        sid = "aaaa1111-2222-3333-4444-555566667777"
        log_content = "\n".join(f"log line {i}" for i in range(100))
        (log_dir / f"{sid}.log").write_text(log_content)
        with patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs", sid, "-n", "3"])
        assert result.exit_code == 0
        assert "log line 97" in result.output
        assert "log line 98" in result.output
        assert "log line 99" in result.output
        assert "log line 96" not in result.output

    def test_logs_resolves_partial_id(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        sid = "aaaa1111-2222-3333-4444-555566667777"
        (log_dir / f"{sid}.log").write_text("resolved log line\n")
        mock_ctx = _mock_registry(resolve=_ACTIVE_SESSION)
        with (
            patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path),
            patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs", "aaaa1111", "-n", "1"])
        assert result.exit_code == 0
        assert "resolved log line" in result.output

    def test_logs_not_found_by_name(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        mock_ctx = _mock_registry(resolve=None)
        with (
            patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path),
            patch("summon_claude.cli.helpers.SessionRegistry", return_value=mock_ctx),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs", "nonexistent"])
        assert "Session not found" in result.output

    def test_logs_session_not_found(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        with patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "logs", "aaaa1111-2222-3333-4444-555566667777"])
        assert result.exit_code == 0
        assert "No log file found for session" in result.output


class TestSessionCleanup:
    """Behavior tests for 'session cleanup'."""

    def test_cleanup_no_stale(self):
        mock_ctx = _mock_registry(stale=[])
        with patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "cleanup"])
        assert result.exit_code == 0
        assert "No stale sessions found." in result.output

    def test_cleanup_marks_stale_sessions(self):
        stale = [{**_ACTIVE_SESSION, "status": "active"}]
        mock_ctx = _mock_registry(stale=stale)
        with (
            patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx),
            patch("summon_claude.cli.session.SummonConfig", side_effect=Exception("no config")),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "cleanup"])
        assert result.exit_code == 0
        assert "Cleaned up 1 stale session(s)." in result.output

    def test_cleanup_removes_orphan_log_files(self, tmp_path):
        """Log files with no matching session in registry are deleted."""
        import os

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # Orphan: no matching session (backdate past the 60s safety window)
        orphan = log_dir / "deadbeef-dead-dead-dead-deaddeaddead.log"
        orphan.write_text("orphan\n")
        old_mtime = time.time() - 120
        os.utime(orphan, (old_mtime, old_mtime))
        # Known: matches a session in registry
        known = log_dir / f"{_ACTIVE_SESSION['session_id']}.log"
        known.write_text("valid\n")
        # Daemon log should never be deleted
        (log_dir / "daemon.log").write_text("daemon\n")

        mock_ctx = _mock_registry(stale=[], all=[_ACTIVE_SESSION])
        with (
            patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx),
            patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "cleanup"])
        assert result.exit_code == 0
        assert "Removed 1 orphan log file(s)." in result.output
        assert not orphan.exists()
        assert known.exists()
        assert (log_dir / "daemon.log").exists()

    def test_cleanup_removes_old_rotated_daemon_logs(self, tmp_path):
        """Rotated daemon logs older than 7 days are deleted."""
        import os

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "daemon.log").write_text("current\n")
        old_rotated = log_dir / "daemon.log.1"
        old_rotated.write_text("old rotated\n")
        # Set mtime to 8 days ago
        old_mtime = time.time() - 8 * 86400
        os.utime(old_rotated, (old_mtime, old_mtime))
        # Fresh rotated log should survive
        fresh_rotated = log_dir / "daemon.log.2"
        fresh_rotated.write_text("fresh rotated\n")

        mock_ctx = _mock_registry(stale=[], all=[])
        with (
            patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx),
            patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "cleanup"])
        assert result.exit_code == 0
        assert not old_rotated.exists()
        assert fresh_rotated.exists()

    def test_cleanup_runs_log_cleanup_even_without_stale_sessions(self, tmp_path):
        """Log cleanup runs regardless of whether stale sessions exist."""
        import os

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        orphan = log_dir / "deadbeef-dead-dead-dead-deaddeaddead.log"
        orphan.write_text("orphan\n")
        old_mtime = time.time() - 120
        os.utime(orphan, (old_mtime, old_mtime))

        mock_ctx = _mock_registry(stale=[], all=[])
        with (
            patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx),
            patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "cleanup"])
        assert result.exit_code == 0
        assert "No stale sessions found." in result.output
        assert "Removed 1 orphan log file(s)." in result.output
        assert not orphan.exists()

    def test_cleanup_preserves_recently_created_orphan_logs(self, tmp_path):
        """Orphan log files created within the last 60s are not deleted (race guard)."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # Fresh orphan — mtime is now, within the 60s safety window
        fresh_orphan = log_dir / "deadbeef-dead-dead-dead-deaddeaddead.log"
        fresh_orphan.write_text("just created\n")

        mock_ctx = _mock_registry(stale=[], all=[])
        with (
            patch("summon_claude.cli.session.SessionRegistry", return_value=mock_ctx),
            patch("summon_claude.cli.session.get_data_dir", return_value=tmp_path),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["session", "cleanup"])
        assert result.exit_code == 0
        assert fresh_orphan.exists(), "Recently-created orphan should survive cleanup"
        assert "orphan" not in result.output


# ---------------------------------------------------------------------------
# Update check integration in cmd_start
# ---------------------------------------------------------------------------


class TestAutoGeneratedNames:
    """Test auto-generated session names with hex suffix."""

    def test_auto_name_has_hex_suffix(self, tmp_path):
        """When --name is not provided, name should be cwd-basename-<6hex>."""
        import re

        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        captured_name = []

        async def capture_start(_config, _cwd, name, _model, _effort, _resume):
            captured_name.append(name)
            return "ABCD1234"

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "summon_claude.cli.SummonConfig.from_file",
                return_value=MagicMock(
                    slack_bot_token="xoxb-fake",
                    slack_app_token="xapp-fake",
                    slack_signing_secret="fake-secret",
                    default_model=None,
                    default_effort="high",
                    validate=MagicMock(),
                ),
            ),
            patch("summon_claude.cli.start.async_start", side_effect=capture_start),
            patch("summon_claude.cli.async_start", side_effect=capture_start),
            patch("summon_claude.cli.update_check.check_for_update", return_value=None),
        ):
            runner = CliRunner()
            runner.invoke(cli, ["start", "--cwd", str(project_dir)])

        assert len(captured_name) == 1
        name = captured_name[0]
        # Should match pattern: my-project-<6hex>
        assert re.match(r"^my-project-[0-9a-f]{6}$", name), (
            f"Auto-generated name {name!r} doesn't match pattern"
        )

    def test_explicit_name_not_suffixed(self):
        """When --name is provided, it should be used as-is."""
        captured_name = []

        async def capture_start(_config, _cwd, name, _model, _effort, _resume):
            captured_name.append(name)
            return "ABCD1234"

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "summon_claude.cli.SummonConfig.from_file",
                return_value=MagicMock(
                    slack_bot_token="xoxb-fake",
                    slack_app_token="xapp-fake",
                    slack_signing_secret="fake-secret",
                    default_model=None,
                    default_effort="high",
                    validate=MagicMock(),
                ),
            ),
            patch("summon_claude.cli.start.async_start", side_effect=capture_start),
            patch("summon_claude.cli.async_start", side_effect=capture_start),
            patch("summon_claude.cli.update_check.check_for_update", return_value=None),
        ):
            runner = CliRunner()
            runner.invoke(cli, ["start", "--name", "exact-name"])

        assert len(captured_name) == 1
        assert captured_name[0] == "exact-name"


def _mock_config():
    """Return a mock SummonConfig that passes validation."""
    config = MagicMock()
    config.slack_bot_token = "xoxb-fake"
    config.slack_app_token = "xapp-fake"
    config.slack_signing_secret = "fake-secret"
    config.default_model = None
    config.default_effort = "high"
    return config


def _start_patches(update_info=None):
    """Context manager that patches cmd_start dependencies and the update checker."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("shutil.which", return_value="/usr/bin/claude"))
    stack.enter_context(
        patch("summon_claude.cli.SummonConfig.from_file", return_value=_mock_config())
    )
    # Patch daemon interaction so tests don't try to fork/connect.
    stack.enter_context(patch("summon_claude.cli.start.start_daemon", return_value=None))
    stack.enter_context(
        patch(
            "summon_claude.cli.daemon_client.create_session",
            AsyncMock(return_value="ABCD1234"),
        )
    )
    stack.enter_context(
        patch(
            "summon_claude.cli.update_check.check_for_update",
            return_value=update_info,
        )
    )
    return stack


class TestUpdateCheckIntegration:
    """Test update check integration in cmd_start."""

    def test_start_shows_update_notification_on_stderr(self):
        from summon_claude.cli.update_check import UpdateInfo

        info = UpdateInfo(current="0.1.0", latest="0.2.0")
        with _start_patches(update_info=info):
            runner = CliRunner()
            result = runner.invoke(cli, ["start"])
        assert "0.1.0" in result.output
        assert "0.2.0" in result.output
        assert "uv tool upgrade" in result.output

    def test_start_no_notification_when_up_to_date(self):
        with _start_patches(update_info=None):
            runner = CliRunner()
            result = runner.invoke(cli, ["start"])
        assert "Update available" not in result.output

    def test_start_quiet_suppresses_update_notification(self):
        from summon_claude.cli.update_check import UpdateInfo

        info = UpdateInfo(current="0.1.0", latest="0.2.0")
        with _start_patches(update_info=info):
            runner = CliRunner()
            result = runner.invoke(cli, ["-q", "start"])
        assert "Update available" not in result.output

    def test_start_env_var_suppresses_update_check(self, monkeypatch):
        monkeypatch.setenv("SUMMON_NO_UPDATE_CHECK", "1")
        with _start_patches(update_info=None):
            runner = CliRunner()
            result = runner.invoke(cli, ["start"])
        assert "Update available" not in result.output

    def test_start_slow_update_check_does_not_block(self):
        """Update check that exceeds the join timeout should not block startup."""

        def slow_check():
            time.sleep(10)

        with _start_patches(update_info=None) as stack:
            stack.enter_context(
                patch("summon_claude.cli.update_check.check_for_update", side_effect=slow_check)
            )
            runner = CliRunner()
            start = time.monotonic()
            result = runner.invoke(cli, ["start"])
            elapsed = time.monotonic() - start
        # Should not wait the full 10 seconds — join timeout is 4s
        assert elapsed < 8
        assert "Update available" not in result.output


class TestMigrationNotification:
    """Test schema migration notification in cmd_start."""

    def test_start_shows_migration_notification(self, tmp_path):
        """cmd_start should print migration notice when schema was behind."""
        import asyncio

        import aiosqlite

        from summon_claude.sessions.registry import SessionRegistry

        db_path = tmp_path / "registry.db"

        # Create DB at version 1, then downgrade to 0
        async def _setup():
            async with SessionRegistry(db_path=db_path):
                pass
            async with aiosqlite.connect(str(db_path), isolation_level=None) as raw_db:
                await raw_db.execute("UPDATE schema_version SET version = 0")

        asyncio.run(_setup())

        with _start_patches() as stack:
            stack.enter_context(
                patch(
                    "summon_claude.sessions.registry.default_db_path",
                    return_value=db_path,
                )
            )
            runner = CliRunner()
            result = runner.invoke(cli, ["start"])
        assert "Database schema upgraded" in result.output
        assert "v0" in result.output

    def test_start_no_migration_notification_when_current(self, tmp_path):
        """cmd_start should not print migration notice when schema is current."""
        import asyncio

        from summon_claude.sessions.registry import SessionRegistry

        # Pre-create DB at current version
        db_path = tmp_path / "registry.db"

        async def _create():
            async with SessionRegistry(db_path=db_path):
                pass

        asyncio.run(_create())

        with _start_patches() as stack:
            stack.enter_context(
                patch(
                    "summon_claude.sessions.registry.default_db_path",
                    return_value=db_path,
                )
            )
            runner = CliRunner()
            result = runner.invoke(cli, ["start"])
        assert "schema upgraded" not in result.output


class TestCliDetection:
    """Tests for Claude CLI detection in CLI startup."""

    def test_start_fails_without_claude_cli(self, monkeypatch):
        """start command should fail with error when claude CLI not found."""

        # Mock shutil.which to return None (CLI not found)
        def mock_which(_program):
            pass

        monkeypatch.setattr("shutil.which", mock_which)

        runner = CliRunner()
        # The start command should check for claude CLI and fail
        result = runner.invoke(cli, ["start"])

        # Should exit with error (not 0)
        assert result.exit_code != 0
        # Error message should mention claude or installation
        assert "claude" in result.output.lower() or "install" in result.output.lower()

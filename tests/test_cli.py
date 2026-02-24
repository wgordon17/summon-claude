"""Tests for summon_claude.cli."""

from __future__ import annotations

from click.testing import CliRunner

from summon_claude.cli import (
    _format_ts,
    _print_auth_banner,
    _print_session_detail,
    _print_session_table,
    _truncate,
    cli,
)


class TestCLICommands:
    """Test that CLI commands are properly configured."""

    def test_cli_start_command_exists(self):
        """Test that start command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start a new summon session" in result.output

    def test_cli_status_command_exists(self):
        """Test that status command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0

    def test_cli_stop_command_exists(self):
        """Test that stop command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0

    def test_cli_sessions_command_exists(self):
        """Test that sessions command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["sessions", "--help"])
        assert result.exit_code == 0

    def test_cli_cleanup_command_exists(self):
        """Test that cleanup command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["cleanup", "--help"])
        assert result.exit_code == 0

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

    def test_start_accepts_background_flag(self):
        """Test that start command accepts --background flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["start", "--help"])
        assert "--background" in result.output or "-b" in result.output

    def test_verbose_flag_supported(self):
        """Test that -v/--verbose flag is supported."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert "-v" in result.output or "--verbose" in result.output

    def test_logs_command_exists(self):
        """Test that logs command is available (BUG-009)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["logs", "--help"])
        assert result.exit_code == 0


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
        _print_session_table(sessions)
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
                "total_turns": 5,
                "total_cost_usd": 0.0123,
            }
        ]
        _print_session_table(sessions)
        captured = capsys.readouterr()
        assert "active" in captured.out or "STATUS" in captured.out
        assert "5" in captured.out

    def test_handles_none_values(self, capsys):
        sessions = [
            {
                "session_id": "sess-x",
                "status": "pending_auth",
                "session_name": None,
                "slack_channel_name": None,
                "cwd": "/home",
                "total_turns": 0,
                "total_cost_usd": 0.0,
            }
        ]
        _print_session_table(sessions)
        captured = capsys.readouterr()
        assert "pending_auth" in captured.out


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
        _print_session_detail(session)
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
        _print_session_detail(session)
        captured = capsys.readouterr()
        assert "Connection failed" in captured.out


class TestTruncate:
    def test_short_string_not_truncated(self):
        result = _truncate("hello", 10)
        assert result == "hello"

    def test_long_string_truncated(self):
        result = _truncate("hello world this is long", 10)
        assert len(result) <= 10
        assert "..." in result

    def test_exactly_at_limit(self):
        result = _truncate("hello", 5)
        assert result == "hello"

    def test_one_over_limit(self):
        result = _truncate("hello!", 5)
        assert len(result) <= 5
        assert "..." in result


class TestFormatTs:
    def test_valid_iso_timestamp(self):
        result = _format_ts("2025-02-22T10:30:45+00:00")
        assert isinstance(result, str)
        assert "2025" in result or "10:30" in result

    def test_none_returns_dash(self):
        result = _format_ts(None)
        assert result == "-"

    def test_empty_string_returns_dash(self):
        result = _format_ts("")
        assert result == "-"

    def test_invalid_format_returns_as_is(self):
        result = _format_ts("not-a-timestamp")
        assert result == "not-a-timestamp"

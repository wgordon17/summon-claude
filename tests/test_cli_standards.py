"""Tests for CLI standards: global flags and new commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from summon_claude.cli import cli
from tests.conftest import ACTIVE_SESSION as _ACTIVE_SESSION
from tests.conftest import mock_registry as _mock_registry

# ---------------------------------------------------------------------------
# TestVersionFlag
# ---------------------------------------------------------------------------


class TestVersionFlag:
    """Tests for --version flag."""

    def test_version_flag_outputs_version(self):
        """Test that summon --version prints version string."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        # Click's version_option outputs: "version, <version>" format
        assert "summon" in result.output.lower()

    def test_version_flag_contains_package_name(self):
        """Test that version output contains 'summon'."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--version"])
        assert "summon" in result.output.lower()


# ---------------------------------------------------------------------------
# TestVersionCommand
# ---------------------------------------------------------------------------


class TestVersionCommand:
    """Tests for 'version' subcommand."""

    def test_version_command_outputs_extended_info(self):
        """Test that summon version prints extended info."""
        runner = CliRunner()
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert "summon" in result.output
        assert "Python" in result.output or "python" in result.output.lower()
        assert "Platform" in result.output or "platform" in result.output.lower()

    def test_version_command_json_output(self):
        """Test that summon version -o json outputs valid JSON."""
        runner = CliRunner()
        result = runner.invoke(cli, ["version", "-o", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "version" in data
        assert "python" in data
        assert "platform" in data

    def test_version_command_contains_all_fields(self):
        """Test that version command includes all expected fields."""
        runner = CliRunner()
        result = runner.invoke(cli, ["version", "-o", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        expected_fields = ["version", "python", "platform", "config_file", "data_dir", "db_path"]
        for field in expected_fields:
            assert field in data, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# TestQuietFlag
# ---------------------------------------------------------------------------


class TestQuietFlag:
    """Tests for --quiet flag."""

    def test_quiet_flag_suppresses_status_messages(self):
        """Test that --quiet suppresses non-essential output."""
        runner = CliRunner()
        with patch("summon_claude.cli.SessionRegistry", return_value=_mock_registry(active=[])):
            result = runner.invoke(cli, ["--quiet", "session", "list"])
            assert result.exit_code == 0
            # With quiet flag and no sessions, output should be empty
            assert result.output.strip() == ""

    def test_quiet_verbose_mutually_exclusive(self):
        """Test that --verbose and --quiet are mutually exclusive."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--verbose", "--quiet", "version"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_quiet_errors_still_shown(self):
        """Test that error messages are shown even with --quiet."""
        runner = CliRunner()
        with patch("summon_claude.cli.SessionRegistry", return_value=_mock_registry(session=None)):
            result = runner.invoke(cli, ["--quiet", "session", "info", "nonexistent"])
            # Errors should still be visible
            assert "not found" in result.output.lower() or result.exit_code != 0


# ---------------------------------------------------------------------------
# TestNoColorFlag
# ---------------------------------------------------------------------------


class TestNoColorFlag:
    """Tests for --no-color flag."""

    def test_no_color_flag_disables_color(self):
        """Test that --no-color flag is accepted and doesn't crash."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--no-color", "version"])
        assert result.exit_code == 0
        assert "summon" in result.output

    def test_no_color_env_var(self, monkeypatch):
        """Test that NO_COLOR environment variable also disables color."""
        monkeypatch.setenv("NO_COLOR", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["version"])
        # Just verify it doesn't crash; the flag should be honored
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# TestOutputFlag
# ---------------------------------------------------------------------------


class TestOutputFlag:
    """Tests for --output flag."""

    def test_output_json_session_list(self):
        """Test that -o json session list outputs valid JSON array."""
        runner = CliRunner()
        sessions = [_ACTIVE_SESSION]
        mock_ctx = _mock_registry(active=sessions)
        with patch("summon_claude.cli.SessionRegistry", return_value=mock_ctx):
            result = runner.invoke(cli, ["session", "list", "-o", "json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["session_id"] == _ACTIVE_SESSION["session_id"]

    def test_output_json_session_info(self):
        """Test that -o json session info outputs valid JSON object."""
        runner = CliRunner()
        mock_ctx = _mock_registry(session=_ACTIVE_SESSION)
        with patch("summon_claude.cli.SessionRegistry", return_value=mock_ctx):
            result = runner.invoke(cli, ["session", "info", "test-id", "-o", "json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, dict)
            assert data["session_id"] == _ACTIVE_SESSION["session_id"]

    def test_output_table_is_default(self):
        """Test that default output format is table."""
        runner = CliRunner()
        sessions = [_ACTIVE_SESSION]
        mock_ctx = _mock_registry(active=sessions)
        with patch("summon_claude.cli.SessionRegistry", return_value=mock_ctx):
            result = runner.invoke(cli, ["session", "list"])
            assert result.exit_code == 0
            # Table format should have headers and dashes
            assert "STATUS" in result.output or "my-proj" in result.output

    def test_output_invalid_choice_rejected(self):
        """Test that invalid output choice is rejected."""
        runner = CliRunner()
        result = runner.invoke(cli, ["version", "-o", "invalid"])
        assert result.exit_code != 0
        assert "invalid" in result.output.lower() or "choice" in result.output.lower()


# ---------------------------------------------------------------------------
# TestConfigFlag
# ---------------------------------------------------------------------------


class TestConfigFlag:
    """Tests for --config flag."""

    def test_config_flag_overrides_config_path(self, tmp_path):
        """Test that --config PATH overrides config file path."""
        runner = CliRunner()
        custom_config = tmp_path / "custom.env"
        custom_config.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-test\n")

        with patch(
            "summon_claude.cli.get_config_file",
            side_effect=lambda override=None: Path(override) if override else Path("/default"),
        ):
            result = runner.invoke(cli, ["--config", str(custom_config), "config", "path"])
            assert result.exit_code == 0
            assert str(custom_config) in result.output

    def test_config_show_with_override(self, tmp_path):
        """Test that config show respects --config override."""
        runner = CliRunner()
        custom_config = tmp_path / "custom.env"
        custom_config.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-test\n")

        with patch("summon_claude.cli.config.get_config_file", return_value=custom_config):
            result = runner.invoke(cli, ["--config", str(custom_config), "config", "show"])
            assert result.exit_code == 0
            # Secret values should show as 'configured', not the raw token
            assert "SUMMON_SLACK_BOT_TOKEN=configured" in result.output


# ---------------------------------------------------------------------------
# TestConfigCheck
# ---------------------------------------------------------------------------


class TestConfigCheck:
    """Tests for 'config check' subcommand."""

    def test_config_check_all_pass(self, tmp_path):
        """Test config check passes with valid config."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-valid-token\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-valid-token\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcd1234\n"
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.get_data_dir") as mock_data_dir,
        ):
            mock_data_dir.return_value = tmp_path
            result = runner.invoke(cli, ["config", "check"])
            assert result.exit_code == 0

    def test_config_check_missing_keys(self, tmp_path):
        """Test config check reports missing required keys."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-token\n")

        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            result = runner.invoke(cli, ["config", "check"])
            assert result.exit_code != 0
            assert "FAIL" in result.output

    def test_config_check_invalid_token_format(self, tmp_path):
        """Test config check reports invalid token format."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=invalid-token\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-token\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcd1234\n"
        )

        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            result = runner.invoke(cli, ["config", "check"])
            assert result.exit_code != 0
            assert "FAIL" in result.output

    def test_config_check_quiet_mode(self, tmp_path):
        """Test that quiet mode suppresses PASS messages."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-valid-token\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-valid-token\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcd1234\n"
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.get_data_dir") as mock_data_dir,
        ):
            mock_data_dir.return_value = tmp_path
            result = runner.invoke(cli, ["--quiet", "config", "check"])
            assert result.exit_code == 0
            # In quiet mode, PASS messages should not appear
            assert "PASS" not in result.output

    def test_config_check_exit_code_1_on_failure(self, tmp_path):
        """Test that exit code is 1 when config check fails."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        # Missing required keys
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-token\n")

        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            result = runner.invoke(cli, ["config", "check"])
            assert result.exit_code == 1

    def test_config_check_db_writable(self, tmp_path):
        """Test that config check verifies DB writability."""
        runner = CliRunner()
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-valid-token\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-valid-token\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcd1234\n"
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.get_data_dir", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["config", "check"])
            # Should pass since tmp_path is writable
            assert result.exit_code == 0
            assert "DB" in result.output


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestCLIStandardsIntegration:
    """Integration tests combining multiple standards."""

    def test_quiet_and_output_json_together(self):
        """Test that --quiet and -o json work together."""
        runner = CliRunner()
        sessions = [_ACTIVE_SESSION]
        mock_ctx = _mock_registry(active=sessions)
        with patch("summon_claude.cli.SessionRegistry", return_value=mock_ctx):
            result = runner.invoke(cli, ["--quiet", "session", "list", "-o", "json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)

    def test_config_flag_with_quiet(self, tmp_path):
        """Test that --config works with --quiet."""
        runner = CliRunner()
        custom_config = tmp_path / "custom.env"
        custom_config.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-test\n")

        with patch(
            "summon_claude.cli.get_config_file",
            side_effect=lambda override=None: Path(override) if override else Path("/default"),
        ):
            args = ["--quiet", "--config", str(custom_config), "config", "path"]
            result = runner.invoke(cli, args)
            assert result.exit_code == 0

    def test_no_color_with_table_output(self):
        """Test that --no-color works with default table output."""
        runner = CliRunner()
        sessions = [_ACTIVE_SESSION]
        mock_ctx = _mock_registry(active=sessions)
        with patch("summon_claude.cli.SessionRegistry", return_value=mock_ctx):
            result = runner.invoke(cli, ["--no-color", "session", "list"])
            assert result.exit_code == 0

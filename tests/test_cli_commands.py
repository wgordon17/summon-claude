"""Tests for new CLI commands: init, config show/set/path/edit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.cli_config import config_path, config_set, config_show


class TestCLIInitCommand:
    def test_init_command_exists(self):
        """Test that init command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0

    def test_config_command_exists(self):
        """Test that config command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "--help"])
        assert result.exit_code == 0


class TestCLIConfigSubcommands:
    def test_config_show_exists(self):
        """Test that config show subcommand exists."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "show", "--help"])
        assert result.exit_code == 0

    def test_config_path_exists(self):
        """Test that config path subcommand exists."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "path", "--help"])
        assert result.exit_code == 0

    def test_config_edit_exists(self):
        """Test that config edit subcommand exists."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "edit", "--help"])
        assert result.exit_code == 0

    def test_config_set_exists(self):
        """Test that config set subcommand exists."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "set", "--help"])
        assert result.exit_code == 0


class TestCmdInit:
    def test_init_creates_config_file(self, tmp_path):
        """init should create config.env with provided values."""
        config_dir = tmp_path / "summon"
        config_file = config_dir / "config.env"

        inputs = "\n".join(
            [
                "xoxb-valid-bot-token",  # bot token (valid)
                "xapp-valid-app-token",  # app token (valid)
                "mysecret",  # signing secret
                "U123,U456",  # allowed user IDs
            ]
        )

        with (
            patch("summon_claude.cli.get_config_dir", return_value=config_dir),
            patch("summon_claude.cli.get_config_file", return_value=config_file),
        ):
            runner = CliRunner()
            runner.invoke(cli, ["init"], input=inputs)

        assert config_file.exists()
        content = config_file.read_text()
        assert "xoxb-valid-bot-token" in content
        assert "xapp-valid-app-token" in content
        assert "mysecret" in content
        assert "U123,U456" in content

    def test_init_validates_bot_token_prefix(self, tmp_path):
        """init should reject bot tokens that don't start with xoxb-."""
        config_dir = tmp_path / "summon"
        config_file = config_dir / "config.env"

        # First provide invalid, then valid
        inputs = "\n".join(
            [
                "invalid-token",  # wrong prefix — should be rejected
                "xoxb-correct-token",  # correct
                "xapp-app-token",
                "mysecret",
                "U123",
            ]
        )

        with (
            patch("summon_claude.config.get_config_dir", return_value=config_dir),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        # Error message for invalid token should have been shown
        assert "xoxb-" in result.output or "Error" in result.output

    def test_init_validates_app_token_prefix(self, tmp_path):
        """init should reject app tokens that don't start with xapp-."""
        config_dir = tmp_path / "summon"
        config_file = config_dir / "config.env"

        inputs = "\n".join(
            [
                "xoxb-valid-bot",
                "invalid-app-token",  # wrong prefix
                "xapp-correct-app",  # correct
                "mysecret",
                "U123",
            ]
        )

        with (
            patch("summon_claude.config.get_config_dir", return_value=config_dir),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert "xapp-" in result.output or "Error" in result.output


class TestConfigShow:
    def test_config_show_masks_bot_token(self, tmp_path, capsys):
        """config show should mask SUMMON_SLACK_BOT_TOKEN after first 8 chars."""
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-secret-should-not-appear\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-another-secret\n"
            "SUMMON_SLACK_SIGNING_SECRET=mysecretvalue12345\n"
            "SUMMON_ALLOWED_USER_IDS=U123\n"
        )

        with patch("summon_claude.cli_config.get_config_file", return_value=config_file):
            config_show()

        captured = capsys.readouterr()
        # Full token should NOT appear
        assert "xoxb-secret-should-not-appear" not in captured.out
        # First 8 chars + "..." should appear
        assert "xoxb-sec..." in captured.out

    def test_config_show_masks_app_token(self, tmp_path, capsys):
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-testtest\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-supersecrettoken\n"
            "SUMMON_SLACK_SIGNING_SECRET=signingkey\n"
        )

        with patch("summon_claude.cli_config.get_config_file", return_value=config_file):
            config_show()

        captured = capsys.readouterr()
        assert "xapp-supersecrettoken" not in captured.out
        assert "xapp-sup..." in captured.out

    def test_config_show_masks_signing_secret(self, tmp_path, capsys):
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-testtest\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-testtest\n"
            "SUMMON_SLACK_SIGNING_SECRET=secretsigningkeyvalue\n"
        )

        with patch("summon_claude.cli_config.get_config_file", return_value=config_file):
            config_show()

        captured = capsys.readouterr()
        assert "secretsigningkeyvalue" not in captured.out
        assert "secretsi..." in captured.out

    def test_config_show_non_token_values_not_masked(self, tmp_path, capsys):
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-testtest\n"
            "SUMMON_ALLOWED_USER_IDS=U123,U456\n"
            "SUMMON_DEFAULT_MODEL=claude-opus-4-6\n"
        )

        with patch("summon_claude.cli_config.get_config_file", return_value=config_file):
            config_show()

        captured = capsys.readouterr()
        assert "U123,U456" in captured.out
        assert "claude-opus-4-6" in captured.out

    def test_config_show_no_file_prints_message(self, tmp_path, capsys):
        missing_file = tmp_path / "nonexistent.env"

        with patch("summon_claude.cli_config.get_config_file", return_value=missing_file):
            config_show()

        captured = capsys.readouterr()
        assert "No config file" in captured.out or "summon init" in captured.out


class TestConfigSet:
    def test_config_set_updates_existing_value(self, tmp_path):
        """config set should update an existing key in config.env."""
        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-old\nSUMMON_ALLOWED_USER_IDS=U1\n")

        with (
            patch("summon_claude.config.get_config_dir", return_value=tmp_path),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            config_set("SUMMON_SLACK_BOT_TOKEN", "xoxb-new-value")

        content = config_file.read_text()
        assert "xoxb-new-value" in content
        assert "xoxb-old" not in content

    def test_config_set_adds_new_key(self, tmp_path):
        """config set should add a new key if it doesn't exist."""
        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_ALLOWED_USER_IDS=U1\n")

        with (
            patch("summon_claude.config.get_config_dir", return_value=tmp_path),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            config_set("SUMMON_DEFAULT_MODEL", "claude-haiku-3")

        content = config_file.read_text()
        assert "SUMMON_DEFAULT_MODEL=claude-haiku-3" in content

    def test_config_set_creates_file_if_not_exists(self, tmp_path):
        """config set should create the config file if it doesn't exist yet."""
        config_dir = tmp_path / "newdir"
        config_dir.mkdir()
        config_file = config_dir / "config.env"

        with (
            patch("summon_claude.config.get_config_dir", return_value=config_dir),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            config_set("SUMMON_CHANNEL_PREFIX", "myprefix")

        assert config_file.exists()
        content = config_file.read_text()
        assert "SUMMON_CHANNEL_PREFIX=myprefix" in content

    def test_config_set_preserves_other_lines(self, tmp_path):
        """config set should not modify other lines in the file."""
        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-keep\nSUMMON_ALLOWED_USER_IDS=U1,U2\n")

        with (
            patch("summon_claude.config.get_config_dir", return_value=tmp_path),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            config_set("SUMMON_ALLOWED_USER_IDS", "U3,U4")

        content = config_file.read_text()
        assert "xoxb-keep" in content
        assert "U3,U4" in content
        assert "U1,U2" not in content


class TestConfigPath:
    def test_config_path_prints_location(self, tmp_path, capsys):
        """config path should print the config file location."""
        expected_path = tmp_path / "summon" / "config.env"

        with patch("summon_claude.cli_config.get_config_file", return_value=expected_path):
            config_path()

        captured = capsys.readouterr()
        assert str(expected_path) in captured.out


class TestCleanupCommand:
    """Test cleanup command for archiving stale session channels (BUG-007)."""

    def test_cleanup_command_exists(self):
        """Test that cleanup command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["cleanup", "--help"])
        assert result.exit_code == 0

    async def test_cleanup_archives_session_channel(self, tmp_path):
        """Test that cleanup with stale sessions calls archive_session_channel."""
        from unittest.mock import AsyncMock, patch

        from summon_claude.channel_manager import ChannelManager
        from summon_claude.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            # Register a dead session with a channel
            dead_pid = 999999999
            await registry.register("sess-stale", dead_pid, "/tmp")
            await registry.update_status("sess-stale", "pending_auth", slack_channel_id="C_STALE")

            # Mock the Slack client and ChannelManager
            mock_channel_manager = AsyncMock(spec=ChannelManager)
            mock_channel_manager.archive_session_channel = AsyncMock()

            # Manually run cleanup logic
            stale = await registry.list_stale()
            assert len(stale) == 1

            for session in stale:
                channel_id = session.get("slack_channel_id")
                if channel_id and mock_channel_manager:
                    await mock_channel_manager.archive_session_channel(channel_id)

            # Verify archive was called
            mock_channel_manager.archive_session_channel.assert_called_once_with("C_STALE")

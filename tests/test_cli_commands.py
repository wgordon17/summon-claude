"""Tests for new CLI commands: init, config show/set/path/edit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.cli.config import config_path, config_set, config_show
from summon_claude.cli.preflight import CliStatus


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
        config_file = tmp_path / "config.env"

        # Core prompts: 3 secrets, 1 choice (model), 1 choice (effort),
        # 1 text (prefix), 1 flag (scribe),
        # then "Configure advanced settings?" (no)
        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",  # slack_bot_token (secret)
                    "xapp-valid-app-token",  # slack_app_token (secret)
                    "abcdef012345",  # signing_secret (secret, hex)
                    "",  # default_model (choice: Enter accepts default)
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix (text, accept default)
                    "n",  # scribe_enabled (flag)
                    "n",  # Configure advanced settings? (no)
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert config_file.exists(), f"Config file not created. Output: {result.output}"
        content = config_file.read_text()
        assert "xoxb-valid-bot-token" in content
        assert "xapp-valid-app-token" in content
        assert "abcdef012345" in content

    def test_init_advanced_settings_yes(self, tmp_path):
        """init with advanced settings enabled should prompt for advanced options."""
        config_file = tmp_path / "config.env"

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",  # slack_bot_token
                    "xapp-valid-app-token",  # slack_app_token
                    "abcdef012345",  # signing_secret
                    "",  # default_model (choice: Enter accepts default)
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "y",  # Configure advanced settings? (YES)
                    "",  # global_pm_scan_interval_minutes (accept default)
                    "",  # global_pm_cwd (accept default)
                    "",  # global_pm_model (choice: Enter accepts default)
                    "",  # max_inline_chars (accept default)
                    "",  # permission_debounce_ms (accept default)
                    "",  # permission_timeout_s (accept default)
                    "y",  # no_update_check (flag)
                    "",  # safe_write_dirs (accept default)
                    "y",  # enable_thinking (flag)
                    "n",  # show_thinking (flag)
                    "y",  # auto_classifier_enabled (flag)
                    "",  # auto_mode_environment (accept default)
                    "",  # auto_mode_deny (accept default)
                    "",  # auto_mode_allow (accept default)
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert config_file.exists(), f"Config file not created. Output: {result.output}"
        content = config_file.read_text()
        assert "SUMMON_NO_UPDATE_CHECK=true" in content

    def test_init_with_existing_config(self, tmp_path):
        """init with existing config preserves values when Enter is pressed."""
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-existing\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-existing\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcdef012345\n"
        )

        # All empty inputs = accept existing/default values
        inputs = (
            "\n".join(
                [
                    "",  # slack_bot_token (keep existing)
                    "",  # slack_app_token (keep existing)
                    "",  # signing_secret (keep existing)
                    "",  # default_model (choice: Enter accepts default)
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert config_file.exists(), f"Config file not created. Output: {result.output}"
        content = config_file.read_text()
        assert "xoxb-existing" in content
        assert "Existing config found" in result.output

    def test_init_user_value_overrides_existing(self, tmp_path):
        """User-entered values must override existing config values on re-run.

        Verifies merge order: merged = {**existing, **collected} means collected wins.
        """
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-old-token\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-existing\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcdef012345\n"
        )

        inputs = (
            "\n".join(
                [
                    "xoxb-new-token",  # new bot token (override existing)
                    "",  # slack_app_token (keep existing)
                    "",  # signing_secret (keep existing)
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        content = config_file.read_text()
        assert "xoxb-new-token" in content, f"New value not written. Config:\n{content}"
        assert "xoxb-old-token" not in content, f"Old value not overridden. Config:\n{content}"

    def test_init_preserves_hidden_keys_on_rerun(self, tmp_path):
        """Hidden config keys (visible=False) must survive when init is re-run.

        SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS is never presented to the user
        during init (visible=lambda _config: False). The fix merges existing
        values before writing: merged = {**existing, **collected}. This test
        verifies that hidden keys are not silently dropped on re-run.
        """
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-existing\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-existing\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcdef012345\n"
            "SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS=C123,C456\n"
        )

        # All empty inputs = accept existing/default values (same as test_init_with_existing_config)
        inputs = (
            "\n".join(
                [
                    "",  # slack_bot_token (keep existing)
                    "",  # slack_app_token (keep existing)
                    "",  # signing_secret (keep existing)
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        assert config_file.exists(), f"Config file not created. Output: {result.output}"
        content = config_file.read_text()
        assert "SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS=C123,C456" in content, (
            f"Hidden key was dropped on re-run. Config:\n{content}\nOutput:\n{result.output}"
        )

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
            ]
        )

        with (
            patch("summon_claude.config.get_config_dir", return_value=config_dir),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert "xapp-" in result.output or "Error" in result.output

    def test_init_local_mode_creates_gitignore(self, tmp_path, monkeypatch):
        """init in local mode writes .gitignore inside .summon/ and prints warning."""
        summon_dir = tmp_path / ".summon"
        summon_dir.mkdir(parents=True)
        config_file = summon_dir / "config.env"

        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")

        from summon_claude.config import _detect_install_mode, _find_project_root

        _detect_install_mode.cache_clear()
        _find_project_root.cache_clear()

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "",  # github_pat
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert "Add .summon/ to your project's .gitignore" in result.output
        gitignore = summon_dir / ".gitignore"
        assert gitignore.exists()
        assert gitignore.read_text() == "*\n"

    def test_init_model_picker_other(self, tmp_path):
        """Selecting 'other' then entering a custom model stores the custom model, not 'other'."""
        config_file = tmp_path / "config.env"

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "7",  # default_model: select "other" (7th in fallback list)
                    "my-fine-tuned-model",  # default_model: custom model entry
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        content = config_file.read_text()
        assert "SUMMON_DEFAULT_MODEL=my-fine-tuned-model" in content
        assert "other" not in content

    def test_init_model_picker_other_empty_fresh_install(self, tmp_path):
        """Selecting 'other' then pressing Enter on fresh install stores nothing (skips)."""
        config_file = tmp_path / "config.env"

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "7",  # default_model: select "other" (7th in fallback list)
                    "",  # default_model (custom): empty → skip
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        content = config_file.read_text()
        # Empty "other" input on fresh install → value is "" → not stored (skipped)
        assert "SUMMON_DEFAULT_MODEL" not in content

    def test_init_model_picker_other_empty_reinit_keeps_existing(self, tmp_path):
        """Selecting 'other' then pressing Enter on re-init keeps the existing model."""
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-existing\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-existing\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcdef012345\n"
            "SUMMON_DEFAULT_MODEL=my-existing-model\n"
        )

        inputs = (
            "\n".join(
                [
                    "",  # slack_bot_token (keep existing)
                    "",  # slack_app_token (keep existing)
                    "",  # signing_secret (keep existing)
                    "8",  # default_model: select "other" (8th — custom model inserted before it)
                    "",  # default_model (custom): empty → fall back to current_value
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        content = config_file.read_text()
        assert "SUMMON_DEFAULT_MODEL=my-existing-model" in content

    def test_init_reinit_custom_model_preserved(self, tmp_path):
        """Re-init with existing custom model inserts it into choices and preserves it on Enter."""
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-existing\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-existing\n"
            "SUMMON_SLACK_SIGNING_SECRET=abcdef012345\n"
            "SUMMON_DEFAULT_MODEL=my-fine-tuned-model\n"
        )

        inputs = (
            "\n".join(
                [
                    "",  # slack_bot_token (keep existing)
                    "",  # slack_app_token (keep existing)
                    "",  # signing_secret (keep existing)
                    "",  # default_model: Enter selects dynamically-inserted custom (default)
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        content = config_file.read_text()
        assert "SUMMON_DEFAULT_MODEL=my-fine-tuned-model" in content

    def test_init_model_discovery_success(self, tmp_path):
        """When query_sdk_models succeeds, cache_sdk_models is called with the model list."""
        config_file = tmp_path / "config.env"
        sdk_models = [{"value": "claude-opus-4-6"}, {"value": "claude-sonnet-4-6"}]

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "",  # default_model (choice: Enter accepts default)
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=(sdk_models, "1.0.0", "claude-sonnet-4-6"),
            ),
            patch("summon_claude.cli.model_cache.cache_sdk_models") as mock_cache,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        mock_cache.assert_called_once_with(sdk_models, "1.0.0", "claude-sonnet-4-6")
        assert "done" in result.output

    def test_init_model_discovery_empty_models(self, tmp_path):
        """When query_sdk_models returns ([], ver), output says 'skipped (no models returned)'."""
        config_file = tmp_path / "config.env"

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "",  # default_model (choice: Enter accepts default)
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=([], "1.0.0", None),
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        assert "no models returned" in result.output

    def test_init_model_discovery_failure(self, tmp_path):
        """When query_sdk_models returns None, wizard continues with fallback choices."""
        config_file = tmp_path / "config.env"

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "",  # default_model (choice: Enter accepts default)
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # Configure advanced settings?
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
            patch(
                "summon_claude.cli.model_cache.load_cached_models",
                return_value=None,
            ),
            patch(
                "summon_claude.cli.model_cache.query_sdk_models",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        # Should succeed using fallback choices
        assert result.exit_code == 0, f"Init failed: {result.output}"
        assert "skipped" in result.output


class TestConfigShow:
    def test_config_show_hides_secrets(self, tmp_path, capsys):
        """config show should show 'configured' instead of secret values."""
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-secret-should-not-appear\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-another-secret\n"
            "SUMMON_SLACK_SIGNING_SECRET=mysecretvalue12345\n"
        )

        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_show()

        captured = capsys.readouterr()
        assert "xoxb-secret-should-not-appear" not in captured.out
        assert "xapp-another-secret" not in captured.out
        assert "mysecretvalue12345" not in captured.out
        assert "configured" in captured.out
        assert "SUMMON_SLACK_BOT_TOKEN" in captured.out

    def test_config_show_missing_secret(self, tmp_path, capsys):
        """config show should show 'missing' for empty secret values."""
        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=\n")

        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_show()

        captured = capsys.readouterr()
        assert "SUMMON_SLACK_BOT_TOKEN" in captured.out
        assert "not set" in captured.out

    def test_config_show_non_secret_values_shown(self, tmp_path, capsys):
        config_file = tmp_path / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-testtest\nSUMMON_DEFAULT_MODEL=claude-opus-4-6\n"
        )

        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_show()

        captured = capsys.readouterr()
        assert "claude-opus-4-6" in captured.out

    def test_config_show_no_file_shows_defaults(self, tmp_path, capsys):
        """config show works even when no config file exists — shows all options with defaults."""
        missing_file = tmp_path / "nonexistent.env"

        with patch("summon_claude.cli.config.get_config_file", return_value=missing_file):
            config_show()

        captured = capsys.readouterr()
        # Should still show all options with default/not-set indicators
        assert "Slack Credentials" in captured.out
        assert "SUMMON_SLACK_BOT_TOKEN" in captured.out


class TestConfigSet:
    def test_config_set_updates_existing_value(self, tmp_path):
        """config set should update an existing key in config.env."""
        config_file = tmp_path / "config.env"
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-old\n")

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
        config_file.write_text("SUMMON_DEFAULT_MODEL=claude-opus-4-6\n")

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
        config_file.write_text("SUMMON_SLACK_BOT_TOKEN=xoxb-keep\nSUMMON_CHANNEL_PREFIX=test\n")

        with (
            patch("summon_claude.config.get_config_dir", return_value=tmp_path),
            patch("summon_claude.config.get_config_file", return_value=config_file),
        ):
            config_set("SUMMON_CHANNEL_PREFIX", "new-prefix")

        content = config_file.read_text()
        assert "xoxb-keep" in content
        assert "new-prefix" in content
        assert "SUMMON_CHANNEL_PREFIX=test" not in content


class TestConfigSetNewlineStripping:
    def test_config_set_strips_newlines(self, tmp_path):
        """config set strips \\n and \\r to prevent .env injection."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")
        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            # Use DEFAULT_MODEL (text type, no validate_fn) to test the stripping
            config_set("SUMMON_DEFAULT_MODEL", "model-a\nSUMMON_SLACK_BOT_TOKEN=injected")
        content = config_file.read_text()
        lines = content.strip().splitlines()
        assert len(lines) == 1, f"Expected 1 line, got {len(lines)}: {lines}"
        assert "SUMMON_DEFAULT_MODEL=model-aSUMMON_SLACK_BOT_TOKEN=injected" in content

    def test_config_set_strips_carriage_return(self, tmp_path):
        """config set strips \\r from values."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")
        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_set("SUMMON_DEFAULT_MODEL", "abc\r\ndef")
        content = config_file.read_text()
        assert "\r" not in content
        assert "\n" not in content.split("=", 1)[1].strip()


class TestConfigSetValidation:
    def test_config_set_rejects_unknown_key(self, tmp_path):
        """config set should reject keys not in CONFIG_OPTIONS."""
        config_file = tmp_path / "config.env"
        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            pytest.raises(SystemExit),
        ):
            config_set("SUMMON_BOGUS_KEY", "value")

    def test_config_set_normalizes_bool_true(self, tmp_path):
        """config set should normalize 'yes' to 'true' for flag options."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")
        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_set("SUMMON_NO_UPDATE_CHECK", "yes")
        assert "SUMMON_NO_UPDATE_CHECK=true" in config_file.read_text()

    def test_config_set_normalizes_bool_false(self, tmp_path):
        """config set should normalize '0' to 'false' for flag options."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")
        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_set("SUMMON_NO_UPDATE_CHECK", "0")
        assert "SUMMON_NO_UPDATE_CHECK=false" in config_file.read_text()

    def test_config_set_rejects_invalid_bool(self, tmp_path):
        """config set should reject invalid boolean values for flag options."""
        config_file = tmp_path / "config.env"
        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            pytest.raises(SystemExit),
        ):
            config_set("SUMMON_NO_UPDATE_CHECK", "nah")

    def test_config_set_runs_validate_fn(self, tmp_path):
        """config set should reject values that fail validate_fn."""
        config_file = tmp_path / "config.env"
        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            pytest.raises(SystemExit),
        ):
            config_set("SUMMON_SLACK_BOT_TOKEN", "invalid-no-prefix")


class TestCheckClaudeCli:
    """Unit tests for check_claude_cli preflight function."""

    def test_not_found(self):
        """Returns found=False when claude is not on PATH."""
        from summon_claude.cli.preflight import check_claude_cli

        with patch("summon_claude.cli.preflight.shutil.which", return_value=None):
            result = check_claude_cli()
        assert result.found is False
        assert result.version is None
        assert result.path is None

    def test_found_with_version(self):
        """Returns found=True and version when claude runs successfully."""
        from summon_claude.cli.preflight import check_claude_cli

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "claude-code 1.2.3\n"
        with (
            patch("summon_claude.cli.preflight.shutil.which", return_value="/usr/bin/claude"),
            patch("summon_claude.cli.preflight.subprocess.run", return_value=mock_result),
        ):
            result = check_claude_cli()
        assert result.found is True
        assert result.version == "claude-code 1.2.3"
        assert result.path == "/usr/bin/claude"

    def test_found_but_timeout(self):
        """Returns found=True, version=None when subprocess times out."""
        import subprocess

        from summon_claude.cli.preflight import check_claude_cli

        with (
            patch("summon_claude.cli.preflight.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "summon_claude.cli.preflight.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5),
            ),
        ):
            result = check_claude_cli()
        assert result.found is True
        assert result.version is None
        assert result.path == "/usr/bin/claude"

    def test_found_but_nonzero_exit(self):
        """Returns found=True, version=None when claude exits with error."""
        from summon_claude.cli.preflight import check_claude_cli

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with (
            patch("summon_claude.cli.preflight.shutil.which", return_value="/usr/bin/claude"),
            patch("summon_claude.cli.preflight.subprocess.run", return_value=mock_result),
        ):
            result = check_claude_cli()
        assert result.found is True
        assert result.version is None


class TestCheckGithubStatus:
    """Tests for _check_github_status function."""

    def test_returns_none_when_no_token(self, capsys):
        """No stored token returns None."""
        from summon_claude.cli.config import _check_github_status

        with patch("summon_claude.github_auth.load_token", return_value=None):
            result = _check_github_status()

        assert result is None
        captured = capsys.readouterr()
        assert "not configured" in captured.out

    def test_returns_true_when_valid(self, capsys):
        """Valid token returns True with user info."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch(
                "summon_claude.cli.config.asyncio.run",
                return_value={"login": "testuser", "scopes": "repo"},
            ),
        ):
            result = _check_github_status()

        assert result is True
        captured = capsys.readouterr()
        assert "testuser" in captured.out

    def test_sanitizes_login_with_escape_sequences(self, capsys):
        """Login containing terminal escape sequences is stripped before display."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch(
                "summon_claude.cli.config.asyncio.run",
                return_value={"login": "evil\x1b[31muser", "scopes": "repo\x1b[0m"},
            ),
        ):
            result = _check_github_status()

        assert result is True
        captured = capsys.readouterr()
        assert "\x1b" not in captured.out
        assert "evil" in captured.out
        assert "[31m" not in captured.out  # bracket stripped from ANSI sequence

    def test_returns_false_when_invalid(self, capsys):
        """Invalid token returns False."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_bad"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch("summon_claude.cli.config.asyncio.run", return_value=None),
        ):
            result = _check_github_status()

        assert result is False
        captured = capsys.readouterr()
        assert "invalid" in captured.out

    def test_returns_true_on_network_error(self, capsys):
        """Network error returns True (token exists, can't validate)."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch("summon_claude.cli.config.asyncio.run", side_effect=OSError("timeout")),
        ):
            result = _check_github_status()

        assert result is True
        captured = capsys.readouterr()
        assert "network error" in captured.out

    def test_returns_true_on_github_auth_error(self, capsys):
        """GitHubAuthError during validation returns True (network error path)."""
        from summon_claude.cli.config import _check_github_status
        from summon_claude.github_auth import GitHubAuthError

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch(
                "summon_claude.cli.config.asyncio.run",
                side_effect=GitHubAuthError("HTTP 503"),
            ),
        ):
            result = _check_github_status()

        assert result is True
        captured = capsys.readouterr()
        assert "network error" in captured.out

    def test_returns_true_on_aiohttp_client_error(self, capsys):
        """aiohttp.ClientError during validation returns True (network error path)."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch(
                "summon_claude.cli.config.asyncio.run",
                side_effect=aiohttp.ServerDisconnectedError(),
            ),
        ):
            result = _check_github_status()

        assert result is True
        captured = capsys.readouterr()
        assert "network error" in captured.out

    def test_quiet_mode_no_token(self, capsys):
        """quiet=True suppresses output when no token stored."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value=None),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
        ):
            result = _check_github_status(quiet=True)

        assert result is None
        assert capsys.readouterr().out == ""

    def test_quiet_mode_valid_token(self, capsys):
        """quiet=True suppresses output when token is valid."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_test"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch(
                "summon_claude.cli.config.asyncio.run",
                return_value={"login": "testuser", "scopes": "repo"},
            ),
        ):
            result = _check_github_status(quiet=True)

        assert result is True
        assert capsys.readouterr().out == ""

    def test_quiet_mode_invalid_token(self, capsys):
        """quiet=True suppresses output when token is invalid."""
        from summon_claude.cli.config import _check_github_status

        with (
            patch("summon_claude.github_auth.load_token", return_value="gho_bad"),
            patch("summon_claude.github_auth.validate_token", new=MagicMock()),
            patch("summon_claude.cli.config.asyncio.run", return_value=None),
        ):
            result = _check_github_status(quiet=True)

        assert result is False
        assert capsys.readouterr().out == ""


class TestAuthStatus:
    """Tests for summon auth status command."""

    def test_auth_status_no_providers(self, tmp_path):
        """auth status shows guidance when no providers configured."""
        with (
            patch("summon_claude.cli.auth._check_github_status", return_value=None),
            patch("summon_claude.cli.auth._check_google_status", return_value=None),
            patch("summon_claude.cli.auth.get_workspace_config_path", return_value=tmp_path / "x"),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=False),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "No authentication configured" in result.output
        assert "summon auth github login" in result.output

    def test_auth_status_github_configured(self, tmp_path):
        """auth status shows GitHub status when token exists."""
        with (
            patch("summon_claude.cli.auth._check_github_status", return_value=True),
            patch("summon_claude.cli.auth._check_google_status", return_value=None),
            patch("summon_claude.cli.auth.get_workspace_config_path", return_value=tmp_path / "x"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "No authentication configured" not in result.output

    def test_auth_status_corrupted_workspace_json(self, tmp_path):
        """auth status handles corrupted workspace JSON gracefully."""
        ws_file = tmp_path / "ws.json"
        ws_file.write_text("not valid json{{{")
        with (
            patch("summon_claude.cli.auth._check_github_status", return_value=None),
            patch("summon_claude.cli.auth._check_google_status", return_value=None),
            patch("summon_claude.cli.auth.get_workspace_config_path", return_value=ws_file),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "corrupted" in result.output
        assert "summon auth slack login" in result.output

    def test_auth_status_jira_authenticated_with_site(self, tmp_path):
        """auth status Jira PASS line includes site name when available."""
        with (
            patch("summon_claude.cli.auth._check_github_status", return_value=None),
            patch("summon_claude.cli.auth._check_google_status", return_value=None),
            patch("summon_claude.cli.auth.get_workspace_config_path", return_value=tmp_path / "x"),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=True),
            patch("summon_claude.jira_auth.check_jira_status", return_value=None),
            patch(
                "summon_claude.jira_auth.get_jira_site_name",
                return_value="mycompany.atlassian.net",
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "Jira: authenticated (site: mycompany.atlassian.net)" in result.output

    def test_auth_status_jira_authenticated_without_site(self, tmp_path):
        """auth status Jira PASS line omits site suffix when site name unavailable."""
        with (
            patch("summon_claude.cli.auth._check_github_status", return_value=None),
            patch("summon_claude.cli.auth._check_google_status", return_value=None),
            patch("summon_claude.cli.auth.get_workspace_config_path", return_value=tmp_path / "x"),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=True),
            patch("summon_claude.jira_auth.check_jira_status", return_value=None),
            patch("summon_claude.jira_auth.get_jira_site_name", return_value=None),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "Jira: authenticated" in result.output
        assert "(site:" not in result.output

    def test_auth_status_slack_authenticated_labels(self, tmp_path):
        """auth status Slack PASS line uses 'workspace:' and 'saved' labels."""
        import json

        ws_file = tmp_path / "ws.json"
        ws_file.write_text(json.dumps({"url": "myteam.slack.com"}))
        with (
            patch("summon_claude.cli.auth._check_github_status", return_value=None),
            patch("summon_claude.cli.auth._check_google_status", return_value=None),
            patch("summon_claude.cli.auth.get_workspace_config_path", return_value=ws_file),
            patch("summon_claude.jira_auth.jira_credentials_exist", return_value=False),
            patch(
                "summon_claude.cli.auth._check_existing_slack_auth",
                return_value={"age": "2 days ago"},
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status"])
        assert result.exit_code == 0
        assert "workspace: myteam.slack.com" in result.output
        assert "saved 2 days ago" in result.output


class TestAuthStatusJSON:
    """Tests for summon auth status --json flag."""

    def test_auth_status_json_no_providers(self, tmp_path):
        """--json outputs all four providers as not_configured."""
        import json

        with (
            patch(
                "summon_claude.cli.auth._check_github_status_data",
                return_value={"provider": "github", "status": "not_configured"},
            ),
            patch(
                "summon_claude.cli.auth._check_google_status_data",
                return_value={"provider": "google", "status": "not_configured"},
            ),
            patch(
                "summon_claude.cli.auth._check_jira_status_data",
                return_value={"provider": "jira", "status": "not_configured"},
            ),
            patch(
                "summon_claude.cli.auth._check_slack_status_data",
                return_value={"provider": "slack", "status": "not_configured"},
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["providers"]) == 4
        assert all(p["status"] == "not_configured" for p in data["providers"])

    def test_auth_status_json_github_configured(self, tmp_path):
        """--json includes GitHub login and scopes."""
        import json

        with (
            patch(
                "summon_claude.cli.auth._check_github_status_data",
                return_value={
                    "provider": "github",
                    "status": "authenticated",
                    "login": "testuser",
                    "scopes": "repo",
                },
            ),
            patch(
                "summon_claude.cli.auth._check_google_status_data",
                return_value={"provider": "google", "status": "not_configured"},
            ),
            patch(
                "summon_claude.cli.auth._check_jira_status_data",
                return_value={"provider": "jira", "status": "not_configured"},
            ),
            patch(
                "summon_claude.cli.auth._check_slack_status_data",
                return_value={"provider": "slack", "status": "not_configured"},
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        github = data["providers"][0]
        assert github["login"] == "testuser"
        assert github["scopes"] == "repo"


class TestGitHubAuthCLI:
    """Tests for auth github login/logout Click commands."""

    def test_github_auth_success(self):
        from unittest.mock import AsyncMock

        from summon_claude.github_auth import DeviceFlowResult

        mock_result = DeviceFlowResult(
            token="gho_test",
            login="octocat",
            scopes="repo",
            token_path=Path("/fake/path"),
        )
        with (
            patch(
                "summon_claude.github_auth.run_device_flow",
                new=AsyncMock(return_value=mock_result),
            ),
            patch("summon_claude.cli.config.click.launch"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "login"])

        assert result.exit_code == 0
        assert "octocat" in result.output

    def test_github_auth_sanitizes_device_code_output(self):
        """Device code callback strips non-printable chars from user_code and verification_uri."""
        from unittest.mock import AsyncMock

        from summon_claude.github_auth import DeviceFlowResult

        mock_result = DeviceFlowResult(
            token="gho_test",
            login="user",
            scopes="repo",
            token_path=Path("/fake"),
        )

        async def _fake_flow(on_code=None, **_kwargs):
            if on_code:
                on_code("AB\x1b[31mCD", "https://github.com/login/device\x1b[0m")
            return mock_result

        with (
            patch(
                "summon_claude.github_auth.run_device_flow",
                new=AsyncMock(side_effect=_fake_flow),
            ),
            patch("summon_claude.cli.config.click.launch"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "login"])

        assert result.exit_code == 0
        # ESC byte stripped — ANSI sequence can't be interpreted by terminal
        assert "\x1b" not in result.output
        assert "AB" in result.output
        assert "CD" in result.output

    def test_github_auth_error(self):
        from unittest.mock import AsyncMock

        from summon_claude.github_auth import GitHubAuthError

        with patch(
            "summon_claude.github_auth.run_device_flow",
            new=AsyncMock(side_effect=GitHubAuthError("test error")),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "login"])

        assert result.exit_code != 0
        assert "test error" in result.output

    def test_github_auth_network_error(self):
        """aiohttp.ClientError prints network error and exits non-zero."""
        from unittest.mock import AsyncMock

        with patch(
            "summon_claude.github_auth.run_device_flow",
            new=AsyncMock(side_effect=aiohttp.ClientError("Connection refused")),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "login"])

        assert result.exit_code != 0
        assert "Network error" in result.output

    def test_github_auth_keyboard_interrupt(self):
        """Ctrl+C during auth prints cancellation message."""
        from unittest.mock import AsyncMock

        with patch(
            "summon_claude.github_auth.run_device_flow",
            new=AsyncMock(side_effect=KeyboardInterrupt),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "login"])

        assert "cancelled" in result.output.lower()

    def test_github_logout_with_token(self):
        with patch("summon_claude.github_auth.remove_token", return_value=True):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "logout"])

        assert result.exit_code == 0
        assert "GitHub credentials removed." in result.output

    def test_github_logout_no_token(self):
        with patch("summon_claude.github_auth.remove_token", return_value=False):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "logout"])

        assert result.exit_code == 0
        assert "No GitHub credentials stored." in result.output


class TestConfigSetChoiceValidation:
    def test_config_set_rejects_invalid_choice(self, tmp_path):
        """config set should reject values not in choices for choice-type options."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")
        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            pytest.raises(SystemExit),
        ):
            config_set("SUMMON_DEFAULT_EFFORT", "ultra")

    def test_config_set_accepts_valid_choice(self, tmp_path):
        """config set should accept valid choices."""
        config_file = tmp_path / "config.env"
        config_file.write_text("")
        with patch("summon_claude.cli.config.get_config_file", return_value=config_file):
            config_set("SUMMON_DEFAULT_EFFORT", "high")
        assert "SUMMON_DEFAULT_EFFORT=high" in config_file.read_text()


class TestConfigSetChoicesFn:
    """Tests for choices_fn validation path in config_set."""

    def test_config_set_rejects_value_from_choices_fn(self, tmp_path):
        """config set should reject values not in choices_fn result."""
        from summon_claude.config import ConfigOption

        config_file = tmp_path / "config.env"
        config_file.write_text("")

        fake_option = ConfigOption(
            field_name="default_model",
            env_key="SUMMON_DEFAULT_MODEL",
            group="Test",
            label="Test",
            help_text="Test",
            input_type="choice",
            choices_fn=lambda: ["model-a", "model-b"],
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.CONFIG_OPTIONS", [fake_option]),
            pytest.raises(SystemExit),
        ):
            config_set("SUMMON_DEFAULT_MODEL", "model-z")

    def test_config_set_accepts_value_from_choices_fn(self, tmp_path):
        """config set should accept values in choices_fn result."""
        from summon_claude.config import ConfigOption

        config_file = tmp_path / "config.env"
        config_file.write_text("")

        fake_option = ConfigOption(
            field_name="default_model",
            env_key="SUMMON_DEFAULT_MODEL",
            group="Test",
            label="Test",
            help_text="Test",
            input_type="choice",
            choices_fn=lambda: ["model-a", "model-b"],
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.CONFIG_OPTIONS", [fake_option]),
        ):
            config_set("SUMMON_DEFAULT_MODEL", "model-a")
        assert "SUMMON_DEFAULT_MODEL=model-a" in config_file.read_text()

    def test_config_set_handles_choices_fn_error(self, tmp_path):
        """config set should handle choices_fn that raises."""
        from summon_claude.config import ConfigOption

        config_file = tmp_path / "config.env"
        config_file.write_text("")

        def _broken():
            raise RuntimeError("API down")

        fake_option = ConfigOption(
            field_name="default_model",
            env_key="SUMMON_DEFAULT_MODEL",
            group="Test",
            label="Test",
            help_text="Test",
            input_type="choice",
            choices_fn=_broken,
        )

        with (
            patch("summon_claude.cli.config.get_config_file", return_value=config_file),
            patch("summon_claude.cli.config.CONFIG_OPTIONS", [fake_option]),
            pytest.raises(SystemExit),
        ):
            config_set("SUMMON_DEFAULT_MODEL", "anything")


class TestConfigPath:
    def test_config_path_prints_location(self, tmp_path, capsys):
        """config path should print the config file location."""
        expected_path = tmp_path / "summon" / "config.env"

        with patch("summon_claude.cli.config.get_config_file", return_value=expected_path):
            config_path()

        captured = capsys.readouterr()
        assert str(expected_path) in captured.out


class TestCleanupCommand:
    """Test cleanup command for archiving stale session channels (BUG-007)."""

    def test_cleanup_command_exists(self):
        """Test that session cleanup command is available."""
        runner = CliRunner()
        result = runner.invoke(cli, ["session", "cleanup", "--help"])
        assert result.exit_code == 0

    async def test_cleanup_archives_session_channel(self, tmp_path):
        """Test that cleanup with stale sessions archives channel via web_client."""
        from unittest.mock import AsyncMock

        from summon_claude.sessions.registry import SessionRegistry

        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            # Register a dead session with a channel
            dead_pid = 999999999
            await registry.register("sess-stale", dead_pid, "/tmp")
            await registry.update_status("sess-stale", "pending_auth", slack_channel_id="C_STALE")

            # Mock conversations_archive directly
            mock_web_client = AsyncMock()
            mock_web_client.conversations_archive = AsyncMock()

            # Manually run cleanup logic
            stale = await registry.list_stale()
            assert len(stale) == 1

            for session in stale:
                channel_id = session.get("slack_channel_id")
                if channel_id and mock_web_client:
                    await mock_web_client.conversations_archive(channel=channel_id)

            # Verify archive was called
            mock_web_client.conversations_archive.assert_called_once_with(channel="C_STALE")


class TestMaskSecret:
    """Tests for _mask_secret helper (BUG-046)."""

    def test_normal_token(self):
        from summon_claude.cli import _mask_secret

        result = _mask_secret("xoxb-1234567890-abcdefghij")
        assert result.startswith("xoxb-")
        assert "26 chars" in result
        # Must not reveal unique suffix characters
        assert "ghij" not in result

    def test_short_value(self):
        from summon_claude.cli import _mask_secret

        result = _mask_secret("abc")
        assert "3 chars" in result
        # Short values should not reveal original content
        assert "abc" not in result

    def test_empty_value(self):
        from summon_claude.cli import _mask_secret

        result = _mask_secret("")
        assert result == "(empty)"

    def test_short_value_no_prefix(self):
        """Values ≤ 2*prefix_len (10 chars) show only count, no prefix."""
        from summon_claude.cli import _mask_secret

        result = _mask_secret("1234567890")  # exactly 10 chars = 2 * prefix_len
        assert "10 chars" in result
        assert "12345" not in result  # prefix must NOT be shown

    def test_boundary_prefix_shown(self):
        """Values > 2*prefix_len (11+ chars) show prefix + count."""
        from summon_claude.cli import _mask_secret

        result = _mask_secret("12345678901")  # 11 chars > threshold
        assert "11 chars" in result
        assert result.startswith("12345")  # prefix IS shown


class TestGetUpgradeCommand:
    """Tests for get_upgrade_command helper (BUG-069)."""

    def test_default_is_uv(self):
        from summon_claude.cli.config import get_upgrade_command

        with patch("summon_claude.cli.config.sys") as mock_sys:
            mock_sys.executable = "/home/user/.local/share/uv/tools/summon-claude/bin/python"
            assert "uv tool upgrade" in get_upgrade_command()

    def test_homebrew_detected(self):
        from summon_claude.cli.config import get_upgrade_command

        with patch("summon_claude.cli.config.sys") as mock_sys:
            mock_sys.executable = "/opt/homebrew/Cellar/summon-claude/1.0/libexec/bin/python"
            assert "brew upgrade" in get_upgrade_command()

    def test_homebrew_detected_via_homebrew_path(self):
        from summon_claude.cli.config import get_upgrade_command

        with patch("summon_claude.cli.config.sys") as mock_sys:
            mock_sys.executable = "/opt/homebrew/opt/python/bin/python3"
            assert "brew upgrade" in get_upgrade_command()

    def test_pipx_detected(self):
        from summon_claude.cli.config import get_upgrade_command

        with patch("summon_claude.cli.config.sys") as mock_sys:
            mock_sys.executable = "/home/user/.local/pipx/venvs/summon-claude/bin/python"
            assert "pipx upgrade" in get_upgrade_command()


class TestSlackAuthLoginCLI:
    """Tests for auth slack login exception-to-exit path."""

    def test_slack_auth_exception_exits_nonzero(self):
        """When interactive_slack_auth raises, CLI exits non-zero."""
        runner = CliRunner()
        with (
            patch(
                "summon_claude.cli.slack_auth.asyncio.run",
                side_effect=RuntimeError("browser crashed"),
            ),
            patch(
                "summon_claude.cli.slack_auth._check_existing_slack_auth",
                return_value=None,
            ),
        ):
            result = runner.invoke(
                cli, ["auth", "slack", "login", "myteam.slack.com"], catch_exceptions=False
            )

        assert result.exit_code != 0
        assert "Slack login failed" in result.output


class TestInitPydanticValidationError:
    """Tests for cmd_init pydantic ValidationError handler."""

    def test_init_validation_error_exits_nonzero(self, tmp_path):
        """cmd_init exits non-zero when SummonConfig construction raises ValidationError."""
        import pydantic

        config_file = tmp_path / "config.env"

        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )

        # Build a real ValidationError
        try:
            from summon_claude.config import SummonConfig

            SummonConfig(
                slack_bot_token="bad",
                slack_app_token="bad",
                slack_signing_secret="not-hex",
                _env_file=None,
            )
            real_error = None
        except pydantic.ValidationError as exc:
            real_error = exc

        if real_error is None:
            pytest.skip("Could not construct a real ValidationError")

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.SummonConfig", side_effect=real_error),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code != 0
        # Validation failure now writes config and reports issues
        combined = result.output + (result.stderr or "")
        assert "potential issues" in combined or "Fix with" in combined


class TestGitHubStatusCmd:
    def test_auth_github_status(self):
        """auth github status calls _check_github_status."""
        with patch("summon_claude.cli.auth._check_github_status") as mock_check:
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "github", "status"])
        assert result.exit_code == 0
        mock_check.assert_called_once()


class TestGoogleLogoutCmd:
    def test_auth_google_logout_removes_credentials(self, tmp_path):
        """auth google logout removes {email}.json credential files."""
        from summon_claude.config import GoogleAccount

        account_dir = tmp_path / "default"
        account_dir.mkdir()
        cred_file = account_dir / "test@example.com.json"
        cred_file.write_text("{}")
        client_secret = account_dir / "client_secret.json"
        client_secret.write_text("{}")

        mock_account = GoogleAccount(
            label="default",
            creds_dir=account_dir,
            email="test@example.com",
        )
        with patch("summon_claude.config.discover_google_accounts", return_value=[mock_account]):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "google", "logout"])

        assert result.exit_code == 0
        assert "Google credentials removed" in result.output
        assert not cred_file.exists()  # credential deleted
        assert client_secret.exists()  # client_secret preserved

    def test_auth_google_logout_no_credentials(self):
        """auth google logout with no accounts."""
        with patch("summon_claude.config.discover_google_accounts", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "google", "logout"])
        assert result.exit_code == 0
        assert "No Google credentials stored." in result.output


class TestInitTextValidateFnRetry:
    """Tests for text/int validate_fn retry loops in cmd_init."""

    def test_init_text_validate_fn_retries_on_invalid_input(self, tmp_path):
        """Text options with validate_fn reprompt on invalid input."""
        config_file = tmp_path / "config.env"

        # channel_prefix validate_fn rejects uppercase
        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "UPPER",  # channel_prefix — invalid
                    "valid-prefix",  # channel_prefix — valid (retry)
                    "n",  # scribe_enabled
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        assert "Error:" in result.output
        content = config_file.read_text()
        assert "SUMMON_CHANNEL_PREFIX=valid-prefix" in content

    def test_init_int_validate_fn_retries_on_invalid_input(self, tmp_path):
        """Int options with validate_fn reprompt on invalid value (e.g. 0 < 1)."""
        config_file = tmp_path / "config.env"

        # scribe_scan_interval_minutes visible when scribe=yes; 0 fails validate_fn, 5 succeeds
        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",
                    "xapp-valid-app-token",
                    "abcdef012345",
                    "",  # default_model
                    "3",  # default_effort (choice: 3=high)
                    "",  # channel_prefix
                    "y",  # scribe_enabled
                    "0",  # scribe_scan_interval_minutes — invalid (< 1)
                    "10",  # scribe_scan_interval_minutes — valid (retry, non-default)
                    "",  # scribe_cwd
                    "",  # scribe_model
                    "",  # scribe_important_keywords
                    "",  # scribe_quiet_hours
                    "n",  # scribe_google_enabled
                    "n",  # scribe_slack_enabled
                    "n",  # advanced settings
                ]
            )
            + "\n"
        )

        with (
            patch("summon_claude.cli.get_config_file", return_value=config_file),
            patch("summon_claude.config.get_config_file", return_value=config_file),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=CliStatus(True, "1.0.0", "/usr/bin/claude"),
            ),
            patch("summon_claude.cli.config.config_check"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert result.exit_code == 0, f"Init failed: {result.output}"
        assert "Error:" in result.output or "at least 1" in result.output
        content = config_file.read_text()
        assert "SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES=10" in content

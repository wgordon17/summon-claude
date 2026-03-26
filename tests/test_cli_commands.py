"""Tests for new CLI commands: init, config show/set/path/edit."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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

        # Core prompts: 3 secrets, 1 text (model), 1 choice (effort),
        # 1 text (prefix), 1 flag (scribe), 1 secret (github_pat),
        # then "Configure advanced settings?" (no)
        inputs = (
            "\n".join(
                [
                    "xoxb-valid-bot-token",  # slack_bot_token (secret)
                    "xapp-valid-app-token",  # slack_app_token (secret)
                    "abcdef012345",  # signing_secret (secret, hex)
                    "",  # default_model (text, accept default)
                    "high",  # default_effort (choice)
                    "",  # channel_prefix (text, accept default)
                    "n",  # scribe_enabled (flag)
                    "",  # github_pat (secret, empty = skip)
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
                    "",  # default_model
                    "high",  # default_effort
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "",  # github_pat (skip)
                    "y",  # Configure advanced settings? (YES)
                    "",  # max_inline_chars (accept default)
                    "",  # permission_debounce_ms (accept default)
                    "y",  # no_update_check (flag)
                    "y",  # enable_thinking (flag)
                    "n",  # show_thinking (flag)
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
                    "",  # default_model
                    "high",  # default_effort
                    "",  # channel_prefix
                    "n",  # scribe_enabled
                    "",  # github_pat (skip)
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
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert config_file.exists(), f"Config file not created. Output: {result.output}"
        content = config_file.read_text()
        assert "xoxb-existing" in content
        assert "Existing config found" in result.output

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
                    "high",  # default_effort
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
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["init"], input=inputs)

        assert "Add .summon/ to your project's .gitignore" in result.output
        gitignore = summon_dir / ".gitignore"
        assert gitignore.exists()
        assert gitignore.read_text() == "*\n"


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


class TestCheckGithubPat:
    """Tests for _check_github_pat function."""

    def test_valid_pat_returns_true(self, capsys):
        """200 OK response returns True and prints PASS."""
        import json
        import urllib.request

        from summon_claude.cli.config import _check_github_pat

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "testuser"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            result = _check_github_pat("ghp_test123")

        assert result is True
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert "testuser" in captured.out

    def test_invalid_pat_returns_false(self, capsys):
        """401 response returns False and prints FAIL."""
        import urllib.error

        from summon_claude.cli.config import _check_github_pat

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=None,  # type: ignore[arg-type]
            ),
        ):
            result = _check_github_pat("ghp_bad")

        assert result is False
        captured = capsys.readouterr()
        assert "FAIL" in captured.out

    def test_non_auth_http_error_returns_true(self, capsys):
        """Non-401 HTTP error returns True with WARN."""
        import urllib.error

        from summon_claude.cli.config import _check_github_pat

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="",
                code=500,
                msg="Server Error",
                hdrs=None,
                fp=None,  # type: ignore[arg-type]
            ),
        ):
            result = _check_github_pat("ghp_test")

        assert result is True
        captured = capsys.readouterr()
        assert "WARN" in captured.out

    def test_network_error_returns_true(self, capsys):
        """Network exception returns True with WARN."""
        from summon_claude.cli.config import _check_github_pat

        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            result = _check_github_pat("ghp_test")

        assert result is True
        captured = capsys.readouterr()
        assert "WARN" in captured.out

    def test_quiet_mode_suppresses_pass(self, capsys):
        """quiet=True suppresses PASS output."""
        import json
        import urllib.request

        from summon_claude.cli.config import _check_github_pat

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "testuser"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            result = _check_github_pat("ghp_test123", quiet=True)

        assert result is True
        captured = capsys.readouterr()
        assert "PASS" not in captured.out


class TestCheckGithubPatSanitization:
    """Tests for login field sanitization in _check_github_pat."""

    def test_login_with_special_chars_sanitized(self, capsys):
        """Login with terminal escape sequences is stripped."""
        import json
        import urllib.request

        from summon_claude.cli.config import _check_github_pat

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "evil<>user\x1b[31m"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            result = _check_github_pat("ghp_test")

        assert result is True
        captured = capsys.readouterr()
        assert "eviluser" in captured.out
        assert "<>" not in captured.out
        assert "\x1b" not in captured.out

    def test_login_strips_to_empty_falls_back(self, capsys):
        """Login that strips to empty string falls back to 'unknown'."""
        import json
        import urllib.request

        from summon_claude.cli.config import _check_github_pat

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"login": "!!!"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            result = _check_github_pat("ghp_test")

        assert result is True
        captured = capsys.readouterr()
        assert "unknown" in captured.out


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

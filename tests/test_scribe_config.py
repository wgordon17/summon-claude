"""Tests for scribe agent configuration."""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from summon_claude.config import SummonConfig


def _make_config(**overrides) -> SummonConfig:
    """Create a SummonConfig with valid defaults, bypassing .env file loading."""
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "test-secret",
    }
    defaults.update(overrides)
    with patch.dict(os.environ, {}, clear=False):
        return SummonConfig.model_validate(defaults)


class TestScribeConfigDefaults:
    def test_scribe_disabled_by_default(self):
        cfg = _make_config()
        assert cfg.scribe_enabled is False

    def test_scan_interval_default(self):
        cfg = _make_config()
        assert cfg.scribe_scan_interval_minutes == 5

    def test_scribe_cwd_default_none(self):
        cfg = _make_config()
        assert cfg.scribe_cwd is None

    def test_scribe_model_default_none(self):
        cfg = _make_config()
        assert cfg.scribe_model is None

    def test_importance_keywords_default_empty(self):
        cfg = _make_config()
        assert cfg.scribe_importance_keywords == ""

    def test_quiet_hours_default_empty(self):
        cfg = _make_config()
        assert cfg.scribe_quiet_hours == ""

    def test_google_enabled_default(self):
        cfg = _make_config()
        assert cfg.scribe_google_enabled is True

    def test_google_services_default(self):
        cfg = _make_config()
        assert cfg.scribe_google_services == "gmail,calendar,drive"

    def test_slack_disabled_by_default(self):
        cfg = _make_config()
        assert cfg.scribe_slack_enabled is False

    def test_slack_browser_default(self):
        cfg = _make_config()
        assert cfg.scribe_slack_browser == "chrome"

    def test_monitored_channels_default_empty(self):
        cfg = _make_config()
        assert cfg.scribe_slack_monitored_channels == ""


class TestScribeConfigValidation:
    def test_valid_browser_chrome(self):
        cfg = _make_config(scribe_slack_browser="chrome")
        assert cfg.scribe_slack_browser == "chrome"

    def test_valid_browser_firefox(self):
        cfg = _make_config(scribe_slack_browser="firefox")
        assert cfg.scribe_slack_browser == "firefox"

    def test_valid_browser_webkit(self):
        cfg = _make_config(scribe_slack_browser="webkit")
        assert cfg.scribe_slack_browser == "webkit"

    def test_invalid_browser_raises(self):
        with pytest.raises(ValueError, match="SUMMON_SCRIBE_SLACK_BROWSER"):
            _make_config(scribe_slack_browser="opera")

    def test_valid_quiet_hours(self):
        cfg = _make_config(scribe_quiet_hours="22:00-07:00")
        assert cfg.scribe_quiet_hours == "22:00-07:00"

    def test_empty_quiet_hours_valid(self):
        cfg = _make_config(scribe_quiet_hours="")
        assert cfg.scribe_quiet_hours == ""

    def test_invalid_quiet_hours_format(self):
        with pytest.raises(ValueError, match="HH:MM-HH:MM"):
            _make_config(scribe_quiet_hours="10pm-7am")

    def test_invalid_quiet_hours_missing_dash(self):
        with pytest.raises(ValueError, match="HH:MM-HH:MM"):
            _make_config(scribe_quiet_hours="22:00")

    def test_scribe_enabled_no_collectors_warns(self, caplog):
        cfg = _make_config(
            scribe_enabled=True,
            scribe_google_enabled=False,
            scribe_slack_enabled=False,
        )
        with caplog.at_level(logging.WARNING):
            cfg.validate()
        assert "no data collectors configured" in caplog.text

    def test_scribe_enabled_with_google_no_warning(self, caplog):
        cfg = _make_config(scribe_enabled=True, scribe_google_enabled=True)
        with caplog.at_level(logging.WARNING):
            cfg.validate()
        assert "no data collectors configured" not in caplog.text

    def test_scribe_enabled_with_slack_no_warning(self, caplog):
        cfg = _make_config(
            scribe_enabled=True,
            scribe_google_enabled=False,
            scribe_slack_enabled=True,
        )
        with caplog.at_level(logging.WARNING):
            cfg.validate()
        assert "no data collectors configured" not in caplog.text


class TestScribeConfigSettableKeys:
    """Verify all scribe keys are in _SETTABLE_KEYS."""

    def test_scribe_keys_in_settable(self):
        from summon_claude.cli.config import _SETTABLE_KEYS

        expected_scribe_keys = {
            "SUMMON_SCRIBE_ENABLED",
            "SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES",
            "SUMMON_SCRIBE_CWD",
            "SUMMON_SCRIBE_MODEL",
            "SUMMON_SCRIBE_IMPORTANCE_KEYWORDS",
            "SUMMON_SCRIBE_QUIET_HOURS",
            "SUMMON_SCRIBE_GOOGLE_ENABLED",
            "SUMMON_SCRIBE_GOOGLE_SERVICES",
            "SUMMON_SCRIBE_SLACK_ENABLED",
            "SUMMON_SCRIBE_SLACK_BROWSER",
            "SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS",
        }
        assert expected_scribe_keys.issubset(_SETTABLE_KEYS)


class TestGoogleWorkspaceMCP:
    """Tests for Google Workspace MCP config helper."""

    def test_build_google_workspace_mcp_default_services(self):
        from summon_claude.sessions.session import _build_google_workspace_mcp

        result = _build_google_workspace_mcp("gmail,calendar,drive")
        assert result["command"] == sys.executable
        assert "-m" in result["args"]
        assert "workspace_mcp" in result["args"]
        assert "gmail,calendar,drive" in result["args"]
        assert "--tool-tier" in result["args"]
        assert "core" in result["args"]

    def test_build_google_workspace_mcp_custom_services(self):
        from summon_claude.sessions.session import _build_google_workspace_mcp

        result = _build_google_workspace_mcp("gmail")
        assert "gmail" in result["args"]


class TestGoogleAuthCLI:
    """Tests for google-auth and google-status CLI commands."""

    def test_google_auth_missing_package(self):
        from summon_claude.cli import cli

        runner = CliRunner()
        with patch.dict(sys.modules, {"workspace_mcp": None, "workspace_mcp.auth": None}):
            result = runner.invoke(cli, ["config", "google-auth"])
        # Should fail with import error message
        assert result.exit_code != 0 or "google" in result.output.lower()

    def test_google_status_missing_package(self):
        from summon_claude.cli import cli

        runner = CliRunner()
        with patch.dict(sys.modules, {"workspace_mcp": None, "workspace_mcp.auth": None}):
            result = runner.invoke(cli, ["config", "google-status"])
        assert result.exit_code != 0 or "google" in result.output.lower()

    def test_pyproject_has_google_optional_dep(self):
        """Verify pyproject.toml declares the google optional dependency."""
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text())
        optional_deps = data.get("project", {}).get("optional-dependencies", {})
        assert "google" in optional_deps
        google_deps = optional_deps["google"]
        assert any("workspace-mcp" in dep for dep in google_deps)

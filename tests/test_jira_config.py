"""Tests for Jira-related config properties in SummonConfig."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from summon_claude.config import SummonConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**kwargs) -> SummonConfig:
    """Build a minimal valid SummonConfig for testing."""
    defaults = {
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "slack_signing_secret": "abcd1234",
    }
    defaults.update(kwargs)
    return SummonConfig(**defaults)


# ---------------------------------------------------------------------------
# jira_enabled
# ---------------------------------------------------------------------------


class TestJiraEnabled:
    def test_returns_true_when_credentials_exist(self):
        config = _make_config()
        with patch("summon_claude.jira_auth.jira_credentials_exist", return_value=True):
            assert config.jira_enabled is True

    def test_returns_false_when_credentials_absent(self):
        config = _make_config()
        with patch("summon_claude.jira_auth.jira_credentials_exist", return_value=False):
            assert config.jira_enabled is False

    def test_no_network_io(self, tmp_path):
        """jira_enabled only does a stat check — token file absent means False."""
        config = _make_config()
        # Patch get_jira_token_path to point to a non-existent path
        with patch(
            "summon_claude.jira_auth.get_jira_token_path",
            return_value=tmp_path / "nonexistent" / "token.json",
        ):
            assert config.jira_enabled is False

    def test_true_when_token_file_present_fixture(self, tmp_path):
        config = _make_config()
        token_path = tmp_path / "token.json"
        token_path.write_text('{"access_token": "test"}')
        with patch("summon_claude.jira_auth.get_jira_token_path", return_value=token_path):
            assert config.jira_enabled is True


# ---------------------------------------------------------------------------
# jira_mcp_config
# ---------------------------------------------------------------------------


class TestJiraMcpConfig:
    def test_returns_none_when_no_token(self):
        config = _make_config()
        with patch("summon_claude.jira_auth.load_jira_token", return_value=None):
            assert config.jira_mcp_config() is None

    def test_returns_none_when_token_has_no_access_token(self):
        config = _make_config()
        token = {"refresh_token": "rtoken", "expires_at": 9999999999}
        with patch("summon_claude.jira_auth.load_jira_token", return_value=token):
            assert config.jira_mcp_config() is None

    def test_returns_http_mcp_config(self):
        config = _make_config()
        token = {
            "access_token": "my-access-token",
            "refresh_token": "my-refresh-token",
            "client_secret": "my-client-secret",
            "expires_at": 9999999999,
        }
        with patch("summon_claude.jira_auth.load_jira_token", return_value=token):
            result = config.jira_mcp_config()

        assert result is not None
        assert result["type"] == "http"
        assert result["url"] == "https://mcp.atlassian.com/v1/mcp"
        assert result["headers"]["Authorization"] == "Bearer my-access-token"

    def test_access_token_only_in_mcp_config(self):
        """SC-03: refresh_token and client_secret must NOT appear in MCP config."""
        config = _make_config()
        token = {
            "access_token": "atoken",
            "refresh_token": "rtoken",
            "client_secret": "csecret",
            "client_id": "cid",
            "expires_at": 9999999999,
        }
        with patch("summon_claude.jira_auth.load_jira_token", return_value=token):
            result = config.jira_mcp_config()

        assert result is not None
        result_str = str(result)
        assert "rtoken" not in result_str
        assert "csecret" not in result_str
        assert "cid" not in result_str
        assert "atoken" in result_str

    def test_mcp_url_is_atlassian(self):
        config = _make_config()
        token = {"access_token": "tok", "expires_at": 9999999999}
        with patch("summon_claude.jira_auth.load_jira_token", return_value=token):
            result = config.jira_mcp_config()
        assert result["url"].startswith("https://mcp.atlassian.com")

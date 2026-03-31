"""Tests for Jira CLI auth wrapper functions (jira_login, jira_logout, jira_status)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.cli.auth import auth_jira_login, auth_jira_logout, auth_jira_status

# ---------------------------------------------------------------------------
# jira_login
# ---------------------------------------------------------------------------


class TestJiraLogin:
    def test_jira_login_happy_path(self):
        """Login succeeds, site found → saves token with cloud_id, prints success."""
        token_data = {"access_token": "atoken", "refresh_token": "rtoken"}
        sites = [{"id": "cloud-abc", "name": "My Jira", "url": "https://myjira.atlassian.net"}]

        # jira_login imports lazily — patch at source module so all importers see the mock
        with (
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
                return_value=token_data,
            ),
            patch(
                "summon_claude.jira_auth.discover_cloud_sites",
                new_callable=AsyncMock,
                return_value=sites,
            ),
            patch("summon_claude.jira_auth.save_jira_token") as mock_save,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "login"])

        assert result.exit_code == 0
        assert "authenticated" in result.output.lower() or "My Jira" in result.output
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert saved["cloud_id"] == "cloud-abc"
        assert saved["cloud_name"] == "My Jira"

    def test_jira_login_no_sites_prompts_for_org(self):
        """Login with no auto-discovered sites → prompts for org name, saves with cloud_id."""
        token_data = {"access_token": "atoken"}

        with (
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
                return_value=token_data,
            ),
            patch(
                "summon_claude.jira_auth.discover_cloud_sites",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("summon_claude.jira_auth.save_jira_token") as mock_save,
        ):
            runner = CliRunner()
            # User enters just the org name — auto-appends .atlassian.net
            result = runner.invoke(cli, ["auth", "jira", "login"], input="myorg\n")

        assert result.exit_code == 0
        mock_save.assert_called()
        saved = mock_save.call_args[0][0]
        assert saved["cloud_id"] == "myorg.atlassian.net"
        assert saved["cloud_name"] == "myorg"

    def test_jira_login_site_flag_skips_discovery(self):
        """--site flag skips REST API discovery and interactive prompt."""
        token_data = {"access_token": "atoken"}

        with (
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
                return_value=token_data,
            ),
            patch("summon_claude.jira_auth.save_jira_token") as mock_save,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "login", "--site", "redhat"])

        assert result.exit_code == 0
        mock_save.assert_called()
        saved = mock_save.call_args[0][0]
        assert saved["cloud_id"] == "redhat.atlassian.net"
        assert saved["cloud_name"] == "redhat"

    def test_jira_login_timeout_exits_nonzero(self):
        """If start_auth_flow raises TimeoutError, CLI exits with code 1."""
        with patch(
            "summon_claude.jira_auth.start_auth_flow",
            new_callable=AsyncMock,
            side_effect=TimeoutError("flow timed out"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "login"])

        assert result.exit_code == 1
        assert "timed out" in result.output.lower()

    def test_jira_login_runtime_error_exits_nonzero(self):
        """If start_auth_flow raises RuntimeError, CLI exits with code 1."""
        with patch(
            "summon_claude.jira_auth.start_auth_flow",
            new_callable=AsyncMock,
            side_effect=RuntimeError("authorization denied"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "login"])

        assert result.exit_code == 1
        assert "authentication failed" in result.output.lower()

    def test_jira_login_direct_function_happy_path(self):
        """Direct call to jira_login() echoes success message."""
        token_data = {"access_token": "at"}
        sites = [{"id": "cid", "name": "SiteName", "url": "https://x.atlassian.net"}]

        with (
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
                return_value=token_data,
            ),
            patch(
                "summon_claude.jira_auth.discover_cloud_sites",
                new_callable=AsyncMock,
                return_value=sites,
            ),
            patch("summon_claude.jira_auth.save_jira_token"),
        ):
            # Should not raise
            auth_jira_login.callback(site=None)


# ---------------------------------------------------------------------------
# jira_logout
# ---------------------------------------------------------------------------


class TestJiraLogout:
    def test_jira_logout_removes_credentials(self):
        """When credentials exist, logout() is called and removal message shown."""
        with (
            patch(
                "summon_claude.jira_auth.jira_credentials_exist",
                return_value=True,
            ),
            patch("summon_claude.jira_auth.logout") as mock_logout,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "logout"])

        assert result.exit_code == 0
        mock_logout.assert_called_once()
        assert "removed" in result.output.lower()

    def test_jira_logout_no_credentials_no_op(self):
        """When no credentials exist, logout() is NOT called and informative message shown."""
        with (
            patch(
                "summon_claude.jira_auth.jira_credentials_exist",
                return_value=False,
            ),
            patch("summon_claude.jira_auth.logout") as mock_logout,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "logout"])

        assert result.exit_code == 0
        mock_logout.assert_not_called()
        assert "no jira credentials" in result.output.lower()

    def test_jira_logout_direct_function_credentials_present(self):
        """Direct call to jira_logout() when credentials exist calls logout()."""
        with (
            patch(
                "summon_claude.jira_auth.jira_credentials_exist",
                return_value=True,
            ),
            patch("summon_claude.jira_auth.logout") as mock_logout,
        ):
            auth_jira_logout.callback()

        mock_logout.assert_called_once()


# ---------------------------------------------------------------------------
# jira_status
# ---------------------------------------------------------------------------


class TestJiraStatus:
    def test_jira_status_authenticated(self):
        """When _check_jira_status returns None, output says 'authenticated'."""
        with patch(
            "summon_claude.jira_auth.check_jira_status",
            return_value=None,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "status"])

        assert result.exit_code == 0
        assert "authenticated" in result.output.lower()

    def test_jira_status_error(self):
        """When _check_jira_status returns an error string, it is displayed."""
        error_msg = "No Jira credentials found. Run: summon auth jira login"
        with patch(
            "summon_claude.jira_auth.check_jira_status",
            return_value=error_msg,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "status"])

        assert result.exit_code == 0
        assert error_msg in result.output

    def test_jira_status_missing_cloud_id_error(self):
        """Partial credentials (no cloud_id) produce an error message."""
        error_msg = "Jira credentials found but no cloud_id is configured."
        with patch(
            "summon_claude.jira_auth.check_jira_status",
            return_value=error_msg,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "status"])

        assert result.exit_code == 0
        assert error_msg in result.output

    def test_jira_status_direct_function_authenticated(self):
        """Direct call to jira_status() does not raise when authenticated."""
        with patch(
            "summon_claude.jira_auth.check_jira_status",
            return_value=None,
        ):
            auth_jira_status.callback()  # Should not raise

    def test_jira_status_direct_function_error(self):
        """Direct call to jira_status() does not raise on error status."""
        with patch(
            "summon_claude.jira_auth.check_jira_status",
            return_value="some error",
        ):
            auth_jira_status.callback()  # Should not raise

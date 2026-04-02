"""Tests for Jira CLI auth wrapper functions (jira_login, jira_logout, jira_status)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.cli.auth import (
    _extract_site_host,
    _normalize_site,
    auth_jira_login,
    auth_jira_logout,
    auth_jira_status,
)

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
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
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
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
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

    def test_jira_login_site_flag_matches_discovered_site(self):
        """--site flag calls discover_cloud_sites, matches by hostname, saves UUID cloud_id."""
        token_data = {"access_token": "atoken"}
        sites = [
            {
                "id": "uuid-redhat-123",
                "name": "Red Hat",
                "url": "https://redhat.atlassian.net",
            }
        ]

        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
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
            result = runner.invoke(cli, ["auth", "jira", "login", "--site", "redhat"])

        assert result.exit_code == 0
        mock_save.assert_called()
        saved = mock_save.call_args[0][0]
        # UUID from discovered site, not the bare hostname
        assert saved["cloud_id"] == "uuid-redhat-123"
        assert saved["cloud_name"] == "Red Hat"

    def test_jira_login_site_flag_no_match_stores_hostname_with_warning(self):
        """--site flag with no matching discovered site stores hostname and emits warning."""
        token_data = {"access_token": "atoken"}
        sites = [
            {
                "id": "uuid-other-456",
                "name": "OtherOrg",
                "url": "https://otherorg.atlassian.net",
            }
        ]

        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
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
            result = runner.invoke(cli, ["auth", "jira", "login", "--site", "redhat"])

        assert result.exit_code == 0
        mock_save.assert_called()
        saved = mock_save.call_args[0][0]
        # Falls back to hostname when no site matches
        assert saved["cloud_id"] == "redhat.atlassian.net"
        assert saved["cloud_name"] == "redhat"
        # Warning should have been emitted to stderr
        assert "warning" in result.output.lower() or "did not match" in result.output.lower()

    def test_jira_login_site_flag_discovery_unavailable_stores_hostname(self):
        """--site with discovery returning empty stores hostname with warning."""
        token_data = {"access_token": "atoken"}

        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
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
            result = runner.invoke(cli, ["auth", "jira", "login", "--site", "myorg"])

        assert result.exit_code == 0
        saved = mock_save.call_args[0][0]
        assert saved["cloud_id"] == "myorg.atlassian.net"
        output = result.output.lower()
        assert "discovery unavailable" in output or "warning" in output

    def test_jira_login_timeout_exits_nonzero(self):
        """If start_auth_flow raises TimeoutError, CLI exits with code 1."""
        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
                side_effect=TimeoutError("flow timed out"),
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "login"])

        assert result.exit_code == 1
        assert "timed out" in result.output.lower()

    def test_jira_login_runtime_error_exits_nonzero(self):
        """If start_auth_flow raises RuntimeError, CLI exits with code 1."""
        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
                side_effect=RuntimeError("authorization denied"),
            ),
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
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            # Should not raise
            auth_jira_login.callback(site=None)

    def test_login_refresh_shortcut(self):
        """When try_refresh_only returns True, browser flow is not called."""
        fresh_token = {
            "access_token": "refreshed-token",
            "cloud_name": "MyOrg",
        }

        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "summon_claude.jira_auth.load_jira_token",
                return_value=fresh_token,
            ),
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
            ) as mock_browser,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "login"])

        assert result.exit_code == 0
        mock_browser.assert_not_called()
        assert "refreshed" in result.output.lower()

    def test_login_refresh_fails_falls_through(self):
        """When try_refresh_only returns False, browser flow is called."""
        token_data = {"access_token": "atoken", "refresh_token": "rtoken"}
        sites = [{"id": "cloud-abc", "name": "My Jira", "url": "https://myjira.atlassian.net"}]

        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "summon_claude.jira_auth.start_auth_flow",
                new_callable=AsyncMock,
                return_value=token_data,
            ) as mock_browser,
            patch(
                "summon_claude.jira_auth.discover_cloud_sites",
                new_callable=AsyncMock,
                return_value=sites,
            ),
            patch("summon_claude.jira_auth.save_jira_token"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["auth", "jira", "login"])

        assert result.exit_code == 0
        mock_browser.assert_called_once()


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


# ---------------------------------------------------------------------------
# _normalize_site
# ---------------------------------------------------------------------------


class TestExtractSiteHost:
    """Tests for _extract_site_host() — safe hostname extraction from API URLs."""

    def test_https_url(self):
        assert _extract_site_host("https://myorg.atlassian.net") == "myorg.atlassian.net"

    def test_url_with_path(self):
        assert _extract_site_host("https://myorg.atlassian.net/wiki") == "myorg.atlassian.net"

    def test_url_with_query(self):
        assert _extract_site_host("https://myorg.atlassian.net?q=1") == "myorg.atlassian.net"

    def test_empty_string(self):
        assert _extract_site_host("") == ""

    def test_no_scheme(self):
        # urlparse treats schemeless URLs as path-only — hostname is None
        assert _extract_site_host("myorg.atlassian.net") == ""

    def test_none_like_garbage(self):
        assert _extract_site_host("not-a-url") == ""


class TestNormalizeSite:
    """Tests for _normalize_site() URL normalization."""

    def test_bare_org_name(self):
        assert _normalize_site("myorg") == "myorg.atlassian.net"

    def test_already_qualified_hostname(self):
        assert _normalize_site("myorg.atlassian.net") == "myorg.atlassian.net"

    def test_https_url(self):
        assert _normalize_site("https://myorg.atlassian.net") == "myorg.atlassian.net"

    def test_http_url(self):
        assert _normalize_site("http://myorg.atlassian.net") == "myorg.atlassian.net"

    def test_trailing_slash(self):
        assert _normalize_site("https://myorg.atlassian.net/") == "myorg.atlassian.net"

    def test_whitespace_stripped(self):
        assert _normalize_site("  myorg  ") == "myorg.atlassian.net"

    def test_url_with_path_strips_path(self):
        assert _normalize_site("https://myorg.atlassian.net/jira") == "myorg.atlassian.net"

    def test_url_with_deep_path_strips_path(self):
        assert _normalize_site("https://myorg.atlassian.net/wiki/spaces") == "myorg.atlassian.net"

    def test_rejects_path_traversal(self):
        with pytest.raises(click.BadParameter, match="does not look like a valid hostname"):
            _normalize_site("../../etc/passwd")

    def test_rejects_spaces_in_hostname(self):
        with pytest.raises(click.BadParameter, match="does not look like a valid hostname"):
            _normalize_site("my org")

    def test_rejects_angle_brackets(self):
        with pytest.raises(click.BadParameter, match="does not look like a valid hostname"):
            _normalize_site("<script>alert(1)</script>")

    def test_rejects_empty_after_strip(self):
        with pytest.raises(click.BadParameter, match="does not look like a valid hostname"):
            _normalize_site("   ")


# ---------------------------------------------------------------------------
# TestJiraLogin — multi-site selection
# ---------------------------------------------------------------------------


class TestJiraLoginMultiSite:
    """Tests for multi-site discovery and selection in jira_login."""

    def test_jira_login_multiple_sites_selects_second(self):
        """Multi-site discovery prompts for selection; user picks site 2."""
        token_data = {"access_token": "atoken"}
        sites = [
            {"id": "site-1-id", "name": "OrgOne", "url": "https://orgone.atlassian.net"},
            {"id": "site-2-id", "name": "OrgTwo", "url": "https://orgtwo.atlassian.net"},
        ]

        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
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
            # User selects site 2 at the prompt
            result = runner.invoke(cli, ["auth", "jira", "login"], input="2\n")

        assert result.exit_code == 0
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert saved["cloud_id"] == "site-2-id"
        assert saved["cloud_name"] == "OrgTwo"

    def test_jira_login_multiple_sites_selects_first_by_default(self):
        """Multi-site selection defaults to 1 when user presses Enter."""
        token_data = {"access_token": "atoken"}
        sites = [
            {"id": "site-1-id", "name": "OrgOne", "url": "https://orgone.atlassian.net"},
            {"id": "site-2-id", "name": "OrgTwo", "url": "https://orgtwo.atlassian.net"},
        ]

        with (
            patch(
                "summon_claude.jira_auth.try_refresh_only",
                new_callable=AsyncMock,
                return_value=False,
            ),
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
            # User presses Enter (accepts default=1)
            result = runner.invoke(cli, ["auth", "jira", "login"], input="\n")

        assert result.exit_code == 0
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][0]
        assert saved["cloud_id"] == "site-1-id"
        assert saved["cloud_name"] == "OrgOne"


# ---------------------------------------------------------------------------
# project add --jql
# ---------------------------------------------------------------------------


class TestProjectAddJQL:
    """Tests for project add --jql CLI path."""

    def test_add_project_with_jql(self):
        """project add NAME DIR --jql 'filter' passes jira_jql to async_project_add."""
        fake_project_id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"

        with patch(
            "summon_claude.cli.async_project_add",
            new_callable=AsyncMock,
            return_value=fake_project_id,
        ) as mock_add:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["project", "add", "myproj", ".", "--jql", "project = FOO"],
            )

        assert result.exit_code == 0, result.output
        mock_add.assert_called_once_with("myproj", ".", jira_jql="project = FOO")

    def test_add_project_without_jql_passes_none(self):
        """project add NAME DIR without --jql passes jira_jql=None."""
        fake_project_id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"

        with patch(
            "summon_claude.cli.async_project_add",
            new_callable=AsyncMock,
            return_value=fake_project_id,
        ) as mock_add:
            runner = CliRunner()
            result = runner.invoke(cli, ["project", "add", "myproj", "."])

        assert result.exit_code == 0, result.output
        mock_add.assert_called_once_with("myproj", ".", jira_jql=None)

    def test_add_project_rejects_jql_too_long(self):
        """project add --jql with >500 chars is rejected."""
        long_jql = "x" * 501
        runner = CliRunner()
        result = runner.invoke(cli, ["project", "add", "myproj", ".", "--jql", long_jql])
        assert result.exit_code != 0
        assert "too long" in result.output.lower()


# ---------------------------------------------------------------------------
# project update
# ---------------------------------------------------------------------------


class TestProjectUpdateCLI:
    """Tests for the project update CLI command."""

    def test_update_jql_set(self):
        """project update NAME --jql 'filter' calls async_project_update with the JQL."""
        with patch(
            "summon_claude.cli.async_project_update",
            new_callable=AsyncMock,
        ) as mock_update:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["project", "update", "myproj", "--jql", "project = FOO"],
            )

        assert result.exit_code == 0, result.output
        mock_update.assert_called_once_with("myproj", jira_jql="project = FOO")
        assert "set" in result.output.lower()

    def test_update_jql_clear(self):
        """project update NAME --jql '' clears the JQL filter (passes None to update)."""
        with patch(
            "summon_claude.cli.async_project_update",
            new_callable=AsyncMock,
        ) as mock_update:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["project", "update", "myproj", "--jql", ""],
            )

        assert result.exit_code == 0, result.output
        mock_update.assert_called_once_with("myproj", jira_jql=None)
        assert "cleared" in result.output.lower()

    def test_update_no_fields_error(self):
        """project update NAME without --jql raises UsageError (non-zero exit)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["project", "update", "myproj"])

        assert result.exit_code != 0
        assert "no fields" in result.output.lower() or "No fields" in result.output

    def test_update_rejects_jql_too_long(self):
        """project update --jql with >500 chars is rejected."""
        long_jql = "x" * 501
        runner = CliRunner()
        result = runner.invoke(cli, ["project", "update", "myproj", "--jql", long_jql])
        assert result.exit_code != 0
        assert "too long" in result.output.lower()

    def test_update_project_not_found(self):
        """project update with nonexistent project returns error."""
        with patch(
            "summon_claude.cli.async_project_update",
            new_callable=AsyncMock,
            side_effect=click.ClickException("No project found: 'nosuch'"),
        ):
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["project", "update", "nosuch", "--jql", "project = X"],
            )

        assert result.exit_code != 0
        assert "no project found" in result.output.lower()


# ---------------------------------------------------------------------------
# project list — JQL display
# ---------------------------------------------------------------------------


class TestProjectListJQLDisplay:
    """Verify that project list shows JQL filter when set."""

    def test_list_shows_jql_when_set(self):
        projects = [
            {
                "name": "myproj",
                "directory": "/tmp/myproj",
                "pm_running": False,
                "project_id": "abc123",
                "channel_prefix": None,
                "jira_jql": "project = MYPROJ AND status != Done",
            }
        ]
        with patch(
            "summon_claude.cli.async_project_list",
            new_callable=AsyncMock,
            return_value=projects,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["project", "list"])

        assert result.exit_code == 0
        assert "JQL: project = MYPROJ AND status != Done" in result.output

    def test_list_omits_jql_when_not_set(self):
        projects = [
            {
                "name": "myproj",
                "directory": "/tmp/myproj",
                "pm_running": False,
                "project_id": "abc123",
                "channel_prefix": None,
            }
        ]
        with patch(
            "summon_claude.cli.async_project_list",
            new_callable=AsyncMock,
            return_value=projects,
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["project", "list"])

        assert result.exit_code == 0
        assert "JQL" not in result.output

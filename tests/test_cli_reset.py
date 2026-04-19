"""Tests for the 'summon reset' CLI subgroup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from summon_claude.cli import cli
from summon_claude.config import (
    get_browser_auth_dir,
    get_config_dir,
    get_google_credentials_dir,
    get_workspace_config_path,
)


class TestResetBare:
    def test_reset_bare_shows_help(self):
        """'summon reset' with no subcommand shows usage."""
        runner = CliRunner()
        result = runner.invoke(cli, ["reset"])
        assert "data" in result.output
        assert "config" in result.output


class TestResetData:
    def test_reset_data_deletes_data_dir(self, tmp_path):
        """'reset data' should delete the data directory after confirmation."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "registry.db").touch()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["reset", "data"], input="y\n")
        assert result.exit_code == 0
        assert not data_dir.exists()
        assert "summon start" in result.output

    def test_reset_data_aborts_on_no(self, tmp_path):
        """'reset data' should abort when user declines."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["reset", "data"], input="n\n")
        assert result.exit_code != 0
        assert data_dir.exists()
        assert "Continue?" in result.output

    def test_reset_data_refuses_if_sessions_running(self):
        """'reset data' should refuse when daemon has active sessions."""
        runner = CliRunner()
        mock_sessions = [{"session_id": "abc", "session_name": "test-sess"}]
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "summon stop --all" in result.output
        assert "summon project down" not in result.output

    def test_reset_data_refuses_project_sessions_only(self):
        """'reset data' should show only project guidance when only PM sessions exist."""
        runner = CliRunner()
        mock_sessions = [{"session_id": "abc", "session_name": "pm-agent"}]
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "summon project down" in result.output
        assert "summon stop --all" not in result.output

    def test_reset_data_classifies_project_children_correctly(self):
        """Project child sessions (with project_id but no -pm-) should be classified as project."""
        runner = CliRunner()
        mock_sessions = [
            {"session_id": "abc", "session_name": "myproj-abc123", "project_id": "proj1"},
        ]
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "summon project down" in result.output
        assert "summon stop --all" not in result.output

    def test_reset_data_refuses_mixed_sessions(self):
        """'reset data' should show both messages when ad-hoc and project sessions exist."""
        runner = CliRunner()
        mock_sessions = [
            {"session_id": "abc", "session_name": "test-sess"},
            {"session_id": "def", "session_name": "pm-agent"},
        ]
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "summon stop --all" in result.output
        assert "summon project down" in result.output

    def test_reset_data_refuses_idle_daemon(self):
        """'reset data' should refuse when daemon is running with no sessions."""
        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "daemon is still running" in result.output

    def test_reset_data_rmtree_failure(self, tmp_path):
        """'reset data' should show friendly error if rmtree fails."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
            patch(
                "summon_claude.cli.reset.shutil.rmtree",
                side_effect=OSError("Permission denied"),
            ),
        ):
            result = runner.invoke(cli, ["reset", "data"], input="y\n")
        assert result.exit_code != 0
        assert "Failed to delete" in result.output

    def test_reset_data_refuses_on_ipc_failure(self):
        """'reset data' should refuse when daemon IPC fails."""
        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                side_effect=ConnectionRefusedError,
            ),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "Could not determine session status" in result.output

    def test_reset_data_noop_if_dir_missing(self, tmp_path):
        """'reset data' should no-op when data directory does not exist."""
        missing_dir = tmp_path / "nonexistent"

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=missing_dir),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code == 0
        assert "Nothing to reset" in result.output

    def test_reset_data_refuses_symlink(self, tmp_path):
        """'reset data' should refuse when data directory is a symlink — before prompting."""
        target = tmp_path / "real"
        target.mkdir()
        symlink = tmp_path / "data"
        symlink.symlink_to(target)

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=symlink),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "symlink" in result.output
        assert "--force" in result.output
        assert "Continue?" not in result.output
        assert target.exists()

    def test_reset_data_force_bypasses_symlink(self, tmp_path):
        """'reset data --force' should bypass symlink check but still confirm."""
        target = tmp_path / "real"
        target.mkdir()
        (target / "registry.db").touch()
        symlink = tmp_path / "data"
        symlink.symlink_to(target)

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=symlink),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["reset", "data", "--force"], input="y\n")
        assert result.exit_code == 0
        assert "Safety checks bypassed" in result.output
        assert "Are you SURE?" in result.output
        assert not target.exists()

    def test_reset_data_refuses_non_interactive(self, tmp_path):
        """'reset data' should refuse in non-interactive mode."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
        ):
            result = runner.invoke(cli, ["--no-interactive", "reset", "data"])
        assert result.exit_code != 0
        assert "interactive mode" in result.output
        assert data_dir.exists()

    def test_reset_data_force_still_refuses_non_interactive(self, tmp_path):
        """'reset data --force' should still refuse in non-interactive mode."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
        ):
            result = runner.invoke(cli, ["--no-interactive", "reset", "data", "--force"])
        assert result.exit_code != 0
        assert "interactive mode" in result.output
        assert data_dir.exists()

    def test_reset_data_refuses_path_outside_home(self, tmp_path):
        """'reset data' should refuse when directory resolves outside $HOME — before prompting."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path / "fakehome"),
        ):
            result = runner.invoke(cli, ["reset", "data"])
        assert result.exit_code != 0
        assert "outside home" in result.output
        assert "--force" in result.output
        assert "Continue?" not in result.output
        assert data_dir.exists()

    def test_reset_data_force_bypasses_outside_home(self, tmp_path):
        """'reset data --force' should bypass outside-home check but still confirm."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "registry.db").touch()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=data_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path / "fakehome"),
        ):
            result = runner.invoke(cli, ["reset", "data", "--force"], input="y\n")
        assert result.exit_code == 0
        assert "Safety checks bypassed" in result.output
        assert "Are you SURE?" in result.output
        assert not data_dir.exists()

    def test_reset_data_force_refuses_shallow_path(self):
        """'reset data --force' should still refuse paths with < 3 components (e.g. /etc)."""
        mock_target = MagicMock()
        mock_target.exists.return_value = True
        mock_target.is_symlink.return_value = True
        mock_target.resolve.return_value = Path("/etc")

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=mock_target),
            patch("summon_claude.cli.reset.Path.home", return_value=Path("/Users/fake")),
        ):
            result = runner.invoke(cli, ["reset", "data", "--force"])
        assert result.exit_code != 0
        assert "too shallow" in result.output

    def test_reset_data_force_aborts_on_no(self, tmp_path):
        """'reset data --force' should abort when user declines 'Are you SURE?'."""
        target = tmp_path / "real"
        target.mkdir()
        symlink = tmp_path / "data"
        symlink.symlink_to(target)

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_data_dir", return_value=symlink),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["reset", "data", "--force"], input="n\n")
        assert result.exit_code != 0
        assert target.exists()
        assert "Are you SURE?" in result.output

    def test_reset_data_force_still_refuses_running_sessions(self):
        """'reset data --force' should still refuse when sessions are running."""
        runner = CliRunner()
        mock_sessions = [{"session_id": "abc", "session_name": "test-sess"}]
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
        ):
            result = runner.invoke(cli, ["reset", "data", "--force"])
        assert result.exit_code != 0
        assert "summon stop --all" in result.output


class TestResetConfig:
    def test_reset_config_deletes_config_dir(self, tmp_path):
        """'reset config' should delete the config directory after confirmation."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").touch()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_config_dir", return_value=config_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["reset", "config"], input="y\n")
        assert result.exit_code == 0
        assert not config_dir.exists()
        assert "summon hooks uninstall" in result.output
        assert "summon init" in result.output

    def test_reset_config_aborts_on_no(self, tmp_path):
        """'reset config' should abort when user declines."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_config_dir", return_value=config_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["reset", "config"], input="n\n")
        assert result.exit_code != 0
        assert config_dir.exists()
        assert "Continue?" in result.output

    def test_reset_config_refuses_adhoc_sessions(self):
        """'reset config' should refuse when daemon has ad-hoc sessions."""
        runner = CliRunner()
        mock_sessions = [{"session_id": "abc", "session_name": "test-sess"}]
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
        ):
            result = runner.invoke(cli, ["reset", "config"])
        assert result.exit_code != 0
        assert "summon stop --all" in result.output
        assert "summon project down" not in result.output

    def test_reset_config_refuses_project_sessions(self):
        """'reset config' should refuse when daemon has project sessions."""
        runner = CliRunner()
        mock_sessions = [{"session_id": "abc", "session_name": "pm-agent"}]
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=mock_sessions,
            ),
        ):
            result = runner.invoke(cli, ["reset", "config"])
        assert result.exit_code != 0
        assert "summon project down" in result.output

    def test_reset_config_refuses_on_ipc_failure(self):
        """'reset config' should refuse when daemon IPC fails."""
        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                side_effect=OSError("Connection refused"),
            ),
        ):
            result = runner.invoke(cli, ["reset", "config"])
        assert result.exit_code != 0
        assert "Could not determine session status" in result.output

    def test_reset_config_refuses_idle_daemon(self):
        """'reset config' should refuse when daemon is running with no sessions."""
        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=True),
            patch(
                "summon_claude.cli.daemon_client.list_sessions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = runner.invoke(cli, ["reset", "config"])
        assert result.exit_code != 0
        assert "daemon is still running" in result.output

    def test_reset_config_noop_if_dir_missing(self, tmp_path):
        """'reset config' should no-op when config directory does not exist."""
        missing_dir = tmp_path / "nonexistent"

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_config_dir", return_value=missing_dir),
        ):
            result = runner.invoke(cli, ["reset", "config"])
        assert result.exit_code == 0
        assert "Nothing to reset" in result.output

    def test_reset_config_refuses_symlink(self, tmp_path):
        """'reset config' should refuse when config directory is a symlink — before prompting."""
        target = tmp_path / "real"
        target.mkdir()
        symlink = tmp_path / "config"
        symlink.symlink_to(target)

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_config_dir", return_value=symlink),
        ):
            result = runner.invoke(cli, ["reset", "config"])
        assert result.exit_code != 0
        assert "symlink" in result.output
        assert "--force" in result.output
        assert "Continue?" not in result.output
        assert target.exists()

    def test_reset_config_force_bypasses_symlink(self, tmp_path):
        """'reset config --force' should bypass symlink check but still confirm."""
        target = tmp_path / "real"
        target.mkdir()
        (target / "config.env").touch()
        symlink = tmp_path / "config"
        symlink.symlink_to(target)

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_config_dir", return_value=symlink),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["reset", "config", "--force"], input="y\n")
        assert result.exit_code == 0
        assert "Safety checks bypassed" in result.output
        assert "Are you SURE?" in result.output
        assert not target.exists()

    def test_reset_config_force_bypasses_outside_home(self, tmp_path):
        """'reset config --force' should bypass outside-home check but still confirm."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.env").touch()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_config_dir", return_value=config_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path / "fakehome"),
        ):
            result = runner.invoke(cli, ["reset", "config", "--force"], input="y\n")
        assert result.exit_code == 0
        assert "Safety checks bypassed" in result.output
        assert "Are you SURE?" in result.output
        assert not config_dir.exists()

    def test_reset_config_rmtree_failure(self, tmp_path):
        """'reset config' should show friendly error if rmtree fails."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.is_interactive", return_value=True),
            patch("summon_claude.cli.reset.is_daemon_running", return_value=False),
            patch("summon_claude.cli.reset.get_config_dir", return_value=config_dir),
            patch("summon_claude.cli.reset.Path.home", return_value=tmp_path),
            patch(
                "summon_claude.cli.reset.shutil.rmtree",
                side_effect=OSError("Permission denied"),
            ),
        ):
            result = runner.invoke(cli, ["reset", "config"], input="y\n")
        assert result.exit_code != 0
        assert "Failed to delete" in result.output

    def test_reset_config_refuses_non_interactive(self, tmp_path):
        """'reset config' should refuse in non-interactive mode."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        runner = CliRunner()
        with (
            patch("summon_claude.cli.reset.get_config_dir", return_value=config_dir),
        ):
            result = runner.invoke(cli, ["--no-interactive", "reset", "config"])
        assert result.exit_code != 0
        assert "interactive mode" in result.output
        assert config_dir.exists()


class TestConfigPaths:
    def test_google_credentials_dir_is_under_config_dir(self):
        """get_google_credentials_dir() must return a path under config dir, not data dir."""
        assert get_google_credentials_dir() == get_config_dir() / "google-credentials"

    def test_workspace_config_path_is_under_config_dir(self):
        """get_workspace_config_path() must return a path under config dir, not data dir."""
        assert get_workspace_config_path().parent == get_config_dir()

    def test_browser_auth_dir_is_under_config_dir(self):
        """get_browser_auth_dir() must return a path under config dir, not data dir."""
        assert get_browser_auth_dir() == get_config_dir() / "browser_auth"

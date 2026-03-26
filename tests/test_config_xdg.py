"""Tests for XDG path resolution in summon_claude.config."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from summon_claude.config import (
    SummonConfig,
    get_claude_config_dir,
    get_config_dir,
    get_config_file,
    get_data_dir,
)


class TestGetConfigDir:
    def test_xdg_config_home_set(self, tmp_path, monkeypatch):
        """XDG_CONFIG_HOME set → returns XDG_CONFIG_HOME/summon."""
        xdg_config = tmp_path / "xdg_config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        # Also clear any cached state by reimporting
        import importlib

        import summon_claude.config as cfg_mod

        importlib.reload(cfg_mod)

        result = cfg_mod.get_config_dir()
        assert result == xdg_config / "summon"

    def test_xdg_config_home_not_set_dotconfig_exists(self, tmp_path, monkeypatch):
        """No XDG_CONFIG_HOME + ~/.config exists → returns ~/.config/summon."""
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Create ~/.config to simulate it existing
        dot_config = tmp_path / ".config"
        dot_config.mkdir()

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import importlib

            import summon_claude.config as cfg_mod

            # Need to use the function directly, not the cached value
            result = cfg_mod.get_config_dir()
            # Since ~/.config exists under tmp_path, should return tmp_path/.config/summon
            assert result == tmp_path / ".config" / "summon"

    def test_xdg_fallback_to_home_summon(self, tmp_path, monkeypatch):
        """No XDG_CONFIG_HOME + no ~/.config → returns ~/.summon."""
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Do NOT create ~/.config, so it doesn't exist
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            result = cfg_mod.get_config_dir()
            assert result == tmp_path / ".summon"


class TestGetDataDir:
    def test_xdg_data_home_set(self, tmp_path, monkeypatch):
        """XDG_DATA_HOME set → returns XDG_DATA_HOME/summon."""
        xdg_data = tmp_path / "xdg_data"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

        import summon_claude.config as cfg_mod

        result = cfg_mod.get_data_dir()
        assert result == xdg_data / "summon"

    def test_xdg_data_home_not_set_local_share_exists(self, tmp_path, monkeypatch):
        """No XDG_DATA_HOME + ~/.local/share exists → returns ~/.local/share/summon."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        local_share = tmp_path / ".local" / "share"
        local_share.mkdir(parents=True)

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            result = cfg_mod.get_data_dir()
            assert result == tmp_path / ".local" / "share" / "summon"

    def test_xdg_data_fallback_to_home_summon(self, tmp_path, monkeypatch):
        """No XDG_DATA_HOME + no ~/.local/share → returns ~/.summon."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        # Do NOT create ~/.local/share
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            result = cfg_mod.get_data_dir()
            assert result == tmp_path / ".summon"


class TestGetConfigFile:
    def test_config_file_path_is_under_config_dir(self, tmp_path, monkeypatch):
        """get_config_file() returns get_config_dir() / 'config.env'."""
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            config_dir = cfg_mod.get_config_dir()
            config_file = cfg_mod.get_config_file()
            assert config_file == config_dir / "config.env"
            assert config_file.name == "config.env"

    def test_config_file_has_env_extension(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            import summon_claude.config as cfg_mod

            assert cfg_mod.get_config_file().suffix == ".env"


class TestConfigLoadsFromXdg:
    def test_config_loads_from_xdg_config_path(self, tmp_path, monkeypatch):
        """SummonConfig should load from the XDG config file when it exists."""
        xdg_config = tmp_path / "xdg_config"
        xdg_config.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

        config_dir = xdg_config / "summon"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-from-xdg\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-from-xdg\n"
            "SUMMON_SLACK_SIGNING_SECRET=secret-from-xdg\n"
        )

        # Need to reload config module so get_config_file() picks up new XDG var
        import importlib

        import summon_claude.config as cfg_mod

        importlib.reload(cfg_mod)

        # The config file path should now point to our XDG config
        cfg_file = cfg_mod.get_config_file()
        assert cfg_file == config_dir / "config.env"
        assert cfg_file.exists()

        # Read the content to verify
        content = cfg_file.read_text()
        assert "xoxb-from-xdg" in content
        assert "xapp-from-xdg" in content


class TestGetClaudeConfigDir:
    def test_returns_default_when_env_not_set(self, monkeypatch):
        """Returns ~/.claude when CLAUDE_CONFIG_DIR is not set."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"

    def test_returns_absolute_env_path(self, monkeypatch, tmp_path):
        """Returns the CLAUDE_CONFIG_DIR value when it's an absolute path."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom-claude"))
        result = get_claude_config_dir()
        assert result == tmp_path / "custom-claude"

    def test_falls_back_on_relative_path(self, monkeypatch):
        """Falls back to ~/.claude when CLAUDE_CONFIG_DIR is relative."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/path")
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"

    def test_falls_back_on_empty_string(self, monkeypatch):
        """Falls back to ~/.claude when CLAUDE_CONFIG_DIR is empty."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"

    def test_falls_back_on_whitespace_only(self, monkeypatch):
        """Falls back to ~/.claude when CLAUDE_CONFIG_DIR is whitespace."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
        result = get_claude_config_dir()
        assert result == Path.home() / ".claude"


class TestLocalInstallDetection:
    """Tests for local install mode detection via _detect_install_mode()."""

    def test_virtual_env_under_project_root(self, tmp_path, monkeypatch):
        """VIRTUAL_ENV set under project root with pyproject.toml -> local."""
        (tmp_path / "pyproject.toml").touch()
        venv = tmp_path / ".venv"
        venv.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        from unittest.mock import patch

        from summon_claude.config import _detect_install_mode, _find_project_root

        _detect_install_mode.cache_clear()
        _find_project_root.cache_clear()
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            assert _detect_install_mode() == ("local", tmp_path)

    def test_virtual_env_not_set(self, tmp_path, monkeypatch):
        """No VIRTUAL_ENV, pyproject.toml present -> global."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _detect_install_mode() == ("global", None)

    def test_virtual_env_outside_project(self, tmp_path, monkeypatch):
        """VIRTUAL_ENV outside project root -> global."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", "/some/other/path")

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _detect_install_mode() == ("global", None)

    def test_no_pyproject_toml(self, tmp_path, monkeypatch):
        """No pyproject.toml -> global even with VIRTUAL_ENV."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
        # Patch _find_project_root to guarantee no pyproject.toml is found,
        # regardless of what exists in tmp_path's ancestors.
        monkeypatch.setattr("summon_claude.config._find_project_root", lambda: None)

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        mode, _ = _detect_install_mode()
        assert mode == "global"

    def test_summon_local_1_with_pyproject(self, tmp_path, monkeypatch):
        """SUMMON_LOCAL=1 + pyproject.toml -> local (no VIRTUAL_ENV needed)."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _detect_install_mode() == ("local", tmp_path)

    def test_summon_local_0_overrides_auto_detect(self, tmp_path, monkeypatch):
        """SUMMON_LOCAL=0 forces global even with VIRTUAL_ENV under project."""
        (tmp_path / "pyproject.toml").touch()
        venv = tmp_path / ".venv"
        venv.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))
        monkeypatch.setenv("SUMMON_LOCAL", "0")

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _detect_install_mode() == ("global", None)

    def test_summon_local_1_without_pyproject(self, tmp_path, monkeypatch):
        """SUMMON_LOCAL=1 without pyproject.toml -> graceful fallback to global."""
        isolated = tmp_path / "no_project"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        monkeypatch.setenv("SUMMON_LOCAL", "1")
        monkeypatch.setattr("summon_claude.config._find_project_root", lambda: None)

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _detect_install_mode() == ("global", None)

    def test_relative_virtual_env_rejected(self, tmp_path, monkeypatch):
        """Relative VIRTUAL_ENV is rejected even if it resolves under project root."""
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / ".venv").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VIRTUAL_ENV", ".venv")

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _detect_install_mode() == ("global", None)

    def test_subdirectory_walk_up(self, tmp_path, monkeypatch):
        """CWD in subdirectory finds pyproject.toml in parent via walk-up."""
        (tmp_path / "pyproject.toml").touch()
        subdir = tmp_path / "src" / "app"
        subdir.mkdir(parents=True)
        venv = tmp_path / ".venv"
        venv.mkdir()
        monkeypatch.chdir(subdir)
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        from unittest.mock import patch

        from summon_claude.config import _detect_install_mode, _find_project_root

        _detect_install_mode.cache_clear()
        _find_project_root.cache_clear()
        # tmp_path is outside real $HOME; mock so auto-detect home check passes
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            assert _detect_install_mode() == ("local", tmp_path)


class TestLocalInstallPathResolution:
    """Tests for path resolution in local vs global mode."""

    def test_local_mode_config_dir(self, tmp_path, monkeypatch):
        """Local mode: get_config_dir() returns project_root/.summon."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")

        import importlib

        import summon_claude.config as cfg_mod

        importlib.reload(cfg_mod)

        assert cfg_mod.get_config_dir() == tmp_path / ".summon"

    def test_local_mode_data_dir(self, tmp_path, monkeypatch):
        """Local mode: get_data_dir() returns project_root/.summon."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")

        import importlib

        import summon_claude.config as cfg_mod

        importlib.reload(cfg_mod)

        assert cfg_mod.get_data_dir() == tmp_path / ".summon"

    def test_global_mode_uses_xdg(self, tmp_path, monkeypatch):
        """Global mode: get_config_dir() uses XDG_CONFIG_HOME."""
        xdg_config = tmp_path / "xdg_config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("SUMMON_LOCAL", raising=False)

        import importlib

        import summon_claude.config as cfg_mod

        importlib.reload(cfg_mod)

        assert cfg_mod.get_config_dir() == xdg_config / "summon"


class TestFindLocalDaemonHint:
    """Tests for find_local_daemon_hint() — mode mismatch detection."""

    def test_returns_hint_when_local_daemon_exists_in_global_mode(self, tmp_path, monkeypatch):
        """Global mode + .summon/daemon.sock exists nearby -> returns hint."""
        (tmp_path / "pyproject.toml").touch()
        summon_dir = tmp_path / ".summon"
        summon_dir.mkdir()
        (summon_dir / "daemon.sock").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("SUMMON_LOCAL", raising=False)

        from summon_claude.config import _detect_install_mode, find_local_daemon_hint

        _detect_install_mode.cache_clear()
        hint = find_local_daemon_hint()
        assert hint is not None
        assert "SUMMON_LOCAL=1" in hint

    def test_returns_none_in_local_mode(self, tmp_path, monkeypatch):
        """Already in local mode -> no hint needed."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")

        from summon_claude.config import _detect_install_mode, find_local_daemon_hint

        _detect_install_mode.cache_clear()
        assert find_local_daemon_hint() is None

    def test_returns_none_when_no_local_daemon(self, tmp_path, monkeypatch):
        """Global mode + no .summon/daemon.sock -> no hint."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        from summon_claude.config import _detect_install_mode, find_local_daemon_hint

        _detect_install_mode.cache_clear()
        assert find_local_daemon_hint() is None

    def test_returns_none_when_root_found_but_no_socket(self, tmp_path, monkeypatch):
        """Global mode + pyproject.toml found but no daemon.sock -> no hint."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("SUMMON_LOCAL", raising=False)

        from summon_claude.config import _detect_install_mode, find_local_daemon_hint

        _detect_install_mode.cache_clear()
        assert find_local_daemon_hint() is None


class TestPublicApiWrappers:
    """Direct tests for is_local_install() and get_local_root()."""

    def test_is_local_install_true(self, tmp_path, monkeypatch):
        """is_local_install() returns True in local mode."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")

        from summon_claude.config import _detect_install_mode, is_local_install

        _detect_install_mode.cache_clear()
        assert is_local_install() is True

    def test_is_local_install_false(self, tmp_path, monkeypatch):
        """is_local_install() returns False in global mode."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        from summon_claude.config import _detect_install_mode, is_local_install

        _detect_install_mode.cache_clear()
        assert is_local_install() is False

    def test_get_local_root_returns_path(self, tmp_path, monkeypatch):
        """get_local_root() returns project root in local mode."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")

        from summon_claude.config import _detect_install_mode, get_local_root

        _detect_install_mode.cache_clear()
        assert get_local_root() == tmp_path

    def test_get_local_root_returns_none(self, tmp_path, monkeypatch):
        """get_local_root() returns None in global mode."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        from summon_claude.config import _detect_install_mode, get_local_root

        _detect_install_mode.cache_clear()
        assert get_local_root() is None


class TestUnrecognizedSummonLocal:
    """Tests for SUMMON_LOCAL with unrecognized values."""

    def test_unrecognized_value_logs_warning(self, tmp_path, monkeypatch, caplog):
        """SUMMON_LOCAL=true (not '0' or '1') logs a warning and falls through."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "true")
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        import logging

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        with caplog.at_level(logging.WARNING, logger="summon_claude.config"):
            mode, _ = _detect_install_mode()

        assert mode == "global"
        assert "SUMMON_LOCAL='true' not recognized" in caplog.text

    def test_unrecognized_value_falls_through_to_auto_detect(self, tmp_path, monkeypatch):
        """SUMMON_LOCAL=yes with valid VIRTUAL_ENV still auto-detects local."""
        (tmp_path / "pyproject.toml").touch()
        venv = tmp_path / ".venv"
        venv.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "yes")
        monkeypatch.setenv("VIRTUAL_ENV", str(venv))

        from unittest.mock import patch

        from summon_claude.config import _detect_install_mode, _find_project_root

        _detect_install_mode.cache_clear()
        _find_project_root.cache_clear()
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            mode, root = _detect_install_mode()
        assert mode == "local"
        assert root == tmp_path


class TestFindProjectRootHardening:
    """Tests for _find_project_root() security hardening."""

    def test_rejects_symlink_sentinel(self, tmp_path, monkeypatch):
        """pyproject.toml that is a symlink is rejected."""
        real_file = tmp_path / "real.txt"
        real_file.write_text("content")
        (tmp_path / "pyproject.toml").symlink_to(real_file)
        monkeypatch.chdir(tmp_path)

        from summon_claude.config import _find_project_root

        assert _find_project_root() is None

    def test_stops_at_home_parent(self, tmp_path, monkeypatch):
        """Walk-up does not ascend above home directory parent."""
        # Simulate a home directory inside tmp_path so home_parent is testable
        fake_home = tmp_path / "home" / "user"
        fake_home.mkdir(parents=True)
        # Place pyproject.toml at home_parent level — should NOT be found
        (tmp_path / "home" / "pyproject.toml").touch()
        monkeypatch.chdir(fake_home)

        from unittest.mock import patch

        from summon_claude.config import _find_project_root

        with patch("summon_claude.config.Path.home", return_value=fake_home):
            assert _find_project_root() is None

    def test_rejects_project_root_outside_home(self, tmp_path, monkeypatch):
        """Project root outside $HOME is rejected by _detect_install_mode."""
        fake_home = tmp_path / "fakehome" / "user"
        fake_home.mkdir(parents=True)
        outside = tmp_path / "outside" / "project"
        outside.mkdir(parents=True)
        (outside / "pyproject.toml").touch()
        (outside / ".venv").mkdir()
        monkeypatch.chdir(outside)
        monkeypatch.setenv("VIRTUAL_ENV", str(outside / ".venv"))

        from unittest.mock import patch

        from summon_claude.config import _detect_install_mode, _find_project_root

        _detect_install_mode.cache_clear()
        _find_project_root.cache_clear()
        with patch("summon_claude.config.Path.home", return_value=fake_home):
            # _find_project_root finds it, but _detect_install_mode rejects it
            mode, _ = _detect_install_mode()
            assert mode == "global"


class TestDefaultDbPathMigrationGuard:
    """Tests for default_db_path() local-mode migration suppression.

    The conftest patches ``default_db_path`` at session scope, so we reload
    the module to get the real function and patch its dependencies directly.
    """

    @staticmethod
    def _get_real_default_db_path():
        import importlib

        import summon_claude.sessions.registry as reg_mod

        importlib.reload(reg_mod)
        return reg_mod, reg_mod.default_db_path

    def test_global_mode_migrates_old_path(self, tmp_path):
        """Global mode: old ~/.summon/registry.db migrates to new XDG path."""
        fake_home = tmp_path / "fakehome"
        old_db = fake_home / ".summon" / "registry.db"
        old_db.parent.mkdir(parents=True)
        old_db.write_text("test")

        new_dir = tmp_path / "new"
        new_path = new_dir / "registry.db"

        reg_mod, real_fn = self._get_real_default_db_path()
        with (
            patch.object(reg_mod, "is_local_install", return_value=False),
            patch.object(reg_mod, "get_data_dir", return_value=new_dir),
            patch("pathlib.Path.home", return_value=fake_home),
        ):
            result = real_fn()

        assert result == new_path
        assert not old_db.exists()
        assert new_path.exists()

    def test_local_mode_skips_migration(self, tmp_path):
        """Local mode: old ~/.summon/registry.db is NOT migrated."""
        fake_home = tmp_path / "fakehome"
        old_db = fake_home / ".summon" / "registry.db"
        old_db.parent.mkdir(parents=True)
        old_db.write_text("test")

        local_dir = tmp_path / "project" / ".summon"
        local_path = local_dir / "registry.db"

        reg_mod, real_fn = self._get_real_default_db_path()
        with (
            patch.object(reg_mod, "is_local_install", return_value=True),
            patch.object(reg_mod, "get_data_dir", return_value=local_dir),
            patch("pathlib.Path.home", return_value=fake_home),
        ):
            result = real_fn()

        # Old DB should still exist — migration was skipped
        assert old_db.exists()
        assert result == local_path

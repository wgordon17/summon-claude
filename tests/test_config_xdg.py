"""Tests for XDG path resolution in summon_claude.config."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

# Captured at import time (before session fixtures run) so tests can call
# the real default_db_path without importlib.reload() — which breaks
# session-scoped patches and causes xdist flakiness.
import summon_claude.sessions.registry as _registry_mod
from summon_claude.config import (
    SummonConfig,
    get_claude_config_dir,
    get_config_dir,
    get_config_file,
    get_data_dir,
)
from summon_claude.config import (
    _local_socket_path as _real_local_socket_path,  # captured before _isolate_data_dir patches it
)
from summon_claude.config import (
    _xdg_dir as _real_xdg_dir,  # captured before _isolate_data_dir patches it
)
from summon_claude.config import (
    get_local_root as _real_get_local_root,  # captured before _isolate_data_dir patches it
)

_real_default_db_path = _registry_mod.default_db_path


class TestGetConfigDir:
    def test_xdg_config_home_set(self, tmp_path, monkeypatch):
        """XDG_CONFIG_HOME set → returns XDG_CONFIG_HOME/summon.

        _fake_xdg_dir (session fixture) respects explicit XDG env vars,
        so setting XDG_CONFIG_HOME is sufficient — no importlib.reload needed.
        """
        xdg_config = tmp_path / "xdg_config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

        result = get_config_dir()
        assert result == xdg_config / "summon"

    def test_xdg_config_home_not_set_dotconfig_exists(self, tmp_path, monkeypatch):
        """No XDG_CONFIG_HOME + ~/.config exists → _xdg_dir returns ~/.config/summon.

        Tests _xdg_dir directly: _isolate_data_dir patches the name in
        summon_claude.config's namespace, so get_config_dir() is always
        intercepted by the session fixture.  _xdg_dir() is the unit under test.
        _real_xdg_dir is captured at module-import time, before the fixture patches.
        """
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Create ~/.config to simulate it existing
        dot_config = tmp_path / ".config"
        dot_config.mkdir()

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = _real_xdg_dir("XDG_CONFIG_HOME", ".config/summon", "summon")
            # Since ~/.config exists under tmp_path, should return tmp_path/.config/summon
            assert result == tmp_path / ".config" / "summon"

    def test_xdg_fallback_to_home_summon(self, tmp_path, monkeypatch):
        """No XDG_CONFIG_HOME + no ~/.config → _xdg_dir returns ~/.summon.

        Tests _xdg_dir directly — see test_xdg_config_home_not_set_dotconfig_exists.
        """
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        # Do NOT create ~/.config, so it doesn't exist
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = _real_xdg_dir("XDG_CONFIG_HOME", ".config/summon", "summon")
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
        """No XDG_DATA_HOME + ~/.local/share exists → _xdg_dir returns ~/.local/share/summon."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        local_share = tmp_path / ".local" / "share"
        local_share.mkdir(parents=True)

        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = _real_xdg_dir("XDG_DATA_HOME", ".local/share/summon", "summon")
            assert result == tmp_path / ".local" / "share" / "summon"

    def test_xdg_data_fallback_to_home_summon(self, tmp_path, monkeypatch):
        """No XDG_DATA_HOME + no ~/.local/share → _xdg_dir returns ~/.summon."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        # Do NOT create ~/.local/share
        with patch("summon_claude.config.Path.home", return_value=tmp_path):
            result = _real_xdg_dir("XDG_DATA_HOME", ".local/share/summon", "summon")
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

        # _fake_xdg_dir respects explicit XDG vars — no reload needed
        cfg_file = get_config_file()
        assert cfg_file == config_dir / "config.env"
        assert cfg_file.exists()

        # Read the content to verify
        content = cfg_file.read_text()
        assert "xoxb-from-xdg" in content
        assert "xapp-from-xdg" in content

    def test_summon_config_instantiation_loads_from_xdg(self, tmp_path, monkeypatch):
        """SummonConfig() reads tokens from the XDG config file at instantiation time.

        Verifies the lazy injection contract: get_config_file() is called inside
        __init__, not at class-definition / import time, so the XDG env var set
        after import is respected.
        """
        xdg_config = tmp_path / "xdg_config"
        xdg_config.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

        config_dir = xdg_config / "summon"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.env"
        config_file.write_text(
            "SUMMON_SLACK_BOT_TOKEN=xoxb-from-xdg\n"
            "SUMMON_SLACK_APP_TOKEN=xapp-from-xdg\n"
            "SUMMON_SLACK_SIGNING_SECRET=abc123def456\n"
        )

        cfg = SummonConfig()
        assert cfg.slack_bot_token == "xoxb-from-xdg"
        assert cfg.slack_app_token == "xapp-from-xdg"
        assert cfg.slack_signing_secret == "abc123def456"


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
        """Local mode: get_config_dir() returns project_root/.summon.

        Overrides the session-scoped get_local_root=None patch so local
        mode resolution works.  No importlib.reload needed.
        """
        with patch("summon_claude.config.get_local_root", return_value=tmp_path):
            assert get_config_dir() == tmp_path / ".summon"

    def test_local_mode_data_dir(self, tmp_path, monkeypatch):
        """Local mode: get_data_dir() returns project_root/.summon.

        Overrides the session-scoped get_local_root=None patch so local
        mode resolution works.  No importlib.reload needed.
        """
        with patch("summon_claude.config.get_local_root", return_value=tmp_path):
            assert get_data_dir() == tmp_path / ".summon"

    def test_global_mode_uses_xdg(self, tmp_path, monkeypatch):
        """Global mode: get_config_dir() uses XDG_CONFIG_HOME.

        _fake_xdg_dir respects explicit XDG env vars.  Session fixture
        already forces global mode.  No importlib.reload needed.
        """
        xdg_config = tmp_path / "xdg_config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("SUMMON_LOCAL", raising=False)

        assert get_config_dir() == xdg_config / "summon"


class TestFindLocalDaemonHint:
    """Tests for find_local_daemon_hint() — mode mismatch detection."""

    def test_returns_hint_when_local_daemon_exists_in_global_mode(self, tmp_path, monkeypatch):
        """Global mode + socket exists for nearby project -> returns hint."""
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("SUMMON_LOCAL", raising=False)

        from summon_claude.config import (
            _detect_install_mode,
            _find_project_root,
            _local_socket_path,
            find_local_daemon_hint,
        )

        _detect_install_mode.cache_clear()
        _find_project_root.cache_clear()

        # Use the (conftest-patched) _local_socket_path so the path matches
        # what find_local_daemon_hint() will check
        sock = _local_socket_path(tmp_path)
        sock.parent.mkdir(parents=True, exist_ok=True)
        sock.touch()

        try:
            hint = find_local_daemon_hint()
            assert hint is not None
            assert "SUMMON_LOCAL=1" in hint
        finally:
            sock.unlink(missing_ok=True)

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
        """get_local_root() returns project root in local mode.

        Uses _real_get_local_root captured at module-import time, before
        _isolate_data_dir patches summon_claude.config.get_local_root to None.
        """
        (tmp_path / "pyproject.toml").touch()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SUMMON_LOCAL", "1")

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _real_get_local_root() == tmp_path

    def test_get_local_root_returns_none(self, tmp_path, monkeypatch):
        """get_local_root() returns None in global mode.

        Uses _real_get_local_root captured at module-import time, before
        _isolate_data_dir patches summon_claude.config.get_local_root to None.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)

        from summon_claude.config import _detect_install_mode

        _detect_install_mode.cache_clear()
        assert _real_get_local_root() is None


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


class TestGetSocketPath:
    """Tests for get_socket_path() and _local_socket_path()."""

    def test_local_mode_returns_hashed_path(self, tmp_path):
        """Local mode: get_socket_path() returns a <hash>.sock path."""
        from summon_claude.config import get_socket_path

        with patch("summon_claude.config.get_local_root", return_value=tmp_path):
            result = get_socket_path()

        assert str(result).endswith(".sock")
        assert len(result.name) == len("aabbccddeeff.sock")  # 12 hex chars + .sock

    def test_local_mode_hash_deterministic(self, tmp_path):
        """Same project root always produces the same hash."""
        from summon_claude.config import get_socket_path

        with patch("summon_claude.config.get_local_root", return_value=tmp_path):
            result1 = get_socket_path()
            result2 = get_socket_path()

        assert result1 == result2

    def test_global_mode_returns_data_dir_daemon_sock(self):
        """Global mode: get_socket_path() returns get_data_dir() / 'daemon.sock'."""
        from summon_claude.config import get_data_dir, get_socket_path

        with patch("summon_claude.config.get_local_root", return_value=None):
            result = get_socket_path()
            expected = get_data_dir() / "daemon.sock"

        assert result == expected

    def test_different_project_roots_produce_different_hashes(self, tmp_path):
        """Different project roots produce different socket paths."""
        from summon_claude.config import _local_socket_path

        root_a = tmp_path / "project_a"
        root_b = tmp_path / "project_b"
        root_a.mkdir()
        root_b.mkdir()

        sock_a = _local_socket_path(root_a)
        sock_b = _local_socket_path(root_b)

        assert sock_a != sock_b
        assert sock_a.name != sock_b.name

    def test_hash_is_12_hex_chars(self, tmp_path):
        """Socket filename is exactly 12 hex chars + .sock."""
        import re

        from summon_claude.config import _local_socket_path

        sock = _local_socket_path(tmp_path)
        assert re.fullmatch(r"[0-9a-f]{12}\.sock", sock.name), f"Unexpected name: {sock.name}"

    def test_symlinked_paths_share_socket(self, tmp_path):
        """Two symlinked paths pointing to the same project produce the same socket."""
        from summon_claude.config import _local_socket_path

        real_dir = tmp_path / "real_project"
        real_dir.mkdir()
        link_dir = tmp_path / "link_project"
        link_dir.symlink_to(real_dir)

        sock_real = _local_socket_path(real_dir)
        sock_link = _local_socket_path(link_dir)

        assert sock_real == sock_link

    def test_uses_xdg_runtime_dir_when_set(self, tmp_path, monkeypatch):
        """_local_socket_path uses $XDG_RUNTIME_DIR/summon/ when the env var is set."""
        runtime_dir = tmp_path / "run"
        runtime_dir.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))

        sock = _real_local_socket_path(tmp_path)
        assert sock.parent == runtime_dir / "summon"

    def test_falls_back_to_tmp_without_xdg_runtime_dir(self, tmp_path, monkeypatch):
        """_local_socket_path falls back to /tmp/summon-<uid>/ without XDG_RUNTIME_DIR."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        sock = _real_local_socket_path(tmp_path)
        assert sock.parent == Path(f"/tmp/summon-{os.getuid()}")

    def test_ignores_relative_xdg_runtime_dir(self, tmp_path, monkeypatch, caplog):
        """Relative XDG_RUNTIME_DIR is ignored with warning — falls back to /tmp."""
        monkeypatch.setenv("XDG_RUNTIME_DIR", "./relative/path")

        sock = _real_local_socket_path(tmp_path)
        assert sock.parent == Path(f"/tmp/summon-{os.getuid()}")
        assert "not absolute" in caplog.text


class TestDefaultDbPathMigrationGuard:
    """Tests for default_db_path() local-mode migration suppression.

    Uses module-level captures of ``_registry_mod`` and ``_real_default_db_path``
    (captured before session fixtures run) to access the real function without
    ``importlib.reload()``, which would break session-scoped patches.
    """

    @staticmethod
    def _get_real_default_db_path():
        # Use module-level captures (pre-session-fixture) to avoid
        # importlib.reload() which breaks session-scoped patches.
        return _registry_mod, _real_default_db_path

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


class TestRealSocketPathUnder104Chars:
    """Verify that real socket paths fit the Unix 104-byte limit."""

    def test_tmp_fallback_path_under_104_chars(self):
        """The /tmp/summon-<uid>/<hash>.sock fallback path is < 104 chars."""
        import hashlib

        uid = os.getuid()
        base = f"/tmp/summon-{uid}"
        long_project = "/home/someuser/projects/a-rather-deeply-nested-project-directory"
        digest = hashlib.sha256(long_project.encode()).hexdigest()[:12]
        real_path = f"{base}/{digest}.sock"

        assert len(real_path) < 104, (
            f"Socket path is {len(real_path)} chars (limit 104): {real_path}"
        )

    def test_xdg_runtime_dir_path_under_104_chars(self):
        """The $XDG_RUNTIME_DIR/summon/<hash>.sock path is < 104 chars."""
        import hashlib

        xdg_path = "/run/user/1000/summon"
        long_project = "/home/someuser/projects/a-rather-deeply-nested-project-directory"
        digest = hashlib.sha256(long_project.encode()).hexdigest()[:12]
        real_path = f"{xdg_path}/{digest}.sock"

        assert len(real_path) < 104, (
            f"Socket path is {len(real_path)} chars (limit 104): {real_path}"
        )

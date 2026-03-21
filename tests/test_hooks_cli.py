"""Tests for summon hooks install/uninstall CLI (C7).

Covers:
- install_hooks / uninstall_hooks business logic
- Settings.json manipulation
- Shell script writing and executable bit
- Idempotency and preservation of existing hooks
- sqlite3 / summon binary warning paths
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import summon_claude.cli.hooks as hooks_module
from summon_claude.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_paths(tmp_path: Path):
    """Return a context manager that redirects all path constants to tmp_path."""
    hooks_dir = tmp_path / ".claude" / "hooks"
    settings_path = tmp_path / ".claude" / "settings.json"
    pre_script = hooks_dir / "summon-pre-worktree.sh"
    post_script = hooks_dir / "summon-post-worktree.sh"

    return (
        patch.object(hooks_module, "_HOOKS_DIR", hooks_dir),
        patch.object(hooks_module, "_SETTINGS_PATH", settings_path),
        patch.object(hooks_module, "_PRE_SCRIPT", pre_script),
        patch.object(hooks_module, "_POST_SCRIPT", post_script),
    )


def _invoke_install(
    tmp_path: Path,
    summon_bin: str = "/usr/local/bin/summon",
    sqlite3_bin: str | None = "/usr/bin/sqlite3",
) -> object:
    """Invoke install_hooks with all paths redirected to tmp_path."""
    which_map = {"summon": summon_bin, "sqlite3": sqlite3_bin}
    patches = _patch_paths(tmp_path)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patch("shutil.which", side_effect=which_map.get),
    ):
        from summon_claude.cli.hooks import install_hooks

        install_hooks()


def _read_settings(tmp_path: Path) -> dict:
    settings_path = tmp_path / ".claude" / "settings.json"
    return json.loads(settings_path.read_text())


# ---------------------------------------------------------------------------
# Install: script creation
# ---------------------------------------------------------------------------


class TestHooksInstallCreatesScripts:
    def test_hooks_install_creates_scripts(self, tmp_path):
        """Shell wrappers are written to the hooks directory."""
        _invoke_install(tmp_path)

        pre = tmp_path / ".claude" / "hooks" / "summon-pre-worktree.sh"
        post = tmp_path / ".claude" / "hooks" / "summon-post-worktree.sh"
        assert pre.exists()
        assert post.exists()

    def test_hooks_install_scripts_are_executable(self, tmp_path):
        """Shell wrapper files have the executable bit set."""
        _invoke_install(tmp_path)

        pre = tmp_path / ".claude" / "hooks" / "summon-pre-worktree.sh"
        post = tmp_path / ".claude" / "hooks" / "summon-post-worktree.sh"
        assert pre.stat().st_mode & stat.S_IXUSR
        assert post.stat().st_mode & stat.S_IXUSR

    def test_hooks_install_scripts_have_shebang(self, tmp_path):
        """Shell wrapper files start with #!/bin/bash."""
        _invoke_install(tmp_path)

        pre = tmp_path / ".claude" / "hooks" / "summon-pre-worktree.sh"
        post = tmp_path / ".claude" / "hooks" / "summon-post-worktree.sh"
        assert pre.read_text().startswith("#!/bin/bash")
        assert post.read_text().startswith("#!/bin/bash")

    def test_hooks_install_creates_hooks_dir(self, tmp_path):
        """hooks directory is created if it doesn't exist."""
        hooks_dir = tmp_path / ".claude" / "hooks"
        assert not hooks_dir.exists()
        _invoke_install(tmp_path)
        assert hooks_dir.exists()


# ---------------------------------------------------------------------------
# Install: @@SUMMON_PATH@@ substitution
# ---------------------------------------------------------------------------


class TestInstallSubstitutesSummonPath:
    def test_install_substitutes_summon_path(self, tmp_path):
        """@@SUMMON_PATH@@ in post-worktree.sh is replaced with the resolved binary path."""
        summon_bin = "/custom/bin/summon"
        _invoke_install(tmp_path, summon_bin=summon_bin)

        post = tmp_path / ".claude" / "hooks" / "summon-post-worktree.sh"
        content = post.read_text()
        assert "@@SUMMON_PATH@@" not in content
        assert summon_bin in content

    def test_install_pre_script_has_no_token(self, tmp_path):
        """Pre-worktree script does not contain the substitution token."""
        _invoke_install(tmp_path)
        pre = tmp_path / ".claude" / "hooks" / "summon-pre-worktree.sh"
        assert "@@SUMMON_PATH@@" not in pre.read_text()


# ---------------------------------------------------------------------------
# Install: settings.json entries
# ---------------------------------------------------------------------------


class TestHooksInstallAddsSettingsEntries:
    def test_install_adds_pretooluse_entry(self, tmp_path):
        """PreToolUse EnterWorktree entry is added to settings.json."""
        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        pre_entries = settings["hooks"]["PreToolUse"]
        assert any(
            e.get("matcher") == "EnterWorktree"
            and any("summon-pre-worktree" in h.get("command", "") for h in e.get("hooks", []))
            for e in pre_entries
        )

    def test_install_adds_posttooluse_entry(self, tmp_path):
        """PostToolUse EnterWorktree entry is added to settings.json."""
        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        post_entries = settings["hooks"]["PostToolUse"]
        assert any(
            e.get("matcher") == "EnterWorktree"
            and any("summon-post-worktree" in h.get("command", "") for h in e.get("hooks", []))
            for e in post_entries
        )

    def test_install_hook_entry_structure(self, tmp_path):
        """Hook entries have correct type: command structure."""
        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        for hook_type in ("PreToolUse", "PostToolUse"):
            for entry in settings["hooks"][hook_type]:
                for hook in entry.get("hooks", []):
                    assert hook["type"] == "command"
                    assert "command" in hook


# ---------------------------------------------------------------------------
# Install: idempotency
# ---------------------------------------------------------------------------


class TestHooksInstallIdempotent:
    def test_install_twice_does_not_duplicate_pretooluse(self, tmp_path):
        """Running install twice does not duplicate PreToolUse entries."""
        _invoke_install(tmp_path)
        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        pre_entries = settings["hooks"]["PreToolUse"]
        summon_entries = [
            e
            for e in pre_entries
            if any("summon-pre-worktree" in h.get("command", "") for h in e.get("hooks", []))
        ]
        assert len(summon_entries) == 1

    def test_install_twice_does_not_duplicate_posttooluse(self, tmp_path):
        """Running install twice does not duplicate PostToolUse entries."""
        _invoke_install(tmp_path)
        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        post_entries = settings["hooks"]["PostToolUse"]
        summon_entries = [
            e
            for e in post_entries
            if any("summon-post-worktree" in h.get("command", "") for h in e.get("hooks", []))
        ]
        assert len(summon_entries) == 1


# ---------------------------------------------------------------------------
# Install: preserves existing hooks
# ---------------------------------------------------------------------------


class TestHooksInstallPreservesExistingHooks:
    def test_install_preserves_existing_pretooluse_hooks(self, tmp_path):
        """Pre-existing PreToolUse hooks in settings.json are not removed."""
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "/usr/local/bin/other-tool"}],
                    }
                ]
            }
        }
        settings_path.write_text(json.dumps(existing))

        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        pre_entries = settings["hooks"]["PreToolUse"]
        commands = [h["command"] for e in pre_entries for h in e.get("hooks", [])]
        assert "/usr/local/bin/other-tool" in commands

    def test_install_preserves_existing_posttooluse_hooks(self, tmp_path):
        """Pre-existing PostToolUse hooks are not removed."""
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write",
                        "hooks": [{"type": "command", "command": "/usr/local/bin/formatter"}],
                    }
                ]
            }
        }
        settings_path.write_text(json.dumps(existing))

        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        post_entries = settings["hooks"]["PostToolUse"]
        commands = [h["command"] for e in post_entries for h in e.get("hooks", [])]
        assert "/usr/local/bin/formatter" in commands

    def test_install_preserves_top_level_settings_keys(self, tmp_path):
        """Other top-level keys in settings.json are not removed."""
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {"model": "claude-opus-4-6", "hooks": {}}
        settings_path.write_text(json.dumps(existing))

        _invoke_install(tmp_path)
        settings = _read_settings(tmp_path)
        assert settings["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Install: creates settings.json if missing
# ---------------------------------------------------------------------------


class TestHooksInstallCreatesSettingsIfMissing:
    def test_install_creates_settings_json_when_absent(self, tmp_path):
        """settings.json is created from scratch if it doesn't exist."""
        settings_path = tmp_path / ".claude" / "settings.json"
        assert not settings_path.exists()

        _invoke_install(tmp_path)

        assert settings_path.exists()
        settings = _read_settings(tmp_path)
        assert "hooks" in settings

    def test_install_creates_claude_dir_if_missing(self, tmp_path):
        """~/.claude directory is created if it doesn't exist."""
        claude_dir = tmp_path / ".claude"
        assert not claude_dir.exists()
        _invoke_install(tmp_path)
        assert claude_dir.exists()


# ---------------------------------------------------------------------------
# Install: sqlite3 warning
# ---------------------------------------------------------------------------


class TestInstallWarnsNoSqlite3:
    def test_install_warns_when_sqlite3_missing(self, tmp_path, capsys):
        """A warning is printed to stderr when sqlite3 CLI is not on PATH."""
        which_map = {"summon": "/usr/local/bin/summon", "sqlite3": None}
        patches = _patch_paths(tmp_path)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch("shutil.which", side_effect=which_map.get),
        ):
            from summon_claude.cli.hooks import install_hooks

            install_hooks()

        # Warning goes to stderr via click.echo(err=True)
        captured = capsys.readouterr()
        assert "sqlite3" in captured.err.lower()

    def test_install_no_warning_when_sqlite3_present(self, tmp_path, capsys):
        """No sqlite3 warning when sqlite3 is found on PATH."""
        _invoke_install(tmp_path, sqlite3_bin="/usr/bin/sqlite3")
        captured = capsys.readouterr()
        assert "sqlite3" not in captured.err.lower()


# ---------------------------------------------------------------------------
# Uninstall: removes entries
# ---------------------------------------------------------------------------


class TestHooksUninstallRemovesEntries:
    def test_uninstall_removes_pretooluse_entry(self, tmp_path):
        """uninstall_hooks removes the PreToolUse summon entry from settings.json."""
        _invoke_install(tmp_path)
        # Verify it was added
        settings = _read_settings(tmp_path)
        assert settings["hooks"]["PreToolUse"]

        # Now uninstall
        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            from summon_claude.cli.hooks import uninstall_hooks

            uninstall_hooks()

        settings = _read_settings(tmp_path)
        pre_entries = settings["hooks"].get("PreToolUse", [])
        summon_entries = [
            e
            for e in pre_entries
            if any("summon-pre-worktree" in h.get("command", "") for h in e.get("hooks", []))
        ]
        assert summon_entries == []

    def test_uninstall_removes_posttooluse_entry(self, tmp_path):
        """uninstall_hooks removes the PostToolUse summon entry from settings.json."""
        _invoke_install(tmp_path)

        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            from summon_claude.cli.hooks import uninstall_hooks

            uninstall_hooks()

        settings = _read_settings(tmp_path)
        post_entries = settings["hooks"].get("PostToolUse", [])
        summon_entries = [
            e
            for e in post_entries
            if any("summon-post-worktree" in h.get("command", "") for h in e.get("hooks", []))
        ]
        assert summon_entries == []


# ---------------------------------------------------------------------------
# Uninstall: removes scripts
# ---------------------------------------------------------------------------


class TestHooksUninstallRemovesScripts:
    def test_uninstall_removes_pre_script(self, tmp_path):
        """uninstall_hooks deletes summon-pre-worktree.sh."""
        _invoke_install(tmp_path)
        pre = tmp_path / ".claude" / "hooks" / "summon-pre-worktree.sh"
        assert pre.exists()

        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            from summon_claude.cli.hooks import uninstall_hooks

            uninstall_hooks()

        assert not pre.exists()

    def test_uninstall_removes_post_script(self, tmp_path):
        """uninstall_hooks deletes summon-post-worktree.sh."""
        _invoke_install(tmp_path)
        post = tmp_path / ".claude" / "hooks" / "summon-post-worktree.sh"
        assert post.exists()

        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            from summon_claude.cli.hooks import uninstall_hooks

            uninstall_hooks()

        assert not post.exists()

    def test_uninstall_tolerates_missing_scripts(self, tmp_path):
        """uninstall_hooks doesn't raise if scripts don't exist (already removed)."""
        # Don't install first — just uninstall on a fresh settings.json
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("{}")

        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            from summon_claude.cli.hooks import uninstall_hooks

            uninstall_hooks()  # Should not raise


# ---------------------------------------------------------------------------
# Uninstall: safety — doesn't touch non-summon hooks
# ---------------------------------------------------------------------------


class TestHooksUninstallSafe:
    def test_uninstall_preserves_other_pretooluse_hooks(self, tmp_path):
        """uninstall_hooks only removes summon entries, leaves others intact."""
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        other_entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/other/tool"}]}
        settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [other_entry]}}))

        # Install (adds summon entry alongside existing)
        _invoke_install(tmp_path)

        # Uninstall
        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            from summon_claude.cli.hooks import uninstall_hooks

            uninstall_hooks()

        settings = _read_settings(tmp_path)
        pre_entries = settings["hooks"].get("PreToolUse", [])
        commands = [h["command"] for e in pre_entries for h in e.get("hooks", [])]
        assert "/other/tool" in commands

    def test_uninstall_preserves_other_posttooluse_hooks(self, tmp_path):
        """uninstall_hooks doesn't remove non-summon PostToolUse hooks."""
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        other_entry = {
            "matcher": "Write",
            "hooks": [{"type": "command", "command": "/other/formatter"}],
        }
        settings_path.write_text(json.dumps({"hooks": {"PostToolUse": [other_entry]}}))

        _invoke_install(tmp_path)

        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            from summon_claude.cli.hooks import uninstall_hooks

            uninstall_hooks()

        settings = _read_settings(tmp_path)
        post_entries = settings["hooks"].get("PostToolUse", [])
        commands = [h["command"] for e in post_entries for h in e.get("hooks", [])]
        assert "/other/formatter" in commands


# ---------------------------------------------------------------------------
# CLI CliRunner integration: hooks install / uninstall commands
# ---------------------------------------------------------------------------


class TestHooksCliInstallCommand:
    def test_hooks_install_command_exits_zero(self, tmp_path):
        """'summon hooks install' exits 0 on success."""
        runner = CliRunner()
        which_map = {"summon": "/usr/local/bin/summon", "sqlite3": "/usr/bin/sqlite3"}
        patches = _patch_paths(tmp_path)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch("shutil.which", side_effect=which_map.get),
        ):
            result = runner.invoke(cli, ["hooks", "install"])
        assert result.exit_code == 0

    def test_hooks_install_command_prints_confirmation(self, tmp_path):
        """'summon hooks install' prints confirmation with hook paths."""
        runner = CliRunner()
        which_map = {"summon": "/usr/local/bin/summon", "sqlite3": "/usr/bin/sqlite3"}
        patches = _patch_paths(tmp_path)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch("shutil.which", side_effect=which_map.get),
        ):
            result = runner.invoke(cli, ["hooks", "install"])
        assert "Installed" in result.output or "settings.json" in result.output

    def test_hooks_install_fails_without_summon_on_path(self, tmp_path):
        """'summon hooks install' exits non-zero when summon binary not on PATH."""
        runner = CliRunner()
        patches = _patch_paths(tmp_path)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch("shutil.which", return_value=None),
        ):
            result = runner.invoke(cli, ["hooks", "install"])
        assert result.exit_code != 0

    def test_hooks_install_fails_with_single_quote_in_path(self, tmp_path):
        """'summon hooks install' exits non-zero when summon path has a single quote."""
        runner = CliRunner()
        which_map = {"summon": "/path/with'quote/summon", "sqlite3": "/usr/bin/sqlite3"}
        patches = _patch_paths(tmp_path)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch("shutil.which", side_effect=which_map.get),
        ):
            result = runner.invoke(cli, ["hooks", "install"])
        assert result.exit_code != 0
        assert "single quote" in result.output.lower()

    def test_hooks_uninstall_command_exits_zero(self, tmp_path):
        """'summon hooks uninstall' exits 0 (even with nothing to remove)."""
        runner = CliRunner()
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text("{}")

        patches = _patch_paths(tmp_path)
        with patches[0], patches[1], patches[2], patches[3]:
            result = runner.invoke(cli, ["hooks", "uninstall"])
        assert result.exit_code == 0

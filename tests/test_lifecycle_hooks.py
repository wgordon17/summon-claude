"""Tests for lifecycle hooks DB schema, runner, and CLI entry point.

Covers Task 1 of hack/plans/2026-03-15-worktree-support.md:
- Registry methods: get/set/clear_lifecycle_hooks, get_lifecycle_hooks_by_directory
- hooks.py: run_lifecycle_hooks, run_post_worktree_hooks
- VALID_HOOK_TYPES guard test
- CLI: summon hooks show/set/clear
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.sessions.registry import SessionRegistry

# ---------------------------------------------------------------------------
# Registry: get/set/clear_lifecycle_hooks
# ---------------------------------------------------------------------------


class TestLifecycleHooksDefaultEmpty:
    async def test_lifecycle_hooks_default_empty_global(self, registry):
        """No hooks configured returns empty list for any hook type."""
        result = await registry.get_lifecycle_hooks("worktree_create")
        assert result == []

    async def test_lifecycle_hooks_default_empty_per_project(self, registry, tmp_path):
        project_id = await registry.add_project("test-proj", str(tmp_path))
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == []

    async def test_lifecycle_hooks_project_up_default_empty(self, registry):
        result = await registry.get_lifecycle_hooks("project_up")
        assert result == []

    async def test_lifecycle_hooks_project_down_default_empty(self, registry):
        result = await registry.get_lifecycle_hooks("project_down")
        assert result == []


class TestLifecycleHooksRoundtrip:
    async def test_set_and_get_global_hooks(self, registry):
        """set_lifecycle_hooks + get_lifecycle_hooks roundtrip for global."""
        hooks = {"worktree_create": ["ln -sfn $PROJECT_ROOT/hack hack"]}
        await registry.set_lifecycle_hooks(hooks)
        result = await registry.get_lifecycle_hooks("worktree_create")
        assert result == ["ln -sfn $PROJECT_ROOT/hack hack"]

    async def test_set_and_get_per_project_hooks(self, registry, tmp_path):
        project_id = await registry.add_project("rt-proj", str(tmp_path))
        hooks = {"worktree_create": ["echo hello"], "project_up": ["make install"]}
        await registry.set_lifecycle_hooks(hooks, project_id=project_id)
        assert await registry.get_lifecycle_hooks("worktree_create", project_id=project_id) == [
            "echo hello"
        ]
        assert await registry.get_lifecycle_hooks("project_up", project_id=project_id) == [
            "make install"
        ]

    async def test_set_multiple_hooks_per_type(self, registry):
        hooks = {"worktree_create": ["cmd1", "cmd2", "cmd3"]}
        await registry.set_lifecycle_hooks(hooks)
        result = await registry.get_lifecycle_hooks("worktree_create")
        assert result == ["cmd1", "cmd2", "cmd3"]

    async def test_set_overwrites_existing(self, registry):
        await registry.set_lifecycle_hooks({"worktree_create": ["old-cmd"]})
        await registry.set_lifecycle_hooks({"worktree_create": ["new-cmd"]})
        result = await registry.get_lifecycle_hooks("worktree_create")
        assert result == ["new-cmd"]

    async def test_set_empty_list_per_type(self, registry):
        hooks = {"worktree_create": [], "project_up": ["make up"]}
        await registry.set_lifecycle_hooks(hooks)
        assert await registry.get_lifecycle_hooks("worktree_create") == []
        assert await registry.get_lifecycle_hooks("project_up") == ["make up"]


class TestLifecycleHooksValidatesHookTypes:
    async def test_rejects_unknown_hook_type_in_set(self, registry):
        with pytest.raises((ValueError, KeyError)):
            await registry.set_lifecycle_hooks({"nonexistent_hook": ["cmd"]})

    async def test_rejects_unknown_hook_type_in_get(self, registry):
        with pytest.raises((ValueError, KeyError)):
            await registry.get_lifecycle_hooks("nonexistent_hook")

    async def test_accepts_all_valid_hook_types(self, registry):
        hooks = {
            "worktree_create": ["cmd1"],
            "project_up": ["cmd2"],
            "project_down": ["cmd3"],
        }
        # Should not raise
        await registry.set_lifecycle_hooks(hooks)


class TestLifecycleHooksValidatesCommands:
    async def test_rejects_empty_string_commands(self, registry):
        with pytest.raises(ValueError, match="empty"):
            await registry.set_lifecycle_hooks({"worktree_create": [""]})

    async def test_rejects_non_string_items(self, registry):
        with pytest.raises((ValueError, TypeError)):
            await registry.set_lifecycle_hooks({"worktree_create": [42]})  # type: ignore[list-item]

    async def test_rejects_none_items(self, registry):
        with pytest.raises((ValueError, TypeError)):
            await registry.set_lifecycle_hooks({"worktree_create": [None]})  # type: ignore[list-item]


class TestLifecycleHooksValidatesValueTypes:
    async def test_rejects_non_list_string_value(self, registry):
        with pytest.raises((ValueError, TypeError)):
            await registry.set_lifecycle_hooks({"worktree_create": "echo hello"})  # type: ignore[dict-item]

    async def test_rejects_non_list_int_value(self, registry):
        with pytest.raises((ValueError, TypeError)):
            await registry.set_lifecycle_hooks({"worktree_create": 123})  # type: ignore[dict-item]


class TestLifecycleHooksClearLifecycleHooks:
    async def test_clear_global_hooks(self, registry):
        await registry.set_lifecycle_hooks({"worktree_create": ["cmd"]})
        await registry.clear_lifecycle_hooks()
        result = await registry.get_lifecycle_hooks("worktree_create")
        assert result == []

    async def test_clear_project_hooks(self, registry, tmp_path):
        project_id = await registry.add_project("clear-proj", str(tmp_path))
        await registry.set_lifecycle_hooks({"worktree_create": ["cmd"]}, project_id=project_id)
        await registry.clear_lifecycle_hooks(project_id=project_id)
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == []

    async def test_clear_sets_null(self, registry):
        """clear_lifecycle_hooks sets hooks to NULL, not empty JSON."""
        await registry.set_lifecycle_hooks({"worktree_create": ["cmd"]})
        await registry.clear_lifecycle_hooks()
        # After clear, get_lifecycle_hooks returns [] (NULL → fall back → no global → [])
        assert await registry.get_lifecycle_hooks("worktree_create") == []


class TestLifecycleHooksGlobalFallback:
    async def test_project_null_falls_back_to_global(self, registry, tmp_path):
        """Per-project NULL falls back to global hooks."""
        await registry.set_lifecycle_hooks({"worktree_create": ["global-cmd"]})
        project_id = await registry.add_project("fallback-proj", str(tmp_path))
        # Project has no hooks set (NULL) — should fall back to global
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == ["global-cmd"]

    async def test_project_hooks_after_clear_fall_back_to_global(self, registry, tmp_path):
        """After clearing project hooks, falls back to global again."""
        await registry.set_lifecycle_hooks({"worktree_create": ["global-cmd"]})
        project_id = await registry.add_project("clear-fb-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["project-cmd"]}, project_id=project_id
        )
        await registry.clear_lifecycle_hooks(project_id=project_id)
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == ["global-cmd"]


class TestLifecycleHooksExplicitEmptyOverrides:
    async def test_explicit_empty_dict_overrides_global(self, registry, tmp_path):
        """Per-project {} explicitly overrides global with no hooks."""
        await registry.set_lifecycle_hooks({"worktree_create": ["global-cmd"]})
        project_id = await registry.add_project("explicit-empty-proj", str(tmp_path))
        # Setting an empty hooks dict (no hook types → empty for all types)
        await registry.set_lifecycle_hooks({}, project_id=project_id)
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == []

    async def test_explicit_empty_list_suppresses_global(self, registry, tmp_path):
        """Per-project {"worktree_create": []} suppresses global hooks for that type."""
        await registry.set_lifecycle_hooks({"worktree_create": ["global-cmd"]})
        project_id = await registry.add_project("suppress-proj", str(tmp_path))
        await registry.set_lifecycle_hooks({"worktree_create": []}, project_id=project_id)
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == []


class TestGetLifecycleHooksByDirectory:
    async def test_resolves_directory_to_project(self, registry, tmp_path):
        """get_lifecycle_hooks_by_directory resolves directory to project."""
        project_id = await registry.add_project("dir-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["echo $PROJECT_ROOT"]}, project_id=project_id
        )
        result = await registry.get_lifecycle_hooks_by_directory("worktree_create", str(tmp_path))
        assert result == ["echo $PROJECT_ROOT"]

    async def test_no_match_returns_empty_list(self, registry, tmp_path):
        """Unknown directory returns empty list."""
        other_dir = tmp_path / "nonexistent"
        result = await registry.get_lifecycle_hooks_by_directory("worktree_create", str(other_dir))
        assert result == []

    async def test_resolves_path_normalize(self, registry, tmp_path):
        """Path is resolved/normalized before matching."""
        project_id = await registry.add_project("norm-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["normalize-cmd"]}, project_id=project_id
        )
        # Use unnormalized path with trailing slash — resolve should handle it
        unnormalized = str(tmp_path) + "/"
        result = await registry.get_lifecycle_hooks_by_directory("worktree_create", unnormalized)
        assert result == ["normalize-cmd"]

    async def test_no_project_in_db_returns_empty_list(self, registry, tmp_path):
        """get_lifecycle_hooks_by_directory returns [] when no projects exist."""
        result = await registry.get_lifecycle_hooks_by_directory("worktree_create", str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# VALID_HOOK_TYPES guard test
# ---------------------------------------------------------------------------


class TestValidHookTypesPinned:
    def test_valid_hook_types_pinned(self):
        """Guard test: VALID_HOOK_TYPES frozenset must not change without review."""
        from summon_claude.sessions.hooks import VALID_HOOK_TYPES

        expected = frozenset({"worktree_create", "project_up", "project_down"})
        assert expected == VALID_HOOK_TYPES, (
            f"VALID_HOOK_TYPES changed: {VALID_HOOK_TYPES!r} != {expected!r}. "
            "Update this test if the change is intentional."
        )


# ---------------------------------------------------------------------------
# run_lifecycle_hooks
# ---------------------------------------------------------------------------


class TestRunLifecycleHooksSuccess:
    async def test_runs_commands_in_correct_cwd(self, tmp_path):
        """run_lifecycle_hooks executes commands in the given cwd."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        output_file = tmp_path / "cwd_check.txt"
        hooks = [f"pwd > {output_file}"]
        errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)

        assert errors == []
        assert output_file.exists()
        cwd_output = output_file.read_text().strip()
        assert Path(cwd_output).resolve() == tmp_path.resolve()

    async def test_runs_multiple_commands(self, tmp_path):
        """All commands in the list run."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        file_a = tmp_path / "a.txt"
        file_b = tmp_path / "b.txt"
        hooks = [f"touch {file_a}", f"touch {file_b}"]
        errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)

        assert errors == []
        assert file_a.exists()
        assert file_b.exists()

    async def test_empty_hooks_list_returns_empty_errors(self, tmp_path):
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        errors = await run_lifecycle_hooks("worktree_create", tmp_path, [])
        assert errors == []


class TestRunLifecycleHooksFailureContinues:
    async def test_failing_hook_does_not_abort_remaining(self, tmp_path):
        """A failing hook does not abort subsequent hooks."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        sentinel = tmp_path / "sentinel.txt"
        hooks = ["exit 1", f"touch {sentinel}"]
        errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)

        # The sentinel file should exist — hook 2 ran despite hook 1 failing
        assert sentinel.exists()
        # And there should be one error reported
        assert len(errors) == 1


class TestRunLifecycleHooksReturnsErrors:
    async def test_returns_error_messages_for_failed_hooks(self, tmp_path):
        """Failed hooks produce error messages in the returned list."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        hooks = ["exit 42"]
        errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)
        assert len(errors) == 1
        assert errors[0]  # non-empty error message

    async def test_returns_empty_for_successful_hooks(self, tmp_path):
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        hooks = ["true"]
        errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)
        assert errors == []

    async def test_mixed_success_and_failure(self, tmp_path):
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        hooks = ["true", "exit 1", "true", "exit 2"]
        errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)
        assert len(errors) == 2


class TestRunLifecycleHooksTimeoutKillsProcess:
    async def test_timeout_kills_subprocess_and_reports_error(self, tmp_path):
        """Subprocess is killed after 30s timeout; error is returned."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        # Patch the timeout to 0.1s so the test doesn't actually wait 30s
        with patch("summon_claude.sessions.hooks._HOOK_TIMEOUT_SECONDS", 0.1):
            hooks = ["sleep 100"]
            errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)

        assert len(errors) == 1
        assert "timed out" in errors[0].lower() or "timeout" in errors[0].lower()

    async def test_timeout_does_not_abort_next_hook(self, tmp_path):
        """After a timeout, remaining hooks still run."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        sentinel = tmp_path / "after_timeout.txt"
        with patch("summon_claude.sessions.hooks._HOOK_TIMEOUT_SECONDS", 0.1):
            hooks = ["sleep 100", f"touch {sentinel}"]
            errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)

        assert sentinel.exists()
        assert len(errors) == 1


class TestRunLifecycleHooksProjectRootEnv:
    async def test_project_root_set_in_hook_env(self, tmp_path):
        """$PROJECT_ROOT is set in the hook subprocess environment."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        output_file = tmp_path / "project_root.txt"
        project_root = tmp_path / "the_project_root"
        project_root.mkdir()

        hooks = [f"echo $PROJECT_ROOT > {output_file}"]
        errors = await run_lifecycle_hooks(
            "worktree_create", tmp_path, hooks, project_root=project_root
        )

        assert errors == []
        assert output_file.exists()
        recorded = output_file.read_text().strip()
        assert recorded == str(project_root)

    async def test_project_root_defaults_to_cwd_when_not_given(self, tmp_path):
        """When project_root is None, $PROJECT_ROOT defaults to cwd."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        output_file = tmp_path / "default_root.txt"
        hooks = [f"echo $PROJECT_ROOT > {output_file}"]
        errors = await run_lifecycle_hooks("worktree_create", tmp_path, hooks)

        assert errors == []
        recorded = output_file.read_text().strip()
        assert Path(recorded).resolve() == tmp_path.resolve()

    async def test_project_root_with_spaces(self, tmp_path):
        """$PROJECT_ROOT containing spaces is handled correctly."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        spaced_dir = tmp_path / "path with spaces"
        spaced_dir.mkdir()
        output_file = tmp_path / "spaces_check.txt"

        hooks = [f'echo "$PROJECT_ROOT" > {output_file}']
        errors = await run_lifecycle_hooks(
            "worktree_create", tmp_path, hooks, project_root=spaced_dir
        )

        assert errors == []
        recorded = output_file.read_text().strip()
        assert "path with spaces" in recorded


# ---------------------------------------------------------------------------
# run_post_worktree_hooks integration (CLI entry point)
# ---------------------------------------------------------------------------


class TestRunPostWorktreeHooksIntegration:
    def _make_mock_registry(self, hooks: list[str]) -> MagicMock:
        """Build a mock SessionRegistry async context manager returning given hooks."""
        mock_reg_instance = AsyncMock()
        mock_reg_instance.__aenter__ = AsyncMock(return_value=mock_reg_instance)
        mock_reg_instance.__aexit__ = AsyncMock(return_value=False)
        mock_reg_instance.get_lifecycle_hooks_by_directory = AsyncMock(return_value=hooks)
        return MagicMock(return_value=mock_reg_instance)

    def test_run_post_worktree_hooks_returns_zero_on_success(self, tmp_path):
        """run_post_worktree_hooks returns 0 on success."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        mock_reg_cls = self._make_mock_registry(["true"])
        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0

    def test_run_post_worktree_hooks_returns_zero_on_hook_failure(self, tmp_path):
        """run_post_worktree_hooks returns 0 even when hooks fail."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        mock_reg_cls = self._make_mock_registry(["exit 1"])
        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0

    def test_run_post_worktree_hooks_returns_zero_when_no_project_match(self, tmp_path):
        """run_post_worktree_hooks returns 0 cleanly when no project matches the CWD."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        mock_reg_cls = self._make_mock_registry([])
        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0

    def test_run_post_worktree_hooks_returns_zero_when_not_in_git_worktree(self, tmp_path):
        """run_post_worktree_hooks returns 0 when _get_worktree_project_root returns None."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        with patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=None):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0


# ---------------------------------------------------------------------------
# Hooks CLI commands: show, set, clear
# ---------------------------------------------------------------------------


class TestHooksCliShowEmpty:
    def test_hooks_cli_show_empty(self):
        """summon hooks show prints 'No hooks configured' when no hooks set."""
        from click.testing import CliRunner

        from summon_claude.cli import cli

        runner = CliRunner()
        with patch(
            "summon_claude.sessions.registry.SessionRegistry.get_lifecycle_hooks",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = runner.invoke(cli, ["hooks", "show"])

        assert result.exit_code == 0
        assert "no" in result.output.lower() and "lifecycle hooks" in result.output.lower()


class TestHooksCliSetRoundtrip:
    def test_hooks_cli_set_roundtrip(self, tmp_path):
        """Set hooks via CLI, verify they are stored and retrievable via show."""

        from click.testing import CliRunner

        from summon_claude.cli import cli

        db_path = tmp_path / "roundtrip.db"
        runner = CliRunner()

        hooks_json = json.dumps({"worktree_create": ["echo set-via-cli"]})

        with patch("summon_claude.sessions.registry.default_db_path", return_value=db_path):
            set_result = runner.invoke(cli, ["hooks", "set", hooks_json])
            assert set_result.exit_code == 0, set_result.output

            show_result = runner.invoke(cli, ["hooks", "show"])
            assert show_result.exit_code == 0, show_result.output
            assert "echo set-via-cli" in show_result.output


class TestHooksCliClear:
    def test_hooks_cli_clear_removes_hooks(self, tmp_path):
        """summon hooks clear removes configured hooks."""
        from click.testing import CliRunner

        from summon_claude.cli import cli

        db_path = tmp_path / "clear_test.db"
        runner = CliRunner()
        hooks_json = json.dumps({"worktree_create": ["cmd-to-clear"]})

        with patch("summon_claude.sessions.registry.default_db_path", return_value=db_path):
            runner.invoke(cli, ["hooks", "set", "--json", hooks_json])
            clear_result = runner.invoke(cli, ["hooks", "clear"])
            assert clear_result.exit_code == 0, clear_result.output

            show_result = runner.invoke(cli, ["hooks", "show"])
            assert "cmd-to-clear" not in show_result.output

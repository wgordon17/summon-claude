"""Tests for lifecycle hooks DB schema, runner, and CLI entry point.

Covers Task 1 of hack/plans/2026-03-15-worktree-support.md:
- Registry methods: get/set/clear_lifecycle_hooks, get_lifecycle_hooks_by_directory
- hooks.py: run_lifecycle_hooks, run_post_worktree_hooks
- VALID_HOOK_TYPES guard test
- CLI: summon hooks show/set/clear
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import click
import pytest

from summon_claude.sessions.registry import SessionRegistry
from tests.conftest import make_hooks_mock_registry as _make_hooks_mock_registry

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


class TestIncludeGlobalToken:
    async def test_include_global_splices_global_hooks(self, registry, tmp_path):
        """$INCLUDE_GLOBAL in project hooks is replaced with global hooks."""
        await registry.set_lifecycle_hooks({"worktree_create": ["global-cmd"]})
        project_id = await registry.add_project("include-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["$INCLUDE_GLOBAL", "project-cmd"]}, project_id=project_id
        )
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == ["global-cmd", "project-cmd"]

    async def test_include_global_preserves_ordering(self, registry, tmp_path):
        """$INCLUDE_GLOBAL expands in-place, preserving surrounding commands."""
        await registry.set_lifecycle_hooks({"worktree_create": ["g1", "g2"]})
        project_id = await registry.add_project("order-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["before", "$INCLUDE_GLOBAL", "after"]}, project_id=project_id
        )
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == ["before", "g1", "g2", "after"]

    async def test_include_global_with_no_global_hooks(self, registry, tmp_path):
        """$INCLUDE_GLOBAL expands to nothing when no global hooks configured."""
        project_id = await registry.add_project("no-global-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["$INCLUDE_GLOBAL", "project-only"]}, project_id=project_id
        )
        result = await registry.get_lifecycle_hooks("worktree_create", project_id=project_id)
        assert result == ["project-only"]

    async def test_include_global_rejected_in_global_hooks(self, registry):
        """$INCLUDE_GLOBAL in global hooks raises ValueError."""
        with pytest.raises(ValueError, match="global hooks"):
            await registry.set_lifecycle_hooks(
                {"worktree_create": ["$INCLUDE_GLOBAL", "global-cmd"]}
            )

    async def test_include_global_works_with_by_directory(self, registry, tmp_path):
        """$INCLUDE_GLOBAL expands in get_lifecycle_hooks_by_directory too."""
        await registry.set_lifecycle_hooks({"worktree_create": ["global-cmd"]})
        project_id = await registry.add_project("dir-include-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["$INCLUDE_GLOBAL", "local-cmd"]}, project_id=project_id
        )
        result = await registry.get_lifecycle_hooks_by_directory("worktree_create", tmp_path)
        assert result == ["global-cmd", "local-cmd"]


class TestGetRawHooksJson:
    async def test_raw_preserves_include_global_token(self, registry, tmp_path):
        """get_raw_hooks_json returns unexpanded $INCLUDE_GLOBAL."""
        project_id = await registry.add_project("raw-proj", str(tmp_path))
        await registry.set_lifecycle_hooks(
            {"worktree_create": ["$INCLUDE_GLOBAL", "make setup"]}, project_id=project_id
        )
        raw = await registry.get_raw_hooks_json(project_id=project_id)
        assert raw is not None
        data = json.loads(raw)
        assert "$INCLUDE_GLOBAL" in data["worktree_create"]

    async def test_raw_returns_none_for_null_hooks(self, registry, tmp_path):
        """get_raw_hooks_json returns None when hooks column is NULL."""
        project_id = await registry.add_project("null-proj", str(tmp_path))
        raw = await registry.get_raw_hooks_json(project_id=project_id)
        assert raw is None

    async def test_raw_returns_none_for_nonexistent_project(self, registry):
        """get_raw_hooks_json returns None for unknown project_id."""
        raw = await registry.get_raw_hooks_json(project_id="nonexistent-id")
        assert raw is None

    async def test_raw_global_hooks(self, registry):
        """get_raw_hooks_json(project_id=None) returns global hooks."""
        await registry.set_lifecycle_hooks({"worktree_create": ["global-cmd"]})
        raw = await registry.get_raw_hooks_json(project_id=None)
        assert raw is not None
        data = json.loads(raw)
        assert data["worktree_create"] == ["global-cmd"]

    async def test_raw_global_returns_none_when_empty(self, registry):
        """get_raw_hooks_json(project_id=None) returns None when no global hooks set."""
        raw = await registry.get_raw_hooks_json(project_id=None)
        assert raw is None


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
    def test_run_post_worktree_hooks_returns_zero_on_success(self, tmp_path):
        """run_post_worktree_hooks returns 0 on success."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        mock_reg_cls = _make_hooks_mock_registry(["true"])
        with (
            patch("summon_claude.sessions.hooks.get_git_main_repo_root", return_value=tmp_path),
            patch("summon_claude.sessions.hooks.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0

    def test_run_post_worktree_hooks_returns_zero_on_hook_failure(self, tmp_path):
        """run_post_worktree_hooks returns 0 even when hooks fail."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        mock_reg_cls = _make_hooks_mock_registry(["exit 1"])
        with (
            patch("summon_claude.sessions.hooks.get_git_main_repo_root", return_value=tmp_path),
            patch("summon_claude.sessions.hooks.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0

    def test_run_post_worktree_hooks_returns_zero_when_no_project_match(self, tmp_path):
        """run_post_worktree_hooks returns 0 cleanly when no project matches the CWD."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        mock_reg_cls = _make_hooks_mock_registry([])
        with (
            patch("summon_claude.sessions.hooks.get_git_main_repo_root", return_value=tmp_path),
            patch("summon_claude.sessions.hooks.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0

    def test_run_post_worktree_hooks_returns_zero_when_not_in_git_worktree(self, tmp_path):
        """run_post_worktree_hooks returns 0 when get_git_main_repo_root returns None."""
        from summon_claude.sessions.hooks import run_post_worktree_hooks

        with patch("summon_claude.sessions.hooks.get_git_main_repo_root", return_value=None):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0


# ---------------------------------------------------------------------------
# _list_worktree_paths unit tests
# ---------------------------------------------------------------------------


class TestListWorktreePaths:
    """Unit tests for _list_worktree_paths — the subprocess bridge for security validation."""

    def test_returns_paths_from_valid_porcelain_output(self, tmp_path):
        """Parses 'worktree <path>' lines from git porcelain output."""
        import subprocess as _subprocess

        from summon_claude.sessions.hooks import _list_worktree_paths

        home = Path.home()
        main_path = home / "projects" / "repo"
        wt_path = home / "projects" / "repo" / ".claude" / "worktrees" / "feat-a"

        fake_output = (
            f"worktree {main_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {wt_path}\n"
            "HEAD def456\n"
            "branch refs/heads/worktree-feat-a\n"
        )
        mock_result = _subprocess.CompletedProcess(
            args=["git", "worktree", "list", "--porcelain"],
            returncode=0,
            stdout=fake_output,
            stderr="",
        )
        with patch("summon_claude.sessions.hooks.subprocess.run", return_value=mock_result):
            paths = _list_worktree_paths(tmp_path)

        assert len(paths) == 2
        assert paths[0] == main_path.resolve()
        assert paths[1] == wt_path.resolve()

    def test_returns_empty_list_on_nonzero_returncode(self, tmp_path):
        """Returns [] (fail-closed) when git exits with non-zero status."""
        import subprocess as _subprocess

        from summon_claude.sessions.hooks import _list_worktree_paths

        mock_result = _subprocess.CompletedProcess(
            args=["git", "worktree", "list", "--porcelain"],
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository",
        )
        with patch("summon_claude.sessions.hooks.subprocess.run", return_value=mock_result):
            paths = _list_worktree_paths(tmp_path)

        assert paths == []

    def test_returns_empty_list_on_exception(self, tmp_path):
        """Returns [] (fail-closed) when subprocess.run raises (e.g. FileNotFoundError)."""
        from summon_claude.sessions.hooks import _list_worktree_paths

        with patch(
            "summon_claude.sessions.hooks.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            paths = _list_worktree_paths(tmp_path)

        assert paths == []

    def test_skips_path_outside_home_and_logs_warning(self, tmp_path, caplog):
        """A worktree path outside Path.home() is dropped and a WARNING is logged."""
        import logging
        import subprocess as _subprocess

        from summon_claude.sessions.hooks import _list_worktree_paths

        home = Path.home()
        in_home_path = home / "projects" / "repo"
        out_of_home_path = Path("/opt/outside-home/repo")

        fake_output = (
            f"worktree {in_home_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {out_of_home_path}\n"
            "HEAD def456\n"
            "branch refs/heads/other\n"
        )
        mock_result = _subprocess.CompletedProcess(
            args=["git", "worktree", "list", "--porcelain"],
            returncode=0,
            stdout=fake_output,
            stderr="",
        )
        with (
            patch("summon_claude.sessions.hooks.subprocess.run", return_value=mock_result),
            caplog.at_level(logging.WARNING, logger="summon_claude.sessions.hooks"),
        ):
            paths = _list_worktree_paths(tmp_path)

        assert in_home_path.resolve() in paths
        assert out_of_home_path.resolve() not in paths
        assert any("outside home" in record.getMessage() for record in caplog.records), (
            "Expected a WARNING about path outside home directory"
        )


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
            set_result = runner.invoke(cli, ["hooks", "set", hooks_json])
            assert set_result.exit_code == 0, f"set failed: {set_result.output}"

            # Verify hooks were actually stored before clearing
            pre_clear = runner.invoke(cli, ["hooks", "show"])
            assert pre_clear.exit_code == 0, f"show failed: {pre_clear.output}"
            assert "cmd-to-clear" in pre_clear.output, (
                f"hooks set did not persist: {pre_clear.output}"
            )

            clear_result = runner.invoke(cli, ["hooks", "clear"])
            assert clear_result.exit_code == 0, clear_result.output

            show_result = runner.invoke(cli, ["hooks", "show"])
            assert show_result.exit_code == 0, show_result.output
            assert "cmd-to-clear" not in show_result.output


class TestHooksCliProjectFlag:
    def test_hooks_cli_set_show_clear_with_project_flag(self, tmp_path):
        """summon hooks set/show/clear --project round-trip with per-project hooks."""
        import asyncio

        from click.testing import CliRunner

        from summon_claude.cli import cli

        db_path = tmp_path / "project_flag.db"
        runner = CliRunner()
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        # Create a project directly via registry (project add CLI needs daemon).
        async def _create_project() -> str:
            pid = ""
            async with SessionRegistry(db_path=db_path) as reg:
                pid = await reg.add_project("flag-test-proj", str(project_dir))
            return pid

        project_id = asyncio.run(_create_project())

        hooks_json = json.dumps({"worktree_create": ["echo project-hook"]})

        with patch("summon_claude.sessions.registry.default_db_path", return_value=db_path):
            # Set per-project hooks
            set_result = runner.invoke(cli, ["hooks", "set", "--project", project_id, hooks_json])
            assert set_result.exit_code == 0, f"set failed: {set_result.output}"

            # Show per-project hooks
            show_result = runner.invoke(cli, ["hooks", "show", "--project", project_id])
            assert show_result.exit_code == 0, f"show failed: {show_result.output}"
            assert "echo project-hook" in show_result.output

            # Global hooks should NOT have the project hook
            global_show = runner.invoke(cli, ["hooks", "show"])
            assert global_show.exit_code == 0, global_show.output
            assert "echo project-hook" not in global_show.output

            # Clear per-project hooks
            clear_result = runner.invoke(cli, ["hooks", "clear", "--project", project_id])
            assert clear_result.exit_code == 0, f"clear failed: {clear_result.output}"

            # Verify cleared
            post_clear = runner.invoke(cli, ["hooks", "show", "--project", project_id])
            assert post_clear.exit_code == 0, post_clear.output
            assert "echo project-hook" not in post_clear.output


class TestRunProjectHooksProjectIdsFilter:
    async def test_project_ids_filter_runs_only_specified_projects(self, tmp_path):
        """_run_project_hooks with project_ids only runs hooks for those projects."""
        from summon_claude.cli.project import _run_project_hooks

        dir_a = tmp_path / "proj-a"
        dir_b = tmp_path / "proj-b"
        dir_a.mkdir()
        dir_b.mkdir()
        sentinel_a = tmp_path / "ran_a.txt"
        sentinel_b = tmp_path / "ran_b.txt"

        db_path = tmp_path / "filter.db"
        pid_a = ""
        async with SessionRegistry(db_path=db_path) as reg:
            pid_a = await reg.add_project("proj-a", str(dir_a))
            pid_b = await reg.add_project("proj-b", str(dir_b))
            await reg.set_lifecycle_hooks(
                {"project_down": [f"touch {sentinel_a}"]}, project_id=pid_a
            )
            await reg.set_lifecycle_hooks(
                {"project_down": [f"touch {sentinel_b}"]}, project_id=pid_b
            )

        with patch("summon_claude.sessions.registry.default_db_path", return_value=db_path):
            await _run_project_hooks("project_down", project_ids=[pid_a])

        assert sentinel_a.exists(), "proj-a hook should have run"
        assert not sentinel_b.exists(), "proj-b hook should NOT have run"

    async def test_project_ids_none_runs_all_projects(self, tmp_path):
        """_run_project_hooks without project_ids runs hooks for all projects."""
        from summon_claude.cli.project import _run_project_hooks

        dir_a = tmp_path / "all-a"
        dir_b = tmp_path / "all-b"
        dir_a.mkdir()
        dir_b.mkdir()
        sentinel_a = tmp_path / "all_ran_a.txt"
        sentinel_b = tmp_path / "all_ran_b.txt"

        db_path = tmp_path / "all.db"
        async with SessionRegistry(db_path=db_path) as reg:
            pid_a = await reg.add_project("all-a", str(dir_a))
            pid_b = await reg.add_project("all-b", str(dir_b))
            await reg.set_lifecycle_hooks(
                {"project_down": [f"touch {sentinel_a}"]}, project_id=pid_a
            )
            await reg.set_lifecycle_hooks(
                {"project_down": [f"touch {sentinel_b}"]}, project_id=pid_b
            )

        with patch("summon_claude.sessions.registry.default_db_path", return_value=db_path):
            await _run_project_hooks("project_down")

        assert sentinel_a.exists(), "proj-a hook should have run"
        assert sentinel_b.exists(), "proj-b hook should have run"


# ---------------------------------------------------------------------------
# Regression: clear_workflow_defaults preserves hooks (#5)
# ---------------------------------------------------------------------------


class TestClearWorkflowDefaultsPreservesHooks:
    async def test_clear_workflow_defaults_preserves_hooks(self, registry):
        """Clearing workflow instructions must not destroy lifecycle hooks."""
        await registry.set_lifecycle_hooks({"worktree_create": ["hook-cmd"]})
        await registry.set_workflow_defaults("some instructions")
        await registry.clear_workflow_defaults()
        assert await registry.get_workflow_defaults() == ""
        assert await registry.get_lifecycle_hooks("worktree_create") == ["hook-cmd"]


# ---------------------------------------------------------------------------
# Coverage: set_lifecycle_hooks KeyError for unknown project (#7)
# ---------------------------------------------------------------------------


class TestSetLifecycleHooksKeyError:
    async def test_set_raises_key_error_for_unknown_project(self, registry):
        """set_lifecycle_hooks raises KeyError when project_id does not exist."""
        with pytest.raises(KeyError, match="nonexistent-id"):
            await registry.set_lifecycle_hooks(
                {"worktree_create": ["cmd"]}, project_id="nonexistent-id"
            )


# ---------------------------------------------------------------------------
# Coverage: run_lifecycle_hooks ValueError for invalid hook_type (#8)
# ---------------------------------------------------------------------------


class TestRunLifecycleHooksInvalidHookType:
    async def test_raises_value_error_for_invalid_hook_type(self, tmp_path):
        """run_lifecycle_hooks raises ValueError for unknown hook types."""
        from summon_claude.sessions.hooks import run_lifecycle_hooks

        with pytest.raises(ValueError, match="invalid_type"):
            await run_lifecycle_hooks("invalid_type", tmp_path, ["cmd"])


# ---------------------------------------------------------------------------
# stop_project_managers: daemon not running + name filter
# ---------------------------------------------------------------------------


class TestStopProjectManagersDaemonDown:
    async def test_name_filter_applied_when_daemon_not_running(self, tmp_path):
        """project down --name X with no daemon only runs hooks for project X."""
        from summon_claude.cli.project import stop_project_managers

        dir_a = tmp_path / "proj-a"
        dir_b = tmp_path / "proj-b"
        dir_a.mkdir()
        dir_b.mkdir()
        sentinel_a = tmp_path / "down_a.txt"
        sentinel_b = tmp_path / "down_b.txt"

        db_path = tmp_path / "daemon_down.db"
        async with SessionRegistry(db_path=db_path) as reg:
            await reg.add_project("proj-a", str(dir_a))
            await reg.add_project("proj-b", str(dir_b))
            await reg.set_lifecycle_hooks(
                {"project_down": [f"touch {sentinel_a}"]},
                project_id=(await reg.list_projects())[0]["project_id"],
            )
            await reg.set_lifecycle_hooks(
                {"project_down": [f"touch {sentinel_b}"]},
                project_id=(await reg.list_projects())[1]["project_id"],
            )

        with (
            patch("summon_claude.cli.project.is_daemon_running", return_value=False),
            patch("summon_claude.sessions.registry.default_db_path", return_value=db_path),
        ):
            result = await stop_project_managers(name="proj-a")

        assert result == []
        assert sentinel_a.exists(), "proj-a hook should have run"
        assert not sentinel_b.exists(), "proj-b hook should NOT have run"

    async def test_nonexistent_name_raises_error(self, tmp_path):
        """project down --name nonexistent with no daemon raises ClickException."""
        from summon_claude.cli.project import stop_project_managers

        dir_a = tmp_path / "proj-a"
        dir_a.mkdir()
        sentinel = tmp_path / "should_not_run.txt"

        db_path = tmp_path / "no_match.db"
        async with SessionRegistry(db_path=db_path) as reg:
            pid = await reg.add_project("proj-a", str(dir_a))
            await reg.set_lifecycle_hooks({"project_down": [f"touch {sentinel}"]}, project_id=pid)

        with (
            patch("summon_claude.cli.project.is_daemon_running", return_value=False),
            patch("summon_claude.sessions.registry.default_db_path", return_value=db_path),
            pytest.raises(click.ClickException, match="nonexistent"),
        ):
            await stop_project_managers(name="nonexistent")

        assert not sentinel.exists(), "no hooks should run for nonexistent project"

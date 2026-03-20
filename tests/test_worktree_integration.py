"""Integration tests for the worktree hook flow.

Covers Task 3 of the worktree support plan:
- End-to-end hook bridge: configure hooks → trigger post-worktree → verify execution
- Shell wrapper scripts: fast-path exit (no DB, no projects), spaces in paths
- Project override and explicit-empty semantics
- Hook failure non-fatal
- symlink use-case (ln -sfn $PROJECT_ROOT/hack hack)
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.cli.hooks import POST_WORKTREE_TEMPLATE, PRE_WORKTREE_TEMPLATE
from summon_claude.sessions.hooks import run_lifecycle_hooks, run_post_worktree_hooks
from summon_claude.sessions.registry import SessionRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_script(path: Path, content: str) -> Path:
    """Write a bash script to path and make it executable."""
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_mock_registry(hooks: list[str]) -> MagicMock:
    """Build a mock SessionRegistry async context manager returning given hooks."""
    mock_reg_instance = AsyncMock()
    mock_reg_instance.__aenter__ = AsyncMock(return_value=mock_reg_instance)
    mock_reg_instance.__aexit__ = AsyncMock(return_value=False)
    mock_reg_instance.get_lifecycle_hooks_by_directory = AsyncMock(return_value=hooks)
    return MagicMock(return_value=mock_reg_instance)


# ---------------------------------------------------------------------------
# 1. End-to-end: configure hooks in real DB → run → verify output
# ---------------------------------------------------------------------------


class TestWorktreeE2eHookBridge:
    async def test_hooks_run_against_real_registry(self, tmp_path):
        """Configure hooks in real DB via registry, run via run_post_worktree_hooks.

        run_post_worktree_hooks uses asyncio.run() internally, which cannot be called
        from a running event loop. We run it in a thread to avoid the conflict.
        """
        import concurrent.futures

        project_root = tmp_path / "my-project"
        project_root.mkdir()
        worktree_dir = tmp_path / "my-worktree"
        worktree_dir.mkdir()
        sentinel = worktree_dir / "hook_ran.txt"

        db_path = tmp_path / "e2e.db"
        async with SessionRegistry(db_path=db_path) as registry:
            project_id = await registry.add_project("e2e-proj", str(project_root))
            await registry.set_lifecycle_hooks(
                {"worktree_create": [f"touch {sentinel}"]},
                project_id=project_id,
            )

        def _run_in_thread() -> int:
            with (
                patch(
                    "summon_claude.sessions.hooks._get_worktree_project_root",
                    return_value=project_root,
                ),
                patch("summon_claude.sessions.registry.default_db_path", return_value=db_path),
            ):
                return run_post_worktree_hooks(cwd=worktree_dir)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_in_thread)
            exit_code = future.result(timeout=10)

        assert exit_code == 0
        assert sentinel.exists(), "worktree_create hook did not create sentinel file"


# ---------------------------------------------------------------------------
# 2. run_post_worktree_hooks executes worktree_create hooks
# ---------------------------------------------------------------------------


class TestWorktreePostHookRunsCreateHooks:
    def test_post_hook_executes_configured_commands(self, tmp_path):
        """Configured worktree_create hooks run when post-worktree is triggered."""
        sentinel = tmp_path / "create_hook_ran.txt"
        mock_reg_cls = _make_mock_registry([f"touch {sentinel}"])

        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0
        assert sentinel.exists()

    def test_post_hook_runs_multiple_commands(self, tmp_path):
        """Multiple worktree_create hooks all run."""
        file_a = tmp_path / "a.txt"
        file_b = tmp_path / "b.txt"
        mock_reg_cls = _make_mock_registry([f"touch {file_a}", f"touch {file_b}"])

        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            run_post_worktree_hooks(cwd=tmp_path)

        assert file_a.exists()
        assert file_b.exists()


# ---------------------------------------------------------------------------
# 3. Symlink use-case: ln -sfn $PROJECT_ROOT/hack hack
# ---------------------------------------------------------------------------


class TestWorktreePostHookSymlinksHackDir:
    async def test_symlink_hack_dir_use_case(self, tmp_path):
        """The canonical use case: ln -sfn $PROJECT_ROOT/hack hack in a worktree."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        hack_dir = project_root / "hack"
        hack_dir.mkdir()
        (hack_dir / "notes.md").write_text("project notes")

        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        hook_cmd = "ln -sfn $PROJECT_ROOT/hack hack"
        errors = await run_lifecycle_hooks(
            "worktree_create",
            worktree_dir,
            [hook_cmd],
            project_root=project_root,
        )

        assert errors == [], f"Hook errors: {errors}"
        link = worktree_dir / "hack"
        assert link.is_symlink(), "Expected symlink 'hack' in worktree"
        assert link.resolve() == hack_dir.resolve()
        # Verify the symlink points at the right content
        assert (link / "notes.md").read_text() == "project notes"


# ---------------------------------------------------------------------------
# 4. Hook failure is non-fatal — runner always returns 0
# ---------------------------------------------------------------------------


class TestWorktreePostHookFailureNonFatal:
    def test_hook_failure_returns_zero(self, tmp_path):
        """Hook failures do not crash the runner — exit code is always 0."""
        mock_reg_cls = _make_mock_registry(["exit 42"])

        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0

    def test_failing_hook_does_not_abort_subsequent_hooks(self, tmp_path):
        """A failing hook does not abort subsequent hooks in the list."""
        sentinel = tmp_path / "after_failure.txt"
        mock_reg_cls = _make_mock_registry(["exit 1", f"touch {sentinel}"])

        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=tmp_path)

        assert exit_code == 0
        assert sentinel.exists(), "Hook after failure did not run"


# ---------------------------------------------------------------------------
# 5. Per-project hooks override global defaults
# ---------------------------------------------------------------------------


class TestWorktreeProjectOverride:
    async def test_project_hooks_override_global(self, tmp_path):
        """Per-project hooks are used instead of global defaults."""
        import concurrent.futures

        project_root = tmp_path / "proj"
        project_root.mkdir()

        global_sentinel = tmp_path / "global_ran.txt"
        project_sentinel = tmp_path / "project_ran.txt"

        db_path = tmp_path / "override.db"
        async with SessionRegistry(db_path=db_path) as registry:
            await registry.set_lifecycle_hooks({"worktree_create": [f"touch {global_sentinel}"]})
            project_id = await registry.add_project("override-proj", str(project_root))
            await registry.set_lifecycle_hooks(
                {"worktree_create": [f"touch {project_sentinel}"]},
                project_id=project_id,
            )

        worktree_dir = tmp_path / "wt"
        worktree_dir.mkdir()

        def _run() -> None:
            with (
                patch(
                    "summon_claude.sessions.hooks._get_worktree_project_root",
                    return_value=project_root,
                ),
                patch("summon_claude.sessions.registry.default_db_path", return_value=db_path),
            ):
                run_post_worktree_hooks(cwd=worktree_dir)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_run).result(timeout=10)

        # Project-specific hook ran
        assert project_sentinel.exists()
        # Global hook did NOT run (project overrides it)
        assert not global_sentinel.exists()


# ---------------------------------------------------------------------------
# 6. Explicit empty per-project dict suppresses global hooks
# ---------------------------------------------------------------------------


class TestWorktreeProjectExplicitEmpty:
    async def test_explicit_empty_suppresses_global_hooks(self, tmp_path):
        """Per-project {} prevents global worktree_create hooks from running."""
        import concurrent.futures

        project_root = tmp_path / "proj"
        project_root.mkdir()

        global_sentinel = tmp_path / "global_should_not_run.txt"

        db_path = tmp_path / "explicit_empty.db"
        async with SessionRegistry(db_path=db_path) as registry:
            await registry.set_lifecycle_hooks({"worktree_create": [f"touch {global_sentinel}"]})
            project_id = await registry.add_project("empty-override-proj", str(project_root))
            # Explicit empty JSON object — overrides global with nothing
            await registry.set_lifecycle_hooks({}, project_id=project_id)

        worktree_dir = tmp_path / "wt"
        worktree_dir.mkdir()

        def _run() -> None:
            with (
                patch(
                    "summon_claude.sessions.hooks._get_worktree_project_root",
                    return_value=project_root,
                ),
                patch("summon_claude.sessions.registry.default_db_path", return_value=db_path),
            ):
                run_post_worktree_hooks(cwd=worktree_dir)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_run).result(timeout=10)

        assert not global_sentinel.exists(), (
            "Global hook ran despite project explicit empty override"
        )


# ---------------------------------------------------------------------------
# 7. Shell wrapper: pre-hook exits fast when no DB
# ---------------------------------------------------------------------------


class TestShellPreWorktreeNoDbFastExit:
    def test_pre_hook_exits_zero_when_no_db(self, tmp_path):
        """Pre-hook script exits 0 immediately when DB file does not exist."""
        script = _write_script(tmp_path / "pre.sh", PRE_WORKTREE_TEMPLATE)

        env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "nonexistent_data")}
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0

    def test_pre_hook_exits_zero_when_no_projects(self, tmp_path):
        """Pre-hook exits 0 quickly when DB exists but has no registered projects."""
        # Create a real DB with schema but no projects
        import asyncio

        db_path = tmp_path / "summon" / "registry.db"
        db_path.parent.mkdir(parents=True)

        async def _create_db() -> None:
            async with SessionRegistry(db_path=db_path):
                pass

        asyncio.run(_create_db())

        script = _write_script(tmp_path / "pre.sh", PRE_WORKTREE_TEMPLATE)
        env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}

        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 8. Shell wrapper: post-hook exits fast when no DB
# ---------------------------------------------------------------------------


class TestShellPostWorktreeNoDbFastExit:
    def test_post_hook_exits_zero_when_no_db(self, tmp_path):
        """Post-hook script exits 0 immediately when DB file does not exist."""
        # Use a dummy summon binary path that won't be reached
        content = POST_WORKTREE_TEMPLATE.replace("@@SUMMON_PATH@@", "/nonexistent/summon")
        script = _write_script(tmp_path / "post.sh", content)

        env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "nonexistent_data")}
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0

    def test_post_hook_exits_zero_when_no_projects(self, tmp_path):
        """Post-hook exits 0 quickly when DB has no registered projects."""
        import asyncio

        db_path = tmp_path / "summon" / "registry.db"
        db_path.parent.mkdir(parents=True)

        async def _create_db() -> None:
            async with SessionRegistry(db_path=db_path):
                pass

        asyncio.run(_create_db())

        content = POST_WORKTREE_TEMPLATE.replace("@@SUMMON_PATH@@", "/nonexistent/summon")
        script = _write_script(tmp_path / "post.sh", content)
        env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}

        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0

    def test_post_hook_exits_zero_when_summon_binary_missing(self, tmp_path):
        """Post-hook exits 0 cleanly when summon binary path is not executable."""
        import asyncio

        db_path = tmp_path / "summon" / "registry.db"
        db_path.parent.mkdir(parents=True)

        async def _create_db() -> None:
            async with SessionRegistry(db_path=db_path) as reg:
                await reg.add_project("stale-proj", str(tmp_path))

        asyncio.run(_create_db())

        # Non-existent binary path — [ -x ] gate catches this
        content = POST_WORKTREE_TEMPLATE.replace("@@SUMMON_PATH@@", "/nonexistent/summon")
        script = _write_script(tmp_path / "post.sh", content)
        env = {**os.environ, "XDG_DATA_HOME": str(tmp_path)}

        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 9. Shell wrapper: PROJECT_ROOT with spaces handled correctly
# ---------------------------------------------------------------------------


class TestShellHookProjectRootWithSpaces:
    async def test_project_root_with_spaces_in_hook_env(self, tmp_path):
        """$PROJECT_ROOT containing spaces is passed correctly to hook subprocess."""
        spaced_root = tmp_path / "path with spaces"
        spaced_root.mkdir()
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        output_file = tmp_path / "root_check.txt"
        # The hook must quote $PROJECT_ROOT to handle spaces
        hook_cmd = f'echo "$PROJECT_ROOT" > {output_file}'

        errors = await run_lifecycle_hooks(
            "worktree_create",
            worktree_dir,
            [hook_cmd],
            project_root=spaced_root,
        )

        assert errors == [], f"Hook errors: {errors}"
        assert output_file.exists()
        recorded = output_file.read_text().strip()
        assert "path with spaces" in recorded
        assert str(spaced_root) in recorded

    def test_post_hook_script_cwd_with_spaces(self, tmp_path):
        """Post-hook runs successfully when cwd contains spaces."""
        spaced_dir = tmp_path / "work dir with spaces"
        spaced_dir.mkdir()

        sentinel = spaced_dir / "ran.txt"
        mock_reg_cls = _make_mock_registry([f"touch '{sentinel}'"])

        with (
            patch("summon_claude.sessions.hooks._get_worktree_project_root", return_value=tmp_path),
            patch("summon_claude.sessions.registry.SessionRegistry", mock_reg_cls),
        ):
            exit_code = run_post_worktree_hooks(cwd=spaced_dir)

        assert exit_code == 0
        assert sentinel.exists()

"""Tests for write gate — safe-dir validation and worktree gating."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from helpers import make_mock_slack_client
from summon_claude.config import SummonConfig
from summon_claude.sessions.permissions import (
    _AUTO_APPROVE_TOOLS,
    _WRITE_GATED_TOOLS,
    _WRITE_TOOL_PATH_KEYS,
    PermissionHandler,
    _is_in_safe_dir,
)
from summon_claude.slack.router import ThreadRouter
from tests.test_sessions_permissions import _interactive_auto_approve, _interactive_auto_deny


def _make_config(safe_write_dirs: str = "", debounce_ms: int = 10):
    return SummonConfig.model_validate(
        {
            "slack_bot_token": "xoxb-t",
            "slack_app_token": "xapp-t",
            "slack_signing_secret": "abcdef",
            "permission_debounce_ms": debounce_ms,
            "safe_write_dirs": safe_write_dirs,
        }
    )


def _make_handler(
    safe_write_dirs: str = "",
    project_root: str = "/project",
):
    client = make_mock_slack_client()
    router = ThreadRouter(client)
    config = _make_config(safe_write_dirs=safe_write_dirs)
    handler = PermissionHandler(
        router,
        config,
        authenticated_user_id="U_TEST",
        project_root=project_root,
    )
    return handler, client


class TestIsSafeDir:
    """Unit tests for _is_in_safe_dir path validation."""

    def test_file_inside_safe_dir(self, tmp_path: Path):
        safe = tmp_path / "hack"
        safe.mkdir()
        target = safe / "notes.md"
        target.touch()
        assert _is_in_safe_dir(str(target), ["hack/"], tmp_path) is True

    def test_file_outside_safe_dir(self, tmp_path: Path):
        (tmp_path / "hack").mkdir()
        target = tmp_path / "src" / "main.py"
        target.parent.mkdir()
        target.touch()
        assert _is_in_safe_dir(str(target), ["hack/"], tmp_path) is False

    def test_empty_safe_dirs_returns_false(self, tmp_path: Path):
        target = tmp_path / "anything.txt"
        target.touch()
        assert _is_in_safe_dir(str(target), [], tmp_path) is False

    def test_none_project_root_returns_false(self):
        assert _is_in_safe_dir("/some/file.py", ["hack/"], None) is False

    def test_relative_project_root_returns_false(self):
        assert _is_in_safe_dir("/some/file.py", ["hack/"], Path("relative")) is False

    def test_empty_project_root_returns_false(self):
        assert _is_in_safe_dir("/some/file.py", ["hack/"], Path()) is False

    def test_dotdot_traversal_blocked(self, tmp_path: Path):
        safe = tmp_path / "hack"
        safe.mkdir()
        # ../src/main.py should NOT be in hack/ even though it starts with ../
        assert _is_in_safe_dir(str(safe / ".." / "src" / "main.py"), ["hack/"], tmp_path) is False

    def test_symlink_resolved(self, tmp_path: Path):
        real_dir = tmp_path / "real_safe"
        real_dir.mkdir()
        target = real_dir / "notes.md"
        target.touch()
        link = tmp_path / "link_safe"
        link.symlink_to(real_dir)
        # File accessed via symlink should resolve to the real dir
        assert _is_in_safe_dir(str(link / "notes.md"), ["real_safe/"], tmp_path) is True

    def test_multiple_safe_dirs(self, tmp_path: Path):
        (tmp_path / "hack").mkdir()
        (tmp_path / ".dev").mkdir()
        target = tmp_path / ".dev" / "scratch.py"
        target.touch()
        assert _is_in_safe_dir(str(target), ["hack/", ".dev/"], tmp_path) is True

    def test_relative_file_path_resolved_against_project_root(self, tmp_path: Path):
        safe = tmp_path / "hack"
        safe.mkdir()
        target = safe / "notes.md"
        target.touch()
        # Relative path should be resolved against project_root
        assert _is_in_safe_dir("hack/notes.md", ["hack/"], tmp_path) is True

    def test_absolute_file_path_works(self, tmp_path: Path):
        safe = tmp_path / "hack"
        safe.mkdir()
        target = safe / "notes.md"
        target.touch()
        assert _is_in_safe_dir(str(target), ["hack/"], tmp_path) is True


class TestWriteGateGuards:
    """Pin constants for the write gate."""

    def test_write_gated_tools_pinned(self):
        assert (
            frozenset(
                {
                    "Write",
                    "Edit",
                    "str_replace_editor",
                    "MultiEdit",
                    "NotebookEdit",
                    "Bash",
                }
            )
            == _WRITE_GATED_TOOLS
        )

    def test_write_tool_path_keys_pinned(self):
        assert _WRITE_TOOL_PATH_KEYS == {
            "Write": ("file_path", "path"),
            "Edit": ("file_path", "path"),
            "str_replace_editor": ("path", "file_path"),
            "MultiEdit": ("file_path", "path"),
            "NotebookEdit": ("notebook_path",),
        }

    def test_write_gated_and_auto_approve_disjoint(self):
        """No tool should be both write-gated and auto-approved."""
        overlap = _WRITE_GATED_TOOLS & _AUTO_APPROVE_TOOLS
        assert not overlap, f"Overlap: {overlap}"


class TestWriteGateBehavior:
    """Tests for the write gate in PermissionHandler.handle()."""

    async def test_write_denied_not_in_worktree(self):
        handler, _ = _make_handler()
        result = await handler.handle("Write", {"file_path": "/f"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "worktree" in result.message.lower()

    async def test_bash_denied_not_in_worktree(self):
        handler, _ = _make_handler()
        result = await handler.handle("Bash", {"command": "ls"}, None)
        assert isinstance(result, PermissionResultDeny)

    async def test_sdk_deny_inside_gate_honored(self):
        """SDK deny should fire even for safe-dir files."""
        handler, _ = _make_handler()
        suggestion = MagicMock()
        suggestion.behavior = "deny"
        context = MagicMock()
        context.suggestions = [suggestion]
        result = await handler.handle("Write", {"file_path": "/f"}, context)
        assert isinstance(result, PermissionResultDeny)
        assert "permission rules" in result.message.lower()

    async def test_read_not_gated(self):
        handler, _ = _make_handler()
        result = await handler.handle("Read", {"file_path": "/f"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_notify_worktree_sets_flag(self):
        handler, _ = _make_handler()
        assert not handler._in_containment
        handler.notify_entered_worktree("test-wt")
        assert handler._in_containment

    async def test_notify_worktree_computes_root(self):
        handler, _ = _make_handler(project_root="/project")
        handler.notify_entered_worktree("feature-x")
        assert handler._containment_root is not None
        assert str(handler._containment_root).endswith(".claude/worktrees/feature-x")

    async def test_notify_worktree_rejects_path_traversal(self):
        """Worktree name with ../ should fail-closed (no CWD auto-approve)."""
        handler, _ = _make_handler(project_root="/project")
        handler.notify_entered_worktree("../../")
        # Fail-closed: _containment_root stays None, all writes require HITL
        assert handler._containment_root is None
        assert handler._in_containment is True  # gate can still be unlocked

    async def test_notify_worktree_rejects_slash_in_name(self):
        """Worktree name with / should fail-closed."""
        handler, _ = _make_handler(project_root="/project")
        handler.notify_entered_worktree("foo/bar")
        assert handler._containment_root is None

    async def test_notify_worktree_no_name_stays_none(self):
        """Empty worktree name should leave _containment_root None (fail-closed)."""
        handler, _ = _make_handler(project_root="/project")
        handler.notify_entered_worktree("")
        assert handler._containment_root is None

    async def test_write_after_worktree_prompts_once(self):
        handler, client = _make_handler()
        handler.notify_entered_worktree("test-wt")
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": "/f"}, None)
        assert isinstance(result, PermissionResultAllow)
        assert handler._write_access_granted

    async def test_gate_approval_does_not_blanket_cache_tools(self):
        """Gate approval should NOT add write tools to _session_approved_tools."""
        handler, _ = _make_handler()
        handler.notify_entered_worktree("test-wt")
        client = handler._router.client
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        await handler.handle("Write", {"file_path": "/f"}, None)
        # No blanket caching — CWD containment handles it
        assert "Write" not in handler._session_approved_tools
        assert "Edit" not in handler._session_approved_tools
        assert "Bash" not in handler._session_approved_tools

    async def test_safe_dir_write_auto_approved_e2e(self, tmp_path: Path):
        """End-to-end test for safe-dir write through handle()."""
        safe = tmp_path / "hack"
        safe.mkdir()
        target = safe / "notes.md"
        target.touch()
        handler, client = _make_handler(
            safe_write_dirs="hack/",
            project_root=str(tmp_path),
        )
        result = await handler.handle("Write", {"file_path": str(target)}, None)
        assert isinstance(result, PermissionResultAllow)
        # Should NOT reach HITL
        client.post_interactive.assert_not_called()

    async def test_safe_dir_write_outside_dir_denied(self, tmp_path: Path):
        """Write outside safe-dir should be denied when not in worktree."""
        (tmp_path / "hack").mkdir()
        handler, _ = _make_handler(
            safe_write_dirs="hack/",
            project_root=str(tmp_path),
        )
        result = await handler.handle(
            "Write", {"file_path": str(tmp_path / "src" / "main.py")}, None
        )
        assert isinstance(result, PermissionResultDeny)


class TestWriteGateFullFlow:
    """End-to-end integration tests for the full permission flow with write gate."""

    async def test_deny_then_worktree_then_approve(self, tmp_path: Path):
        """Full flow: Write denied → EnterWorktree → Write within CWD approved."""
        handler, client = _make_handler(project_root=str(tmp_path))
        wt_dir = tmp_path / ".claude" / "worktrees" / "test-wt"
        wt_dir.mkdir(parents=True)

        # 1. Write before worktree → denied
        result = await handler.handle("Write", {"file_path": "/f"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "worktree" in result.message.lower()

        # 2. EnterWorktree detected
        handler.notify_entered_worktree("test-wt")
        assert handler._in_containment

        # 3. First write after worktree → one-time gate approval
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": str(wt_dir / "f.py")}, None)
        assert isinstance(result, PermissionResultAllow)
        assert handler._write_access_granted

        # 4. Subsequent Write within worktree → auto-approved (CWD containment)
        client.post_interactive.reset_mock()
        result = await handler.handle("Edit", {"path": str(wt_dir / "g.py")}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_not_called()

        # 5. Write OUTSIDE worktree → requires HITL
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Edit", {"file_path": "/etc/hosts"}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called_once()

        # 6. Bash still requires HITL even after gate approval
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "ls"}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called_once()

    async def test_write_gate_denial_does_not_set_flag(self):
        """User denying after worktree entry should NOT set _write_access_granted."""
        handler, client = _make_handler()
        handler.notify_entered_worktree("test-wt")
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_deny(handler))
        result = await handler.handle("Write", {"file_path": "/f"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert not handler._write_access_granted

    async def test_read_grep_glob_unaffected_by_gate(self):
        """Read-only tools should work regardless of gate state."""
        handler, _ = _make_handler()
        # Not in worktree, no safe-dirs — gate active
        for tool in ("Read", "Grep", "Glob", "WebSearch"):
            result = await handler.handle(tool, {}, None)
            assert isinstance(result, PermissionResultAllow), f"{tool} should be auto-approved"


class TestCWDContainment:
    """Tests for CWD-based write containment after gate approval."""

    async def test_write_within_worktree_auto_approved(self, tmp_path: Path):
        """Write to a path within the worktree should be auto-approved."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True
        result = await handler.handle("Write", {"file_path": str(wt_dir / "main.py")}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_not_called()

    async def test_write_outside_worktree_requires_hitl(self, tmp_path: Path):
        """Write to a path outside the worktree should fall through to HITL."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Edit", {"file_path": "/etc/hosts"}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called()

    async def test_edit_with_dotdot_escape_requires_hitl(self, tmp_path: Path):
        """Path traversal via .. should NOT be treated as within worktree."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True
        # This resolves outside the worktree
        escape_path = str(wt_dir / ".." / ".." / ".." / "etc" / "passwd")
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Edit", {"file_path": escape_path}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called()

    async def test_bash_always_falls_through(self, tmp_path: Path):
        """Bash should never be auto-approved by CWD check (no file path to check)."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Bash", {"command": "ls"}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called()

    async def test_sdk_allow_does_not_bypass_cwd_containment(self, tmp_path: Path):
        """SDK allow suggestions must NOT bypass CWD containment for write tools."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True

        # SDK says "allow Edit" (user configured allowedTools)
        suggestion = MagicMock()
        suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [suggestion]

        # Edit to /etc/hosts is outside worktree — must go to HITL
        # even though SDK suggests allow
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Edit", {"file_path": "/etc/hosts"}, context)
        assert isinstance(result, PermissionResultAllow)
        # MUST have gone through HITL, not auto-approved by SDK
        client.post_interactive.assert_called()

    async def test_sdk_allow_still_works_for_non_write_tools(self):
        """SDK allow should still work normally for non-write-gated tools."""
        handler, client = _make_handler()
        suggestion = MagicMock()
        suggestion.behavior = "allow"
        context = MagicMock()
        context.suggestions = [suggestion]

        result = await handler.handle("CustomTool", {"key": "val"}, context)
        assert isinstance(result, PermissionResultAllow)
        # Should NOT go to HITL — SDK allow works for non-write tools
        client.post_interactive.assert_not_called()

    async def test_no_worktree_root_falls_through(self):
        """If containment root is unknown, CWD check should fail-closed."""
        handler, client = _make_handler(project_root="")
        handler._in_containment = True
        handler._write_access_granted = True
        # No worktree root → can't determine containment → falls through
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": "/some/file"}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called()

    async def test_relative_path_resolved_against_worktree(self, tmp_path: Path):
        """Relative paths should be resolved against worktree root."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True
        # Relative path within worktree
        result = await handler.handle("Write", {"file_path": "src/main.py"}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_not_called()

    async def test_empty_path_not_auto_approved(self, tmp_path: Path):
        """Empty file_path must NOT auto-approve via CWD containment."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": ""}, None)
        assert isinstance(result, PermissionResultAllow)
        # Should go to HITL, not CWD auto-approve
        client.post_interactive.assert_called()

    async def test_whitespace_path_not_auto_approved(self, tmp_path: Path):
        """Whitespace-only file_path must NOT auto-approve via CWD containment."""
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler, client = _make_handler(project_root=str(tmp_path))
        handler.notify_entered_worktree("feat")
        handler._write_access_granted = True
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": "  "}, None)
        assert isinstance(result, PermissionResultAllow)
        # Whitespace-only should NOT pass CWD containment
        client.post_interactive.assert_called()


class TestNonGitContainment:
    """Tests for notify_containment_active and non-git directory containment."""

    def _make_non_git_handler(self, tmp_path: Path):
        """Make a handler with non-git CWD containment active."""
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        config = _make_config()
        handler = PermissionHandler(
            router,
            config,
            authenticated_user_id="U_TEST",
            project_root=str(tmp_path),
        )
        handler.notify_containment_active(tmp_path, is_git_repo=False)
        return handler, client

    def test_notify_containment_active_sets_flag(self, tmp_path: Path):
        handler, _ = self._make_non_git_handler(tmp_path)
        assert handler._in_containment is True
        assert handler._is_git_repo is False
        assert handler._containment_root == tmp_path.resolve()

    def test_notify_containment_active_resolves_root_eagerly(self, tmp_path: Path):
        """SC-01: containment root must be resolved at call time."""
        handler, _ = self._make_non_git_handler(tmp_path)
        assert handler._containment_root is not None
        assert handler._containment_root.is_absolute()

    def test_notify_containment_active_noop_if_already_set(self, tmp_path: Path):
        """SC-04: anti-widening guard — second call is a no-op."""
        handler, _ = self._make_non_git_handler(tmp_path)
        other_dir = tmp_path / "subdir"
        other_dir.mkdir()
        handler.notify_containment_active(other_dir, is_git_repo=False)
        # Root must NOT have changed — anti-widening
        assert handler._containment_root == tmp_path.resolve()

    def test_in_containment_property(self, tmp_path: Path):
        handler, _ = self._make_non_git_handler(tmp_path)
        assert handler.in_containment is True

    def test_in_containment_property_false_initially(self):
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        config = _make_config()
        handler = PermissionHandler(router, config, authenticated_user_id="U_TEST")
        assert handler.in_containment is False

    async def test_write_denied_before_gate_approval(self, tmp_path: Path):
        """Before gate approval, writes still require the one-time HITL prompt."""
        handler, client = self._make_non_git_handler(tmp_path)
        # _in_containment is True but _write_access_granted is False
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Write", {"file_path": str(tmp_path / "f.py")}, None)
        assert isinstance(result, PermissionResultAllow)
        assert handler._write_access_granted
        client.post_interactive.assert_called_once()

    async def test_write_denied_message_non_git(self):
        """SC-08: denial message must NOT mention EnterWorktree for non-git."""
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        config = _make_config()
        handler = PermissionHandler(router, config, authenticated_user_id="U_TEST")
        # is_git_repo=False, _in_containment=False → denial path
        handler._is_git_repo = False
        result = await handler.handle("Write", {"file_path": "/f"}, None)
        assert isinstance(result, PermissionResultDeny)
        assert "EnterWorktree" not in result.message
        assert "project directory" in result.message.lower()

    async def test_gate_message_includes_non_git_warning(self, tmp_path: Path):
        """SC-02: non-git warning must appear inline in gate approval message."""
        handler, client = self._make_non_git_handler(tmp_path)
        posted_text: list[str] = []

        async def capture_post(*args, **kwargs):
            # Capture the text passed to post_interactive
            if args:
                posted_text.append(str(args[0]))
            if "blocks" in kwargs:
                for block in kwargs["blocks"]:
                    if block.get("type") == "section":
                        posted_text.append(str(block.get("text", {}).get("text", "")))

            # Auto-approve
            async def do():
                import asyncio as _asyncio

                await _asyncio.sleep(0.05)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            import asyncio as _asyncio

            _asyncio.create_task(do())
            return MagicMock(ts="mock_ts")

        client.post_interactive = AsyncMock(side_effect=capture_post)
        await handler.handle("Write", {"file_path": str(tmp_path / "f.py")}, None)
        full_text = " ".join(posted_text)
        assert "No version control detected" in full_text or "version control" in full_text.lower()

    async def test_write_within_cwd_auto_approved_after_gate(self, tmp_path: Path):
        """After gate approval, writes within CWD are auto-approved."""
        handler, client = self._make_non_git_handler(tmp_path)
        handler._write_access_granted = True
        target = tmp_path / "src" / "main.py"
        target.parent.mkdir()
        target.touch()
        result = await handler.handle("Write", {"file_path": str(target)}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_not_called()

    async def test_write_outside_cwd_requires_hitl(self, tmp_path: Path):
        """Writes outside the containment root require HITL even after gate approval."""
        handler, client = self._make_non_git_handler(tmp_path)
        handler._write_access_granted = True
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Edit", {"file_path": "/etc/hosts"}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called()

    async def test_symlink_escape_rejected(self, tmp_path: Path):
        """Path traversal via symlink must NOT be auto-approved."""
        handler, client = self._make_non_git_handler(tmp_path)
        handler._write_access_granted = True
        escape_path = str(tmp_path / ".." / "etc" / "passwd")
        client.post_interactive = AsyncMock(side_effect=_interactive_auto_approve(handler))
        result = await handler.handle("Edit", {"file_path": escape_path}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_called()

    async def test_safe_dir_bypass_works_in_non_git_mode(self, tmp_path: Path):
        """SC-09: safe-dir bypass still works even in non-git containment mode."""
        safe = tmp_path / "hack"
        safe.mkdir()
        target = safe / "notes.md"
        target.touch()
        client = make_mock_slack_client()
        router = ThreadRouter(client)
        config = _make_config(safe_write_dirs="hack/")
        handler = PermissionHandler(
            router,
            config,
            authenticated_user_id="U_TEST",
            project_root=str(tmp_path),
        )
        handler.notify_containment_active(tmp_path, is_git_repo=False)
        result = await handler.handle("Write", {"file_path": str(target)}, None)
        assert isinstance(result, PermissionResultAllow)
        client.post_interactive.assert_not_called()

    async def test_sdk_deny_overrides_non_git_containment(self, tmp_path: Path):
        """SC-10: SDK deny must override non-git containment for write tools."""
        handler, client = self._make_non_git_handler(tmp_path)
        handler._write_access_granted = True
        suggestion = MagicMock()
        suggestion.behavior = "deny"
        context = MagicMock()
        context.suggestions = [suggestion]
        result = await handler.handle("Write", {"file_path": str(tmp_path / "f.py")}, context)
        assert isinstance(result, PermissionResultDeny)

    async def test_worktree_entry_narrows_containment(self, tmp_path: Path):
        """Worktree entry after non-git containment is active correctly narrows the root.

        The worktree directory is a subdirectory of the project root (which is the
        non-git containment root), so notify_entered_worktree narrows the write boundary.
        This is the expected and safe direction — narrowing is always allowed.
        """
        handler, _ = self._make_non_git_handler(tmp_path)
        original_root = handler._containment_root
        wt_dir = tmp_path / ".claude" / "worktrees" / "feat"
        wt_dir.mkdir(parents=True)
        handler.notify_entered_worktree("feat")
        # worktree is a subdirectory of original_root — containment narrows as expected
        assert handler._containment_root == wt_dir.resolve()
        assert handler._in_containment is True
        _ = original_root  # used above for context

    async def test_notify_entered_worktree_does_not_widen_broader_path(self, tmp_path: Path):
        """Anti-widening guard: a worktree outside the current root must NOT widen it."""
        # Set containment to a narrow subdirectory first
        narrow_dir = tmp_path / ".claude" / "worktrees" / "feat"
        narrow_dir.mkdir(parents=True)
        handler, _ = self._make_non_git_handler(tmp_path)
        # Manually narrow to the worktree directory
        handler._containment_root = narrow_dir.resolve()

        # Now call notify_entered_worktree with a sibling worktree (same level, not a
        # subdir of narrow_dir — would widen if allowed)
        sibling = tmp_path / ".claude" / "worktrees" / "other"
        sibling.mkdir(parents=True)
        handler.notify_entered_worktree("other")

        # Root must NOT have changed — sibling is not relative to narrow_dir
        assert handler._containment_root == narrow_dir.resolve()

    async def test_notify_entered_worktree_does_not_widen_parent_child(self, tmp_path: Path):
        """Anti-widening: worktree that is a parent of current root must not widen."""
        # Start with a narrow worktree root
        deep_dir = tmp_path / ".claude" / "worktrees" / "feat" / "sub"
        deep_dir.mkdir(parents=True)
        handler, _ = self._make_non_git_handler(tmp_path)
        handler._containment_root = deep_dir.resolve()

        # Enter a worktree whose root is a parent of the current root
        handler.notify_entered_worktree("feat")
        # Root must stay at the narrower deep_dir
        assert handler._containment_root == deep_dir.resolve()

    def test_notify_containment_active_resolves_symlink_root(self, tmp_path: Path):
        """notify_containment_active must store the resolved real path, not the symlink."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        client = make_mock_slack_client()
        from summon_claude.slack.router import ThreadRouter

        router = ThreadRouter(client)
        config = _make_config()
        handler = PermissionHandler(router, config, authenticated_user_id="U_TEST")
        handler.notify_containment_active(link_dir, is_git_repo=False)

        # containment_root must be the resolved real path, not the symlink
        assert handler._containment_root == real_dir.resolve()
        assert handler._containment_root != link_dir


class TestDetectGitFailurePath:
    """Integration tests for _detect_git failure handling in PermissionHandler."""

    async def test_non_git_containment_set_when_detect_git_fails(self, tmp_path: Path):
        """When _detect_git returns (False, None), non-git containment must be activated."""
        from unittest.mock import patch

        from summon_claude.slack.router import ThreadRouter

        client = make_mock_slack_client()
        router = ThreadRouter(client)
        config = _make_config()
        handler = PermissionHandler(
            router,
            config,
            authenticated_user_id="U_TEST",
            project_root=str(tmp_path),
        )

        # Simulate what session.py does when _detect_git returns (False, None)
        # (i.e., not a git repo or git subprocess failed)
        is_git = False
        if not is_git:
            handler.notify_containment_active(
                containment_root=tmp_path,
                is_git_repo=False,
            )

        assert handler._in_containment is True
        assert handler._is_git_repo is False
        assert handler._containment_root == tmp_path.resolve()

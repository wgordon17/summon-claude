"""Tests for bug hunter session profile wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from summon_claude.sessions.session import (
    _BUG_HUNTER_DISALLOWED_TOOLS,
    _WORKTREE_DISALLOWED_TOOLS,
    SessionOptions,
    SummonSession,
)


def _make_options(**kwargs) -> SessionOptions:
    defaults = dict(cwd="/tmp/test", name="bug-hunter")
    defaults.update(kwargs)
    return SessionOptions(**defaults)


class TestBugHunterSessionOptions:
    def test_bug_hunter_profile_field_exists(self):
        """SessionOptions has bug_hunter_profile field defaulting to False."""
        opts = _make_options()
        assert opts.bug_hunter_profile is False

    def test_bug_hunter_profile_set_true(self):
        """SessionOptions bug_hunter_profile can be set True."""
        opts = _make_options(bug_hunter_profile=True)
        assert opts.bug_hunter_profile is True

    def test_bug_hunter_and_pm_are_separate_fields(self):
        """bug_hunter_profile and pm_profile are independent."""
        opts = _make_options(bug_hunter_profile=True, pm_profile=False)
        assert opts.bug_hunter_profile is True
        assert opts.pm_profile is False


class TestBugHunterDisallowedTools:
    def test_bug_hunter_disallowed_tools_pinned(self):
        """_BUG_HUNTER_DISALLOWED_TOOLS contains the expected tool names."""
        expected = frozenset(
            {
                "session_start",
                "session_stop",
                "session_message",
                "session_resume",
                "CronCreate",
                "CronDelete",
                "CronList",
                "TeamCreate",
                "TeamDelete",
                "slack_upload_file",
                "slack_create_thread",
                "slack_react",
                "slack_post_snippet",
                "slack_update_message",
            }
        )
        assert expected == _BUG_HUNTER_DISALLOWED_TOOLS

    def test_bug_hunter_disallowed_tools_superset_of_worktree(self):
        """Bug hunter disallowed set has no overlap with worktree set (they're unioned)."""
        # The two sets should be compatible for union — no semantic conflict expected.
        combined = _WORKTREE_DISALLOWED_TOOLS | _BUG_HUNTER_DISALLOWED_TOOLS
        assert len(combined) == len(_WORKTREE_DISALLOWED_TOOLS) + len(_BUG_HUNTER_DISALLOWED_TOOLS)

    def test_session_lifecycle_tools_in_bug_hunter_disallowed(self):
        """Session lifecycle tools blocked to prevent nested spawning."""
        for tool in ("session_start", "session_stop", "session_message", "session_resume"):
            assert tool in _BUG_HUNTER_DISALLOWED_TOOLS


class TestBugHunterMcpGating:
    def test_is_bug_hunter_gates_session_lifecycle_tools(self):
        """When is_bug_hunter=True, session lifecycle tools are excluded from MCP."""
        from unittest.mock import AsyncMock

        from summon_claude.sessions.scheduler import SessionScheduler
        from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools

        reg = MagicMock()
        reg.list_active = AsyncMock(return_value=[])
        scheduler = MagicMock(spec=SessionScheduler)

        tools = create_summon_cli_mcp_tools(
            reg,
            "sess-1",
            "user-1",
            "chan-1",
            "/tmp/test",
            is_pm=False,
            is_bug_hunter=True,
            scheduler=scheduler,
        )
        tool_names = {getattr(t, "name", None) for t in tools}
        assert "session_start" not in tool_names
        assert "session_stop" not in tool_names
        assert "session_message" not in tool_names
        assert "session_resume" not in tool_names
        assert "session_list" not in tool_names
        assert "session_info" not in tool_names
        assert "cron_create" not in tool_names
        assert "cron_delete" not in tool_names
        assert "cron_list" not in tool_names
        assert any("task" in (n or "").lower() for n in tool_names)

    def test_is_bug_hunter_false_includes_session_tools_for_pm(self):
        """When is_pm=True and is_bug_hunter=False, PM lifecycle tools are included."""
        from unittest.mock import AsyncMock

        from summon_claude.sessions.scheduler import SessionScheduler
        from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools

        reg = MagicMock()
        reg.list_active = AsyncMock(return_value=[])
        scheduler = MagicMock(spec=SessionScheduler)

        tools = create_summon_cli_mcp_tools(
            reg,
            "sess-1",
            "user-1",
            "chan-1",
            "/tmp/test",
            is_pm=True,
            is_bug_hunter=False,
            scheduler=scheduler,
        )
        tool_names = {getattr(t, "name", None) for t in tools}
        assert "session_start" in tool_names


class TestBugHunterCanvasTemplate:
    def test_bug_hunter_canvas_template_in_dict(self):
        """bug_hunter key maps to BUG_HUNTER_CANVAS_TEMPLATE in _TEMPLATES."""
        from summon_claude.slack.canvas_templates import (
            _TEMPLATES,
            BUG_HUNTER_CANVAS_TEMPLATE,
            get_canvas_template,
        )

        assert "bug-hunter" in _TEMPLATES
        assert _TEMPLATES["bug-hunter"] is BUG_HUNTER_CANVAS_TEMPLATE

    def test_get_canvas_template_bug_hunter(self):
        """get_canvas_template('bug_hunter') returns the bug hunter template."""
        from summon_claude.slack.canvas_templates import (
            BUG_HUNTER_CANVAS_TEMPLATE,
            get_canvas_template,
        )

        result = get_canvas_template("bug-hunter")
        assert result is BUG_HUNTER_CANVAS_TEMPLATE

    def test_bug_hunter_canvas_contains_findings_table(self):
        """Bug hunter canvas template contains the Findings section."""
        from summon_claude.slack.canvas_templates import BUG_HUNTER_CANVAS_TEMPLATE

        assert "## Findings" in BUG_HUNTER_CANVAS_TEMPLATE
        assert "Severity" in BUG_HUNTER_CANVAS_TEMPLATE
        assert "Confidence" in BUG_HUNTER_CANVAS_TEMPLATE
        assert "## Suppressions" in BUG_HUNTER_CANVAS_TEMPLATE
        assert "## Last Scan" in BUG_HUNTER_CANVAS_TEMPLATE


class TestBugHunterSessionProperties:
    def _make_session(self, **option_kwargs) -> SummonSession:
        config = MagicMock()
        config.permission_debounce_ms = 2000
        config.permission_timeout_s = 900
        config.safe_write_dirs = ""
        config.auto_classifier_enabled = False
        options = _make_options(**option_kwargs)
        return SummonSession(
            config=config,
            options=options,
            session_id="test-session-id",
        )

    def test_is_bug_hunter_property_false_by_default(self):
        """is_bug_hunter property returns False when bug_hunter_profile=False."""
        session = self._make_session()
        assert session.is_bug_hunter is False

    def test_is_bug_hunter_property_true_when_set(self):
        """is_bug_hunter property returns True when bug_hunter_profile=True."""
        session = self._make_session(bug_hunter_profile=True)
        assert session.is_bug_hunter is True

    def test_bug_hunter_profile_mutual_with_pm_check(self):
        """bug_hunter_profile and pm_profile can be set independently."""
        session_bh = self._make_session(bug_hunter_profile=True)
        session_pm = self._make_session(pm_profile=True)
        assert session_bh.is_bug_hunter is True
        assert session_bh.is_pm is False
        assert session_pm.is_pm is True
        assert session_pm.is_bug_hunter is False


class TestBugHunterPmSpawnGate:
    def test_session_start_absent_for_non_pm(self):
        """Non-PM sessions do not receive the session_start MCP tool."""
        from unittest.mock import AsyncMock

        from summon_claude.sessions.scheduler import SessionScheduler
        from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools

        reg = MagicMock()
        reg.list_active = AsyncMock(return_value=[])
        scheduler = MagicMock(spec=SessionScheduler)

        tools = create_summon_cli_mcp_tools(
            reg,
            "sess-1",
            "user-1",
            "chan-1",
            "/tmp/test",
            is_pm=False,
            is_bug_hunter=False,
            scheduler=scheduler,
        )
        session_start = next(
            (t for t in tools if getattr(t, "name", None) == "session_start"), None
        )
        # Non-PM sessions don't get session_start at all — the gate is upstream of
        # any bug_hunter_profile validation inside the handler.
        assert session_start is None


class TestBugHunterMutualExclusion:
    def _make_session(self, **option_kwargs):
        config = MagicMock()
        config.permission_debounce_ms = 2000
        config.permission_timeout_s = 900
        config.safe_write_dirs = ""
        config.auto_classifier_enabled = False
        options = _make_options(**option_kwargs)
        return SummonSession(
            config=config,
            options=options,
            session_id="test-bh-excl",
        )

    def test_bug_hunter_and_pm_profile_raises_value_error(self):
        with pytest.raises(ValueError, match=r"[Oo]nly one.*profile"):
            self._make_session(bug_hunter_profile=True, pm_profile=True)

    def test_bug_hunter_and_scribe_profile_raises_value_error(self):
        with pytest.raises(ValueError, match=r"[Oo]nly one.*profile"):
            self._make_session(bug_hunter_profile=True, scribe_profile=True)


class TestBugHunterMemoryVolumePath:
    def _compute_memory_vol_path(self, project_id: str | None) -> str:
        from summon_claude.config import get_data_dir

        slug = project_id or "default"
        return str(get_data_dir() / "bug-hunter-memory" / slug)

    def test_project_id_produces_project_slug(self):
        path = self._compute_memory_vol_path("proj-abc")
        assert path.endswith("bug-hunter-memory/proj-abc")

    def test_none_project_id_produces_default_slug(self):
        path = self._compute_memory_vol_path(None)
        assert path.endswith("bug-hunter-memory/default")

    def test_different_project_ids_produce_different_paths(self):
        path_a = self._compute_memory_vol_path("proj-a")
        path_b = self._compute_memory_vol_path("proj-b")
        assert path_a != path_b

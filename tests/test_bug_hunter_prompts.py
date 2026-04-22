"""Tests for bug hunter prompt builder functions."""

from __future__ import annotations

from summon_claude.sessions.prompts.bug_hunter import (
    build_bug_hunter_scan_prompt,
    build_bug_hunter_system_prompt,
)


class TestBuildBugHunterSystemPrompt:
    def test_returns_preset_dict(self) -> None:
        result = build_bug_hunter_system_prompt(
            cwd="/workspace/myproject",
            scan_interval_s=3600,
            project_name="MyProject",
        )
        assert isinstance(result, dict)
        assert result["type"] == "preset"
        assert result["preset"] == "claude_code"
        assert "append" in result

    def test_cwd_interpolated(self) -> None:
        result = build_bug_hunter_system_prompt(
            cwd="/workspace/myproject",
            scan_interval_s=3600,
            project_name="MyProject",
        )
        assert "/workspace/myproject" in result["append"]

    def test_project_name_interpolated(self) -> None:
        result = build_bug_hunter_system_prompt(
            cwd="/workspace",
            scan_interval_s=3600,
            project_name="SpecialProject",
        )
        assert "SpecialProject" in result["append"]

    def test_scan_interval_minutes_conversion(self) -> None:
        result = build_bug_hunter_system_prompt(
            cwd="/workspace",
            scan_interval_s=3600,
            project_name="MyProject",
        )
        assert "60" in result["append"]

    def test_scan_interval_minimum_one_minute(self) -> None:
        result = build_bug_hunter_system_prompt(
            cwd="/workspace",
            scan_interval_s=30,
            project_name="MyProject",
        )
        assert "every 1 minute" in result["append"]
        assert "every 0 minute" not in result["append"]

    def test_no_unresolved_user_placeholders(self) -> None:
        result = build_bug_hunter_system_prompt(
            cwd="/some/path",
            scan_interval_s=1800,
            project_name="TestProject",
        )
        # Only check the three user-interpolated placeholders are resolved
        assert "{cwd}" not in result["append"]
        assert "{scan_interval_min}" not in result["append"]
        assert "{project_name}" not in result["append"]

    def test_key_sections_present(self) -> None:
        result = build_bug_hunter_system_prompt(
            cwd="/workspace",
            scan_interval_s=3600,
            project_name="MyProject",
        )
        append = result["append"]
        assert "## Scan Phases" in append
        assert "## Memory Directory" in append
        assert "## Finding Format" in append


class TestBuildBugHunterScanPrompt:
    def test_returns_string(self) -> None:
        result = build_bug_hunter_scan_prompt()
        assert isinstance(result, str)

    def test_contains_scan_trigger_prefix(self) -> None:
        result = build_bug_hunter_scan_prompt()
        assert result.startswith("[SUMMON-BUG-HUNTER-SCAN]")

    def test_instructs_dynamic_scope_from_scan_log(self) -> None:
        result = build_bug_hunter_scan_prompt()
        assert "SCAN_LOG.md" in result
        assert "first scan" in result.lower()

    def test_timeout_reminder(self) -> None:
        result = build_bug_hunter_scan_prompt()
        assert "30-minute" in result

"""Tests for summon_claude.slack.canvas_templates."""

from __future__ import annotations

from summon_claude.slack.canvas_templates import (
    AGENT_CANVAS_TEMPLATE,
    GLOBAL_PM_CANVAS_TEMPLATE,
    PM_CANVAS_TEMPLATE,
    SCRIBE_CANVAS_TEMPLATE,
    get_canvas_template,
)


class TestGetCanvasTemplate:
    def test_agent_profile(self):
        assert get_canvas_template("agent") is AGENT_CANVAS_TEMPLATE

    def test_pm_profile(self):
        assert get_canvas_template("pm") is PM_CANVAS_TEMPLATE

    def test_global_pm_profile(self):
        assert get_canvas_template("global-pm") is GLOBAL_PM_CANVAS_TEMPLATE

    def test_scribe_profile(self):
        assert get_canvas_template("scribe") is SCRIBE_CANVAS_TEMPLATE

    def test_unknown_profile_falls_back_to_agent(self):
        assert get_canvas_template("nonexistent") is AGENT_CANVAS_TEMPLATE

    def test_empty_profile_falls_back_to_agent(self):
        assert get_canvas_template("") is AGENT_CANVAS_TEMPLATE


class TestTemplateFormatting:
    """Canvas templates are interpolated via .replace(), not .format()."""

    def test_agent_template_formats(self):
        result = AGENT_CANVAS_TEMPLATE.replace("{model}", "opus-4").replace(
            "{cwd}", "/home/user/proj"
        )
        assert "opus-4" in result
        assert "/home/user/proj" in result
        assert "{model}" not in result
        assert "{cwd}" not in result
        assert "Changed Files" in result

    def test_changed_files_only_in_agent(self):
        assert "Changed Files" in AGENT_CANVAS_TEMPLATE
        assert "Changed Files" not in PM_CANVAS_TEMPLATE
        assert "Changed Files" not in GLOBAL_PM_CANVAS_TEMPLATE
        assert "Changed Files" not in SCRIBE_CANVAS_TEMPLATE

    def test_pm_template_formats(self):
        result = PM_CANVAS_TEMPLATE.replace("{model}", "sonnet-4").replace("{cwd}", "/tmp")
        assert "sonnet-4" in result
        assert "Active Tasks" in result

    def test_scribe_template_formats(self):
        result = SCRIBE_CANVAS_TEMPLATE.replace("{model}", "haiku-4.5").replace(
            "{cwd}", "/workspace"
        )
        assert "haiku-4.5" in result
        assert "Session Timeline" in result

    def test_global_pm_template_has_no_cwd(self):
        """Global PM template should format with model only — no cwd in output."""
        assert "{cwd}" not in GLOBAL_PM_CANVAS_TEMPLATE
        result = GLOBAL_PM_CANVAS_TEMPLATE.replace("{model}", "opus-4")
        assert "opus-4" in result
        assert "Active Sessions" in result

    def test_cwd_with_curly_braces(self):
        """Ensure cwd containing curly braces doesn't crash .replace()."""
        result = AGENT_CANVAS_TEMPLATE.replace("{model}", "opus-4").replace(
            "{cwd}", "/home/user/{project}"
        )
        assert "/home/user/{project}" in result
        assert "{model}" not in result


class TestScheduledJobsSection:
    def test_all_templates_have_scheduled_jobs(self):
        assert "## Scheduled Jobs" in AGENT_CANVAS_TEMPLATE
        assert "## Scheduled Jobs" in PM_CANVAS_TEMPLATE
        assert "## Scheduled Jobs" in GLOBAL_PM_CANVAS_TEMPLATE
        assert "## Scheduled Jobs" in SCRIBE_CANVAS_TEMPLATE

    def test_non_pm_templates_have_tasks_heading(self):
        assert "## Tasks" in AGENT_CANVAS_TEMPLATE
        assert "## Tasks" in GLOBAL_PM_CANVAS_TEMPLATE
        assert "## Tasks" in SCRIBE_CANVAS_TEMPLATE

    def test_pm_template_has_work_items_not_tasks(self):
        assert "## Work Items" in PM_CANVAS_TEMPLATE
        assert "## Tasks" not in PM_CANVAS_TEMPLATE

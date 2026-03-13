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
    def test_agent_template_formats(self):
        result = AGENT_CANVAS_TEMPLATE.format(model="opus-4", cwd="/home/user/proj")
        assert "opus-4" in result
        assert "/home/user/proj" in result

    def test_pm_template_formats(self):
        result = PM_CANVAS_TEMPLATE.format(model="sonnet-4", cwd="/tmp")
        assert "sonnet-4" in result
        assert "Active Tasks" in result

    def test_scribe_template_formats(self):
        result = SCRIBE_CANVAS_TEMPLATE.format(model="haiku-4.5", cwd="/workspace")
        assert "haiku-4.5" in result
        assert "Session Timeline" in result

    def test_global_pm_template_has_no_cwd(self):
        # global-pm template should only need model, not cwd
        result = GLOBAL_PM_CANVAS_TEMPLATE.format(model="opus-4", cwd="/tmp")
        assert "opus-4" in result
        assert "Active Sessions" in result

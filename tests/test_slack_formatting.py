"""Tests for summon_claude.slack.formatting — markdown to Slack mrkdwn conversion."""

from __future__ import annotations

import pytest

from summon_claude.slack.formatting import markdown_to_mrkdwn, snippet_type_for_extension


class TestMarkdownToMrkdwn:
    @pytest.mark.parametrize(
        ("md", "expected"),
        [
            ("**text**", "*text*"),
            ("*text*", "_text_"),
            ("***text***", "*_text_*"),
            ("# Heading", "*Heading*"),
            ("## Heading", "*Heading*"),
            ("### Heading", "*Heading*"),
            ("[text](https://example.com)", "<https://example.com|text>"),
            ("~~text~~", "~text~"),
            ("no formatting here", "no formatting here"),
            ("use `func()`", "use `func()`"),
        ],
    )
    def test_conversion(self, md: str, expected: str) -> None:
        result = markdown_to_mrkdwn(md)
        assert expected in result

    @pytest.mark.parametrize(
        ("fenced", "preserved"),
        [
            ("```\n**bold** [link](url)\n# heading\n```", "**bold**"),
            ("```python\ndef foo():\n    return **bar**\n```", "**bar**"),
        ],
    )
    def test_code_fence_protection(self, fenced: str, preserved: str) -> None:
        result = markdown_to_mrkdwn(fenced)
        # Content inside code fences must NOT be converted
        assert preserved in result

    @pytest.mark.parametrize(
        "slack_element",
        [
            "<@U123>",
            "<#C123|general>",
            ":emoji:",
        ],
    )
    def test_slack_elements_preserved(self, slack_element: str) -> None:
        result = markdown_to_mrkdwn(f"Hello {slack_element} world")
        assert slack_element in result

    @pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t", "  \t  "])
    def test_empty_and_whitespace(self, text: str) -> None:
        assert markdown_to_mrkdwn(text) == text

    def test_mixed_realistic(self) -> None:
        md = (
            "## Summary\n\n"
            "I've made **three changes** to the codebase:\n\n"
            "1. Updated `config.py` with *new defaults*\n"
            "2. Fixed the [auth bug](https://github.com/issue/1)\n\n"
            "```python\ndef fix():\n    return True\n```\n"
        )
        result = markdown_to_mrkdwn(md)
        # Bold should be converted
        assert "*three changes*" in result
        # Code fence content should be preserved
        assert "def fix():" in result


class TestSnippetTypeForExtension:
    @pytest.mark.parametrize(
        ("ext", "expected"),
        [
            ("py", "python"),
            ("js", "javascript"),
            ("ts", "typescript"),
            ("sh", "shell"),
            ("rb", "ruby"),
            ("rs", "rust"),
            ("yml", "yaml"),
            ("kt", "kotlin"),
            ("jsx", "javascript"),
            ("tsx", "typescript"),
            ("md", "markdown"),
        ],
    )
    def test_mapped_extensions(self, ext: str, expected: str) -> None:
        assert snippet_type_for_extension(ext) == expected

    @pytest.mark.parametrize("ext", ["go", "json", "yaml", "html", "css", "toml", "sql"])
    def test_identity_extensions(self, ext: str) -> None:
        assert snippet_type_for_extension(ext) == ext

    def test_empty_returns_none(self) -> None:
        assert snippet_type_for_extension("") is None

    def test_strips_leading_dot(self) -> None:
        assert snippet_type_for_extension(".py") == "python"

    def test_case_insensitive(self) -> None:
        assert snippet_type_for_extension("PY") == "python"

    @pytest.mark.parametrize("ext", ["lock", "dockerfile", "xyz", "bak"])
    def test_unknown_extensions_pass_through(self, ext: str) -> None:
        """Unknown extensions pass through as-is; Slack ignores unrecognized types."""
        assert snippet_type_for_extension(ext) == ext


# ---------------------------------------------------------------------------
# comp-7: build_home_view
# ---------------------------------------------------------------------------


class TestBuildHomeView:
    from summon_claude.slack.formatting import (
        build_home_view,
    )

    def _make_session(self, **overrides) -> dict:
        base = {
            "session_id": "sess-abc-1234",
            "session_name": "my-proj",
            "model": "claude-sonnet-4-6",
            "slack_channel_name": "summon-my-proj-0224",
            "slack_channel_id": "C001",
            "status": "active",
            "context_pct": 42.0,
        }
        base.update(overrides)
        return base

    def test_empty_sessions_shows_no_active_message(self):
        from summon_claude.slack.formatting import build_home_view

        result = build_home_view([])
        assert result["type"] == "home"
        all_text = " ".join(
            b.get("text", {}).get("text", "") for b in result["blocks"] if "text" in b
        )
        assert "No active sessions" in all_text

    def test_empty_sessions_has_header(self):
        from summon_claude.slack.formatting import build_home_view

        result = build_home_view([])
        header_blocks = [b for b in result["blocks"] if b["type"] == "header"]
        assert len(header_blocks) == 1
        assert "Summon Claude Dashboard" in header_blocks[0]["text"]["text"]

    def test_single_session_produces_section_with_fields(self):
        from summon_claude.slack.formatting import build_home_view

        result = build_home_view([self._make_session()])
        field_sections = [b for b in result["blocks"] if b.get("fields")]
        assert len(field_sections) == 1

    def test_session_card_contains_name_model_channel_status(self):
        from summon_claude.slack.formatting import build_home_view

        session = self._make_session(
            session_name="my-proj",
            model="claude-opus-4-6",
            slack_channel_name="summon-my-proj-0224",
            status="active",
        )
        result = build_home_view([session])
        field_sections = [b for b in result["blocks"] if b.get("fields")]
        all_fields = " ".join(f["text"] for b in field_sections for f in b["fields"])
        assert "my-proj" in all_fields
        assert "claude-opus-4-6" in all_fields
        assert "summon-my-proj-0224" in all_fields
        assert "active" in all_fields

    def test_multiple_sessions_produce_multiple_cards(self):
        from summon_claude.slack.formatting import build_home_view

        sessions = [self._make_session(session_name=f"proj-{i}") for i in range(3)]
        result = build_home_view(sessions)
        field_sections = [b for b in result["blocks"] if b.get("fields")]
        assert len(field_sections) == 3

    def test_cap_at_twenty_sessions(self):
        from summon_claude.slack.formatting import build_home_view

        sessions = [self._make_session(session_name=f"s{i}") for i in range(25)]
        result = build_home_view(sessions)
        field_sections = [b for b in result["blocks"] if b.get("fields")]
        assert len(field_sections) == 20

    def test_overflow_message_shown_when_capped(self):
        from summon_claude.slack.formatting import build_home_view

        sessions = [self._make_session(session_name=f"s{i}") for i in range(25)]
        result = build_home_view(sessions)
        context_texts = " ".join(
            elem.get("text", "")
            for b in result["blocks"]
            if b["type"] == "context"
            for elem in b.get("elements", [])
        )
        assert "5 more sessions not shown" in context_texts

    def test_exactly_twenty_sessions_no_overflow_message(self):
        from summon_claude.slack.formatting import build_home_view

        sessions = [self._make_session(session_name=f"s{i}") for i in range(20)]
        result = build_home_view(sessions)
        context_texts = " ".join(
            elem.get("text", "")
            for b in result["blocks"]
            if b["type"] == "context"
            for elem in b.get("elements", [])
        )
        assert "not shown" not in context_texts

    def test_context_pct_shown_when_present(self):
        from summon_claude.slack.formatting import build_home_view

        result = build_home_view([self._make_session(context_pct=75.0)])
        field_sections = [b for b in result["blocks"] if b.get("fields")]
        all_fields = " ".join(f["text"] for b in field_sections for f in b["fields"])
        assert "75%" in all_fields

    def test_context_pct_dash_when_absent(self):
        from summon_claude.slack.formatting import build_home_view

        result = build_home_view([self._make_session(context_pct=None)])
        field_sections = [b for b in result["blocks"] if b.get("fields")]
        all_fields = " ".join(f["text"] for b in field_sections for f in b["fields"])
        assert "Context:* —" in all_fields

    def test_last_updated_context_block_always_present(self):
        from summon_claude.slack.formatting import build_home_view

        result = build_home_view([])
        context_texts = " ".join(
            elem.get("text", "")
            for b in result["blocks"]
            if b["type"] == "context"
            for elem in b.get("elements", [])
        )
        assert "Last updated:" in context_texts

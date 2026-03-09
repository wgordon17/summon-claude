"""Tests for summon_claude.slack.formatting — markdown to Slack mrkdwn conversion."""

from __future__ import annotations

import pytest

from summon_claude.slack.formatting import markdown_to_mrkdwn


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

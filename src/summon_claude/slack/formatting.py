"""Markdown-to-Slack-mrkdwn conversion."""

from __future__ import annotations

import logging

from markdown_to_mrkdwn import SlackMarkdownConverter

logger = logging.getLogger(__name__)

_converter = SlackMarkdownConverter()


def markdown_to_mrkdwn(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn format.

    Skips conversion for empty or whitespace-only text.
    Returns the original text unchanged if conversion fails.
    """
    if not text or text.isspace():
        return text
    try:
        return _converter.convert(text)
    except Exception:
        logger.warning("mrkdwn conversion failed for text (len=%d)", len(text), exc_info=True)
        return text

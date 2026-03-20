"""Markdown-to-Slack-mrkdwn conversion and snippet type resolution."""

from __future__ import annotations

import logging

from markdown_to_mrkdwn import SlackMarkdownConverter

logger = logging.getLogger(__name__)

_converter = SlackMarkdownConverter()

# File extension → Slack snippet_type (identity mappings like "go" or "json"
# are omitted; snippet_type_for_extension() passes them through unchanged)
_EXT_TO_SNIPPET_TYPE: dict[str, str] = {
    "sh": "shell",
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rb": "ruby",
    "yml": "yaml",
    "md": "markdown",
    "rs": "rust",
    "kt": "kotlin",
}


def snippet_type_for_extension(ext: str) -> str | None:
    """Resolve a file extension to a Slack snippet_type.

    Identity extensions (e.g. ``go``, ``json``, ``yaml``) pass through unchanged.
    Returns ``None`` for empty extensions.
    """
    ext = ext.lstrip(".").lower()
    if not ext:
        return None
    return _EXT_TO_SNIPPET_TYPE.get(ext, ext)


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

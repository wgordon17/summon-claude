"""Markdown-to-Slack-mrkdwn conversion and snippet type resolution."""

from __future__ import annotations

import logging
import time
from typing import Any

from markdown_to_mrkdwn import SlackMarkdownConverter

from summon_claude.slack.client import sanitize_for_mrkdwn

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

# Home tab has a 100-block limit; cap displayed sessions to stay safe
_HOME_MAX_SESSIONS = 20


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


def build_home_view(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Block Kit Home tab view showing the user's active sessions.

    Takes a list of session dicts from SessionRegistry.list_active_by_user().
    Caps at _HOME_MAX_SESSIONS to stay within the 100-block Home tab limit.

    Note: App Home is only visible while the daemon is running (the Bolt
    instance must be active to receive app_home_opened events).
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Summon Claude Dashboard"},
        },
        {"type": "divider"},
    ]

    capped = sessions[:_HOME_MAX_SESSIONS]

    if not capped:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No active sessions._"},
            }
        )
    else:
        for s in capped:
            name = sanitize_for_mrkdwn(s.get("session_name") or s.get("session_id", "")[:8])
            model = sanitize_for_mrkdwn(s.get("model") or "default")
            channel = sanitize_for_mrkdwn(
                s.get("slack_channel_name") or s.get("slack_channel_id") or "—"
            )
            status = sanitize_for_mrkdwn(s.get("status", "unknown"))
            context_pct = s.get("context_pct")
            ctx_text = f"{context_pct:.0f}%" if context_pct is not None else "—"

            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Name:* {name}"},
                        {"type": "mrkdwn", "text": f"*Model:* {model}"},
                        {"type": "mrkdwn", "text": f"*Channel:* #{channel}"},
                        {"type": "mrkdwn", "text": f"*Status:* {status}"},
                        {"type": "mrkdwn", "text": f"*Context:* {ctx_text}"},
                    ],
                }
            )
            blocks.append({"type": "divider"})

    if len(sessions) > _HOME_MAX_SESSIONS:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_{len(sessions) - _HOME_MAX_SESSIONS} more sessions not shown._",
                    }
                ],
            }
        )

    updated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Last updated: {updated}"}],
        }
    )

    return {"type": "home", "blocks": blocks}

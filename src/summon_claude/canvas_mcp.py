"""MCP tools — canvas read/write/update for all sessions with a CanvasStore."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from claude_agent_sdk import create_sdk_mcp_server, tool

from summon_claude.sandbox import BUG_HUNTER_SESSION_NAME
from summon_claude.security import mark_untrusted
from summon_claude.sessions.registry import SessionRegistry

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool
    from claude_agent_sdk.types import McpSdkServerConfig

    from summon_claude.slack.canvas_store import CanvasStore

logger = logging.getLogger(__name__)

_CANVAS_MAX_CHARS = 102400  # 100K characters


def create_canvas_mcp_tools(
    canvas_store: CanvasStore,
    registry: SessionRegistry,
    authenticated_user_id: str,
    channel_id: str,
) -> list[SdkMcpTool]:
    """Create MCP tool instances for canvas read/write operations.

    Args:
        canvas_store: CanvasStore instance for the session's canvas.
        registry: SessionRegistry for cross-channel canvas lookups.
        authenticated_user_id: For cross-channel scope guards.
        channel_id: The session's own channel ID.
    """

    @tool(
        "summon_canvas_read",
        (
            "Read the channel canvas. Returns the full markdown content of the "
            "persistent work-tracking document. "
            "channel: optional channel ID to read another channel's canvas. "
            "Omit or leave empty to read the current session's canvas."
        ),
        {"channel": str},
    )
    async def summon_canvas_read(args: dict) -> dict:
        target_channel = args.get("channel", "").strip()
        if not target_channel or target_channel == channel_id:
            return {"content": [{"type": "text", "text": canvas_store.read()}]}
        # Cross-channel read via registry
        try:
            _, canvas_markdown, owner_user_id = await registry.get_canvas_by_channel(target_channel)
            if canvas_markdown is None or owner_user_id != authenticated_user_id:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: No canvas found for channel {target_channel}.",
                        }
                    ],
                    "is_error": True,
                }
            # Wrap bug hunter canvas content as untrusted — findings are external
            # data that must never be followed as instructions (SEC-D-013).
            # Check regardless of session status: a completed/errored bug hunter
            # session's canvas content is still untrusted.
            async with registry.db.execute(
                "SELECT session_name FROM sessions "
                "WHERE slack_channel_id = ? AND session_name = ? LIMIT 1",
                (target_channel, BUG_HUNTER_SESSION_NAME),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    canvas_markdown = mark_untrusted(canvas_markdown, source="bug-hunter-canvas")
            return {"content": [{"type": "text", "text": canvas_markdown}]}
        except Exception:
            logger.exception("Cross-channel canvas read failed for %s", target_channel)
            return {
                "content": [{"type": "text", "text": "Error: could not read canvas."}],
                "is_error": True,
            }

    @tool(
        "summon_canvas_write",
        (
            "Replace the entire channel canvas with new markdown content. "
            "WARNING: this overwrites all existing canvas content. "
            "Prefer summon_canvas_update_section for partial updates. "
            "markdown: the full canvas content (required, max 100K characters)."
        ),
        {"markdown": str},
    )
    async def summon_canvas_write(args: dict) -> dict:
        markdown = args.get("markdown", "")
        if not markdown or not markdown.strip():
            return {
                "content": [{"type": "text", "text": "Error: markdown content is required."}],
                "is_error": True,
            }
        if len(markdown) > _CANVAS_MAX_CHARS:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Error: markdown content exceeds 100K character limit"
                            f" ({len(markdown)} chars)."
                        ),
                    }
                ],
                "is_error": True,
            }
        try:
            await canvas_store.write(markdown)
            return {"content": [{"type": "text", "text": "Canvas updated."}]}
        except Exception:
            logger.exception("Canvas write failed")
            return {
                "content": [{"type": "text", "text": "Error: could not write canvas."}],
                "is_error": True,
            }

    @tool(
        "summon_canvas_update_section",
        (
            "Update a single section of the channel canvas by heading name. "
            "If the section exists, replaces its content. If not found, appends a new section. "
            "heading: the section heading text WITHOUT the ## prefix (required). "
            "markdown: the new section body content (required, max 100K characters). "
            "Pass empty string to clear a section while keeping the heading."
        ),
        {"heading": str, "markdown": str},
    )
    async def summon_canvas_update_section(args: dict) -> dict:
        heading = args.get("heading", "")
        markdown = args.get("markdown", "")
        if not heading:
            return {
                "content": [{"type": "text", "text": "Error: heading is required."}],
                "is_error": True,
            }
        if len(markdown) > _CANVAS_MAX_CHARS:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Error: markdown content exceeds 100K character limit"
                            f" ({len(markdown)} chars)."
                        ),
                    }
                ],
                "is_error": True,
            }
        try:
            await canvas_store.update_section(heading, markdown)
            return {"content": [{"type": "text", "text": f"Section '{heading}' updated."}]}
        except ValueError as e:
            return {
                "content": [{"type": "text", "text": f"Error: {e}"}],
                "is_error": True,
            }
        except Exception:
            logger.exception("Canvas update_section failed for heading=%s", heading)
            return {
                "content": [{"type": "text", "text": "Error: could not update canvas section."}],
                "is_error": True,
            }

    return [summon_canvas_read, summon_canvas_write, summon_canvas_update_section]


def create_canvas_mcp_server(
    canvas_store: CanvasStore,
    registry: SessionRegistry,
    authenticated_user_id: str,
    channel_id: str,
) -> McpSdkServerConfig:
    """Create an MCP server with canvas tools."""
    tools = create_canvas_mcp_tools(canvas_store, registry, authenticated_user_id, channel_id)
    return create_sdk_mcp_server(name="summon-canvas", version="1.0.0", tools=tools)

"""In-process MCP tools that give Claude direct Slack channel access."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from claude_agent_sdk import create_sdk_mcp_server, tool

from summon_claude.thread_router import ThreadRouter

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool
    from claude_agent_sdk.types import McpSdkServerConfig

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_TEXT_CHARS = 3000  # Slack block limit

_PARENT_TS_RE = re.compile(r"^\d+\.\d+$")
_EMOJI_RE = re.compile(r"^[A-Za-z0-9_+]+$")


def _sanitize_mrkdwn_meta(value: str) -> str:
    """Strip mrkdwn formatting characters from metadata values (title, language)."""
    # Remove characters that break Slack mrkdwn structure
    return re.sub(r"[*`~\n]", "", value)


def create_summon_mcp_tools(router: ThreadRouter) -> list[SdkMcpTool]:
    """Create MCP tool instances bound to the given router."""

    @tool(
        "slack_upload_file",
        (
            "Upload a file to the Slack session channel turn thread. "
            "content: file text content. filename: name with extension (e.g. 'output.txt'). "
            "title: display title shown in Slack."
        ),
        {"content": str, "filename": str, "title": str},
    )
    async def upload_file(args: dict) -> dict:
        content = args["content"]
        if len(content.encode("utf-8", errors="replace")) > _MAX_UPLOAD_BYTES:
            return {
                "content": [{"type": "text", "text": "Error: file content exceeds 10 MB limit"}],
                "is_error": True,
            }
        try:
            await router.upload_to_turn_thread(
                args["content"],
                args["filename"],
                title=args.get("title", args["filename"]),
            )
        except Exception:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: failed to upload file."
                        " Check Slack API connectivity and permissions.",
                    }
                ],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": f"Uploaded {args['filename']} to Slack"}]}

    @tool(
        "slack_create_thread",
        (
            "Reply in a thread to a specific Slack message. "
            "parent_ts: message timestamp in '1234567890.123456' format (seconds.microseconds). "
            "text: reply text (max 3000 chars)."
        ),
        {"parent_ts": str, "text": str},
    )
    async def create_thread(args: dict) -> dict:
        parent_ts = args.get("parent_ts", "")
        if not _PARENT_TS_RE.match(parent_ts):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Error: invalid parent_ts format. "
                            "Expected 'seconds.microseconds' e.g. '1234567890.123456'."
                        ),
                    }
                ],
                "is_error": True,
            }
        text = args["text"][:_MAX_TEXT_CHARS]
        try:
            await router.provider.post_message(
                router.channel_id,
                text,
                thread_ts=parent_ts,
            )
        except Exception:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: failed to post thread reply."
                        " Check Slack API connectivity and permissions.",
                    }
                ],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": "Thread reply posted"}]}

    @tool(
        "slack_react",
        (
            "Add an emoji reaction to a Slack message. "
            "timestamp: message timestamp in '1234567890.123456' format. "
            "emoji: emoji name without colons (e.g. 'thumbsup', 'white_check_mark')."
        ),
        {"timestamp": str, "emoji": str},
    )
    async def react(args: dict) -> dict:
        emoji_name = args["emoji"].strip(":")
        if not _EMOJI_RE.match(emoji_name):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Error: invalid emoji name. "
                            "Use alphanumeric characters, underscores, or plus signs only."
                        ),
                    }
                ],
                "is_error": True,
            }
        ts = args["timestamp"]
        if not _PARENT_TS_RE.match(ts):
            return {
                "content": [{"type": "text", "text": f"Error: invalid timestamp format: {ts}"}],
                "is_error": True,
            }
        try:
            await router.add_reaction(
                router.channel_id,
                ts,
                emoji_name,
            )
        except Exception:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: failed to add reaction."
                        " Check Slack API connectivity and permissions.",
                    }
                ],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": f"Added :{emoji_name}: reaction"}]}

    @tool(
        "slack_post_snippet",
        (
            "Post a formatted code snippet with syntax highlighting to the turn thread. "
            "code: source code content. "
            "language: syntax highlighting language (e.g. 'python', 'bash', 'json'). "
            "title: display title for the snippet."
        ),
        {"code": str, "language": str, "title": str},
    )
    async def post_snippet(args: dict) -> dict:
        code = args["code"][: _MAX_TEXT_CHARS - 20]  # Reserve space for fences/title
        lang = _sanitize_mrkdwn_meta(args.get("language", ""))
        title = _sanitize_mrkdwn_meta(args.get("title", "Code"))
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title}*\n```{lang}\n{code}\n```",
                },
            }
        ]
        try:
            await router.post_to_turn_thread(title, blocks=blocks)
        except Exception:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: failed to post snippet."
                        " Check Slack API connectivity and permissions.",
                    }
                ],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": "Code snippet posted to Slack"}]}

    return [upload_file, create_thread, react, post_snippet]


def create_summon_mcp_server(router: ThreadRouter) -> McpSdkServerConfig:
    """Create an MCP server with Slack tools bound to the current session."""
    tools = create_summon_mcp_tools(router)
    return create_sdk_mcp_server(name="summon-slack", version="1.0.0", tools=tools)

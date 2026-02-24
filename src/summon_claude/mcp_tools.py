"""In-process MCP tools that give Claude direct Slack channel access."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_agent_sdk import create_sdk_mcp_server, tool

from summon_claude.thread_router import ThreadRouter

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpSdkServerConfig

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_TEXT_CHARS = 3000  # Slack block limit


def create_summon_mcp_server(router: ThreadRouter) -> McpSdkServerConfig:
    """Create an MCP server with Slack tools bound to the current session."""

    @tool(
        "slack_upload_file",
        "Upload a file to the Slack session channel",
        {"content": str, "filename": str, "title": str},
    )
    async def upload_file(args: dict) -> dict:
        content = args["content"]
        if len(content.encode("utf-8", errors="replace")) > _MAX_UPLOAD_BYTES:
            return {
                "content": [{"type": "text", "text": "Error: file content exceeds 10 MB limit"}]
            }
        await router.upload_to_turn_thread(
            args["content"],
            args["filename"],
            title=args.get("title", args["filename"]),
        )
        return {"content": [{"type": "text", "text": f"Uploaded {args['filename']} to Slack"}]}

    @tool(
        "slack_create_thread",
        "Reply in a thread to a specific message",
        {"parent_ts": str, "text": str},
    )
    async def create_thread(args: dict) -> dict:
        text = args["text"][:_MAX_TEXT_CHARS]
        await router.provider.post_message(
            router.channel_id,
            text,
            thread_ts=args["parent_ts"],
        )
        return {"content": [{"type": "text", "text": "Thread reply posted"}]}

    @tool(
        "slack_react",
        "Add an emoji reaction to a message",
        {"timestamp": str, "emoji": str},
    )
    async def react(args: dict) -> dict:
        emoji_name = args["emoji"].strip(":")
        await router.add_reaction(
            router.channel_id,
            args["timestamp"],
            emoji_name,
        )
        return {"content": [{"type": "text", "text": f"Added :{emoji_name}: reaction"}]}

    @tool(
        "slack_post_snippet",
        "Post a formatted code snippet with syntax highlighting",
        {"code": str, "language": str, "title": str},
    )
    async def post_snippet(args: dict) -> dict:
        code = args["code"][: _MAX_TEXT_CHARS - 20]  # Reserve space for fences/title
        lang = args.get("language", "")
        title = args.get("title", "Code")
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title}*\n```{lang}\n{code}\n```",
                },
            }
        ]
        await router.post_to_turn_thread(title, blocks=blocks)
        return {"content": [{"type": "text", "text": "Code snippet posted to Slack"}]}

    return create_sdk_mcp_server(
        name="summon-slack",
        version="1.0.0",
        tools=[upload_file, create_thread, react, post_snippet],
    )

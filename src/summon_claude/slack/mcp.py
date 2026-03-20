"""MCP tools — Slack actions and reading tools bound via SlackClient."""

# pyright: reportArgumentType=false, reportReturnType=false
# claude_agent_sdk doesn't ship type stubs

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultDeny,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from summon_claude.slack.client import SlackClient

if TYPE_CHECKING:
    from claude_agent_sdk import SdkMcpTool
    from claude_agent_sdk.types import McpSdkServerConfig

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_TEXT_CHARS = 3000  # Slack section block limit
_MARKDOWN_BLOCK_LIMIT = 12000  # Slack type: markdown block cumulative limit

_PARENT_TS_RE = re.compile(r"^\d+\.\d+$")
_EMOJI_RE = re.compile(r"^[A-Za-z0-9_+]+$")

_RAW_MAX_BYTES = 100_000

_NOISE_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "group_topic",
        "group_purpose",
        "group_name",
        "group_archive",
        "group_unarchive",
        "pinned_item",
        "unpinned_item",
    }
)

_URL_RE = re.compile(r"archives/([A-Z0-9]+)/p(\d+)")

_VALID_FORMATS = frozenset({"summary", "raw", "ai"})

logger = logging.getLogger(__name__)

# Serializes CLAUDECODE env var manipulation across concurrent sessions.
# The SDK subprocess inherits the parent env at fork time — we must ensure
# only one task mutates os.environ at a time during client startup.
_ai_env_lock = asyncio.Lock()


_AI_MAX_INPUT_CHARS = 150_000  # ~37K tokens, well within Haiku's context
_AI_TIMEOUT_SECONDS = 30


async def _ai_summarize(messages: list[dict[str, Any]], *, cwd: str = ".") -> str:
    """Spawn a Haiku SDK session to summarize Slack messages."""
    raw = json.dumps(messages, default=str)
    if len(raw) > _AI_MAX_INPUT_CHARS:
        raw = raw[:_AI_MAX_INPUT_CHARS] + "\n... (truncated)"

    async def _deny_all_tools(
        tool_name: str,  # noqa: ARG001
        input_data: dict[str, Any],  # noqa: ARG001
        context: Any,  # noqa: ARG001
    ) -> PermissionResultDeny:
        return PermissionResultDeny(message="Tool use not allowed in summarization session")

    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        effort="low",
        max_turns=1,
        cwd=cwd,
        can_use_tool=_deny_all_tools,
        system_prompt=(
            "Summarize this Slack conversation concisely. "
            "Preserve decisions, action items, and key context. "
            "Include timestamps and user IDs for reference."
        ),
    )
    # Hold lock only through subprocess spawn, not the full query.
    async with _ai_env_lock:
        saved = os.environ.pop("CLAUDECODE", None)
        try:
            client_ctx = ClaudeSDKClient(options)
            haiku = await client_ctx.__aenter__()
        except BaseException:
            if saved is not None:
                os.environ["CLAUDECODE"] = saved
            raise
        if saved is not None:
            os.environ["CLAUDECODE"] = saved

    try:
        await haiku.query(f"Summarize:\n\n{raw}")
        parts: list[str] = []
        async for msg in haiku.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
        return "".join(parts).strip() or "No summary generated."
    finally:
        await client_ctx.__aexit__(None, None, None)


async def _ai_format_response(
    messages: list[dict[str, Any]], *, has_more: bool = False, cwd: str = "."
) -> dict:
    """Summarize messages with AI, falling back to summary format on error."""
    try:
        summary = await asyncio.wait_for(
            _ai_summarize(messages, cwd=cwd), timeout=_AI_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning("AI summarization failed, falling back to summary", exc_info=True)
        summary = "\n".join(_format_message_summary(m) for m in _filter_noise(messages))
    if has_more:
        summary += "\n(more messages available — adjust limit or oldest to paginate)"
    return {"content": [{"type": "text", "text": summary}]}


def _sanitize_mrkdwn_meta(value: str) -> str:
    """Strip mrkdwn formatting characters from metadata values (title, language).

    Distinct from sanitize_for_mrkdwn — strips *`~\\n only, no \\r replacement,
    no truncation.
    """
    return re.sub(r"[*`~\n]", "", value)


def _format_message_summary(msg: dict[str, Any]) -> str:
    ts = msg.get("ts", "?")
    user = msg.get("user", msg.get("bot_id", "unknown"))
    raw_text = msg.get("text", "")
    text = raw_text[:500] + ("…" if len(raw_text) > 500 else "")
    reply_count = msg.get("reply_count", 0)
    suffix = f" [{reply_count} replies]" if reply_count else ""
    subtype = msg.get("subtype", "")
    tag = f" ({subtype})" if subtype else ""
    return f"[{ts}] <{user}>{tag}: {text}{suffix}"


def _filter_noise(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("subtype") not in _NOISE_SUBTYPES]


def _format_messages(
    messages: list[dict[str, Any]], fmt: str, *, has_more: bool = False
) -> list[dict[str, Any]]:
    if not messages:
        return [{"type": "text", "text": "No messages found."}]
    more_note = (
        "\n(more messages available — adjust limit or oldest to paginate)" if has_more else ""
    )
    if fmt == "raw":
        parts: list[str] = []
        total = 2  # [ and ]
        for msg in messages:
            encoded = json.dumps(msg, default=str)
            needed = len(encoded) + (2 if parts else 0)
            if total + needed > _RAW_MAX_BYTES:
                break
            parts.append(encoded)
            total += needed
        raw = "[" + ", ".join(parts) + "]"
        if len(parts) < len(messages):
            raw += f"\n({len(messages)} messages total, showing {len(parts)})"
        return [{"type": "text", "text": raw + more_note}]
    filtered = _filter_noise(messages)
    if not filtered:
        return [
            {
                "type": "text",
                "text": "No conversation messages found (only system messages)." + more_note,
            }
        ]
    lines = [_format_message_summary(m) for m in filtered]
    return [{"type": "text", "text": "\n".join(lines) + more_note}]


def create_summon_mcp_tools(  # noqa: PLR0915
    client: SlackClient,
    allowed_channels: Callable[[], Awaitable[set[str]]],
    cwd: str = ".",
) -> list[SdkMcpTool]:
    """Create MCP tool instances bound to the given SlackClient.

    All tools post to the main channel only — no active-thread state.
    BEHAVIOR CHANGE from mcp_tools.py: slack_upload_file and slack_post_snippet
    previously posted to the active turn thread; they now post to main channel.
    """

    async def _check_channel(channel: str | None) -> str:
        resolved = channel or client.channel_id
        allowed = await allowed_channels()
        if resolved not in allowed:
            raise ValueError("Channel access denied")
        return resolved

    @tool(
        "slack_upload_file",
        (
            "Upload a file to the Slack session channel. "
            "content: file text content. filename: name with extension (e.g. 'output.txt'). "
            "title: display title shown in Slack. "
            "snippet_type: optional syntax highlighting type (e.g. 'diff', 'python', 'json'). "
            "Enables Slack's native syntax highlighting for the uploaded file."
        ),
        {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "filename": {"type": "string"},
                "title": {"type": "string"},
                "snippet_type": {"type": "string"},
            },
            "required": ["content", "filename", "title"],
        },
    )
    async def upload_file(args: dict) -> dict:
        try:
            await _check_channel(None)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
        content = args["content"]
        if len(content.encode("utf-8", errors="replace")) > _MAX_UPLOAD_BYTES:
            return {
                "content": [{"type": "text", "text": "Error: file content exceeds 10 MB limit"}],
                "is_error": True,
            }
        try:
            await client.upload(
                args["content"],
                args["filename"],
                title=args.get("title", args["filename"]),
                snippet_type=args.get("snippet_type"),
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
        try:
            await _check_channel(None)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
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
            await client.post(text, thread_ts=parent_ts)
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
        try:
            await _check_channel(None)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
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
            await client.react(ts, emoji_name)
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
            "Post a formatted code snippet with syntax highlighting to the channel. "
            "code: source code content. "
            "language: Slack snippet type for syntax highlighting. Use exact values: "
            "python, javascript, typescript, shell, go, rust, ruby, java, kotlin, "
            "swift, c, cpp, csharp, html, css, json, yaml, toml, xml, sql, diff, "
            "markdown, text. "
            "title: display title for the snippet."
        ),
        {"code": str, "language": str, "title": str},
    )
    async def post_snippet(args: dict) -> dict:
        try:
            await _check_channel(None)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
        code = args["code"]
        if len(code.encode("utf-8", errors="replace")) > _MAX_UPLOAD_BYTES:
            return {
                "content": [{"type": "text", "text": "Error: code content exceeds 10 MB limit"}],
                "is_error": True,
            }
        lang = _sanitize_mrkdwn_meta(args.get("language", ""))
        title = _sanitize_mrkdwn_meta(args.get("title", "Code"))

        formatted = f"*{title}*\n```{lang}\n{code}\n```"

        # Content > 12K → file upload fallback
        if len(formatted) > _MARKDOWN_BLOCK_LIMIT:
            snippet_type = lang.lower() or None
            try:
                await client.upload(
                    code,
                    f"snippet.{lang or 'txt'}",
                    title=title,
                    snippet_type=snippet_type,
                )
            except Exception:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: failed to upload snippet."
                            " Check Slack API connectivity and permissions.",
                        }
                    ],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": "Code snippet uploaded to Slack"}]}

        # Use type: markdown block (12K limit)
        blocks = [{"type": "markdown", "text": formatted}]
        try:
            await client.post(title, blocks=blocks)
        except Exception:
            # Fallback to section/mrkdwn if markdown blocks fail
            try:
                # Account for title/lang/fence overhead in 3K section limit
                overhead = len(f"*{title}*\n```{lang}\n\n```")
                truncated = code[: max(_MAX_TEXT_CHARS - overhead, 100)]
                fallback_blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{title}*\n```{lang}\n{truncated}\n```",
                        },
                    }
                ]
                await client.post(title, blocks=fallback_blocks)
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

    @tool(
        "slack_update_message",
        (
            "Update an existing Slack message. "
            "ts: message timestamp in '1234567890.123456' format. "
            "text: new message text (max 3000 chars). "
            "channel: channel ID (default: session channel)."
        ),
        {"ts": str, "text": str, "channel": str},
    )
    async def update_message(args: dict) -> dict:
        ts = args.get("ts", "")
        if not _PARENT_TS_RE.match(ts):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Error: invalid ts format. "
                            "Expected 'seconds.microseconds' e.g. '1234567890.123456'."
                        ),
                    }
                ],
                "is_error": True,
            }
        channel = args.get("channel") or client.channel_id
        try:
            await _check_channel(channel)
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
        text = args.get("text", "")[:_MAX_TEXT_CHARS]
        if not text:
            return {
                "content": [{"type": "text", "text": "Error: text is required."}],
                "is_error": True,
            }
        try:
            await client.update(ts, text, channel=channel)
        except Exception:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: failed to update message."
                        " Check Slack API connectivity and permissions.",
                    }
                ],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": "Message updated"}]}

    # ------------------------------------------------------------------
    # Reading tools
    # ------------------------------------------------------------------

    @tool(
        "slack_read_history",
        (
            "Read recent messages from a Slack channel. "
            "Returns channel message history (top-level messages only, no thread replies) "
            "in newest-first order. "
            "Use slack_fetch_thread to read thread replies for a specific message. "
            "Note: when reading your own session channel, you will see your own messages. "
            "System messages (join/leave/topic) are filtered from summary format. "
            "limit: max messages to return (default 50, max 200). "
            "oldest: only return messages after this Unix timestamp (e.g. '1234567890.123456'). "
            "channel: channel ID to read (default: session channel). "
            "format: 'summary' (default) for compact output, 'raw' for full Slack API data, "
            "'ai' for AI-generated summary (slower, uses a Haiku session)."
        ),
        {"limit": int, "oldest": str, "channel": str, "format": str},
    )
    async def read_history(args: dict) -> dict:
        try:
            limit = max(1, min(args.get("limit", 50), 200))
            oldest = args.get("oldest")
            if oldest and not _PARENT_TS_RE.match(oldest):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: invalid oldest format."
                            " Expected 'seconds.microseconds'.",
                        }
                    ],
                    "is_error": True,
                }
            channel = await _check_channel(args.get("channel"))
            fmt = args.get("format", "summary")
            if fmt not in _VALID_FORMATS:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: invalid format '{fmt}'."
                            " Must be 'summary', 'raw', or 'ai'.",
                        }
                    ],
                    "is_error": True,
                }
            result = await client.fetch_history(channel=channel, limit=limit, oldest=oldest)
            if fmt == "ai":
                return await _ai_format_response(result.messages, has_more=result.has_more, cwd=cwd)
            return {"content": _format_messages(result.messages, fmt, has_more=result.has_more)}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error reading history: {e}"}],
                "is_error": True,
            }

    @tool(
        "slack_fetch_thread",
        (
            "Read replies in a Slack message thread. "
            "Results include the parent message as the first entry, followed by replies "
            "in chronological order. "
            "parent_ts: timestamp of the thread's parent message "
            "(required, e.g. '1234567890.123456'). "
            "limit: max replies to return (default 50, max 200). "
            "channel: channel ID (default: session channel). "
            "format: 'summary' (default) for compact output, 'raw' for full Slack API data, "
            "'ai' for AI-generated summary (slower, uses a Haiku session)."
        ),
        {"parent_ts": str, "limit": int, "channel": str, "format": str},
    )
    async def fetch_thread(args: dict) -> dict:
        parent_ts = args.get("parent_ts", "")
        if not _PARENT_TS_RE.match(parent_ts):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: invalid parent_ts format. Expected 'seconds.microseconds'.",
                    }
                ],
                "is_error": True,
            }
        try:
            limit = max(1, min(args.get("limit", 50), 200))
            channel = await _check_channel(args.get("channel"))
            fmt = args.get("format", "summary")
            if fmt not in _VALID_FORMATS:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: invalid format '{fmt}'."
                            " Must be 'summary', 'raw', or 'ai'.",
                        }
                    ],
                    "is_error": True,
                }
            result = await client.fetch_thread_replies(parent_ts, channel=channel, limit=limit)
            if fmt == "ai":
                return await _ai_format_response(result.messages, has_more=result.has_more, cwd=cwd)
            return {"content": _format_messages(result.messages, fmt, has_more=result.has_more)}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error fetching thread: {e}"}],
                "is_error": True,
            }

    @tool(
        "slack_get_context",
        (
            "Get messages surrounding a specific Slack message, identified by URL or "
            "channel+timestamp. "
            "Use this when a user references a Slack message URL to understand the "
            "conversation context. "
            "This tool makes 2-3 API calls per invocation. "
            "url: a Slack message URL (e.g. "
            "'https://workspace.slack.com/archives/C0123/p1234567890123456'). "
            "channel: channel ID (alternative to URL). "
            "message_ts: message timestamp (alternative to URL, requires channel). "
            "surrounding: number of messages before and after the target (default 5, max 20). "
            "format: 'summary' (default) for compact output, 'raw' for full Slack API data, "
            "'ai' for AI-generated summary (slower, uses a Haiku session)."
        ),
        {"url": str, "channel": str, "message_ts": str, "surrounding": int, "format": str},
    )
    async def get_context(args: dict) -> dict:  # noqa: PLR0911, PLR0912
        try:
            fmt = args.get("format", "summary")
            if fmt not in _VALID_FORMATS:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: invalid format '{fmt}'."
                            " Must be 'summary', 'raw', or 'ai'.",
                        }
                    ],
                    "is_error": True,
                }
            surrounding = max(1, min(args.get("surrounding", 5), 20))
            url = args.get("url")
            channel = args.get("channel")
            message_ts = args.get("message_ts")

            # Parse URL if provided
            thread_ts_from_url = None
            if url:
                m = _URL_RE.search(url)
                if not m:
                    return {
                        "content": [{"type": "text", "text": "Error: could not parse Slack URL."}],
                        "is_error": True,
                    }
                channel = m.group(1)
                digits = m.group(2)
                message_ts = digits[:10] + "." + digits[10:]

                # Check for threaded URL
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                if "thread_ts" in qs:
                    thread_ts_from_url = qs["thread_ts"][0]

            if not channel or not message_ts:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: provide either url or both channel and message_ts.",
                        }
                    ],
                    "is_error": True,
                }

            # Validate timestamp formats
            if not _PARENT_TS_RE.match(message_ts):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: invalid message_ts format."
                            " Expected 'seconds.microseconds'.",
                        }
                    ],
                    "is_error": True,
                }
            if thread_ts_from_url and not _PARENT_TS_RE.match(thread_ts_from_url):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "Error: invalid thread_ts format in URL.",
                        }
                    ],
                    "is_error": True,
                }

            channel = await _check_channel(channel)

            # Threaded URL: fetch thread, not channel context
            if thread_ts_from_url:
                result = await client.fetch_thread_replies(
                    thread_ts_from_url, channel=channel, limit=200
                )
                if fmt == "ai":
                    return await _ai_format_response(
                        result.messages, has_more=result.has_more, cwd=cwd
                    )
                content = _format_messages(result.messages, fmt, has_more=result.has_more)
                header = (
                    f"Thread context (parent: {thread_ts_from_url}, highlight: {message_ts}):\n"
                )
                if content and content[0].get("type") == "text":
                    content[0]["text"] = header + content[0]["text"]
                return {"content": content}

            # Standard URL / manual params: fetch channel context
            ctx = await client.fetch_context(message_ts, channel=channel, surrounding=surrounding)

            if fmt == "ai":
                all_msgs = list(ctx["messages"])
                if ctx.get("thread"):
                    all_msgs.extend(ctx["thread"])
                return await _ai_format_response(all_msgs, cwd=cwd)

            text_parts = []

            # Format channel context (oldest-first)
            msg_content = _format_messages(ctx["messages"], fmt)
            if msg_content:
                text_parts.append(
                    f"Channel context around {message_ts}:\n" + msg_content[0].get("text", "")
                )

            # Format thread if present
            if ctx.get("thread"):
                thread_content = _format_messages(ctx["thread"], fmt)
                if thread_content:
                    text_parts.append("\nThread replies:\n" + thread_content[0].get("text", ""))

            return {"content": [{"type": "text", "text": "\n".join(text_parts)}]}

        except ValueError as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error getting context: {e}"}],
                "is_error": True,
            }

    return [
        upload_file,
        create_thread,
        react,
        post_snippet,
        update_message,
        read_history,
        fetch_thread,
        get_context,
    ]


def create_summon_mcp_server(
    client: SlackClient,
    allowed_channels: Callable[[], Awaitable[set[str]]],
    cwd: str = ".",
) -> McpSdkServerConfig:
    """Create an MCP server with Slack tools bound to the current SlackClient."""
    tools = create_summon_mcp_tools(client, allowed_channels=allowed_channels, cwd=cwd)
    return create_sdk_mcp_server(name="summon-slack", version="1.0.0", tools=tools)

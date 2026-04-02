"""SlackClient — channel-bound Slack output client (Layer 1)."""

# pyright: reportArgumentType=false, reportReturnType=false
# slack_sdk doesn't ship type stubs

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.security import validate_agent_output

logger = logging.getLogger(__name__)

ZZZ_PREFIX = "zzz-"
_SLACK_CHANNEL_NAME_MAX = 80


def make_zzz_name(channel_name: str) -> str:
    """Return channel_name with zzz- prefix, truncated to Slack's 80-char limit.

    Idempotent: already-prefixed names are returned truncated but not double-prefixed.
    """
    if channel_name.startswith(ZZZ_PREFIX):
        return channel_name[:_SLACK_CHANNEL_NAME_MAX]
    return ZZZ_PREFIX + channel_name[: _SLACK_CHANNEL_NAME_MAX - len(ZZZ_PREFIX)]


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Reference to a posted message (channel + timestamp)."""

    channel_id: str
    ts: str


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """Result from Slack conversations.history or conversations.replies."""

    messages: list[dict[str, Any]]
    has_more: bool


def sanitize_for_mrkdwn(text: str, max_len: int = 100) -> str:
    """Remove mrkdwn-significant characters and newlines to prevent injection."""
    sanitized = text.replace("\n", " ").replace("\r", " ").replace("`", "'").replace("*", "")
    return sanitized if max_len >= len(sanitized) else sanitized[:max_len]


# Note: Atlassian OAuth access tokens are standard JWTs (eyJ...) with no
# Atlassian-specific prefix. We match any sufficiently-long base64url JWT
# starting with eyJ (the base64 prefix for any JSON header starting with
# '{\"'). The 40-char minimum avoids short base64 fragments. Defense-in-depth
# alongside the architectural defence (tokens only flow to MCP subprocess,
# not Slack output).
_SECRET_RE = re.compile(
    r"xox[a-z]-[A-Za-z0-9\-]+|xapp-[A-Za-z0-9\-]+|sk-ant-[A-Za-z0-9\-]+"
    r"|ghp_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+"
    r"|gho_[A-Za-z0-9_]+|ghu_[A-Za-z0-9_]+|ghs_[A-Za-z0-9_]+|ghr_[A-Za-z0-9_]+"
    r"|eyJ[A-Za-z0-9._\-]{40,}"
    r"|X-Summon-Proxy-Token:\s*\S+"
)


def redact_secrets(text: str) -> str:
    """Replace secret token patterns with [REDACTED] to prevent leakage."""
    return _SECRET_RE.sub("[REDACTED]", text)


def sanitize_for_slack(text: str) -> str:
    """Strip Slack mention markup and redact secrets for safe Slack display."""
    safe = re.sub(r"<!(channel|here|everyone)>", r"\1", text)
    safe = re.sub(r"<@(U\w+)>", r"user:\1", safe)
    safe = re.sub(r"<!subteam\^[^>]+>", "group", safe)
    # Neutralize hyperlink syntax: <url|label> → label [url]
    safe = re.sub(r"<(https?://[^|>]+)\|([^>]*)>", r"\2 [\1]", safe)
    return redact_secrets(safe)


def _redact_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Redact secrets in Block Kit structures by round-tripping through JSON."""
    raw = json.dumps(blocks)
    if _SECRET_RE.search(raw):
        return json.loads(_SECRET_RE.sub("[REDACTED]", raw))
    return blocks


class SlackClient:
    """Channel-bound Slack output client (Layer 1).

    Created AFTER a channel exists. All ongoing session Slack output goes
    through this. Does NOT handle channel creation, invite, or archive —
    those are pre-session operations on the raw web_client.
    """

    def __init__(self, web_client: AsyncWebClient, channel_id: str) -> None:
        self._web = web_client  # private — no outside access
        self.channel_id = channel_id  # plain attribute, immutable after init

    async def post(
        self,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post a message to the channel."""
        text = redact_secrets(text)
        text, sec_warnings = validate_agent_output(text)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        kwargs: dict[str, Any] = {"channel": self.channel_id, "text": text}
        if blocks:
            kwargs["blocks"] = _redact_blocks(blocks)
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = await self._web.chat_postMessage(**kwargs)
        return MessageRef(channel_id=resp["channel"], ts=resp["ts"])

    async def post_ephemeral(
        self,
        user_id: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        """Post an ephemeral message visible only to user_id."""
        text = redact_secrets(text)
        text, sec_warnings = validate_agent_output(text)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        kwargs: dict[str, Any] = {
            "channel": self.channel_id,
            "user": user_id,
            "text": text,
        }
        if blocks:
            kwargs["blocks"] = _redact_blocks(blocks)
        try:
            await self._web.chat_postEphemeral(**kwargs)
        except Exception as e:
            logger.warning("Failed to post ephemeral to %s: %s", user_id, e)

    async def update(
        self,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        channel: str | None = None,
    ) -> None:
        """Update an existing message."""
        text = redact_secrets(text)
        text, sec_warnings = validate_agent_output(text)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        kwargs: dict[str, Any] = {"channel": channel or self.channel_id, "ts": ts, "text": text}
        if blocks:
            kwargs["blocks"] = _redact_blocks(blocks)
        await self._web.chat_update(**kwargs)

    async def react(self, ts: str, emoji: str) -> None:
        """Add a reaction to a message."""
        try:
            await self._web.reactions_add(
                channel=self.channel_id,
                timestamp=ts,
                name=emoji.strip(":"),
            )
        except Exception as e:
            logger.debug("Failed to add reaction :%s: — %s", emoji, e)

    async def unreact(self, ts: str, emoji: str) -> None:
        """Remove a reaction from a message."""
        try:
            await self._web.reactions_remove(
                channel=self.channel_id,
                timestamp=ts,
                name=emoji.strip(":"),
            )
        except Exception as e:
            logger.debug("Failed to remove reaction :%s: — %s", emoji, e)

    async def set_thread_status(self, thread_ts: str, status: str) -> None:
        """Set assistant thread status indicator (typing-style).

        Auto-clears when bot sends a reply. Send empty string to clear explicitly.
        Requires chat:write scope (as of March 2026).
        """
        try:
            await self._web.assistant_threads_setStatus(
                channel_id=self.channel_id,
                thread_ts=thread_ts,
                status=status,
            )
        except Exception as e:
            logger.debug("Failed to set thread status — %s", e)

    async def upload(
        self,
        content: str,
        filename: str,
        *,
        title: str = "",
        thread_ts: str | None = None,
        snippet_type: str | None = None,
    ) -> None:
        """Upload a file to the channel."""
        content = redact_secrets(content)
        content, sec_warnings = validate_agent_output(content)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        kwargs: dict[str, Any] = {
            "channel": self.channel_id,
            "content": content,
            "filename": filename,
            "title": title or filename,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if snippet_type:
            kwargs["snippet_type"] = snippet_type
        await self._web.files_upload_v2(**kwargs)

    async def set_topic(self, topic: str) -> None:
        """Set the channel topic."""
        topic = redact_secrets(topic)
        topic, sec_warnings = validate_agent_output(topic)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        await self._web.conversations_setTopic(channel=self.channel_id, topic=topic)

    async def rename_channel(self, new_name: str) -> str | None:
        """Rename the channel (best-effort).

        Returns the normalized name from Slack on success, None on failure (logs warning).
        """
        try:
            resp = await self._web.conversations_rename(channel=self.channel_id, name=new_name)
            return resp["channel"]["name"]  # type: ignore[index]
        except Exception as e:
            logger.warning("rename_channel failed for %s → %s: %s", self.channel_id, new_name, e)
            return None

    async def fetch_history(
        self,
        *,
        channel: str | None = None,
        limit: int = 50,
        oldest: str | None = None,
    ) -> HistoryResult:
        """Fetch channel message history (top-level messages only)."""
        kwargs: dict[str, Any] = {
            "channel": channel or self.channel_id,
            "limit": limit,
        }
        if oldest is not None:
            kwargs["oldest"] = oldest
        response = await self._web.conversations_history(**kwargs)
        return HistoryResult(
            messages=response.get("messages", []),
            has_more=response.get("has_more", False),
        )

    async def fetch_thread_replies(
        self,
        thread_ts: str,
        *,
        channel: str | None = None,
        limit: int = 50,
    ) -> HistoryResult:
        """Fetch replies in a thread. First message is the parent."""
        response = await self._web.conversations_replies(
            channel=channel or self.channel_id,
            ts=thread_ts,
            limit=limit,
        )
        return HistoryResult(
            messages=response.get("messages", []),
            has_more=response.get("has_more", False),
        )

    async def fetch_context(
        self,
        message_ts: str,
        *,
        channel: str | None = None,
        surrounding: int = 5,
    ) -> dict[str, Any]:
        """Fetch messages surrounding a specific timestamp."""
        ch = channel or self.channel_id
        # Slack inclusive=True on latest → includes the target message itself.
        # Slack oldest defaults to inclusive=False → excludes target from "after".
        # Dedup via by_ts handles any edge-case overlap.
        before_resp = await self._web.conversations_history(
            channel=ch,
            latest=message_ts,
            limit=surrounding,
            inclusive=True,
        )
        after_resp = await self._web.conversations_history(
            channel=ch,
            oldest=message_ts,
            limit=surrounding,
        )
        # Merge and dedupe by ts
        by_ts: dict[str, dict[str, Any]] = {}
        for msg in before_resp.get("messages", []):
            by_ts[msg["ts"]] = msg
        for msg in after_resp.get("messages", []):
            by_ts[msg["ts"]] = msg
        messages = sorted(by_ts.values(), key=lambda m: m["ts"])

        # Check if target has thread replies
        thread = None
        target = by_ts.get(message_ts)
        if target and target.get("reply_count", 0) > 0:
            thread_result = await self.fetch_thread_replies(message_ts, channel=ch, limit=200)
            thread = thread_result.messages

        return {"messages": messages, "thread": thread, "target_ts": message_ts}

    # --- Canvas methods ---

    async def canvas_create(self, markdown: str, *, title: str = "Session Canvas") -> str | None:
        """Create a canvas in the channel and return its file ID.

        On free-plan workspaces ``canvases.create`` without a channel may fail,
        so we always pass ``channel_id``.  If creation fails entirely we fall
        back to finding an existing canvas via ``get_canvas_id``, overwriting
        its content and renaming it.

        Returns the canvas file ID, or ``None`` if all attempts fail.
        """
        markdown = redact_secrets(markdown)
        markdown, sec_warnings = validate_agent_output(markdown)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        try:
            resp = await self._web.api_call(
                "canvases.create",
                json={
                    "title": title,
                    "document_content": {"type": "markdown", "markdown": markdown},
                    "channel_id": self.channel_id,
                },
            )
            canvas_id: str | None = resp.get("canvas_id")
            if canvas_id:
                logger.info("Canvas created: %s in channel %s", canvas_id, self.channel_id)
                return canvas_id
        except Exception as e:
            logger.warning("canvases.create failed: %s — attempting fallback", e)

        # Fallback: find existing canvas, overwrite content and rename
        existing_id = await self.get_canvas_id()
        if existing_id:
            await self.canvas_sync(existing_id, markdown)
            await self.canvas_rename(existing_id, title)
        return existing_id

    async def canvas_sync(self, canvas_id: str, markdown: str) -> bool:
        """Update a canvas with new markdown content (best-effort).

        Replaces all content with a single ``replace`` operation.
        Returns ``True`` on success, ``False`` on failure (never raises).
        """
        markdown = redact_secrets(markdown)
        markdown, sec_warnings = validate_agent_output(markdown)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        try:
            await self._web.api_call(
                "canvases.edit",
                json={
                    "canvas_id": canvas_id,
                    "changes": [
                        {
                            "operation": "replace",
                            "document_content": {
                                "type": "markdown",
                                "markdown": markdown,
                            },
                        }
                    ],
                },
            )
            return True
        except Exception as e:
            logger.debug("canvas_sync failed for %s: %s", canvas_id, e)
            return False

    async def canvas_rename(self, canvas_id: str, title: str) -> bool:
        """Rename a canvas title (best-effort).

        Returns ``True`` on success, ``False`` on failure (never raises).
        """
        title = redact_secrets(title)
        title, sec_warnings = validate_agent_output(title)
        if sec_warnings:
            for w in sec_warnings:
                logger.warning("Output validation [%s]: %s", self.channel_id, w)
        try:
            await self._web.api_call(
                "canvases.edit",
                json={
                    "canvas_id": canvas_id,
                    "changes": [
                        {
                            "operation": "rename",
                            "title_content": {
                                "type": "markdown",
                                "markdown": title,
                            },
                        }
                    ],
                },
            )
            return True
        except Exception as e:
            logger.debug("canvas_rename failed for %s: %s", canvas_id, e)
            return False

    async def delete_message(self, ts: str) -> None:
        """Delete a message by timestamp (best-effort, never raises).

        Uses chat.delete. Failures are logged at debug level and swallowed
        so callers never need to handle errors from cleanup operations.
        """
        try:
            await self._web.chat_delete(channel=self.channel_id, ts=ts)
        except Exception as e:
            logger.debug("Failed to delete message %s: %s", ts, e)

    async def post_interactive(
        self,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post an interactive (button-bearing) message to the channel.

        Functionally identical to post(). Callers should store the returned
        ``MessageRef.ts`` for later deletion via ``delete_message()``.
        """
        return await self.post(text, thread_ts=thread_ts, blocks=blocks)

    async def get_canvas_id(self) -> str | None:
        """Discover an existing canvas in the channel via files.list.

        Returns the first canvas file ID found, or ``None``.
        """
        try:
            resp = await self._web.files_list(
                channel=self.channel_id,
                types="spaces",
                count=1,
            )
            files = resp.get("files", [])
            if files:
                return files[0].get("id")
        except Exception as e:
            logger.debug("files.list canvas discovery failed: %s", e)
        return None

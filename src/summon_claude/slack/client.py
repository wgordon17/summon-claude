"""SlackClient — channel-bound Slack output client (Layer 1)."""

# pyright: reportArgumentType=false, reportReturnType=false
# slack_sdk doesn't ship type stubs

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Reference to a posted message (channel + timestamp)."""

    channel_id: str
    ts: str


@dataclass(frozen=True, slots=True)
class ChannelRef:
    """Reference to a created channel."""

    channel_id: str
    name: str


def sanitize_for_mrkdwn(text: str, max_len: int = 100) -> str:
    """Remove mrkdwn-significant characters and newlines to prevent injection."""
    return text.replace("\n", " ").replace("\r", " ").replace("`", "'").replace("*", "")[:max_len]


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
        raw: bool = False,
    ) -> MessageRef:
        """Post message. Sanitizes text by default. raw=True to skip."""
        if not raw:
            text = sanitize_for_mrkdwn(text, max_len=len(text))
        kwargs: dict[str, Any] = {"channel": self.channel_id, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
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
        kwargs: dict[str, Any] = {
            "channel": self.channel_id,
            "user": user_id,
            "text": text,
        }
        if blocks:
            kwargs["blocks"] = blocks
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
    ) -> None:
        """Update an existing message."""
        kwargs: dict[str, Any] = {"channel": self.channel_id, "ts": ts, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
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

    async def upload(
        self,
        content: str,
        filename: str,
        *,
        title: str = "",
        thread_ts: str | None = None,
    ) -> None:
        """Upload a file to the channel."""
        kwargs: dict[str, Any] = {
            "channel": self.channel_id,
            "content": content,
            "filename": filename,
            "title": title or filename,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await self._web.files_upload_v2(**kwargs)

    async def set_topic(self, topic: str) -> None:
        """Set the channel topic."""
        await self._web.conversations_setTopic(channel=self.channel_id, topic=topic)

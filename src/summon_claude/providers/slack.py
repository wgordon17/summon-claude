"""SlackChatProvider — wraps AsyncWebClient behind the ChatProvider protocol."""

# pyright: reportArgumentType=false, reportReturnType=false
# slack_sdk doesn't ship type stubs

from __future__ import annotations

import logging
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.providers.base import ChannelRef, MessageRef

logger = logging.getLogger(__name__)


class SlackChatProvider:
    """ChatProvider implementation backed by the Slack Web API."""

    def __init__(self, client: AsyncWebClient) -> None:
        self._client = client

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        reply_broadcast: bool = False,
    ) -> MessageRef:
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        if reply_broadcast:
            kwargs["reply_broadcast"] = True
        resp = await self._client.chat_postMessage(**kwargs)
        return MessageRef(channel_id=resp["channel"], ts=resp["ts"])

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        await self._client.chat_update(**kwargs)

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        try:
            await self._client.reactions_add(channel=channel, timestamp=ts, name=emoji.strip(":"))
        except Exception as e:
            logger.debug("Failed to add reaction :%s: — %s", emoji, e)

    async def upload_file(
        self,
        channel: str,
        content: str,
        filename: str,
        *,
        title: str | None = None,
        thread_ts: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "content": content,
            "filename": filename,
            "title": title or filename,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await self._client.files_upload_v2(**kwargs)

    async def create_channel(self, name: str, *, is_private: bool = False) -> ChannelRef:
        resp = await self._client.conversations_create(name=name, is_private=is_private)
        channel = resp.get("channel") or {}
        return ChannelRef(channel_id=channel["id"], name=channel.get("name", name))

    async def invite_user(self, channel: str, user_id: str) -> None:
        await self._client.conversations_invite(channel=channel, users=[user_id])

    async def archive_channel(self, channel_id: str) -> None:
        try:
            await self._client.conversations_archive(channel=channel_id)
        except Exception as e:
            logger.debug("Failed to archive channel %s: %s", channel_id, e)

    async def set_topic(self, channel: str, topic: str) -> None:
        await self._client.conversations_setTopic(channel=channel, topic=topic)

    async def post_ephemeral(
        self,
        channel: str,
        user: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"channel": channel, "user": user, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        try:
            await self._client.chat_postEphemeral(**kwargs)
        except Exception as e:
            logger.warning("Failed to post ephemeral to %s: %s", user, e)

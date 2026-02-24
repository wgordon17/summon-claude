"""ChatProvider protocol — messaging surface abstraction for Slack, Discord, CLI, etc."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


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


@runtime_checkable
class ChatProvider(Protocol):
    """Async messaging surface used by session, streamer, and permissions."""

    async def post_message(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ts: str | None = None,
        reply_broadcast: bool = False,
    ) -> MessageRef: ...

    async def update_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None: ...

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None: ...

    async def upload_file(
        self,
        channel: str,
        content: str,
        filename: str,
        *,
        title: str | None = None,
        thread_ts: str | None = None,
    ) -> None: ...

    async def create_channel(self, name: str, *, is_private: bool = False) -> ChannelRef: ...

    async def archive_channel(self, channel_id: str) -> None: ...

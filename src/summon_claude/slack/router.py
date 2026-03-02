"""ThreadRouter — thread management for Slack message routing (Layer 2)."""

from __future__ import annotations

from typing import Any

from summon_claude.slack.client import MessageRef, SlackClient

_MAX_SUBAGENT_THREADS = 100


class ThreadRouter:
    """Thread management for Slack message routing (Layer 2).

    Tracks active thread and subagent threads. _client is PRIVATE.
    Knows about threads, NOT about turns.
    """

    def __init__(self, client: SlackClient) -> None:
        self._client: SlackClient = client
        self.active_thread_ts: str | None = None
        self.active_thread_ref: MessageRef | None = None
        self.subagent_threads: dict[str, str] = {}  # tool_use_id → thread_ts

    @property
    def channel_id(self) -> str:
        return self._client.channel_id

    # --- Thread lifecycle ---

    def set_active_thread(self, ts: str, ref: MessageRef) -> None:
        """Record the active thread ts and ref."""
        self.active_thread_ts = ts
        self.active_thread_ref = ref

    def clear_active_thread(self) -> None:
        """Clear the active thread state."""
        self.active_thread_ts = None
        self.active_thread_ref = None

    # --- Thread-aware posting (delegates to _client) ---

    async def post_to_main(
        self,
        text: str,
        *,
        thread_ts: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
        raw: bool = False,
    ) -> MessageRef:
        """Post directly to the main channel, optionally into a thread."""
        return await self._client.post(text, thread_ts=thread_ts, blocks=blocks, raw=raw)

    async def post_to_active_thread(
        self, text: str, *, blocks: list[dict[str, Any]] | None = None, raw: bool = False
    ) -> MessageRef:
        """Post to the current active thread; falls back to main if no active thread."""
        if not self.active_thread_ts:
            return await self.post_to_main(text, blocks=blocks, raw=raw)
        return await self._client.post(
            text, blocks=blocks, thread_ts=self.active_thread_ts, raw=raw
        )

    async def post_to_subagent_thread(
        self,
        tool_use_id: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post to a subagent's dedicated thread."""
        thread_ts = self.subagent_threads.get(tool_use_id)
        if not thread_ts:
            return await self.post_to_active_thread(text, blocks=blocks)
        return await self._client.post(text, blocks=blocks, thread_ts=thread_ts)

    async def upload_to_active_thread(
        self,
        content: str,
        filename: str,
        *,
        title: str | None = None,
    ) -> None:
        """Upload a file to the current active thread."""
        await self._client.upload(
            content,
            filename,
            title=title or filename,
            thread_ts=self.active_thread_ts,
        )

    async def update_message(self, ts: str, text: str, **kw: Any) -> None:
        """Update a message. Used by ResponseStreamer for turn summary updates.

        Note: signature is (ts, text) — channel is implicit (bound to _client.channel_id).
        """
        await self._client.update(ts, text, **kw)

    async def react(self, ts: str, emoji: str) -> None:
        """Add a reaction. Used by ResponseStreamer for turn completion checkmark."""
        await self._client.react(ts, emoji)

    # --- Subagent management ---

    async def start_subagent_thread(self, tool_use_id: str, description: str) -> str:
        """Create a dedicated subagent thread, return thread_ts."""
        # Evict oldest entries if we've hit the cap to prevent unbounded growth
        if len(self.subagent_threads) >= _MAX_SUBAGENT_THREADS:
            # dict preserves insertion order (Python 3.7+); drop oldest half
            keys = list(self.subagent_threads)
            for key in keys[: len(keys) // 2]:
                del self.subagent_threads[key]

        ref = await self._client.post(
            f"\U0001f916 Subagent: {description}",
            raw=True,
        )
        self.subagent_threads[tool_use_id] = ref.ts
        return ref.ts

    # --- Permission posting ---

    async def post_permission_ephemeral(
        self,
        user_id: str,
        text: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        """Post an ephemeral permission/question prompt visible only to user_id."""
        await self._client.post_ephemeral(user_id, text, blocks=blocks)

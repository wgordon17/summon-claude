"""ThreadRouter — thread management for Slack message routing (Layer 2)."""

from __future__ import annotations

from typing import Any

from summon_claude.slack.client import MessageRef, SlackClient

_MAX_SUBAGENT_THREADS = 100


class ThreadRouter:
    """Thread management for Slack message routing (Layer 2).

    Tracks active thread and subagent threads. ``client`` is public —
    callers may use it directly for simple pass-through operations.
    Knows about threads, NOT about turns.
    """

    def __init__(self, client: SlackClient) -> None:
        self.client: SlackClient = client
        self.active_thread_ts: str | None = None
        self.active_thread_ref: MessageRef | None = None
        self.subagent_threads: dict[str, str] = {}  # tool_use_id → thread_ts

    # --- Thread lifecycle ---

    def set_active_thread(self, ts: str, ref: MessageRef) -> None:
        """Record the active thread ts and ref."""
        self.active_thread_ts = ts
        self.active_thread_ref = ref

    # --- Thread-aware posting (delegates to client) ---

    async def post_to_main(
        self,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> MessageRef:
        """Post directly to the main channel."""
        return await self.client.post(text, blocks=blocks)

    async def post_to_active_thread(
        self, text: str, *, blocks: list[dict[str, Any]] | None = None
    ) -> MessageRef:
        """Post to the current active thread; falls back to main if no active thread."""
        if not self.active_thread_ts:
            return await self.post_to_main(text, blocks=blocks)
        return await self.client.post(text, blocks=blocks, thread_ts=self.active_thread_ts)

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
        return await self.client.post(text, blocks=blocks, thread_ts=thread_ts)

    async def upload_to_active_thread(
        self,
        content: str,
        filename: str,
        *,
        title: str | None = None,
    ) -> None:
        """Upload a file to the current active thread."""
        await self.client.upload(
            content,
            filename,
            title=title or filename,
            thread_ts=self.active_thread_ts,
        )

    # --- Subagent management ---

    async def start_subagent_thread(self, tool_use_id: str, description: str) -> str:
        """Create a dedicated subagent thread, return thread_ts."""
        # Evict oldest entries if we've hit the cap to prevent unbounded growth
        if len(self.subagent_threads) >= _MAX_SUBAGENT_THREADS:
            # dict preserves insertion order (Python 3.7+); drop oldest half
            keys = list(self.subagent_threads)
            for key in keys[: len(keys) // 2]:
                del self.subagent_threads[key]

        ref = await self.client.post(
            f"\U0001f916 Subagent: {description}",
        )
        self.subagent_threads[tool_use_id] = ref.ts
        return ref.ts

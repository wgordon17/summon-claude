"""CanvasStore — local markdown state with background Slack sync."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from summon_claude.sessions.registry import SessionRegistry
    from summon_claude.slack.client import SlackClient

logger = logging.getLogger(__name__)

_SYNC_DEBOUNCE_S = 60.0
_DIRTY_DELAY_S = 2.0


class CanvasStore:
    """Manages local canvas markdown and syncs it to Slack periodically.

    Thread-safe for concurrent ``update_section`` calls via ``_write_lock``.
    Background sync loop writes to Slack at most every ``_SYNC_DEBOUNCE_S``
    seconds, with a ``_DIRTY_DELAY_S`` delay after the last write to batch
    rapid updates.
    """

    def __init__(
        self,
        *,
        session_id: str,
        canvas_id: str,
        client: SlackClient,
        registry: SessionRegistry,
        markdown: str = "",
    ) -> None:
        self._session_id = session_id
        self._canvas_id = canvas_id
        self._client = client
        self._registry = registry
        self._markdown = markdown
        self._dirty = False
        self._write_lock = asyncio.Lock()
        self._sync_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def canvas_id(self) -> str:
        return self._canvas_id

    @property
    def markdown(self) -> str:
        return self._markdown

    def read(self) -> str:
        """Return the current markdown content."""
        return self._markdown

    async def write(self, markdown: str) -> None:
        """Replace all markdown content and mark dirty."""
        async with self._write_lock:
            self._markdown = markdown
            self._dirty = True
        await self._persist()

    async def update_section(self, heading: str, content: str) -> None:
        """Replace the body of a markdown section identified by heading.

        The section runs from the heading line to the next heading of equal
        or higher level (or end of document).  The heading line itself is
        preserved; only the body is replaced.
        """
        async with self._write_lock:
            self._markdown = _replace_section(self._markdown, heading, content)
            self._dirty = True
        await self._persist()

    def start_sync(self) -> None:
        """Start the background sync loop."""
        if self._sync_task is None or self._sync_task.done():
            self._stop_event.clear()
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop_sync(self) -> None:
        """Stop the background sync loop and do a final flush."""
        self._stop_event.set()
        if self._sync_task is not None:
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sync_task
            self._sync_task = None
        # Final flush
        if self._dirty:
            await self._flush()

    @classmethod
    async def restore(
        cls,
        *,
        session_id: str,
        client: SlackClient,
        registry: SessionRegistry,
    ) -> CanvasStore | None:
        """Restore a CanvasStore from SQLite for session resume.

        Returns ``None`` if no canvas data is stored for this session.
        """
        canvas_id, canvas_markdown = await registry.get_canvas(session_id)
        if not canvas_id:
            return None
        return cls(
            session_id=session_id,
            canvas_id=canvas_id,
            client=client,
            registry=registry,
            markdown=canvas_markdown or "",
        )

    async def _persist(self) -> None:
        """Write current markdown to SQLite."""
        try:
            await self._registry.update_canvas(self._session_id, self._canvas_id, self._markdown)
        except Exception as e:
            logger.warning("Failed to persist canvas to SQLite: %s", e)

    async def _flush(self) -> None:
        """Sync markdown to Slack and clear dirty flag."""
        async with self._write_lock:
            md = self._markdown
            self._dirty = False
        ok = await self._client.canvas_sync(self._canvas_id, md)
        if not ok:
            self._dirty = True  # re-mark for retry
            logger.debug("Canvas sync to Slack failed for session %s", self._session_id)
        else:
            logger.debug("Canvas synced to Slack for session %s", self._session_id)

    async def _sync_loop(self) -> None:
        """Background loop that periodically flushes dirty state to Slack."""
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=_SYNC_DEBOUNCE_S)
                    break  # stop_event was set
                except TimeoutError:
                    pass

                if self._dirty:
                    # Small delay to batch rapid writes
                    await asyncio.sleep(_DIRTY_DELAY_S)
                    if self._stop_event.is_set():
                        break
                    await self._flush()
        except asyncio.CancelledError:
            pass


def _replace_section(markdown: str, heading: str, new_body: str) -> str:
    """Replace the body under *heading* while preserving the heading line.

    Finds the heading (any ``#`` level), then replaces everything between it
    and the next heading of equal or higher level (or EOF) with *new_body*.
    """
    lines = markdown.split("\n")
    heading_stripped = heading.strip().lstrip("#").strip()

    # Find the heading line
    start_idx: int | None = None
    heading_level: int = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m and m.group(2).strip() == heading_stripped:
            start_idx = i
            heading_level = len(m.group(1))
            break

    if start_idx is None:
        # Heading not found — append new section at end
        suffix = f"\n\n## {heading}\n{new_body}"
        return markdown.rstrip() + suffix

    # Find the end of the section (next heading of same or higher level)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[i])
        if m and len(m.group(1)) <= heading_level:
            end_idx = i
            break

    # Rebuild: heading line + new body + rest
    result_lines = lines[: start_idx + 1]
    body = new_body.strip()
    if body:
        result_lines.append("")
        result_lines.append(body)
    result_lines.append("")
    result_lines.extend(lines[end_idx:])
    return "\n".join(result_lines)

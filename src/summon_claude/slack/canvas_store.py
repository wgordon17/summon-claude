"""CanvasStore — local markdown state with background Slack sync."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import TYPE_CHECKING

from summon_claude.security import validate_agent_output

if TYPE_CHECKING:
    from summon_claude.sessions.registry import SessionRegistry
    from summon_claude.slack.client import SlackClient

logger = logging.getLogger(__name__)

_SYNC_DEBOUNCE_S = 60.0
_DIRTY_DELAY_S = 2.0
_BACKOFF_THRESHOLD = 3
_BACKOFF_INTERVAL_S = 300.0


class CanvasStore:
    """Manages local canvas markdown and syncs it to Slack periodically.

    Thread-safe for concurrent ``update_section`` calls via ``_write_lock``.
    Background sync loop writes to Slack at most every ``_SYNC_DEBOUNCE_S``
    seconds, with a ``_DIRTY_DELAY_S`` delay after the last write to batch
    rapid updates.
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        session_id: str,
        canvas_id: str,
        client: SlackClient,
        registry: SessionRegistry,
        markdown: str = "",
        channel_id: str,
    ) -> None:
        self._session_id = session_id
        self._canvas_id = canvas_id
        self._client = client
        self._registry = registry
        self._markdown = markdown
        self._channel_id = channel_id
        self._dirty = False
        self._consecutive_failures = 0
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

        *heading* is matched by text only (e.g. ``"Status"``, not
        ``"## Status"``).  Any leading ``#`` characters are stripped.
        If the heading is not found, a new h2 section is appended.

        Raises ``ValueError`` if *heading* is empty after stripping.
        """
        normalized = heading.strip().lstrip("#").strip()
        if not normalized:
            raise ValueError(f"heading must contain non-whitespace text, got {heading!r}")
        async with self._write_lock:
            self._markdown = _replace_section(self._markdown, normalized, content)
            self._dirty = True
        await self._persist()

    async def update_table_field(self, field_name: str, value: str) -> None:
        """Update a field value in a two-column markdown table row.

        Finds ``| {field_name} | ... |`` and replaces the value cell.
        Only works for rows with exactly two data columns (field + value).
        """
        pattern = re.compile(
            rf"^\| *{re.escape(field_name)} *\|[^|]*\|",
            re.MULTILINE,
        )
        replacement = f"| {field_name} | {value} |"
        async with self._write_lock:
            new_md = pattern.sub(replacement, self._markdown, count=1)
            if new_md != self._markdown:
                self._markdown = new_md
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
        channel_id: str,
    ) -> CanvasStore | None:
        """Restore a CanvasStore from the channels table for session resume.

        Returns ``None`` if no canvas data is found for the channel.
        """
        channel = await registry.get_channel(channel_id)
        if not channel or not channel.get("canvas_id"):
            return None
        return cls(
            session_id=session_id,
            canvas_id=channel["canvas_id"],
            client=client,
            registry=registry,
            markdown=channel.get("canvas_markdown") or "",
            channel_id=channel_id,
        )

    async def _persist(self) -> None:
        """Write current markdown to channels table."""
        try:
            await self._registry.update_channel_canvas(
                self._channel_id, self._canvas_id, self._markdown
            )
        except Exception as e:
            logger.warning("Failed to persist canvas to SQLite: %s", e)

    async def _flush(self) -> None:
        """Sync markdown to Slack and clear dirty flag."""
        async with self._write_lock:
            md = self._markdown
            self._dirty = False
        md, warnings = validate_agent_output(md)
        if warnings:
            logger.warning(
                "Canvas output validation modified content for session %s: %s",
                self._session_id,
                "; ".join(warnings),
            )
        ok = await self._client.canvas_sync(self._canvas_id, md)
        if not ok:
            self._consecutive_failures += 1
            # Safe without _write_lock: this only escalates False→True (never
            # clears the flag).  A concurrent write() would have already set
            # _dirty=True under the lock, so this re-mark is idempotent.
            self._dirty = True
            if self._consecutive_failures >= _BACKOFF_THRESHOLD:
                logger.error(
                    "Canvas sync failed %d times for session %s, backing off to %ds",
                    self._consecutive_failures,
                    self._session_id,
                    _BACKOFF_INTERVAL_S,
                )
            else:
                logger.debug("Canvas sync to Slack failed for session %s", self._session_id)
        else:
            self._consecutive_failures = 0
            logger.debug("Canvas synced to Slack for session %s", self._session_id)

    async def _sync_loop(self) -> None:
        """Background loop that periodically flushes dirty state to Slack.

        Uses ``_SYNC_DEBOUNCE_S`` as the normal interval.  After
        ``_BACKOFF_THRESHOLD`` consecutive failures, switches to
        ``_BACKOFF_INTERVAL_S`` until a sync succeeds.
        """
        try:
            while not self._stop_event.is_set():
                interval = (
                    _BACKOFF_INTERVAL_S
                    if self._consecutive_failures >= _BACKOFF_THRESHOLD
                    else _SYNC_DEBOUNCE_S
                )
                try:
                    async with asyncio.timeout(interval):
                        await self._stop_event.wait()
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

    *heading* is matched by text only — any leading ``#`` characters are
    stripped before matching.  If the heading is not found, a new ``##``
    (h2) section is appended at the end of the document.

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
        # Heading not found — append new section at end.
        # heading_stripped has any leading '#' removed, so we always create h2.
        suffix = f"\n\n## {heading_stripped}\n{new_body}"
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

"""Tests for slack_browser module — _slugify and SlackBrowserMonitor filtering.

Covers C14 (Phase 3) test requirements.
No Playwright required — tests exercise only the filtering logic (_on_frame)
by creating SlackBrowserMonitor instances directly and calling _on_frame with
synthetic payloads.

All _on_frame tests are async so asyncio.get_running_loop() is available for
the monitor's call_soon_threadsafe path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from summon_claude.slack_browser import SlackBrowserMonitor, SlackMessage, _slugify

# ---------------------------------------------------------------------------
# C14: _slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_slugify_basic(self):
        assert _slugify("https://myteam.slack.com") == "myteam_slack_com"

    def test_slugify_strips_scheme(self):
        result = _slugify("https://myteam.slack.com")
        assert not result.startswith("https")
        assert not result.startswith("http")

    def test_slugify_strips_trailing_slash(self):
        assert _slugify("https://myteam.slack.com/") == "myteam_slack_com"

    def test_slugify_hyphenated_subdomain(self):
        assert _slugify("https://acme-corp.slack.com/") == "acme-corp_slack_com"

    def test_slugify_no_double_underscores(self):
        result = _slugify("https://myteam.slack.com")
        assert "__" not in result

    def test_slugify_no_leading_trailing_underscores(self):
        result = _slugify("https://myteam.slack.com/")
        assert not result.startswith("_")
        assert not result.endswith("_")


# ---------------------------------------------------------------------------
# Helpers for SlackBrowserMonitor tests
# ---------------------------------------------------------------------------


def make_monitor(
    monitored_channels: list[str] | None = None,
    user_id: str = "U999",
) -> SlackBrowserMonitor:
    """Create a SlackBrowserMonitor without a real browser.

    The monitor's _loop is set by callers inside async tests via
    ``monitor._loop = asyncio.get_running_loop()``.
    """
    monitor = SlackBrowserMonitor(
        workspace_id="test-ws",
        workspace_url="https://test.slack.com",
        state_file=Path("/tmp/test_state.json"),
        monitored_channel_ids=monitored_channels or [],
        user_id=user_id,
    )
    return monitor


def make_frame(  # noqa: PLR0913
    type_: str = "message",
    subtype: str = "",
    channel: str = "C001",
    user: str = "U123",
    text: str = "Hello",
    ts: str = "1234567890.000001",
) -> str:
    payload = {
        "type": type_,
        "channel": channel,
        "user": user,
        "text": text,
        "ts": ts,
    }
    if subtype:
        payload["subtype"] = subtype
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# C14: SlackBrowserMonitor._on_frame filtering
# All tests are async so the running loop is available for call_soon_threadsafe.
# ---------------------------------------------------------------------------


class TestMonitorOnFrame:
    @pytest.mark.asyncio
    async def test_monitor_queues_message_on_valid_frame(self):
        """A valid user message in a monitored channel is enqueued."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="C001")
        monitor._on_frame(frame)

        # Allow call_soon_threadsafe to execute
        await asyncio.sleep(0)

        msg = monitor._queue.get_nowait()
        assert isinstance(msg, SlackMessage)
        assert msg.channel == "C001"
        assert msg.text == "Hello"

    @pytest.mark.asyncio
    async def test_monitor_skips_bot_messages(self):
        """Frames with subtype=bot_message are discarded."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="C001", subtype="bot_message")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        with pytest.raises(asyncio.QueueEmpty):
            monitor._queue.get_nowait()

    @pytest.mark.asyncio
    async def test_monitor_skips_message_changed(self):
        """message_changed subtype is discarded."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="C001", subtype="message_changed")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        with pytest.raises(asyncio.QueueEmpty):
            monitor._queue.get_nowait()

    @pytest.mark.asyncio
    async def test_monitor_skips_non_message_types(self):
        """Frames with type != 'message' (e.g. presence_change) are discarded."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(type_="presence_change", channel="C001")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        with pytest.raises(asyncio.QueueEmpty):
            monitor._queue.get_nowait()

    @pytest.mark.asyncio
    async def test_monitor_filters_unmonitored_channels(self):
        """Non-DM, non-mention, non-monitored channel is dropped."""
        monitor = make_monitor(monitored_channels=["C001"], user_id="U999")
        monitor._loop = asyncio.get_running_loop()

        # C999 is not monitored, no mention
        frame = make_frame(channel="C999", text="no mention here")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        with pytest.raises(asyncio.QueueEmpty):
            monitor._queue.get_nowait()

    @pytest.mark.asyncio
    async def test_monitor_captures_dms(self):
        """Channel starting with 'D' (DM) is always captured."""
        monitor = make_monitor(monitored_channels=[], user_id="U999")
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="D001", text="direct message")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        msg = monitor._queue.get_nowait()
        assert msg.channel == "D001"
        assert msg.is_dm is True

    @pytest.mark.asyncio
    async def test_monitor_captures_mentions(self):
        """Message containing <@USER_ID> is captured even in unmonitored channel."""
        monitor = make_monitor(monitored_channels=[], user_id="U999")
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="C999", text="hey <@U999> check this out")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        msg = monitor._queue.get_nowait()
        assert msg.is_mention is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "broadcast,text_pattern",
        [
            ("here", "<!here>"),
            ("channel", "<!channel>"),
            ("everyone", "<!everyone>"),
        ],
    )
    async def test_monitor_captures_broadcast_mentions(self, broadcast, text_pattern):
        """@here, @channel, @everyone in non-monitored channels are captured."""
        monitor = make_monitor(monitored_channels=[], user_id="U999")
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="C999", text=f"hey {text_pattern} check this out")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        msg = monitor._queue.get_nowait()
        assert msg.is_mention is True
        assert msg.channel == "C999"

    @pytest.mark.asyncio
    async def test_monitor_skips_non_json(self):
        """Non-JSON payloads are silently discarded."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        monitor._on_frame("not json at all ~~~")

        await asyncio.sleep(0)

        with pytest.raises(asyncio.QueueEmpty):
            monitor._queue.get_nowait()

    @pytest.mark.asyncio
    async def test_monitor_handles_bytes_payload(self):
        """Bytes payloads are decoded before JSON parsing."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="C001").encode("utf-8")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        msg = monitor._queue.get_nowait()
        assert msg.channel == "C001"


# ---------------------------------------------------------------------------
# C14: drain
# ---------------------------------------------------------------------------


class TestMonitorDrain:
    @pytest.mark.asyncio
    async def test_monitor_drain_empties_queue(self):
        """drain() returns all queued messages; second drain returns empty."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        for i in range(3):
            frame = make_frame(channel="C001", ts=f"123456789{i}.000001")
            monitor._on_frame(frame)

        # Give call_soon_threadsafe a chance to execute
        await asyncio.sleep(0)

        first = await monitor.drain()
        assert len(first) == 3

        second = await monitor.drain()
        assert second == []

    @pytest.mark.asyncio
    async def test_monitor_drain_with_limit(self):
        """drain(limit=N) stops after N messages and leaves the rest."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        for i in range(5):
            frame = make_frame(channel="C001", ts=f"123456789{i}.000001")
            monitor._on_frame(frame)

        await asyncio.sleep(0)

        first = await monitor.drain(limit=3)
        assert len(first) == 3
        assert monitor._queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_monitor_queue_full_drops_without_crash(self):
        """Overflow beyond _QUEUE_MAX does not raise — drops with a warning."""
        from summon_claude.slack_browser import _QUEUE_MAX

        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        # Fill queue to capacity
        for i in range(_QUEUE_MAX):
            msg = SlackMessage(
                channel="C001",
                user="U123",
                text=f"msg {i}",
                ts=str(i),
                workspace="test-ws",
            )
            monitor._queue.put_nowait(msg)

        # One more frame — should not raise even though queue is full
        frame = make_frame(channel="C001", ts="overflow.000001")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        # Queue is still at max capacity (overflow was dropped)
        assert monitor._queue.qsize() == _QUEUE_MAX

    @pytest.mark.asyncio
    async def test_monitor_skips_message_deleted(self):
        """message_deleted subtype is discarded."""
        monitor = make_monitor(monitored_channels=["C001"])
        monitor._loop = asyncio.get_running_loop()

        frame = make_frame(channel="C001", subtype="message_deleted")
        monitor._on_frame(frame)

        await asyncio.sleep(0)

        with pytest.raises(asyncio.QueueEmpty):
            monitor._queue.get_nowait()

    @pytest.mark.asyncio
    async def test_monitor_on_frame_without_loop(self):
        """Frames arriving before start() (loop=None) are silently dropped."""
        monitor = make_monitor(monitored_channels=["C001"])
        # Do NOT set monitor._loop — it defaults to None

        frame = make_frame(channel="C001")
        monitor._on_frame(frame)

        assert monitor._queue.qsize() == 0


# ---------------------------------------------------------------------------
# C14: stop() symlink guard
# ---------------------------------------------------------------------------


class TestMonitorStop:
    @pytest.mark.asyncio
    async def test_stop_refuses_symlink_state_file(self, tmp_path):
        """stop() refuses to save auth state if state_file is a symlink (SEC-R-002)."""
        real_file = tmp_path / "real_state.json"
        real_file.write_text("{}")
        symlink = tmp_path / "symlinked_state.json"
        symlink.symlink_to(real_file)

        monitor = make_monitor()
        monitor._state_file = symlink
        monitor._context = AsyncMock()
        monitor._browser = AsyncMock()
        monitor._playwright = AsyncMock()

        await monitor.stop()

        # storage_state should NOT have been called (symlink refused)
        monitor._context.storage_state.assert_not_called()
        # But cleanup should still run
        monitor._context.close.assert_called_once()
        monitor._browser.close.assert_called_once()
        monitor._playwright.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_saves_state_for_normal_file(self, tmp_path):
        """stop() saves auth state for a normal (non-symlink) file."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")

        monitor = make_monitor()
        monitor._state_file = state_file
        monitor._context = AsyncMock()
        monitor._browser = AsyncMock()
        monitor._playwright = AsyncMock()

        await monitor.stop()

        monitor._context.storage_state.assert_called_once_with(path=str(state_file))


# ---------------------------------------------------------------------------
# interactive_slack_auth symlink guard
# ---------------------------------------------------------------------------


class TestInteractiveSlackAuth:
    @pytest.mark.asyncio
    async def test_interactive_slack_auth_rejects_symlinked_dir(self, tmp_path):
        """interactive_slack_auth raises RuntimeError if browser_auth/ is a symlink."""
        from summon_claude.slack_browser import interactive_slack_auth

        real_dir = tmp_path / "real_auth"
        real_dir.mkdir()
        symlinked_dir = tmp_path / "browser_auth"
        symlinked_dir.symlink_to(real_dir)

        from unittest.mock import patch as _patch

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=symlinked_dir),
            pytest.raises(RuntimeError, match="symlink"),
        ):
            await interactive_slack_auth("https://test.slack.com")

    @pytest.mark.asyncio
    async def test_non_timeout_exception_reraises_original(self, tmp_path):
        """Non-timeout exceptions from wait_for_url must propagate unchanged."""
        pytest.importorskip("playwright")
        from unittest.mock import AsyncMock, MagicMock
        from unittest.mock import patch as _patch

        from summon_claude.slack_browser import interactive_slack_auth

        auth_dir = tmp_path / "browser_auth"
        auth_dir.mkdir()

        class NetworkError(Exception):
            pass

        # Build Playwright mock chain
        mock_page = AsyncMock()
        mock_page.url = "https://test.slack.com/signin"
        mock_page.wait_for_url = AsyncMock(side_effect=NetworkError("connection reset"))
        mock_page.bring_to_front = AsyncMock()
        mock_page.locator = MagicMock()
        mock_page.locator.return_value.first = AsyncMock()
        mock_page.locator.return_value.first.click = AsyncMock(side_effect=Exception("no input"))

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        mock_pw_instance = AsyncMock()
        mock_pw_instance.chromium = AsyncMock()
        mock_pw_instance.chromium.launch = AsyncMock(return_value=mock_browser)

        mock_pw_cm = AsyncMock()
        mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_pw_instance)
        mock_pw_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=auth_dir),
            _patch("playwright.async_api.async_playwright", return_value=mock_pw_cm),
            pytest.raises(NetworkError, match="connection reset"),
        ):
            await interactive_slack_auth("https://test.slack.com")

        mock_browser.close.assert_awaited_once()

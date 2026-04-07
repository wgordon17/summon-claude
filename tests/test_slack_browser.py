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
from unittest.mock import AsyncMock, MagicMock, PropertyMock
from unittest.mock import patch as _patch

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

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=symlinked_dir),
            pytest.raises(RuntimeError, match="symlink"),
        ):
            await interactive_slack_auth("https://test.slack.com")

    @pytest.mark.asyncio
    async def test_non_timeout_exception_reraises_original(self, tmp_path):
        """Non-timeout exceptions from wait_for_url are caught by _monitor_page.

        In the refactored code, NetworkError from page.wait_for_url is caught by
        _monitor_page's except clause and logged — auth_done is never set — so
        asyncio.wait_for raises TimeoutError (not NetworkError).
        """

        from summon_claude.slack_browser import interactive_slack_auth

        auth_dir = tmp_path / "browser_auth"
        auth_dir.mkdir()

        class NetworkError(Exception):
            pass

        # Build Playwright mock chain
        mock_page = AsyncMock()
        url_mock = PropertyMock(return_value="https://test.slack.com/signin")
        type(mock_page).url = url_mock
        mock_page.wait_for_url = AsyncMock(side_effect=NetworkError("connection reset"))
        mock_page.bring_to_front = AsyncMock()
        mock_page.locator = MagicMock()
        mock_page.locator.return_value.first = AsyncMock()
        mock_page.locator.return_value.first.click = AsyncMock(side_effect=Exception("no input"))

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.on = MagicMock()
        mock_context.remove_listener = MagicMock()

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
            _patch("summon_claude.slack_browser._AUTH_TIMEOUT_S", 0.1),
            pytest.raises(TimeoutError),
        ):
            await interactive_slack_auth("https://test.slack.com")


# ---------------------------------------------------------------------------
# Enterprise Grid auth (C4)
# ---------------------------------------------------------------------------


def _make_interactive_auth_mocks(tmp_path, page_urls):
    """Build Playwright mock chain for interactive_slack_auth tests."""
    auth_dir = tmp_path / "browser_auth"
    auth_dir.mkdir(exist_ok=True)

    mock_page = AsyncMock()
    url_mock = PropertyMock(side_effect=page_urls)
    type(mock_page).url = url_mock
    mock_page.bring_to_front = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.wait_for_url = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()

    locator_mock = MagicMock()
    locator_mock.first = MagicMock()
    locator_mock.first.click = AsyncMock()
    mock_page.locator = MagicMock(return_value=locator_mock)

    async def _fake_storage_state(path: str = "", **kwargs) -> None:
        if path:
            Path(path).write_text("{}")

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.storage_state = AsyncMock(side_effect=_fake_storage_state)
    mock_context.on = MagicMock()
    mock_context.remove_listener = MagicMock()  # sync in Playwright

    mock_browser = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_pw_instance = AsyncMock()
    mock_pw_instance.chromium = AsyncMock()
    mock_pw_instance.chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw_cm = AsyncMock()
    mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_pw_instance)
    mock_pw_cm.__aexit__ = AsyncMock(return_value=False)

    return auth_dir, mock_page, mock_context, mock_browser, mock_pw_cm, locator_mock


class TestEnterpriseGridAuth:
    # -----------------------------------------------------------------------
    # SlackAuthResult dataclass
    # -----------------------------------------------------------------------

    def test_auth_result_resolved_url_defaults_none(self):
        from summon_claude.slack_browser import SlackAuthResult

        result = SlackAuthResult(state_file=Path("/tmp/test.json"))
        assert result.resolved_url is None

    def test_auth_result_resolved_url_set(self):
        from summon_claude.slack_browser import SlackAuthResult

        result = SlackAuthResult(
            state_file=Path("/tmp/test.json"),
            resolved_url="https://ext.slack.com",
        )
        assert result.resolved_url == "https://ext.slack.com"

    # -----------------------------------------------------------------------
    # _resolve_workspace_url_from_page
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_resolve_workspace_url_from_page_success(self):
        from summon_claude.slack_browser import _resolve_workspace_url_from_page

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(
            return_value={"T123": {"url": "https://ext-redhat.slack.com/"}}
        )

        result = await _resolve_workspace_url_from_page(mock_page, "T123")
        assert result == "https://ext-redhat.slack.com"

    @pytest.mark.asyncio
    async def test_resolve_workspace_url_from_page_team_not_found(self):
        from summon_claude.slack_browser import _resolve_workspace_url_from_page

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"T123": {"url": "https://ext.slack.com/"}})

        with _patch("summon_claude.slack_browser._USER_ID_POLL_TIMEOUT", 0.1):
            result = await _resolve_workspace_url_from_page(mock_page, "T999")

        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_workspace_url_from_page_page_closed(self):
        from summon_claude.slack_browser import _resolve_workspace_url_from_page

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(
            side_effect=Exception("Target page, context or browser has been closed"),
        )

        result = await _resolve_workspace_url_from_page(mock_page, "T123")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_workspace_url_from_page_rejects_bad_domain(self):
        from summon_claude.slack_browser import _resolve_workspace_url_from_page

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"T123": {"url": "https://evil.example.com/"}})

        result = await _resolve_workspace_url_from_page(mock_page, "T123")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_workspace_url_from_page_rejects_http_scheme(self):
        from summon_claude.slack_browser import _resolve_workspace_url_from_page

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value={"T123": {"url": "http://myteam.slack.com/"}})

        result = await _resolve_workspace_url_from_page(mock_page, "T123")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_workspace_url_from_page_rejects_userinfo(self):
        from summon_claude.slack_browser import _resolve_workspace_url_from_page

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(
            return_value={"T123": {"url": "https://x@inject.slack.com/"}}
        )

        result = await _resolve_workspace_url_from_page(mock_page, "T123")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_workspace_url_from_page_retries_on_transient_error(self):
        from summon_claude.slack_browser import _resolve_workspace_url_from_page

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(
            side_effect=[
                Exception("JS evaluation timeout"),
                {"T123": {"url": "https://ext.slack.com/"}},
            ]
        )

        result = await _resolve_workspace_url_from_page(mock_page, "T123")
        assert result == "https://ext.slack.com"
        assert mock_page.evaluate.call_count == 2

    # -----------------------------------------------------------------------
    # interactive_slack_auth — Enterprise Grid flows
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_enterprise_redirect_with_resolved_url(self, tmp_path):
        """Enterprise Grid: redirected_url triggers URL resolution; result carries resolved_url."""

        from summon_claude.slack_browser import interactive_slack_auth

        # 4 url reads: after goto, domain validation, log, regex
        page_urls = [
            "https://redhat.enterprise.slack.com",  # read 1: redirect detection
            "https://app.slack.com/client/T123/C456",  # read 2: domain validation
            "https://app.slack.com/client/T123/C456",  # read 3: log
            "https://app.slack.com/client/T123/C456",  # read 4: regex
        ]
        auth_dir, mock_page, mock_context, mock_browser, mock_pw_cm, locator_mock = (
            _make_interactive_auth_mocks(tmp_path, page_urls)
        )

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=auth_dir),
            _patch("playwright.async_api.async_playwright", return_value=mock_pw_cm),
            _patch(
                "summon_claude.slack_browser._resolve_workspace_url_from_page",
                new=AsyncMock(return_value="https://ext-redhat.slack.com"),
            ),
            _patch(
                "summon_claude.slack_browser._extract_user_id",
                new=AsyncMock(return_value="U123"),
            ),
            _patch(
                "summon_claude.slack_browser._extract_channels",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await interactive_slack_auth("https://redhat.enterprise.slack.com")

        assert result.resolved_url == "https://ext-redhat.slack.com"
        assert result.team_id == "T123"

    @pytest.mark.asyncio
    async def test_workspace_picker_skips_email_autofocus(self, tmp_path):
        """Enterprise Grid workspace picker: email input click is skipped."""

        from summon_claude.slack_browser import interactive_slack_auth

        # 4 url reads: after goto, domain validation, log, regex
        page_urls = [
            "https://redhat.enterprise.slack.com",  # read 1: redirect
            "https://app.slack.com/client/T123/C456",  # read 2: domain validation
            "https://app.slack.com/client/T123/C456",  # read 3: log
            "https://app.slack.com/client/T123/C456",  # read 4: regex
        ]
        auth_dir, mock_page, mock_context, mock_browser, mock_pw_cm, locator_mock = (
            _make_interactive_auth_mocks(tmp_path, page_urls)
        )

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=auth_dir),
            _patch("playwright.async_api.async_playwright", return_value=mock_pw_cm),
            _patch(
                "summon_claude.slack_browser._resolve_workspace_url_from_page",
                new=AsyncMock(return_value="https://ext-redhat.slack.com"),
            ),
            _patch(
                "summon_claude.slack_browser._extract_user_id",
                new=AsyncMock(return_value="U123"),
            ),
            _patch(
                "summon_claude.slack_browser._extract_channels",
                new=AsyncMock(return_value=[]),
            ),
        ):
            await interactive_slack_auth("https://redhat.enterprise.slack.com")

        # Email input click must NOT have been awaited
        locator_mock.first.click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_tab_auth_completion(self, tmp_path):
        """New tab opened by Slack SSO completes auth; _extract_user_id uses new page."""

        from summon_claude.slack_browser import interactive_slack_auth

        # Original page URL stays on enterprise login, new page navigates to /client/
        page_urls = ["https://redhat.enterprise.slack.com"]
        auth_dir, mock_page, mock_context, mock_browser, mock_pw_cm, _ = (
            _make_interactive_auth_mocks(tmp_path, page_urls)
        )

        mock_new_page = AsyncMock()
        new_page_url_mock = PropertyMock(return_value="https://app.slack.com/client/T789/C012")
        type(mock_new_page).url = new_page_url_mock
        mock_new_page.wait_for_url = AsyncMock()
        mock_new_page.wait_for_selector = AsyncMock()

        async def original_page_wait(*args, **kwargs):
            # Fire the new-page callback before blocking forever
            on_page_calls = [c for c in mock_context.on.call_args_list if c[0][0] == "page"]
            on_new_page_cb = on_page_calls[0][0][1]
            on_new_page_cb(mock_new_page)
            await asyncio.Event().wait()

        mock_page.wait_for_url = AsyncMock(side_effect=original_page_wait)

        captured_extract_user_id_page = []

        async def fake_extract_user_id(page, url):
            captured_extract_user_id_page.append(page)
            return "U789"

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=auth_dir),
            _patch("playwright.async_api.async_playwright", return_value=mock_pw_cm),
            _patch(
                "summon_claude.slack_browser._resolve_workspace_url_from_page",
                new=AsyncMock(return_value=None),
            ),
            _patch(
                "summon_claude.slack_browser._extract_user_id",
                new=AsyncMock(side_effect=fake_extract_user_id),
            ),
            _patch(
                "summon_claude.slack_browser._extract_channels",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await interactive_slack_auth("https://redhat.enterprise.slack.com")

        assert result.team_id == "T789"
        assert captured_extract_user_id_page[0] is mock_new_page

    @pytest.mark.asyncio
    async def test_non_navigating_page_handled_gracefully(self, tmp_path):
        """Non-navigating pages (e.g. slack:// deep links) don't break auth flow.

        A new tab at about:blank that never reaches /client/ is monitored but
        cancelled when the original page completes auth.
        """

        from summon_claude.slack_browser import interactive_slack_auth

        # Original page: redirect detection read, then 3 auth reads (domain, log, regex)
        page_urls = [
            "https://myteam.slack.com",  # read 1: redirect
            "https://app.slack.com/client/T555/C999",  # read 2: domain validation
            "https://app.slack.com/client/T555/C999",  # read 3: log
            "https://app.slack.com/client/T555/C999",  # read 4: regex
        ]
        auth_dir, mock_page, mock_context, mock_browser, mock_pw_cm, _ = (
            _make_interactive_auth_mocks(tmp_path, page_urls)
        )

        # A non-navigating page (slack:// deep link opens about:blank)
        mock_blank_page = AsyncMock()
        blank_url_mock = PropertyMock(return_value="about:blank")
        type(mock_blank_page).url = blank_url_mock

        # This page never reaches /client/ — blocks forever
        async def blank_blocks_forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        mock_blank_page.wait_for_url = AsyncMock(side_effect=blank_blocks_forever)

        # Original page completes normally, but fire the blank page first
        original_wait = mock_page.wait_for_url

        async def page_wait_with_blank(*args, **kwargs):
            # Fire the non-navigating page before original completes
            on_page_calls = [c for c in mock_context.on.call_args_list if c[0][0] == "page"]
            on_new_page_cb = on_page_calls[0][0][1]
            on_new_page_cb(mock_blank_page)
            # Original page completes immediately (reaches /client/)
            return await original_wait(*args, **kwargs)

        mock_page.wait_for_url = AsyncMock(side_effect=page_wait_with_blank)

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=auth_dir),
            _patch("playwright.async_api.async_playwright", return_value=mock_pw_cm),
            _patch(
                "summon_claude.slack_browser._resolve_workspace_url_from_page",
                new=AsyncMock(return_value=None),
            ),
            _patch(
                "summon_claude.slack_browser._extract_user_id",
                new=AsyncMock(return_value="U555"),
            ),
            _patch(
                "summon_claude.slack_browser._extract_channels",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await interactive_slack_auth("https://myteam.slack.com")

        # Auth completed via original page despite the non-navigating tab
        assert result.team_id == "T555"
        assert result.user_id == "U555"

    @pytest.mark.asyncio
    async def test_timeout_error_includes_all_page_urls(self, tmp_path):
        """TimeoutError message includes URLs from all open pages."""

        from summon_claude.slack_browser import interactive_slack_auth

        # 2 url reads: after goto (redirect detection), timeout path (page_urls)
        page_urls = [
            "https://redhat.enterprise.slack.com",
            "https://redhat.enterprise.slack.com",
        ]
        auth_dir, mock_page, mock_context, mock_browser, mock_pw_cm, _ = (
            _make_interactive_auth_mocks(tmp_path, page_urls)
        )

        mock_new_page = AsyncMock()
        new_page_url_mock = PropertyMock(return_value="https://sso.redhat.com/login")
        type(mock_new_page).url = new_page_url_mock

        async def block_forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        mock_new_page.wait_for_url = AsyncMock(side_effect=block_forever)

        async def original_page_wait(*args, **kwargs):
            on_page_calls = [c for c in mock_context.on.call_args_list if c[0][0] == "page"]
            on_new_page_cb = on_page_calls[0][0][1]
            on_new_page_cb(mock_new_page)
            await asyncio.Event().wait()

        mock_page.wait_for_url = AsyncMock(side_effect=original_page_wait)

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=auth_dir),
            _patch("playwright.async_api.async_playwright", return_value=mock_pw_cm),
            _patch("summon_claude.slack_browser._AUTH_TIMEOUT_S", 0.1),
            pytest.raises(TimeoutError) as exc_info,
        ):
            await interactive_slack_auth("https://redhat.enterprise.slack.com")

        # Both page URLs should appear in the error message
        msg = str(exc_info.value)
        assert "redhat.enterprise.slack.com" in msg
        assert "sso.redhat.com/login" in msg

    @pytest.mark.asyncio
    async def test_non_enterprise_regression(self, tmp_path):
        """Standard single-workspace login behaves unchanged (no resolved_url, email focused)."""

        from summon_claude.slack_browser import interactive_slack_auth

        # 4 url reads: after goto, domain validation, log, regex
        page_urls = [
            "https://myteam.slack.com",  # read 1: no redirect
            "https://app.slack.com/client/T555/C999",  # read 2: domain validation
            "https://app.slack.com/client/T555/C999",  # read 3: log
            "https://app.slack.com/client/T555/C999",  # read 4: regex
        ]
        auth_dir, mock_page, mock_context, mock_browser, mock_pw_cm, locator_mock = (
            _make_interactive_auth_mocks(tmp_path, page_urls)
        )

        with (
            _patch("summon_claude.slack_browser.get_browser_auth_dir", return_value=auth_dir),
            _patch("playwright.async_api.async_playwright", return_value=mock_pw_cm),
            _patch(
                "summon_claude.slack_browser._resolve_workspace_url_from_page",
                new=AsyncMock(return_value=None),
            ),
            _patch(
                "summon_claude.slack_browser._extract_user_id",
                new=AsyncMock(return_value="U555"),
            ),
            _patch(
                "summon_claude.slack_browser._extract_channels",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await interactive_slack_auth("https://myteam.slack.com")

        assert result.resolved_url is None
        assert result.team_id == "T555"
        # Email input focus WAS attempted (no .enterprise.slack.com in redirected URL)
        locator_mock.first.click.assert_awaited_once()

    # -----------------------------------------------------------------------
    # CLI: resolved_url propagation to _save_workspace_config
    # -----------------------------------------------------------------------

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    def test_resolved_url_propagation_to_config(self):
        """slack_auth() passes resolved_url as workspace_url to _save_workspace_config."""
        from summon_claude.slack_browser import SlackAuthResult

        mock_result = SlackAuthResult(
            state_file=Path("/tmp/t.json"),
            resolved_url="https://ext.slack.com",
            team_id="T1",
            user_id="U1",
            channels=[],
        )

        with (
            _patch("summon_claude.cli.slack_auth.asyncio") as mock_asyncio,
            _patch("summon_claude.cli.slack_auth._save_workspace_config") as mock_save,
            _patch(
                "summon_claude.cli.slack_auth._check_existing_slack_auth",
                return_value=None,
            ),
        ):
            mock_asyncio.run.return_value = mock_result

            from summon_claude.cli.slack_auth import slack_auth

            slack_auth("redhat")

        mock_save.assert_called_once()
        # Second positional arg (workspace_url) must be the resolved URL
        assert mock_save.call_args[0][1] == "https://ext.slack.com"

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    def test_no_resolved_url_uses_original(self):
        """slack_auth() falls back to original workspace_url when resolved_url is None."""
        from summon_claude.slack_browser import SlackAuthResult

        mock_result = SlackAuthResult(
            state_file=Path("/tmp/t.json"),
            resolved_url=None,
            team_id="T1",
            user_id="U1",
            channels=[],
        )

        with (
            _patch("summon_claude.cli.slack_auth.asyncio") as mock_asyncio,
            _patch("summon_claude.cli.slack_auth._save_workspace_config") as mock_save,
            _patch(
                "summon_claude.cli.slack_auth._check_existing_slack_auth",
                return_value=None,
            ),
        ):
            mock_asyncio.run.return_value = mock_result

            from summon_claude.cli.slack_auth import slack_auth

            slack_auth("myteam")

        mock_save.assert_called_once()
        # Falls back to normalized workspace_url
        assert mock_save.call_args[0][1] == "https://myteam.slack.com"


# ---------------------------------------------------------------------------
# Security hardening tests
# ---------------------------------------------------------------------------


class TestSecurityHardening:
    def test_normalize_workspace_rejects_userinfo(self):
        """_normalize_workspace rejects https:// URLs with embedded credentials."""
        from summon_claude.cli.slack_auth import _normalize_workspace

        with pytest.raises(SystemExit):
            _normalize_workspace("https://user@myteam.slack.com")

    def test_normalize_workspace_allows_clean_url(self):
        """_normalize_workspace passes through clean https:// URLs."""
        from summon_claude.cli.slack_auth import _normalize_workspace

        assert _normalize_workspace("https://myteam.slack.com") == "https://myteam.slack.com"

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    def test_save_workspace_config_rejects_invalid_url(self):
        """_save_workspace_config raises ValueError on non-Slack URLs."""
        from summon_claude.slack_browser import SlackAuthResult

        result = SlackAuthResult(state_file=Path("/tmp/t.json"), user_id="U1")

        with (
            _patch("summon_claude.cli.slack_auth._pick_channels", return_value=""),
            pytest.raises(ValueError, match="Refusing to save"),
        ):
            from summon_claude.cli.slack_auth import _save_workspace_config

            _save_workspace_config(result, "https://evil.example.com", "chrome")

    def test_save_workspace_config_accepts_valid_url(self, tmp_path):
        """_save_workspace_config accepts valid Slack URLs."""
        from summon_claude.slack_browser import SlackAuthResult

        result = SlackAuthResult(state_file=Path("/tmp/t.json"), user_id="U1")

        with (
            _patch("summon_claude.cli.slack_auth._pick_channels", return_value=""),
            _patch(
                "summon_claude.cli.slack_auth.get_workspace_config_path",
                return_value=tmp_path / "ws.json",
            ),
        ):
            from summon_claude.cli.slack_auth import _save_workspace_config

            _save_workspace_config(result, "https://myteam.slack.com", "chrome")

        saved = json.loads((tmp_path / "ws.json").read_text())
        assert saved["url"] == "https://myteam.slack.com"

    def test_resolve_client_url_exact_origin_match(self, tmp_path):
        """_resolve_client_url uses exact origin match, not substring."""
        from summon_claude.slack_browser import _resolve_client_url

        state_file = tmp_path / "state.json"
        # Origin that would pass a substring check but fails exact match
        state = {
            "origins": [
                {
                    "origin": "https://evil-app.slack.com.attacker.com",
                    "localStorage": [
                        {
                            "name": "localConfig_v2",
                            "value": json.dumps({"teams": {"T666": {"url": "https://evil.com/"}}}),
                        }
                    ],
                }
            ]
        }
        state_file.write_text(json.dumps(state))

        # Should fall back to workspace_url (evil origin rejected)
        result = _resolve_client_url("https://myteam.slack.com", state_file)
        assert result == "https://myteam.slack.com"

    def test_resolve_client_url_accepts_app_slack_origin(self, tmp_path):
        """_resolve_client_url accepts https://app.slack.com origin."""
        from summon_claude.slack_browser import _resolve_client_url

        state_file = tmp_path / "state.json"
        state = {
            "origins": [
                {
                    "origin": "https://app.slack.com",
                    "localStorage": [
                        {
                            "name": "localConfig_v2",
                            "value": json.dumps(
                                {"teams": {"T123": {"url": "https://myteam.slack.com/"}}}
                            ),
                        }
                    ],
                }
            ]
        }
        state_file.write_text(json.dumps(state))

        result = _resolve_client_url("https://myteam.slack.com", state_file)
        assert result == "https://app.slack.com/client/T123"

    def test_channel_error_scrubs_tokens(self):
        """_extract_channels scrubs xoxc- tokens from error log messages."""
        import logging

        from summon_claude.slack_browser import _extract_channels

        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(
            return_value={"error": "fetch failed with xoxc-secret-token-12345"}
        )

        with _patch("summon_claude.slack_browser.logger") as mock_logger:
            import asyncio

            asyncio.run(_extract_channels(mock_page, "https://test.slack.com"))

        # The info log call should have the token scrubbed
        info_calls = [c for c in mock_logger.info.call_args_list if "Channel API" in str(c)]
        assert len(info_calls) >= 1
        logged_msg = str(info_calls[0])
        assert "xoxc-" not in logged_msg
        assert "[TOKEN]" in logged_msg

"""Tests for EventProbe in summon_claude.slack.bolt."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig
from summon_claude.slack.bolt import DiagnosticResult, EventProbe


def _make_config(**overrides) -> SummonConfig:
    defaults = {"slack_app_token": "xapp-1-A0123ABCDE-12345-abc"}
    defaults.update(overrides)
    return SummonConfig.for_test(**defaults)


def _make_web_client(**overrides) -> AsyncMock:
    client = AsyncMock()
    client.conversations_create = AsyncMock(return_value={"channel": {"id": "C_PROBE"}})
    client.conversations_list = AsyncMock(
        return_value={"channels": [], "response_metadata": {"next_cursor": ""}}
    )
    client.chat_postMessage = AsyncMock(return_value={"ts": "1234567890.000001"})
    client.reactions_add = AsyncMock(return_value={"ok": True})
    client.reactions_remove = AsyncMock(return_value={"ok": True})
    client.api_test = AsyncMock(return_value={"ok": True})
    client.auth_test = AsyncMock(return_value={"ok": True, "user_id": "UBOT"})
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


def _make_probe(client=None, config=None) -> EventProbe:
    if client is None:
        client = _make_web_client()
    if config is None:
        config = _make_config()
    return EventProbe(web_client=client, config=config)


def _make_ws_message(data: dict):
    """Create a mock aiohttp WSMessage with TEXT type."""
    import aiohttp

    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.TEXT
    msg.data = json.dumps(data)
    return msg


def _make_non_text_ws_message():
    """Create a mock aiohttp WSMessage with BINARY type."""
    import aiohttp

    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.BINARY
    msg.data = b"bytes"
    return msg


# ---------------------------------------------------------------------------
# setup_anchor
# ---------------------------------------------------------------------------


class TestSetupAnchor:
    """All tests patch channel cache to isolate from filesystem."""

    _NO_CACHE = patch.object(EventProbe, "_load_channel_cache", return_value=None)
    _NO_SAVE = patch.object(EventProbe, "_save_channel_cache")

    async def test_setup_anchor_creates_channel_and_posts(self):
        client = _make_web_client()
        probe = _make_probe(client)

        with self._NO_CACHE, self._NO_SAVE:
            await probe.setup_anchor()

        client.conversations_create.assert_awaited_once()
        client.chat_postMessage.assert_awaited_once()
        assert probe._anchor_channel_id == "C_PROBE"
        assert probe._anchor_ts == "1234567890.000001"

    async def test_setup_anchor_uses_cached_channel(self):
        """Cached channel ID validated with conversations_info — skips create."""
        client = _make_web_client()
        client.conversations_info = AsyncMock(
            return_value={"channel": {"id": "C_CACHED", "is_archived": False}}
        )
        probe = _make_probe(client)

        with (
            patch.object(EventProbe, "_load_channel_cache", return_value="C_CACHED"),
            self._NO_SAVE,
        ):
            await probe.setup_anchor()

        assert probe._anchor_channel_id == "C_CACHED"
        client.conversations_create.assert_not_awaited()
        client.conversations_info.assert_awaited_once_with(channel="C_CACHED")

    async def test_setup_anchor_stale_cache_creates_new(self):
        """Stale cached channel ID falls through to create."""
        client = _make_web_client()
        client.conversations_info = AsyncMock(side_effect=Exception("channel_not_found"))
        probe = _make_probe(client)

        with (
            patch.object(EventProbe, "_load_channel_cache", return_value="C_STALE"),
            self._NO_SAVE,
            patch.object(EventProbe, "_clear_channel_cache"),
        ):
            await probe.setup_anchor()

        assert probe._anchor_channel_id == "C_PROBE"
        client.conversations_create.assert_awaited_once()

    async def test_setup_anchor_name_taken_creates_with_random_suffix(self):
        """When canonical name is taken, creates with random suffix."""
        client = _make_web_client()
        call_count = 0

        async def _create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("name_taken")
            return {"channel": {"id": "C_SUFFIXED"}}

        client.conversations_create = _create
        probe = _make_probe(client)

        with self._NO_CACHE, self._NO_SAVE:
            await probe.setup_anchor()

        assert probe._anchor_channel_id == "C_SUFFIXED"
        assert call_count == 2

    async def test_setup_anchor_is_idempotent(self):
        client = _make_web_client()
        probe = _make_probe(client)

        with self._NO_CACHE, self._NO_SAVE:
            await probe.setup_anchor()
            await probe.setup_anchor()  # second call must be a no-op

        assert client.conversations_create.await_count == 1

    async def test_setup_anchor_raises_on_unexpected_error(self):
        client = _make_web_client()
        client.conversations_create = AsyncMock(side_effect=Exception("access_denied"))
        probe = _make_probe(client)

        with self._NO_CACHE, self._NO_SAVE, pytest.raises(Exception, match="access_denied"):
            await probe.setup_anchor()

    async def test_setup_anchor_both_names_taken_raises(self):
        """When both canonical and suffixed names are taken, raises RuntimeError."""
        client = _make_web_client()
        client.conversations_create = AsyncMock(side_effect=Exception("name_taken"))
        probe = _make_probe(client)

        with (
            self._NO_CACHE,
            self._NO_SAVE,
            pytest.raises(RuntimeError, match="could not find or create"),
        ):
            await probe.setup_anchor()


# ---------------------------------------------------------------------------
# run_probe
# ---------------------------------------------------------------------------


class TestRunProbe:
    def _primed_probe(self, client=None) -> EventProbe:
        """Return a probe with anchor already set up."""
        if client is None:
            client = _make_web_client()
        probe = _make_probe(client)
        probe._anchor_channel_id = "C_PROBE"
        probe._anchor_ts = "111.222"
        return probe

    async def test_run_probe_healthy(self):
        probe = self._primed_probe()

        # Set the event after a short delay to simulate receiving the WS message
        async def _trigger():
            await asyncio.sleep(0.05)
            probe._event_received.set()

        trigger = asyncio.create_task(_trigger())
        result = await probe.run_probe(timeout=5.0)
        await trigger

        assert result.healthy is True
        assert result.reason == "healthy"

    async def test_run_probe_no_anchor_returns_unknown(self):
        probe = _make_probe()
        # anchor not set up
        result = await probe.run_probe()
        assert result.healthy is False
        assert result.reason == "unknown"

    async def test_run_probe_already_reacted_retries(self):
        client = _make_web_client()
        # First reactions_add raises already_reacted; retry remove+add succeeds
        call_count = 0

        async def _reactions_add(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("already_reacted")
            return {"ok": True}

        client.reactions_add = _reactions_add
        probe = self._primed_probe(client)

        async def _trigger():
            await asyncio.sleep(0.05)
            probe._event_received.set()

        trigger = asyncio.create_task(_trigger())
        result = await probe.run_probe(timeout=5.0)
        await trigger

        assert result.healthy is True
        assert call_count == 2

    async def test_run_probe_already_reacted_stuck(self):
        """Both reaction add attempts return already_reacted — returns healthy (skip)."""
        client = _make_web_client()

        async def _reactions_add(**kwargs):
            raise Exception("already_reacted")

        client.reactions_add = _reactions_add
        probe = self._primed_probe(client)

        result = await probe.run_probe(timeout=0.5)

        assert result.healthy is True
        assert result.reason == "healthy"
        assert "stuck" in result.details.lower()

    async def test_run_probe_cancelled_during_reconnect(self):
        probe = self._primed_probe()

        # Set _probe_cancelled concurrently while the probe waits for the event
        async def _cancel():
            await asyncio.sleep(0.02)
            probe._probe_cancelled = True

        cancel_task = asyncio.create_task(_cancel())
        result = await probe.run_probe(timeout=0.5)
        await cancel_task

        assert result.healthy is True
        assert result.reason == "cancelled"

    async def test_run_probe_timeout_runs_cascade(self):
        client = _make_web_client()
        probe = self._primed_probe(client)
        # Don't set the event — probe times out and runs cascade
        # api_test + auth_test both succeed → events_disabled
        result = await probe.run_probe(timeout=0.01)

        assert result.healthy is False
        assert result.reason == "events_disabled"

    async def test_run_probe_add_reaction_fails_returns_unknown(self):
        client = _make_web_client()
        client.reactions_add = AsyncMock(side_effect=Exception("channel_not_found"))
        probe = self._primed_probe(client)

        result = await probe.run_probe(timeout=5.0)

        assert result.healthy is False
        assert result.reason == "unknown"

    async def test_run_probe_cancel_interrupts_wait(self):
        """cancel_probe() should unblock the wait immediately."""
        probe = self._primed_probe()

        async def _cancel_quickly():
            await asyncio.sleep(0.02)
            probe.cancel_probe()

        cancel_task = asyncio.create_task(_cancel_quickly())
        import time

        start = time.monotonic()
        result = await probe.run_probe(timeout=5.0)
        elapsed = time.monotonic() - start
        await cancel_task

        assert result.healthy is True
        assert result.reason == "cancelled"
        # Should return much faster than the 5s timeout
        assert elapsed < 1.0


# ---------------------------------------------------------------------------
# _last_disconnect_reason clearing
# ---------------------------------------------------------------------------


class TestDisconnectReasonClearing:
    def _primed_probe(self, client=None) -> EventProbe:
        if client is None:
            client = _make_web_client()
        probe = _make_probe(client)
        probe._anchor_channel_id = "C_PROBE"
        probe._anchor_ts = "111.222"
        return probe

    async def test_run_probe_clears_disconnect_reason(self):
        """run_probe() should clear _last_disconnect_reason to prevent stale values."""
        probe = self._primed_probe()
        probe._last_disconnect_reason = "link_disabled"

        # Trigger event quickly so probe succeeds
        async def _trigger():
            await asyncio.sleep(0.05)
            probe._event_received.set()

        trigger = asyncio.create_task(_trigger())
        result = await probe.run_probe(timeout=5.0)
        await trigger

        assert result.healthy is True
        assert probe._last_disconnect_reason is None


# ---------------------------------------------------------------------------
# _run_diagnostic_cascade
# ---------------------------------------------------------------------------


class TestDiagnosticCascade:
    def _primed_probe(self, client=None) -> EventProbe:
        if client is None:
            client = _make_web_client()
        probe = _make_probe(client)
        probe._anchor_channel_id = "C_PROBE"
        probe._anchor_ts = "111.222"
        return probe

    async def test_cascade_slack_down(self):
        client = _make_web_client()
        client.api_test = AsyncMock(side_effect=Exception("network error"))
        probe = self._primed_probe(client)

        result = await probe._run_diagnostic_cascade()

        assert result.healthy is False
        assert result.reason == "slack_down"
        assert result.remediation_url is None

    async def test_cascade_token_revoked(self):
        client = _make_web_client()
        client.auth_test = AsyncMock(side_effect=Exception("token_revoked"))
        probe = self._primed_probe(client)

        result = await probe._run_diagnostic_cascade()

        assert result.healthy is False
        assert result.reason == "token_revoked"
        assert result.remediation_url is not None
        assert "oauth" in result.remediation_url

    async def test_cascade_events_disabled(self):
        client = _make_web_client()
        # api_test and auth_test both succeed; no link_disabled reason
        probe = self._primed_probe(client)

        result = await probe._run_diagnostic_cascade()

        assert result.healthy is False
        assert result.reason == "events_disabled"
        assert result.remediation_url is not None
        assert "event-subscriptions" in result.remediation_url

    async def test_cascade_link_disabled(self):
        client = _make_web_client()
        # api_test OK, auth_test OK, but disconnect reason was link_disabled
        probe = self._primed_probe(client)
        probe._last_disconnect_reason = "link_disabled"

        result = await probe._run_diagnostic_cascade()

        assert result.healthy is False
        assert result.reason == "socket_disabled"
        assert result.remediation_url is not None
        assert "socket-mode" in result.remediation_url


# ---------------------------------------------------------------------------
# on_ws_message
# ---------------------------------------------------------------------------


class TestOnWsMessage:
    def _primed_probe(self, client=None) -> EventProbe:
        if client is None:
            client = _make_web_client()
        probe = _make_probe(client)
        probe._anchor_channel_id = "C_PROBE"
        probe._anchor_ts = "111.222"
        return probe

    async def test_on_ws_message_reaction_added_sets_event(self):
        probe = self._primed_probe()
        assert not probe._event_received.is_set()

        msg = _make_ws_message(
            {
                "type": "events_api",
                "payload": {
                    "event": {
                        "type": "reaction_added",
                        "item": {"channel": "C_PROBE", "ts": "111.222"},
                    }
                },
            }
        )
        await probe.on_ws_message(msg)

        assert probe._event_received.is_set()

    async def test_on_ws_message_wrong_channel_ignored(self):
        probe = self._primed_probe()

        msg = _make_ws_message(
            {
                "type": "events_api",
                "payload": {
                    "event": {
                        "type": "reaction_added",
                        "item": {"channel": "C_OTHER", "ts": "111.222"},
                    }
                },
            }
        )
        await probe.on_ws_message(msg)

        assert not probe._event_received.is_set()

    async def test_on_ws_message_wrong_ts_ignored(self):
        probe = self._primed_probe()

        msg = _make_ws_message(
            {
                "type": "events_api",
                "payload": {
                    "event": {
                        "type": "reaction_added",
                        "item": {"channel": "C_PROBE", "ts": "999.000"},
                    }
                },
            }
        )
        await probe.on_ws_message(msg)

        assert not probe._event_received.is_set()

    async def test_on_ws_message_disconnect_stores_reason(self):
        probe = self._primed_probe()

        msg = _make_ws_message({"type": "disconnect", "reason": "link_disabled"})
        await probe.on_ws_message(msg)

        assert probe._last_disconnect_reason == "link_disabled"

    async def test_on_ws_message_disconnect_no_reason_no_crash(self):
        probe = self._primed_probe()

        msg = _make_ws_message({"type": "disconnect"})
        await probe.on_ws_message(msg)

        assert probe._last_disconnect_reason is None

    async def test_on_ws_message_ignores_unrelated_types(self):
        probe = self._primed_probe()

        msg = _make_ws_message({"type": "hello", "num_connections": 1})
        await probe.on_ws_message(msg)

        assert not probe._event_received.is_set()

    async def test_on_ws_message_non_text_ignored(self):
        probe = self._primed_probe()
        msg = _make_non_text_ws_message()
        await probe.on_ws_message(msg)
        assert not probe._event_received.is_set()

    async def test_on_ws_message_exception_safe(self):
        probe = self._primed_probe()

        import aiohttp

        # malformed message — data is not valid JSON
        msg = MagicMock()
        msg.type = aiohttp.WSMsgType.TEXT
        msg.data = "not-json{"
        await probe.on_ws_message(msg)  # must not raise


# ---------------------------------------------------------------------------
# format_alert
# ---------------------------------------------------------------------------


class TestFormatAlert:
    def _make_probe_with_config(self, app_token: str) -> EventProbe:
        config = _make_config(slack_app_token=app_token)
        client = _make_web_client()
        return EventProbe(web_client=client, config=config)

    def test_format_alert_with_url(self):
        probe = self._make_probe_with_config("xapp-1-A0123ABCDE-12345-abc")
        result = DiagnosticResult(
            healthy=False,
            reason="events_disabled",
            details="Events are not being delivered.",
            remediation_url="https://api.slack.com/apps/A0123ABCDE/event-subscriptions",
        )
        with patch("summon_claude.slack.client.redact_secrets", side_effect=lambda s: s):
            alert = probe.format_alert(result)

        assert "Event pipeline failure" in alert
        assert "Events are not being delivered." in alert
        assert "https://api.slack.com/apps/A0123ABCDE/event-subscriptions" in alert

    def test_format_alert_without_url(self):
        probe = self._make_probe_with_config("xapp-test-token")
        result = DiagnosticResult(
            healthy=False,
            reason="slack_down",
            details="Slack API is unreachable.",
        )
        with patch("summon_claude.slack.client.redact_secrets", side_effect=lambda s: s):
            alert = probe.format_alert(result)

        assert "Event pipeline failure" in alert
        assert "Slack API is unreachable." in alert
        assert "http" not in alert


# ---------------------------------------------------------------------------
# Guard tests — pin interfaces and constants
# ---------------------------------------------------------------------------


class TestEventProbeGuards:
    """Pin DiagnosticResult reason values and EventProbe public API."""

    def test_valid_reason_values_pinned(self):
        valid_reasons = {
            "events_disabled",
            "token_revoked",
            "slack_down",
            "socket_disabled",
            "unknown",
            "healthy",
            "cancelled",
        }
        # Verify all reasons are used in the codebase by checking EventProbe methods
        import inspect

        source = inspect.getsource(EventProbe)
        for reason in valid_reasons:
            assert f'"{reason}"' in source or f"'{reason}'" in source, (
                f"Reason {reason!r} not found in EventProbe source"
            )

    def test_event_probe_public_api_pinned(self):
        expected_methods = {
            "setup_anchor",
            "run_probe",
            "on_ws_message",
            "format_alert",
            "cancel_probe",
            "reset_cancel",
        }
        actual_public = {
            name
            for name in dir(EventProbe)
            if not name.startswith("_") and callable(getattr(EventProbe, name))
        }
        assert expected_methods <= actual_public, (
            f"Missing public methods: {expected_methods - actual_public}"
        )

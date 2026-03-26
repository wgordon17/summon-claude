"""Unit tests for EventConsumer (tests/integration/conftest.py).

Tests event queue behavior, predicate matching, timeout logic, and
lifecycle management without requiring Slack credentials or a real
Socket Mode connection.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.integration.conftest import EventConsumer

# ---------------------------------------------------------------------------
# wait_for_event
# ---------------------------------------------------------------------------


class TestWaitForEvent:
    """Tests for EventConsumer.wait_for_event()."""

    async def test_returns_first_matching_event(self):
        """Predicate match returns immediately."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        await consumer._events.put({"type": "message", "text": "hello"})

        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=1.0,
        )
        assert event["text"] == "hello"

    async def test_skips_non_matching_returns_match(self):
        """Non-matching events are skipped, first match returned."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        await consumer._events.put({"type": "reaction_added", "reaction": "eyes"})
        await consumer._events.put({"type": "file_shared", "file_id": "F1"})
        await consumer._events.put({"type": "message", "text": "target"})

        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=1.0,
        )
        assert event["text"] == "target"

    async def test_timeout_includes_seen_summary(self):
        """TimeoutError message includes non-matching event types."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        await consumer._events.put({"type": "reaction_added"})
        await consumer._events.put({"type": "file_shared"})

        with pytest.raises(TimeoutError, match="2 non-matching"):
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message",
                timeout=0.5,
            )

    async def test_timeout_on_empty_queue(self):
        """Empty queue times out with zero non-matching."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")

        with pytest.raises(TimeoutError, match="0 non-matching"):
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message",
                timeout=0.5,
            )

    async def test_event_arriving_during_wait(self):
        """Event put into queue while wait_for_event is blocking."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")

        async def delayed_put():
            await asyncio.sleep(0.1)
            await consumer._events.put({"type": "message", "text": "delayed"})

        asyncio.create_task(delayed_put())
        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=2.0,
        )
        assert event["text"] == "delayed"

    async def test_multiple_matches_returns_first(self):
        """When multiple events match, the first one is returned."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        await consumer._events.put({"type": "message", "text": "first"})
        await consumer._events.put({"type": "message", "text": "second"})

        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=1.0,
        )
        assert event["text"] == "first"
        # Second event still in queue
        assert not consumer._events.empty()


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


class TestDrain:
    """Tests for EventConsumer.drain()."""

    async def test_drain_returns_all_events(self):
        """drain() returns all queued events in order."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        await consumer._events.put({"type": "message"})
        await consumer._events.put({"type": "reaction_added"})

        events = consumer.drain()
        assert len(events) == 2
        assert events[0]["type"] == "message"
        assert events[1]["type"] == "reaction_added"

    async def test_drain_empties_queue(self):
        """Queue is empty after drain()."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        await consumer._events.put({"type": "message"})

        consumer.drain()
        assert consumer._events.empty()

    async def test_drain_empty_queue_returns_empty_list(self):
        """drain() on empty queue returns empty list."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        assert consumer.drain() == []


# ---------------------------------------------------------------------------
# _capture_event
# ---------------------------------------------------------------------------


class TestCaptureEvent:
    """Tests for EventConsumer._capture_event()."""

    async def test_puts_event_in_queue(self):
        """_capture_event enqueues the event dict."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        event = {"type": "message", "text": "hello", "channel": "C001"}

        await consumer._capture_event(event)

        queued = consumer._events.get_nowait()
        assert queued == event

    async def test_multiple_captures_preserve_order(self):
        """Multiple captures enqueue in FIFO order."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")

        await consumer._capture_event({"type": "message", "text": "first"})
        await consumer._capture_event({"type": "message", "text": "second"})

        events = consumer.drain()
        assert [e["text"] for e in events] == ["first", "second"]


# ---------------------------------------------------------------------------
# Lifecycle (start / stop)
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Tests for EventConsumer.start() and stop()."""

    async def test_start_creates_app_with_self_events_disabled(self):
        """start() passes ignoring_self_events_enabled=False to AsyncApp."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")

        with (
            patch("tests.integration.conftest.AsyncApp") as mock_app_cls,
            patch("tests.integration.conftest.AsyncSocketModeHandler") as mock_handler_cls,
        ):
            mock_app = MagicMock()
            mock_app.event = MagicMock(return_value=lambda fn: fn)
            mock_app_cls.return_value = mock_app

            mock_handler = AsyncMock()
            mock_handler.connect_async = AsyncMock()
            mock_handler_cls.return_value = mock_handler

            await consumer.start()

            mock_app_cls.assert_called_once_with(
                token="xoxb-test",
                signing_secret="secret",
                ignoring_self_events_enabled=False,
            )

    async def test_start_registers_all_event_types(self):
        """start() registers handlers for all subscribed event types."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        registered_types: list[str] = []

        def mock_event_decorator(event_type: str):
            registered_types.append(event_type)
            return lambda fn: fn

        with (
            patch("tests.integration.conftest.AsyncApp") as mock_app_cls,
            patch("tests.integration.conftest.AsyncSocketModeHandler") as mock_handler_cls,
        ):
            mock_app = MagicMock()
            mock_app.event = MagicMock(side_effect=mock_event_decorator)
            mock_app_cls.return_value = mock_app

            mock_handler = AsyncMock()
            mock_handler.connect_async = AsyncMock()
            mock_handler_cls.return_value = mock_handler

            await consumer.start()

        assert set(registered_types) == {
            "message",
            "reaction_added",
            "file_shared",
            "app_home_opened",
        }

    async def test_start_calls_connect_async(self):
        """start() establishes the Socket Mode connection."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")

        with (
            patch("tests.integration.conftest.AsyncApp") as mock_app_cls,
            patch("tests.integration.conftest.AsyncSocketModeHandler") as mock_handler_cls,
        ):
            mock_app = MagicMock()
            mock_app.event = MagicMock(return_value=lambda fn: fn)
            mock_app_cls.return_value = mock_app

            mock_handler = AsyncMock()
            mock_handler.connect_async = AsyncMock()
            mock_handler_cls.return_value = mock_handler

            await consumer.start()

            mock_handler.connect_async.assert_awaited_once()
            assert consumer._handler is mock_handler

    async def test_stop_with_no_handler_is_noop(self):
        """stop() does nothing when _handler is None."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        assert consumer._handler is None
        await consumer.stop()  # must not raise

    async def test_stop_closes_handler(self):
        """stop() calls close_async() on the handler."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock()
        consumer._handler = mock_handler

        await consumer.stop()
        mock_handler.close_async.assert_awaited_once()

    async def test_stop_catches_close_error(self):
        """stop() catches and logs close_async() errors without raising."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")
        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock(side_effect=RuntimeError("close failed"))
        consumer._handler = mock_handler

        await consumer.stop()  # must not raise

    async def test_handler_stays_none_on_connect_failure(self):
        """If connect_async() raises, _handler remains None."""
        consumer = EventConsumer("xoxb-test", "xapp-test", "secret")

        with (
            patch("tests.integration.conftest.AsyncApp") as mock_app_cls,
            patch("tests.integration.conftest.AsyncSocketModeHandler") as mock_handler_cls,
        ):
            mock_app = MagicMock()
            mock_app.event = MagicMock(return_value=lambda fn: fn)
            mock_app_cls.return_value = mock_app

            mock_handler = AsyncMock()
            mock_handler.connect_async = AsyncMock(side_effect=ConnectionError("refused"))
            mock_handler_cls.return_value = mock_handler

            with pytest.raises(ConnectionError):
                await consumer.start()

            assert consumer._handler is None

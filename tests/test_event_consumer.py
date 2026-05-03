"""Unit tests for EventConsumer (tests/integration/conftest.py).

Tests event queue behavior, predicate matching, timeout logic, and
lifecycle management without requiring Slack credentials or a real
Socket Mode connection.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.integration.conftest import EventConsumer, SharedEventStore

# ---------------------------------------------------------------------------
# SharedEventStore
# ---------------------------------------------------------------------------


class TestSharedEventStore:
    """Direct unit tests for SharedEventStore."""

    def test_put_before_open_writer_is_noop(self, tmp_path):
        """put() before open_writer() silently drops the event (no write_fd)."""
        path = tmp_path / "events.jsonl"
        store = SharedEventStore(path)
        # _write_fd is None at construction — put() must not raise or write
        store.put({"type": "message"})
        assert path.read_bytes() == b""

    def test_refcount_fd_stays_open_until_last_close(self, tmp_path):
        """FD stays open when multiple consumers share the store."""
        path = tmp_path / "events.jsonl"
        store = SharedEventStore(path)
        store.open_writer()
        store.open_writer()
        store.close_writer()
        assert store._write_fd is not None
        store.put({"type": "message"})
        store.close_writer()
        assert store._write_fd is None
        events = store.drain()
        assert len(events) == 1

    def test_refcount_put_works_after_first_consumer_stops(self, tmp_path):
        """put() still writes after one of two consumers disconnects."""
        path = tmp_path / "events.jsonl"
        store = SharedEventStore(path)
        store.open_writer()
        store.open_writer()
        store.put({"type": "a"})
        store.close_writer()
        store.put({"type": "b"})
        store.close_writer()
        events = store.drain()
        assert [e["type"] for e in events] == ["a", "b"]

    def test_close_without_open_raises(self, tmp_path):
        """close_writer() without open_writer() raises AssertionError."""
        path = tmp_path / "events.jsonl"
        store = SharedEventStore(path)
        with pytest.raises(AssertionError, match="close_writer called more"):
            store.close_writer()

    def test_reset_reader_skips_preexisting_events(self, tmp_path):
        """reset_reader() causes subsequent drain() to skip events written before the call."""
        path = tmp_path / "events.jsonl"
        store = SharedEventStore(path)
        store.open_writer()
        store.put({"type": "old_event"})
        store.close_writer()

        # reset_reader() advances the read offset to current EOF
        store.reset_reader()

        store.open_writer()
        store.put({"type": "new_event"})
        store.close_writer()

        events = store.drain()
        assert len(events) == 1
        assert events[0]["type"] == "new_event"


@pytest.fixture
def event_store(tmp_path):
    """Per-test SharedEventStore backed by a temp file."""
    path = tmp_path / "events.jsonl"
    store = SharedEventStore(path)
    store.reset_reader()
    return store


def _make_consumer(event_store: SharedEventStore) -> EventConsumer:
    return EventConsumer("xoxb-test", "xapp-test", "secret", event_store=event_store)


# ---------------------------------------------------------------------------
# wait_for_event
# ---------------------------------------------------------------------------


class TestWaitForEvent:
    """Tests for EventConsumer.wait_for_event()."""

    async def test_returns_first_matching_event(self, event_store):
        """Predicate match returns immediately."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()
        event_store.put({"type": "message", "text": "hello"})

        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=1.0,
        )
        assert event["text"] == "hello"
        event_store.close_writer()

    async def test_skips_non_matching_returns_match(self, event_store):
        """Non-matching events are skipped, first match returned."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()
        event_store.put({"type": "reaction_added", "reaction": "eyes"})
        event_store.put({"type": "file_shared", "file_id": "F1"})
        event_store.put({"type": "message", "text": "target"})

        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=1.0,
        )
        assert event["text"] == "target"
        event_store.close_writer()

    async def test_timeout_includes_seen_summary(self, event_store):
        """TimeoutError message includes non-matching event types."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()
        event_store.put({"type": "reaction_added"})
        event_store.put({"type": "file_shared"})

        with pytest.raises(TimeoutError, match="2 non-matching"):
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message",
                timeout=0.5,
            )
        event_store.close_writer()

    async def test_timeout_on_empty_queue(self, event_store):
        """Empty queue times out with zero non-matching."""
        consumer = _make_consumer(event_store)

        with pytest.raises(TimeoutError, match="0 non-matching"):
            await consumer.wait_for_event(
                lambda e: e.get("type") == "message",
                timeout=0.5,
            )

    async def test_event_arriving_during_wait(self, event_store):
        """Event put into store while wait_for_event is polling."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()

        async def delayed_put():
            await asyncio.sleep(0.3)
            event_store.put({"type": "message", "text": "delayed"})

        asyncio.create_task(delayed_put())
        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=2.0,
        )
        assert event["text"] == "delayed"
        event_store.close_writer()

    async def test_multiple_matches_returns_first(self, event_store):
        """When multiple events match, the first one is returned."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()
        event_store.put({"type": "message", "text": "first"})
        event_store.put({"type": "message", "text": "second"})

        event = await consumer.wait_for_event(
            lambda e: e.get("type") == "message",
            timeout=1.0,
        )
        assert event["text"] == "first"
        event_store.close_writer()


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


class TestDrain:
    """Tests for EventConsumer.drain()."""

    def test_drain_returns_all_events(self, event_store):
        """drain() returns all pending events in order."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()
        event_store.put({"type": "message"})
        event_store.put({"type": "reaction_added"})

        events = consumer.drain()
        assert len(events) == 2
        assert events[0]["type"] == "message"
        assert events[1]["type"] == "reaction_added"
        event_store.close_writer()

    def test_drain_advances_reader(self, event_store):
        """After drain(), subsequent drain() returns empty."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()
        event_store.put({"type": "message"})

        consumer.drain()
        assert consumer.drain() == []
        event_store.close_writer()

    def test_drain_empty_returns_empty_list(self, event_store):
        """drain() on empty store returns empty list."""
        consumer = _make_consumer(event_store)
        assert consumer.drain() == []


# ---------------------------------------------------------------------------
# _capture_event
# ---------------------------------------------------------------------------


class TestCaptureEvent:
    """Tests for EventConsumer._capture_event()."""

    async def test_puts_event_in_store(self, event_store):
        """_capture_event writes the event to the shared store."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()
        event = {"type": "message", "text": "hello", "channel": "C001"}

        await consumer._capture_event(event)

        events = consumer.drain()
        assert len(events) == 1
        assert events[0] == event
        event_store.close_writer()

    async def test_multiple_captures_preserve_order(self, event_store):
        """Multiple captures write in FIFO order."""
        consumer = _make_consumer(event_store)
        event_store.open_writer()

        await consumer._capture_event({"type": "message", "text": "first"})
        await consumer._capture_event({"type": "message", "text": "second"})

        events = consumer.drain()
        assert [e["text"] for e in events] == ["first", "second"]
        event_store.close_writer()


# ---------------------------------------------------------------------------
# Lifecycle (start / stop)
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Tests for EventConsumer.start() and stop()."""

    async def test_start_creates_app_with_self_events_disabled(self, event_store):
        """start() passes ignoring_self_events_enabled=False to AsyncApp."""
        consumer = _make_consumer(event_store)

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

    async def test_start_registers_all_event_types(self, event_store):
        """start() registers handlers for all subscribed event types."""
        consumer = _make_consumer(event_store)
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

    async def test_start_calls_connect_async(self, event_store):
        """start() establishes the Socket Mode connection."""
        consumer = _make_consumer(event_store)

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

    async def test_stop_with_no_handler_is_noop(self, event_store):
        """stop() does nothing when _handler is None."""
        consumer = _make_consumer(event_store)
        assert consumer._handler is None
        await consumer.stop()  # must not raise

    async def test_double_stop_is_idempotent(self, event_store):
        """Calling stop() twice does not crash or corrupt refcount."""
        consumer = _make_consumer(event_store)
        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock()
        consumer._handler = mock_handler
        event_store.open_writer()

        await consumer.stop()
        await consumer.stop()
        assert event_store._write_fd is None
        assert event_store._writer_refcount == 0

    async def test_stop_closes_handler(self, event_store):
        """stop() calls close_async() on the handler and closes the writer."""
        consumer = _make_consumer(event_store)
        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock()
        consumer._handler = mock_handler
        event_store.open_writer()

        await consumer.stop()
        mock_handler.close_async.assert_awaited_once()
        assert event_store._write_fd is None

    async def test_stop_catches_close_error(self, event_store):
        """stop() catches and logs close_async() errors without raising."""
        consumer = _make_consumer(event_store)
        mock_handler = AsyncMock()
        mock_handler.close_async = AsyncMock(side_effect=RuntimeError("close failed"))
        consumer._handler = mock_handler
        event_store.open_writer()

        await consumer.stop()  # must not raise
        assert event_store._write_fd is None

    async def test_handler_stays_none_on_connect_failure(self, event_store):
        """If connect_async() raises, _handler remains None."""
        consumer = _make_consumer(event_store)

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

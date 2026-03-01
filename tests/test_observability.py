"""Tests for observability features: queue backpressure, log correlation, per-session logs."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.session import SessionIdFilter, _session_id_var

# ---------------------------------------------------------------------------
# Queue backpressure
# ---------------------------------------------------------------------------


class TestQueueBackpressure:
    def test_queue_has_maxsize_100(self):
        """SummonSession._message_queue must have maxsize=100 for backpressure."""
        from unittest.mock import MagicMock

        from summon_claude.session import SessionOptions, SummonSession
        from summon_claude.sessions.auth import SessionAuth

        config = MagicMock()
        options = SessionOptions(session_id="test-bp", cwd="/tmp", name="test")
        auth = MagicMock(spec=SessionAuth)
        session = SummonSession(config=config, options=options, auth=auth)

        assert session._message_queue.maxsize == 100

    async def test_queue_raises_full_at_limit(self):
        """Queue raises QueueFull when maxsize is exceeded with put_nowait."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=100)
        for i in range(100):
            q.put_nowait(i)

        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(100)

    async def test_queue_accepts_up_to_maxsize(self):
        """Queue accepts exactly maxsize items without error."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=100)
        for i in range(100):
            q.put_nowait(i)
        assert q.qsize() == 100
        assert q.full()


# ---------------------------------------------------------------------------
# Log correlation via session_id contextvar
# ---------------------------------------------------------------------------


class TestSessionIdFilter:
    def test_filter_sets_empty_session_id_by_default(self):
        """Outside a session task, session_id should be empty string."""
        flt = SessionIdFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello",
            args=(),
            exc_info=None,
        )
        result = flt.filter(record)
        assert result is True
        assert record.session_id == ""  # type: ignore[attr-defined]

    def test_filter_sets_session_id_when_contextvar_set(self):
        """When session_id contextvar is set, filter wraps it in brackets."""
        _session_id_var.set("abc-123")
        try:
            flt = SessionIdFilter()
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="hello",
                args=(),
                exc_info=None,
            )
            flt.filter(record)
            assert record.session_id == "[abc-123] "  # type: ignore[attr-defined]
        finally:
            _session_id_var.set("")

    async def test_filter_is_task_scoped(self):
        """Each asyncio task sees its own contextvar value."""
        results: dict[str, str] = {}

        async def _task(sid: str) -> None:
            _session_id_var.set(sid)
            await asyncio.sleep(0)  # yield to allow interleaving
            flt = SessionIdFilter()
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg="hello",
                args=(),
                exc_info=None,
            )
            flt.filter(record)
            results[sid] = record.session_id  # type: ignore[attr-defined]

        await asyncio.gather(_task("session-A"), _task("session-B"))

        assert results["session-A"] == "[session-A] "
        assert results["session-B"] == "[session-B] "

    async def test_session_start_sets_contextvar(self):
        """SummonSession.start() sets _session_id_var before registry work."""
        from summon_claude.session import SessionOptions, SummonSession
        from summon_claude.sessions.auth import SessionAuth

        captured_sid: list[str] = []

        async def _fake_register(**kwargs):
            captured_sid.append(_session_id_var.get())

        config = MagicMock()
        options = SessionOptions(session_id="ctx-test-session", cwd="/tmp", name="test")
        auth = MagicMock(spec=SessionAuth)
        session = SummonSession(config=config, options=options, auth=auth)
        # Trigger immediate shutdown so start() exits quickly
        session._shutdown_event.set()

        mock_registry = AsyncMock()
        mock_registry.__aenter__ = AsyncMock(return_value=mock_registry)
        mock_registry.__aexit__ = AsyncMock(return_value=False)
        mock_registry.register = _fake_register
        mock_registry.log_event = AsyncMock()
        mock_registry.update_status = AsyncMock()
        mock_registry.delete_pending_token = AsyncMock()

        with patch("summon_claude.session.SessionRegistry", return_value=mock_registry):
            await session.start()

        assert captured_sid == ["ctx-test-session"]


# ---------------------------------------------------------------------------
# Per-session log files
# ---------------------------------------------------------------------------


class TestPerSessionLogFile:
    """Per-session log files at ~/.summon/logs/{session_id}.log."""

    def test_install_creates_log_file(self, tmp_path):
        """_install_session_log_handler creates a log file for the session."""
        from summon_claude.session import SessionOptions, SummonSession
        from summon_claude.sessions.auth import SessionAuth

        config = MagicMock()
        options = SessionOptions(session_id="log-test-session", cwd="/tmp", name="test")
        auth = MagicMock(spec=SessionAuth)
        session = SummonSession(config=config, options=options, auth=auth)

        with patch("summon_claude.session.get_data_dir", return_value=tmp_path):
            handler = session._install_session_log_handler()

        assert handler is not None
        log_file = tmp_path / "logs" / "log-test-session.log"
        assert log_file.exists()

        # Cleanup
        logging.getLogger().removeHandler(handler)
        handler.close()

    def test_handler_filters_by_session_id(self, tmp_path):
        """Only log records from the matching session task are written."""
        from summon_claude.session import SessionOptions, SummonSession
        from summon_claude.sessions.auth import SessionAuth

        config = MagicMock()
        options = SessionOptions(session_id="filter-test", cwd="/tmp", name="test")
        auth = MagicMock(spec=SessionAuth)
        session = SummonSession(config=config, options=options, auth=auth)

        with patch("summon_claude.session.get_data_dir", return_value=tmp_path):
            handler = session._install_session_log_handler()

        assert handler is not None
        test_logger = logging.getLogger("summon_claude.test_filter")
        test_logger.setLevel(logging.DEBUG)

        # Log with matching session_id — should appear in file
        _session_id_var.set("filter-test")
        test_logger.info("This should be captured")

        # Log with different session_id — should NOT appear
        _session_id_var.set("other-session")
        test_logger.info("This should NOT be captured")

        # Log with no session_id — should NOT appear
        _session_id_var.set("")
        test_logger.info("This should also NOT be captured")

        handler.flush()
        log_file = tmp_path / "logs" / "filter-test.log"
        content = log_file.read_text()

        assert "This should be captured" in content
        assert "This should NOT be captured" not in content
        assert "This should also NOT be captured" not in content

        # Cleanup
        _session_id_var.set("")
        logging.getLogger().removeHandler(handler)
        handler.close()

    def test_remove_handler_cleans_up(self, tmp_path):
        """_remove_session_log_handler removes the handler from root logger."""
        from summon_claude.session import SessionOptions, SummonSession
        from summon_claude.sessions.auth import SessionAuth

        config = MagicMock()
        options = SessionOptions(session_id="cleanup-test", cwd="/tmp", name="test")
        auth = MagicMock(spec=SessionAuth)
        session = SummonSession(config=config, options=options, auth=auth)

        with patch("summon_claude.session.get_data_dir", return_value=tmp_path):
            handler = session._install_session_log_handler()

        assert handler in logging.getLogger().handlers
        SummonSession._remove_session_log_handler(handler)
        assert handler not in logging.getLogger().handlers

    def test_remove_none_handler_is_noop(self):
        """_remove_session_log_handler(None) does not raise."""
        from summon_claude.session import SummonSession

        SummonSession._remove_session_log_handler(None)  # must not raise

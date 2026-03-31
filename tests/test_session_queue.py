"""Tests for session queue — FIFO per-project queue in SessionManager."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig
from summon_claude.sessions.auth import SessionAuth
from summon_claude.sessions.manager import _MAX_QUEUED_SESSIONS, SessionManager, _QueuedSession
from summon_claude.sessions.registry import MAX_SPAWN_CHILDREN_PM
from summon_claude.sessions.session import SessionOptions

# ---------------------------------------------------------------------------
# Helpers (mirror test_sessions_manager.py conventions)
# ---------------------------------------------------------------------------


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "slack_signing_secret": "secret",
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def make_options(name: str = "test", **overrides) -> SessionOptions:
    defaults = {"cwd": "/tmp", "name": name}
    defaults.update(overrides)
    return SessionOptions(**defaults)


def _make_manager() -> tuple[SessionManager, MagicMock, MagicMock]:
    """Return (manager, mock_provider, mock_dispatcher)."""
    cfg = make_config()
    mock_provider = MagicMock()
    mock_provider.post_message = AsyncMock()
    mock_provider.chat_postMessage = AsyncMock()
    mock_dispatcher = MagicMock()
    mock_dispatcher.unregister = MagicMock()

    manager = SessionManager(
        config=cfg,
        web_client=mock_provider,
        bot_user_id="UBOT",
        dispatcher=mock_dispatcher,
    )
    return manager, mock_provider, mock_dispatcher


class _StubSession:
    """Minimal stub satisfying SessionManager's session interface."""

    def __init__(self, *, fail_with: Exception | None = None):
        self._fail_with = fail_with
        self.channel_id: str | None = None
        self.target_channel_id: str | None = None
        self.is_pm: bool = False
        self.project_id: str | None = None
        self._shutdown_requested = False
        self._authenticated_user_id: str | None = None
        self._authenticated_event = asyncio.Event()
        self._session_id: str = "stub"

    async def start(self) -> bool:
        if self._fail_with is not None:
            raise self._fail_with
        await asyncio.sleep(0)
        return True

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    def authenticate(self, user_id: str) -> None:
        self._authenticated_user_id = user_id
        self._authenticated_event.set()


# ---------------------------------------------------------------------------
# Tests: _QueuedSession dataclass structure
# ---------------------------------------------------------------------------


class TestQueuedSessionGuard:
    """Pin _QueuedSession fields to prevent accidental schema drift."""

    def test_fields_are_pinned(self):
        fields = {f.name for f in dataclasses.fields(_QueuedSession)}
        assert fields == {"options", "project_id", "pm_session_id", "authenticated_user_id"}

    def test_parent_session_id_not_a_field(self):
        """parent_session_id must NOT be a direct field on _QueuedSession."""
        fields = {f.name for f in dataclasses.fields(_QueuedSession)}
        assert "parent_session_id" not in fields

    def test_is_frozen(self):
        assert _QueuedSession.__dataclass_params__.frozen is True  # type: ignore[attr-defined]

    def test_construction(self):
        opts = make_options()
        entry = _QueuedSession(
            options=opts,
            project_id="proj-1",
            pm_session_id="pm-abc",
            authenticated_user_id="U_OWNER",
        )
        assert entry.project_id == "proj-1"
        assert entry.pm_session_id == "pm-abc"
        assert entry.authenticated_user_id == "U_OWNER"
        assert entry.options is opts


# ---------------------------------------------------------------------------
# Tests: queue_session — basic enqueue + FIFO ordering
# ---------------------------------------------------------------------------


class TestQueueBasicEnqueue:
    def test_enqueue_returns_position_1_based(self):
        manager, _, _ = _make_manager()
        opts = make_options(name="task-a")
        pos = manager.queue_session(
            opts,
            project_id="proj-1",
            pm_session_id="pm-1",
            authenticated_user_id="U_OWNER",
        )
        assert pos == 1

    def test_enqueue_fifo_order(self):
        manager, _, _ = _make_manager()
        names = ["task-a", "task-b", "task-c"]
        positions = []
        for name in names:
            pos = manager.queue_session(
                make_options(name=name),
                project_id="proj-1",
                pm_session_id="pm-1",
                authenticated_user_id="U_OWNER",
            )
            positions.append(pos)

        assert positions == [1, 2, 3]

        # Dequeue in FIFO order by inspecting the deque directly
        q = manager._session_queue["proj-1"]
        dequeued_names = [entry.options.name for entry in q]
        assert dequeued_names == names

    def test_enqueue_different_projects_isolated(self):
        manager, _, _ = _make_manager()
        manager.queue_session(
            make_options(name="a1"),
            project_id="proj-a",
            pm_session_id="pm-a",
            authenticated_user_id="U_OWNER",
        )
        manager.queue_session(
            make_options(name="b1"),
            project_id="proj-b",
            pm_session_id="pm-b",
            authenticated_user_id="U_OWNER",
        )
        manager.queue_session(
            make_options(name="a2"),
            project_id="proj-a",
            pm_session_id="pm-a",
            authenticated_user_id="U_OWNER",
        )

        assert len(manager._session_queue["proj-a"]) == 2
        assert len(manager._session_queue["proj-b"]) == 1

    def test_enqueue_initial_prompt_preserved(self):
        """initial_prompt on SessionOptions survives queue round-trip."""
        manager, _, _ = _make_manager()
        opts = SessionOptions(cwd="/tmp", name="task", initial_prompt="Build the login page")
        manager.queue_session(
            opts,
            project_id="proj-1",
            pm_session_id="pm-1",
            authenticated_user_id="U_OWNER",
        )
        entry = manager._session_queue["proj-1"][0]
        assert entry.options.initial_prompt == "Build the login page"


# ---------------------------------------------------------------------------
# Tests: queue_session — cap enforcement
# ---------------------------------------------------------------------------


class TestQueueCapEnforcement:
    def test_queue_full_returns_minus_one(self):
        manager, _, _ = _make_manager()
        # Fill proj-a up to its per-project cap
        for i in range(_MAX_QUEUED_SESSIONS):
            manager.queue_session(
                make_options(name=f"a{i}"),
                project_id="proj-a",
                pm_session_id="pm-a",
                authenticated_user_id="U_OWNER",
            )

        # proj-b is empty — it has its own independent cap
        # proj-a is at cap — next enqueue for proj-a should return -1
        result = manager.queue_session(
            make_options(name="overflow"),
            project_id="proj-a",
            pm_session_id="pm-a",
            authenticated_user_id="U_OWNER",
        )
        assert result == -1

        # proj-b still accepts sessions (per-project isolation)
        result_b = manager.queue_session(
            make_options(name="b-ok"),
            project_id="proj-b",
            pm_session_id="pm-b",
            authenticated_user_id="U_OWNER",
        )
        assert result_b == 1

    def test_queue_at_cap_does_not_add_entry(self):
        manager, _, _ = _make_manager()
        for i in range(_MAX_QUEUED_SESSIONS):
            manager.queue_session(
                make_options(name=f"t{i}"),
                project_id="proj-1",
                pm_session_id="pm-1",
                authenticated_user_id="U_OWNER",
            )

        total_before = sum(len(dq) for dq in manager._session_queue.values())
        manager.queue_session(
            make_options(name="overflow"),
            project_id="proj-1",
            pm_session_id="pm-1",
            authenticated_user_id="U_OWNER",
        )
        total_after = sum(len(dq) for dq in manager._session_queue.values())
        assert total_after == total_before  # cap prevents addition


# ---------------------------------------------------------------------------
# Tests: clear_queue
# ---------------------------------------------------------------------------


class TestClearQueue:
    def test_clear_queue_removes_all_entries(self):
        manager, _, _ = _make_manager()
        for i in range(3):
            manager.queue_session(
                make_options(name=f"t{i}"),
                project_id="proj-1",
                pm_session_id="pm-1",
                authenticated_user_id="U_OWNER",
            )

        count = manager.clear_queue("proj-1")
        assert count == 3
        assert "proj-1" not in manager._session_queue

    def test_clear_queue_empty_returns_zero(self):
        manager, _, _ = _make_manager()
        count = manager.clear_queue("nonexistent")
        assert count == 0

    def test_clear_queue_project_isolation(self):
        manager, _, _ = _make_manager()
        manager.queue_session(
            make_options(name="a1"),
            project_id="proj-a",
            pm_session_id="pm-a",
            authenticated_user_id="U_OWNER",
        )
        manager.queue_session(
            make_options(name="b1"),
            project_id="proj-b",
            pm_session_id="pm-b",
            authenticated_user_id="U_OWNER",
        )

        manager.clear_queue("proj-a")

        assert "proj-a" not in manager._session_queue
        assert "proj-b" in manager._session_queue
        assert len(manager._session_queue["proj-b"]) == 1


# ---------------------------------------------------------------------------
# Tests: _dequeue_and_start — core dequeue logic
# ---------------------------------------------------------------------------


class TestDequeueAndStart:
    async def test_dequeue_pops_entry_and_starts_session(self):
        """_dequeue_and_start removes the entry from the queue and starts a session."""
        manager, mock_provider, mock_dispatcher = _make_manager()

        # Pre-populate the queue for proj-1
        opts = make_options(name="task-a")
        manager.queue_session(
            opts,
            project_id="proj-1",
            pm_session_id="pm-sess-1",
            authenticated_user_id="U_OWNER",
        )

        # Stub the PM session (needed by _dequeue_and_start cap check)
        pm_stub = _StubSession()
        pm_stub.is_pm = True
        pm_stub.project_id = "proj-1"
        pm_stub.channel_id = "C_PM"
        manager._sessions["pm-sess-1"] = pm_stub  # type: ignore[assignment]

        new_sessions: list = []

        with (
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
            patch("summon_claude.sessions.manager.SummonSession") as mock_sess_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            # Return empty children — cap not reached
            mock_reg.list_children = AsyncMock(return_value=[])

            mock_sess_instance = MagicMock()
            mock_sess_instance.start = AsyncMock()
            mock_sess_instance.authenticate = MagicMock()
            mock_sess_instance.channel_id = None
            mock_sess_instance.is_pm = False
            mock_sess_instance.project_id = "proj-1"
            mock_sess_cls.return_value = mock_sess_instance
            new_sessions.append(mock_sess_instance)

            await manager._dequeue_and_start("proj-1")

        # Queue should be empty now
        assert "proj-1" not in manager._session_queue
        # Session was constructed and authenticated
        mock_sess_cls.assert_called_once()
        mock_sess_instance.authenticate.assert_called_once_with("U_OWNER")

    async def test_dequeue_no_queue_is_noop(self):
        """_dequeue_and_start with no queue for the project does nothing."""
        manager, _, _ = _make_manager()
        # No entries for proj-1
        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg_cls.return_value.__aenter__ = AsyncMock()
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            # Should return early without accessing registry
            await manager._dequeue_and_start("proj-1")

        # registry was NOT consulted (early return)
        mock_reg_cls.assert_not_called()

    async def test_dequeue_no_pm_skips(self):
        """If there's no live PM for the project, dequeue is skipped."""
        manager, _, _ = _make_manager()
        manager.queue_session(
            make_options(name="task"),
            project_id="proj-1",
            pm_session_id="pm-gone",
            authenticated_user_id="U_OWNER",
        )
        # No PM in _sessions

        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await manager._dequeue_and_start("proj-1")

        # Entry should remain in queue (not consumed)
        assert "proj-1" in manager._session_queue
        assert len(manager._session_queue["proj-1"]) == 1

    async def test_dequeue_cap_still_reached_skips(self):
        """_dequeue_and_start re-checks cap inside the lock; if still full, skips."""
        manager, _, _ = _make_manager()
        opts = make_options(name="task")
        manager.queue_session(
            opts,
            project_id="proj-1",
            pm_session_id="pm-sess-1",
            authenticated_user_id="U_OWNER",
        )

        pm_stub = _StubSession()
        pm_stub.is_pm = True
        pm_stub.project_id = "proj-1"
        pm_stub._session_id = "pm-sess-1"
        manager._sessions["pm-sess-1"] = pm_stub  # type: ignore[assignment]

        # Return children at the cap
        active_children = [
            {"status": "active", "session_id": f"c{i}"} for i in range(MAX_SPAWN_CHILDREN_PM)
        ]

        with patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls:
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_reg.list_children = AsyncMock(return_value=active_children)

            await manager._dequeue_and_start("proj-1")

        # Entry still in queue — cap was full
        assert "proj-1" in manager._session_queue
        assert len(manager._session_queue["proj-1"]) == 1

    async def test_concurrent_dequeue_no_cap_overshoot(self):
        """Lock serializes concurrent _dequeue_and_start calls for the same project."""
        manager, _, _ = _make_manager()

        # Enqueue 2 sessions for proj-1
        for i in range(2):
            manager.queue_session(
                make_options(name=f"task-{i}"),
                project_id="proj-1",
                pm_session_id="pm-sess-1",
                authenticated_user_id="U_OWNER",
            )

        pm_stub = _StubSession()
        pm_stub.is_pm = True
        pm_stub.project_id = "proj-1"
        pm_stub._session_id = "pm-sess-1"
        manager._sessions["pm-sess-1"] = pm_stub  # type: ignore[assignment]

        started_sessions: list[str] = []

        with (
            patch("summon_claude.sessions.manager.SessionRegistry") as mock_reg_cls,
            patch("summon_claude.sessions.manager.SummonSession") as mock_sess_cls,
        ):
            mock_reg = AsyncMock()
            mock_reg_cls.return_value.__aenter__ = AsyncMock(return_value=mock_reg)
            mock_reg_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            # Return empty list (below cap)
            mock_reg.list_children = AsyncMock(return_value=[])

            call_count = 0

            def make_stub_session(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                stub = MagicMock()
                stub.start = AsyncMock()
                stub.authenticate = MagicMock()
                stub.channel_id = None
                stub.is_pm = False
                stub.project_id = "proj-1"
                started_sessions.append(f"sess-{call_count}")
                return stub

            mock_sess_cls.side_effect = make_stub_session

            # Fire two concurrent dequeues
            await asyncio.gather(
                manager._dequeue_and_start("proj-1"),
                manager._dequeue_and_start("proj-1"),
            )

        # Both entries should have been processed (lock serializes, not blocks entirely)
        assert len(started_sessions) == 2
        assert "proj-1" not in manager._session_queue


# ---------------------------------------------------------------------------
# Tests: _notify_pm_of_dequeue
# ---------------------------------------------------------------------------


class TestNotifyPmOnDequeue:
    async def test_pm_notified_on_dequeue(self):
        """After a session is dequeued, PM channel gets a notification."""
        manager, mock_provider, _ = _make_manager()

        pm_stub = _StubSession()
        pm_stub.is_pm = True
        pm_stub.project_id = "proj-1"
        pm_stub.channel_id = "C_PM_CHAN"
        manager._sessions["pm-sess-1"] = pm_stub  # type: ignore[assignment]

        entry = _QueuedSession(
            options=make_options(name="task-a"),
            project_id="proj-1",
            pm_session_id="pm-sess-1",
            authenticated_user_id="U_OWNER",
        )

        await manager._notify_pm_of_dequeue(entry, "new-sess-abc123")

        mock_provider.chat_postMessage.assert_awaited_once()
        call_kwargs = mock_provider.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C_PM_CHAN"
        text = call_kwargs["text"]
        assert "task-a" in text
        # The format is "{session_id[:8]}..." — verify the 8-char prefix is present
        assert "new-sess" in text  # "new-sess-abc123"[:8] == "new-sess"
        assert "..." in text

    async def test_pm_gone_at_dequeue_no_error(self):
        """If PM is gone at notification time, the call is suppressed (best-effort)."""
        manager, mock_provider, _ = _make_manager()
        # PM not in _sessions

        entry = _QueuedSession(
            options=make_options(name="task-a"),
            project_id="proj-1",
            pm_session_id="pm-gone",
            authenticated_user_id="U_OWNER",
        )

        # Should not raise
        await manager._notify_pm_of_dequeue(entry, "new-sess-123")
        mock_provider.chat_postMessage.assert_not_awaited()

    async def test_pm_no_channel_id_no_notification(self):
        """PM with no channel_id skips the notification."""
        manager, mock_provider, _ = _make_manager()

        pm_stub = _StubSession()
        pm_stub.is_pm = True
        pm_stub.project_id = "proj-1"
        pm_stub.channel_id = None  # no channel yet
        manager._sessions["pm-sess-1"] = pm_stub  # type: ignore[assignment]

        entry = _QueuedSession(
            options=make_options(name="task-a"),
            project_id="proj-1",
            pm_session_id="pm-sess-1",
            authenticated_user_id="U_OWNER",
        )

        await manager._notify_pm_of_dequeue(entry, "new-sess-123")
        mock_provider.chat_postMessage.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: _on_task_done triggers dequeue
# ---------------------------------------------------------------------------


class TestOnTaskDoneTriggersDequeue:
    async def test_task_done_schedules_dequeue_for_project(self):
        """When a non-PM session with a project completes, _dequeue_and_start is scheduled."""
        manager, _, mock_dispatcher = _make_manager()

        # Enqueue a waiting session
        manager.queue_session(
            make_options(name="waiting"),
            project_id="proj-1",
            pm_session_id="pm-1",
            authenticated_user_id="U_OWNER",
        )

        stub = _StubSession()
        stub.is_pm = False
        stub.project_id = "proj-1"
        stub.channel_id = "C001"

        # Put stub in _sessions and simulate task completion
        manager._sessions["child-1"] = stub  # type: ignore[assignment]

        dequeue_called_for: list[str] = []

        async def fake_dequeue(project_id: str) -> None:
            dequeue_called_for.append(project_id)

        manager._dequeue_and_start = fake_dequeue  # type: ignore[method-assign]
        manager._update_pm_topic = AsyncMock()  # type: ignore[method-assign]

        task = asyncio.create_task(asyncio.sleep(0))
        await task  # let it complete

        manager._on_task_done(task, "child-1")
        await asyncio.sleep(0)  # allow background tasks to fire

        assert "proj-1" in dequeue_called_for

    async def test_task_done_no_dequeue_without_queue_entry(self):
        """When no queue entry exists for the project, no dequeue is scheduled."""
        manager, _, _ = _make_manager()

        stub = _StubSession()
        stub.is_pm = False
        stub.project_id = "proj-1"
        stub.channel_id = None
        manager._sessions["child-1"] = stub  # type: ignore[assignment]

        dequeue_called_for: list[str] = []

        async def fake_dequeue(project_id: str) -> None:
            dequeue_called_for.append(project_id)

        manager._dequeue_and_start = fake_dequeue  # type: ignore[method-assign]
        manager._update_pm_topic = AsyncMock()  # type: ignore[method-assign]

        task = asyncio.create_task(asyncio.sleep(0))
        await task

        manager._on_task_done(task, "child-1")
        await asyncio.sleep(0)

        assert dequeue_called_for == []


# ---------------------------------------------------------------------------
# Tests: project isolation
# ---------------------------------------------------------------------------


class TestProjectIsolation:
    async def test_completing_project_a_dequeues_only_a(self):
        """Task done for project A only dequeues A's queue, not B's."""
        manager, _, _ = _make_manager()

        manager.queue_session(
            make_options(name="a-task"),
            project_id="proj-a",
            pm_session_id="pm-a",
            authenticated_user_id="U_OWNER",
        )
        manager.queue_session(
            make_options(name="b-task"),
            project_id="proj-b",
            pm_session_id="pm-b",
            authenticated_user_id="U_OWNER",
        )

        dequeued_projects: list[str] = []

        async def fake_dequeue(project_id: str) -> None:
            dequeued_projects.append(project_id)

        manager._dequeue_and_start = fake_dequeue  # type: ignore[method-assign]
        manager._update_pm_topic = AsyncMock()  # type: ignore[method-assign]

        stub = _StubSession()
        stub.is_pm = False
        stub.project_id = "proj-a"
        stub.channel_id = None
        manager._sessions["child-a"] = stub  # type: ignore[assignment]

        task = asyncio.create_task(asyncio.sleep(0))
        await task
        manager._on_task_done(task, "child-a")
        await asyncio.sleep(0)

        assert dequeued_projects == ["proj-a"]
        # proj-b still in queue, untouched
        assert "proj-b" in manager._session_queue


# ---------------------------------------------------------------------------
# Tests: MCP tool — queued response vs hard error
# ---------------------------------------------------------------------------


class TestMcpQueueBehavior:
    async def test_pm_session_gets_queued_response(self, registry):
        """PM at cap queues the session and returns a 'queued' (non-error) response."""
        import os

        from conftest import make_scheduler

        from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools

        await registry.register(
            session_id="pm-sess-111",
            pid=os.getpid(),
            cwd="/tmp",
            name="pm-session",
            authenticated_user_id="U_OWNER",
        )
        await registry.update_status(
            "pm-sess-111",
            "active",
            slack_channel_id="C100",
            slack_channel_name="summon-pm",
            authenticated_user_id="U_OWNER",
        )

        active_children = [
            {"status": "active", "session_id": f"c{i}", "session_name": f"sess-{i}"}
            for i in range(MAX_SPAWN_CHILDREN_PM)
        ]
        queued_calls: list[tuple] = []

        def fake_queue(options, *, project_id, pm_session_id, authenticated_user_id):
            queued_calls.append((options, project_id, pm_session_id))
            return 1  # position 1

        # Patch list_children and get_session directly on the registry object
        original_list_children = registry.list_children
        original_get_session = registry.get_session

        async def patched_list_children(sid, limit=500):
            return active_children

        async def patched_get_session(sid):
            return {
                "session_id": sid,
                "project_id": "proj-test",
                "authenticated_user_id": "U_OWNER",
            }

        registry.list_children = patched_list_children
        registry.get_session = patched_get_session

        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=registry,
                session_id="pm-sess-111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/tmp",
                scheduler=make_scheduler(),
                is_pm=True,
                project_id="proj-test",
                _ipc_queue_session=fake_queue,
            )
        }

        result = await tools["session_start"].handler({"name": "new-task"})
        registry.list_children = original_list_children
        registry.get_session = original_get_session

        assert not result.get("is_error"), f"Expected queued response, got: {result}"
        text = result["content"][0]["text"]
        assert "queued" in text
        assert len(queued_calls) == 1

    def test_non_pm_session_does_not_get_session_start_tool(self):
        """session_start is a PM-only tool — non-PM sessions don't receive it."""
        from conftest import make_scheduler

        from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools

        # A real registry instance is not needed for this structural check
        mock_reg = MagicMock()
        tool_names = {
            t.name
            for t in create_summon_cli_mcp_tools(
                registry=mock_reg,
                session_id="child-sess-111",
                authenticated_user_id="U_OWNER",
                channel_id="C100",
                cwd="/tmp",
                scheduler=make_scheduler(),
                is_pm=False,
            )
        }
        assert "session_start" not in tool_names

    async def test_mcp_queue_full_returns_error(self, registry):
        """When _ipc_queue_session returns -1 (full), session_start returns an error."""
        import os

        from conftest import make_scheduler

        from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools

        await registry.register(
            session_id="pm-sess-222",
            pid=os.getpid(),
            cwd="/tmp",
            name="pm-session",
            authenticated_user_id="U_OWNER",
        )
        await registry.update_status(
            "pm-sess-222",
            "active",
            slack_channel_id="C200",
            slack_channel_name="summon-pm2",
            authenticated_user_id="U_OWNER",
        )

        active_children = [
            {"status": "active", "session_id": f"c{i}", "session_name": f"s{i}"}
            for i in range(MAX_SPAWN_CHILDREN_PM)
        ]

        def full_queue(options, *, project_id, pm_session_id, authenticated_user_id):
            return -1  # queue is full

        original_list_children = registry.list_children
        original_get_session = registry.get_session

        async def patched_list_children(sid, limit=500):
            return active_children

        async def patched_get_session(sid):
            return {
                "session_id": sid,
                "project_id": "proj-test",
                "authenticated_user_id": "U_OWNER",
            }

        registry.list_children = patched_list_children
        registry.get_session = patched_get_session

        tools = {
            t.name: t
            for t in create_summon_cli_mcp_tools(
                registry=registry,
                session_id="pm-sess-222",
                authenticated_user_id="U_OWNER",
                channel_id="C200",
                cwd="/tmp",
                scheduler=make_scheduler(),
                is_pm=True,
                project_id="proj-test",
                _ipc_queue_session=full_queue,
            )
        }

        result = await tools["session_start"].handler({"name": "overflow-task"})
        registry.list_children = original_list_children
        registry.get_session = original_get_session

        assert result.get("is_error") is True
        assert "queue is full" in result["content"][0]["text"]

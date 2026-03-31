"""Integration tests for cron/task/canvas feature."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from conftest import make_scheduler

from summon_claude.sessions.scheduler import SessionScheduler
from summon_claude.sessions.session import _sync_scheduler_to_canvas, _sync_tasks_to_canvas
from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools

# ---------------------------------------------------------------------------
# Scheduler + session lifecycle integration
# ---------------------------------------------------------------------------


class TestSchedulerSessionLifecycle:
    async def test_pm_session_scheduler_has_internal_scan_job(self):
        """When PM session registers a scan timer, scheduler has an internal job."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)

        # Simulate what session.py does for PM sessions
        interval_min = max(1, 300 // 60)  # 5-minute scan interval
        scan_cron = f"*/{interval_min} * * * *"
        await scheduler.create(
            cron_expr=scan_cron,
            prompt="[SCAN TRIGGER] scan",
            internal=True,
            max_lifetime_s=0,
        )

        jobs = scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].internal is True
        assert jobs[0].max_lifetime_s == 0
        assert "SCAN TRIGGER" in jobs[0].prompt

        scheduler.cancel_all()

    async def test_non_pm_session_starts_with_empty_scheduler(self):
        """Non-PM sessions start with no jobs in the scheduler."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)
        # Don't register any internal jobs (non-PM path)
        assert len(scheduler.list_jobs()) == 0

    async def test_compaction_restart_clears_and_reregisters(self):
        """cancel_all + re-register simulates compaction restart correctly."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)

        # Register internal scan job + agent job
        await scheduler.create("*/5 * * * *", "scan", internal=True, max_lifetime_s=0)
        await scheduler.create("*/10 * * * *", "agent-cron")
        assert len(scheduler.list_jobs()) == 2

        # Simulate compaction restart
        scheduler.cancel_all()
        assert len(scheduler.list_jobs()) == 0  # All cleared

        # Re-register only internal job (memory-only mode — no DB persistence)
        await scheduler.create("*/5 * * * *", "scan", internal=True, max_lifetime_s=0)
        jobs = scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].internal is True

        scheduler.cancel_all()

    async def test_cancel_all_clears_in_memory_jobs(self):
        """cancel_all clears in-memory jobs (DB rows preserved separately)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)

        await scheduler.create("*/5 * * * *", "scan", internal=True, max_lifetime_s=0)
        await scheduler.create("*/10 * * * *", "check CI")
        await scheduler.create("0 9 * * 1-5", "daily standup", recurring=True)

        assert len(scheduler.list_jobs()) == 3
        scheduler.cancel_all()
        assert len(scheduler.list_jobs()) == 0

    async def test_hourly_scan_cron_expression(self):
        """Scan intervals ≥ 60 minutes produce hourly cron expressions."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)

        # Simulate what session.py does for scan_interval_s=7200 (2 hours)
        interval_min = max(1, 7200 // 60)  # 120 minutes
        if interval_min <= 59:
            scan_cron = f"*/{interval_min} * * * *"
        else:
            scan_cron = f"0 */{max(1, interval_min // 60)} * * *"

        assert scan_cron == "0 */2 * * *"

        await scheduler.create(
            cron_expr=scan_cron,
            prompt="[SCAN TRIGGER] scan",
            internal=True,
            max_lifetime_s=0,
        )

        jobs = scheduler.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].cron_expr == "0 */2 * * *"

        scheduler.cancel_all()

    async def test_shutdown_cancels_all_jobs(self):
        """cancel_all stops all asyncio tasks."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)

        job1 = await scheduler.create("*/5 * * * *", "scan", internal=True)
        job2 = await scheduler.create("*/10 * * * *", "agent")
        assert job1.task is not None
        assert job2.task is not None
        assert not job1.task.done()
        assert not job2.task.done()

        scheduler.cancel_all()
        # Give tasks time to process cancellation
        await asyncio.sleep(0.1)

        assert job1.task.done()
        assert job2.task.done()
        assert len(scheduler.list_jobs()) == 0


# ---------------------------------------------------------------------------
# Canvas sync integration
# ---------------------------------------------------------------------------


class TestCanvasSyncIntegration:
    async def test_cron_create_triggers_canvas_sync(self):
        """Scheduler on_change fires when a job is created."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)

        sync_calls: list[str] = []

        async def mock_sync():
            sync_calls.append("synced")

        scheduler.on_change = mock_sync

        await scheduler.create("*/5 * * * *", "test")
        assert len(sync_calls) == 1

        scheduler.cancel_all()

    async def test_task_create_triggers_canvas_sync(self, registry):
        """TaskCreate MCP tool fires on_task_change callback."""
        sync_calls: list[str] = []

        async def mock_sync():
            sync_calls.append("synced")

        await registry.register("int-sid", 1234, "/tmp", authenticated_user_id="U_INT")
        tools = create_summon_cli_mcp_tools(
            registry=registry,
            session_id="int-sid",
            authenticated_user_id="U_INT",
            channel_id="C_INT",
            cwd="/tmp",
            scheduler=make_scheduler(),
            on_task_change=mock_sync,
        )
        create_tool = next(t for t in tools if t.name == "TaskCreate")
        await create_tool.handler({"content": "test task", "priority": "medium"})
        assert len(sync_calls) == 1

    async def test_no_canvas_no_crash(self):
        """Scheduler with no on_change callback doesn't error."""
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        ev = asyncio.Event()
        scheduler = SessionScheduler(q, ev)
        # on_change is None by default
        await scheduler.create("*/5 * * * *", "test")  # Should not crash
        await scheduler.delete(scheduler.list_jobs()[0].id)  # Should not crash


# ---------------------------------------------------------------------------
# Canvas sync render tests (direct unit tests for _sync_* functions)
# ---------------------------------------------------------------------------


class TestSyncSchedulerToCanvas:
    async def test_empty_jobs_renders_placeholder(self):
        scheduler = SessionScheduler(asyncio.Queue(maxsize=100), asyncio.Event())
        mock_canvas = AsyncMock()
        await _sync_scheduler_to_canvas(scheduler, mock_canvas)
        mock_canvas.update_section.assert_awaited_once_with(
            "Scheduled Jobs", "_No scheduled jobs._"
        )

    async def test_jobs_render_table(self):
        scheduler = SessionScheduler(asyncio.Queue(maxsize=100), asyncio.Event())
        await scheduler.create("*/5 * * * *", "check things")
        mock_canvas = AsyncMock()
        await _sync_scheduler_to_canvas(scheduler, mock_canvas)
        call_args = mock_canvas.update_section.call_args
        assert call_args[0][0] == "Scheduled Jobs"
        markdown = call_args[0][1]
        assert "| ID |" in markdown
        assert "Agent" in markdown
        assert "check things" in markdown

    async def test_internal_job_shows_system_label(self):
        scheduler = SessionScheduler(asyncio.Queue(maxsize=100), asyncio.Event())
        await scheduler.create("*/5 * * * *", "scan", internal=True)
        mock_canvas = AsyncMock()
        await _sync_scheduler_to_canvas(scheduler, mock_canvas)
        markdown = mock_canvas.update_section.call_args[0][1]
        assert "System" in markdown
        assert "Project scan timer" in markdown


class TestSyncTasksToCanvas:
    async def test_empty_tasks_renders_placeholder(self, registry):
        await registry.register("canvas-empty", 1234, "/tmp")
        mock_canvas = AsyncMock()
        await _sync_tasks_to_canvas(registry, "canvas-empty", mock_canvas, "Tasks")
        mock_canvas.update_section.assert_awaited_once_with("Tasks", "_No tasks tracked._")

    async def test_pm_heading_renders_work_items_placeholder(self, registry):
        await registry.register("canvas-pm-empty", 1234, "/tmp")
        mock_canvas = AsyncMock()
        await _sync_tasks_to_canvas(registry, "canvas-pm-empty", mock_canvas, "Work Items")
        mock_canvas.update_section.assert_awaited_once_with(
            "Work Items", "_No work items tracked._"
        )

    async def test_completed_tasks_have_strikethrough(self, registry):
        await registry.register("canvas-done", 1234, "/tmp")
        await registry.create_task("canvas-done", "t1", "Active task")
        await registry.create_task("canvas-done", "t2", "Done task")
        await registry.update_task("canvas-done", "t2", status="completed")
        mock_canvas = AsyncMock()
        await _sync_tasks_to_canvas(registry, "canvas-done", mock_canvas, "Tasks")
        markdown = mock_canvas.update_section.call_args[0][1]
        assert "Active task" in markdown
        assert "~~completed~~" in markdown
        assert "~~Done task~~" in markdown

    async def test_active_tasks_before_completed(self, registry):
        await registry.register("canvas-order", 1234, "/tmp")
        await registry.create_task("canvas-order", "t1", "First")
        await registry.create_task("canvas-order", "t2", "Second")
        await registry.update_task("canvas-order", "t1", status="completed")
        mock_canvas = AsyncMock()
        await _sync_tasks_to_canvas(registry, "canvas-order", mock_canvas, "Tasks")
        markdown = mock_canvas.update_section.call_args[0][1]
        # Active (Second) should appear before completed (First)
        second_pos = markdown.find("Second")
        first_pos = markdown.find("~~First~~")
        assert second_pos < first_pos


# ---------------------------------------------------------------------------
# Tool availability gating
# ---------------------------------------------------------------------------


class TestToolAvailabilityGating:
    async def test_pm_gets_session_start_stop(self, registry):
        """PM sessions get session_start and session_stop tools."""
        tools = create_summon_cli_mcp_tools(
            registry=registry,
            session_id="sid",
            authenticated_user_id="U",
            channel_id="C",
            cwd="/tmp",
            scheduler=make_scheduler(),
            is_pm=True,
        )
        names = {t.name for t in tools}
        assert "session_start" in names
        assert "session_stop" in names
        assert "session_log_status" in names

    async def test_non_pm_excludes_session_start_stop(self, registry):
        """Non-PM sessions do NOT get session_start, session_stop, or session_log_status."""
        tools = create_summon_cli_mcp_tools(
            registry=registry,
            session_id="sid",
            authenticated_user_id="U",
            channel_id="C",
            cwd="/tmp",
            scheduler=make_scheduler(),
            is_pm=False,
        )
        names = {t.name for t in tools}
        assert "session_start" not in names
        assert "session_stop" not in names
        assert "session_log_status" not in names

    async def test_non_pm_gets_common_tools(self, registry):
        """Non-PM sessions get session_list, session_info, task tools."""
        tools = create_summon_cli_mcp_tools(
            registry=registry,
            session_id="sid",
            authenticated_user_id="U",
            channel_id="C",
            cwd="/tmp",
            scheduler=make_scheduler(),
            is_pm=False,
        )
        names = {t.name for t in tools}
        assert "session_list" in names
        assert "session_info" in names
        assert "TaskCreate" in names
        assert "TaskUpdate" in names
        assert "TaskList" in names

    async def test_cron_tools_present(self, registry):
        """Cron tools are always present (scheduler is required)."""
        scheduler = make_scheduler()
        tools = create_summon_cli_mcp_tools(
            registry=registry,
            session_id="sid",
            authenticated_user_id="U",
            channel_id="C",
            cwd="/tmp",
            scheduler=scheduler,
        )
        names = {t.name for t in tools}
        assert "CronCreate" in names
        assert "CronDelete" in names
        assert "CronList" in names


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


class TestGuardTests:
    async def test_session_tasks_columns(self, registry):
        """Pin session_tasks table schema via PRAGMA."""
        db = registry._check_connected()
        async with db.execute("PRAGMA table_info(session_tasks)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        assert columns == {
            "id",
            "session_id",
            "content",
            "status",
            "priority",
            "created_at",
            "updated_at",
        }

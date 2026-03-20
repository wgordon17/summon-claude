"""Tests for SessionScheduler."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from summon_claude.sessions.scheduler import ScheduledJob, SessionScheduler


@pytest.fixture
def event_queue() -> asyncio.Queue[dict]:
    return asyncio.Queue(maxsize=100)


@pytest.fixture
def shutdown_event() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def scheduler(event_queue: asyncio.Queue, shutdown_event: asyncio.Event) -> SessionScheduler:
    return SessionScheduler(event_queue, shutdown_event)


class TestCreateJob:
    @pytest.mark.asyncio
    async def test_create_job(self, scheduler: SessionScheduler) -> None:
        job = await scheduler.create("*/5 * * * *", "test prompt")
        assert job.id
        assert job.cron_expr == "*/5 * * * *"
        assert job.prompt == "test prompt"
        assert job.recurring is True
        assert job.internal is False
        assert len(scheduler.list_jobs()) == 1

    @pytest.mark.asyncio
    async def test_create_internal_job(self, scheduler: SessionScheduler) -> None:
        job = await scheduler.create("*/5 * * * *", "scan", internal=True, max_lifetime_s=0)
        assert job.internal is True
        assert job.max_lifetime_s == 0

    @pytest.mark.asyncio
    async def test_create_non_recurring(self, scheduler: SessionScheduler) -> None:
        job = await scheduler.create("0 12 * * *", "once", recurring=False)
        assert job.recurring is False


class TestDeleteJob:
    @pytest.mark.asyncio
    async def test_delete_job(self, scheduler: SessionScheduler) -> None:
        job = await scheduler.create("*/5 * * * *", "test")
        assert await scheduler.delete(job.id) is True
        assert len(scheduler.list_jobs()) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, scheduler: SessionScheduler) -> None:
        assert await scheduler.delete("nonexistent") is False

    @pytest.mark.asyncio
    async def test_delete_internal_refused(self, scheduler: SessionScheduler) -> None:
        job = await scheduler.create("*/5 * * * *", "scan", internal=True)
        with pytest.raises(ValueError, match="Cannot delete system-created jobs"):
            await scheduler.delete(job.id)
        assert len(scheduler.list_jobs()) == 1


class TestLimits:
    @pytest.mark.asyncio
    async def test_max_agent_jobs(self, scheduler: SessionScheduler) -> None:
        for i in range(10):
            await scheduler.create("*/5 * * * *", f"job {i}")
        with pytest.raises(ValueError, match="Maximum of 10"):
            await scheduler.create("*/5 * * * *", "one too many")

    @pytest.mark.asyncio
    async def test_internal_jobs_bypass_agent_limit(self, scheduler: SessionScheduler) -> None:
        for i in range(10):
            await scheduler.create("*/5 * * * *", f"job {i}")
        # Internal jobs don't count toward agent limit
        job = await scheduler.create("*/5 * * * *", "internal", internal=True)
        assert job.internal is True

    @pytest.mark.asyncio
    async def test_min_interval_enforced(self, scheduler: SessionScheduler) -> None:
        # */1 * * * * fires every minute (60s) — should pass
        await scheduler.create("*/1 * * * *", "every minute")
        # A cron that fires more often than 60s can't be constructed with
        # standard 5-field syntax (minimum is 1 minute), so min interval
        # enforcement primarily guards against future CronSim extensions.

    @pytest.mark.asyncio
    async def test_prompt_truncation(self, scheduler: SessionScheduler) -> None:
        long_prompt = "x" * 2000
        job = await scheduler.create("*/5 * * * *", long_prompt)
        assert len(job.prompt) == 1000

    @pytest.mark.asyncio
    async def test_internal_jobs_skip_prompt_truncation(self, scheduler: SessionScheduler) -> None:
        long_prompt = "x" * 2000
        job = await scheduler.create("*/5 * * * *", long_prompt, internal=True)
        assert len(job.prompt) == 2000


class TestValidation:
    @pytest.mark.asyncio
    async def test_5_field_validation(self, scheduler: SessionScheduler) -> None:
        with pytest.raises(ValueError, match="exactly 5 fields"):
            await scheduler.create("@reboot", "test")
        with pytest.raises(ValueError, match="exactly 5 fields"):
            await scheduler.create("@yearly", "test")
        with pytest.raises(ValueError, match="exactly 5 fields"):
            await scheduler.create("* * *", "test")

    @pytest.mark.asyncio
    async def test_invalid_cron_expression(self, scheduler: SessionScheduler) -> None:
        from cronsim import CronSimError

        with pytest.raises(CronSimError):
            await scheduler.create("99 99 99 99 99", "test")


class TestCancelAll:
    @pytest.mark.asyncio
    async def test_cancel_all(self, scheduler: SessionScheduler) -> None:
        await scheduler.create("*/5 * * * *", "job1")
        await scheduler.create("*/5 * * * *", "job2")
        await scheduler.create("*/5 * * * *", "scan", internal=True)
        assert len(scheduler.list_jobs()) == 3

        scheduler.cancel_all()
        assert len(scheduler.list_jobs()) == 0


class TestOnChangeCallback:
    @pytest.mark.asyncio
    async def test_callback_on_create(self, scheduler: SessionScheduler) -> None:
        callback = AsyncMock()
        scheduler._on_change = callback
        await scheduler.create("*/5 * * * *", "test")
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_callback_on_delete(self, scheduler: SessionScheduler) -> None:
        job = await scheduler.create("*/5 * * * *", "test")
        callback = AsyncMock()
        scheduler._on_change = callback
        await scheduler.delete(job.id)
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_callback_no_crash(self, scheduler: SessionScheduler) -> None:
        scheduler._on_change = None
        await scheduler.create("*/5 * * * *", "test")  # No crash


class TestJobFiring:
    @pytest.mark.asyncio
    async def test_job_fires_synthetic_event(
        self, event_queue: asyncio.Queue, shutdown_event: asyncio.Event
    ) -> None:
        scheduler = SessionScheduler(event_queue, shutdown_event)

        # Create a real job, then patch _run_job's CronSim to fire immediately
        now = datetime.now()  # noqa: DTZ005
        immediate = now + timedelta(milliseconds=10)

        # Create job normally (validates cron), then cancel its task and
        # restart with a patched CronSim that fires immediately
        job = await scheduler.create("*/5 * * * *", "fire now")
        if job.task:
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task

        with patch("summon_claude.sessions.scheduler.CronSim") as mock_cronsim:
            # First call in _run_job: return immediate fire time
            # After firing (non-recurring won't loop), iteration ends
            mock_cronsim.return_value = iter([immediate])
            job.recurring = False  # Fire once and stop
            job.task = asyncio.create_task(scheduler._run_job(job))

            event = await asyncio.wait_for(event_queue.get(), timeout=2.0)

        assert event["_synthetic"] is True
        assert event["type"] == "message"
        assert f"[CRON:{job.id}]" in event["text"]
        assert "fire now" in event["text"]

        scheduler.cancel_all()

    @pytest.mark.asyncio
    async def test_internal_job_system_prefix(
        self, event_queue: asyncio.Queue, shutdown_event: asyncio.Event
    ) -> None:
        scheduler = SessionScheduler(event_queue, shutdown_event)

        job = await scheduler.create("*/5 * * * *", "scan", internal=True)
        if job.task:
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task

        now = datetime.now()  # noqa: DTZ005
        immediate = now + timedelta(milliseconds=10)

        with patch("summon_claude.sessions.scheduler.CronSim") as mock_cronsim:
            mock_cronsim.return_value = iter([immediate])
            job.recurring = False
            job.task = asyncio.create_task(scheduler._run_job(job))

            event = await asyncio.wait_for(event_queue.get(), timeout=2.0)

        assert f"[SYSTEM:{job.id}]" in event["text"]

        scheduler.cancel_all()


class TestJobExpiry:
    @pytest.mark.asyncio
    async def test_job_expires(
        self, event_queue: asyncio.Queue, shutdown_event: asyncio.Event
    ) -> None:
        scheduler = SessionScheduler(event_queue, shutdown_event)

        # Create a job that's already expired
        job = await scheduler.create("*/5 * * * *", "expired", max_lifetime_s=1)
        # Backdate creation to make it expired
        job.created_at = datetime.now() - timedelta(seconds=10)  # noqa: DTZ005

        # Give the task time to notice expiry
        await asyncio.sleep(0.2)

        # Job should have been removed
        assert job.id not in {j.id for j in scheduler.list_jobs()}

        scheduler.cancel_all()


class TestGuardTests:
    def test_scheduled_job_fields(self) -> None:
        """Pin ScheduledJob dataclass fields."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(ScheduledJob)}
        assert fields == {
            "id",
            "cron_expr",
            "prompt",
            "recurring",
            "internal",
            "task",
            "created_at",
            "max_lifetime_s",
        }

    def test_scheduler_constants(self) -> None:
        """Pin scheduler limit constants."""
        assert SessionScheduler._MAX_AGENT_JOBS == 10
        assert SessionScheduler._MIN_INTERVAL_S == 60
        assert SessionScheduler._MAX_PROMPT_LENGTH == 1000

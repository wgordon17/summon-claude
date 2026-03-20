"""Tests for CronCreate/CronDelete/CronList MCP tools."""

from __future__ import annotations

import asyncio

import pytest

from summon_claude.sessions.scheduler import SessionScheduler
from summon_claude.summon_cli_mcp import create_summon_cli_mcp_tools


@pytest.fixture
def scheduler() -> SessionScheduler:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    ev = asyncio.Event()
    return SessionScheduler(q, ev)


@pytest.fixture
def tools(registry, scheduler):
    return {
        t.name: t
        for t in create_summon_cli_mcp_tools(
            registry=registry,
            session_id="test-sid",
            authenticated_user_id="U_TEST",
            channel_id="C_TEST",
            cwd="/tmp",
            is_pm=True,
            scheduler=scheduler,
        )
    }


class TestCronCreate:
    async def test_returns_job_id(self, tools):
        result = await tools["CronCreate"].handler(
            {"cron": "*/5 * * * *", "prompt": "test", "recurring": True}
        )
        assert not result.get("is_error")
        text = result["content"][0]["text"]
        assert "Created job" in text

    async def test_invalid_expression(self, tools):
        result = await tools["CronCreate"].handler(
            {"cron": "@reboot", "prompt": "test", "recurring": True}
        )
        assert result.get("is_error") is True
        assert "5 fields" in result["content"][0]["text"]

    async def test_max_jobs_enforced(self, tools, scheduler):
        for i in range(10):
            await scheduler.create("*/5 * * * *", f"job-{i}")
        result = await tools["CronCreate"].handler(
            {"cron": "*/5 * * * *", "prompt": "one-too-many", "recurring": True}
        )
        assert result.get("is_error") is True
        assert "Maximum" in result["content"][0]["text"]


class TestCronDelete:
    async def test_removes_job(self, tools, scheduler):
        job = await scheduler.create("*/5 * * * *", "to-delete")
        result = await tools["CronDelete"].handler({"id": job.id})
        assert not result.get("is_error")
        assert "cancelled" in result["content"][0]["text"]

    async def test_internal_refused(self, tools, scheduler):
        job = await scheduler.create("*/5 * * * *", "scan", internal=True)
        result = await tools["CronDelete"].handler({"id": job.id})
        assert result.get("is_error") is True
        assert "system" in result["content"][0]["text"].lower()

    async def test_not_found(self, tools):
        result = await tools["CronDelete"].handler({"id": "nonexistent"})
        assert result.get("is_error") is True


class TestCronList:
    async def test_empty(self, tools):
        result = await tools["CronList"].handler({})
        assert not result.get("is_error")
        assert "No scheduled jobs" in result["content"][0]["text"]

    async def test_shows_all_jobs(self, tools, scheduler):
        await scheduler.create("*/5 * * * *", "agent-job")
        await scheduler.create("*/10 * * * *", "scan", internal=True)
        result = await tools["CronList"].handler({})
        text = result["content"][0]["text"]
        assert "Agent" in text
        assert "System" in text
        assert "Next Fire" in text

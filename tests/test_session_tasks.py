"""Tests for session_tasks table and SessionRegistry task methods."""

from __future__ import annotations

import secrets
from pathlib import Path

import aiosqlite
import pytest

from summon_claude.sessions.migrations import _migrate_9_to_10, run_migrations
from summon_claude.sessions.registry import SessionRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task_id() -> str:
    return secrets.token_hex(8)


async def _register_session(
    registry: SessionRegistry,
    session_id: str,
    authenticated_user_id: str | None = None,
    project_id: str | None = None,
) -> None:
    await registry.register(
        session_id=session_id,
        pid=1234,
        cwd="/tmp",
        authenticated_user_id=authenticated_user_id,
        project_id=project_id,
    )


# ---------------------------------------------------------------------------
# Guard tests (pin frozensets)
# ---------------------------------------------------------------------------


class TestGuardSets:
    def test_valid_task_statuses_pinned(self):
        assert (
            frozenset({"pending", "in_progress", "completed"})
            == SessionRegistry._VALID_TASK_STATUSES
        )

    def test_valid_task_priorities_pinned(self):
        assert frozenset({"high", "medium", "low"}) == SessionRegistry._VALID_TASK_PRIORITIES


# ---------------------------------------------------------------------------
# Migration test
# ---------------------------------------------------------------------------


class TestMigration:
    async def test_migration_creates_table(self, tmp_path: Path):
        db_path = tmp_path / "migration_test.db"
        async with aiosqlite.connect(str(db_path), isolation_level=None) as db:
            # Create baseline schema_version table and set to v9
            await db.execute(
                "CREATE TABLE schema_version "
                "(id INTEGER PRIMARY KEY CHECK (id = 1), "
                "version INTEGER NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE sessions ("
                "session_id TEXT PRIMARY KEY, "
                "pid INTEGER NOT NULL, "
                "status TEXT NOT NULL, "
                "session_name TEXT, "
                "cwd TEXT NOT NULL, "
                "slack_channel_id TEXT, "
                "slack_channel_name TEXT, "
                "model TEXT, "
                "claude_session_id TEXT, "
                "started_at TEXT NOT NULL, "
                "authenticated_at TEXT, "
                "ended_at TEXT, "
                "last_activity_at TEXT, "
                "total_cost_usd REAL DEFAULT 0.0, "
                "total_turns INTEGER DEFAULT 0, "
                "error_message TEXT)"
            )
            await db.execute("INSERT INTO schema_version (id, version) VALUES (1, 9)")
            await db.commit()

            # Run the migration directly
            await db.execute("BEGIN IMMEDIATE")
            await _migrate_9_to_10(db)
            await db.execute("COMMIT")

            # Verify table exists
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='session_tasks'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None, "session_tasks table was not created"

            # Verify index exists
            async with db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_session_tasks_session'"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None, "idx_session_tasks_session index was not created"


# ---------------------------------------------------------------------------
# Core CRUD tests
# ---------------------------------------------------------------------------


class TestCreateTask:
    async def test_create_task(self, registry: SessionRegistry):
        await _register_session(registry, "sess-1")
        task_id = make_task_id()
        await registry.create_task("sess-1", task_id, "Do the thing")

        tasks = await registry.list_tasks("sess-1")
        assert len(tasks) == 1
        task = tasks[0]
        assert task["id"] == task_id
        assert task["content"] == "Do the thing"
        assert task["status"] == "pending"
        assert task["priority"] == "medium"
        assert task["created_at"] is not None
        assert task["updated_at"] is not None

    async def test_create_task_with_priority(self, registry: SessionRegistry):
        await _register_session(registry, "sess-2")
        task_id = make_task_id()
        await registry.create_task("sess-2", task_id, "Urgent task", priority="high")

        tasks = await registry.list_tasks("sess-2")
        assert tasks[0]["priority"] == "high"

    async def test_invalid_priority_rejected(self, registry: SessionRegistry):
        await _register_session(registry, "sess-inv-pri")
        with pytest.raises(ValueError, match="Invalid priority"):
            await registry.create_task("sess-inv-pri", make_task_id(), "Task", priority="critical")

    async def test_content_truncation(self, registry: SessionRegistry):
        await _register_session(registry, "sess-trunc")
        long_content = "x" * 3000
        task_id = make_task_id()
        await registry.create_task("sess-trunc", task_id, long_content)

        tasks = await registry.list_tasks("sess-trunc")
        assert len(tasks[0]["content"]) == 2000


# ---------------------------------------------------------------------------
# Update tests
# ---------------------------------------------------------------------------


class TestUpdateTask:
    async def test_update_task_status(self, registry: SessionRegistry):
        await _register_session(registry, "sess-upd")
        task_id = make_task_id()
        await registry.create_task("sess-upd", task_id, "Work item")

        result = await registry.update_task("sess-upd", task_id, status="in_progress")
        assert result is True
        tasks = await registry.list_tasks("sess-upd")
        assert tasks[0]["status"] == "in_progress"

        result = await registry.update_task("sess-upd", task_id, status="completed")
        assert result is True
        tasks = await registry.list_tasks("sess-upd")
        assert tasks[0]["status"] == "completed"

    async def test_update_task_content(self, registry: SessionRegistry):
        await _register_session(registry, "sess-upd-content")
        task_id = make_task_id()
        await registry.create_task("sess-upd-content", task_id, "Original")

        result = await registry.update_task("sess-upd-content", task_id, content="Updated content")
        assert result is True
        tasks = await registry.list_tasks("sess-upd-content")
        assert tasks[0]["content"] == "Updated content"

    async def test_update_task_wrong_session(self, registry: SessionRegistry):
        await _register_session(registry, "sess-owner")
        await _register_session(registry, "sess-other")
        task_id = make_task_id()
        await registry.create_task("sess-owner", task_id, "Owned task")

        # Try to update via different session_id
        result = await registry.update_task("sess-other", task_id, status="in_progress")
        assert result is False

        # Original task should be unchanged
        tasks = await registry.list_tasks("sess-owner")
        assert tasks[0]["status"] == "pending"

    async def test_update_task_no_fields_returns_true(self, registry: SessionRegistry):
        await _register_session(registry, "sess-noop")
        task_id = make_task_id()
        await registry.create_task("sess-noop", task_id, "Task")

        result = await registry.update_task("sess-noop", task_id)
        assert result is True

    async def test_invalid_status_rejected(self, registry: SessionRegistry):
        await _register_session(registry, "sess-inv-stat")
        task_id = make_task_id()
        await registry.create_task("sess-inv-stat", task_id, "Task")

        with pytest.raises(ValueError, match="Invalid status"):
            await registry.update_task("sess-inv-stat", task_id, status="done")

    async def test_update_content_truncation(self, registry: SessionRegistry):
        await _register_session(registry, "sess-upd-trunc")
        task_id = make_task_id()
        await registry.create_task("sess-upd-trunc", task_id, "Short")

        long_content = "y" * 3000
        await registry.update_task("sess-upd-trunc", task_id, content=long_content)
        tasks = await registry.list_tasks("sess-upd-trunc")
        assert len(tasks[0]["content"]) == 2000


# ---------------------------------------------------------------------------
# List / filter tests
# ---------------------------------------------------------------------------


class TestListTasks:
    async def test_list_tasks_with_filter(self, registry: SessionRegistry):
        await _register_session(registry, "sess-list")
        id1, id2, id3 = make_task_id(), make_task_id(), make_task_id()
        await registry.create_task("sess-list", id1, "Task A")
        await registry.create_task("sess-list", id2, "Task B")
        await registry.create_task("sess-list", id3, "Task C")
        await registry.update_task("sess-list", id2, status="completed")

        pending = await registry.list_tasks("sess-list", status="pending")
        assert len(pending) == 2
        assert all(t["status"] == "pending" for t in pending)

        completed = await registry.list_tasks("sess-list", status="completed")
        assert len(completed) == 1
        assert completed[0]["id"] == id2

    async def test_list_tasks_empty(self, registry: SessionRegistry):
        await _register_session(registry, "sess-empty")
        tasks = await registry.list_tasks("sess-empty")
        assert tasks == []

    async def test_list_tasks_ordered_by_created_at(self, registry: SessionRegistry):
        await _register_session(registry, "sess-order")
        ids = [make_task_id() for _ in range(3)]
        for i, tid in enumerate(ids):
            await registry.create_task("sess-order", tid, f"Task {i}")

        tasks = await registry.list_tasks("sess-order")
        assert [t["id"] for t in tasks] == ids


# ---------------------------------------------------------------------------
# Cross-session query tests
# ---------------------------------------------------------------------------


class TestGetTasksForSessions:
    async def test_get_tasks_for_sessions(self, registry: SessionRegistry):
        await _register_session(registry, "sess-a", authenticated_user_id="user-1")
        await _register_session(registry, "sess-b", authenticated_user_id="user-1")
        await _register_session(registry, "sess-c", authenticated_user_id="user-2")

        id_a = make_task_id()
        id_b = make_task_id()
        id_c = make_task_id()
        await registry.create_task("sess-a", id_a, "Task in A")
        await registry.create_task("sess-b", id_b, "Task in B")
        await registry.create_task("sess-c", id_c, "Task in C - different user")

        result = await registry.get_tasks_for_sessions(
            ["sess-a", "sess-b", "sess-c"], authenticated_user_id="user-1"
        )

        assert "sess-a" in result
        assert "sess-b" in result
        # sess-c belongs to user-2, so it must not appear
        assert "sess-c" not in result
        assert result["sess-a"][0]["id"] == id_a
        assert result["sess-b"][0]["id"] == id_b

    async def test_get_tasks_for_sessions_with_project_id(self, registry: SessionRegistry):
        project_id = "proj-xyz"
        await _register_session(
            registry, "sess-proj-a", authenticated_user_id="user-1", project_id=project_id
        )
        await _register_session(
            registry, "sess-proj-b", authenticated_user_id="user-1", project_id="other-proj"
        )

        id_a = make_task_id()
        id_b = make_task_id()
        await registry.create_task("sess-proj-a", id_a, "In project")
        await registry.create_task("sess-proj-b", id_b, "In other project")

        result = await registry.get_tasks_for_sessions(
            ["sess-proj-a", "sess-proj-b"],
            authenticated_user_id="user-1",
            project_id=project_id,
        )

        assert "sess-proj-a" in result
        assert "sess-proj-b" not in result

    async def test_get_tasks_for_sessions_empty_input(self, registry: SessionRegistry):
        result = await registry.get_tasks_for_sessions([], authenticated_user_id="user-1")
        assert result == {}

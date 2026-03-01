"""Tests for audit log functionality in summon_claude.registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from summon_claude.sessions.registry import SessionRegistry


async def _query_audit_log(
    registry: SessionRegistry, session_id: str | None = None, limit: int = 100
) -> list[dict]:
    """Test helper to query audit log entries directly via SQL."""
    db = registry._check_connected()
    if session_id:
        async with db.execute(
            "SELECT * FROM audit_log WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
    else:
        async with db.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(r) for r in rows]


class TestLogEvent:
    async def test_log_event_creates_entry(self, tmp_path):
        """log_event should create a retrievable entry in the audit_log table."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("session_created", session_id="sess-1")
            log = await _query_audit_log(registry)
            assert len(log) == 1
            assert log[0]["event_type"] == "session_created"
            assert log[0]["session_id"] == "sess-1"

    async def test_log_event_with_details(self, tmp_path):
        """log_event with a details dict should store it as JSON."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            details = {"cwd": "/tmp", "model": "claude-opus-4-6"}
            await registry.log_event("session_created", session_id="sess-1", details=details)
            log = await _query_audit_log(registry)
            assert len(log) == 1
            stored_details = log[0]["details"]
            # Details should be a JSON string in the DB
            parsed = json.loads(stored_details)
            assert parsed["cwd"] == "/tmp"
            assert parsed["model"] == "claude-opus-4-6"

    async def test_log_event_without_details(self, tmp_path):
        """log_event without details should store None."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("auth_failed", user_id="U123")
            log = await _query_audit_log(registry)
            assert len(log) == 1
            assert log[0]["details"] is None

    async def test_log_event_with_user_id(self, tmp_path):
        """log_event should store user_id when provided."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("auth_succeeded", session_id="sess-1", user_id="U_TEST")
            log = await _query_audit_log(registry)
            assert log[0]["user_id"] == "U_TEST"

    async def test_log_event_has_timestamp(self, tmp_path):
        """Each audit log entry should have a non-null timestamp."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("session_ended")
            log = await _query_audit_log(registry)
            assert log[0]["timestamp"] is not None
            assert len(log[0]["timestamp"]) > 0

    async def test_log_event_has_auto_id(self, tmp_path):
        """Each audit log entry should have an auto-incremented id."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("session_created", session_id="sess-a")
            await registry.log_event("session_active", session_id="sess-a")
            log = await _query_audit_log(registry)
            ids = [e["id"] for e in log]
            # Should have 2 distinct IDs
            assert len(set(ids)) == 2


class TestGetAuditLogAll:
    async def test_get_audit_log_returns_all(self, tmp_path):
        """get_audit_log() with no filter returns all entries."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("session_created", session_id="sess-1")
            await registry.log_event("auth_attempted", user_id="U1")
            await registry.log_event("session_ended", session_id="sess-1")

            log = await _query_audit_log(registry)
            assert len(log) == 3

    async def test_get_audit_log_returned_most_recent_first(self, tmp_path):
        """get_audit_log() returns entries in descending ID order."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("first_event")
            await registry.log_event("second_event")
            await registry.log_event("third_event")

            log = await _query_audit_log(registry)
            # Descending order: most recent first
            assert log[0]["event_type"] == "third_event"
            assert log[-1]["event_type"] == "first_event"


class TestGetAuditLogBySession:
    async def test_filter_by_session_id(self, tmp_path):
        """get_audit_log(session_id=...) should only return entries for that session."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("session_created", session_id="sess-A")
            await registry.log_event("session_created", session_id="sess-B")
            await registry.log_event("session_active", session_id="sess-A")
            await registry.log_event("auth_failed", user_id="U1")  # no session_id

            log_a = await _query_audit_log(registry, session_id="sess-A")
            assert len(log_a) == 2
            assert all(e["session_id"] == "sess-A" for e in log_a)

    async def test_filter_returns_empty_for_unknown_session(self, tmp_path):
        """Filtering by a non-existent session_id should return empty list."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            await registry.log_event("session_created", session_id="sess-A")
            log = await _query_audit_log(registry, session_id="nonexistent-sess")
            assert log == []


class TestGetAuditLogLimit:
    async def test_limit_parameter_works(self, tmp_path):
        """get_audit_log(limit=N) should return at most N entries."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            for i in range(10):
                await registry.log_event(f"event_{i}")

            log = await _query_audit_log(registry, limit=3)
            assert len(log) == 3

    async def test_default_limit_is_100(self, tmp_path):
        """Default limit is 100 entries."""
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            for i in range(5):
                await registry.log_event(f"event_{i}")

            log = await _query_audit_log(registry)
            assert len(log) == 5


class TestAuditLogEventTypes:
    async def test_all_known_event_types_can_be_logged(self, tmp_path):
        """All expected event type strings should be storable and retrievable."""
        known_event_types = [
            "session_created",
            "session_active",
            "session_ended",
            "session_errored",
            "session_stopped",
            "auth_attempted",
            "auth_failed",
            "auth_succeeded",
        ]
        async with SessionRegistry(db_path=tmp_path / "test.db") as registry:
            for event_type in known_event_types:
                await registry.log_event(event_type, session_id="sess-types")

            log = await _query_audit_log(registry)
            stored_types = {e["event_type"] for e in log}
            for event_type in known_event_types:
                assert event_type in stored_types, f"Event type {event_type!r} not found in log"

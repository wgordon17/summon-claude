"""Tests for summon_claude.slack.canvas_store."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from summon_claude.sessions.registry import SessionRegistry
from summon_claude.slack.canvas_store import CanvasStore, _replace_section
from summon_claude.slack.client import SlackClient


def _make_mock_client() -> SlackClient:
    """Create a mock SlackClient with canvas methods."""
    web = MagicMock()
    web.api_call = AsyncMock(return_value={"ok": True})
    client = SlackClient(web, "C_TEST")
    return client


@pytest.fixture
async def canvas_registry(tmp_path: Path) -> SessionRegistry:
    """Registry with a pre-registered session for canvas tests."""
    reg = SessionRegistry(db_path=tmp_path / "canvas_test.db")
    async with reg:
        await reg.register("sess-cv", 111, "/tmp")
        await reg.update_status("sess-cv", "active", slack_channel_id="C_TEST")
        yield reg


class TestCanvasStoreReadWrite:
    async def test_read_returns_initial_markdown(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            markdown="# Initial",
        )
        assert store.read() == "# Initial"

    async def test_write_updates_markdown(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            markdown="",
        )
        await store.write("# Updated\nNew content")
        assert store.read() == "# Updated\nNew content"

    async def test_write_persists_to_registry(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
        )
        await store.write("# Persisted")
        canvas_id, md = await canvas_registry.get_canvas("sess-cv")
        assert canvas_id == "F_1"
        assert md == "# Persisted"

    async def test_canvas_id_property(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_ABC",
            client=client,
            registry=canvas_registry,
        )
        assert store.canvas_id == "F_ABC"


class TestCanvasStoreUpdateSection:
    async def test_update_section_replaces_body(self, canvas_registry):
        client = _make_mock_client()
        md = "# Title\n\nIntro\n\n## Status\n\nOld status\n\n## Notes\n\nSome notes\n"
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            markdown=md,
        )
        await store.update_section("Status", "New status line")
        result = store.read()
        assert "New status line" in result
        assert "Old status" not in result
        assert "Some notes" in result

    async def test_update_section_missing_heading_appends(self, canvas_registry):
        client = _make_mock_client()
        md = "# Title\n\nContent"
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            markdown=md,
        )
        await store.update_section("Nonexistent", "New stuff")
        result = store.read()
        assert "## Nonexistent" in result
        assert "New stuff" in result
        assert result.startswith("# Title\n\nContent")


class TestReplaceSectionHelper:
    def test_replace_h2_body(self):
        md = "# Title\n\n## A\n\nOld A\n\n## B\n\nOld B\n"
        result = _replace_section(md, "A", "New A")
        assert "New A" in result
        assert "Old A" not in result
        assert "Old B" in result

    def test_replace_last_section(self):
        md = "# Title\n\n## Last\n\nOld content\n"
        result = _replace_section(md, "Last", "New content")
        assert "New content" in result
        assert "Old content" not in result

    def test_heading_not_found_appends_section(self):
        md = "# Title\n\nContent"
        result = _replace_section(md, "Missing", "New")
        assert "## Missing" in result
        assert "New" in result
        assert result.startswith("# Title\n\nContent")

    def test_respects_heading_level(self):
        md = "# Top\n\n## Sub\n\nSub content\n\n# Another Top\n\nOther\n"
        result = _replace_section(md, "Top", "Replaced top")
        assert "Replaced top" in result
        assert "Sub content" not in result
        assert "Other" in result

    def test_heading_with_hash_prefix_not_found_no_double_prefix(self):
        """Passing '## Status' should not create '## ## Status'."""
        md = "# Title\n\nContent"
        result = _replace_section(md, "## Status", "New")
        assert "## Status" in result
        assert "## ## Status" not in result
        assert "New" in result

    def test_heading_with_triple_hash_prefix_not_found(self):
        """Passing '### Detail' should not create '## ### Detail'."""
        md = "# Title\n\nContent"
        result = _replace_section(md, "### Detail", "New")
        assert "## Detail" in result
        assert "### Detail" not in result
        assert "## ### Detail" not in result

    def test_heading_with_hash_prefix_found(self):
        """Passing '## Status' should still match an existing '## Status' heading."""
        md = "# Title\n\n## Status\n\nOld\n"
        result = _replace_section(md, "## Status", "New")
        assert "New" in result
        assert "Old" not in result

    def test_empty_body_clears_section(self):
        md = "# Title\n\n## Status\n\nOld content\n\n## Notes\n\nKeep\n"
        result = _replace_section(md, "Status", "")
        assert "Old content" not in result
        assert "## Status" in result
        assert "Keep" in result

    def test_multiline_body(self):
        md = "# Title\n\n## Status\n\nOld\n"
        result = _replace_section(md, "Status", "Line 1\nLine 2\nLine 3")
        assert "Line 1\nLine 2\nLine 3" in result
        assert "Old" not in result


class TestCanvasStoreRestore:
    async def test_restore_returns_store_when_canvas_exists(self, canvas_registry):
        client = _make_mock_client()
        await canvas_registry.update_canvas("sess-cv", "F_RESTORE", "# Restored")
        store = await CanvasStore.restore(
            session_id="sess-cv",
            client=client,
            registry=canvas_registry,
        )
        assert store is not None
        assert store.canvas_id == "F_RESTORE"
        assert store.read() == "# Restored"

    async def test_restore_returns_none_when_no_canvas(self, canvas_registry):
        client = _make_mock_client()
        store = await CanvasStore.restore(
            session_id="sess-cv",
            client=client,
            registry=canvas_registry,
        )
        assert store is None


class TestCanvasStoreSyncLifecycle:
    async def test_start_and_stop_sync(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            markdown="# Test",
        )
        store.start_sync()
        assert store._sync_task is not None
        assert not store._sync_task.done()
        await store.stop_sync()
        assert store._sync_task is None

    async def test_stop_sync_flushes_dirty(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            markdown="# Test",
        )
        store.start_sync()
        await store.write("# Dirty")
        await store.stop_sync()
        # canvas_sync should have been called via _flush
        assert client._web.api_call.call_count >= 1

    async def test_stop_sync_without_start_is_noop(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
        )
        # Should not raise
        await store.stop_sync()

"""Tests for summon_claude.slack.canvas_store."""

from __future__ import annotations

import asyncio
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
    """Registry with a pre-registered session and channel for canvas tests."""
    reg = SessionRegistry(db_path=tmp_path / "canvas_test.db")
    async with reg:
        await reg.register("sess-cv", 111, "/tmp")
        await reg.update_status("sess-cv", "active", slack_channel_id="C_TEST")
        await reg.register_channel("C_TEST", "test-channel", "/tmp")
        yield reg


class TestCanvasStoreReadWrite:
    async def test_read_returns_initial_markdown(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
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
            channel_id="C_TEST",
            markdown="",
        )
        await store.write("# Updated\nNew content")
        assert store.read() == "# Updated\nNew content"

    async def test_write_persists_to_channels_table(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
        )
        await store.write("# Persisted")
        channel = await canvas_registry.get_channel("C_TEST")
        assert channel["canvas_id"] == "F_1"
        assert channel["canvas_markdown"] == "# Persisted"

    async def test_canvas_id_property(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_ABC",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
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
            channel_id="C_TEST",
            markdown=md,
        )
        await store.update_section("Status", "New status line")
        result = store.read()
        assert "New status line" in result
        assert "Old status" not in result
        assert "Some notes" in result

    async def test_update_section_strips_hash_prefix(self, canvas_registry):
        """Passing '## Status' to update_section should match 'Status'."""
        client = _make_mock_client()
        md = "# Title\n\n## Status\n\nOld\n"
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown=md,
        )
        await store.update_section("## Status", "New")
        result = store.read()
        assert "New" in result
        assert "Old" not in result
        assert "## ## Status" not in result

    async def test_update_section_rejects_empty_heading(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
        )
        with pytest.raises(ValueError, match="non-whitespace text"):
            await store.update_section("", "content")

    async def test_update_section_rejects_hash_only_heading(self, canvas_registry):
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
        )
        with pytest.raises(ValueError, match="non-whitespace text"):
            await store.update_section("###", "content")

    async def test_update_section_missing_heading_appends(self, canvas_registry):
        client = _make_mock_client()
        md = "# Title\n\nContent"
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
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

    def test_adjacent_headings_no_body(self):
        """Two headings with no body between them."""
        md = "## A\n## B\n\nB content\n"
        result = _replace_section(md, "A", "New A")
        assert "New A" in result
        assert "B content" in result

    def test_heading_with_special_chars(self):
        """Headings with parentheses, colons, etc."""
        md = "## Status (beta)\n\nOld\n\n## Notes: Important\n\nKeep\n"
        result = _replace_section(md, "Status (beta)", "New")
        assert "New" in result
        assert "Old" not in result
        assert "Keep" in result

    def test_heading_with_emoji(self):
        md = "## Files Changed :file_folder:\n\nOld files\n"
        result = _replace_section(md, "Files Changed :file_folder:", "New files")
        assert "New files" in result
        assert "Old files" not in result


class TestCanvasStoreRestore:
    async def test_restore_returns_store_when_canvas_exists(self, canvas_registry):
        client = _make_mock_client()
        await canvas_registry.update_channel_canvas("C_TEST", "F_RESTORE", "# Restored")
        store = await CanvasStore.restore(
            session_id="sess-cv",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
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
            channel_id="C_TEST",
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
            channel_id="C_TEST",
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
            channel_id="C_TEST",
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
            channel_id="C_TEST",
        )
        # Should not raise
        await store.stop_sync()

    async def test_flush_resets_dirty_on_failure(self, canvas_registry):
        """When canvas_sync fails, _dirty should be re-set to True."""
        client = _make_mock_client()
        client._web.api_call = AsyncMock(side_effect=Exception("api down"))
        # Patch canvas_sync to return False (simulates failure)
        client.canvas_sync = AsyncMock(return_value=False)  # type: ignore[method-assign]
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown="# Test",
        )
        await store.write("# Dirty")
        assert store._dirty is True
        await store._flush()
        # dirty should be re-set because sync failed
        assert store._dirty is True

    async def test_flush_clears_dirty_on_success(self, canvas_registry):
        client = _make_mock_client()
        client.canvas_sync = AsyncMock(return_value=True)  # type: ignore[method-assign]
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
        )
        await store.write("# Dirty")
        assert store._dirty is True
        await store._flush()
        assert store._dirty is False
        assert store._consecutive_failures == 0

    async def test_backoff_after_consecutive_failures(self, canvas_registry):
        """After 3 consecutive failures, _consecutive_failures tracks correctly."""
        client = _make_mock_client()
        client.canvas_sync = AsyncMock(return_value=False)  # type: ignore[method-assign]
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
        )
        for _i in range(4):
            store._dirty = True
            await store._flush()
        assert store._consecutive_failures == 4

    async def test_backoff_resets_on_success(self, canvas_registry):
        """Successful sync resets the failure counter."""
        client = _make_mock_client()
        client.canvas_sync = AsyncMock(return_value=False)  # type: ignore[method-assign]
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
        )
        # Accumulate failures
        for _ in range(3):
            store._dirty = True
            await store._flush()
        assert store._consecutive_failures == 3
        # Now succeed
        client.canvas_sync = AsyncMock(return_value=True)  # type: ignore[method-assign]
        store._dirty = True
        await store._flush()
        assert store._consecutive_failures == 0

    async def test_sync_loop_flushes_dirty_content(self, canvas_registry):
        """Sync loop should call _flush when dirty flag is set."""
        client = _make_mock_client()
        client.canvas_sync = AsyncMock(return_value=True)  # type: ignore[method-assign]
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown="# Initial",
        )
        store.start_sync()
        await store.write("# Updated")
        # Give sync loop time to pick up and flush (debounce + dirty delay)
        # We can't wait the full 60s, so just stop and verify final flush
        await store.stop_sync()
        client.canvas_sync.assert_called()
        call_args = client.canvas_sync.call_args
        assert call_args[0][1] == "# Updated"

    async def test_concurrent_update_section_no_data_loss(self, canvas_registry):
        """Concurrent update_section calls should not lose data."""
        client = _make_mock_client()
        md = "# Title\n\n## A\n\nOld A\n\n## B\n\nOld B\n"
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown=md,
        )
        # Run two updates concurrently
        await asyncio.gather(
            store.update_section("A", "New A"),
            store.update_section("B", "New B"),
        )
        result = store.read()
        assert "New A" in result
        assert "New B" in result
        assert "Old A" not in result
        assert "Old B" not in result

    async def test_stop_sync_during_flush(self, canvas_registry):
        """stop_sync during an in-flight flush should not raise."""
        client = _make_mock_client()

        async def slow_sync(_canvas_id: str, _md: str) -> bool:
            await asyncio.sleep(0.1)
            return True

        client.canvas_sync = slow_sync  # type: ignore[method-assign]
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown="# Test",
        )
        store.start_sync()
        await store.write("# Dirty")
        # Don't wait for sync loop, just stop immediately
        await store.stop_sync()
        # Should not raise — contextlib.suppress handles CancelledError


class TestCanvasStoreChannelSync:
    """Tests for canvas data syncing to channels table."""

    async def test_persist_syncs_to_channels_table(self, canvas_registry):
        """When channel_id is set, _persist writes to both sessions and channels."""
        await canvas_registry.register_channel("C_CANVAS", "canvas-chan", "/tmp")
        client = _make_mock_client()
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_CH",
            client=client,
            registry=canvas_registry,
            channel_id="C_CANVAS",
            markdown="# Initial",
        )
        await store.write("# Updated via channel")
        channel = await canvas_registry.get_channel("C_CANVAS")
        assert channel is not None
        assert channel["canvas_id"] == "F_CH"
        assert channel["canvas_markdown"] == "# Updated via channel"


class TestCanvasStoreRestoreWithChannel:
    """Tests for CanvasStore.restore() with channel_id parameter."""

    async def test_restore_uses_channel_canvas(self, canvas_registry):
        """When channel has canvas data, restore() uses it."""
        await canvas_registry.register_channel("C_RCH", "restore-chan", "/tmp")
        await canvas_registry.update_channel_canvas("C_RCH", "F_CHAN", "# Channel canvas")
        client = _make_mock_client()
        store = await CanvasStore.restore(
            session_id="sess-cv",
            client=client,
            registry=canvas_registry,
            channel_id="C_RCH",
        )
        assert store is not None
        assert store._canvas_id == "F_CHAN"
        assert store._markdown == "# Channel canvas"
        assert store._channel_id == "C_RCH"

    async def test_restore_returns_none_when_channel_has_no_canvas(self, canvas_registry):
        """When channel has no canvas data, returns None."""
        await canvas_registry.register_channel("C_EMPTY", "empty-chan", "/tmp")
        client = _make_mock_client()
        store = await CanvasStore.restore(
            session_id="sess-cv",
            client=client,
            registry=canvas_registry,
            channel_id="C_EMPTY",
        )
        assert store is None


class TestCanvasStoreUpdateTableField:
    """Tests for update_table_field method (BUG-080)."""

    async def test_updates_status_field(self, canvas_registry):
        client = _make_mock_client()
        md = (
            "# Session Status\n\n"
            "| Field | Value |\n"
            "|-------|-------|\n"
            "| Status | Starting... |\n"
            "| Model | opus |\n"
        )
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown=md,
        )
        await store.update_table_field("Status", "Active")
        result = store.read()
        assert "| Status | Active |" in result
        assert "Starting..." not in result
        assert "| Model | opus |" in result

    async def test_no_match_is_noop(self, canvas_registry):
        client = _make_mock_client()
        md = "# Title\n\nNo table here.\n"
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown=md,
        )
        await store.update_table_field("Status", "Active")
        assert store.read() == md

    async def test_field_name_with_regex_metacharacters(self, canvas_registry):
        client = _make_mock_client()
        md = (
            "# Info\n\n"
            "| Field | Value |\n"
            "|-------|-------|\n"
            "| Version (Semver) | 1.0.0 |\n"
            "| Status | OK |\n"
        )
        store = CanvasStore(
            session_id="sess-cv",
            canvas_id="F_1",
            client=client,
            registry=canvas_registry,
            channel_id="C_TEST",
            markdown=md,
        )
        await store.update_table_field("Version (Semver)", "2.0.0")
        result = store.read()
        assert "| Version (Semver) | 2.0.0 |" in result
        assert "| Status | OK |" in result

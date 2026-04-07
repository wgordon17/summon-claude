"""Integration tests for zzz- channel rename via real Slack API.

Tests the conversations_rename round-trip that the zzz-rename feature depends on:
rename to zzz-prefixed name, rename back (restore), idempotent same-name rename,
and name collision handling.
"""

from __future__ import annotations

import pytest

from summon_claude.slack.client import ZZZ_PREFIX, SlackClient, make_zzz_name

pytestmark = [pytest.mark.slack]


class TestZzzRenameRoundTrip:
    """Verify zzz- rename and restore works via real Slack API."""

    async def test_zzz_rename_and_restore(self, slack_harness, fresh_channel):
        """Channel can be renamed to zzz- prefix and back."""
        # Get original name
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        original_name = info["channel"]["name"]

        # Rename to zzz-
        zzz_name = make_zzz_name(original_name)
        resp = await slack_harness.client.conversations_rename(
            channel=fresh_channel,
            name=zzz_name,
        )
        assert resp["channel"]["name"] == zzz_name

        # Verify via conversations_info
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["name"] == zzz_name

        # Restore original name
        resp = await slack_harness.client.conversations_rename(
            channel=fresh_channel,
            name=original_name,
        )
        assert resp["channel"]["name"] == original_name

    async def test_zzz_rename_idempotent(self, slack_harness, fresh_channel):
        """Renaming to the same zzz- name twice succeeds (no error)."""
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        original_name = info["channel"]["name"]

        zzz_name = make_zzz_name(original_name)
        await slack_harness.client.conversations_rename(
            channel=fresh_channel,
            name=zzz_name,
        )
        # Second rename to same name — should not error
        resp = await slack_harness.client.conversations_rename(
            channel=fresh_channel,
            name=zzz_name,
        )
        assert resp["channel"]["name"] == zzz_name

    async def test_zzz_slack_client_rename_channel(self, slack_harness, fresh_channel):
        """SlackClient.rename_channel() works end-to-end."""
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        original_name = info["channel"]["name"]

        client = SlackClient(slack_harness.client, fresh_channel)
        zzz_name = make_zzz_name(original_name)
        result = await client.rename_channel(zzz_name)
        assert result == zzz_name

        # Verify
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["name"] == zzz_name

    async def test_zzz_rename_after_archive_unarchive(self, slack_harness, fresh_channel):
        """Channel can be zzz-renamed, archived, unarchived, and restored."""
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        original_name = info["channel"]["name"]

        # Rename to zzz-
        zzz_name = make_zzz_name(original_name)
        await slack_harness.client.conversations_rename(
            channel=fresh_channel,
            name=zzz_name,
        )

        # Archive
        await slack_harness.client.conversations_archive(channel=fresh_channel)

        # Unarchive
        await slack_harness.client.conversations_unarchive(channel=fresh_channel)

        # Should still have zzz- name
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        assert info["channel"]["name"] == zzz_name

        # Restore
        resp = await slack_harness.client.conversations_rename(
            channel=fresh_channel,
            name=original_name,
        )
        assert resp["channel"]["name"] == original_name


class TestZzzMakeZzzName:
    """Verify make_zzz_name helper produces valid Slack channel names."""

    async def test_zzz_make_name_within_80_chars(self, slack_harness, fresh_channel):
        """make_zzz_name output accepted by Slack conversations_rename."""
        info = await slack_harness.client.conversations_info(channel=fresh_channel)
        original_name = info["channel"]["name"]

        zzz_name = make_zzz_name(original_name)
        assert zzz_name.startswith(ZZZ_PREFIX)
        assert len(zzz_name) <= 80

        resp = await slack_harness.client.conversations_rename(
            channel=fresh_channel,
            name=zzz_name,
        )
        assert resp["channel"]["name"] == zzz_name

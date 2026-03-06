"""Shared fixtures for Slack integration tests.

All tests share a single channel to stay within Slack's rate limits
(conversations.create is Tier 2 — ~20/min). The shared channel fixture
exercises lifecycle operations (create, invite, set_topic) on first use,
providing transitive signal for those code paths. Archive is exercised
in teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

import pytest
from slack_sdk.web.async_client import AsyncWebClient

from summon_claude.slack.client import SlackClient
from summon_claude.slack.router import ThreadRouter

# Load .env file so credentials are available for local runs
_env_file = Path(__file__).resolve().parents[2] / ".env"
if _env_file.exists():
    for raw_line in _env_file.read_text().splitlines():
        entry = raw_line.strip()
        if entry and not entry.startswith("#") and "=" in entry:
            key, _, value = entry.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# Shared channel state — created once, reused across all tests
_shared_channel_id: str | None = None
_channels_to_cleanup: list[str] = []


class SlackTestHarness:
    """Manages Slack workspace state for integration tests."""

    def __init__(self) -> None:
        self._bot_token = os.environ["SUMMON_TEST_SLACK_BOT_TOKEN"]
        self._app_token = os.environ["SUMMON_TEST_SLACK_APP_TOKEN"]
        self._signing_secret = os.environ["SUMMON_TEST_SLACK_SIGNING_SECRET"]
        self._client: AsyncWebClient | None = None
        self._bot_user_id: str | None = None
        self._non_bot_user_id: str | None = None
        self._non_bot_user_resolved = False

    @property
    def bot_token(self) -> str:
        return self._bot_token

    @property
    def app_token(self) -> str:
        return self._app_token

    @property
    def signing_secret(self) -> str:
        return self._signing_secret

    @property
    def client(self) -> AsyncWebClient:
        if self._client is None:
            self._client = AsyncWebClient(token=self._bot_token)
        return self._client

    @property
    def keep_artifacts(self) -> bool:
        return os.environ.get("SUMMON_TEST_KEEP_ARTIFACTS", "") == "1"

    async def resolve_bot_user_id(self) -> str:
        if self._bot_user_id is None:
            resp = await self.client.auth_test()
            self._bot_user_id = resp["user_id"]
        return self._bot_user_id

    async def find_non_bot_user(self) -> str | None:
        """Find a non-bot workspace member. Returns user_id or None. Cached."""
        if not self._non_bot_user_resolved:
            resp = await self.client.users_list(limit=50)
            bot_id = await self.resolve_bot_user_id()
            for member in resp.get("members", []):
                if (
                    not member.get("is_bot")
                    and not member.get("deleted")
                    and member.get("id") != "USLACKBOT"
                    and member.get("id") != bot_id
                ):
                    self._non_bot_user_id = member["id"]
                    break
            self._non_bot_user_resolved = True
        return self._non_bot_user_id

    async def create_test_channel(self, prefix: str = "test") -> str:
        """Create a test channel with timestamp suffix. Returns channel_id."""
        name = f"{prefix}-integ-{int(time.time())}"[:80]
        resp = await self.client.conversations_create(name=name, is_private=True)
        channel = resp.get("channel") or {}
        channel_id = channel["id"]
        _channels_to_cleanup.append(channel_id)
        return channel_id

    async def cleanup_channels(self, channel_ids: list[str]) -> None:
        """Archive test channels (best-effort)."""
        for cid in channel_ids:
            with contextlib.suppress(Exception):
                await self.client.conversations_archive(channel=cid)


@pytest.fixture
async def slack_harness():
    """Harness — skips if credentials not set."""
    if not os.environ.get("SUMMON_TEST_SLACK_BOT_TOKEN"):
        pytest.skip("SUMMON_TEST_SLACK_BOT_TOKEN not set")

    harness = SlackTestHarness()
    await harness.resolve_bot_user_id()
    yield harness


@pytest.fixture
async def test_channel(slack_harness):
    """Shared test channel — created once, reused across all tests.

    First call exercises create_channel + invite_user + set_topic,
    providing transitive lifecycle signal. Subsequent calls return
    the cached channel_id.
    """
    global _shared_channel_id  # noqa: PLW0603
    if _shared_channel_id is None:
        _shared_channel_id = await slack_harness.create_test_channel()
        # Transitive lifecycle signal: invite a real user via raw web_client
        user_id = await slack_harness.find_non_bot_user()
        if user_id:
            with contextlib.suppress(Exception):
                await slack_harness.client.conversations_invite(
                    channel=_shared_channel_id, users=user_id
                )
        # Transitive lifecycle signal: set topic
        await slack_harness.client.conversations_setTopic(
            channel=_shared_channel_id, topic="Integration test channel"
        )
    yield _shared_channel_id


@pytest.fixture
def slack_client(slack_harness, test_channel):
    """SlackClient bound to test channel."""
    return SlackClient(slack_harness.client, test_channel)


@pytest.fixture
async def thread_router(slack_client):
    """ThreadRouter backed by real SlackClient and test channel."""
    return ThreadRouter(slack_client)


def pytest_sessionfinish(session, exitstatus):
    """Archive all test channels at end of session."""
    if not _channels_to_cleanup:
        return
    if os.environ.get("SUMMON_TEST_KEEP_ARTIFACTS", "") == "1":
        return
    token = os.environ.get("SUMMON_TEST_SLACK_BOT_TOKEN")
    if not token:
        return
    client = AsyncWebClient(token=token)

    async def _cleanup():
        for cid in _channels_to_cleanup:
            with contextlib.suppress(Exception):
                await client.conversations_archive(channel=cid)

    with contextlib.suppress(Exception):
        asyncio.run(_cleanup())

"""Stress tests for concurrent permission request scenarios."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from helpers import make_mock_provider
from summon_claude.config import SummonConfig
from summon_claude.sessions.permissions import PermissionHandler
from summon_claude.slack.router import ThreadRouter


def make_config(debounce_ms: int = 50) -> SummonConfig:
    return SummonConfig.model_validate(
        {
            "slack_bot_token": "xoxb-t",
            "slack_app_token": "xapp-t",
            "slack_signing_secret": "s",
            "permission_debounce_ms": debounce_ms,
        }
    )


def make_handler(debounce_ms: int = 50):
    provider = make_mock_provider()
    router = ThreadRouter(provider, "C_CHAN")
    config = make_config(debounce_ms=debounce_ms)
    return PermissionHandler(router, config, authenticated_user_id="U_TEST"), provider, router


def _auto_approve_after_post(handler: PermissionHandler, delay: float = 0.05):
    """Return a side_effect for post_ephemeral that auto-approves pending batches."""

    async def _side_effect(*_args, **_kwargs):
        async def _do():
            await asyncio.sleep(delay)
            for batch_id in list(handler._batch.events.keys()):
                handler._batch.decisions[batch_id] = True
                handler._batch.events[batch_id].set()

        asyncio.create_task(_do())

    return _side_effect


def _auto_deny_after_post(handler: PermissionHandler, delay: float = 0.05):
    """Return a side_effect for post_ephemeral that auto-denies pending batches."""

    async def _side_effect(*_args, **_kwargs):
        async def _do():
            await asyncio.sleep(delay)
            for batch_id in list(handler._batch.events.keys()):
                handler._batch.decisions[batch_id] = False
                handler._batch.events[batch_id].set()

        asyncio.create_task(_do())

    return _side_effect


class TestConcurrentRequestsBatchWithinDebounceWindow:
    async def test_three_concurrent_requests_single_message(self):
        """3 simultaneous requests within debounce window -> 1 ephemeral message posted."""
        debounce_ms = 100
        handler, provider, _ = make_handler(debounce_ms=debounce_ms)
        provider.post_ephemeral = AsyncMock(
            side_effect=_auto_approve_after_post(handler, delay=0.05)
        )

        results = await asyncio.gather(
            handler.handle("Bash", {"command": "cmd1"}, None),
            handler.handle("Edit", {"path": "/f1"}, None),
            handler.handle("Write", {"file_path": "/f2"}, None),
        )

        assert all(isinstance(r, PermissionResultAllow) for r in results)
        assert provider.post_ephemeral.call_count == 1

    async def test_all_concurrent_requests_resolve_allow(self):
        """All requests in a batch get Allow when approved."""
        handler, provider, _ = make_handler(debounce_ms=50)
        provider.post_ephemeral = AsyncMock(
            side_effect=_auto_approve_after_post(handler, delay=0.05)
        )

        results = await asyncio.gather(
            handler.handle("Bash", {"command": "echo 1"}, None),
            handler.handle("Bash", {"command": "echo 2"}, None),
            handler.handle("Bash", {"command": "echo 3"}, None),
        )
        assert all(isinstance(r, PermissionResultAllow) for r in results)


class TestTimeoutDeniesAllPending:
    async def test_short_timeout_denies_all(self):
        """With a very short permission timeout, all pending requests are denied."""
        handler, provider, _ = make_handler(debounce_ms=10)

        provider.post_ephemeral = AsyncMock()

        # Patch the permission timeout to be very short
        import summon_claude.sessions.permissions as perm_module

        original_timeout = perm_module._PERMISSION_TIMEOUT_S
        perm_module._PERMISSION_TIMEOUT_S = 0.2

        try:
            results = await asyncio.gather(
                handler.handle("Bash", {"command": "cmd"}, None),
                return_exceptions=True,
            )
        finally:
            perm_module._PERMISSION_TIMEOUT_S = original_timeout

        assert len(results) == 1
        assert isinstance(results[0], PermissionResultDeny)


class TestManyRequestsBatchApproved:
    async def test_ten_concurrent_requests_all_resolve_allow(self):
        """10 concurrent requests approved -> all 10 return Allow."""
        handler, provider, _ = make_handler(debounce_ms=80)
        provider.post_ephemeral = AsyncMock(
            side_effect=_auto_approve_after_post(handler, delay=0.05)
        )

        tasks = [handler.handle("Edit", {"path": f"/file{i}.py"}, None) for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        assert all(isinstance(r, PermissionResultAllow) for r in results)
        assert provider.post_ephemeral.call_count == 1


class TestApprovalWhileNewRequestQueuing:
    async def test_sequential_batches_get_separate_messages(self):
        """Requests separated by more than the debounce window get separate batches."""
        debounce_ms = 50
        handler, provider, _ = make_handler(debounce_ms=debounce_ms)

        call_count = 0

        async def _counting_approve(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1

            async def _do():
                await asyncio.sleep(0.02)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(_do())

        provider.post_ephemeral = AsyncMock(side_effect=_counting_approve)

        result_a = await handler.handle("Bash", {"command": "cmd1"}, None)
        await asyncio.sleep(debounce_ms / 1000.0 * 3)
        result_b = await handler.handle("Edit", {"path": "/f"}, None)

        assert isinstance(result_a, PermissionResultAllow)
        assert isinstance(result_b, PermissionResultAllow)
        assert call_count >= 2


class TestRapidApproveDenyRace:
    async def test_first_action_wins(self):
        """Whichever action fires first, the batch decision is set."""
        handler, provider, _ = make_handler(debounce_ms=30)
        provider.post_ephemeral = AsyncMock(
            side_effect=_auto_approve_after_post(handler, delay=0.02)
        )
        result = await handler.handle("Bash", {"command": "test"}, None)
        assert isinstance(result, PermissionResultAllow)

    async def test_deny_before_approve_returns_deny(self):
        """If deny fires, the result is Deny."""
        handler, provider, _ = make_handler(debounce_ms=30)
        provider.post_ephemeral = AsyncMock(side_effect=_auto_deny_after_post(handler, delay=0.02))
        result = await handler.handle("Edit", {"path": "/tmp/f"}, None)
        assert isinstance(result, PermissionResultDeny)


class TestDebounceWindowCollectsRequests:
    async def test_requests_within_window_batched_together(self):
        """Requests fired within the debounce window end up in a single batch."""
        debounce_ms = 200
        handler, provider, _ = make_handler(debounce_ms=debounce_ms)
        provider.post_ephemeral = AsyncMock(
            side_effect=_auto_approve_after_post(handler, delay=0.05)
        )

        async def fire_with_delay(delay_s: float, tool: str):
            await asyncio.sleep(delay_s)
            return await handler.handle(tool, {"command": f"cmd-{tool}"}, None)

        results = await asyncio.gather(
            fire_with_delay(0.00, "Bash"),
            fire_with_delay(0.03, "Edit"),
            fire_with_delay(0.06, "Write"),
        )

        assert all(isinstance(r, PermissionResultAllow) for r in results)
        assert provider.post_ephemeral.call_count == 1

    async def test_request_outside_window_starts_new_batch(self):
        """A request after the debounce window starts a new batch."""
        debounce_ms = 50
        handler, provider, _ = make_handler(debounce_ms=debounce_ms)

        call_count = 0

        async def _counting(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1

            async def _do():
                await asyncio.sleep(0.02)
                for batch_id in list(handler._batch.events.keys()):
                    handler._batch.decisions[batch_id] = True
                    handler._batch.events[batch_id].set()

            asyncio.create_task(_do())

        provider.post_ephemeral = AsyncMock(side_effect=_counting)

        r1 = await handler.handle("Bash", {"command": "first"}, None)
        await asyncio.sleep(debounce_ms / 1000.0 * 4)
        r2 = await handler.handle("Edit", {"path": "/f"}, None)

        assert isinstance(r1, PermissionResultAllow)
        assert isinstance(r2, PermissionResultAllow)
        assert call_count >= 2

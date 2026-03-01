"""Tests for summon_claude.bolt_router — BoltRouter lifecycle and handler registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.config import SummonConfig


def make_config(**overrides) -> SummonConfig:
    defaults = {
        "slack_bot_token": "xoxb-test-token",
        "slack_app_token": "xapp-test-token",
        "slack_signing_secret": "test-secret",
    }
    defaults.update(overrides)
    return SummonConfig.model_validate(defaults)


def _mock_app() -> MagicMock:
    """Return a MagicMock that behaves like AsyncApp for handler registration."""
    app = MagicMock()
    app.command = MagicMock(return_value=lambda f: f)
    app.event = MagicMock(return_value=lambda f: f)
    app.action = MagicMock(return_value=lambda f: f)
    return app


def _make_router(config: SummonConfig | None = None, dispatcher=None):
    """Return a BoltRouter with all Slack SDK constructors patched out.

    The returned router has NOT been started — call ``await router.start()``
    to build the app and register handlers.  The patches are stored on the
    router so callers can inspect mocks after ``start()``.
    """
    from contextlib import ExitStack

    from summon_claude.bolt_router import BoltRouter

    cfg = config or make_config()
    if dispatcher is None:
        dispatcher = MagicMock()
        dispatcher.dispatch_message = AsyncMock()
        dispatcher.dispatch_reaction = AsyncMock()
        dispatcher.dispatch_action = AsyncMock()
        dispatcher.dispatch_command = AsyncMock()
        dispatcher.all_channel_ids = MagicMock(return_value=[])

    stack = ExitStack()
    patched_app_cls = stack.enter_context(patch("summon_claude.bolt_router.AsyncApp"))
    stack.enter_context(patch("summon_claude.bolt_router.AsyncWebClient"))
    patched_handler_cls = stack.enter_context(
        patch("summon_claude.bolt_router.AsyncSocketModeHandler")
    )

    mock_a = _mock_app()
    patched_app_cls.return_value = mock_a

    mock_h = AsyncMock()
    mock_h.connect_async = AsyncMock()
    mock_h.close_async = AsyncMock()
    patched_handler_cls.return_value = mock_h

    router = BoltRouter(cfg, dispatcher)
    # Store stack so patches persist through start()
    router._patch_stack = stack  # type: ignore[attr-defined]
    router._mock_app_factory = mock_a  # type: ignore[attr-defined]
    router._mock_handler_factory = mock_h  # type: ignore[attr-defined]
    router._mock_dispatcher = dispatcher  # type: ignore[attr-defined]

    # Make auth_test() awaitable so start() works in tests
    router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
    return router


async def _make_started_router(config: SummonConfig | None = None):
    """Return a BoltRouter that has been started (app built, handlers registered)."""
    router = _make_router(config)
    await router.start()
    return router


class TestBoltRouterInit:
    """Tests for BoltRouter construction and initial state."""

    def test_provider_is_slack_chat_provider(self):
        """provider property returns a SlackChatProvider instance."""
        from summon_claude.providers.slack import SlackChatProvider

        router = _make_router()
        assert isinstance(router.provider, SlackChatProvider)

    def test_provider_is_stable(self):
        """provider returns the same object on repeated access."""
        router = _make_router()
        assert router.provider is router.provider

    def test_dispatcher_stored_from_constructor(self):
        """dispatcher is stored from the constructor argument."""
        mock_dispatcher = MagicMock()
        router = _make_router(dispatcher=mock_dispatcher)
        assert router._dispatcher is mock_dispatcher

    def test_app_is_none_before_start(self):
        """_app is None until start() is called."""
        router = _make_router()
        assert router._app is None

    async def test_handlers_registered_at_start(self):
        """Handler registration happens during start(), not __init__."""
        router = _make_router()
        await router.start()
        app = router._app
        assert app is not None
        app.command.assert_called_with("/summon")  # type: ignore[union-attr]
        assert app.event.call_count >= 2  # type: ignore[union-attr]
        assert app.action.call_count >= 3  # type: ignore[union-attr]


class TestConstructorWiring:
    """Tests that dispatcher is properly wired via constructor."""

    def test_dispatcher_is_required(self):
        """BoltRouter requires a dispatcher argument."""
        from summon_claude.bolt_router import BoltRouter

        cfg = make_config()
        dispatcher = MagicMock()
        with (
            patch("summon_claude.bolt_router.AsyncWebClient"),
        ):
            router = BoltRouter(cfg, dispatcher)
            assert router._dispatcher is dispatcher


class TestLifecycle:
    """Tests for start() and stop()."""

    async def test_start_calls_connect_async(self):
        """start() calls connect_async() on the socket handler."""
        router = _make_router()
        await router.start()
        router._socket_handler.connect_async.assert_awaited_once()

    async def test_stop_calls_close_async(self):
        """stop() calls close_async() on the socket handler."""
        router = _make_router()
        await router.start()
        router._socket_handler.close_async.reset_mock()
        await router.stop()
        router._socket_handler.close_async.assert_awaited_once()

    async def test_stop_tolerates_close_error(self):
        """stop() does not propagate exceptions from close_async()."""
        router = _make_router()
        await router.start()
        router._socket_handler.close_async.side_effect = RuntimeError("already closed")
        await router.stop()  # must not raise

    async def test_stop_before_start_is_safe(self):
        """stop() is safe to call even if start() was never called."""
        router = _make_router()
        await router.stop()  # must not raise


class TestReconnect:
    """Tests for reconnect() — fresh AsyncApp creation."""

    async def test_reconnect_creates_new_app(self):
        """reconnect() closes the old handler and creates a new AsyncApp."""
        from summon_claude.bolt_router import BoltRouter

        cfg = make_config()
        mock_a1, mock_a2 = _mock_app(), _mock_app()
        mock_h1, mock_h2 = AsyncMock(), AsyncMock()
        mock_dispatcher = MagicMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp") as patched_app_cls,
            patch("summon_claude.bolt_router.AsyncWebClient"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.side_effect = [mock_a1, mock_a2]
            patched_handler_cls.side_effect = [mock_h1, mock_h2]

            router = BoltRouter(cfg, mock_dispatcher)
            router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
            await router.start()
            assert router._app is mock_a1

            await router.reconnect()

            mock_h1.close_async.assert_awaited_once()
            assert router._app is mock_a2
            mock_h2.connect_async.assert_awaited_once()

    async def test_reconnect_re_registers_handlers(self):
        """reconnect() registers handlers on the new app."""
        from summon_claude.bolt_router import BoltRouter

        cfg = make_config()
        mock_a1, mock_a2 = _mock_app(), _mock_app()
        mock_dispatcher = MagicMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp") as patched_app_cls,
            patch("summon_claude.bolt_router.AsyncWebClient"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.side_effect = [mock_a1, mock_a2]
            patched_handler_cls.return_value = AsyncMock()

            router = BoltRouter(cfg, mock_dispatcher)
            router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
            await router.start()
            await router.reconnect()

        assert mock_a2.event.call_count >= 2
        assert mock_a2.command.call_count >= 1

    async def test_reconnect_tolerates_old_close_error(self):
        """reconnect() continues even if closing the old handler raises."""
        from summon_claude.bolt_router import BoltRouter

        cfg = make_config()
        mock_h1 = AsyncMock()
        mock_h2 = AsyncMock()
        mock_dispatcher = MagicMock()

        with (
            patch("summon_claude.bolt_router.AsyncApp") as patched_app_cls,
            patch("summon_claude.bolt_router.AsyncWebClient"),
            patch("summon_claude.bolt_router.AsyncSocketModeHandler") as patched_handler_cls,
        ):
            patched_app_cls.return_value = _mock_app()
            patched_handler_cls.side_effect = [mock_h1, mock_h2]

            router = BoltRouter(cfg, mock_dispatcher)
            router._client.auth_test = AsyncMock(return_value={"user_id": "UBOT"})
            await router.start()
            mock_h1.close_async.side_effect = RuntimeError("dead")
            await router.reconnect()  # must not raise

        mock_h2.connect_async.assert_awaited_once()


class TestSummonCommandHandler:
    """Tests for the /summon slash command Bolt handler."""

    async def _invoke_summon(self, router, command: dict) -> dict:
        """Extract the /summon handler from BoltRouter and call it directly."""
        registered_fn = None

        def capture_command(path):
            def decorator(fn):
                nonlocal registered_fn
                if path == "/summon":
                    registered_fn = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = capture_command
        mock_a.event = MagicMock(return_value=lambda f: f)
        mock_a.action = MagicMock(return_value=lambda f: f)
        router._register_handlers(mock_a)

        assert registered_fn is not None, "No /summon handler was registered"

        ack = AsyncMock()
        respond = AsyncMock()
        await registered_fn(ack=ack, command=command, respond=respond)
        return {"ack": ack, "respond": respond}

    async def test_summon_acks_immediately(self):
        """The /summon handler calls ack() before any other processing."""
        router = _make_router()
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "abc123"})
        result["ack"].assert_awaited_once()

    async def test_summon_empty_text_responds_with_usage(self):
        """/summon with no code text responds with usage instructions."""
        router = _make_router()
        result = await self._invoke_summon(router, {"user_id": "U001", "text": ""})
        respond_call = result["respond"].call_args
        text = respond_call.kwargs.get("text", "")
        assert "Usage" in text

    async def test_summon_delegates_to_dispatcher(self):
        """/summon with valid code calls dispatcher.dispatch_command."""
        router = _make_router()

        result = await self._invoke_summon(router, {"user_id": "U001", "text": "mycode"})

        router._mock_dispatcher.dispatch_command.assert_awaited_once_with(
            user_id="U001",
            code="mycode",
            respond=result["respond"],
        )

    async def test_summon_rate_limited_user_gets_cooldown_message(self):
        """/summon rate-limits repeated calls from the same user."""
        router = _make_router()
        # Exhaust the rate-limit token directly
        router._rate_limiter.check("U001")
        result = await self._invoke_summon(router, {"user_id": "U001", "text": "code"})
        text = result["respond"].call_args.kwargs.get("text", "")
        assert "wait" in text.lower()


class TestMessageHandlerRouting:
    """Tests that message/reaction/action events are routed to dispatcher."""

    def _extract_event_handler(self, router, event_type: str):
        """Re-register handlers on a capturing mock and return the named handler."""
        captured: dict = {}

        def capture_event(ev_type):
            def decorator(fn):
                captured[ev_type] = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = MagicMock(return_value=lambda f: f)
        mock_a.event = capture_event
        mock_a.action = MagicMock(return_value=lambda f: f)
        router._register_handlers(mock_a)
        return captured.get(event_type)

    def _extract_action_handler(self, router, action_id: str):
        """Re-register handlers and return the handler for a given action_id."""
        captured: dict = {}

        def capture_action(action_id_or_pattern):
            key = str(action_id_or_pattern)

            def decorator(fn):
                captured[key] = fn
                return fn

            return decorator

        mock_a = MagicMock()
        mock_a.command = MagicMock(return_value=lambda f: f)
        mock_a.event = MagicMock(return_value=lambda f: f)
        mock_a.action = capture_action
        router._register_handlers(mock_a)
        return captured.get(action_id)

    async def test_message_event_routes_to_dispatcher(self):
        """message events call dispatcher.dispatch_message."""
        router = _make_router()

        handler = self._extract_event_handler(router, "message")
        assert handler is not None

        event = {"type": "message", "channel": "C001", "text": "hello"}
        await handler(event=event, say=AsyncMock())

        router._mock_dispatcher.dispatch_message.assert_awaited_once_with(event)

    async def test_reaction_added_routes_to_dispatcher(self):
        """reaction_added events call dispatcher.dispatch_reaction."""
        router = _make_router()

        handler = self._extract_event_handler(router, "reaction_added")
        assert handler is not None

        event = {"type": "reaction_added", "item": {"channel": "C001"}}
        await handler(event=event)

        router._mock_dispatcher.dispatch_reaction.assert_awaited_once_with(event)

    async def test_permission_approve_action_calls_dispatcher(self):
        """permission_approve action calls dispatcher.dispatch_action (after ack)."""
        router = _make_router()

        handler = self._extract_action_handler(router, "permission_approve")
        assert handler is not None

        ack = AsyncMock()
        action = {"action_id": "permission_approve", "value": "approve:b1"}
        body = {"channel": {"id": "C001"}, "user": {"id": "U001"}, "response_url": ""}
        await handler(ack=ack, action=action, body=body)

        ack.assert_awaited_once()
        router._mock_dispatcher.dispatch_action.assert_awaited_once_with(action, body)


class TestReconnectExhausted:
    """Tests for _on_reconnect_exhausted bound method."""

    async def test_calls_shutdown_callback(self):
        """Exhaustion invokes the registered shutdown callback."""
        router = _make_router()
        cb = MagicMock()
        router.shutdown_callback = cb
        await router._on_reconnect_exhausted()
        cb.assert_called_once()

    async def test_warns_when_no_callback(self):
        """Logs warning when no shutdown callback is registered."""
        router = _make_router()
        assert router.shutdown_callback is None
        # Should not raise
        await router._on_reconnect_exhausted()

    async def test_posts_notices_to_all_channels(self):
        """Posts disconnect notice to every channel from the dispatcher."""
        router = _make_router()
        router.shutdown_callback = MagicMock()
        router._mock_dispatcher.all_channel_ids.return_value = ["C001", "C002"]
        router.provider.post_message = AsyncMock()

        await router._on_reconnect_exhausted()
        # Wait for the fire-and-forget notice task
        if router._exhausted_notice_task is not None:
            await router._exhausted_notice_task

        assert router.provider.post_message.await_count == 2

    async def test_no_channels_does_not_crash(self):
        """No crash when dispatcher has no channels to notify."""
        router = _make_router()
        router.shutdown_callback = MagicMock()
        router._mock_dispatcher.all_channel_ids.return_value = []
        await router._on_reconnect_exhausted()

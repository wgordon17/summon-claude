"""Tests for summon_claude.jira_proxy -- JiraAuthProxy reverse proxy."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from summon_claude.jira_proxy import JiraAuthProxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token_data(
    *,
    access_token: str = "test-access-token",  # noqa: S107
    expires_at: float | None = None,
) -> dict[str, Any]:
    if expires_at is None:
        expires_at = time.time() + 7200  # fresh
    return {"access_token": access_token, "expires_at": expires_at}


def _make_mock_path(mtime: float = 1000.0) -> MagicMock:
    """Return a MagicMock that behaves like a Path with a controllable .stat() mtime."""
    mock_path = MagicMock(spec=Path)
    mock_stat = MagicMock()
    mock_stat.st_mtime = mtime
    mock_path.stat.return_value = mock_stat
    return mock_path


def _make_missing_path() -> MagicMock:
    """Return a MagicMock Path whose .stat() raises FileNotFoundError."""
    mock_path = MagicMock(spec=Path)
    mock_path.stat.side_effect = FileNotFoundError
    return mock_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proxy(tmp_path: Path):
    """Return a JiraAuthProxy. Token path set to a non-existent tmp file by default."""
    p = JiraAuthProxy()
    p._token_path = tmp_path / "token.json"
    return p


@pytest.fixture
async def started_proxy(proxy: JiraAuthProxy):
    """Start and yield a proxy, stop it after the test."""
    await proxy.start()
    yield proxy
    await proxy.stop()


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestProxyLifecycle:
    def test_port_raises_before_start(self, proxy: JiraAuthProxy):
        with pytest.raises(RuntimeError, match="not started"):
            _ = proxy.port

    async def test_start_returns_ephemeral_port(self, proxy: JiraAuthProxy):
        port = await proxy.start()
        try:
            assert port > 0
            assert proxy.port == port
        finally:
            await proxy.stop()

    async def test_stop_resets_port(self, proxy: JiraAuthProxy):
        await proxy.start()
        await proxy.stop()
        with pytest.raises(RuntimeError, match="not started"):
            _ = proxy.port

    async def test_double_stop_no_error(self, proxy: JiraAuthProxy):
        await proxy.start()
        await proxy.stop()
        await proxy.stop()  # Must not raise


# ---------------------------------------------------------------------------
# Token caching tests
# ---------------------------------------------------------------------------


class TestTokenCache:
    async def test_token_cache_hit(self, proxy: JiraAuthProxy):
        """Fresh cached token is returned without calling refresh or load."""
        proxy._cached_token = "cached-token"
        proxy._token_expires_at = time.time() + 7200
        proxy._token_file_mtime = 1000.0
        proxy._token_path = _make_mock_path(mtime=1000.0)  # same mtime, no invalidation

        with (
            patch(
                "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                new_callable=AsyncMock,
            ) as mock_refresh,
            patch("summon_claude.jira_proxy.load_jira_token") as mock_load,
        ):
            result = await proxy._get_fresh_token()

        assert result == "cached-token"
        mock_refresh.assert_not_called()
        mock_load.assert_not_called()

    async def test_token_refresh_on_expiry(self, proxy: JiraAuthProxy):
        """Expired token triggers refresh, then loads from disk."""
        proxy._cached_token = "old-token"
        proxy._token_expires_at = time.time() - 10  # expired
        proxy._token_file_mtime = 1000.0
        proxy._token_path = _make_mock_path(mtime=1000.0)

        token_data = _make_token_data(access_token="new-token")

        with (
            patch(
                "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                new_callable=AsyncMock,
            ) as mock_refresh,
            patch("summon_claude.jira_proxy.load_jira_token", return_value=token_data),
        ):
            result = await proxy._get_fresh_token()

        assert result == "new-token"
        mock_refresh.assert_called_once()

    async def test_token_refresh_failure_returns_none(self, proxy: JiraAuthProxy):
        """When load_jira_token returns None after refresh attempt, result is None."""
        proxy._token_expires_at = 0  # expired
        proxy._token_path = _make_mock_path(mtime=1000.0)

        with (
            patch(
                "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                new_callable=AsyncMock,
            ),
            patch("summon_claude.jira_proxy.load_jira_token", return_value=None),
        ):
            result = await proxy._get_fresh_token()

        assert result is None

    async def test_mtime_cache_invalidation(self, proxy: JiraAuthProxy):
        """Changed file mtime forces a re-read even if expiry appears fresh."""
        proxy._cached_token = "stale-token"
        proxy._token_expires_at = time.time() + 7200
        proxy._token_file_mtime = 1000.0  # old mtime
        proxy._token_path = _make_mock_path(mtime=2000.0)  # new mtime, invalidates cache

        token_data = _make_token_data(access_token="fresh-token")

        with (
            patch(
                "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                new_callable=AsyncMock,
            ),
            patch("summon_claude.jira_proxy.load_jira_token", return_value=token_data),
        ):
            result = await proxy._get_fresh_token()

        assert result == "fresh-token"

    async def test_failed_load_updates_mtime_for_backoff(self, proxy: JiraAuthProxy):
        """After failed token load, mtime is updated so backoff timer holds.

        Without the mtime update, the next call would see mtime != cached_mtime,
        reset _token_expires_at to 0, and re-enter the cold path — defeating backoff.
        """
        proxy._token_expires_at = 0  # expired
        proxy._token_file_mtime = 0.0  # initial state
        mock_path = _make_mock_path(mtime=1234.0)
        proxy._token_path = mock_path

        with (
            patch(
                "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                new_callable=AsyncMock,
            ),
            patch("summon_claude.jira_proxy.load_jira_token", return_value=None),
        ):
            result = await proxy._get_fresh_token()

        assert result is None
        # mtime must be updated so next call doesn't re-trigger via mismatch
        assert proxy._token_file_mtime == 1234.0
        # backoff timer must be set (expires_at > now)
        assert proxy._token_expires_at > time.time()

    async def test_token_file_not_found_returns_none(self, proxy: JiraAuthProxy):
        """Missing token file returns None immediately."""
        proxy._token_path = _make_missing_path()
        result = await proxy._get_fresh_token()
        assert result is None

    async def test_concurrent_refresh_serialization(self, proxy: JiraAuthProxy):
        """Multiple concurrent callers trigger only one refresh call."""
        proxy._token_expires_at = 0  # expired
        proxy._token_file_mtime = 1000.0
        proxy._token_path = _make_mock_path(mtime=1000.0)
        refresh_call_count = 0

        async def _fake_refresh() -> None:
            nonlocal refresh_call_count
            refresh_call_count += 1
            await asyncio.sleep(0.01)  # simulate network delay

        token_data = _make_token_data(access_token="refreshed-token")

        with (
            patch(
                "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                side_effect=_fake_refresh,
            ),
            patch("summon_claude.jira_proxy.load_jira_token", return_value=token_data),
        ):
            results = await asyncio.gather(
                proxy._get_fresh_token(),
                proxy._get_fresh_token(),
                proxy._get_fresh_token(),
            )

        assert all(r == "refreshed-token" for r in results)
        assert refresh_call_count == 1


# ---------------------------------------------------------------------------
# Proxy authentication tests
# ---------------------------------------------------------------------------


class TestProxyAuthentication:
    async def test_proxy_token_missing_returns_403(self, started_proxy: JiraAuthProxy):
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{started_proxy.port}/test") as resp,
        ):
            assert resp.status == 403

    async def test_proxy_token_wrong_returns_403(self, started_proxy: JiraAuthProxy):
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"http://127.0.0.1:{started_proxy.port}/test",
                headers={"X-Summon-Proxy-Token": "wrong-token"},
            ) as resp,
        ):
            assert resp.status == 403

    async def test_proxy_token_correct_proceeds(self, started_proxy: JiraAuthProxy):
        """Correct proxy token passes auth gate (proceeds to token check, not 403)."""
        # token_path is a real non-existent path, stat() raises FileNotFoundError -> 502
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"http://127.0.0.1:{started_proxy.port}/test",
                headers={"X-Summon-Proxy-Token": started_proxy.access_token},
            ) as resp,
        ):
            assert resp.status != 403


# ---------------------------------------------------------------------------
# Request forwarding tests
# ---------------------------------------------------------------------------


class TestRequestForwarding:
    async def test_request_forwarding(self, proxy: JiraAuthProxy):
        """Method, path, query string, body, and Authorization forwarded upstream."""
        received: dict[str, Any] = {}

        async def _upstream_handler(request: web.Request) -> web.Response:
            received["method"] = request.method
            received["path"] = request.path
            received["query"] = request.query_string
            received["body"] = await request.read()
            received["auth"] = request.headers.get("Authorization")
            return web.Response(status=200, text="ok")

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{path_info:.*}", _upstream_handler)

        proxy._token_path = _make_mock_path(mtime=1000.0)
        token_data = _make_token_data(access_token="my-access-token")

        async with TestServer(upstream_app) as upstream_server:
            with (
                patch(
                    "summon_claude.jira_proxy._TARGET_URL",
                    f"http://127.0.0.1:{upstream_server.port}",
                ),
                patch(
                    "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                    new_callable=AsyncMock,
                ),
                patch(
                    "summon_claude.jira_proxy.load_jira_token",
                    return_value=token_data,
                ),
            ):
                port = await proxy.start()
                try:
                    async with (
                        aiohttp.ClientSession() as session,
                        session.post(
                            f"http://127.0.0.1:{port}/api/v1/resource?key=val",
                            headers={"X-Summon-Proxy-Token": proxy.access_token},
                            data=b"request-body",
                        ) as resp,
                    ):
                        assert resp.status == 200
                finally:
                    await proxy.stop()

        assert received["method"] == "POST"
        assert received["path"] == "/api/v1/resource"
        assert received["query"] == "key=val"
        assert received["body"] == b"request-body"
        assert received["auth"] == "Bearer my-access-token"

    async def test_proxy_token_not_forwarded_upstream(self, proxy: JiraAuthProxy):
        """X-Summon-Proxy-Token header must not appear in the upstream request."""
        received_headers: dict[str, str] = {}

        async def _upstream_handler(request: web.Request) -> web.Response:
            received_headers.update(dict(request.headers))
            return web.Response(status=200, text="ok")

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{path_info:.*}", _upstream_handler)

        proxy._token_path = _make_mock_path(mtime=1000.0)
        token_data = _make_token_data()

        async with TestServer(upstream_app) as upstream_server:
            with (
                patch(
                    "summon_claude.jira_proxy._TARGET_URL",
                    f"http://127.0.0.1:{upstream_server.port}",
                ),
                patch(
                    "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                    new_callable=AsyncMock,
                ),
                patch(
                    "summon_claude.jira_proxy.load_jira_token",
                    return_value=token_data,
                ),
            ):
                port = await proxy.start()
                try:
                    async with (
                        aiohttp.ClientSession() as session,
                        session.get(
                            f"http://127.0.0.1:{port}/test",
                            headers={"X-Summon-Proxy-Token": proxy.access_token},
                        ) as resp,
                    ):
                        assert resp.status == 200
                finally:
                    await proxy.stop()

        assert "X-Summon-Proxy-Token" not in received_headers
        assert "x-summon-proxy-token" not in {k.lower() for k in received_headers}

    async def test_response_streaming(self, proxy: JiraAuthProxy):
        """Response status and body are preserved from upstream."""

        async def _upstream_handler(request: web.Request) -> web.Response:
            return web.Response(status=201, text="created-body")

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{path_info:.*}", _upstream_handler)

        proxy._token_path = _make_mock_path(mtime=1000.0)
        token_data = _make_token_data()

        async with TestServer(upstream_app) as upstream_server:
            with (
                patch(
                    "summon_claude.jira_proxy._TARGET_URL",
                    f"http://127.0.0.1:{upstream_server.port}",
                ),
                patch(
                    "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                    new_callable=AsyncMock,
                ),
                patch(
                    "summon_claude.jira_proxy.load_jira_token",
                    return_value=token_data,
                ),
            ):
                port = await proxy.start()
                try:
                    async with (
                        aiohttp.ClientSession() as session,
                        session.get(
                            f"http://127.0.0.1:{port}/test",
                            headers={"X-Summon-Proxy-Token": proxy.access_token},
                        ) as resp,
                    ):
                        assert resp.status == 201
                        body = await resp.text()
                        assert body == "created-body"
                finally:
                    await proxy.stop()


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_upstream_network_error_returns_502(self, proxy: JiraAuthProxy):
        """ClientError from upstream -> 502."""
        proxy._token_path = _make_mock_path(mtime=1000.0)
        token_data = _make_token_data()

        port = await proxy.start()
        try:
            with (
                patch(
                    "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                    new_callable=AsyncMock,
                ),
                patch(
                    "summon_claude.jira_proxy.load_jira_token",
                    return_value=token_data,
                ),
            ):
                mock_session = MagicMock()
                mock_session.close = AsyncMock()
                mock_ctx = MagicMock()
                mock_ctx.__aenter__ = AsyncMock(
                    side_effect=aiohttp.ClientConnectionError("refused")
                )
                mock_ctx.__aexit__ = AsyncMock(return_value=False)
                mock_session.request = MagicMock(return_value=mock_ctx)
                proxy._http_session = mock_session

                async with (
                    aiohttp.ClientSession() as session,
                    session.get(
                        f"http://127.0.0.1:{port}/test",
                        headers={"X-Summon-Proxy-Token": proxy.access_token},
                    ) as resp,
                ):
                    assert resp.status == 502
        finally:
            await proxy.stop()

    async def test_upstream_timeout_returns_504(self, proxy: JiraAuthProxy):
        """TimeoutError from upstream -> 504."""
        proxy._token_path = _make_mock_path(mtime=1000.0)
        token_data = _make_token_data()

        port = await proxy.start()
        try:
            with (
                patch(
                    "summon_claude.jira_proxy.refresh_jira_token_if_needed",
                    new_callable=AsyncMock,
                ),
                patch(
                    "summon_claude.jira_proxy.load_jira_token",
                    return_value=token_data,
                ),
            ):
                mock_session = MagicMock()
                mock_session.close = AsyncMock()
                mock_ctx = MagicMock()
                mock_ctx.__aenter__ = AsyncMock(side_effect=TimeoutError())
                mock_ctx.__aexit__ = AsyncMock(return_value=False)
                mock_session.request = MagicMock(return_value=mock_ctx)
                proxy._http_session = mock_session

                async with (
                    aiohttp.ClientSession() as session,
                    session.get(
                        f"http://127.0.0.1:{port}/test",
                        headers={"X-Summon-Proxy-Token": proxy.access_token},
                    ) as resp,
                ):
                    assert resp.status == 504
        finally:
            await proxy.stop()

    async def test_jira_token_unavailable_returns_502(self, started_proxy: JiraAuthProxy):
        """No Jira token file -> 502 before forwarding."""
        # _token_path is a real non-existent path (set in proxy fixture)
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                f"http://127.0.0.1:{started_proxy.port}/test",
                headers={"X-Summon-Proxy-Token": started_proxy.access_token},
            ) as resp,
        ):
            assert resp.status == 502
            text = await resp.text()
            assert "unavailable" in text

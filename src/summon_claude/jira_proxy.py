"""Daemon-level aiohttp reverse proxy for Jira MCP with transparent token refresh."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
import time

import aiohttp
from aiohttp import web

from summon_claude.jira_auth import (
    _REFRESH_BUFFER_SECONDS,
    get_jira_token_path,
    load_jira_token,
    refresh_jira_token_if_needed,
)

__all__ = ["JiraAuthProxy"]

logger = logging.getLogger(__name__)

_TARGET_URL = "https://mcp.atlassian.com"


class JiraAuthProxy:
    """Reverse proxy for Jira MCP that transparently refreshes OAuth tokens."""

    def __init__(self) -> None:
        self._app = web.Application()
        self._app.router.add_route("*", "/{path_info:.*}", self._handle_request)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int = 0
        self._cached_token: str | None = None
        self._token_expires_at: float = 0
        self._token_file_mtime: float = 0.0
        self._token_path = get_jira_token_path()
        self._http_session: aiohttp.ClientSession | None = None
        self._refresh_lock = asyncio.Lock()
        # SEC-PROXY-04: random access token for proxy authentication
        self._proxy_access_token: str = secrets.token_urlsafe(32)

    @property
    def access_token(self) -> str:
        """Proxy access token for SessionOptions propagation."""
        return self._proxy_access_token

    @property
    def port(self) -> int:
        """Bound port. Raises RuntimeError if not started."""
        if self._port == 0:
            raise RuntimeError("Proxy not started")
        return self._port

    async def start(self) -> int:
        """Start the proxy on an ephemeral localhost port. Returns the bound port."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        # SEC-PROXY-01: bind to 127.0.0.1 only — never 0.0.0.0
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # Extract bound port from runner addresses (private API — aiohttp doesn't
        # expose the bound port via a public API as of 3.x)
        addrs = self._runner.addresses
        if addrs:
            self._port = addrs[0][1]  # (host, port) tuple
        # SEC-DR-001: limit concurrent upstream connections
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            connector=aiohttp.TCPConnector(limit=20),
        )
        return self._port

    async def stop(self) -> None:
        """Stop the proxy. Idempotent — safe to call multiple times."""
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._port = 0

    async def _get_fresh_token(self) -> str | None:
        """Get a fresh Jira access token, refreshing if needed.

        Hot path (cache hit + mtime check) is lock-free.
        Refresh path serializes via asyncio.Lock to prevent redundant HTTP calls.
        SEC-PROXY-02: token is only held in memory, never logged.
        """
        # Check token file mtime — invalidate cache if file changed on disk
        try:
            current_mtime = self._token_path.stat().st_mtime
        except FileNotFoundError:
            return None

        if current_mtime != self._token_file_mtime:
            self._token_expires_at = 0  # Force re-read

        # Hot path: cache is still fresh
        if time.time() < self._token_expires_at - _REFRESH_BUFFER_SECONDS:
            return self._cached_token

        # Cold path: need refresh — serialize concurrent attempts
        async with self._refresh_lock:
            # Double-check after acquiring lock (another caller may have refreshed)
            if time.time() < self._token_expires_at - _REFRESH_BUFFER_SECONDS:
                return self._cached_token

            # SEC-PROXY-03: reuse existing flock-based refresh
            try:
                await refresh_jira_token_if_needed()
            except Exception:
                logger.warning("Jira proxy: token refresh failed")

            token_data = load_jira_token()
            if token_data is None:
                # Backoff: avoid hammering the OAuth server on repeated failures.
                # The hot-path check uses (expires_at - _REFRESH_BUFFER_SECONDS),
                # so set expires_at far enough ahead to cover the buffer.
                self._cached_token = None
                self._token_expires_at = time.time() + _REFRESH_BUFFER_SECONDS + 60
                return None

            self._cached_token = token_data.get("access_token")
            self._token_expires_at = token_data.get("expires_at", time.time() + 3600)
            with contextlib.suppress(FileNotFoundError):
                self._token_file_mtime = self._token_path.stat().st_mtime
            return self._cached_token

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        """Catch-all handler: validate proxy token, get fresh Jira token, forward request."""
        # SEC-PROXY-04: validate proxy access token
        proxy_token = request.headers.get("X-Summon-Proxy-Token")
        if proxy_token is None or not hmac.compare_digest(proxy_token, self._proxy_access_token):
            return web.Response(status=403, text="Forbidden")

        # Get fresh Jira token
        token = await self._get_fresh_token()
        if token is None:
            return web.Response(status=502, text="Jira token unavailable")

        # Build upstream URL
        url = f"{_TARGET_URL}{request.path_qs}"

        # Build forwarded headers
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() not in ("host", "x-summon-proxy-token"):
                headers[key] = value
        headers["Host"] = "mcp.atlassian.com"
        headers["Authorization"] = f"Bearer {token}"
        # SEC-PROXY-02: do NOT log headers dict (contains Bearer token)

        # Read request body
        body = await request.read()

        # SEC-PROXY-02: log method+path only, never headers
        logger.debug("Jira proxy: forwarding %s %s", request.method, request.path)

        try:
            if self._http_session is None:
                raise RuntimeError("JiraProxy: http session not started")
            async with self._http_session.request(
                request.method, url, headers=headers, data=body
            ) as upstream:
                # Stream response back — handles both HTTP and SSE transparently
                response = web.StreamResponse(status=upstream.status)
                # Copy headers except Transfer-Encoding and Content-Length
                # (aiohttp manages these for chunked streaming)
                for key, value in upstream.headers.items():
                    if key.lower() not in ("transfer-encoding", "content-length"):
                        response.headers[key] = value
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
        except aiohttp.ClientError as e:
            logger.warning("Jira proxy: upstream error: %s", type(e).__name__)
            return web.Response(status=502, text="Jira MCP server unreachable")
        except TimeoutError:
            logger.warning("Jira proxy: upstream timeout")
            return web.Response(status=504, text="Jira MCP server timeout")

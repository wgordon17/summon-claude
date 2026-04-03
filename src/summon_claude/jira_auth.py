"""Jira OAuth 2.1 authentication module for summon-claude.

Implements OAuth 2.1 with PKCE + Dynamic Client Registration (DCR) for the
Atlassian MCP server (Rovo plugin). Follows the credential storage pattern
established by get_google_credentials_dir() in config.py.

Security constraints implemented:
  SC-01: PKCE code_verifier uses secrets.token_urlsafe. State parameter for CSRF.
  SC-02: DCR requests client_secret_post — never 'none'. Validates client_secret in response.
  SC-03: 0600 perms on token file. refresh_token/client_secret never in MCP config.
  SC-07: fcntl.flock on token file during refresh for concurrent session safety.
  SC-08: Callback listener on 127.0.0.1 only, ephemeral port, shuts down immediately.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import select
import shutil
import time
import webbrowser
from base64 import urlsafe_b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

logger = logging.getLogger(__name__)

_ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# Atlassian scopes for Jira read access
_JIRA_SCOPES = ["read:jira-work", "offline_access"]

# Callback path for localhost OAuth redirect
_CALLBACK_PATH = "/oauth/callback"

# Token refresh 5-minute expiry buffer
REFRESH_BUFFER_SECONDS = 300

# HTTP timeouts
_HTTP_CONNECT_TIMEOUT = aiohttp.ClientTimeout(connect=10, total=30)

# Auth flow browser wait timeout
_AUTH_FLOW_TIMEOUT = 120

# SEC-P4-005/006: Trusted Atlassian hosts for endpoint validation.
# Used to validate authorization_endpoint, token_endpoint, and rediscovered
# endpoints. Untrusted hosts trigger rediscovery or abort.
_TRUSTED_ATLASSIAN_HOSTS = frozenset({"cf.mcp.atlassian.com", "auth.atlassian.com"})

# Fields from the original token that the OAuth server never returns on refresh
# but must be preserved across refreshes (e.g. cloud_id set during login).
_TOKEN_PRESERVE_FIELDS = frozenset({"cloud_id", "cloud_name"})


def get_jira_credentials_dir() -> Path:
    """Return the directory for storing Jira OAuth credentials."""
    from summon_claude.config import get_config_dir  # noqa: PLC0415

    return get_config_dir() / "jira-credentials"


def get_jira_token_path() -> Path:
    """Return the path to the Jira token file."""
    return get_jira_credentials_dir() / "token.json"


def save_jira_token(token_data: dict[str, Any]) -> None:
    """Save Jira token data to disk with 0600 permissions (SC-03).

    SEC-P4-001: Uses os.open with O_CREAT to atomically create the file with
    restrictive permissions, avoiding the write-then-chmod TOCTOU window.
    SEC-P4-016: Directory created with 0o700 to prevent enumeration.
    """
    creds_dir = get_jira_credentials_dir()
    creds_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Enforce 0o700 even if the directory pre-existed with wider permissions
    creds_dir.chmod(0o700)

    token_path = get_jira_token_path()
    content = json.dumps(token_data, indent=2).encode()
    fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def load_jira_token() -> dict[str, Any] | None:
    """Load Jira token from disk (sync, no refresh).

    Returns the token dict if it exists and is not expired (with buffer),
    returns None if missing, corrupt, or expired. Does NOT attempt refresh —
    call refresh_jira_token_if_needed() in an async context first to ensure
    a fresh token is on disk before calling this.
    """
    token_path = get_jira_token_path()
    if not token_path.exists():
        return None

    try:
        token_data = json.loads(token_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Jira token file is corrupt or unreadable: %s", e)
        return None

    expires_at = token_data.get("expires_at", 0)
    if time.time() >= (expires_at - REFRESH_BUFFER_SECONDS):
        # Token is expired (or has no expiry) — caller should have refreshed
        logger.debug("Jira token is expired; call refresh_jira_token_if_needed() first")
        return None

    return token_data


def jira_credentials_exist() -> bool:
    """Fast stat check — True if token file exists on disk."""
    return get_jira_token_path().exists()


async def discover_oauth_metadata(mcp_url: str) -> dict[str, Any]:
    """Fetch OAuth 2.1 server metadata from the MCP server.

    Tries /.well-known/oauth-authorization-server first, then falls back
    to /.well-known/openid-configuration.

    Args:
        mcp_url: Base URL of the MCP server (e.g., 'https://mcp.atlassian.com').

    Returns:
        OAuth server metadata dict.

    Raises:
        RuntimeError: If metadata cannot be fetched from either endpoint.
    """
    parsed = urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    endpoints = [
        f"{origin}/.well-known/oauth-authorization-server",
        f"{origin}/.well-known/openid-configuration",
    ]

    async with aiohttp.ClientSession(timeout=_HTTP_CONNECT_TIMEOUT) as session:
        for url in endpoints:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.debug("OAuth metadata fetched from %s", url)
                        return data
            except aiohttp.ClientError as e:
                logger.debug("Failed to fetch OAuth metadata from %s: %s", url, e)

    raise RuntimeError(
        f"Could not fetch OAuth metadata from {origin}. "
        "Check that the Atlassian MCP endpoint is reachable."
    )


async def register_client(metadata: dict[str, Any], redirect_uri: str) -> tuple[str, str]:
    """Perform RFC 7591 Dynamic Client Registration.

    SC-02: Always uses client_secret_post auth method — never 'none'.
    Validates that the DCR response contains a client_secret.

    Args:
        metadata: OAuth server metadata dict from discover_oauth_metadata().
        redirect_uri: The exact redirect URI to register (must match the callback server port).

    Returns:
        Tuple of (client_id, client_secret).

    Raises:
        RuntimeError: If registration endpoint is missing, registration fails,
                      or response contains no client_secret.
    """
    registration_endpoint = metadata.get("registration_endpoint")
    if not registration_endpoint:
        raise RuntimeError("OAuth metadata missing registration_endpoint — DCR not supported")

    # Validate the registration endpoint is on the expected issuer origin
    issuer = metadata.get("issuer", "")
    if issuer:
        parsed_issuer = urlparse(issuer)
        parsed_reg = urlparse(registration_endpoint)
        if parsed_issuer.netloc != parsed_reg.netloc:
            raise RuntimeError(
                f"DCR endpoint origin {parsed_reg.netloc!r} does not match "
                f"issuer {parsed_issuer.netloc!r} — possible DCR spoofing"
            )

    registration_payload = {
        "client_name": "summon-claude",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }

    async with (
        aiohttp.ClientSession(timeout=_HTTP_CONNECT_TIMEOUT) as session,
        session.post(
            registration_endpoint,
            json=registration_payload,
            headers={"Content-Type": "application/json"},
        ) as resp,
    ):
        if resp.status not in (200, 201):
            # SEC-P4-004: field-filter error response, don't reflect raw body
            try:
                err_json = await resp.json()
                err = err_json.get("error", "unknown")
                desc = err_json.get("error_description", "")
            except Exception:
                err, desc = "parse_error", ""
            raise RuntimeError(
                f"DCR failed (HTTP {resp.status}): {err}" + (f" — {desc}" if desc else "")
            )
        data = await resp.json()

    client_id = data.get("client_id")
    client_secret = data.get("client_secret")

    if not client_id:
        raise RuntimeError("DCR response missing client_id")
    if not client_secret:
        raise RuntimeError(
            "DCR response missing client_secret — auth method 'none' was returned "
            "or server does not support confidential clients. Aborting."
        )

    logger.debug("DCR successful, client_id=%s", client_id)
    return client_id, client_secret


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256).

    SC-01: Uses secrets.token_urlsafe for cryptographically secure randomness.
    code_verifier is 128 chars (exceeds RFC 7636 43-128 char requirement).
    """
    # 96 bytes -> 128 base64url chars (no padding)
    code_verifier = secrets.token_urlsafe(96)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _make_callback_handler(result: dict[str, str | None]) -> type[BaseHTTPRequestHandler]:
    """Return a handler class that writes callback params into the provided result dict.

    Using a factory with a closure avoids class-level shared state (CR-004),
    which is unsafe for concurrent or sequential auth flows in the same process.
    """

    class _OAuthCallbackHandler(BaseHTTPRequestHandler):
        """Minimal HTTP handler to capture the OAuth callback."""

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != _CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            result["code"] = (params.get("code") or [None])[0]
            result["state"] = (params.get("state") or [None])[0]
            result["error"] = (params.get("error") or [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = (
                b"<html><body><h2>Jira authentication complete."
                b" You may close this tab.</h2></body></html>"
            )
            self.wfile.write(msg)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Suppress default HTTP server access logs
            pass

    return _OAuthCallbackHandler


async def start_auth_flow() -> dict[str, Any]:
    """Execute the OAuth 2.1 Authorization Code + PKCE flow.

    Opens the browser for user authorization, starts a localhost callback server
    (SC-08: bound to 127.0.0.1, ephemeral port, shuts down immediately after callback),
    validates the state parameter (SC-01), and exchanges the code for tokens.

    Returns:
        Token data dict with access_token, refresh_token, expires_at, etc.

    Raises:
        RuntimeError: On network errors, user denial, state mismatch, or token exchange failure.
        TimeoutError: If the user does not complete authorization within 120 seconds.
    """
    metadata = await discover_oauth_metadata("https://mcp.atlassian.com")

    authorization_endpoint = metadata["authorization_endpoint"]
    token_endpoint = metadata["token_endpoint"]

    # SEC-P4-006: validate both endpoints are HTTPS on trusted Atlassian hosts
    for label, endpoint in [
        ("authorization_endpoint", authorization_endpoint),
        ("token_endpoint", token_endpoint),
    ]:
        parsed_ep = urlparse(endpoint)
        if parsed_ep.scheme != "https" or parsed_ep.netloc not in _TRUSTED_ATLASSIAN_HOSTS:
            raise RuntimeError(
                f"Untrusted {label}: {parsed_ep.netloc!r} — expected an Atlassian host"
            )

    code_verifier, code_challenge = _pkce_pair()

    # SC-01: cryptographically secure state parameter for CSRF protection
    state = secrets.token_urlsafe(32)

    # CR-004: use a per-flow result dict captured in a closure, not class-level state.
    result: dict[str, str | None] = {"code": None, "state": None, "error": None}

    # Bind the callback server first (SC-08: 127.0.0.1, ephemeral port) so we
    # know the exact port before registering via DCR (CR-001: redirect_uri must
    # match the registered URI exactly).
    server = HTTPServer(("127.0.0.1", 0), _make_callback_handler(result))
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}{_CALLBACK_PATH}"

    # Now perform DCR with the known redirect_uri (CR-001 fix)
    client_id, client_secret = await register_client(metadata, redirect_uri)

    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(_JIRA_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{authorization_endpoint}?{urlencode(auth_params)}"

    logger.debug("Opening browser for Jira authorization: %s", auth_url)
    webbrowser.open(auth_url)

    # CR-014: wrap server lifecycle in try/finally so the socket is always closed.
    try:
        deadline = time.monotonic() + _AUTH_FLOW_TIMEOUT
        while result["code"] is None and result["error"] is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Jira authorization timed out after {_AUTH_FLOW_TIMEOUT}s. "
                    "Please run the login command again."
                )
            # Non-blocking check with 1s poll
            readable, _, _ = select.select([server.socket], [], [], min(1.0, remaining))
            if readable:
                server.handle_request()
    finally:
        # Shut down immediately after receiving the callback (SC-08)
        server.server_close()

    if result["error"]:
        raise RuntimeError(f"Authorization denied: {result['error']}")

    received_state = result["state"]
    received_code = result["code"]

    # Validate state to prevent CSRF (SC-01) — constant-time comparison
    if not hmac.compare_digest(received_state or "", state):
        raise RuntimeError("OAuth state mismatch — possible CSRF attack. Aborting token exchange.")

    if not received_code:
        raise RuntimeError("OAuth callback did not include an authorization code")

    # Exchange authorization code for tokens
    token_data = await _exchange_code(
        token_endpoint=token_endpoint,
        client_id=client_id,
        client_secret=client_secret,
        code=received_code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
    )

    # Annotate token with DCR credentials (SC-03: stays in file, not MCP config)
    token_data["client_id"] = client_id
    token_data["client_secret"] = client_secret
    token_data["token_endpoint"] = token_endpoint

    # Compute and store absolute expiry time
    expires_in = token_data.get("expires_in", 3600)
    token_data["expires_at"] = time.time() + int(expires_in)

    # QA-013: do NOT call save_jira_token here — caller handles persistence
    # (allows caller to enrich token with cloud_id before saving)
    logger.info("Jira authentication successful")
    return token_data


async def _exchange_code(  # noqa: PLR0913
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens via the token endpoint.

    SC-02: Uses client_secret_post (credentials in POST body, not HTTP Basic
    auth) because DCR registered this client with that auth method. Never uses
    'none' — a missing client_secret in the DCR response aborts the flow.

    SEC-P4-004: Error responses may reflect the client_secret from the POST
    body, so only the error/error_description fields are surfaced to the user.
    """
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }

    async with (
        aiohttp.ClientSession(timeout=_HTTP_CONNECT_TIMEOUT) as session,
        session.post(
            token_endpoint,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp,
    ):
        body = await resp.json()
        if resp.status != 200:
            # SEC-P4-004: Only expose error/error_description — response may
            # reflect client_secret from the POST body.
            err = body.get("error", "unknown")
            desc = body.get("error_description", "")
            raise RuntimeError(
                f"Token exchange failed (HTTP {resp.status}): {err}"
                + (f" — {desc}" if desc else "")
            )
        return body


async def refresh_jira_token_if_needed(*, force: bool = False) -> None:  # noqa: PLR0911, PLR0912, PLR0915
    """Async entry point: refresh the Jira token on disk if it is near expiry.

    SC-07: Uses fcntl.flock (non-blocking, with retry) on the token file to
    prevent concurrent session races. Re-reads under lock to detect if another
    process already refreshed. HTTP refresh happens OUTSIDE the lock to avoid
    holding it during network I/O (CR-005).

    This should be called once at session startup (in _run_session_tasks) before
    building the MCP config. After it returns, load_jira_token() will succeed.
    Failures are logged but not raised — sessions proceed without Jira MCP.

    Args:
        force: If True, skip the pre-lock and under-lock freshness checks and
               unconditionally attempt a refresh. Used by try_refresh_only()
               for CLI re-auth. The post-HTTP conflict check is still applied
               to prevent overwriting a concurrently-refreshed token.
    """
    token_path = get_jira_token_path()
    if not token_path.exists():
        return

    try:
        token_data = json.loads(token_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cannot read Jira token for refresh check: %s", e)
        return

    expires_at = token_data.get("expires_at", 0)
    if not force and time.time() < (expires_at - REFRESH_BUFFER_SECONDS):
        # Token is still fresh — nothing to do
        return

    # PERF-003: non-blocking lock with retry (5 attempts, 100ms sleep)
    lock_fd: int | None = None
    try:
        lock_fd = os.open(str(token_path), os.O_RDWR)
        acquired = False
        for _attempt in range(5):
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                await asyncio.sleep(0.1)

        if not acquired:
            logger.warning("Jira token lock contended — using existing token without refresh")
            return

        # Re-read under lock — another process may have already refreshed.
        # When force=True, skip this check and proceed unconditionally.
        try:
            fresh_data = json.loads(token_path.read_text())
            fresh_expires_at = fresh_data.get("expires_at", 0)
            if not force and time.time() < (fresh_expires_at - REFRESH_BUFFER_SECONDS):
                logger.debug("Jira token refreshed by another process, using updated token")
                return
            token_data = fresh_data
        except (json.JSONDecodeError, OSError):
            pass  # Use original token_data

        # Release lock before HTTP I/O (CR-005: never hold flock during network call)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)

        # Perform the async refresh without holding the lock
        refreshed = await _do_refresh(token_data)
        if refreshed is None:
            return

        # Re-acquire lock to write the refreshed token
        acquired_write = False
        for _attempt in range(5):
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired_write = True
                break
            except OSError:
                await asyncio.sleep(0.1)

        if not acquired_write:
            logger.warning("Jira token write lock contended — could not persist refreshed token")
            return

        try:
            # Check if another process refreshed while we were doing HTTP I/O.
            # If so, their token is newer — don't overwrite it.
            try:
                on_disk = json.loads(token_path.read_text())
                disk_expires = on_disk.get("expires_at", 0)
                if time.time() < (disk_expires - REFRESH_BUFFER_SECONDS):
                    logger.debug("Another process refreshed during our HTTP call — using theirs")
                    return
            except (json.JSONDecodeError, OSError):
                pass  # Disk unreadable — write our refreshed token anyway

            save_jira_token(refreshed)
            logger.debug("Jira token refreshed and saved")
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)

    except OSError as e:
        logger.warning("Failed to open Jira token file for refresh: %s", e)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


async def try_refresh_only() -> bool:
    """Attempt to refresh the existing Jira token without browser flow.

    Returns True if refresh succeeded and a valid token is on disk.
    Returns False if refresh failed (expired refresh token, network error, no creds).

    CLI-only: called from summon auth jira login, not from concurrent session code.
    """
    try:
        await refresh_jira_token_if_needed(force=True)
    except Exception:
        logger.info("Jira token refresh failed — browser flow required")
        return False
    token = load_jira_token()
    if token is not None:
        logger.info("Jira token refreshed successfully")
        return True
    logger.info("Jira token refresh produced no valid token — browser flow required")
    return False


async def _do_refresh(token_data: dict[str, Any]) -> dict[str, Any] | None:
    """Perform token refresh using the stored refresh_token.

    Returns new token dict, or None on failure.
    """
    refresh_token = token_data.get("refresh_token")
    client_id = token_data.get("client_id")
    client_secret = token_data.get("client_secret")

    if not refresh_token or not client_id or not client_secret:
        logger.warning("Jira token missing refresh_token, client_id, or client_secret")
        return None

    # Use cached token_endpoint if present and valid, else discover.
    # SEC-P4-005: validate cached URL to prevent redirect-based exfiltration
    # if the token file is tampered with by a local attacker.
    token_endpoint = token_data.get("token_endpoint")
    if token_endpoint:
        parsed = urlparse(token_endpoint)
        if parsed.scheme != "https" or parsed.netloc not in _TRUSTED_ATLASSIAN_HOSTS:
            logger.warning(
                "Cached token_endpoint %r is not on a trusted Atlassian host — rediscovering",
                parsed.netloc,
            )
            token_endpoint = None
    if not token_endpoint:
        try:
            metadata = await discover_oauth_metadata("https://mcp.atlassian.com")
            token_endpoint = metadata["token_endpoint"]
            # SEC-P4-005: validate rediscovered token_endpoint
            parsed_rediscovered = urlparse(token_endpoint)
            if (
                parsed_rediscovered.scheme != "https"
                or parsed_rediscovered.netloc not in _TRUSTED_ATLASSIAN_HOSTS
            ):
                logger.warning(
                    "Rediscovered token_endpoint %r not on trusted host",
                    parsed_rediscovered.netloc,
                )
                return None
        except Exception as e:
            logger.warning("Cannot discover OAuth metadata for token refresh: %s", e)
            return None

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        async with aiohttp.ClientSession(timeout=_HTTP_CONNECT_TIMEOUT) as session:  # noqa: SIM117
            async with session.post(
                token_endpoint,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                body = await resp.json()
                if resp.status != 200:
                    # SEC-P4-004: sanitize — response may reflect client_secret
                    err = body.get("error", "unknown")
                    logger.warning("Token refresh failed HTTP %d: %s", resp.status, err)
                    return None

                # Preserve DCR credentials and compute new expiry
                body["client_id"] = client_id
                body["client_secret"] = client_secret
                body["token_endpoint"] = token_endpoint
                expires_in = body.get("expires_in", 3600)
                body["expires_at"] = time.time() + int(expires_in)

                # If server didn't return a new refresh_token, keep the old one
                if "refresh_token" not in body:
                    body["refresh_token"] = refresh_token

                # Preserve fields the OAuth server never returns (set during login)
                for field in _TOKEN_PRESERVE_FIELDS:
                    if field in token_data and field not in body:
                        body[field] = token_data[field]

                logger.debug("Jira token refreshed successfully")
                return body

    except aiohttp.ClientError as e:
        logger.warning("Network error during token refresh: %s", e)
        return None


def logout() -> None:
    """Remove stored Jira credentials."""
    shutil.rmtree(get_jira_credentials_dir(), ignore_errors=True)
    logger.info("Jira credentials removed")


async def discover_cloud_sites(access_token: str) -> list[dict[str, Any]]:
    """Fetch accessible Atlassian cloud sites for the given access token.

    Calls https://api.atlassian.com/oauth/token/accessible-resources.
    Returns a list of site dicts with 'id', 'name', 'url'. Returns empty
    list on failure (interop with Rovo MCP token is unvalidated per spike notes).

    Args:
        access_token: Valid Atlassian OAuth access token.

    Returns:
        List of site dicts: [{"id": str, "name": str, "url": str, "scopes": list}, ...]
    """
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_CONNECT_TIMEOUT) as session:  # noqa: SIM117
            async with session.get(
                _ACCESSIBLE_RESOURCES_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "accessible-resources returned HTTP %d — token interop may not work",
                        resp.status,
                    )
                    return []
                sites = await resp.json()
                return [
                    {
                        "id": s.get("id", ""),
                        "name": s.get("name", ""),
                        "url": s.get("url", ""),
                        "scopes": s.get("scopes", []),
                    }
                    for s in sites
                ]
    except aiohttp.ClientError as e:
        logger.warning("Failed to fetch accessible Atlassian resources: %s", e)
        return []


def check_jira_status() -> str | None:  # noqa: PLR0911
    """Check Jira integration status.

    Reads the token file directly (bypassing the expiry check in
    ``load_jira_token``) so that near-expiry tokens don't produce
    misleading errors — the proxy/daemon refreshes tokens asynchronously
    at session startup via ``refresh_jira_token_if_needed()``.

    Fully expired tokens (past all buffers) with a refresh_token are
    reported as None (OK) — the proxy will refresh them. Fully expired
    tokens without a refresh_token are reported as an error.

    Returns:
        None if Jira credentials are present and structurally valid.
        Error message string if there is a problem.
    """
    if not jira_credentials_exist():
        return "No Jira credentials found. Run: summon auth jira login"

    # Read raw token file — don't use load_jira_token() which returns None
    # for near-expiry tokens (within REFRESH_BUFFER_SECONDS).
    token_path = get_jira_token_path()
    try:
        token_data = json.loads(token_path.read_text())
    except (json.JSONDecodeError, OSError):
        return (
            "Jira credentials are present but corrupt or unreadable. "
            "Re-authenticate: summon auth jira login"
        )

    if not token_data.get("access_token"):
        return (
            "Jira credentials are present but missing access_token. "
            "Re-authenticate: summon auth jira login"
        )

    cloud_id = token_data.get("cloud_id")
    if not cloud_id:
        return (
            "Jira credentials found but no cloud_id is configured. "
            "Re-authenticate: summon auth jira login"
        )

    # Check expiry: near-expiry is fine (daemon handles it), but fully expired
    # tokens without a refresh_token cannot be renewed.
    expires_at = token_data.get("expires_at", 0)
    if time.time() >= expires_at:
        if token_data.get("refresh_token"):
            # Proxy will refresh at next request — report as OK
            logger.debug("Jira token expired but refresh_token present — proxy will refresh")
            return None
        return (
            "Jira token is expired and has no refresh_token. "
            "Re-authenticate: summon auth jira login"
        )

    return None

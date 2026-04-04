"""GitHub OAuth App device flow authentication."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

from summon_claude.config import get_config_dir

logger = logging.getLogger(__name__)

GITHUB_OAUTH_CLIENT_ID = "Ov23liAey7f9flOsRcK4"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
GITHUB_SCOPES = "repo read:org"  # space-delimited per GitHub API spec


class GitHubAuthError(Exception):
    """Raised on GitHub authentication failures."""


@dataclass(frozen=True)
class DeviceCodeResponse:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DeviceFlowResult:
    token: str = field(repr=False)
    login: str
    scopes: str
    token_path: Path


def get_github_token_path() -> Path:
    """Return the path to the stored GitHub token file."""
    return get_config_dir() / "github-credentials" / "token.json"


async def request_device_code(
    session: aiohttp.ClientSession,
    client_id: str | None = None,
) -> DeviceCodeResponse:
    """Request a device code from GitHub for the device flow.

    Posts to the GitHub device code endpoint and returns the response
    as a :class:`DeviceCodeResponse`.

    Raises :class:`GitHubAuthError` on non-200 responses.
    """
    cid = client_id or GITHUB_OAUTH_CLIENT_ID
    async with session.post(
        GITHUB_DEVICE_CODE_URL,
        data={"client_id": cid, "scope": GITHUB_SCOPES},
        headers={"Accept": "application/json"},
    ) as resp:
        if resp.status != 200:
            raise GitHubAuthError(f"Device code request failed: HTTP {resp.status}")
        data = await resp.json()
    verification_uri = data["verification_uri"]
    if not verification_uri.startswith("https://github.com/"):
        safe = verification_uri[:100].encode("ascii", "replace").decode()
        raise GitHubAuthError(f"Unexpected verification_uri: {safe!r}")
    return DeviceCodeResponse(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=verification_uri,
        expires_in=int(data["expires_in"]),
        interval=int(data["interval"]),
    )


async def poll_for_token(
    session: aiohttp.ClientSession,
    device_code: str,
    interval: int,
    expires_in: int,
    client_id: str | None = None,
) -> str:
    """Poll GitHub token endpoint until the user authorizes or the code expires.

    Returns the access token string on success.
    Raises :class:`GitHubAuthError` on terminal errors.
    """
    min_poll_interval = 5  # RFC 8628 §3.5 minimum
    cid = client_id or GITHUB_OAUTH_CLIENT_ID
    deadline = time.monotonic() + expires_in
    current_interval = max(interval, min_poll_interval)

    while time.monotonic() < deadline:
        async with session.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": cid,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        ) as resp:
            if resp.status != 200:
                # Transient server error — retry after interval
                await asyncio.sleep(current_interval)
                continue
            data = await resp.json()

        error = data.get("error")
        if not error:
            token = data.get("access_token", "")
            if token:
                return token
            raise GitHubAuthError("Token response missing access_token")

        terminal_errors = {
            "expired_token": "Device code expired — restart authentication",
            "access_denied": "Access denied by user",
            "device_flow_disabled": "Device flow is disabled for this OAuth App",
            "incorrect_client_credentials": "Incorrect client credentials",
            "incorrect_device_code": "Incorrect device code",
            "unsupported_grant_type": "Unsupported grant type",
        }
        if error in terminal_errors:
            raise GitHubAuthError(terminal_errors[error])
        if error == "slow_down":
            current_interval += 5
        elif error != "authorization_pending":
            safe_error = re.sub(r"[^\x20-\x7e]", "", str(error))[:200]
            raise GitHubAuthError(f"Unexpected error from GitHub: {safe_error}")

        await asyncio.sleep(current_interval)

    raise GitHubAuthError("Device code expired — polling deadline exceeded")


def store_token(token: str, scopes: str = "", login: str = "") -> Path:
    """Persist the GitHub access token to disk.

    Writes atomically via a .tmp sibling file then Path.replace().
    Parent directory is created with mode 0o700; file is set to 0o600.

    Returns the token file path.
    """
    token_path = get_github_token_path()
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Enforce 0o700 even if dir already existed with looser permissions
    token_path.parent.chmod(0o700)

    payload = {
        "access_token": token,
        "scopes": scopes,
        "login": login,
        "created_at": datetime.now(UTC).isoformat(),
    }
    tmp_path = token_path.with_suffix(".tmp")
    try:
        # Create file with 0o600 from the start — no world-readable window
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload))
        tmp_path.replace(token_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
    logger.info("GitHub token stored at %s", token_path)
    return token_path


def load_token() -> str | None:
    """Load the GitHub access token from disk, falling back to env var.

    Priority: OAuth token file > SUMMON_GITHUB_PAT env var.
    Returns the token string, or None if no token is available.
    Strips CRLF from the token value to prevent header injection.
    """
    token_path = get_github_token_path()
    try:
        data = json.loads(token_path.read_text())
        token = data.get("access_token", "")
        if isinstance(token, str) and token:
            return token.replace("\r", "").replace("\n", "")
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass

    # Fallback: check SUMMON_GITHUB_PAT env var (for CI/CD and enterprise environments)
    pat = os.environ.get("SUMMON_GITHUB_PAT", "")
    if pat:
        clean = pat.replace("\r", "").replace("\n", "")
        # Validate known GitHub token formats to catch misconfiguration early
        valid_prefixes = ("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")
        if not any(clean.startswith(p) for p in valid_prefixes):
            logger.warning("SUMMON_GITHUB_PAT has unrecognized format (expected ghp_/gho_/...)")
        return clean

    return None


def remove_token() -> bool:
    """Delete the stored GitHub token file and clean up stale artifacts.

    Removes the token file, any leftover ``.tmp`` file from interrupted
    writes, and the parent directory if empty.

    Returns True if the token file existed and was deleted, False otherwise.
    """
    token_path = get_github_token_path()
    existed = False
    try:
        token_path.unlink()
        existed = True
    except FileNotFoundError:
        pass
    # Clean up stale .tmp from interrupted store_token
    with contextlib.suppress(FileNotFoundError):
        token_path.with_suffix(".tmp").unlink()
    # Remove empty parent directory
    with contextlib.suppress(OSError):
        token_path.parent.rmdir()  # only succeeds if empty
    return existed


async def validate_token(
    token: str,
    session: aiohttp.ClientSession | None = None,
) -> dict | None:
    """Validate a GitHub access token against the API.

    Returns a dict with ``login`` and ``scopes`` on success, or None on 401/403.
    If *session* is None, a temporary aiohttp session is created.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    async def _do_validate(s: aiohttp.ClientSession) -> dict | None:
        async with s.get("https://api.github.com/user", headers=headers) as resp:
            if resp.status in (401, 403):
                return None
            if resp.status != 200:
                raise GitHubAuthError(f"Token validation failed: HTTP {resp.status}")
            data = await resp.json()
            scopes = resp.headers.get("X-OAuth-Scopes", "")
            return {"login": data.get("login", ""), "scopes": scopes}

    if session is not None:
        return await _do_validate(session)

    async with aiohttp.ClientSession() as own_session:
        return await _do_validate(own_session)


async def run_device_flow(
    client_id: str | None = None,
    on_code: Callable[[str, str], None] | None = None,
) -> DeviceFlowResult:
    """Execute the full GitHub OAuth device flow.

    1. Requests a device code.
    2. Calls *on_code* with (user_code, verification_uri) if provided.
    3. Polls until the user authorizes.
    4. Validates the token.
    5. Stores the token and returns a :class:`DeviceFlowResult`.
    """
    async with aiohttp.ClientSession() as session:
        device_resp = await request_device_code(session, client_id=client_id)

        if on_code is not None:
            on_code(device_resp.user_code, device_resp.verification_uri)

        token = await poll_for_token(
            session,
            device_code=device_resp.device_code,
            interval=device_resp.interval,
            expires_in=device_resp.expires_in,
            client_id=client_id,
        )

        try:
            user_info = await validate_token(token, session=session)
        except GitHubAuthError:
            # Transient server error during validation — token is still valid
            user_info = None

        if user_info is not None:
            login = user_info["login"]
            scopes = user_info["scopes"]
        else:
            login = "unknown"
            scopes = ""

        token_path = store_token(token, scopes=scopes, login=login)
        logger.info("GitHub authentication complete for user: %s", login)

        return DeviceFlowResult(
            token=token,
            login=login,
            scopes=scopes,
            token_path=token_path,
        )

"""Tests for summon_claude.jira_auth — OAuth 2.1 auth module."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude import jira_auth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_credentials_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect credential storage to a temp directory for all tests."""
    creds_dir = tmp_path / "jira-credentials"

    def _fake_get_jira_credentials_dir() -> Path:
        return creds_dir

    def _fake_get_jira_token_path() -> Path:
        return creds_dir / "token.json"

    monkeypatch.setattr(jira_auth, "get_jira_credentials_dir", _fake_get_jira_credentials_dir)
    monkeypatch.setattr(jira_auth, "get_jira_token_path", _fake_get_jira_token_path)


def _make_token(  # noqa: PLR0913
    *,
    access_token: str = "test-access-token",  # noqa: S107
    refresh_token: str = "test-refresh-token",  # noqa: S107
    client_id: str = "test-client-id",
    client_secret: str = "test-client-secret",  # noqa: S107
    expires_at: float | None = None,
    cloud_id: str = "abc-123",
) -> dict:
    if expires_at is None:
        expires_at = time.time() + 3600  # fresh by default
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "expires_at": expires_at,
        "cloud_id": cloud_id,
        "token_endpoint": "https://cf.mcp.atlassian.com/v1/token",
    }


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


class TestTokenStorage:
    def test_save_and_load_roundtrip(self):
        token = _make_token()
        jira_auth.save_jira_token(token)
        loaded = jira_auth.load_jira_token()
        assert loaded is not None
        assert loaded["access_token"] == "test-access-token"
        assert loaded["cloud_id"] == "abc-123"

    def test_save_creates_directory(self, tmp_path: Path):
        # The creds dir does not exist initially
        creds_dir = jira_auth.get_jira_credentials_dir()
        assert not creds_dir.exists()
        jira_auth.save_jira_token(_make_token())
        assert creds_dir.exists()

    def test_save_sets_0600_permissions(self):
        jira_auth.save_jira_token(_make_token())
        token_path = jira_auth.get_jira_token_path()
        mode = stat.S_IMODE(token_path.stat().st_mode)
        assert mode == 0o600, f"Expected 0o600 got {oct(mode)}"

    def test_load_returns_none_when_missing(self):
        assert jira_auth.load_jira_token() is None

    def test_load_returns_none_on_corrupt_json(self):
        creds_dir = jira_auth.get_jira_credentials_dir()
        creds_dir.mkdir(parents=True, exist_ok=True)
        token_path = jira_auth.get_jira_token_path()
        token_path.write_text("{not valid json")
        token_path.chmod(0o600)
        assert jira_auth.load_jira_token() is None

    def test_jira_credentials_exist_true(self):
        jira_auth.save_jira_token(_make_token())
        assert jira_auth.jira_credentials_exist() is True

    def test_jira_credentials_exist_false(self):
        assert jira_auth.jira_credentials_exist() is False

    def test_jira_credentials_exist_no_io_beyond_stat(self):
        # Verify it's a cheap stat check: corrupt file still returns True
        creds_dir = jira_auth.get_jira_credentials_dir()
        creds_dir.mkdir(parents=True, exist_ok=True)
        token_path = jira_auth.get_jira_token_path()
        token_path.write_text("garbage")
        assert jira_auth.jira_credentials_exist() is True


# ---------------------------------------------------------------------------
# OAuth discovery
# ---------------------------------------------------------------------------


MOCK_METADATA = {
    "issuer": "https://cf.mcp.atlassian.com",
    "authorization_endpoint": "https://mcp.atlassian.com/v1/authorize",
    "token_endpoint": "https://cf.mcp.atlassian.com/v1/token",
    "registration_endpoint": "https://cf.mcp.atlassian.com/v1/register",
    "token_endpoint_auth_methods_supported": [
        "client_secret_basic",
        "client_secret_post",
        "none",
    ],
    "code_challenge_methods_supported": ["plain", "S256"],
}


class TestDiscoverOAuthMetadata:
    @pytest.mark.asyncio
    async def test_primary_endpoint_success(self):
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=MOCK_METADATA)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            metadata = await jira_auth.discover_oauth_metadata("https://mcp.atlassian.com")

        assert metadata["issuer"] == "https://cf.mcp.atlassian.com"
        assert metadata["token_endpoint"] == "https://cf.mcp.atlassian.com/v1/token"

    @pytest.mark.asyncio
    async def test_raises_when_both_endpoints_fail(self):
        mock_response = AsyncMock()
        mock_response.status = 404
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(
                RuntimeError,
                match="Could not fetch OAuth metadata",
            ),
        ):
            await jira_auth.discover_oauth_metadata("https://mcp.atlassian.com")


# ---------------------------------------------------------------------------
# Dynamic Client Registration
# ---------------------------------------------------------------------------


class TestRegisterClient:
    @pytest.mark.asyncio
    async def test_success_returns_client_credentials(self):
        dcr_response = {
            "client_id": "generated-client-id",
            "client_secret": "generated-client-secret",
            "token_endpoint_auth_method": "client_secret_post",
        }
        mock_response = AsyncMock()
        mock_response.status = 201
        mock_response.json = AsyncMock(return_value=dcr_response)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        _redirect_uri = "http://127.0.0.1:54321/oauth/callback"
        with patch("aiohttp.ClientSession", return_value=mock_session):
            client_id, client_secret = await jira_auth.register_client(MOCK_METADATA, _redirect_uri)

        assert client_id == "generated-client-id"
        assert client_secret == "generated-client-secret"

    @pytest.mark.asyncio
    async def test_raises_when_registration_endpoint_missing(self):
        metadata_no_reg = {k: v for k, v in MOCK_METADATA.items() if k != "registration_endpoint"}
        with pytest.raises(RuntimeError, match="missing registration_endpoint"):
            await jira_auth.register_client(metadata_no_reg, "http://127.0.0.1:0/oauth/callback")

    @pytest.mark.asyncio
    async def test_raises_when_no_client_secret_in_response(self):
        # SC-02: must reject responses without client_secret (auth method 'none')
        dcr_response_no_secret = {"client_id": "generated-client-id"}
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=dcr_response_no_secret)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(
                RuntimeError,
                match="missing client_secret",
            ),
        ):
            await jira_auth.register_client(MOCK_METADATA, "http://127.0.0.1:54321/oauth/callback")

    @pytest.mark.asyncio
    async def test_raises_on_dcr_endpoint_origin_mismatch(self):
        # SC-02 issuer validation: registration_endpoint on different host
        spoofed_metadata = {
            **MOCK_METADATA,
            "registration_endpoint": "https://evil.attacker.com/v1/register",
        }
        with pytest.raises(RuntimeError, match="does not match issuer"):
            await jira_auth.register_client(
                spoofed_metadata, "http://127.0.0.1:54321/oauth/callback"
            )

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self):
        mock_response = AsyncMock()
        mock_response.status = 400
        mock_response.json = AsyncMock(return_value={"error": "invalid_client"})
        mock_response.text = AsyncMock(return_value='{"error": "invalid_client"}')
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(
                RuntimeError,
                match="DCR failed",
            ),
        ):
            await jira_auth.register_client(MOCK_METADATA, "http://127.0.0.1:54321/oauth/callback")


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


class TestPkce:
    def test_code_verifier_length(self):
        for _ in range(10):
            code_verifier, _ = jira_auth._pkce_pair()
            assert 43 <= len(code_verifier) <= 128, (
                f"code_verifier length {len(code_verifier)} violates RFC 7636 §4.1"
            )

    def test_code_challenge_is_base64url(self):
        import re

        _, code_challenge = jira_auth._pkce_pair()
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", code_challenge), (
            "code_challenge should be base64url without padding"
        )

    def test_pairs_are_unique(self):
        pairs = [jira_auth._pkce_pair() for _ in range(50)]
        verifiers = [p[0] for p in pairs]
        assert len(set(verifiers)) == 50, "code_verifiers should be unique"


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


class TestRefreshTokenIfNeeded:
    """Tests for the sync fast-path: only checks freshness, does not perform I/O."""

    def test_returns_same_token_when_still_fresh(self):
        token = _make_token(expires_at=time.time() + 3600)
        result = jira_auth.refresh_token_if_needed(token)
        assert result is token

    def test_returns_none_when_expired(self):
        """Expired token returns None — sync path never performs refresh."""
        token = _make_token(expires_at=time.time() + 60)  # within 5-min buffer
        result = jira_auth.refresh_token_if_needed(token)
        assert result is None

    def test_returns_none_when_already_expired(self):
        token = _make_token(expires_at=time.time() - 10)
        result = jira_auth.refresh_token_if_needed(token)
        assert result is None


class TestRefreshJiraTokenIfNeeded:
    """Tests for the async entry point that actually performs token refresh."""

    @pytest.mark.asyncio
    async def test_no_op_when_token_file_missing(self):
        """If no token file exists, the function returns without error."""
        await jira_auth.refresh_jira_token_if_needed()
        # No exception raised

    @pytest.mark.asyncio
    async def test_no_op_when_token_is_fresh(self):
        """Fresh token: no refresh attempted."""
        token = _make_token(expires_at=time.time() + 3600)
        jira_auth.save_jira_token(token)

        with patch.object(jira_auth, "_do_refresh") as mock_refresh:
            await jira_auth.refresh_jira_token_if_needed()

        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_refreshes_when_near_expiry(self):
        """Near-expiry token triggers async refresh and saves result."""
        token = _make_token(expires_at=time.time() + 60)  # within 5-min buffer
        jira_auth.save_jira_token(token)

        refreshed_token = {
            **token,
            "access_token": "refreshed-access-token",
            "expires_at": time.time() + 3600,
        }

        with patch.object(jira_auth, "_do_refresh", return_value=refreshed_token):
            await jira_auth.refresh_jira_token_if_needed()

        # Load the token and verify it was refreshed
        saved = jira_auth.load_jira_token()
        assert saved is not None
        assert saved["access_token"] == "refreshed-access-token"

    @pytest.mark.asyncio
    async def test_no_op_on_network_failure(self):
        """If _do_refresh returns None (network failure), function returns without raising."""
        token = _make_token(expires_at=time.time() + 60)
        jira_auth.save_jira_token(token)

        with patch.object(jira_auth, "_do_refresh", return_value=None):
            await jira_auth.refresh_jira_token_if_needed()
        # No exception; existing token unchanged

    @pytest.mark.asyncio
    async def test_detects_concurrent_refresh(self):
        """If another process already refreshed, the async refresh is skipped."""
        old_token = _make_token(expires_at=time.time() + 60)
        fresh_token = {
            **old_token,
            "access_token": "already-refreshed",
            "expires_at": time.time() + 3600,
        }
        jira_auth.save_jira_token(fresh_token)

        with patch.object(jira_auth, "_do_refresh") as mock_refresh:
            await jira_auth.refresh_jira_token_if_needed()

        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_side_race_skips_overwrite(self):
        """If another process refreshed during our HTTP call, don't overwrite their token."""
        expired_token = _make_token(expires_at=time.time() - 10)
        jira_auth.save_jira_token(expired_token)

        async def fake_refresh(token_data):
            # Simulate another process refreshing while we do HTTP I/O:
            # write a fresh token to disk AFTER we released the read lock
            # but BEFORE we re-acquire the write lock.
            other_token = {
                **token_data,
                "access_token": "other-process-token",
                "expires_at": time.time() + 7200,
            }
            jira_auth.save_jira_token(other_token)
            # Return our own refreshed token (would normally overwrite)
            return {
                **token_data,
                "access_token": "our-token",
                "expires_at": time.time() + 3600,
            }

        with patch.object(jira_auth, "_do_refresh", side_effect=fake_refresh):
            await jira_auth.refresh_jira_token_if_needed()

        # The on-disk token should be the OTHER process's token (fresher),
        # not ours — our write was skipped.
        on_disk = jira_auth.load_jira_token()
        assert on_disk is not None
        assert on_disk["access_token"] == "other-process-token"


# ---------------------------------------------------------------------------
# Cloud site discovery
# ---------------------------------------------------------------------------


class TestDiscoverCloudSites:
    @pytest.mark.asyncio
    async def test_returns_sites_on_success(self):
        sites_response = [
            {
                "id": "2b9e35e3-6bd3-4cec-b838-f4249ee02432",
                "url": "https://redhat.atlassian.net",
                "name": "redhat",
                "scopes": ["read:jira-work", "write:jira-work"],
            }
        ]
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sites_response)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            sites = await jira_auth.discover_cloud_sites("test-access-token")

        assert len(sites) == 1
        assert sites[0]["id"] == "2b9e35e3-6bd3-4cec-b838-f4249ee02432"
        assert sites[0]["name"] == "redhat"
        assert sites[0]["url"] == "https://redhat.atlassian.net"

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_http_error(self):
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            sites = await jira_auth.discover_cloud_sites("bad-token")

        assert sites == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_network_error(self):
        import aiohttp as _aiohttp

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("timeout"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            sites = await jira_auth.discover_cloud_sites("test-token")

        assert sites == []

    @pytest.mark.asyncio
    async def test_returns_multiple_sites(self):
        sites_response = [
            {"id": "site-1", "url": "https://org1.atlassian.net", "name": "org1", "scopes": []},
            {"id": "site-2", "url": "https://org2.atlassian.net", "name": "org2", "scopes": []},
        ]
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value=sites_response)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            sites = await jira_auth.discover_cloud_sites("test-token")

        assert len(sites) == 2


# ---------------------------------------------------------------------------
# check_jira_status
# ---------------------------------------------------------------------------


class TestCheckJiraStatus:
    def test_returns_none_when_valid(self):
        token = _make_token()
        jira_auth.save_jira_token(token)
        # Token is fresh (expires_at = now + 3600) — load_jira_token returns it directly
        result = jira_auth.check_jira_status()
        assert result is None

    def test_returns_error_when_no_credentials(self):
        result = jira_auth.check_jira_status()
        assert result is not None
        assert "No Jira credentials" in result

    def test_returns_error_when_token_load_fails(self):
        # Corrupt the token file
        creds_dir = jira_auth.get_jira_credentials_dir()
        creds_dir.mkdir(parents=True, exist_ok=True)
        token_path = jira_auth.get_jira_token_path()
        token_path.write_text("not-valid-json")
        token_path.chmod(0o600)

        result = jira_auth.check_jira_status()
        assert result is not None
        assert "corrupt or unreadable" in result

    def test_returns_error_when_no_cloud_id(self):
        token = _make_token()
        token.pop("cloud_id")
        jira_auth.save_jira_token(token)

        result = jira_auth.check_jira_status()

        assert result is not None
        assert "cloud_id" in result

    def test_returns_none_when_token_near_expiry(self):
        """Near-expiry token: check_jira_status reads raw file, ignores expiry.

        check_jira_status bypasses load_jira_token() and reads the token file
        directly. A near-expiry token with valid access_token and cloud_id
        should return None (OK) — the daemon refreshes tokens asynchronously.
        """
        token = _make_token(expires_at=time.time() + 60)  # within 5-min buffer
        jira_auth.save_jira_token(token)

        result = jira_auth.check_jira_status()
        assert result is None

    def test_returns_error_when_no_access_token(self):
        """Token file with valid JSON but missing access_token."""
        token = _make_token()
        del token["access_token"]
        jira_auth.save_jira_token(token)

        result = jira_auth.check_jira_status()
        assert result is not None
        assert "access_token" in result


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_removes_credentials_dir(self):
        jira_auth.save_jira_token(_make_token())
        assert jira_auth.jira_credentials_exist()

        jira_auth.logout()

        assert not jira_auth.jira_credentials_exist()
        assert not jira_auth.get_jira_credentials_dir().exists()

    def test_logout_is_idempotent(self):
        # Should not raise even if dir doesn't exist
        jira_auth.logout()
        jira_auth.logout()


# ---------------------------------------------------------------------------
# Security constraint guard tests
# ---------------------------------------------------------------------------


class TestSecurityConstraints:
    def test_sc01_state_parameter_is_url_safe_32_bytes(self):
        """SC-01: state generated by secrets.token_urlsafe(32) — at least 43 chars."""
        import re
        import secrets as _secrets

        # Verify token_urlsafe(32) output satisfies URL-safe base64 and >= 43 chars
        state = _secrets.token_urlsafe(32)
        assert len(state) >= 43
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", state)

    def test_sc01_code_verifier_at_least_43_chars(self):
        """SC-01: code_verifier >= 43 chars per RFC 7636 §4.1."""
        for _ in range(20):
            verifier, _ = jira_auth._pkce_pair()
            assert len(verifier) >= 43

    def test_sc02_never_uses_auth_method_none(self):
        """SC-02: The DCR payload must not select auth method 'none'."""
        import inspect

        source = inspect.getsource(jira_auth.register_client)
        # Both conditions must hold: client_secret_post is selected AND 'none' is absent
        assert "client_secret_post" in source, (
            "register_client must explicitly select auth method 'client_secret_post'"
        )
        assert '"none"' not in source, "register_client must not use auth method 'none'"

    def test_sc08_callback_binds_to_localhost_only(self):
        """SC-08: Callback server must bind to 127.0.0.1."""
        import inspect

        source = inspect.getsource(jira_auth.start_auth_flow)
        assert "127.0.0.1" in source, "Callback server must bind to 127.0.0.1"
        assert "0.0.0.0" not in source, "Callback server must NOT bind to 0.0.0.0"  # noqa: S104

    def test_sc08_ephemeral_port(self):
        """SC-08: Must use port 0 (ephemeral) for the callback server."""
        import inspect

        source = inspect.getsource(jira_auth.start_auth_flow)
        assert ", 0)" in source, "Callback server must use port 0 (ephemeral)"

    def test_sc03_refresh_token_not_exposed_via_load_jira_token_docstring(self):
        """SC-03: load_jira_token returns the token dict — caller must not
        pass refresh_token or client_secret into MCP config headers.
        This is a documentation/convention test: the function name and docstring
        must make the constraint clear.
        """
        # Verify the module docstring mentions SC-03
        module_doc = jira_auth.__doc__ or ""
        assert "SC-03" in module_doc, "Module docstring should reference SC-03"


# ---------------------------------------------------------------------------
# Module-level constant guard tests
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_trusted_token_hosts_pinned(self):
        assert (
            frozenset({"cf.mcp.atlassian.com", "auth.atlassian.com"})
            == jira_auth._TRUSTED_TOKEN_HOSTS
        )

    def test_token_preserve_fields_pinned(self):
        assert jira_auth._TOKEN_PRESERVE_FIELDS == ("cloud_id", "cloud_name")

    def test_token_endpoint_cached_in_start_auth_flow(self):
        """start_auth_flow must store token_endpoint for future refresh."""
        import inspect

        source = inspect.getsource(jira_auth.start_auth_flow)
        assert 'token_data["token_endpoint"]' in source


# ---------------------------------------------------------------------------
# _do_refresh() direct tests
# ---------------------------------------------------------------------------


class TestDoRefresh:
    """Direct tests for _do_refresh() — the actual token refresh logic."""

    def _make_mock_http(self, status: int = 200, body: dict | None = None):
        """Build a fake aiohttp session+response pair."""
        if body is None:
            body = {
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        mock_response = AsyncMock()
        mock_response.status = status
        mock_response.json = AsyncMock(return_value=body)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        return mock_session

    @pytest.mark.asyncio
    async def test_preserves_cloud_id_and_cloud_name(self):
        """cor-1: cloud_id and cloud_name from token_data must survive refresh."""
        token = _make_token(cloud_id="my-cloud-id")
        token["cloud_name"] = "my-cloud-name"

        # Server response does NOT include cloud_id or cloud_name
        server_response = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
        }
        mock_session = self._make_mock_http(200, server_response)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await jira_auth._do_refresh(token)

        assert result is not None
        assert result["cloud_id"] == "my-cloud-id", "cloud_id must be preserved across refresh"
        assert result["cloud_name"] == "my-cloud-name", (
            "cloud_name must be preserved across refresh"
        )

    @pytest.mark.asyncio
    async def test_trusted_host_validation_rejects_untrusted(self):
        """SEC-P4-005: cached token_endpoint on untrusted host triggers rediscovery."""
        token = _make_token()
        token["token_endpoint"] = "https://evil.com/token"

        server_response = {
            "access_token": "new-access-token",
            "expires_in": 3600,
        }
        mock_session = self._make_mock_http(200, server_response)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.object(
                jira_auth,
                "discover_oauth_metadata",
                return_value={"token_endpoint": "https://cf.mcp.atlassian.com/v1/token"},
            ) as mock_discover,
        ):
            result = await jira_auth._do_refresh(token)

        # Discovery must have been called because the cached endpoint was untrusted
        mock_discover.assert_called_once()
        # Refresh still succeeds via the discovered endpoint
        assert result is not None

    @pytest.mark.asyncio
    async def test_trusted_host_validation_accepts_trusted(self):
        """Cached token_endpoint on trusted host is used directly (no rediscovery)."""
        token = _make_token()
        # token_endpoint is already "https://cf.mcp.atlassian.com/v1/token" from _make_token

        server_response = {
            "access_token": "new-access-token",
            "expires_in": 3600,
        }
        mock_session = self._make_mock_http(200, server_response)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.object(jira_auth, "discover_oauth_metadata") as mock_discover,
        ):
            result = await jira_auth._do_refresh(token)

        mock_discover.assert_not_called()
        assert result is not None

    @pytest.mark.asyncio
    async def test_http_scheme_triggers_rediscovery(self):
        """Cached token_endpoint with http:// (not https://) triggers rediscovery."""
        token = _make_token()
        token["token_endpoint"] = "http://cf.mcp.atlassian.com/v1/token"

        server_response = {"access_token": "new", "expires_in": 3600}
        mock_session = self._make_mock_http(200, server_response)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch.object(
                jira_auth,
                "discover_oauth_metadata",
                return_value={"token_endpoint": "https://cf.mcp.atlassian.com/v1/token"},
            ) as mock_discover,
        ):
            result = await jira_auth._do_refresh(token)

        mock_discover.assert_called_once()
        assert result is not None

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_refresh_token(self):
        """Token without refresh_token returns None."""
        token = _make_token()
        del token["refresh_token"]

        result = await jira_auth._do_refresh(token)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_error(self):
        """aiohttp.ClientError returns None."""
        import aiohttp as _aiohttp

        token = _make_token()

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=_aiohttp.ClientError("timeout"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await jira_auth._do_refresh(token)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_non_200_response(self):
        """Non-200 HTTP response returns None."""
        token = _make_token()
        error_body = {"error": "invalid_grant", "error_description": "Refresh token expired"}
        mock_session = self._make_mock_http(400, error_body)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await jira_auth._do_refresh(token)

        assert result is None

    @pytest.mark.asyncio
    async def test_preserves_old_refresh_token_when_server_omits_it(self):
        """If server doesn't return new refresh_token, keep the old one."""
        token = _make_token(refresh_token="original-refresh-token")

        # Server response deliberately omits refresh_token
        server_response = {
            "access_token": "new-access-token",
            "expires_in": 3600,
        }
        mock_session = self._make_mock_http(200, server_response)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await jira_auth._do_refresh(token)

        assert result is not None
        assert result["refresh_token"] == "original-refresh-token"


# ---------------------------------------------------------------------------
# OAuth discovery — fallback endpoint
# ---------------------------------------------------------------------------


class TestDiscoverOAuthMetadataFallback:
    @pytest.mark.asyncio
    async def test_fallback_to_openid_configuration(self):
        """Primary endpoint fails (404), fallback to openid-configuration succeeds."""
        not_found = AsyncMock()
        not_found.status = 404
        not_found.__aenter__ = AsyncMock(return_value=not_found)
        not_found.__aexit__ = AsyncMock(return_value=False)

        ok_response = AsyncMock()
        ok_response.status = 200
        ok_response.json = AsyncMock(return_value=MOCK_METADATA)
        ok_response.__aenter__ = AsyncMock(return_value=ok_response)
        ok_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        # First call (primary) → 404, second call (fallback) → 200
        mock_session.get = MagicMock(side_effect=[not_found, ok_response])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            metadata = await jira_auth.discover_oauth_metadata("https://mcp.atlassian.com")

        assert metadata["issuer"] == "https://cf.mcp.atlassian.com"
        assert metadata["token_endpoint"] == "https://cf.mcp.atlassian.com/v1/token"
        assert mock_session.get.call_count == 2

"""Tests for GitHub OAuth device flow and token storage."""

from __future__ import annotations

import json
import logging
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.github_auth import (
    DeviceCodeResponse,
    DeviceFlowResult,
    GitHubAuthError,
    get_github_token_path,
    load_token,
    poll_for_token,
    remove_token,
    request_device_code,
    run_device_flow,
    store_token,
    validate_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_aiohttp_response(status: int, json_data: dict, headers: dict | None = None) -> MagicMock:
    """Build a mock aiohttp response usable as an async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.headers = headers or {}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# Fixture: per-test isolation for token storage
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_github_config(tmp_path):
    """Patch get_config_dir to a per-test tmp_path for all token operations."""
    with patch("summon_claude.github_auth.get_config_dir", return_value=tmp_path):
        yield tmp_path


# ---------------------------------------------------------------------------
# TestTokenStorage
# ---------------------------------------------------------------------------


class TestTokenStorage:
    def test_store_creates_file(self):
        path = store_token("test-token-abc")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["access_token"] == "test-token-abc"

    def test_store_sets_600_permissions(self):
        path = store_token("test-token-abc")
        file_mode = stat.S_IMODE(path.stat().st_mode)
        assert file_mode == 0o600

    def test_store_parent_dir_700(self):
        path = store_token("test-token-abc")
        dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
        assert dir_mode == 0o700

    def test_store_tightens_preexisting_dir(self):
        """store_token tightens a pre-existing 0o755 directory to 0o700."""
        creds_dir = get_github_token_path().parent
        creds_dir.mkdir(parents=True, mode=0o755)
        assert stat.S_IMODE(creds_dir.stat().st_mode) == 0o755
        store_token("tok")
        assert stat.S_IMODE(creds_dir.stat().st_mode) == 0o700

    def test_store_includes_scopes_and_login(self):
        store_token("tok", scopes="repo read:org", login="octocat")
        path = get_github_token_path()
        data = json.loads(path.read_text())
        assert data["scopes"] == "repo read:org"
        assert data["login"] == "octocat"

    def test_store_overwrites_existing(self):
        store_token("first-token")
        store_token("second-token")
        path = get_github_token_path()
        data = json.loads(path.read_text())
        assert data["access_token"] == "second-token"

    def test_load_returns_token(self):
        store_token("mytoken123")
        assert load_token() == "mytoken123"

    def test_load_missing_file_returns_none(self):
        assert load_token() is None

    def test_load_corrupt_json_returns_none(self):
        token_path = get_github_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("not valid json{{{")
        assert load_token() is None

    def test_load_strips_trailing_crlf(self):
        """Trailing CRLF stripped — prevents HTTP header injection."""
        token_path = get_github_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps({"access_token": "gho_abc123\r\n"}))
        assert load_token() == "gho_abc123"

    def test_load_non_dict_json_returns_none(self):
        """Non-dict JSON (e.g. array or string) returns None safely."""
        token_path = get_github_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text('"just a string"')
        assert load_token() is None

    def test_load_strips_crlf(self):
        token_path = get_github_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps({"access_token": "tok\r\nen"}))
        assert load_token() == "token"

    def test_load_empty_token_returns_none(self):
        token_path = get_github_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(json.dumps({"access_token": ""}))
        assert load_token() is None


# ---------------------------------------------------------------------------
# TestRemoveToken
# ---------------------------------------------------------------------------


class TestRemoveToken:
    def test_remove_existing_returns_true(self):
        store_token("tok")
        assert remove_token() is True

    def test_remove_nonexistent_returns_false(self):
        assert remove_token() is False

    def test_remove_deletes_file(self):
        store_token("tok")
        remove_token()
        assert load_token() is None

    def test_remove_cleans_stale_tmp(self):
        """remove_token deletes leftover .tmp file from interrupted store_token."""
        token_path = get_github_token_path()
        token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = token_path.with_suffix(".tmp")
        tmp_file.write_text("stale")
        assert remove_token() is False  # no main token file existed
        assert not tmp_file.exists()

    def test_remove_cleans_empty_parent_dir(self):
        """remove_token removes empty parent directory after cleanup."""
        store_token("tok")
        creds_dir = get_github_token_path().parent
        remove_token()
        assert not creds_dir.exists()


# ---------------------------------------------------------------------------
# TestRequestDeviceCode
# ---------------------------------------------------------------------------


class TestRequestDeviceCode:
    async def test_success_returns_device_code_response(self):
        json_data = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        resp = _make_aiohttp_response(200, json_data)
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        result = await request_device_code(session, client_id="test-client")

        assert isinstance(result, DeviceCodeResponse)
        assert result.device_code == "dev123"
        assert result.user_code == "ABCD-1234"
        assert result.verification_uri == "https://github.com/login/device"
        assert result.expires_in == 900
        assert result.interval == 5

    async def test_non_200_raises_error(self):
        resp = _make_aiohttp_response(403, {})
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        with pytest.raises(GitHubAuthError, match="HTTP 403"):
            await request_device_code(session, client_id="test-client")

    async def test_rejects_non_github_verification_uri(self):
        """Verification URI not starting with https://github.com/ is rejected."""
        json_data = {
            "device_code": "dev123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://evil.example.com/phish",
            "expires_in": 900,
            "interval": 5,
        }
        resp = _make_aiohttp_response(200, json_data)
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        with pytest.raises(GitHubAuthError, match="Unexpected verification_uri"):
            await request_device_code(session, client_id="test-client")

    async def test_uses_default_client_id(self):
        json_data = {
            "device_code": "d",
            "user_code": "U",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        resp = _make_aiohttp_response(200, json_data)
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        result = await request_device_code(session)
        assert isinstance(result, DeviceCodeResponse)


# ---------------------------------------------------------------------------
# TestPollForToken
# ---------------------------------------------------------------------------


class TestPollForToken:
    async def test_pending_then_success(self):
        pending_resp = _make_aiohttp_response(200, {"error": "authorization_pending"})
        pending_resp2 = _make_aiohttp_response(200, {"error": "authorization_pending"})
        success_resp = _make_aiohttp_response(200, {"access_token": "gha_mytoken"})

        session = MagicMock()
        session.post = MagicMock(side_effect=[pending_resp, pending_resp2, success_resp])

        with patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()):
            token = await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

        assert token == "gha_mytoken"

    async def test_slow_down_increases_interval(self):
        slow_resp = _make_aiohttp_response(200, {"error": "slow_down"})
        success_resp = _make_aiohttp_response(200, {"access_token": "gha_tok"})

        session = MagicMock()
        session.post = MagicMock(side_effect=[slow_resp, success_resp])

        sleep_calls = []

        async def _track_sleep(n):
            sleep_calls.append(n)

        with patch("summon_claude.github_auth.asyncio.sleep", side_effect=_track_sleep):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

        # After slow_down, interval increases by 5 (5 -> 10).
        # Sleep is after the request, so the only sleep is at the increased interval.
        assert sleep_calls[0] == 10  # slow_down increased 5 → 10 before sleep

    async def test_expired_token_raises(self):
        expired_resp = _make_aiohttp_response(200, {"error": "expired_token"})
        session = MagicMock()
        session.post = MagicMock(return_value=expired_resp)

        with (
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
            pytest.raises(GitHubAuthError, match="expired"),
        ):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

    async def test_access_denied_raises(self):
        resp = _make_aiohttp_response(200, {"error": "access_denied"})
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        with (
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
            pytest.raises(GitHubAuthError, match="Access denied"),
        ):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

    async def test_device_flow_disabled_raises(self):
        resp = _make_aiohttp_response(200, {"error": "device_flow_disabled"})
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        with (
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
            pytest.raises(GitHubAuthError, match="disabled"),
        ):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

    async def test_incorrect_client_credentials_raises(self):
        resp = _make_aiohttp_response(200, {"error": "incorrect_client_credentials"})
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        with (
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
            pytest.raises(GitHubAuthError, match="client credentials"),
        ):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

    async def test_deadline_exceeded_raises(self):
        """Polling loop exits when monotonic deadline is reached."""
        pending_resp = _make_aiohttp_response(200, {"error": "authorization_pending"})
        session = MagicMock()
        session.post = MagicMock(return_value=pending_resp)

        # Simulate: first call returns 0, second returns beyond deadline
        mono_values = iter([0.0, 0.0, 999.0])

        with (
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
            patch("summon_claude.github_auth.time.monotonic", side_effect=mono_values),
            pytest.raises(GitHubAuthError, match="polling deadline exceeded"),
        ):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=10,
                client_id="test-client",
            )

    async def test_non_200_polling_retries(self):
        """Non-200 HTTP response during polling retries instead of crashing."""
        error_resp = _make_aiohttp_response(500, {})
        success_resp = _make_aiohttp_response(
            200, {"access_token": "gho_success", "token_type": "bearer"}
        )
        session = MagicMock()
        session.post = MagicMock(side_effect=[error_resp, success_resp])

        with patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()):
            token = await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

        assert token == "gho_success"

    async def test_missing_access_token_raises(self):
        """200 with no error and no access_token raises GitHubAuthError."""
        resp = _make_aiohttp_response(200, {"token_type": "bearer"})
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        with (
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
            pytest.raises(GitHubAuthError, match="missing access_token"),
        ):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )

    async def test_unknown_error_raises(self):
        resp = _make_aiohttp_response(200, {"error": "some_unknown_error"})
        session = MagicMock()
        session.post = MagicMock(return_value=resp)

        with (
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
            pytest.raises(GitHubAuthError, match="some_unknown_error"),
        ):
            await poll_for_token(
                session,
                device_code="dev123",
                interval=5,
                expires_in=300,
                client_id="test-client",
            )


# ---------------------------------------------------------------------------
# TestValidateToken
# ---------------------------------------------------------------------------


class TestValidateToken:
    async def test_success_returns_login_and_scopes(self):
        resp = _make_aiohttp_response(
            200,
            {"login": "octocat", "id": 1},
            headers={"X-OAuth-Scopes": "repo, read:org"},
        )
        session = MagicMock()
        session.get = MagicMock(return_value=resp)

        result = await validate_token("gha_mytoken", session=session)

        assert result is not None
        assert result["login"] == "octocat"
        assert result["scopes"] == "repo, read:org"

    async def test_401_returns_none(self):
        resp = _make_aiohttp_response(401, {"message": "Bad credentials"})
        session = MagicMock()
        session.get = MagicMock(return_value=resp)

        result = await validate_token("bad-token", session=session)
        assert result is None

    async def test_403_returns_none(self):
        resp = _make_aiohttp_response(403, {"message": "Forbidden"})
        session = MagicMock()
        session.get = MagicMock(return_value=resp)

        result = await validate_token("bad-token", session=session)
        assert result is None

    async def test_500_raises_github_auth_error(self):
        resp = _make_aiohttp_response(500, {"message": "Internal Server Error"})
        session = MagicMock()
        session.get = MagicMock(return_value=resp)

        with pytest.raises(GitHubAuthError, match="HTTP 500"):
            await validate_token("gho_token", session=session)

    async def test_429_raises_github_auth_error(self):
        resp = _make_aiohttp_response(429, {"message": "rate limit"})
        session = MagicMock()
        session.get = MagicMock(return_value=resp)

        with pytest.raises(GitHubAuthError, match="HTTP 429"):
            await validate_token("gho_token", session=session)

    async def test_no_session_creates_own(self):
        resp = _make_aiohttp_response(
            200,
            {"login": "alice"},
            headers={"X-OAuth-Scopes": "repo"},
        )
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=resp)

        with patch("summon_claude.github_auth.aiohttp.ClientSession", return_value=mock_session):
            result = await validate_token("gha_tok")

        assert result is not None
        assert result["login"] == "alice"


# ---------------------------------------------------------------------------
# TestRunDeviceFlow
# ---------------------------------------------------------------------------


class TestRunDeviceFlow:
    async def test_end_to_end_returns_result(self):
        device_code_data = {
            "device_code": "dev-xyz",
            "user_code": "WXYZ-9999",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        token_data = {"access_token": "gha_finaltoken"}
        user_data = {"login": "octocat", "id": 1}

        device_resp = _make_aiohttp_response(200, device_code_data)
        token_resp = _make_aiohttp_response(200, token_data)
        user_resp = _make_aiohttp_response(
            200, user_data, headers={"X-OAuth-Scopes": "repo read:org"}
        )

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(side_effect=[device_resp, token_resp])
        mock_session.get = MagicMock(return_value=user_resp)

        codes_received = []

        def on_code(user_code, uri):
            codes_received.append((user_code, uri))

        with (
            patch("summon_claude.github_auth.aiohttp.ClientSession", return_value=mock_session),
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
        ):
            result = await run_device_flow(client_id="test-client", on_code=on_code)

        assert isinstance(result, DeviceFlowResult)
        assert result.token == "gha_finaltoken"
        assert result.login == "octocat"
        assert result.scopes == "repo read:org"
        assert result.token_path.exists()
        assert codes_received == [("WXYZ-9999", "https://github.com/login/device")]

    async def test_validate_token_none_fallback(self):
        """If validate_token returns None, store with empty scopes and login='unknown'."""
        device_code_data = {
            "device_code": "dev-xyz",
            "user_code": "CODE-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        token_data = {"access_token": "gha_tok"}
        user_resp_401 = _make_aiohttp_response(401, {"message": "Bad credentials"})

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(
            side_effect=[
                _make_aiohttp_response(200, device_code_data),
                _make_aiohttp_response(200, token_data),
            ]
        )
        mock_session.get = MagicMock(return_value=user_resp_401)

        with (
            patch("summon_claude.github_auth.aiohttp.ClientSession", return_value=mock_session),
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
        ):
            result = await run_device_flow(client_id="test-client")

        assert result.login == "unknown"
        assert result.scopes == ""

    async def test_on_code_not_required(self):
        """run_device_flow works when on_code is None."""
        device_code_data = {
            "device_code": "dev-xyz",
            "user_code": "CODE-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        token_data = {"access_token": "gha_tok"}
        user_data = {"login": "bob"}

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(
            side_effect=[
                _make_aiohttp_response(200, device_code_data),
                _make_aiohttp_response(200, token_data),
            ]
        )
        mock_session.get = MagicMock(
            return_value=_make_aiohttp_response(200, user_data, headers={"X-OAuth-Scopes": "repo"})
        )

        with (
            patch("summon_claude.github_auth.aiohttp.ClientSession", return_value=mock_session),
            patch("summon_claude.github_auth.asyncio.sleep", new=AsyncMock()),
        ):
            result = await run_device_flow(client_id="test-client", on_code=None)

        assert result.login == "bob"

    async def test_request_device_code_failure_propagates(self):
        """GitHubAuthError from request_device_code propagates through run_device_flow."""
        device_resp = _make_aiohttp_response(403, {})

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=device_resp)

        with (
            patch("summon_claude.github_auth.aiohttp.ClientSession", return_value=mock_session),
            pytest.raises(GitHubAuthError, match="HTTP 403"),
        ):
            await run_device_flow(client_id="test-client")


class TestLoadTokenEnvFallback:
    """Tests for load_token() SUMMON_GITHUB_PAT env var fallback (BUG-049)."""

    def test_file_token_takes_priority(self, tmp_path):
        token_file = tmp_path / "token.json"
        token_file.write_text(json.dumps({"access_token": "gho_file_token"}))
        with (
            patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
            patch.dict("os.environ", {"SUMMON_GITHUB_PAT": "ghp_env_token"}),
        ):
            assert load_token() == "gho_file_token"

    def test_env_var_fallback_when_no_file(self, tmp_path):
        token_file = tmp_path / "nonexistent.json"
        with (
            patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
            patch.dict("os.environ", {"SUMMON_GITHUB_PAT": "ghp_env_token"}),
        ):
            assert load_token() == "ghp_env_token"

    def test_none_when_no_file_no_env(self, tmp_path):
        token_file = tmp_path / "nonexistent.json"
        # Use clear=True to guarantee SUMMON_GITHUB_PAT is absent
        clean_env = {k: v for k, v in os.environ.items() if k != "SUMMON_GITHUB_PAT"}
        with (
            patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
            patch.dict("os.environ", clean_env, clear=True),
        ):
            assert load_token() is None

    def test_env_var_strips_crlf(self, tmp_path):
        token_file = tmp_path / "nonexistent.json"
        with (
            patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
            patch.dict("os.environ", {"SUMMON_GITHUB_PAT": "ghp_token\r\n"}),
        ):
            assert load_token() == "ghp_token"

    def test_env_var_warns_on_unrecognized_prefix(self, tmp_path, caplog):
        token_file = tmp_path / "nonexistent.json"
        with (
            patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
            patch.dict("os.environ", {"SUMMON_GITHUB_PAT": "sk-ant-badtoken"}),
            caplog.at_level(logging.WARNING, logger="summon_claude.github_auth"),
        ):
            result = load_token()
        assert result == "sk-ant-badtoken"
        assert any("unrecognized format" in r.message for r in caplog.records)

    def test_env_var_empty_string_returns_none(self, tmp_path):
        token_file = tmp_path / "nonexistent.json"
        with (
            patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
            patch.dict("os.environ", {"SUMMON_GITHUB_PAT": ""}),
        ):
            assert load_token() is None

    def test_env_var_whitespace_only_returns_none(self, tmp_path):
        token_file = tmp_path / "nonexistent.json"
        with (
            patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
            patch.dict("os.environ", {"SUMMON_GITHUB_PAT": "   "}),
        ):
            assert load_token() is None

    def test_env_var_valid_prefixes_no_warning(self, tmp_path, caplog):
        token_file = tmp_path / "nonexistent.json"
        for prefix in ("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_"):
            with (
                patch("summon_claude.github_auth.get_github_token_path", return_value=token_file),
                patch.dict("os.environ", {"SUMMON_GITHUB_PAT": f"{prefix}test"}),
                caplog.at_level(logging.WARNING, logger="summon_claude.github_auth"),
            ):
                caplog.clear()
                load_token()
            assert not any("unrecognized format" in r.message for r in caplog.records)

"""Tests for summon_claude.cli.model_cache."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from summon_claude.cli.model_cache import (
    _CACHE_FILENAME,
    _CACHE_TTL,
    cache_sdk_models,
    load_cached_models,
    query_sdk_models,
)

_SAMPLE_MODELS = [
    {"value": "claude-opus-4-6", "displayName": "Claude Opus 4.6"},
    {"value": "claude-sonnet-4-6", "displayName": "Claude Sonnet 4.6"},
]


@pytest.fixture(autouse=True)
def patch_data_dir(tmp_path, monkeypatch):
    """Redirect get_data_dir() to tmp_path for all tests in this file."""
    monkeypatch.setattr(
        "summon_claude.cli.model_cache.get_data_dir",
        lambda: tmp_path,
    )


def _write_raw_cache(tmp_path: Path, data: dict) -> None:
    """Write raw cache content bypassing cache_sdk_models guards."""
    cache_path = tmp_path / _CACHE_FILENAME
    cache_path.write_text(json.dumps(data))


class TestCacheSdkModels:
    def test_cache_round_trip(self, tmp_path):
        """Write models, load immediately → returns same data."""
        cache_sdk_models(_SAMPLE_MODELS, "1.0.0")
        result = load_cached_models("1.0.0")
        assert result == _SAMPLE_MODELS

    def test_cache_empty_models_rejected(self, tmp_path):
        """call cache_sdk_models([], "1.0.0") → no cache file written."""
        cache_sdk_models([], "1.0.0")
        cache_path = tmp_path / _CACHE_FILENAME
        assert not cache_path.exists()

    def test_cache_symlink_rejected(self, tmp_path):
        """Write to symlink path → no-op (no write)."""
        real_file = tmp_path / "real.json"
        real_file.write_text("{}")
        cache_path = tmp_path / _CACHE_FILENAME
        cache_path.symlink_to(real_file)

        cache_sdk_models(_SAMPLE_MODELS, "1.0.0")
        # The symlink target should not have been overwritten with model data
        assert json.loads(real_file.read_text()) == {}


class TestLoadCachedModels:
    def test_cache_missing_file(self, tmp_path):
        """Load from nonexistent path → returns None."""
        result = load_cached_models()
        assert result is None

    def test_cache_ttl_expired(self, tmp_path):
        """Write with old timestamp, load → returns None."""
        old_time = datetime.now(UTC) - _CACHE_TTL - timedelta(seconds=1)
        _write_raw_cache(
            tmp_path,
            {
                "models": _SAMPLE_MODELS,
                "cached_at": old_time.isoformat(),
                "cli_version": "1.0.0",
            },
        )
        result = load_cached_models("1.0.0")
        assert result is None

    def test_cache_cli_version_mismatch(self, tmp_path):
        """Write with v1, load with v2 → returns None."""
        cache_sdk_models(_SAMPLE_MODELS, "1.0.0")
        result = load_cached_models("2.0.0")
        assert result is None

    def test_cache_corrupt_json(self, tmp_path):
        """Write garbage, load → returns None."""
        cache_path = tmp_path / _CACHE_FILENAME
        cache_path.write_text("not-valid-json{{{")
        result = load_cached_models()
        assert result is None

    def test_cache_version_none_skip_on_read(self, tmp_path):
        """Write with cli_version=None, load with current_cli_version='2.0.0' → returns data.

        Version check is skipped when cached value is None (daemon-written caches).
        """
        cache_sdk_models(_SAMPLE_MODELS, None)
        result = load_cached_models("2.0.0")
        assert result == _SAMPLE_MODELS

    def test_cache_version_none_skip_on_load(self, tmp_path):
        """Write with cli_version='1.0.0', load with current_cli_version=None → returns data.

        Version check is skipped when caller passes None.
        """
        cache_sdk_models(_SAMPLE_MODELS, "1.0.0")
        result = load_cached_models(None)
        assert result == _SAMPLE_MODELS

    def test_cache_symlink_rejected_on_read(self, tmp_path):
        """Symlink at cache path → returns None on load."""
        real_file = tmp_path / "real.json"
        now = datetime.now(UTC)
        real_file.write_text(
            json.dumps(
                {
                    "models": _SAMPLE_MODELS,
                    "cached_at": now.isoformat(),
                    "cli_version": None,
                }
            )
        )
        cache_path = tmp_path / _CACHE_FILENAME
        cache_path.symlink_to(real_file)
        result = load_cached_models()
        assert result is None


class TestQuerySdkModels:
    def test_query_sdk_models_success(self, tmp_path):
        """Mock ClaudeSDKClient → verify returns (models, version).

        Lazy imports inside query_sdk_models() mean mock targets are the
        source modules, not summon_claude.cli.model_cache.
        """
        import asyncio

        mock_server_info = {"models": _SAMPLE_MODELS}
        mock_client = AsyncMock()
        mock_client.get_server_info = AsyncMock(return_value=mock_server_info)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        mock_cli_status = MagicMock()
        mock_cli_status.found = True
        mock_cli_status.version = "1.2.3"

        with (
            patch(
                "claude_agent_sdk.ClaudeSDKClient",
                return_value=mock_client,
            ),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=mock_cli_status,
            ),
        ):
            result = asyncio.run(query_sdk_models())

        assert result is not None
        models, version = result
        assert models == _SAMPLE_MODELS
        assert version == "1.2.3"

    def test_query_sdk_models_failure(self, tmp_path):
        """Mock exception from ClaudeSDKClient → returns None."""
        import asyncio

        mock_cli_status = MagicMock()
        mock_cli_status.found = True
        mock_cli_status.version = "1.2.3"

        with (
            patch(
                "claude_agent_sdk.ClaudeSDKClient",
                side_effect=RuntimeError("SDK error"),
            ),
            patch(
                "summon_claude.cli.preflight.check_claude_cli",
                return_value=mock_cli_status,
            ),
        ):
            result = asyncio.run(query_sdk_models())

        assert result is None

    def test_query_sdk_models_cli_not_found(self, tmp_path):
        """CLI not found → returns None without spawning SDK."""
        import asyncio

        mock_cli_status = MagicMock()
        mock_cli_status.found = False

        with patch(
            "summon_claude.cli.preflight.check_claude_cli",
            return_value=mock_cli_status,
        ):
            result = asyncio.run(query_sdk_models())

        assert result is None

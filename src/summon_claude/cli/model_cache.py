"""TTL-based model cache for summon-claude.

Follows the update_check.py cache pattern: JSON file in get_data_dir(),
7-day TTL, CLI-version-aware invalidation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from summon_claude.config import get_data_dir

logger = logging.getLogger(__name__)

_CACHE_TTL = timedelta(days=7)
_CACHE_FILENAME = "model-cache.json"


def cache_sdk_models(
    models: list[dict[str, str]],
    cli_version: str | None,
    default_model: str | None = None,
) -> None:
    """Write the model list to the local cache file.

    Early-returns for empty lists to prevent creating a 7-day dead zone
    where load_cached_models returns [] and blocks future discovery.
    Rejects symlinks as a defence-in-depth measure.
    All exceptions are caught — this function is fire-and-forget safe.
    """
    try:
        if not models:
            return
        cache_path = get_data_dir() / _CACHE_FILENAME
        if cache_path.is_symlink():
            logger.debug("Refusing to write model cache through symlink: %s", cache_path)
            return
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "models": models,
                    "cached_at": datetime.now(UTC).isoformat(),
                    "cli_version": cli_version,
                    "default_model": default_model,
                }
            )
        )
    except Exception:
        logger.debug("Failed to write model cache", exc_info=True)


def load_cached_models(
    current_cli_version: str | None = None,
) -> tuple[list[dict[str, str]], str | None] | None:
    """Read the cached model list if it is fresh and version-compatible.

    Returns None if: file missing, symlink, TTL expired, CLI version mismatch,
    or parse error. Returns (data["models"], default_model) on success.

    Version check is skipped if either current_cli_version is None OR the
    cached cli_version is None. Mismatch is only flagged when both are non-None
    and differ.
    """
    try:
        cache_path = get_data_dir() / _CACHE_FILENAME
        if not cache_path.exists():
            return None
        if cache_path.is_symlink():
            logger.debug("Refusing to read model cache through symlink: %s", cache_path)
            return None
        data = json.loads(cache_path.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now(UTC) - cached_at >= _CACHE_TTL:
            return None
        cached_version = data.get("cli_version")
        if (
            current_cli_version is not None
            and cached_version is not None
            and current_cli_version != cached_version
        ):
            return None
        return (data["models"], data.get("default_model"))
    except Exception:
        logger.debug("Failed to read model cache", exc_info=True)
        return None


async def query_sdk_models(
    cli_version: str | None = None,
) -> tuple[list[dict[str, str]], str | None, str | None] | None:
    """Spawn a throwaway SDK session to extract server_info.models.

    CLI-only: must be called via asyncio.run() in a single-threaded context.

    Args:
        cli_version: CLI version string from check_claude_cli().version.
            Passed through to the return tuple — avoids re-running
            check_claude_cli() when the caller already has it.

    Returns (models_list, cli_version, default_model) on success, None on any failure.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # noqa: PLC0415

        # Inner coroutine gives wait_for a single awaitable covering both
        # client startup (__aenter__ spawns subprocess) and the query.
        async def _do_query() -> tuple[list[dict[str, str]], str | None, str | None] | None:
            async with ClaudeSDKClient(
                ClaudeAgentOptions(
                    cwd=str(Path.cwd()),
                    stderr=lambda _line: None,
                    setting_sources=[],
                )
            ) as client:
                server_info = await client.get_server_info()
                if server_info is None:
                    return None
                models = server_info.get("models", [])
                default_model = server_info.get("model")
                return (models, cli_version, default_model)

        return await asyncio.wait_for(_do_query(), timeout=10)
    except Exception:
        logger.debug("Failed to query SDK models", exc_info=True)
        return None

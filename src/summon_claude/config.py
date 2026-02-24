"""Configuration for summon-claude using pydantic-settings."""

# pyright: reportCallIssue=false, reportIncompatibleMethodOverride=false
# pydantic-settings metaclass constructor inference gaps

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _xdg_dir(env_var: str, default_subdir: str, xdg_subdir: str) -> Path:
    """Resolve an XDG base directory with fallbacks.

    Rejects non-absolute XDG paths per the XDG Base Directory spec.
    Falls back to ``~/.summon`` when the default parent doesn't exist.
    """
    xdg = os.environ.get(env_var, "").strip()
    if xdg:
        p = Path(xdg)
        if p.is_absolute():
            return p / xdg_subdir
        logger.warning("%s is not absolute (%r), using default", env_var, xdg)
    candidate = Path.home() / default_subdir
    if candidate.parent.exists():
        return candidate
    return Path.home() / ".summon"


def get_config_dir() -> Path:
    """XDG_CONFIG_HOME/summon -> ~/.config/summon -> ~/.summon (fallback)."""
    return _xdg_dir("XDG_CONFIG_HOME", ".config/summon", "summon")


def get_data_dir() -> Path:
    """XDG_DATA_HOME/summon -> ~/.local/share/summon -> ~/.summon (fallback)."""
    return _xdg_dir("XDG_DATA_HOME", ".local/share/summon", "summon")


def get_config_file() -> Path:
    return get_config_dir() / "config.env"


def discover_installed_plugins() -> list[dict]:
    """Discover plugins installed in the Claude CLI.

    Reads ~/.claude/plugins/installed_plugins.json and returns a list of
    SdkPluginConfig-compatible dicts for each plugin whose path exists on disk.
    """
    registry_path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not registry_path.exists():
        logger.debug("No installed_plugins.json found at %s", registry_path)
        return []

    try:
        registry = json.loads(registry_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read installed_plugins.json: %s", e)
        return []

    plugins: list[dict] = []
    if not isinstance(registry, list):
        logger.warning("installed_plugins.json has unexpected format (not a list)")
        return []

    claude_home = Path.home() / ".claude"
    for entry in registry:
        install_path = entry.get("installPath") or entry.get("path")
        if not install_path:
            continue
        path = Path(install_path).resolve()
        # Reject paths outside ~/.claude/ to prevent path traversal
        try:
            path.relative_to(claude_home.resolve())
        except ValueError:
            logger.warning("Skipping plugin outside ~/.claude/: %s", install_path)
            continue
        if path.exists():
            plugins.append({"type": "local", "path": str(path)})
        else:
            logger.debug("Skipping stale plugin path: %s", install_path)

    logger.debug("Discovered %d installed plugins", len(plugins))
    return plugins


class SummonConfig(BaseSettings):
    """Main configuration loaded from environment variables (SUMMON_ prefix) or .env file."""

    model_config = SettingsConfigDict(
        env_prefix="SUMMON_",
        env_file=(str(get_config_file()), ".env"),  # global first, local overrides
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Slack credentials
    slack_bot_token: str
    slack_app_token: str  # Socket Mode app-level token (xapp-)
    slack_signing_secret: str

    # Authorization
    allowed_user_ids: list[str] = []

    # Claude model
    default_model: str | None = None

    # Slack channel configuration
    channel_prefix: str = "summon"

    # Permission handling
    permission_debounce_ms: int = 500

    # Content display
    max_inline_chars: int = 2500

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_user_ids(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string or list of user IDs."""
        if isinstance(v, str):
            return [uid.strip() for uid in v.split(",") if uid.strip()]
        return v

    @field_validator("slack_bot_token")
    @classmethod
    def validate_bot_token_prefix(cls, v: str) -> str:
        """Validate that the Slack bot token starts with xoxb-."""
        if v and not v.startswith("xoxb-"):
            raise ValueError("SUMMON_SLACK_BOT_TOKEN must start with 'xoxb-'")
        return v

    @field_validator("slack_app_token")
    @classmethod
    def validate_app_token_prefix(cls, v: str) -> str:
        """Validate that the Slack app token starts with xapp-."""
        if v and not v.startswith("xapp-"):
            raise ValueError("SUMMON_SLACK_APP_TOKEN must start with 'xapp-'")
        return v

    def validate(self) -> None:
        """Validate required configuration fields and raise with clear errors."""
        errors: list[str] = []

        if not self.slack_bot_token:
            errors.append("SUMMON_SLACK_BOT_TOKEN is required")
        # Prefix format (xoxb-, xapp-) enforced by pydantic @field_validator at construction

        if not self.slack_app_token:
            errors.append("SUMMON_SLACK_APP_TOKEN is required (Socket Mode app-level token)")

        if not self.slack_signing_secret:
            errors.append("SUMMON_SLACK_SIGNING_SECRET is required")

        if not self.allowed_user_ids:
            errors.append("SUMMON_ALLOWED_USER_IDS is required (comma-separated Slack user IDs)")

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

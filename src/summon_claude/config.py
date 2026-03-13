"""Configuration for summon-claude using pydantic-settings."""

# pyright: reportCallIssue=false, reportIncompatibleMethodOverride=false
# pydantic-settings metaclass constructor inference gaps

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
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


def get_update_check_path() -> Path:
    """Return path to the update-check cache file."""
    return get_data_dir() / "update-check.json"


def get_config_file(override: str | None = None) -> Path:
    if override is not None:
        return Path(override)
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

    # Normalize to flat list of entries for both v1 (list) and v2 (dict) formats
    if isinstance(registry, list):
        entries = registry
    elif isinstance(registry, dict) and isinstance(registry.get("plugins"), dict):
        entries = [inst for installs in registry["plugins"].values() for inst in installs]
    else:
        logger.warning("installed_plugins.json has unexpected format")
        return []

    claude_home = Path.home() / ".claude"
    for entry in entries:
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


# ------------------------------------------------------------------
# Plugin skill / command discovery
# ------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)


@dataclass(frozen=True)
class PluginSkill:
    """A skill or command discovered from an installed Claude Code plugin."""

    plugin_name: str
    name: str
    description: str


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML-like key: value pairs from ``---`` frontmatter.

    Handles simple ``key: value`` pairs plus YAML block scalars (``|``, ``>``,
    ``|-``, ``>-``) where continuation lines are indented.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}

    result: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in m.group(1).splitlines():
        # New key: value (non-indented line with colon)
        if line and not line[0].isspace() and ":" in line:
            # Flush previous key
            if current_key is not None:
                result[current_key] = " ".join(current_lines)
            key, _, val = line.partition(":")
            current_key = key.strip()
            val = val.strip().strip("\"'")
            # Block scalar indicator — value is on continuation lines
            current_lines = [] if val in ("|", ">", "|-", ">-") else [val] if val else []
        elif current_key is not None and line and line[0].isspace():
            # Continuation line for block scalar
            current_lines.append(line.strip())

    # Flush last key
    if current_key is not None:
        result[current_key] = " ".join(current_lines)

    return result


def discover_plugin_skills() -> list[PluginSkill]:
    """Enumerate skills and commands from all installed Claude Code plugins.

    For each plugin returned by ``discover_installed_plugins()``, reads:
    - ``.claude-plugin/plugin.json`` for the plugin name (falls back to dir name)
    - ``commands/*.md`` and ``commands/*/COMMAND.md`` for user-invocable commands
    - ``skills/*/SKILL.md`` for model/user-invocable skills

    Returns a flat list of :class:`PluginSkill` entries.
    """
    plugins = discover_installed_plugins()
    results: list[PluginSkill] = []

    for entry in plugins:
        plugin_path = Path(entry["path"])

        # Read plugin name from manifest; fall back to parent dir name
        # (some plugins like claude-plugins-official/plugin-dev have no manifest)
        manifest = plugin_path / ".claude-plugin" / "plugin.json"
        plugin_name = plugin_path.parent.name  # default: cache/<org>/<name>/<ver>
        if manifest.exists():
            try:
                meta = json.loads(manifest.read_text())
                plugin_name = meta.get("name", plugin_name)
            except (json.JSONDecodeError, OSError):
                pass

        # Discover commands — two patterns:
        #   commands/<name>.md (flat)
        #   commands/<name>/COMMAND.md (subdirectory)
        commands_dir = plugin_path / "commands"
        if commands_dir.is_dir():
            for md_file in sorted(commands_dir.glob("*.md")):
                fm = _parse_frontmatter(md_file.read_text(errors="replace"))
                skill_name = fm.get("name", md_file.stem)
                desc = fm.get("description", "")
                results.append(PluginSkill(plugin_name, skill_name, desc))
            for cmd_md in sorted(commands_dir.glob("*/COMMAND.md")):
                fm = _parse_frontmatter(cmd_md.read_text(errors="replace"))
                skill_name = fm.get("name", cmd_md.parent.name)
                desc = fm.get("description", "")
                results.append(PluginSkill(plugin_name, skill_name, desc))

        # Discover skills/*/SKILL.md
        skills_dir = plugin_path / "skills"
        if skills_dir.is_dir():
            for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
                fm = _parse_frontmatter(skill_md.read_text(errors="replace"))
                skill_name = fm.get("name", skill_md.parent.name)
                desc = fm.get("description", "")
                results.append(PluginSkill(plugin_name, skill_name, desc))

    logger.debug("Discovered %d plugin skills/commands", len(results))
    return results


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

    # Claude model
    default_model: str | None = None

    # Claude effort level
    default_effort: str = "high"

    # Slack channel configuration
    channel_prefix: str = "summon"

    # Permission handling
    permission_debounce_ms: int = 500

    # Content display
    max_inline_chars: int = 2500

    @field_validator("default_effort")
    @classmethod
    def validate_effort_level(cls, v: str) -> str:
        """Validate that effort is one of the allowed levels."""
        valid = {"low", "medium", "high", "max"}
        if v not in valid:
            raise ValueError(f"SUMMON_DEFAULT_EFFORT must be one of {sorted(valid)}, got {v!r}")
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

    @classmethod
    def from_file(cls, config_path: str | None = None) -> SummonConfig:
        if config_path:
            return cls(_env_file=config_path)
        return cls()

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

        if errors:
            raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))

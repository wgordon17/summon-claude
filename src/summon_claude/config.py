"""Configuration for summon-claude using pydantic-settings."""

# pyright: reportCallIssue=false, reportIncompatibleMethodOverride=false
# pydantic-settings metaclass constructor inference gaps

from __future__ import annotations

import functools
import importlib
import json
import logging
import os
import re
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
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


@functools.lru_cache(maxsize=1)
def _find_project_root() -> Path | None:
    """Walk up from CWD looking for pyproject.toml. Return containing dir or None.

    Stops at the user's home directory parent to avoid picking up stray
    ``pyproject.toml`` files in system directories.  Uses a single ``lstat``
    call per candidate to atomically reject symlinks.
    """
    current = Path.cwd().resolve()
    home_parent = Path.home().resolve().parent
    for parent in [current, *current.parents]:
        if parent in (home_parent, parent.parent):
            break
        sentinel = parent / "pyproject.toml"
        try:
            st = sentinel.lstat()
            if stat.S_ISREG(st.st_mode):
                return parent
        except OSError:
            pass
    return None


@functools.lru_cache(maxsize=1)
def _detect_install_mode() -> tuple[str, Path | None]:
    """Detect whether this is a local or global install.

    Returns ("local", project_root) or ("global", None).
    Cached — CWD and env vars are frozen at first call (before daemon
    ``os.chdir``).
    """
    project_root = _find_project_root()

    # Explicit override — bypasses home-directory restriction
    summon_local = os.environ.get("SUMMON_LOCAL", "").strip()
    if summon_local == "0":
        return ("global", None)
    if summon_local == "1":
        return ("local", project_root) if project_root is not None else ("global", None)
    if summon_local:
        logger.warning("SUMMON_LOCAL=%r not recognized (use '0' or '1'), ignoring", summon_local)

    # Auto-detect: restrict to $HOME to avoid triggering on system projects
    if project_root is not None:
        home = Path.home().resolve()
        if not project_root.is_relative_to(home):
            return ("global", None)

    # Auto-detect: VIRTUAL_ENV under project root
    venv_str = os.environ.get("VIRTUAL_ENV", "").strip()
    if venv_str and project_root is not None:
        venv_path = Path(venv_str)
        if venv_path.is_absolute() and venv_path.resolve().is_relative_to(project_root.resolve()):
            return ("local", project_root)

    return ("global", None)


def is_local_install() -> bool:
    """Return True if running as a local (project-scoped) install."""
    mode, _ = _detect_install_mode()
    return mode == "local"


def get_local_root() -> Path | None:
    """Return the project root when running as a local install, else None."""
    _, root = _detect_install_mode()
    return root


def find_local_daemon_hint() -> str | None:
    """Check if a local-mode daemon socket exists nearby when in global mode.

    Returns a user-facing hint string if a ``.summon/daemon.sock`` is found
    via CWD walk-up while the current process is in global mode.  Returns
    ``None`` if already in local mode or no local daemon socket is found.
    """
    if is_local_install():
        return None
    root = _find_project_root()
    if root is not None and (root / ".summon" / "daemon.sock").exists():
        return (
            f"A local-mode daemon may be running at {root / '.summon'}.\n"
            "Activate your project's virtualenv or set SUMMON_LOCAL=1 to reach it."
        )
    return None


def get_claude_config_dir() -> Path:
    """Return the Claude Code configuration directory.

    Respects the ``CLAUDE_CONFIG_DIR`` environment variable (set by Claude Code
    when the user overrides the default location). Falls back to ``~/.claude``.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        p = Path(env)
        if p.is_absolute():
            return p
        logger.warning("CLAUDE_CONFIG_DIR is not absolute (%r), using default", env)
    return Path.home() / ".claude"


def get_config_dir() -> Path:
    """XDG config path, or project_root/.summon in local mode."""
    root = get_local_root()
    if root is not None:
        return root / ".summon"
    return _xdg_dir("XDG_CONFIG_HOME", ".config/summon", "summon")


def get_data_dir() -> Path:
    """XDG data path, or project_root/.summon in local mode."""
    root = get_local_root()
    if root is not None:
        return root / ".summon"
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


def get_google_credentials_dir() -> Path:
    """Return the directory for storing Google OAuth credentials.

    Uses ``get_data_dir() / "google-credentials"`` so credentials live
    under summon's own XDG data directory, not workspace-mcp's default.
    """
    return get_data_dir() / "google-credentials"


def google_mcp_env() -> dict[str, str]:
    """Build env var overrides so workspace-mcp uses summon's credential dir.

    Also sets ``GOOGLE_CLIENT_SECRETS_PATH`` if a ``client_secret.json``
    has been saved in the credentials directory.
    """
    creds_dir = get_google_credentials_dir()
    env: dict[str, str] = {"WORKSPACE_MCP_CREDENTIALS_DIR": str(creds_dir)}
    json_path = creds_dir / "client_secret.json"
    if json_path.exists():
        env["GOOGLE_CLIENT_SECRETS_PATH"] = str(json_path)
    return env


VALID_GOOGLE_SERVICES = frozenset(
    {
        "gmail",
        "drive",
        "calendar",
        "docs",
        "sheets",
        "chat",
        "forms",
        "slides",
        "tasks",
        "contacts",
        "search",
        "appscript",
    }
)


_SLACK_WORKSPACE_FILE = "slack_workspace.json"


def get_workspace_config_path() -> Path:
    """Path to the external Slack workspace config file."""
    return get_data_dir() / _SLACK_WORKSPACE_FILE


def find_workspace_mcp_bin() -> Path:
    """Locate the ``workspace-mcp`` console-script in the same Python environment.

    Uses ``sys.executable``'s parent directory so the binary is found
    regardless of installation method (pip, uv, pipx inject, Homebrew
    formula) without relying on PATH.
    """
    return Path(sys.executable).parent / "workspace-mcp"


class SummonConfig(BaseSettings):
    """Main configuration loaded from environment variables (SUMMON_ prefix) or .env file."""

    model_config = SettingsConfigDict(
        env_prefix="SUMMON_",
        env_file=(str(get_config_file()), ".env"),  # global first, local overrides
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Slack credentials — repr=False to prevent leakage in logs/tracebacks
    slack_bot_token: str = Field(repr=False)
    slack_app_token: str = Field(repr=False)  # Socket Mode app-level token (xapp-)
    slack_signing_secret: str = Field(repr=False)

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

    # Behavior
    no_update_check: bool = False

    # Thinking display
    enable_thinking: bool = True  # Pass ThinkingConfigAdaptive to SDK
    show_thinking: bool = False  # Route ThinkingBlock content to Slack turn thread

    # ------------------------------------------------------------------
    # GitHub integration
    # ------------------------------------------------------------------

    github_pat: str | None = Field(default=None, repr=False)  # GitHub PAT for remote MCP

    # ------------------------------------------------------------------
    # Scribe agent settings
    # ------------------------------------------------------------------

    # Core scribe settings
    scribe_enabled: bool = False
    scribe_scan_interval_minutes: int = 5
    scribe_cwd: str | None = None  # None -> get_data_dir() / "scribe"
    scribe_model: str | None = None  # None -> inherit default_model
    scribe_importance_keywords: str = ""  # comma-separated: "urgent,action required,deadline"
    scribe_quiet_hours: str = ""  # "22:00-07:00" — only level-5 alerts during this window

    # Google Workspace data collector (requires workspace-mcp optional dep)
    scribe_google_enabled: bool = False
    scribe_google_services: str = "gmail,calendar,drive"  # comma-separated service list

    # External Slack data collector
    scribe_slack_enabled: bool = False
    scribe_slack_browser: str = "chrome"  # "chrome", "firefox", or "webkit"
    scribe_slack_monitored_channels: str = ""  # comma-separated channel IDs (e.g. "C01ABC,C02DEF")

    @classmethod
    def for_test(cls, **overrides: object) -> SummonConfig:
        """Create a config instance isolated from env vars and .env files.

        Use in tests instead of ``SummonConfig(...)`` or ``model_validate()``.
        Provides sensible defaults for required fields; override any field
        via keyword arguments.
        """
        import os  # noqa: PLC0415

        defaults: dict[str, object] = {
            "slack_bot_token": "xoxb-test-token",
            "slack_app_token": "xapp-test-token",
            "slack_signing_secret": "abc123def456",
        }
        defaults.update(overrides)
        # Temporarily clear all SUMMON_ env vars so pydantic-settings
        # doesn't read them. patch.dict isn't available here, so stash
        # and restore manually.
        stashed = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("SUMMON_")}
        try:
            return cls(_env_file=None, **defaults)  # type: ignore[call-arg]
        finally:
            os.environ.update(stashed)

    @functools.cached_property
    def slack_app_id(self) -> str | None:
        """Parse the Slack App ID from the xapp- token. Returns None if unavailable."""
        token = self.slack_app_token
        if not token:
            return None
        m = re.search(r"xapp-\d+-([A-Z][A-Z0-9]+)-", token)
        if not m:
            return None
        app_id = m.group(1)
        if not re.fullmatch(r"A[A-Z0-9]{9,11}", app_id):
            return None
        return app_id

    @functools.cached_property
    def slack_app_url(self) -> str:
        """Return the Slack app settings URL for diagnostic messages."""
        app_id = self.slack_app_id
        if app_id:
            return f"https://api.slack.com/apps/{app_id}"
        return "https://api.slack.com/apps"

    @field_validator("default_effort")
    @classmethod
    def validate_effort_level(cls, v: str) -> str:
        """Validate that effort is one of the allowed levels."""
        valid = {"low", "medium", "high", "max"}
        if v not in valid:
            raise ValueError(f"SUMMON_DEFAULT_EFFORT must be one of {sorted(valid)}, got {v!r}")
        return v

    @field_validator("scribe_google_services")
    @classmethod
    def validate_scribe_google_services(cls, v: str) -> str:
        """Validate that all service names are recognized by workspace-mcp."""
        if not v:
            return v
        services = [s.strip() for s in v.split(",") if s.strip()]
        invalid = set(services) - VALID_GOOGLE_SERVICES
        if invalid:
            raise ValueError(
                f"SUMMON_SCRIBE_GOOGLE_SERVICES contains unknown services: {sorted(invalid)}. "
                f"Valid: {sorted(VALID_GOOGLE_SERVICES)}"
            )
        return v

    @field_validator("scribe_scan_interval_minutes")
    @classmethod
    def validate_scribe_scan_interval(cls, v: int) -> int:
        """Scan interval must be at least 1 minute."""
        if v < 1:
            raise ValueError("SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES must be at least 1")
        return v

    @field_validator("scribe_slack_browser")
    @classmethod
    def validate_scribe_slack_browser(cls, v: str) -> str:
        """Validate that browser choice is one of the supported Playwright browsers."""
        valid = ("chrome", "firefox", "webkit")
        if v not in valid:
            raise ValueError(f"SUMMON_SCRIBE_SLACK_BROWSER must be one of {valid!r}, got {v!r}")
        return v

    @field_validator("scribe_quiet_hours")
    @classmethod
    def validate_scribe_quiet_hours(cls, v: str) -> str:
        """Validate quiet hours format: HH:MM-HH:MM with valid time values, or empty."""
        if not v:
            return v
        parts = v.split("-")
        if len(parts) != 2:
            raise ValueError(f"SUMMON_SCRIBE_QUIET_HOURS must be in HH:MM-HH:MM format, got {v!r}")
        for part in parts:
            try:
                datetime.strptime(part, "%H:%M")  # noqa: DTZ007
            except ValueError:
                raise ValueError(
                    f"SUMMON_SCRIBE_QUIET_HOURS must be in HH:MM-HH:MM format, got {v!r}"
                ) from None
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

    @field_validator("slack_signing_secret")
    @classmethod
    def _check_signing_secret(cls, v: str) -> str:
        if v and not re.match(r"^[0-9a-f]+$", v):
            raise ValueError("slack_signing_secret must be a hex string")
        return v

    @field_validator("channel_prefix")
    @classmethod
    def _check_channel_prefix(cls, v: str) -> str:
        if not v:
            raise ValueError("channel_prefix cannot be empty")
        if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", v):
            raise ValueError(
                "channel_prefix must be lowercase alphanumeric, hyphens, and underscores only"
                " (Slack channel naming rules)"
            )
        return v

    @field_validator("github_pat")
    @classmethod
    def _check_github_pat(cls, v: str | None) -> str | None:
        if v and not v.startswith(("ghp_", "github_pat_")):
            msg = "github_pat must start with 'ghp_' (classic) or 'github_pat_' (fine-grained)"
            raise ValueError(msg)
        return v

    def github_mcp_config(self) -> dict | None:
        """Return GitHub remote MCP server config, or None if not configured."""
        if not self.github_pat:
            return None
        return {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {
                "Authorization": f"Bearer {self.github_pat}",
            },
        }

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


# ------------------------------------------------------------------
# ConfigOption registry
# ------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigOption:
    """Registry entry mapping a SummonConfig field to display/prompt metadata."""

    field_name: str  # SummonConfig field name
    env_key: str  # SUMMON_... env var name
    group: str  # Display group header
    label: str  # Human-readable label for prompts
    help_text: str  # One-line description
    input_type: str  # 'text', 'secret', 'choice', 'flag', 'int'
    required: bool = False
    advanced: bool = False  # Hidden behind "Configure advanced settings?" in init wizard
    help_hint: str | None = None  # Contextual guidance shown before prompt in init wizard
    choices: tuple[str, ...] | None = None
    choices_fn: Callable[[], list[str]] | None = None
    visible: Callable[[dict[str, str]], bool] | None = None
    validate_fn: Callable[[str], str | None] | None = None


@functools.cache
def is_extra_installed(package: str) -> bool:
    """Check if an optional dependency is importable."""
    try:
        importlib.import_module(package)
        return True
    except ImportError:
        return False


_BOOL_TRUE: frozenset[str] = frozenset({"true", "1", "yes", "on"})
_BOOL_FALSE: frozenset[str] = frozenset({"false", "0", "no", "off"})


def _is_truthy(value: str) -> bool:
    return value.lower() in _BOOL_TRUE


def _workspace_mcp_installed() -> bool:
    """Check if workspace-mcp binary is available (not importable as a Python package)."""
    return find_workspace_mcp_bin().exists()


def _scribe_enabled(cfg: dict[str, str]) -> bool:
    return _is_truthy(cfg.get("SUMMON_SCRIBE_ENABLED", ""))


def _scribe_google_enabled(cfg: dict[str, str]) -> bool:
    return (
        _scribe_enabled(cfg)
        and _is_truthy(cfg.get("SUMMON_SCRIBE_GOOGLE_ENABLED", ""))
        and _workspace_mcp_installed()
    )


def _scribe_slack_enabled(cfg: dict[str, str]) -> bool:
    return (
        _scribe_enabled(cfg)
        and _is_truthy(cfg.get("SUMMON_SCRIBE_SLACK_ENABLED", ""))
        and is_extra_installed("playwright")
    )


def _validate_scribe_scan_interval(v: str) -> str | None:
    try:
        return None if int(v) >= 1 else "Must be at least 1"
    except (ValueError, TypeError):
        return "Must be an integer"


def _validate_channel_prefix(v: str) -> str | None:
    if not v:
        return "Cannot be empty"
    if not re.match(r"^[a-z0-9][a-z0-9\-_]*$", v):
        return "Must be lowercase alphanumeric, hyphens, and underscores only"
    return None


def _validate_quiet_hours(v: str) -> str | None:
    if not v:
        return None
    parts = v.split("-")
    if len(parts) != 2:
        return "Must be in HH:MM-HH:MM format"
    for part in parts:
        try:
            datetime.strptime(part, "%H:%M")  # noqa: DTZ007
        except ValueError:
            return "Must be in HH:MM-HH:MM format"
    return None


def _validate_google_services(v: str) -> str | None:
    if not v:
        return None
    services = [s.strip() for s in v.split(",") if s.strip()]
    invalid = set(services) - VALID_GOOGLE_SERVICES
    if invalid:
        return f"Unknown services: {sorted(invalid)}. Valid: {sorted(VALID_GOOGLE_SERVICES)}"
    return None


CONFIG_OPTIONS: list[ConfigOption] = [
    # Slack Credentials
    ConfigOption(
        field_name="slack_bot_token",
        env_key="SUMMON_SLACK_BOT_TOKEN",
        group="Slack Credentials",
        label="Bot Token",
        help_text="Slack bot token (xoxb-...)",
        input_type="secret",
        required=True,
        help_hint=(
            "Find at: api.slack.com/apps → your app → OAuth & Permissions → Bot User OAuth Token"
        ),
        validate_fn=lambda v: None if v.startswith("xoxb-") else "Must start with xoxb-",
    ),
    ConfigOption(
        field_name="slack_app_token",
        env_key="SUMMON_SLACK_APP_TOKEN",
        group="Slack Credentials",
        label="App Token",
        help_text="Slack Socket Mode app-level token (xapp-...)",
        input_type="secret",
        required=True,
        help_hint="Find at: api.slack.com/apps → your app → Basic Information → App-Level Tokens",
        validate_fn=lambda v: None if v.startswith("xapp-") else "Must start with xapp-",
    ),
    ConfigOption(
        field_name="slack_signing_secret",
        env_key="SUMMON_SLACK_SIGNING_SECRET",
        group="Slack Credentials",
        label="Signing Secret",
        help_text="Slack app signing secret for request verification",
        input_type="secret",
        required=True,
        help_hint="Find at: api.slack.com/apps → your app → Basic Information → App Credentials",
        validate_fn=lambda v: (
            "Cannot be empty"
            if not v
            else (None if re.match(r"^[0-9a-f]+$", v) else "Must be a hex string")
        ),
    ),
    # Session Defaults
    ConfigOption(
        field_name="default_model",
        env_key="SUMMON_DEFAULT_MODEL",
        group="Session Defaults",
        label="Default Model",
        help_text="Claude model to use for sessions (e.g. claude-opus-4-6)",
        input_type="text",
    ),
    ConfigOption(
        field_name="default_effort",
        env_key="SUMMON_DEFAULT_EFFORT",
        group="Session Defaults",
        label="Default Effort",
        help_text="Thinking effort level for sessions",
        input_type="choice",
        choices=("low", "medium", "high", "max"),
    ),
    ConfigOption(
        field_name="channel_prefix",
        env_key="SUMMON_CHANNEL_PREFIX",
        group="Session Defaults",
        label="Channel Prefix",
        help_text="Prefix for Slack channel names created by summon",
        input_type="text",
        validate_fn=_validate_channel_prefix,
    ),
    # Scribe
    ConfigOption(
        field_name="scribe_enabled",
        env_key="SUMMON_SCRIBE_ENABLED",
        group="Scribe",
        label="Enable Scribe",
        help_text="Enable the background scribe agent",
        input_type="flag",
        help_hint="Background agent that monitors Slack/Google and provides context to sessions",
    ),
    ConfigOption(
        field_name="scribe_scan_interval_minutes",
        env_key="SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES",
        group="Scribe",
        label="Scan Interval (minutes)",
        help_text="How often the scribe agent scans for new data",
        input_type="int",
        visible=_scribe_enabled,
        validate_fn=_validate_scribe_scan_interval,
    ),
    ConfigOption(
        field_name="scribe_cwd",
        env_key="SUMMON_SCRIBE_CWD",
        group="Scribe",
        label="Scribe Working Directory",
        help_text="Working directory for the scribe agent (default: XDG data dir)",
        input_type="text",
        visible=_scribe_enabled,
    ),
    ConfigOption(
        field_name="scribe_model",
        env_key="SUMMON_SCRIBE_MODEL",
        group="Scribe",
        label="Scribe Model",
        help_text="Claude model for the scribe agent (default: inherits default_model)",
        input_type="text",
        visible=_scribe_enabled,
    ),
    ConfigOption(
        field_name="scribe_importance_keywords",
        env_key="SUMMON_SCRIBE_IMPORTANCE_KEYWORDS",
        group="Scribe",
        label="Importance Keywords",
        help_text="Comma-separated keywords that raise message importance (e.g. urgent,deadline)",
        input_type="text",
        visible=_scribe_enabled,
    ),
    ConfigOption(
        field_name="scribe_quiet_hours",
        env_key="SUMMON_SCRIBE_QUIET_HOURS",
        group="Scribe",
        label="Quiet Hours",
        help_text="Time window for reduced alerts, format HH:MM-HH:MM (e.g. 22:00-07:00)",
        input_type="text",
        visible=_scribe_enabled,
        validate_fn=_validate_quiet_hours,
    ),
    # Scribe Google
    ConfigOption(
        field_name="scribe_google_enabled",
        env_key="SUMMON_SCRIBE_GOOGLE_ENABLED",
        group="Scribe Google",
        label="Enable Google Collector",
        help_text="Enable the Google Workspace data collector for scribe",
        input_type="flag",
        visible=lambda cfg: _scribe_enabled(cfg) and _workspace_mcp_installed(),
    ),
    ConfigOption(
        field_name="scribe_google_services",
        env_key="SUMMON_SCRIBE_GOOGLE_SERVICES",
        group="Scribe Google",
        label="Google Services",
        help_text="Comma-separated Google services for scribe (e.g. gmail,calendar,drive)",
        input_type="text",
        visible=_scribe_google_enabled,
        validate_fn=_validate_google_services,
    ),
    # Scribe Slack
    ConfigOption(
        field_name="scribe_slack_enabled",
        env_key="SUMMON_SCRIBE_SLACK_ENABLED",
        group="Scribe Slack",
        label="Enable Scribe Slack Collector",
        help_text="Enable the Slack data collector for the scribe agent",
        input_type="flag",
        visible=_scribe_enabled,
    ),
    ConfigOption(
        field_name="scribe_slack_browser",
        env_key="SUMMON_SCRIBE_SLACK_BROWSER",
        group="Scribe Slack",
        label="Scribe Slack Browser",
        help_text="Playwright browser for the Slack collector",
        input_type="choice",
        choices=("chrome", "firefox", "webkit"),
        visible=_scribe_slack_enabled,
    ),
    ConfigOption(
        field_name="scribe_slack_monitored_channels",
        env_key="SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS",
        group="Scribe Slack",
        label="Monitored Slack Channels",
        help_text="Comma-separated Slack channel names for the scribe collector",
        input_type="text",
        visible=_scribe_slack_enabled,
    ),
    # GitHub
    ConfigOption(
        field_name="github_pat",
        env_key="SUMMON_GITHUB_PAT",
        group="GitHub",
        label="GitHub PAT",
        help_text="GitHub Personal Access Token for the GitHub remote MCP server",
        input_type="secret",
        help_hint=(
            "Gives all sessions GitHub tools (search code, read PRs, etc)."
            " Create at: github.com/settings/tokens"
        ),
        validate_fn=lambda v: (
            None
            if not v or v.startswith(("ghp_", "github_pat_"))
            else "Must start with ghp_ or github_pat_"
        ),
    ),
    # Advanced options below this point
    # Display
    ConfigOption(
        field_name="max_inline_chars",
        env_key="SUMMON_MAX_INLINE_CHARS",
        group="Display",
        label="Max Inline Chars",
        help_text="Maximum characters to display inline before uploading as a file",
        input_type="int",
        advanced=True,
    ),
    # Behavior
    ConfigOption(
        field_name="permission_debounce_ms",
        env_key="SUMMON_PERMISSION_DEBOUNCE_MS",
        group="Behavior",
        label="Permission Debounce (ms)",
        help_text="Milliseconds to debounce permission prompts",
        input_type="int",
        advanced=True,
    ),
    ConfigOption(
        field_name="no_update_check",
        env_key="SUMMON_NO_UPDATE_CHECK",
        group="Behavior",
        label="Disable Update Check",
        help_text="Disable automatic update checks on startup",
        input_type="flag",
        advanced=True,
    ),
    # Thinking
    ConfigOption(
        field_name="enable_thinking",
        env_key="SUMMON_ENABLE_THINKING",
        group="Thinking",
        label="Enable Thinking",
        help_text="Pass ThinkingConfigAdaptive to the Claude SDK",
        input_type="flag",
        advanced=True,
    ),
    ConfigOption(
        field_name="show_thinking",
        env_key="SUMMON_SHOW_THINKING",
        group="Thinking",
        label="Show Thinking",
        help_text="Route thinking blocks to the Slack turn thread",
        input_type="flag",
        advanced=True,
    ),
]


def get_config_default(option: ConfigOption) -> Any:
    """Get the default value for a config option from SummonConfig."""
    field_info = SummonConfig.model_fields.get(option.field_name)
    if field_info is None:
        return None
    return field_info.default

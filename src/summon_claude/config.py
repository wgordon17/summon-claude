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

from pydantic import Field, field_validator, model_validator
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


def get_reports_dir() -> Path:
    """Return the directory for Global PM daily reports."""
    return get_data_dir() / "reports"


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

    Uses ``get_config_dir() / "google-credentials"`` so credentials live
    under summon's own XDG config directory, not workspace-mcp's default.
    """
    return get_config_dir() / "google-credentials"


def _migrate_flat_credentials() -> None:
    """Auto-migrate flat google-credentials/ layout to google-credentials/default/.

    Triggers when: google-credentials/ exists AND contains client_env or *.json
    files at the top level AND no subdirectories exist yet.
    Idempotent: skips if migration is already complete.
    """
    creds_dir = get_google_credentials_dir()
    if not creds_dir.exists():
        return

    # Check for existing subdirectories (non-hidden)
    subdirs = [d for d in creds_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]

    # Check for flat credential files
    has_client_env = (creds_dir / "client_env").exists()
    has_credential_json = any(
        f.suffix == ".json" and "@" in f.stem for f in creds_dir.glob("*.json")
    )
    has_flat_files = has_client_env or has_credential_json

    if not has_flat_files:
        return  # Nothing to migrate

    if subdirs and has_flat_files and not (creds_dir / "default").is_dir():
        # Non-default subdirs exist alongside flat files — don't migrate (safety)
        logger.warning(
            "Google credentials directory has mixed layout (flat files + subdirs). "
            "Skipping auto-migration. Move files manually to a subdirectory."
        )
        return
    # Either no subdirs (fresh migration) or default/ exists (partial recovery)

    default_dir = creds_dir / "default"
    default_dir.mkdir(mode=0o700, exist_ok=True)

    # Files to migrate: client_env, client_secret.json, *.json with @ in stem
    files_to_move: list[Path] = []
    if (creds_dir / "client_env").exists():
        files_to_move.append(creds_dir / "client_env")
    if (creds_dir / "client_secret.json").exists():
        files_to_move.append(creds_dir / "client_secret.json")
    for f in creds_dir.glob("*.json"):
        if "@" in f.stem and f.parent == creds_dir:
            files_to_move.append(f)

    moved_count = 0
    for src in files_to_move:
        dest = default_dir / src.name
        try:
            src.rename(dest)
            dest.chmod(0o600)
            moved_count += 1
        except FileNotFoundError:
            pass  # Concurrent migration — file already moved

    if moved_count:
        logger.info(
            "Migrated %d Google credential files to default/ subdirectory. "
            "MCP tool names changed from mcp__workspace__* to mcp__workspace-default__*.",
            moved_count,
        )


def discover_google_accounts() -> list[GoogleAccount]:
    """Scan google-credentials/ for account subdirectories with valid credentials.

    An account is wirable if its subdirectory contains BOTH:
    (a) a client_env file (setup completed), AND
    (b) at least one *.json file with @ in the stem (login completed).

    Calls _migrate_flat_credentials() first for backward compatibility.
    Returns a sorted list of GoogleAccount objects.
    """
    _migrate_flat_credentials()

    creds_dir = get_google_credentials_dir()
    if not creds_dir.exists():
        return []

    accounts: list[GoogleAccount] = []
    for item in sorted(creds_dir.iterdir(), key=lambda p: p.name):
        if not item.is_dir() or item.name.startswith("."):
            continue

        # Validate label
        if not ACCOUNT_LABEL_RE.match(item.name):
            logger.warning("Skipping Google account directory with invalid label: %s", item.name)
            continue

        if item.name in RESERVED_ACCOUNT_LABELS:
            logger.warning("Skipping Google account with reserved label: %s", item.name)
            continue

        # Check for required files
        if not (item / "client_env").exists():
            continue  # Setup not completed

        # Find credential JSON (login completed)
        cred_files = sorted(f for f in item.glob("*.json") if "@" in f.stem)
        if not cred_files:
            continue  # Login not completed

        # Extract and validate email from first credential file
        raw_email = cred_files[0].stem
        email: str | None = raw_email if EMAIL_RE.match(raw_email) else None
        if email is None and raw_email:
            logger.warning(
                "Google account %s has credential file with invalid email format: %s",
                item.name,
                cred_files[0].name,
            )

        accounts.append(GoogleAccount(label=item.name, creds_dir=item, email=email))

    return accounts


def google_mcp_env_for_account(account: GoogleAccount) -> dict[str, str]:
    """Build env var overrides for a specific Google account's workspace-mcp process.

    Each account gets its own WORKSPACE_MCP_CREDENTIALS_DIR pointing to its
    credential subdirectory, ensuring process-level credential isolation.

    Raises ValueError if account.creds_dir is outside the google-credentials directory.
    """
    # Path containment guard — defense-in-depth
    google_dir = get_google_credentials_dir()
    if not account.creds_dir.resolve().is_relative_to(google_dir.resolve()):
        raise ValueError(f"Account credential dir {account.creds_dir} is outside {google_dir}")

    resolved = account.creds_dir.resolve()
    env: dict[str, str] = {"WORKSPACE_MCP_CREDENTIALS_DIR": str(resolved)}
    json_path = resolved / "client_secret.json"
    if json_path.exists():
        env["GOOGLE_CLIENT_SECRETS_PATH"] = str(json_path)
    return env


# Read-only by default.  Append `:rw` to a service name to opt into write
# scopes (e.g. "calendar:rw").  This keeps the consent screen minimal while
# still being compatible with workspace-mcp's has_required_scopes() hierarchy.
GOOGLE_SCOPE_PREFIX = "https://www.googleapis.com/auth/"
GOOGLE_SERVICE_SCOPES: dict[str, dict[str, list[str]]] = {
    "gmail": {
        "ro": ["gmail.readonly"],
        "rw": ["gmail.send", "gmail.compose", "gmail.labels", "gmail.settings.basic"],
    },
    "drive": {
        "ro": ["drive.readonly"],
        "rw": ["drive", "drive.file"],
    },
    "calendar": {
        "ro": ["calendar.readonly"],
        "rw": ["calendar.events"],
    },
    "docs": {
        "ro": ["documents.readonly"],
        "rw": ["documents"],
    },
    "sheets": {
        "ro": ["spreadsheets.readonly"],
        "rw": ["spreadsheets"],
    },
    "chat": {
        "ro": ["chat.messages.readonly", "chat.spaces.readonly"],
        "rw": ["chat.messages", "chat.spaces"],
    },
    "forms": {
        "ro": ["forms.body.readonly", "forms.responses.readonly"],
        "rw": ["forms.body"],
    },
    "slides": {
        "ro": ["presentations.readonly"],
        "rw": ["presentations"],
    },
    "tasks": {
        "ro": ["tasks.readonly"],
        "rw": ["tasks"],
    },
    "contacts": {
        "ro": ["contacts.readonly"],
        "rw": ["contacts"],
    },
    "search": {
        "ro": ["cse"],
        "rw": ["cse"],
    },
    "appscript": {
        "ro": ["script.projects.readonly", "script.deployments.readonly"],
        "rw": ["script.projects", "script.deployments"],
    },
}


def _scopes_to_services(granted: set[str]) -> list[str]:
    """Invert GOOGLE_SERVICE_SCOPES: granted scopes -> list of service names."""
    services = []
    for service, scope_sets in GOOGLE_SERVICE_SCOPES.items():
        all_scopes = scope_sets.get("ro", []) + scope_sets.get("rw", [])
        full_scopes = {
            s if s.startswith("https://") else f"{GOOGLE_SCOPE_PREFIX}{s}" for s in all_scopes
        }
        if granted & full_scopes:
            services.append(service)
    return sorted(services)


def detect_account_services(account: GoogleAccount) -> str | None:
    """Detect which Google services an account's credential supports.

    Returns a comma-separated service string (e.g., "gmail,calendar,drive")
    or None if the credential can't be read.

    Uses a deferred import of LocalDirectoryCredentialStore to avoid making
    workspace-mcp a hard dependency.
    """
    try:
        from auth.credential_store import LocalDirectoryCredentialStore  # noqa: PLC0415
    except ImportError:
        logger.warning("workspace-mcp auth module not importable — cannot detect services")
        return None

    try:
        store = LocalDirectoryCredentialStore(str(account.creds_dir))
        users = store.list_users()
        if not users:
            return None
        cred = store.get_credential(users[0])
        if not cred or not cred.scopes:
            return None
        services = _scopes_to_services(set(cred.scopes))
        return ",".join(services) if services else None
    except Exception:
        logger.warning("Failed to detect services for account %s", account.label, exc_info=True)
        return None


VALID_GOOGLE_SERVICES: frozenset[str] = frozenset(GOOGLE_SERVICE_SCOPES.keys())

ACCOUNT_LABEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,253}\.[a-zA-Z]{2,}$")

# Reserved labels that could create confusing MCP tool namespaces
RESERVED_ACCOUNT_LABELS = frozenset({"cli", "slack", "canvas"})


@dataclass(frozen=True)
class GoogleAccount:
    """A discovered Google account with isolated credentials."""

    label: str  # user-chosen directory name (e.g., "personal")
    creds_dir: Path  # absolute path to the account's credential subdirectory
    email: str | None  # extracted from credential filename ({email}.json)


_SLACK_WORKSPACE_FILE = "slack_workspace.json"


def get_workspace_config_path() -> Path:
    """Path to the external Slack workspace config file."""
    return get_config_dir() / _SLACK_WORKSPACE_FILE


def get_browser_auth_dir() -> Path:
    """Directory for Playwright browser auth state files (Slack cookies/localStorage)."""
    return get_config_dir() / "browser_auth"


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
    permission_debounce_ms: int = 2000
    permission_timeout_s: int = 900  # 15 minutes; 0 = no timeout (wait indefinitely)

    # Write gate — directories where writes are allowed without entering containment
    # comma-separated paths, relative to project root or absolute (e.g. "hack/,.dev/")
    safe_write_dirs: str = ""

    # Content display
    max_inline_chars: int = 2500

    # Behavior
    no_update_check: bool = False

    # Thinking display
    enable_thinking: bool = True  # Pass ThinkingConfigAdaptive to SDK
    show_thinking: bool = False  # Route ThinkingBlock content to Slack turn thread

    # ------------------------------------------------------------------
    # Scribe agent settings
    # ------------------------------------------------------------------

    # Core scribe settings
    # Auto-detected: enabled when any sub-feature (Google or Slack) is
    # detected.  Can be explicitly enabled/disabled with SUMMON_SCRIBE_ENABLED.
    scribe_enabled: bool | None = None  # None = auto-detect
    scribe_scan_interval_minutes: int = 5
    scribe_cwd: str | None = None  # None -> get_data_dir() / "scribe"
    scribe_model: str | None = None  # None -> inherit default_model
    scribe_importance_keywords: str = ""  # comma-separated: "urgent,action required,deadline"
    scribe_quiet_hours: str = ""  # "22:00-07:00" — only level-5 alerts during this window

    # Google Workspace data collector (requires workspace-mcp optional dep).
    # Auto-detected: enabled when workspace-mcp is installed AND a user
    # credential exists (from ``summon auth google login``).  Can be
    # explicitly disabled with SUMMON_SCRIBE_GOOGLE_ENABLED=false.
    scribe_google_enabled: bool | None = None  # None = auto-detect

    # External Slack data collector
    # Auto-detected: enabled when Playwright is installed AND valid browser
    # auth state exists (from ``summon auth slack login``).  Can be
    # explicitly disabled with SUMMON_SCRIBE_SLACK_ENABLED=false.
    scribe_slack_enabled: bool | None = None  # None = auto-detect
    scribe_slack_browser: str = "chrome"  # "chrome", "firefox", or "webkit"
    scribe_slack_monitored_channels: str = ""  # comma-separated channel IDs (e.g. "C01ABC,C02DEF")

    # ------------------------------------------------------------------
    # Global PM settings
    # ------------------------------------------------------------------

    global_pm_scan_interval_minutes: int = 15
    global_pm_cwd: str | None = None  # None -> get_data_dir() / "global-pm"
    global_pm_model: str | None = None  # None -> inherit default_model

    # ------------------------------------------------------------------
    # Auto-mode classifier
    # ------------------------------------------------------------------

    auto_classifier_enabled: bool = True
    auto_mode_environment: str = ""
    auto_mode_deny: str = ""
    auto_mode_allow: str = ""

    @model_validator(mode="after")
    def _auto_detect_scribe(self) -> SummonConfig:
        """Resolve auto-detect (None) fields for scribe and its sub-features.

        Order matters: sub-features resolve first so scribe_enabled can
        check whether any sub-feature is active.
        """
        # 1. Sub-feature: Google
        if self.scribe_google_enabled is None:
            self.scribe_google_enabled = _workspace_mcp_installed() and _google_credentials_exist()
        # 2. Sub-feature: Slack
        if self.scribe_slack_enabled is None:
            self.scribe_slack_enabled = (
                is_extra_installed("playwright") and _slack_browser_auth_exists()
            )
        # 3. Top-level: auto-enable when any sub-feature is detected
        if self.scribe_enabled is None:
            self.scribe_enabled = self.scribe_google_enabled or self.scribe_slack_enabled
        return self

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

    @field_validator("permission_timeout_s")
    @classmethod
    def validate_permission_timeout(cls, v: int) -> int:
        """Permission timeout must be >= 0. 0 = no timeout (wait indefinitely)."""
        if v < 0:
            raise ValueError("SUMMON_PERMISSION_TIMEOUT_S must be >= 0 (0 = no timeout)")
        return v

    @field_validator("scribe_scan_interval_minutes")
    @classmethod
    def validate_scribe_scan_interval(cls, v: int) -> int:
        """Scan interval must be at least 1 minute."""
        if v < 1:
            raise ValueError("SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES must be at least 1")
        return v

    @field_validator("global_pm_scan_interval_minutes")
    @classmethod
    def validate_global_pm_scan_interval(cls, v: int) -> int:
        """Scan interval must be at least 1 minute."""
        if v < 1:
            raise ValueError("SUMMON_GLOBAL_PM_SCAN_INTERVAL_MINUTES must be at least 1")
        return v

    @field_validator("global_pm_cwd")
    @classmethod
    def validate_global_pm_cwd(cls, v: str | None) -> str | None:
        """CWD must be absolute when explicitly set. Expands ~ to home dir."""
        if v is not None:
            v = str(Path(v).expanduser())
            if not Path(v).is_absolute():
                raise ValueError("SUMMON_GLOBAL_PM_CWD must be an absolute path")
        return v

    @field_validator("scribe_cwd")
    @classmethod
    def validate_scribe_cwd(cls, v: str | None) -> str | None:
        """CWD must be absolute when explicitly set. Expands ~ to home dir."""
        if v is not None:
            v = str(Path(v).expanduser())
            if not Path(v).is_absolute():
                raise ValueError("SUMMON_SCRIBE_CWD must be an absolute path")
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

    def github_mcp_config(self) -> dict | None:
        """Return GitHub remote MCP server config, or None if not configured."""
        from summon_claude.github_auth import load_token  # noqa: PLC0415

        token = load_token()
        if not token:
            return None
        return {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {
                "Authorization": f"Bearer {token}",
            },
        }

    # ------------------------------------------------------------------
    # Jira integration
    # ------------------------------------------------------------------

    @property
    def jira_enabled(self) -> bool:
        """True if Jira credentials are present on disk (fast stat check, no network I/O)."""
        from summon_claude.jira_auth import jira_credentials_exist  # noqa: PLC0415

        return jira_credentials_exist()

    def jira_mcp_config(self) -> dict | None:
        """Return Jira remote MCP server config, or None if not configured.

        SC-03: Only the access_token is placed in the MCP config header.
        refresh_token and client_secret remain on disk only.

        Note: production session startup (``session.py``) inlines this logic
        because it must call ``refresh_jira_token_if_needed()`` (async) before
        the sync ``load_jira_token()`` call.  This method is for diagnostics
        and tests only — it does NOT trigger a token refresh.
        """
        from summon_claude.jira_auth import load_jira_token  # noqa: PLC0415

        token = load_jira_token()
        if token is None:
            return None
        access_token = token.get("access_token")
        if not access_token:
            return None
        return {
            "type": "http",
            "url": "https://mcp.atlassian.com/v1/mcp",
            "headers": {
                "Authorization": f"Bearer {access_token}",
            },
        }

    @classmethod
    def from_file(cls, config_path: str | None = None) -> SummonConfig:
        """Load config from env file, re-raising Pydantic errors with clean messages.

        Pydantic's ``ValidationError`` includes ``input_value`` dumps that can
        leak secret values.  This method catches those and re-raises a plain
        ``ValueError`` listing only the field names and error types.
        """
        from pydantic import ValidationError  # noqa: PLC0415

        try:
            if config_path:
                return cls(_env_file=config_path)
            return cls()
        except ValidationError as exc:
            missing = [str(err["loc"][0]) for err in exc.errors() if err["type"] == "missing"]
            if missing:
                msg = f"{len(missing)} required field(s) missing: {', '.join(missing)}"
            else:
                parts = []
                for err in exc.errors():
                    field = ".".join(str(loc) for loc in err["loc"])
                    parts.append(f"{field}: {err['msg']}")
                msg = f"{exc.error_count()} validation error(s):\n" + "\n".join(
                    f"  - {p}" for p in parts
                )
            raise ValueError(msg) from None

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
    format_hint: str | None = None  # Short format pattern shown before secret prompts


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
    explicit = cfg.get("SUMMON_SCRIBE_ENABLED", "")
    if explicit:
        return _is_truthy(explicit)
    # Auto-detect: enabled when any sub-feature's prerequisites are met.
    # Uses raw detection primitives (not _scribe_slack_enabled)
    # to avoid circular dependency — that function gates on _scribe_enabled.
    google_detected = _workspace_mcp_installed() and _google_credentials_exist()
    slack_detected = is_extra_installed("playwright") and _slack_browser_auth_exists()
    return google_detected or slack_detected


def _google_credentials_exist() -> bool:
    """Check if a user has completed Google OAuth (credential file exists).

    Lightweight check that does NOT trigger migration — used as a visibility
    callback in the config wizard where side effects are undesirable.
    Checks both subdirectory layout (post-migration) and flat layout (pre-migration).
    """
    creds_dir = get_google_credentials_dir()
    if not creds_dir.exists():
        return False
    # Check for subdirectory layout (post-migration) — validate labels to
    # stay consistent with discover_google_accounts() (prevents auto-enabling
    # scribe for directories that discover_google_accounts would skip).
    for item in creds_dir.iterdir():
        if (
            item.is_dir()
            and not item.name.startswith(".")
            and ACCOUNT_LABEL_RE.match(item.name)
            and item.name not in RESERVED_ACCOUNT_LABELS
            and (item / "client_env").exists()
            and any(f.suffix == ".json" and "@" in f.stem for f in item.glob("*.json"))
        ):
            return True
    # Check for flat layout (pre-migration, backward compat).
    # Requires both client_env AND credential JSON — consistent with
    # discover_google_accounts() which also requires both.  Env-var-only
    # users (no client_env) must run ``summon auth google setup`` first.
    return (creds_dir / "client_env").exists() and any(
        f.suffix == ".json" and "@" in f.stem for f in creds_dir.glob("*.json")
    )


def _slack_browser_auth_exists() -> bool:
    """Check if valid Slack browser auth state exists (Playwright cookies).

    Lightweight check: verifies the workspace config file references a state
    file that exists.  Does NOT check cookie expiry — that's validated at
    runtime by the browser monitor.
    """
    import json as _json  # noqa: PLC0415

    ws_config = get_workspace_config_path()
    if not ws_config.exists():
        return False
    try:
        data = _json.loads(ws_config.read_text())
    except (ValueError, OSError):
        return False
    state_path = Path(data.get("auth_state_path", ""))
    return state_path.is_file()


def _scribe_slack_enabled(cfg: dict[str, str]) -> bool:
    # Explicit config takes precedence.  If unset, auto-detect from
    # browser auth: if Playwright is installed AND a saved auth state
    # file exists, Slack is enabled automatically.
    explicit = cfg.get("SUMMON_SCRIBE_SLACK_ENABLED", "")
    if explicit:
        return _scribe_enabled(cfg) and _is_truthy(explicit) and is_extra_installed("playwright")
    return (
        _scribe_enabled(cfg) and is_extra_installed("playwright") and _slack_browser_auth_exists()
    )


def _validate_permission_timeout(v: str) -> str | None:
    try:
        return None if int(v) >= 0 else "Must be >= 0 (0 = no timeout)"
    except (ValueError, TypeError):
        return "Must be an integer"


def _validate_scan_interval_minutes(v: str) -> str | None:
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


_FALLBACK_MODEL_CHOICES: tuple[str, ...] = (
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-opus-4-5",
)


def _default_sentinel(default_model: str | None) -> str:
    """Build the 'default (...)' sentinel label for the model picker."""
    if default_model:
        return f"default (currently: {default_model})"
    return "default (auto)"


def get_model_choices() -> list[str]:
    """Return model choices for the init wizard and config set.

    Tries the TTL cache first; falls back to _FALLBACK_MODEL_CHOICES.
    Always prepends a "default (...)" sentinel and appends "other".
    Wraps entirely in try/except — ImportError from model_cache is non-fatal.
    """
    try:
        from summon_claude.cli.model_cache import load_cached_models  # noqa: PLC0415

        result = load_cached_models()
        if result is not None:
            models, default_model = result
            values = [v for m in models if (v := m.get("value"))]
            if values:
                return [_default_sentinel(default_model), *values, "other"]
    except Exception:
        logger.debug("Failed to load cached models", exc_info=True)
    return [_default_sentinel(None), *_FALLBACK_MODEL_CHOICES, "other"]


def _warn_unrecognized_model(value: str) -> str | None:
    """Soft-validate a model string; warn but never block.

    Returns None always. Emits a click.echo warning if value does not
    prefix-match any key in CONTEXT_WINDOW_SIZES (the canonical known-model set).
    """
    try:
        import click  # noqa: PLC0415

        from summon_claude.sessions.context import CONTEXT_WINDOW_SIZES  # noqa: PLC0415

        if not any(value.startswith(prefix) for prefix in CONTEXT_WINDOW_SIZES):
            click.echo(
                f"Warning: '{value}' is not a recognized model. It will be used as-is.",
                err=True,
            )
    except Exception:
        logger.debug("Failed to warn unrecognized model", exc_info=True)
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
        format_hint="xoxb-...",
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
        format_hint="xapp-...",
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
        format_hint="hex string, 32 chars",
    ),
    # Session Defaults
    ConfigOption(
        field_name="default_model",
        env_key="SUMMON_DEFAULT_MODEL",
        group="Session Defaults",
        label="Default Model",
        help_text="Claude model to use for sessions (e.g. claude-opus-4-6)",
        input_type="choice",
        choices_fn=get_model_choices,
        validate_fn=_warn_unrecognized_model,
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
        help_text="Enable the background scribe agent (auto-detected from Google/Slack)",
        input_type="flag",
        help_hint=(
            "Background agent that monitors notifications,"
            " triages by importance, and posts daily summaries"
        ),
    ),
    ConfigOption(
        field_name="scribe_scan_interval_minutes",
        env_key="SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES",
        group="Scribe",
        label="Scan Interval (minutes)",
        help_text="How often the scribe agent scans for new data",
        input_type="int",
        visible=_scribe_enabled,
        validate_fn=_validate_scan_interval_minutes,
    ),
    ConfigOption(
        field_name="scribe_cwd",
        env_key="SUMMON_SCRIBE_CWD",
        group="Scribe",
        label="Scribe Working Directory",
        help_text="Working directory for the scribe agent",
        input_type="text",
        visible=_scribe_enabled,
        help_hint=f"Default: {get_data_dir() / 'scribe'}. Does not need to be a git repo.",
        validate_fn=lambda v: (
            None if not v or Path(v).expanduser().is_absolute() else "Must be an absolute path"
        ),
    ),
    ConfigOption(
        field_name="scribe_model",
        env_key="SUMMON_SCRIBE_MODEL",
        group="Scribe",
        label="Scribe Model",
        help_text="Claude model for the scribe agent (default: inherits default_model)",
        input_type="choice",
        choices_fn=get_model_choices,
        validate_fn=_warn_unrecognized_model,
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
        help_hint=(
            "Comma-separated words/phrases the scribe always flags as important"
            " (triggers @mention). Default: urgent, action required, deadline"
        ),
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
        help_hint="24-hour format, e.g. 22:00-07:00 for 10pm-7am. Leave empty to disable.",
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
    # Scribe Slack
    ConfigOption(
        field_name="scribe_slack_enabled",
        env_key="SUMMON_SCRIBE_SLACK_ENABLED",
        group="Scribe Slack",
        label="Enable Scribe Slack Collector",
        help_text="Enable the Slack data collector for the scribe agent",
        input_type="flag",
        visible=lambda cfg: _scribe_enabled(cfg) and is_extra_installed("playwright"),
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
        help_text="Comma-separated Slack channel IDs for the scribe collector",
        input_type="text",
        visible=lambda _config: False,  # Hidden from init — use `summon auth slack channels`
    ),
    # Global PM
    ConfigOption(
        field_name="global_pm_scan_interval_minutes",
        env_key="SUMMON_GLOBAL_PM_SCAN_INTERVAL_MINUTES",
        group="Global PM",
        label="Scan Interval (minutes)",
        help_text="How often the Global PM scans all projects",
        input_type="int",
        advanced=True,
        validate_fn=_validate_scan_interval_minutes,
    ),
    ConfigOption(
        field_name="global_pm_cwd",
        env_key="SUMMON_GLOBAL_PM_CWD",
        group="Global PM",
        label="Global PM Working Directory",
        help_text="Working directory for the Global PM",
        input_type="text",
        advanced=True,
        help_hint=f"Default: {get_data_dir() / 'global-pm'}. Does not need to be a git repo.",
        validate_fn=lambda v: (
            None
            if not v or Path(v).expanduser().is_absolute()
            else "Must be an absolute path (~ is expanded)"
        ),
    ),
    ConfigOption(
        field_name="global_pm_model",
        env_key="SUMMON_GLOBAL_PM_MODEL",
        group="Global PM",
        label="Global PM Model",
        help_text="Claude model for the Global PM (default: inherits default_model)",
        input_type="choice",
        choices_fn=get_model_choices,
        validate_fn=_warn_unrecognized_model,
        advanced=True,
    ),
    # Advanced options below this point
    # Display
    ConfigOption(
        field_name="max_inline_chars",
        env_key="SUMMON_MAX_INLINE_CHARS",
        group="Display",
        label="Max Inline Thinking Chars",
        help_text="Thinking content longer than this is uploaded as a file instead of inline",
        input_type="int",
        advanced=True,
        help_hint="Sensible range: 500-10000. Default 2500.",
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
        field_name="permission_timeout_s",
        env_key="SUMMON_PERMISSION_TIMEOUT_S",
        group="Behavior",
        label="Permission Timeout (s)",
        help_text="Seconds to wait for user approval before auto-denying (0 = no timeout)",
        input_type="int",
        advanced=True,
        validate_fn=_validate_permission_timeout,
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
    ConfigOption(
        field_name="safe_write_dirs",
        env_key="SUMMON_SAFE_WRITE_DIRS",
        group="Behavior",
        label="Safe Write Directories",
        help_text="Comma-separated dirs where writes are always allowed (e.g. hack/)",
        input_type="text",
        advanced=True,
        help_hint=(
            "Comma-separated paths relative to project root"
            " (e.g. hack/,.dev/). Absolute paths also work."
        ),
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
    # Auto Mode
    ConfigOption(
        field_name="auto_classifier_enabled",
        env_key="SUMMON_AUTO_CLASSIFIER_ENABLED",
        group="Auto Mode",
        label="Auto Classifier",
        help_text="Enable Sonnet classifier for automatic tool approval",
        input_type="flag",
        advanced=True,
    ),
    ConfigOption(
        field_name="auto_mode_environment",
        env_key="SUMMON_AUTO_MODE_ENVIRONMENT",
        group="Auto Mode",
        label="Environment Context",
        help_text="Environment description for the classifier (e.g. 'production server')",
        input_type="text",
        advanced=True,
    ),
    ConfigOption(
        field_name="auto_mode_deny",
        env_key="SUMMON_AUTO_MODE_DENY",
        group="Auto Mode",
        label="Deny Rules",
        help_text="Custom deny rules (newline-separated, overrides defaults)",
        input_type="text",
        advanced=True,
    ),
    ConfigOption(
        field_name="auto_mode_allow",
        env_key="SUMMON_AUTO_MODE_ALLOW",
        group="Auto Mode",
        label="Allow Rules",
        help_text="Custom allow rules (newline-separated, overrides defaults)",
        input_type="text",
        advanced=True,
    ),
]


def get_config_default(option: ConfigOption) -> Any:
    """Get the default value for a config option from SummonConfig."""
    field_info = SummonConfig.model_fields.get(option.field_name)
    if field_info is None:
        return None
    return field_info.default

# Configuration Reference

All configuration options can be set with `summon config set` or as environment
variables. Values in the config file (`~/.config/summon/config.env` by default)
are overridden by actual environment variables.

```{ .bash .notest }
# Set a value
summon config set SUMMON_DEFAULT_MODEL claude-opus-4-6

# View all resolved values (tokens masked)
summon config show

# Open the config file in your editor
summon config edit
```

---

## Slack Credentials

These three options must be set before summon can start. See [Slack Setup](../getting-started/slack-setup.md) for instructions on obtaining these values.

| Config Key | Type | Description |
|------------|------|-------------|
| `SUMMON_SLACK_BOT_TOKEN` | secret | Slack Bot User OAuth token. Must start with `xoxb-`. |
| `SUMMON_SLACK_APP_TOKEN` | secret | Slack App-Level token for Socket Mode. Must start with `xapp-`. |
| `SUMMON_SLACK_SIGNING_SECRET` | secret | Slack signing secret for request verification (hex string). |

---

## Session Defaults

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_DEFAULT_MODEL` | text | _(Claude's default)_ | Claude model to use for new sessions (e.g. `claude-opus-4-6`). |
| `SUMMON_DEFAULT_EFFORT` | choice: `low`, `medium`, `high`, `max` | `high` | Thinking effort level for new sessions. |
| `SUMMON_CHANNEL_PREFIX` | text | `summon` | Prefix for auto-created Slack channel names. Channels are named `{prefix}-{session-name}`. Must be lowercase alphanumeric, hyphens, and underscores only. |

See [Sessions](../guide/sessions.md) for how model and effort affect behavior.

---

## Scribe

Core settings for the background scribe agent. See [Scribe](../guide/scribe.md) for full setup and configuration details.

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_SCRIBE_ENABLED` | boolean | `false` | Enable the background scribe agent. |
| `SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES` | integer | `5` | How often the scribe scans for new information. Minimum 1. |
| `SUMMON_SCRIBE_CWD` | text | _(data dir)/scribe_ | Working directory for the scribe session. |
| `SUMMON_SCRIBE_MODEL` | text | _(inherits default model)_ | Model override for the scribe session. |
| `SUMMON_SCRIBE_IMPORTANCE_KEYWORDS` | text | _(empty)_ | Comma-separated keywords that flag a message as high-priority (e.g. `urgent,deadline`). |
| `SUMMON_SCRIBE_QUIET_HOURS` | text | _(empty)_ | Time window for reduced alerts, format `HH:MM-HH:MM` (e.g. `22:00-07:00`). Only level-5 alerts are surfaced during this window. |

### Scribe Google

Google Workspace data collector settings. Requires the `google` optional extra (`uv tool install summon-claude[google]`).

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_SCRIBE_GOOGLE_ENABLED` | boolean | `false` | Enable the Google Workspace data collector for scribe. |
| `SUMMON_SCRIBE_GOOGLE_SERVICES` | text | `gmail,calendar,drive` | Comma-separated list of Google services to monitor. Valid values: `gmail`, `drive`, `calendar`, `docs`, `sheets`, `chat`, `forms`, `slides`, `tasks`, `contacts`, `search`, `appscript`. |

### Scribe Slack

Slack monitoring via browser automation. Requires the `slack-browser` optional extra (`uv tool install summon-claude[slack-browser]`).

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_SCRIBE_SLACK_ENABLED` | boolean | `false` | Enable the Slack data collector (uses Playwright browser automation). |
| `SUMMON_SCRIBE_SLACK_BROWSER` | choice: `chrome`, `firefox`, `webkit` | `chrome` | Browser for Slack monitoring. |
| `SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS` | text | _(empty)_ | Comma-separated Slack channel names to monitor. |

---

## GitHub

GitHub integration uses OAuth device flow authentication — no environment variable needed. Run `summon auth github login` to authenticate.

See [GitHub Integration](../guide/github-integration.md) for setup details.

---

## Global PM

!!! note "Advanced"
    These options are hidden behind "Configure advanced settings?" in the `summon init` wizard. They can always be set directly with `summon config set`.

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_GLOBAL_PM_SCAN_INTERVAL_MINUTES` | integer | `15` | How often the Global PM scans all projects (minutes, minimum 1). |
| `SUMMON_GLOBAL_PM_CWD` | text | _(data dir)_ | Working directory for the Global PM. Must be an absolute path. Defaults to `<data-dir>/global-pm`. |
| `SUMMON_GLOBAL_PM_MODEL` | text | _(inherit)_ | Claude model for the Global PM. Defaults to `SUMMON_DEFAULT_MODEL`. |

---

## Display

!!! note "Advanced"
    These options are hidden behind "Configure advanced settings?" in the `summon init` wizard. They can always be set directly with `summon config set`.

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_MAX_INLINE_CHARS` | integer | `2500` | Maximum characters for inline Slack messages. Responses longer than this are uploaded as files. |

---

## Behavior

!!! note "Advanced"
    These options are hidden behind "Configure advanced settings?" in the `summon init` wizard. They can always be set directly with `summon config set`.

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_PERMISSION_DEBOUNCE_MS` | integer | `500` | Milliseconds to wait before posting a permission request to Slack. Batches rapid tool approvals into a single message. |
| `SUMMON_NO_UPDATE_CHECK` | boolean | `false` | Disable the background PyPI update check on `summon start`. |

---

## Thinking

!!! note "Advanced"
    These options are hidden behind "Configure advanced settings?" in the `summon init` wizard. They can always be set directly with `summon config set`.

| Config Key | Type | Default | Description |
|------------|------|---------|-------------|
| `SUMMON_ENABLE_THINKING` | boolean | `true` | Enable extended thinking (passes `ThinkingConfigAdaptive` to the Claude SDK). Set to `false` to disable. |
| `SUMMON_SHOW_THINKING` | boolean | `false` | Route thinking block content to the Slack turn thread so thinking is visible. By default thinking is processed but not posted. |

---

## Standard Variables That Affect summon

These are not summon-specific, but summon respects them:

| Variable | Description |
|----------|-------------|
| `NO_COLOR` | Disable colored terminal output. summon checks this alongside `--no-color`. |
| `EDITOR` | Editor opened by `summon config edit` and `summon hooks set` (when no JSON argument is given). Defaults to system editor. |
| `XDG_CONFIG_HOME` | Base for summon's config directory. Config is stored at `$XDG_CONFIG_HOME/summon/config.env`. Defaults to `~/.config/summon/`. Non-absolute values are ignored. |
| `XDG_DATA_HOME` | Base for summon's data directory (SQLite database, logs, update cache). Stored at `$XDG_DATA_HOME/summon/`. Defaults to `~/.local/share/summon/`. Non-absolute values are ignored. |

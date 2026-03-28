# Configuration

summon-claude is configured entirely through environment variables with the `SUMMON_` prefix. You can set them in a config file, a `.env` file in your working directory, or directly in your shell environment.

---

## Config file location

summon follows the [XDG Base Directory spec](https://specifications.freedesktop.org/basedir/basedir-spec-latest.html):

| Variable | Config path |
|----------|-------------|
| `XDG_CONFIG_HOME` set | `$XDG_CONFIG_HOME/summon/config.env` |
| Default | `~/.config/summon/config.env` |
| Fallback (if `~/.config` missing) | `~/.summon/config.env` |

Data (database, logs) follows the same pattern under `XDG_DATA_HOME` / `~/.local/share/summon`.

Use `summon config path` to print the exact config file path in use.

---

## Loading priority

Settings are resolved in this order (later overrides earlier):

1. **Config file** (`~/.config/summon/config.env` or XDG override)
2. **`.env` file** in the current working directory
3. **Shell environment variables**

The `--config PATH` flag on the `summon` command overrides the config file path.

---

## Initial setup

Use the interactive setup wizard to create your configuration:

```{ .bash .notest }
summon init
```

See [Configuring Summon](../getting-started/configuration.md) for the full wizard walkthrough and credential setup details.

The three required Slack credentials are covered in [Slack Setup](../getting-started/slack-setup.md).

---

## Configuration options

For the complete list of all configuration options with config keys, environment variables, types, defaults, and descriptions, see the [Configuration Reference](../reference/environment-variables.md).

---


## Config subcommands

### summon config show

```bash
summon config show
```

Displays all configuration options organized by section (Slack Credentials, Session Defaults, Scribe, Scribe Google, Scribe Slack, GitHub, Display, Behavior, Thinking). Each option shows a source indicator:

- **(set)** — explicitly configured in the config file
- **(default)** — using the built-in default value
- **(not set)** — a required value that is missing
- **(optional)** — an optional secret that has not been configured

Disabled sections (e.g., Scribe Google when `scribe_google_enabled` is false) are shown dimmed with a "disabled" label.

<!-- terminal:config-show -->
```text
  Slack Credentials
    SUMMON_SLACK_BOT_TOKEN                   configured                     (set)
    SUMMON_SLACK_APP_TOKEN                   configured                     (set)
    SUMMON_SLACK_SIGNING_SECRET              configured                     (set)

  Session Defaults
    SUMMON_DEFAULT_MODEL                     claude-opus-4-6                (set)
    SUMMON_DEFAULT_EFFORT                    high                           (default)
    SUMMON_CHANNEL_PREFIX                    summon                         (default)

  Scribe: disabled

  GitHub
    GitHub: configured (OAuth)
```
<!-- /terminal:config-show -->

### summon config path

```bash
summon config path
```

Prints the absolute path to the config file in use.

### summon config set

```{ .bash .notest }
summon config set SUMMON_DEFAULT_MODEL claude-opus-4-6
summon config set SUMMON_CHANNEL_PREFIX my-team
summon config set SUMMON_SCRIBE_ENABLED true
```

Sets a single key in the config file. Creates the file if it does not exist. The key must be a valid `SUMMON_*` configuration variable — unknown keys are rejected with an error listing all valid options.

Boolean values are normalized: `true`, `false`, `yes`, `no`, `on`, `off`, `1`, and `0` are all accepted and stored as `true` or `false`. Choice-type options (like `SUMMON_DEFAULT_EFFORT`) are validated against their allowed values.

### summon config edit

```{ .bash .notest }
summon config edit
```

Opens the config file in `$EDITOR`. If `$EDITOR` is not set, falls back to `vi`.

### summon config check

```bash
summon config check
```

Validates configuration and tests connectivity. See [Configuring Summon](../getting-started/configuration.md#running-summon-config-check) for detailed output interpretation.

---

For external Slack workspace commands (browser-based monitoring), see [Scribe](scribe.md#slack-browser-monitoring).

---

## Example config file

```{ .bash .notest }
# ~/.config/summon/config.env

# Required: Slack credentials
SUMMON_SLACK_BOT_TOKEN=xoxb-your-bot-token
SUMMON_SLACK_APP_TOKEN=xapp-your-app-token
SUMMON_SLACK_SIGNING_SECRET=your-signing-secret

# Optional: model and behavior
SUMMON_DEFAULT_MODEL=claude-opus-4-6
SUMMON_DEFAULT_EFFORT=high
SUMMON_CHANNEL_PREFIX=ai

# Optional: disable update checks
# SUMMON_NO_UPDATE_CHECK=true
```

!!! tip "Secret management"
    The config file is created with `0600` permissions by `summon init`. For team environments or CI, prefer injecting secrets via environment variables rather than committing a config file.

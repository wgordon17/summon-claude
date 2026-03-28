# Configuring Summon

After creating your Slack app and collecting credentials, configure summon-claude using the interactive setup wizard or manual commands.

## Running `summon init`

The fastest way to configure summon-claude is the interactive setup wizard:

```{ .bash .notest }
summon init
```

The wizard walks through configuration options in groups, prompting for each value. If an existing config file is found, current values are shown as defaults -- press Enter to keep them.

**Slack Credentials** (required) -- The wizard asks for your three Slack credentials in order:

1. **Bot Token** -- the `xoxb-` token from OAuth & Permissions
2. **App Token** -- the `xapp-` token from Basic Information > App-Level Tokens
3. **Signing Secret** -- the hex string from Basic Information > App Credentials

Each credential is entered as a hidden input (like a password prompt). The wizard validates format as you go -- for example, it rejects a Bot Token that doesn't start with `xoxb-`.

**Session Defaults** -- Next, the wizard asks for optional session defaults:

- **Default Model** -- which Claude model to use (press Enter to accept the default)
- **Default Effort** -- thinking effort level (`low`, `medium`, `high`, or `max`)
- **Channel Prefix** -- prefix for Slack channel names created by summon

**Scribe** -- The wizard asks whether to enable the Scribe background agent. If you enable it, follow-up prompts appear for scan interval, working directory, model, importance keywords, and quiet hours. Sub-collectors for Google Workspace and Slack are offered if the required dependencies (`workspace-mcp`, `playwright`) are installed.

**GitHub** -- An optional prompt to authenticate with GitHub via OAuth device flow. If configured, all sessions get GitHub tools (search code, read PRs, etc.).

**Advanced settings** -- Finally, the wizard asks "Configure advanced settings?" If you decline (the default), it skips display and behavior tuning options. Most users can skip this.

After all prompts, the wizard validates the full configuration, writes it to `~/.config/summon/config.env` (permissions set to `600`), and automatically runs `summon config check` to verify everything works.

---

## Running `summon config check`

After initial setup -- or any time you want to verify your configuration -- run:

```bash
summon config check
```

This validates credentials, checks token formats, tests database writability, and confirms Slack API connectivity. Here is example output from a working installation:

<!-- terminal:config-check -->
```text
  [PASS] Claude CLI found (2.1.80 (Claude Code))
  [PASS] SUMMON_SLACK_BOT_TOKEN is set
  [PASS] SUMMON_SLACK_APP_TOKEN is set
  [PASS] SUMMON_SLACK_SIGNING_SECRET is set
  [PASS] Bot token format is valid (xoxb-)
  [PASS] App token format is valid (xapp-)
  [PASS] Signing secret format looks valid (hex)
  [PASS] Config values pass validation
  [PASS] DB path is writable: ~/.local/share/summon/registry.db
  [PASS] Schema version 14 (current)
  [PASS] Database integrity OK
  [INFO] Sessions: 24, Audit log: 59
  [PASS] Slack API reachable (team: my-workspace)
  [PASS] Slack bot scopes: all 16 required scopes granted
  [INFO] GitHub: not configured (run `summon auth github login`)
  Google: no credentials found
  [INFO] Google Workspace: not configured (summon auth google login)
  [INFO] workspace-mcp (Google): installed
  [INFO] playwright (Slack browser): installed

Features:
  [INFO] Projects: none registered (summon project add)
  [INFO] Workflow instructions: not set (summon project workflow set)
  [INFO] Lifecycle hooks: not set (summon hooks set)
  [INFO] Hook bridge: not installed (summon hooks install)

Getting started:
  summon project add <path>           Register a project directory
  summon project workflow set          Set workflow instructions
  summon hooks install                Install Claude Code hook bridge
  summon project up                   Start PM agents for all projects
```
<!-- /terminal:config-check -->

Every `[PASS]` line is a check that succeeded. `[INFO]` lines report status or suggest optional next steps. If anything fails, a `[FAIL]` line appears with a specific error message and remediation hint.

---

## Setting individual values

To change a single setting without re-running the full wizard:

```{ .bash .notest }
summon config set SUMMON_DEFAULT_MODEL claude-sonnet-4-20250514
```

This updates the config file in place. Use the `SUMMON_` environment variable name as the key.

---

## Viewing current configuration

To see all current settings and their sources:

```bash
summon config show
```

This displays every configuration value grouped by category, with indicators showing whether each value comes from the config file, an environment variable, or the built-in default.

---

## Next steps

With configuration verified, you're ready to start your first session:

[Quick Start](quickstart.md){ .md-button .md-button--primary }

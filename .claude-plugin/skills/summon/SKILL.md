---
name: summon
description: Start a Slack-bridged Claude Code session using summon-claude. Use when the user wants to "start a Slack session", "summon to Slack", "bridge to Slack", "share this session on Slack", or "connect to Slack".
---

# Skill: summon — Bridge Claude Code to Slack

## When to use this skill

Use this skill when the user asks you to:
- Start a Slack session
- Summon Claude to Slack
- Bridge the current session to Slack
- Share this session on Slack
- Connect to Slack

## Prerequisites check

Before starting, verify:

1. **summon is installed** — run `summon --help` and check for exit code 0. If not installed, run `uv tool install summon-claude` to install it.
2. **Config exists** — run `summon config path` and check the output file exists. If not configured, run `summon init` to start the setup wizard.

## Starting a session

Use the Bash tool with `run_in_background: true`:

```bash
summon start
```

Or with options:
```bash
summon start --cwd /path/to/project --name my-feature --model claude-opus-4-6
```

Flags:
- `--cwd PATH` — working directory for Claude (default: current directory)
- `--name NAME` — session name used for Slack channel naming
- `--model MODEL` — override the default Claude model
- `--resume SESSION_ID` — resume an existing Claude Code session by ID
- `--background` / `-b` — run as a background daemon (logs to `~/.local/share/summon/logs/`)

## Expected output

After running `summon start`, look for a banner like:
```
==================================================
  SUMMON CODE: ABC123
  Type in Slack: /summon ABC123
  Expires in 5 minutes
==================================================
```

Parse the 6-character code (e.g., `ABC123`) and tell the user:
> "Your summon code is **ABC123**. Type `/summon ABC123` in Slack to authenticate."

## Auth flow

1. `summon start` generates a 6-character auth code and waits
2. The user types `/summon <code>` in their Slack workspace
3. The bot verifies the code, creates a dedicated session channel, and posts a header
4. All further interaction happens in that Slack channel
5. The code expires in 5 minutes — if it expires, run `summon start` again

## Session management commands

Once started, manage sessions via the `session` group (alias: `s`):

```bash
# List active sessions
summon session list

# List all recent sessions (last 50)
summon session list --all

# Detailed view of a specific session
summon session info <SESSION_ID>

# Stop a running session
summon session stop <SESSION_ID>

# Clean up stale entries with dead processes
summon session cleanup

# View logs for a background session
summon session logs <SESSION_ID>

# Short alias: 's' = 'session'
summon s list
```

## Config management

```bash
# View current config (tokens masked)
summon config show

# Show config file path
summon config path

# Open config in $EDITOR
summon config edit

# Set a single value
summon config set SUMMON_DEFAULT_MODEL claude-sonnet-4-5
```

---

## Human installation instructions

If the user needs to set up summon-claude from scratch:

### 1. Install

```bash
uv tool install summon-claude
```

### 2. Create a Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From an app manifest**
3. Select your workspace
4. Paste the contents of `slack-app-manifest.yaml` from the summon-claude repo
5. Click **Create**, then **Install to Workspace**

### 3. Collect tokens

| Token | Where to find it |
|-------|-----------------|
| Bot Token (`xoxb-...`) | **OAuth & Permissions** → Bot User OAuth Token |
| App Token (`xapp-...`) | **Settings** → **Basic Information** → App-Level Tokens → Generate with `connections:write` scope |
| Signing Secret | **Settings** → **Basic Information** → App Credentials |

### 4. Find your Slack User ID

In Slack: click your profile picture → **Profile** → three-dot menu → **Copy member ID** (starts with `U`)

### 5. Run the setup wizard

```bash
summon init
```

Enter your tokens when prompted.

### 6. Start a session

```bash
summon start
```

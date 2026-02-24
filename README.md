# summon-claude

Bridge Claude Code sessions to Slack channels. Run `summon start` in a terminal, authenticate from Slack, and interact with Claude entirely through a dedicated Slack channel.

## Quick Start

```bash
# 1. Install
uv tool install summon-claude

# 2. Set up your Slack app (see Slack App Setup below)

# 3. Run the interactive setup wizard
summon init

# 4. Start a session
summon start
```

## Slack App Setup

### Import the manifest (recommended)

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From an app manifest**
3. Select your workspace
4. Paste the contents of `slack-app-manifest.yaml`
5. Click **Create**, then **Install to Workspace**

### Collect your tokens

| Token | Where to find it |
|-------|-----------------|
| Bot Token (`xoxb-...`) | **OAuth & Permissions** → Bot User OAuth Token |
| App Token (`xapp-...`) | **Settings** → **Basic Information** → App-Level Tokens → Generate one with `connections:write` scope |
| Signing Secret | **Settings** → **Basic Information** → App Credentials |

### Find your Slack User ID

In Slack: click your profile picture → **Profile** → click the three-dot menu → **Copy member ID**. This is the `U...` ID to put in `SUMMON_ALLOWED_USER_IDS`.

## Auth Flow

1. Run `summon start` in your terminal
2. The terminal prints:
   ```
   ==================================================
     SUMMON CODE: ABC123
     Type in Slack: /summon ABC123
     Expires in 5 minutes
   ==================================================
   ```
3. In Slack, type `/summon ABC123`
4. The bot verifies the code, creates a session channel, and posts a header
5. All further interaction happens in that Slack channel

The code expires in 5 minutes. Run `summon start` again to get a new one.

## Commands

| Command | Description |
|---------|-------------|
| `summon init` | Interactive setup wizard — creates config file with your tokens |
| `summon start` | Start a new session (prints auth code, waits for `/summon` in Slack) |
| `summon session list` | Show active sessions (use `--all` for all recent) |
| `summon session info SESSION_ID` | Show detailed view of one session |
| `summon session stop SESSION_ID` | Send SIGTERM to a running session |
| `summon session logs [SESSION_ID]` | View session logs (list files, or tail a specific session) |
| `summon session cleanup` | Mark sessions with dead processes as errored |
| `summon config show` | Show current config file (tokens masked) |
| `summon config set KEY VALUE` | Set a single config value |
| `summon config path` | Print the config file path |
| `summon config edit` | Open config file in `$EDITOR` |

> **Alias:** `summon s` is shorthand for `summon session` (e.g., `summon s list`).

### `summon start` flags

| Flag | Description |
|------|-------------|
| `--cwd PATH` | Working directory for Claude (default: current directory) |
| `--name NAME` | Session name used for Slack channel naming |
| `--model MODEL` | Override the default Claude model |
| `--resume SESSION_ID` | Resume an existing Claude Code session by ID |
| `-b`, `--background` | Run session in background as daemon (logs accessible via `summon session logs`) |

### Global flags

| Flag | Description |
|------|-------------|
| `-v`, `--verbose` | Enable verbose logging |

## Configuration

Config is loaded in priority order: environment variables → config file → local `.env`.

### Config file path (XDG-aware)

```
$XDG_CONFIG_HOME/summon/config.env   # if XDG_CONFIG_HOME is set
~/.config/summon/config.env          # default on most systems
~/.summon/config.env                 # fallback if ~/.config doesn't exist
```

Use `summon config path` to see which path is active. Use `summon init` to create the file interactively.

### Required variables

| Variable | Description |
|----------|-------------|
| `SUMMON_SLACK_BOT_TOKEN` | Bot token (`xoxb-...`) from OAuth & Permissions |
| `SUMMON_SLACK_APP_TOKEN` | App-level token (`xapp-...`) for Socket Mode |
| `SUMMON_SLACK_SIGNING_SECRET` | Signing secret from Basic Information |
| `SUMMON_ALLOWED_USER_IDS` | Comma-separated Slack user IDs allowed to use the bot |

### Optional variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SUMMON_DEFAULT_MODEL` | (SDK default) | Default Claude model |
| `SUMMON_CHANNEL_PREFIX` | `summon` | Prefix for created session channels |
| `SUMMON_PERMISSION_DEBOUNCE_MS` | `500` | Debounce window for batching permission requests (ms) |
| `SUMMON_MAX_INLINE_CHARS` | `2500` | Threshold for inline vs file upload display |

A local `.env` in the project directory overrides the config file.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          Slack                                  │
│  (channels, messages, files, interactive buttons, slash cmd)    │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Socket Mode
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    SummonSession (Orchestrator)                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Claude SDK  │  Slack Bolt  │  Auth  │  Permissions       │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
           │                               │
           ▼                               ▼
┌──────────────────────┐      ┌──────────────────────┐
│   Claude SDK         │      │   ChatProvider       │
│   (streaming)        │      │   (Slack adapter)    │
└──────────────────────┘      └──────────────────────┘
```

### Threading Model

Messages are organized into threads to keep the main channel clean:

- **Main channel**: Opens with the initial prompt and closes with Claude's final conclusion
- **Turn threads**: Each Claude turn's tool calls post to a dedicated turn thread with a summary like "🔧 Turn 3: 5 tool calls · session.py, config.py"
- **Subagent threads**: When Claude spawns subagents, their activity posts to dedicated subagent threads
- **Permissions**: Permission requests broadcast to all threads with `<!channel>` notifications

This structure keeps the main conversation readable while preserving full context in threads.

### Provider Abstraction

All Slack API calls go through a `ChatProvider` protocol, enabling future support for Discord, Teams, or CLI providers without changing core routing logic. The `SlackChatProvider` implements this protocol for Slack.

### Modules

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI entry point: start/status/stop/sessions/cleanup/init/config |
| `config.py` | pydantic-settings config with XDG path resolution and plugin discovery |
| `auth.py` | 8-char hex short codes with 5-min TTL, brute-force protection (5 attempts) |
| `registry.py` | SQLite session registry with WAL mode, heartbeat, audit log |
| `channel_manager.py` | Slack channel create/archive/header with collision handling |
| `permissions.py` | Debounced permission batching with Slack interactive buttons |
| `content_display.py` | Hybrid inline/file upload display with diff formatting |
| `streamer.py` | Claude response streaming to Slack with threaded routing |
| `thread_router.py` | Routes content to main channel, turn threads, and subagent threads |
| `session.py` | Core orchestrator: ties all modules together |
| `mcp_tools.py` | In-process MCP server: `slack_upload_file`, `slack_create_thread`, `slack_react`, `slack_post_snippet` |
| `providers/base.py` | ChatProvider protocol and message/channel abstractions |
| `providers/slack.py` | SlackChatProvider implementation for Slack API calls |
| `cli_config.py` | Config subcommand handlers: show, path, edit, set |
| `rate_limiter.py` | Per-key cooldown rate limiter for slash command spam protection |
| `_formatting.py` | Slack mrkdwn formatting helpers and tool argument extraction |

## Security

### Authentication

1. `summon start` generates a short code (8 hex characters)
2. Short code is printed to the terminal only — it is never sent to Slack automatically
3. You type `/summon <code>` in Slack; the bot verifies the code against the registry
4. Code expires after 5 minutes; locked after 5 failed attempts
5. `/summon` has a 2-second per-user rate limit

### Authorization

- Only Slack user IDs in `SUMMON_ALLOWED_USER_IDS` can authenticate or send messages
- The `/summon` slash command rejects unauthorized users before checking the code

### Permission handling

- **Auto-approved tools**: `Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, `LSP`, and other read-only operations
- **User-approved tools**: `Write`, `Edit`, `Bash`, and other destructive operations
- Approval requests are debounced (default 500ms) and batched into a single Slack message
- **Timeout**: 5 minutes — unanswered permission requests are denied automatically
- Only users in `SUMMON_ALLOWED_USER_IDS` can click approve/deny buttons

### Audit logging

All session events (`session_created`, `auth_attempted`, `auth_succeeded`, `auth_failed`, `session_active`, `session_ended`, `session_errored`, `session_stopped`) are written to the SQLite registry for audit purposes.

## Development

```bash
make install   # uv sync with dev dependencies
make lint      # ruff check + format (auto-fix), then pyright
make test      # pytest with asyncio
make all       # install → lint → test
```

## License

MIT

# summon-claude

Bridge Claude Code sessions to Slack channels. Run `summon start` in a terminal, authenticate from Slack, and interact with Claude entirely through a dedicated Slack channel.

## Installation

The package is `summon-claude` on PyPI; once installed, the CLI command is `summon`.

### Option A: uv tool (Recommended)

```bash
uv tool install summon-claude
```

### Option B: pipx

```bash
pipx install summon-claude
```

### Option C: Homebrew (macOS/Linux)

```bash
brew install wgordon17/summon/summon-claude
```

### Keeping up to date

summon checks for new versions on `summon start` and notifies you when an update is available.

To upgrade to the latest version:

| Installation method | Upgrade command |
|---------------------|-----------------|
| uv tool | `uv tool upgrade summon-claude` |
| pipx | `pipx upgrade summon-claude` |
| Homebrew | `brew upgrade summon-claude` |

To disable update checks, set the environment variable:

```bash
export SUMMON_NO_UPDATE_CHECK=1
```

## Quick Start

```bash
# 1. Set up your Slack app (see Slack App Setup below)

# 2. Run the interactive setup wizard
summon init

# 3. Start a session
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
| `summon --version` | Show CLI version |
| `summon version` | Show version and environment info (Python, platform, paths) |
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
| `summon config check` | Validate config file (keys, token format, DB writability, schema version, integrity, Slack API connectivity) |
| `summon db status` | Show schema version, integrity, and row counts (migrations apply automatically on connect) |
| `summon db reset --yes` | Delete and recreate the registry database |
| `summon db vacuum` | Compact the database and check integrity |
| `summon db purge [--older-than N] --yes` | Purge completed/errored sessions, audit logs, and expired tokens older than N days (default: 30) |

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
| `-h`, `--help` | Show help message and exit |
| `--version` | Show version and exit |
| `-v`, `--verbose` | Enable verbose logging |
| `-q`, `--quiet` | Suppress non-essential output (mutually exclusive with `--verbose`) |
| `--no-color` | Disable colored output (respects `NO_COLOR` environment variable) |
| `--config PATH` | Override config file location (default: XDG-aware path) |

`-o`, `--output {json|table}` is available on `version`, `session list`, and `session info` (default: table).

## In-Session Commands

Once a session is active in Slack, type `!`-prefixed commands to control the session without reaching Claude:

| Command | Description |
|---------|-------------|
| `!help` | Show all available commands |
| `!status` | Show session status (model, turns, cost, uptime) |
| `!end` | End the current session |
| `!model` | Show the active model |
| `!model <name>` | Switch model (takes effect on next session start) |

**Aliases:** `!quit`, `!exit`, and `!logout` all map to `!end`.

**Passthrough commands:** Claude SDK slash commands (e.g., `/compact`, `/clear`) are also available as `!compact`, `!clear`, etc. — they are forwarded to the SDK as their `/` equivalents.

**Blocked commands:** `!login` is blocked in Slack sessions.

Use `!help` in a session to see the full list, including any passthrough commands discovered from the SDK.

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

### Optional variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SUMMON_DEFAULT_MODEL` | (SDK default) | Default Claude model |
| `SUMMON_CHANNEL_PREFIX` | `summon` | Prefix for created session channels |
| `SUMMON_PERMISSION_DEBOUNCE_MS` | `500` | Debounce window for batching permission requests (ms) |
| `SUMMON_MAX_INLINE_CHARS` | `2500` | Threshold for inline vs file upload display |
| `SUMMON_NO_UPDATE_CHECK` | (unset) | Set to `1` to disable update notifications on `summon start` |

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
│  BoltRouter (single Bolt app for the daemon)                    │
│  Rate limiter · Health monitor · Event routing                  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│  EventDispatcher (routes events by channel → session)            │
└──────────────────────────┬───────────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
┌──────────────────┐ ┌──────────┐ ┌──────────┐
│  SessionManager  │ │ Session  │ │ Session  │  (N concurrent sessions)
│  IPC · lifecycle │ │          │ │          │
└──────────────────┘ └─────┬────┘ └──────────┘
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
  ┌──────────────┐  ┌─────────────┐  ┌───────────────┐
  │  SlackClient │  │ ThreadRouter│  │ ResponseStream│
  │  (output)    │  │ (routing)   │  │ (streaming)   │
  └──────────────┘  └─────────────┘  └───────────────┘
```

### Threading Model

Messages are organized into threads to keep the main channel clean:

- **Main channel**: Opens with the initial prompt and closes with Claude's final conclusion
- **Turn threads**: Each Claude turn's tool calls post to a dedicated turn thread with a summary like "🔧 Turn 3: 5 tool calls · session.py, config.py"
- **Subagent threads**: When Claude spawns subagents, their activity posts to dedicated subagent threads
- **Permissions**: Permission requests broadcast to all threads with `<!channel>` notifications

This structure keeps the main conversation readable while preserving full context in threads.

### Slack Integration

Slack input flows through `BoltRouter` (a single shared Bolt app per daemon), which dispatches events to sessions via `EventDispatcher`. Slack output goes through `SlackClient` (channel-bound posting, reactions, file uploads) and `ThreadRouter` (thread-aware message routing to main channel, turn threads, and subagent threads).

### Modules

| Module | Purpose |
|--------|---------|
| `config.py` | pydantic-settings config with XDG path resolution and plugin discovery |
| `daemon.py` | Unix daemon with PID/lock management, IPC framing |
| `event_dispatcher.py` | Routes Slack events to session handles by channel |
| `cli/__init__.py` | CLI entry point: global flags, subcommands, daemon interaction |
| `cli/config.py` | Config subcommand handlers: show, path, edit, set, check |
| `cli/daemon_client.py` | Typed async client for daemon Unix socket control API |
| `cli/update_check.py` | PyPI update checker with 24h cache, shown on `summon start` |
| `sessions/session.py` | Session orchestrator: ties Claude SDK + Slack + permissions + streaming together |
| `sessions/manager.py` | Session lifecycle, IPC control plane, daemon coordination |
| `sessions/response.py` | Response streaming, text splitting, turn summaries |
| `sessions/permissions.py` | Debounced permission batching with Slack interactive buttons |
| `sessions/auth.py` | 8-char hex short codes with 5-min TTL, brute-force protection (5 attempts) |
| `sessions/commands.py` | `!`-prefixed command dispatch: local handlers, passthrough, blocking, aliasing |
| `sessions/context.py` | Context window usage tracking |
| `sessions/registry.py` | SQLite session registry with WAL mode, schema versioning, heartbeat, audit log |
| `slack/bolt.py` | Slack Bolt app, rate limiter, health monitor, event routing |
| `slack/client.py` | Channel-bound Slack output client (post, update, react, upload) |
| `slack/router.py` | Thread-aware message routing (main channel, turn threads, subagent threads) |
| `slack/mcp.py` | MCP tools for Claude to interact with Slack |

## Security

### Authentication

1. `summon start` generates a short code (8 hex characters)
2. Short code is printed to the terminal only — it is never sent to Slack automatically
3. You type `/summon <code>` in Slack; the bot verifies the code against the registry
4. Code expires after 5 minutes; locked after 5 failed attempts
5. `/summon` has a 2-second per-user rate limit

### Permission handling

- **Auto-approved tools**: `Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, `LSP`, and other read-only operations
- **User-approved tools**: `Write`, `Edit`, `Bash`, and other destructive operations
- Approval requests are debounced (default 500ms) and batched into a single Slack message
- **Timeout**: 5 minutes — unanswered permission requests are denied automatically

### Audit logging

All session events (`session_created`, `auth_attempted`, `auth_succeeded`, `auth_failed`, `session_active`, `session_ended`, `session_errored`, `session_stopped`) are written to the SQLite registry for audit purposes.

## Development

```bash
make install            # uv sync + install git hooks
make lint               # ruff check + format (auto-fix)
make test               # pytest with asyncio
make build              # build sdist and wheel
make clean              # remove build artifacts and cache
make all                # install → lint → test
make py-typecheck       # pyright type checking
make py-test-quick      # fast tests (exclude slow, fail-fast)
make repo-hooks-install # install prek pre-commit hooks
make repo-hooks-clean   # remove hooks and cache
```

## License

MIT

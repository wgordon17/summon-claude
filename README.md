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
| `summon session list` | Show active sessions (use `--all` for all recent, `--name` to filter) |
| `summon session info SESSION` | Show detailed view of one session (by name or ID) |
| `summon stop SESSION` | Stop a session (by name or ID), or `--all` to stop all |
| `summon session logs [SESSION]` | View session logs (by name or ID, or list available) |
| `summon session cleanup` | Mark sessions with dead processes as errored |
| `summon config show` | Show current config file (tokens masked) |
| `summon config set KEY VALUE` | Set a single config value |
| `summon config path` | Print the config file path |
| `summon config edit` | Open config file in `$EDITOR` |
| `summon config check` | Validate config file (keys, token format, DB writability, schema version, integrity, Slack API connectivity) |
| `summon config google-auth` | Authenticate with Google Workspace for scribe monitoring |
| `summon config google-status` | Check Google Workspace authentication status |
| `summon db status` | Show schema version, integrity, and row counts (migrations apply automatically on connect) |
| `summon db reset --yes` | Delete and recreate the registry database |
| `summon db vacuum` | Compact the database and check integrity |
| `summon db purge [--older-than N] --yes` | Purge completed/errored sessions, audit logs, and expired tokens older than N days (default: 30) |

> **Alias:** `summon s` is shorthand for `summon session` (e.g., `summon s list`).

### `summon start` flags

| Flag | Description |
|------|-------------|
| `--cwd PATH` | Working directory for Claude (default: current directory) |
| `--name NAME` | Session name (default: `<cwd>-<hex6>`, e.g. `myproject-a1b2c3`) |
| `--model MODEL` | Override the default Claude model |
| `--resume SESSION_ID` | Resume an existing Claude Code session by ID |
| `--effort LEVEL` | Effort level: `low`, `medium`, `high`, `max` (default: `high`, or `SUMMON_DEFAULT_EFFORT`) |

### Global flags

| Flag | Description |
|------|-------------|
| `-h`, `--help` | Show help message and exit |
| `--version` | Show version and exit |
| `-v`, `--verbose` | Enable verbose logging |
| `-q`, `--quiet` | Suppress non-essential output (mutually exclusive with `--verbose`) |
| `--no-color` | Disable colored output (respects `NO_COLOR` environment variable) |
| `--config PATH` | Override config file location (default: XDG-aware path) |
| `--no-interactive` | Disable interactive prompts |

`-o`, `--output {json|table}` is available on `version`, `session list`, and `session info` (default: table).

## In-Session Commands

Once a session is active in Slack, type `!`-prefixed commands to control the session without reaching Claude:

| Command | Description |
|---------|-------------|
| `!help` | Show all available commands |
| `!status` | Show session status (model, effort, turns, cost, uptime) |
| `!end` | End the current session |
| `!stop` | Cancel the current Claude turn |
| `!clear` | Clear conversation history |
| `!model` | Show the active model |
| `!model <name>` | Switch the model for the current session |
| `!effort` | Show the current effort level |
| `!effort <level>` | Switch effort (`low`, `medium`, `high`, `max`) |
| `!compact [instructions]` | Compact conversation context |

**Aliases:** `!quit`, `!exit`, and `!logout` all map to `!end`. `!new` and `!reset` map to `!clear`.

**Passthrough commands:** Some Claude SDK slash commands are available as `!`-prefixed passthroughs — forwarded to the SDK as `/` equivalents. Active passthroughs: `!review`, `!init`, `!pr-comments`, `!security-review`, `!debug`, `!claude-developer-platform`.

**Blocked commands:** `!login`, `!context`, `!cost`, `!insights`, and `!release-notes` are blocked or redirected in Slack sessions. CLI-only commands (e.g., `!config`, `!mcp`, `!plan`) are blocked with a message. Use `!help` to see all blocked commands and reasons.

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
| `SUMMON_DEFAULT_EFFORT` | `high` | Default effort level (`low`, `medium`, `high`, `max`) |
| `SUMMON_CHANNEL_PREFIX` | `summon` | Prefix for created session channels |
| `SUMMON_PERMISSION_DEBOUNCE_MS` | `500` | Debounce window for batching permission requests (ms) |
| `SUMMON_MAX_INLINE_CHARS` | `2500` | Threshold for inline vs file upload display |
| `SUMMON_NO_UPDATE_CHECK` | (unset) | Set to `1` to disable update notifications on `summon start` |
| `SUMMON_ENABLE_THINKING` | `true` | Enable adaptive thinking tokens in Claude responses |
| `SUMMON_SHOW_THINKING` | `false` | Post thinking content to turn threads in Slack |
| `SUMMON_SCRIBE_ENABLED` | `false` | Enable scribe monitoring agent (PM agent system — preview) |
| `SUMMON_SCRIBE_MODEL` | (inherit) | Model for the scribe agent; defaults to `SUMMON_DEFAULT_MODEL` (PM agent system — preview) |
| `SUMMON_SCRIBE_GOOGLE_SERVICES` | `gmail,calendar,drive` | Comma-separated Google Workspace services for scribe (PM agent system — preview) |
| `SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES` | `5` | How often the scribe agent polls for new data (PM agent system — preview) |
| `SUMMON_SCRIBE_SLACK_ENABLED` | `false` | Enable Slack channel monitoring via browser scraping in scribe (PM agent system — preview) |

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
  └──────┬───────┘  └─────────────┘  └───────────────┘
         │
         ▼
  ┌──────────────┐
  │  CanvasStore │  (SQLite-backed canvas sync)
  └──────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  summon CLI MCP server (summon_cli_mcp.py)                       │
│  session_list · session_info · session_start · session_stop      │
│  (wired into Claude sessions via SessionManager + Registry)      │
└──────────────────────────────────────────────────────────────────┘
```

### Threading Model

Messages are organized into threads to keep the main channel clean:

- **Main channel**: Text before any tool use in a turn posts to the main channel. After tool use, Claude's conclusion posts to the main channel with an `@mention` prefix on the first chunk.
- **Turn threads**: Each Claude turn opens a thread starter message (`🔧 Turn N: re: _snippet_...`). All tool use and tool results stream into this thread. The thread starter updates with a summary on completion (`5 tool calls · session.py, config.py · 42k/200k (21%)`).
- **Subagent threads**: When Claude uses the `Task` tool, a dedicated subagent thread is created for that agent's activity.
- **Permissions**: Permission requests post to the active thread with `<!channel>` notification.

### Slack UX

**Emoji lifecycle on user messages:**

1. `:inbox_tray:` — added when summon receives the message (pre-send acknowledgement)
2. `:gear:` — swapped in when Claude starts processing the turn
3. On turn completion, `:gear:` is replaced by one of:
   - `:white_check_mark:` — turn completed successfully
   - `:octagonal_sign:` — turn was aborted via `!stop`
   - `:warning:` — an error occurred during the turn

**Thinking blocks:** When `SUMMON_ENABLE_THINKING=true` (default), the SDK sends thinking tokens to Claude. If `SUMMON_SHOW_THINKING=true`, thinking content is posted to the turn thread prefixed with `:thought_balloon:`. Large thinking blocks (over `SUMMON_MAX_INLINE_CHARS`) are uploaded as `thinking.md` files.

**Turn headers:** Show a snippet of the user's message, truncated to 60 characters with mrkdwn special characters stripped.

Slack input flows through `BoltRouter` (a single shared Bolt app per daemon), which dispatches events to sessions via `EventDispatcher`. Slack output goes through `SlackClient` (channel-bound posting, reactions, file uploads) and `ThreadRouter` (thread-aware message routing).

### Modules

| Module | Purpose |
|--------|---------|
| `config.py` | pydantic-settings config with XDG path resolution and plugin discovery |
| `daemon.py` | Unix daemon with PID/lock management, IPC framing |
| `event_dispatcher.py` | Routes Slack events to session handles by channel |
| `summon_cli_mcp.py` | MCP tools exposing session lifecycle management to Claude agents |
| `cli/__init__.py` | CLI entry point: global flags, subcommands, daemon interaction |
| `cli/config.py` | Config subcommand handlers: show, path, edit, set, check, google-auth |
| `cli/daemon_client.py` | Typed async client for daemon Unix socket control API |
| `cli/db.py` | Database maintenance command logic (status, reset, vacuum, purge) |
| `cli/formatting.py` | Formatting helpers for CLI output (echo, format_json, session tables) |
| `cli/helpers.py` | Session resolution and stop helpers for CLI commands |
| `cli/interactive.py` | Interactive terminal selection with TTY-aware fallback |
| `cli/session.py` | Session subcommand logic (list, info, logs, cleanup) |
| `cli/start.py` | Start command logic (auth code flow, daemon delegation) |
| `cli/stop.py` | Stop command logic (by name/ID or --all) |
| `cli/update_check.py` | PyPI update checker with 24h cache, shown on `summon start` |
| `sessions/session.py` | Session orchestrator: Claude SDK + Slack + permissions + streaming + pre-send architecture |
| `sessions/manager.py` | Session lifecycle, IPC control plane, daemon coordination |
| `sessions/migrations.py` | Schema versioning and migration functions (single source of truth for DB changes) |
| `sessions/response.py` | Response streaming, turn threads, emoji lifecycle, turn summaries |
| `sessions/permissions.py` | Debounced permission batching with Slack interactive buttons |
| `sessions/auth.py` | 8-char hex short codes with 5-min TTL, brute-force protection (5 attempts) |
| `sessions/commands.py` | `!`-prefixed command dispatch: local handlers, passthrough, blocking, aliasing, plugin skills |
| `sessions/context.py` | Context window usage tracking via JSONL transcript parsing |
| `sessions/registry.py` | SQLite session registry with WAL mode, schema versioning, heartbeat, audit log |
| `slack/bolt.py` | Slack Bolt app, rate limiter, health monitor, event routing |
| `slack/canvas_store.py` | SQLite-backed canvas markdown state with background Slack sync |
| `slack/canvas_templates.py` | Canvas markdown templates for different agent profiles |
| `slack/client.py` | Channel-bound Slack output client (post, update, react, upload, canvas) |
| `slack/formatting.py` | Markdown-to-Slack-mrkdwn conversion |
| `slack/mcp.py` | MCP tools for Claude to read and interact with Slack channels |
| `slack/router.py` | Thread-aware message routing (main channel, turn threads, subagent threads) |

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

# Sessions

A summon session is a Claude Code process connected to a Slack channel. Each session has its own channel, conversation history, and working directory.

---

## Starting a session

```{ .bash .notest }
summon start
```

summon registers the session with the background daemon, creates a Slack channel, and prints an auth code to your terminal:

<!-- terminal:summon-start -->
```
==================================================
  SUMMON CODE: abc123
  Type in Slack: /summon abc123
  Expires in 5 minutes
==================================================
```
<!-- /terminal:summon-start -->

Open Slack and type `/summon abc123` in any channel to claim the session. Once claimed, Claude is ready to receive messages in the dedicated channel.

### Start options

| Flag | Description |
|------|-------------|
| `--cwd PATH` | Working directory for Claude (default: current directory) |
| `--name NAME` | Channel name suffix (default: auto-generated from directory name) |
| `--model MODEL` | Model to use (default: `SUMMON_DEFAULT_MODEL` or Claude's default) |
| `--effort LEVEL` | Effort level: `low`, `medium`, `high`, `max` (default: `high`) |
| `--resume SESSION_ID` | Resume an existing Claude Code session by ID |

```{ .bash .notest }
# Start in a specific directory with a memorable name
summon start --cwd ~/projects/myapp --name myapp

# Start with a specific model and effort level
summon start --model claude-opus-4-5 --effort max

# Resume a previous session
summon start --resume <session-id>
```

### Session naming

When `--name` is not specified, summon generates a name from the current directory:

```
<directory-name>-<6-hex-chars>
```

For example, in `~/projects/myapp`, you might get `myapp-a3f9c1`. The hex suffix prevents collisions when starting multiple sessions in the same directory.

When `--name` is specified, the name is used directly. If it already exists, the command fails immediately (rather than retrying with a suffix).

The session name becomes part of the Slack channel name, so keep it short and URL-safe.

---

## Background daemon

summon runs a persistent background daemon that manages all sessions. The daemon starts automatically when you run `summon start` — you do not need to start it manually.

The daemon:

- Routes Slack messages to the correct Claude session
- Handles authentication tokens (the 5-minute codes)
- Tracks session status in a local SQLite database
- Receives requests via a Unix socket

If the daemon is not running, `summon start` will start it. If the daemon is already running, the new session is registered with the existing daemon.

---

## Listing sessions

```{ .bash .notest }
summon session list
```

Alias: `summon s list`

By default, only active sessions are shown. Options:

| Flag | Description |
|------|-------------|
| `--all` / `-a` | Show all recent sessions (active + completed + errored) |
| `--name NAME` | Filter by session name (substring match) |
| `-o json\|table` | Output format (default: `table`) |

```{ .bash .notest }
# Show all sessions
summon session list --all

# Filter by name
summon session list --name myapp

# Machine-readable output
summon session list -o json
```

---

## Inspecting a session

```{ .bash .notest }
summon session info SESSION
```

`SESSION` can be a session name or session ID (prefix match accepted). Shows:

- Session ID and name
- Status (`pending_auth`, `active`, `completed`, `errored`, `suspended`)
- Working directory
- Model and effort level
- Slack channel
- Start time and uptime
- Cost and turn count

```{ .bash .notest }
summon session info myapp-a3f9c1
summon session info a3f9c1     # ID prefix also works
```

Use `-o json` for structured output:

```{ .bash .notest }
summon session info myapp-a3f9c1 -o json
```

---

## Stopping a session

```bash
summon stop SESSION
```

Sends a shutdown signal to the session via the daemon. Claude completes its current turn before stopping.

```{ .bash .notest }
# Stop a specific session
summon stop myapp-a3f9c1

# Stop all active sessions
summon stop --all
```

!!! warning "Stopping a PM session"
    Stopping a Project Manager (PM) session while it has active child sessions will orphan those children. summon warns you before stopping a PM with active children. In non-interactive mode (`--no-interactive`), the stop is blocked.

---

## Resuming a session

Claude Code maintains conversation history across sessions using session IDs. To pick up where you left off:

```{ .bash .notest }
summon start --resume SESSION_ID
```

The session ID is shown in `summon session info` output. You can also find it in the Slack channel (Claude reports its session ID when it starts).

Resuming connects to the existing Claude Code conversation transcript — context, tool history, and prior messages are all available to Claude.

---

## Session logs

```{ .bash .notest }
summon session logs SESSION
```

Shows the last 50 log lines for a session. If `SESSION` is omitted, lists available log files.

| Flag | Description |
|------|-------------|
| `--tail N` / `-n N` | Number of lines to show (default: 50) |

```{ .bash .notest }
# Show last 100 lines
summon session logs myapp-a3f9c1 --tail 100

# List available log files
summon session logs
```

Logs are stored in the data directory (see `summon version` for the path). Each session writes to its own log file.

<!-- terminal:summon-version -->
```text
summon, version 0.0.0
Python:      3.x.x
Platform:    darwin
Config file: ~/.config/summon/config.env
Data dir:    ~/.local/share/summon
DB path:     ~/.local/share/summon/registry.db
```
<!-- /terminal:summon-version -->

---

## Cleaning up stale sessions

```{ .bash .notest }
summon session cleanup
```

Scans active sessions and marks any with dead processes as `errored`. This handles cases where a session crashed without updating its status.

```{ .bash .notest }
# Clean up stale sessions, archive their Slack channels
summon session cleanup --archive
```

By default, the associated Slack channels are preserved (left in the workspace). Pass `--archive` to archive them.

---

## Output formats

Most listing and info commands support `--output` / `-o`:

| Format | Description |
|--------|-------------|
| `table` | Human-readable table (default) |
| `json` | Machine-readable JSON (suitable for `jq`) |

```{ .bash .notest }
# Get session list as JSON
summon session list -o json | jq '.[].name'

# Get session info as JSON
summon session info myapp -o json | jq '.status'
```

---

## Session statuses

| Status | Meaning |
|--------|---------|
| `pending_auth` | Waiting for `/summon CODE` in Slack |
| `active` | Claude is running and accepting messages |
| `completed` | Session ended normally |
| `errored` | Session crashed or was cleaned up |
| `suspended` | Session paused by `summon project down` (can be restarted with `project up`; channel name restored on resume) |

---

## Channel naming on disconnect

When a session disconnects — whether it completes, errors, or is suspended — its Slack channel is renamed with a `zzz-` prefix. For example, `summon-worker-a1b2c3` becomes `zzz-summon-worker-a1b2c3`. This pushes inactive channels to the bottom of the Slack sidebar alphabetically, making it easy to distinguish active sessions from finished ones at a glance.

When a session is resumed (via `summon start --resume`, `summon project up`, or the PM's `session_resume` tool), the `zzz-` prefix is removed and the original channel name is restored.

The rename is idempotent — channels that already have the prefix are not double-prefixed. Channel names are truncated to Slack's 80-character limit if needed.

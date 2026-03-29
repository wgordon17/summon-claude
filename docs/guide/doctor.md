# Diagnostics

`summon doctor` runs a comprehensive health check across your entire summon installation — environment, daemon, database, Slack connectivity, logs, and MCP integrations — in a single command.

---

## Running diagnostics

```bash
summon doctor
```

All checks run in parallel and produce a status summary:

```
[PASS] Environment: Python 3.12.8, claude found
[INFO] Daemon: Daemon not running (start with `summon start`)
[PASS] Database: Database OK
[PASS] Slack: auth.test passed
[INFO] Logs: daemon.log and 3 session log(s) found
[SKIP] Mcp Workspace: Scribe not enabled (skipping workspace MCP check)
[SKIP] Mcp Github: No GitHub token stored (run `summon auth github login`)

7 checks: 3 passed, 2 info, 2 skipped
```

Each check produces one of five statuses:

| Status | Meaning |
|--------|---------|
| **PASS** | Check succeeded — no issues found |
| **FAIL** | Something is broken and needs attention |
| **WARN** | Potential issue that may not require immediate action |
| **INFO** | Informational — nothing wrong, just reporting state |
| **SKIP** | Check was skipped because a prerequisite is not configured |

---

## What gets checked

### Environment

Verifies your runtime environment:

- Python version (3.12+ required)
- `claude` CLI presence and version
- `uv` availability
- `gh` CLI (optional — needed for `--submit`)
- `sqlite3` CLI (optional)
- Platform and summon-claude package version

### Daemon

Checks the background daemon:

- Unix socket connectivity (is the daemon responding?)
- PID file liveness (is the recorded process still alive?)
- Orphaned sessions (active sessions in the database but no running daemon)

### Database

Inspects the SQLite registry:

- Database file existence and size
- Schema version (is it current, behind, or ahead of the installed code?)
- SQLite integrity check (`PRAGMA integrity_check`)
- Row counts for all tables (sessions, audit_log, spawn_tokens, etc.)

### Slack

Tests Slack API connectivity:

- Calls `auth.test` with your bot token
- Reports connection failures, timeouts, and invalid tokens
- Skipped if Slack tokens are not configured

### Logs

Inspects log files for diagnostics:

- Daemon and session log presence, size, and age
- Error and warning counts across collected log lines
- Total log disk usage
- Staleness detection (warns if all logs are older than 7 days)
- Collects the last 100 lines from the daemon log and most recent session log (available in verbose mode and exports)

### Workspace MCP (Google)

Checks Google Workspace integration (only when scribe is enabled):

- `workspace-mcp` binary presence
- Google credentials directory and `client_secret.json`
- OAuth scope validation against configured services

### GitHub MCP

Validates the stored GitHub OAuth token:

- Calls the GitHub API to verify the token
- Reports invalid, expired, or missing tokens
- Skipped if no GitHub token is stored

---

## Verbose output

Pass `-v` to the root `summon` command for detailed output including per-check details, suggestions, and log tails:

```bash
summon -v doctor
```

Note: `-v` is a global flag on `summon`, not on `doctor` — it must come before the subcommand.

Verbose mode shows:

- **Details** — itemized sub-findings for each check
- **Suggestions** — actionable next steps when issues are found
- **Collected logs** — the last 20 lines of each collected log (up to 100 lines are collected per log file), redacted

---

## Exporting results

Export the full diagnostic report as a JSON file:

```{ .bash .notest }
summon doctor --export report.json
```

The export includes all check results with details, suggestions, and collected log tails. All output is redacted before writing (see [Redaction](#redaction) below).

The JSON structure:

```json
{
  "version": "1.0",
  "timestamp": "2026-03-26T20:00:00+00:00",
  "summon_version": "0.12.0",
  "checks": [
    {
      "status": "pass",
      "subsystem": "environment",
      "message": "Python 3.12.8, claude found",
      "details": ["Python 3.12.8", "claude CLI 1.0.19", "..."],
      "suggestion": null,
      "collected_logs": {}
    }
  ]
}
```

---

## Submitting a report

Submit a redacted diagnostic report directly as a GitHub issue:

```{ .bash .notest }
summon doctor --submit
```

This requires the `gh` CLI to be installed and authenticated. The command:

1. Runs all diagnostic checks
2. Builds a Markdown report with full details
3. Shows you the redacted report for review
4. Asks for confirmation before submitting
5. Creates an issue on the summon-claude repository

`--submit` prints an error and skips submission in non-interactive mode (`--no-interactive`) to prevent accidental submissions.

---

## Redaction

All doctor output — terminal, exports, and submissions — is automatically redacted. The redactor strips:

- **API tokens** — Slack (`xox*`, `xapp-*`), Anthropic (`sk-ant-*`), GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`)
- **File paths** — home directory replaced with `~`, data/config directories replaced with `[data_dir]`/`[config_dir]`
- **Slack IDs** — user, channel, team, and bot IDs replaced with `U***`, `C***`, `T***`, `B***`
- **Session UUIDs** — truncated to the first 8 characters

This removes credentials, well-known ID formats, and filesystem paths from output. Review exported or submitted reports before sharing — log content from third-party libraries may contain information not covered by these patterns.

---

## Combining with other tools

`summon doctor` complements other diagnostic commands:

| Command | Purpose |
|---------|---------|
| `summon doctor` | Full system health check across all subsystems |
| `summon config check` | Configuration validation and feature inventory |
| `summon auth status` | Authentication status for all providers |
| `summon db status` | Database schema version and integrity |
| `summon session list` | Active session status |
| `summon session logs` | Raw session log viewer |

When troubleshooting, start with `summon doctor` for a broad overview, then drill into specific areas with the targeted commands.

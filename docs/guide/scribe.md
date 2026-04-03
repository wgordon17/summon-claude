# Scribe Agent

??? info "Prerequisites"
    This guide assumes you've completed the [Quick Start](../getting-started/quickstart.md) and have a working `summon config check`.

The scribe is a background monitoring agent that keeps an eye on your inboxes so you don't have to. It periodically checks Gmail, Google Calendar, Google Drive, and optionally external Slack channels, then posts alerts, daily summaries, and important signals to its persistent `#0-scribe` channel.

---

## What the scribe does

The scribe runs as a persistent Claude session that wakes up on a configurable interval (default: every 5 minutes), scans connected data sources, and posts notable items to Slack. It triages items by importance on a 1--5 scale, respects quiet hours, tracks notes and action items, and produces daily summary reports.

The scribe is not interactive in the same way as a regular session — it is meant to run unattended in the background and surface information proactively.

Key behaviors:

- **Alert triage** — each item is scored 1--5 and formatted by importance level (see [Alert formatting](#alert-formatting) below)
- **Quiet hours** — only critical (level 5) alerts are posted during the configured quiet window
- **Note-taking** — messages posted to the scribe channel are tracked as notes or action items
- **Daily summaries** — generated automatically when activity is quiet, when quiet hours begin, or on request
- **State checkpoints** — the scribe posts periodic checkpoints to its channel so it can resume after a restart without re-alerting

---

## Setup

### Step 1: Configure data sources

The scribe auto-enables when it detects any configured data source. Set up at least one.

#### Google Workspace

First, run the guided setup to create Google OAuth credentials:

```{ .bash .notest }
summon auth google setup
```

This walks you through creating a GCP project, enabling the required APIs, configuring the OAuth consent screen, and downloading your credentials.

Then authenticate with Google:

```{ .bash .notest }
summon auth google login
```

This prompts which services need write access (all are read-only by default), then opens a browser for OAuth consent. Once complete, credentials are stored in summon's config directory and the scribe automatically detects them — no manual config flag needed.

To verify authentication status:

```{ .bash .notest }
summon auth google status
```

#### External Slack monitoring

See [Scribe Integrations — Slack Browser Monitoring](scribe-integrations.md#slack-browser-monitoring) for full setup instructions.

### Step 2: Start the scribe

The scribe auto-spawns when you run:

```bash
summon project up
```

It creates (or reuses) a persistent private channel called `#0-scribe`. No manual start or `/summon CODE` authentication is needed — the scribe inherits the authenticated user from `project up`.

If the scribe was previously suspended by `project down`, `project up` resumes it with transcript continuity.

---

## Configuration

All scribe configuration uses `SUMMON_SCRIBE_*` environment variables. These can be set in the summon config file or as shell environment variables.

### Core settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SUMMON_SCRIBE_ENABLED` | auto-detect | Enable the scribe agent (auto-enables when Google or Slack is detected) |
| `SUMMON_SCRIBE_MODEL` | (inherits `SUMMON_DEFAULT_MODEL`) | Model to use for the scribe session |
| `SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES` | `5` | How often the scribe polls for new data |
| `SUMMON_SCRIBE_CWD` | `<data-dir>/scribe` | Working directory for the scribe session |

### Filtering and quiet hours

| Variable | Default | Description |
|----------|---------|-------------|
| `SUMMON_SCRIBE_IMPORTANCE_KEYWORDS` | (unset) | Comma-separated keywords that elevate item importance. Items containing these words are always flagged as importance level 4+. |
| `SUMMON_SCRIBE_QUIET_HOURS` | (unset) | Quiet hours in `HH:MM-HH:MM` format (e.g., `22:00-08:00`). Only level-5 (urgent) items are posted during this window. |

**Example with keyword filtering and quiet hours:**

```bash
summon config set SUMMON_SCRIBE_IMPORTANCE_KEYWORDS urgent,outage,deploy,PagerDuty
summon config set SUMMON_SCRIBE_QUIET_HOURS 23:00-07:00
```

### Google Workspace

| Variable | Default | Description |
|----------|---------|-------------|
| `SUMMON_SCRIBE_GOOGLE_ENABLED` | auto-detect | Enable the Google Workspace data collector (auto-detected when credentials exist) |

Available services are auto-detected from the OAuth scopes granted during `summon auth google login`.

!!! note "Requires workspace-mcp"
    The Google collector requires the `google` extra: `uv tool install "summon-claude[google]"`. Google OAuth credentials must also be configured via `summon auth google setup` and `summon auth google login`.

### Slack channel monitoring

The scribe can monitor an external Slack workspace using browser-based WebSocket interception. This is separate from the native Slack bot integration used for session interaction — it watches a different workspace (e.g., your company's Slack) via a real browser session.

| Variable | Default | Description |
|----------|---------|-------------|
| `SUMMON_SCRIBE_SLACK_ENABLED` | auto-detect | Enable Slack channel monitoring (auto-detected when Playwright and browser auth exist) |
| `SUMMON_SCRIBE_SLACK_BROWSER` | `chrome` | Browser to use: `chrome`, `firefox`, or `webkit` |
| `SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS` | (unset) | Comma-separated channel IDs to monitor |

DMs and @mentions are always captured regardless of `SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS`. The channel list controls which channels have *all* messages monitored.

**Example:**

```bash
# Slack auto-enables when browser auth exists; these are optional overrides
summon config set SUMMON_SCRIBE_SLACK_BROWSER chrome
summon config set SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS C01ABC123,C02DEF456
```

---

## Slack browser monitoring

For full setup instructions (install, authentication, channel selection), see [Scribe Integrations — Slack Browser Monitoring](scribe-integrations.md#slack-browser-monitoring).

---

## Alert formatting

The scribe triages each item on a 1--5 importance scale and formats alerts accordingly:

| Level | Label | Format | Notification |
|-------|-------|--------|--------------|
| 5 | Urgent | `:rotating_light:` **URGENT** with detail block | @mentions the user |
| 4 | Important | `:warning:` **Source**: summary with detail block | No @mention |
| 3 | Normal | Source: summary (one line) | None |
| 1--2 | Low/Noise | Batched into a single "_Low priority (N items)_" line | None |

Items matching configured importance keywords are always elevated to level 4+.

During quiet hours, only level-5 items are posted.

---

## Daily summaries

The scribe produces daily summary reports covering all monitored sources. A summary includes:

- **Email** — count received, important items highlighted
- **Calendar** — events, notable meetings or changes
- **Drive** — documents modified or shared
- **Slack** — message counts, DMs, mentions, key conversations
- **Notes & Action Items** — user-posted notes tracked during the day
- **Agent Work** — summary of what project sessions accomplished (read from the Global PM channel)
- **Alerts** — total items flagged as important

Summaries are generated when:

- Activity has been quiet for 3+ consecutive scans
- The user explicitly asks for a summary
- Quiet hours begin (if configured)

---

## Prompt injection defense

The scribe processes content from external sources (emails, Slack messages, calendar events, documents) that may contain text designed to manipulate the agent. The scribe's system prompt includes explicit defenses against prompt injection attacks — it treats all external content as untrusted data, never as instructions. If a suspected injection attempt is detected, the scribe posts a warning to its channel rather than acting on the content.

---

## Scribe canvas

The scribe's Slack channel has a canvas with a summary layout:

- **Recent Signals** — items surfaced in the last scan
- **Active Items** — ongoing calendar events or long-running threads
- **Suppressed** — items seen but filtered below the importance threshold

The canvas is updated after each scan cycle. See [Canvas Integration](canvas.md) for how the canvas sync works.

---

## Full configuration example

```bash
# ~/.config/summon/config.env (or environment variables)

# Scribe auto-enables when Google or Slack is configured.
# To force on/off: SUMMON_SCRIBE_ENABLED=true/false

# Use a lighter model to reduce cost
SUMMON_SCRIBE_MODEL=claude-haiku-4-5-20251001

# Scan every 10 minutes
SUMMON_SCRIBE_SCAN_INTERVAL_MINUTES=10

# Elevate items with these keywords regardless of importance scoring
SUMMON_SCRIBE_IMPORTANCE_KEYWORDS=urgent,sev1,sev2,outage,on-call

# Don't post non-critical items overnight
SUMMON_SCRIBE_QUIET_HOURS=22:00-08:00

# Google Workspace collector (auto-detected when credentials exist)
# SUMMON_SCRIBE_GOOGLE_ENABLED=true  # optional — auto-detected

# External Slack collector (auto-detected when browser auth exists)
# SUMMON_SCRIBE_SLACK_ENABLED=true  # optional — auto-detected
SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS=C01ABC123,C02DEF456
```

---

## See also

- [Projects](projects.md) — the project system the scribe runs within
- [Configuration](configuration.md) — full configuration reference

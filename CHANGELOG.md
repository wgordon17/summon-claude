# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Event health probe** — Active detection of Slack Events API failures at startup and runtime. `EventProbe` in `bolt.py` uses reaction-based round-trip verification in a private `summon-health-probe` channel. Startup probe hard-fails on definitive signals (`token_revoked`, `socket_disabled`), soft-fails on non-definitive. Runtime probe runs within `_HealthMonitor` with 3-consecutive-failure threshold. Diagnostic cascade provides specific remediation URLs. Sessions are suspended on health failure (resumable via `summon project up`). `summon config check` includes event health status when daemon is running.
- **Unique session names** — `summon start --name` now auto-generates names with a 6-hex-char suffix (e.g. `myproject-a1b2c3`) to prevent collisions. Active session names are unique at the DB level via a partial unique index (#40)
- **Effort configuration** — `summon start --effort LEVEL` and `SUMMON_DEFAULT_EFFORT` config variable. In-session `!effort` command to switch effort dynamically via SDK `/effort` (#40)
- **Channel canvases** — Each session creates a persistent Slack canvas in its channel. `CanvasStore` provides SQLite-backed local markdown state with background sync to Slack (`slack/canvas_store.py`, `slack/canvas_templates.py`) (#42)
- **summon CLI MCP server** — `summon_cli_mcp.py` exposes session lifecycle tools (`session_list`, `session_info`, `session_start`, `session_stop`) as an MCP server, enabling Claude agents to manage summon sessions programmatically (#43)
- **Thinking block display** — `SUMMON_ENABLE_THINKING` (default `true`) enables adaptive thinking in Claude responses. `SUMMON_SHOW_THINKING` (default `false`) routes thinking content to Slack turn threads (#44)
- **Scribe agent configuration** — `SUMMON_SCRIBE_ENABLED`, `SUMMON_SCRIBE_MODEL`, `SUMMON_SCRIBE_GOOGLE_ENABLED` (default `false`), `SUMMON_SCRIBE_GOOGLE_SERVICES`, and related config vars for the scribe monitoring agent. `summon config google-auth` / `summon config google-status` for Google Workspace OAuth (#41)
- **Google Workspace MCP integration** — workspace-mcp subprocess wiring for Gmail, Calendar, and Drive access in Claude sessions when scribe is configured (#41)
- **Scribe agent session profile** — `--scribe-profile` internal flag, persistent `0-summon-scribe` channel with reuse-or-create pattern, scribe-specific canvas template, scan timer via `SessionScheduler`, and hardened prompt injection defense with attack pattern examples and canary phrase
- **Scribe auto-spawn** — `summon project up` spawns scribe after PM sessions when `scribe_enabled=true`. Includes preflight dependency checks, idempotent guard, and scribe stop on `project down`
- **Scribe alert formatting** — Structured delivery templates (level 1-5) with emoji-prefixed urgent alerts, daily summary format with email/calendar/drive/slack/notes sections, quiet hours enforcement suppressing non-urgent alerts
- **External Slack monitoring** — `SlackBrowserMonitor` captures DMs, @mentions (`<@USER>`), broadcast mentions (`<!here>`, `<!channel>`, `<!everyone>`), and monitored channel messages via Playwright WebSocket interception. Enterprise Grid support resolves `app.slack.com/client/{TEAM_ID}` from saved state. `external_slack_check` MCP tool with SEC-001 spotlighting delimiters, 50-message drain cap, and 2000-char truncation
- **External Slack CLI** — `summon config slack-auth`, `summon config slack-status`, `summon config slack-remove`, `summon config slack-channels` for browser-based Slack workspace authentication with 0o600 auth state files. `slack-auth` accepts bare workspace names (`myteam`, `acme.enterprise`) in addition to full URLs. Auto-detects user ID and team ID from localStorage. Credential detection checks `d` cookie expiry before prompting for re-auth. Extracts sidebar channels (grouped by section, muted excluded) via Slack's internal API with DOM fallback. Interactive `pick`-based multi-select for channel monitoring with empty-selection guard. `slack-channels` command for day-2 channel changes using cached channel list (`--refresh` to re-fetch). Auto-focuses email input on macOS Apple Silicon via `bring_to_front()` workaround (playwright#31252)
- **Workflow instructions storage** — `SessionRegistry` stores and retrieves per-channel workflow instructions for recurring sessions (#39)
- **Spawn tokens** — `generate_spawn_token()` / `verify_spawn_token()` infrastructure for pre-authenticated session creation. `SessionManager.create_session_with_spawn_token()` daemon IPC method with cwd enforcement and audit logging (#32)
- **Plugin skill discovery** — `discover_plugin_skills()` in `config.py` enumerates installed Claude Code plugin skills. `register_plugin_skills()` in `commands.py` adds them as passthroughs with unambiguous short aliases (#34)
- **Channel reading MCP tools** — `slack/mcp.py` gained tools for reading channel history and message context (#33)
- **Interactive session picker** — `summon session list` and related commands use `pick` for terminal-based interactive selection with `--no-interactive` fallback (#27)
- **mrkdwn conversion** — `slack/formatting.py` converts Claude's markdown replies to Slack mrkdwn format before posting (#28)
- **Declarative command dispatch** — `sessions/commands.py` uses a `COMMAND_ACTIONS` dict for declarative `!`-command definitions with mid-message detection (#26)
- **PM status messages** — `session_status_update` MCP tool enables PM agents to update a pinned status message in their channel with current session state. Includes mention sanitization, secret redaction, and audit logging
- **Dynamic channel scoping** — PM sessions use registry-driven channel resolvers: project PMs see own channel + child session channels; global PMs see all user channels. Replaces inline Python filtering with SQL-level `authenticated_user_id` scoping
- **PM heartbeat topic reconciliation** — PM sessions update channel topic every 30s via `count_active_children` DB query, providing a safety net for crashed children alongside the event-driven topic updates

- **Config UX overhaul** — `summon init` groups options into core (Slack, model, scribe, GitHub) and advanced (display, behavior, thinking) with a gating prompt. Shows contextual help hints for Slack tokens and GitHub PAT. Auto-runs `config check` on completion
- **Config check features section** — `summon config check` now shows a feature inventory (projects, workflow, hooks, hook bridge) with actionable commands, validates GitHub PAT via API, and nudges `summon config google-auth` when scribe is enabled

### Changed

- **Unified `$INCLUDE_GLOBAL` token** — Replaced `$GLOBAL_WORKFLOW` with `$INCLUDE_GLOBAL` for consistency with lifecycle hooks. Both hooks and workflow instructions now use the same token
- **Channel prefix validation** — `channel_prefix` now validated against Slack naming rules (lowercase alphanumeric, hyphens, underscores, non-empty) at both `config set` and startup time. Previously-accepted invalid prefixes (uppercase, spaces) are now rejected
- **Signing secret validation** — `slack_signing_secret` now validated as hex at `config set` and startup time, not just during `config check`
- **Unified Slack UX** — Pre-send architecture (`_PendingTurn` dataclass, two-task split for preprocessor/consumer). Emoji lifecycle on user messages: `:inbox_tray:` → `:gear:` → `:white_check_mark:`/`:octagonal_sign:`/`:warning:`. Turn threads with user snippet headers and tool call summaries. Eager intermediate text routing to turn threads with main-channel conclusion (#44)
- **Context tracking via JSONL transcript** — `sessions/context.py` parses the Claude CLI JSONL transcript for accurate per-step token counts, avoiding the over-reporting from cumulative SDK usage (#44)
- **Registry schema migrations** — Schema changes extracted into `sessions/migrations.py` as the single source of truth. Fresh databases create the v1 baseline and run all migrations. Migrations v1→v2 (parent session, authenticated user), v2→v3 (workflow defaults table), v3→v4 (active name index), v4→v5 (canvas columns), v5→v6 (parent session index) (#39, #42, #45)
- **CLI module extraction** — Business logic moved from `cli/__init__.py` into focused modules: `cli/start.py`, `cli/stop.py`, `cli/session.py`, `cli/db.py`, `cli/formatting.py`, `cli/helpers.py`, `cli/interactive.py` (#30)
- **Schema versioning and DB CLI** — `summon db` subcommands: `status`, `reset --yes`, `vacuum`, `purge --older-than N --yes`. Migrations apply automatically on connect (#29)
- **`update_status` field validation** — `_UPDATABLE_FIELDS` frozenset guards which columns `update_status()` can modify; `_VALID_STATUSES` frozenset guards valid status values (#31)

### Fixed

- **Session log viewer UX** — Improved log viewer formatting and daemon log hygiene (#36)
- **Registry race window** — Eliminated the race window between v1 schema stamp and migration in fresh databases (#45)

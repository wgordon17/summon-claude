# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-04-02

### Added

#### Project Management

- **Project management** — `summon project add/up/down/list/remove` CLI for registering projects with name, working directory, and Slack channel prefix. `SessionRegistry` gains project CRUD, session-to-project linking, and `count_active_children` queries. PM agent profile with `--pm-profile` flag, PM-specific system prompt, and PM-only MCP tools (#51)
- **PM session control** — PM agents can inject messages into child sessions via `session_message` MCP tool. Channel reuse for recurring sessions — `project up` reconnects to existing channels instead of creating new ones (#55)
- **Workflow injection into PM system prompt** — PM sessions receive project workflow instructions in their system prompt. PM welcome message posted to channel on session start (#53)
- **PR review orchestration** — PM agents can spawn review sessions with `system_prompt_append` for targeted code review instructions (#61)
- **Global PM** — Cross-project PM agent that manages all registered projects. Auto-created by `summon project up`. `slack_post_to_channel` MCP tool for cross-channel posting with `[Global PM]` attribution (#77)
- **Channel archiving and resume** — Sessions rename channels to `zzz-` prefix on stop for visual archiving. `summon project up` resumes suspended sessions deterministically (#63)
- **PM status messages** — `session_status_update` MCP tool enables PM agents to update a pinned status message in their channel with current session state. Includes mention sanitization, secret redaction, and audit logging
- **Dynamic channel scoping** — PM sessions use registry-driven channel resolvers: project PMs see own channel + child session channels; global PMs see all user channels. Replaces inline Python filtering with SQL-level `authenticated_user_id` scoping
- **PM heartbeat topic reconciliation** — PM sessions update channel topic every 30s via `count_active_children` DB query, providing a safety net for crashed children alongside the event-driven topic updates

#### Session Lifecycle

- **Context compaction** — Automatic context management with custom summarization and Claude CLI client restart. Tracks context percentage via JSONL transcript parsing, triggers compaction at configurable threshold, injects recovery context on restart (#49)
- **Spawn sessions from Slack** — `!summon start` command within a running session spawns child sessions with spawn token authentication and CWD enforcement (#50)
- **Initial prompt and session queue** — `initial_prompt` parameter on `session_start` MCP tool allows PM agents to provide startup instructions. FIFO session queue ensures ordered startup when multiple sessions are requested concurrently (#96)
- **Unique session names** — `summon start --name` now auto-generates names with a 6-hex-char suffix (e.g. `myproject-a1b2c3`) to prevent collisions. Active session names are unique at the DB level via a partial unique index (#40)
- **Effort configuration** — `summon start --effort LEVEL` and `SUMMON_DEFAULT_EFFORT` config variable. In-session `!effort` command to switch effort dynamically via SDK `/effort` (#40)
- **Spawn tokens** — `generate_spawn_token()` / `verify_spawn_token()` infrastructure for pre-authenticated session creation. `SessionManager.create_session_with_spawn_token()` daemon IPC method with cwd enforcement and audit logging (#32)

#### Permissions & Security

- **Read-only default with worktree write gate** — Sessions default to read-only permissions. Write tools require explicit approval via Slack HITL. After gate approval, writes within the worktree containment root are auto-approved (#86)
- **Auto permission mode** — Sonnet-powered classifier for automatic tool approval decisions. Evaluates tool calls against session context and project conventions to reduce HITL friction for safe operations (#78)
- **Containment model for non-git directories** — Write gate generalized beyond git repos. Non-git sessions set containment root to CWD at startup. Inline rollback warning for non-git sessions since changes can't be easily reverted (#93)
- **Worktree blocking** — `git worktree add` and `git worktree move` blocked via `disallowed_tools` to prevent sessions from escaping their containment root (#68)
- **Scribe injection defense** — Multi-layer prompt injection defense for scribe agent: spotlighting delimiters, attack pattern examples, canary phrase verification, and input truncation (#74)

#### Slack Integration

- **Channel canvases** — Each session creates a persistent Slack canvas in its channel. `CanvasStore` provides SQLite-backed local markdown state with background sync to Slack (`slack/canvas_store.py`, `slack/canvas_templates.py`) (#42)
- **Canvas MCP tools** — `summon_canvas_read`, `summon_canvas_write`, `summon_canvas_update_section` tools for all sessions with a canvas. Cross-channel reads with user scope guard (#46)
- **Unified Slack UX** — Pre-send architecture (`_PendingTurn` dataclass, two-task split for preprocessor/consumer). Emoji lifecycle on user messages: `:inbox_tray:` → `:gear:` → `:white_check_mark:`/`:octagonal_sign:`/`:warning:`. Turn threads with user snippet headers and tool call summaries. Eager intermediate text routing to turn threads with main-channel conclusion (#44)
- **Slack change visibility** — Diff snippets and file change summaries posted to turn threads so reviewers can see what Claude modified without leaving Slack (#54)
- **Event health probe** — Active detection of Slack Events API failures at startup and runtime. `EventProbe` in `bolt.py` uses reaction-based round-trip verification in a private `summon-health-probe` channel. Startup probe hard-fails on definitive signals (`token_revoked`, `socket_disabled`), soft-fails on non-definitive. Runtime probe runs within `_HealthMonitor` with 3-consecutive-failure threshold. Diagnostic cascade provides specific remediation URLs. Sessions are suspended on health failure (resumable via `summon project up`). `summon config check` includes event health status when daemon is running (#76)
- **Thinking block display** — `SUMMON_ENABLE_THINKING` (default `true`) enables adaptive thinking in Claude responses. `SUMMON_SHOW_THINKING` (default `false`) routes thinking content to Slack turn threads (#44)
- **Channel reading MCP tools** — `slack/mcp.py` gained tools for reading channel history and message context (#33)
- **Interactive session picker** — `summon session list` and related commands use `pick` for terminal-based interactive selection with `--no-interactive` fallback (#27)
- **mrkdwn conversion** — `slack/formatting.py` converts Claude's markdown replies to Slack mrkdwn format before posting (#28)
- **Declarative command dispatch** — `sessions/commands.py` uses a `COMMAND_ACTIONS` dict for declarative `!`-command definitions with mid-message detection (#26)

#### Scribe & External Integrations

- **Scribe agent configuration** — `SUMMON_SCRIBE_ENABLED`, `SUMMON_SCRIBE_MODEL`, `SUMMON_SCRIBE_GOOGLE_ENABLED` (default `false`), `SUMMON_SCRIBE_GOOGLE_SERVICES`, and related config vars for the scribe monitoring agent. `summon auth google login` / `summon auth google status` for Google Workspace OAuth (#41)
- **Google Workspace MCP integration** — workspace-mcp subprocess wiring for Gmail, Calendar, and Drive access in Claude sessions when scribe is configured (#41)
- **Scribe agent session profile** — `--scribe-profile` internal flag, persistent `0-scribe` channel with reuse-or-create pattern, scribe-specific canvas template, scan timer via `SessionScheduler`, and hardened prompt injection defense with attack pattern examples and canary phrase (#67)
- **Scribe auto-spawn** — `summon project up` spawns scribe after PM sessions when `scribe_enabled=true`. Includes preflight dependency checks, idempotent guard, and scribe stop on `project down` (#67)
- **Scribe alert formatting** — Structured delivery templates (level 1-5) with emoji-prefixed urgent alerts, daily summary format with email/calendar/drive/slack/notes sections, quiet hours enforcement suppressing non-urgent alerts (#67)
- **External Slack monitoring** — `SlackBrowserMonitor` captures DMs, @mentions (`<@USER>`), broadcast mentions (`<!here>`, `<!channel>`, `<!everyone>`), and monitored channel messages via Playwright WebSocket interception. Enterprise Grid support resolves `app.slack.com/client/{TEAM_ID}` from saved state. `external_slack_check` MCP tool with SEC-001 spotlighting delimiters, 50-message drain cap, and 2000-char truncation (#67)
- **External Slack CLI** — `summon auth slack login`, `summon auth slack status`, `summon auth slack logout`, `summon auth slack channels` for browser-based Slack workspace authentication with 0o600 auth state files. `slack login` accepts bare workspace names (`myteam`, `acme.enterprise`) in addition to full URLs. Auto-detects user ID and team ID from localStorage. Credential detection checks `d` cookie expiry before prompting for re-auth. Extracts sidebar channels (grouped by section, muted excluded) via Slack's internal API with DOM fallback. Interactive `pick`-based multi-select for channel monitoring with empty-selection guard. `slack channels` command for day-2 channel changes using cached channel list (`--refresh` to re-fetch) (#67)
- **GitHub remote MCP integration** — GitHub tools available in all sessions when a GitHub OAuth token is stored. Remote HTTP transport to `api.githubcopilot.com/mcp/` — no local binary required. Read-only tools auto-approved; all writes require Slack HITL approval (#56)

#### Cron, Tasks & Hooks

- **Cron tools and task tracking** — `CronCreate`, `CronDelete`, `CronList` MCP tools for agent-managed scheduled jobs. `summon_task_create`, `summon_task_update`, `summon_task_list` for in-session task management (#57)
- **Lifecycle hooks** — DB-backed hook storage per project and workflow default, `HookRunner` for executing shell commands at session lifecycle events, and Claude Code hook bridge for integrating with Claude Code's hook system (#58)
- **Cron job persistence** — Agent-created cron jobs survive compaction restarts via `scheduled_jobs` DB table. `SessionScheduler.restore_from_db()` reloads jobs on restart (#90)

#### CLI & Configuration

- **`summon doctor`** — Diagnostic command that checks daemon health, Slack connectivity, auth status, and system dependencies with actionable remediation suggestions (#73)
- **`summon reset data`** — Deletes all runtime data (database, logs, daemon state) and starts fresh (#71)
- **`summon reset config`** — Deletes all configuration (Slack tokens, Google OAuth credentials) (#71)
- **Google OAuth guided setup** — `summon auth google setup` is an interactive wizard with a step progress roadmap, clear-screen transitions, and `pick`-based menu selection. Console deep-links route through Google's account chooser for multi-account users. When `gcloud` CLI is detected, detects the current project, creates new projects, and enables APIs inline; when absent, offers to open browser links automatically via `click.launch()`. Styled output with `click.secho()` for visual hierarchy (#88)
- **Config UX overhaul** — `summon init` groups options into core (Slack, model, scribe, GitHub) and advanced (display, behavior, thinking) with a gating prompt. Shows contextual help hints for Slack tokens and GitHub PAT. Auto-runs `config check` on completion (#64)
- **Config check features section** — `summon config check` now shows a feature inventory (projects, workflow, hooks, hook bridge) with actionable commands, validates GitHub PAT via API, and nudges `summon auth google login` when scribe is enabled (#64)
- **Local install mode** — `.summon/` directory support for project-local configuration as an alternative to `~/.config/summon/` (#72)

#### Infrastructure

- **summon CLI MCP server** — `summon_cli_mcp.py` exposes session lifecycle tools (`session_list`, `session_info`, `session_start`, `session_stop`) as an MCP server, enabling Claude agents to manage summon sessions programmatically (#43)
- **Workflow instructions storage** — `SessionRegistry` stores and retrieves per-channel workflow instructions for recurring sessions (#39)
- **Plugin skill discovery** — `discover_plugin_skills()` in `config.py` enumerates installed Claude Code plugin skills. `register_plugin_skills()` in `commands.py` adds them as passthroughs with unambiguous short aliases (#34)
- **Documentation site** — MkDocs Material documentation site at [summon-claude.github.io](https://summon-claude.github.io/summon-claude/) with getting started guides, concept explainers, CLI reference, and development docs (#66)

### Changed

- **Unified `$INCLUDE_GLOBAL` token** — Replaced `$GLOBAL_WORKFLOW` with `$INCLUDE_GLOBAL` for consistency with lifecycle hooks. Both hooks and workflow instructions now use the same token
- **Channel prefix validation** — `channel_prefix` now validated against Slack naming rules (lowercase alphanumeric, hyphens, underscores, non-empty) at both `config set` and startup time. Previously-accepted invalid prefixes (uppercase, spaces) are now rejected
- **Signing secret validation** — `slack_signing_secret` now validated as hex at `config set` and startup time, not just during `config check`
- **Context tracking via JSONL transcript** — `sessions/context.py` parses the Claude CLI JSONL transcript for accurate per-step token counts, avoiding the over-reporting from cumulative SDK usage (#44)
- **Registry schema migrations** — Schema changes extracted into `sessions/migrations.py` as the single source of truth. Fresh databases create the v1 baseline and run all migrations. Migrations v1→v2 through v10→v11 covering parent sessions, workflow defaults, name uniqueness, canvases, context tracking, projects, hooks, and scheduled jobs (#39, #42, #45, #51, #58, #90)
- **CLI module extraction** — Business logic moved from `cli/__init__.py` into focused modules: `cli/start.py`, `cli/stop.py`, `cli/session.py`, `cli/db.py`, `cli/formatting.py`, `cli/helpers.py`, `cli/interactive.py`, `cli/google_auth.py` (#30, #88)
- **Schema versioning and DB CLI** — `summon db` subcommands: `status`, `vacuum`, `purge --older-than N --yes`. Migrations apply automatically on connect (#29)
- **Google OAuth credentials location** — Now stored in config dir (`~/.config/summon/google-credentials/`) instead of data dir
- **`update_status` field validation** — `_UPDATABLE_FIELDS` frozenset guards which columns `update_status()` can modify; `_VALID_STATUSES` frozenset guards valid status values (#31)
- **Agent system prompt restructuring** — All agent system prompts (PM, scribe, global PM) audited and restructured for consistency, clarity, and reduced prompt injection surface (#92)

### Removed

- **`SUMMON_GITHUB_PAT` config variable** — Replaced by OAuth App device flow via `summon auth github login`. Tokens are stored locally (never in config file). No deprecation period — PAT support is removed entirely (#75)
- **Auth commands under `summon config`** — All authentication commands moved to `summon auth` group. Migration: `summon config github-auth` → `summon auth github login`, `summon config github-logout` → `summon auth github logout`, `summon config google-auth` → `summon auth google login`, `summon config google-status` → `summon auth google status`, `summon config slack-auth` → `summon auth slack login`, `summon config slack-status` → `summon auth slack status`, `summon config slack-remove` → `summon auth slack logout`, `summon config slack-channels` → `summon auth slack channels`. New: `summon auth status` shows unified status for all providers
- **`summon db reset`** — Subcommand removed; replaced by `summon reset data` (interactive-only — the `--yes` flag for non-interactive use is intentionally not carried forward) (#71)

### Fixed

- **In-flight turn abort** — Clean abort of in-flight SDK turns on `request_shutdown`, preventing orphaned responses after session stop (#82)
- **User identity verification** — Centralized user identity checks for Slack message permissions, preventing impersonation via crafted user IDs (#81)
- **Session log viewer UX** — Improved log viewer formatting and daemon log hygiene (#36)
- **Registry race window** — Eliminated the race window between v1 schema stamp and migration in fresh databases (#45)
- **M2 session lifecycle fixes** — Various session startup, shutdown, and error handling improvements (#52)

## [0.2.1] - 2026-03-12

### Changed

- **Schema versioning and DB CLI** — `summon db` subcommands: `status`, `vacuum`, `purge --older-than N --yes`. Migrations apply automatically on connect (#29)
- **`update_status` field validation** — `_UPDATABLE_FIELDS` frozenset guards which columns `update_status()` can modify; `_VALID_STATUSES` frozenset guards valid status values (#31)
- **CLI module extraction** — Business logic moved from `cli/__init__.py` into focused modules: `cli/start.py`, `cli/stop.py`, `cli/session.py`, `cli/db.py`, `cli/formatting.py`, `cli/helpers.py`, `cli/interactive.py` (#30)

## [0.2.0] - 2026-03-09

### Added

- Single-bolt daemon architecture (#23)
- Declarative command dispatch with mid-message detection (#26)
- Interactive session picker with `pick` and `--no-interactive` fallback (#27)
- mrkdwn conversion for Claude replies (#28)
- Slack integration tests (#18)

### Fixed

- Multiple UX and lifecycle bugs (#17)
- Duplicate messages and ephemeral cleanup (#24)

## [0.1.1] - 2026-02-27

### Added

- AskUserQuestion routed to Slack interactive UI (#13)
- Three-layer socket resilience defense (#14)
- `make release` target with semver validation (#15)
- Ephemeral permissions and turn cancellation (#16)

## [0.1.0] - 2026-02-25

### Added

- Initial implementation of summon-claude
- PyPI publishing with trusted publishers and CI
- Global CLI flags and `config check` command
- Private channel support and streamer fixes
- PyPI update checker and Homebrew tap
- Session metadata in Slack channel topic

[Unreleased]: https://github.com/summon-claude/summon-claude/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/summon-claude/summon-claude/compare/v0.2.1...v1.0.0
[0.2.1]: https://github.com/summon-claude/summon-claude/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/summon-claude/summon-claude/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/summon-claude/summon-claude/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/summon-claude/summon-claude/commits/v0.1.0

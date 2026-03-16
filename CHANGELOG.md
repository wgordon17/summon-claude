# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Unique session names** — `summon start --name` now auto-generates names with a 6-hex-char suffix (e.g. `myproject-a1b2c3`) to prevent collisions. Active session names are unique at the DB level via a partial unique index (#40)
- **Effort configuration** — `summon start --effort LEVEL` and `SUMMON_DEFAULT_EFFORT` config variable. In-session `!effort` command to switch effort dynamically via SDK `/effort` (#40)
- **Channel canvases** — Each session creates a persistent Slack canvas in its channel. `CanvasStore` provides SQLite-backed local markdown state with background sync to Slack (`slack/canvas_store.py`, `slack/canvas_templates.py`) (#42)
- **summon CLI MCP server** — `summon_cli_mcp.py` exposes session lifecycle tools (`session_list`, `session_info`, `session_start`, `session_stop`) as an MCP server, enabling Claude agents to manage summon sessions programmatically (#43)
- **Thinking block display** — `SUMMON_ENABLE_THINKING` (default `true`) enables adaptive thinking in Claude responses. `SUMMON_SHOW_THINKING` (default `false`) routes thinking content to Slack turn threads (#44)
- **Scribe agent configuration** — `SUMMON_SCRIBE_ENABLED`, `SUMMON_SCRIBE_MODEL`, `SUMMON_SCRIBE_GOOGLE_SERVICES`, and related config vars for the scribe monitoring agent. `summon config google-auth` / `summon config google-status` for Google Workspace OAuth (#41)
- **Google Workspace MCP integration** — workspace-mcp subprocess wiring for Gmail, Calendar, and Drive access in Claude sessions when scribe is configured (#41)
- **Workflow instructions storage** — `SessionRegistry` stores and retrieves per-channel workflow instructions for recurring sessions (#39)
- **Spawn tokens** — `generate_spawn_token()` / `verify_spawn_token()` infrastructure for pre-authenticated session creation. `SessionManager.create_session_with_spawn_token()` daemon IPC method with cwd enforcement and audit logging (#32)
- **Plugin skill discovery** — `discover_plugin_skills()` in `config.py` enumerates installed Claude Code plugin skills. `register_plugin_skills()` in `commands.py` adds them as passthroughs with unambiguous short aliases (#34)
- **Channel reading MCP tools** — `slack/mcp.py` gained tools for reading channel history and message context (#33)
- **Interactive session picker** — `summon session list` and related commands use `pick` for terminal-based interactive selection with `--no-interactive` fallback (#27)
- **mrkdwn conversion** — `slack/formatting.py` converts Claude's markdown replies to Slack mrkdwn format before posting (#28)
- **Declarative command dispatch** — `sessions/commands.py` uses a `COMMAND_ACTIONS` dict for declarative `!`-command definitions with mid-message detection (#26)

### Changed

- **Unified Slack UX** — Pre-send architecture (`_PendingTurn` dataclass, two-task split for preprocessor/consumer). Emoji lifecycle on user messages: `:inbox_tray:` → `:gear:` → `:white_check_mark:`/`:octagonal_sign:`/`:warning:`. Turn threads with user snippet headers and tool call summaries. Eager intermediate text routing to turn threads with main-channel conclusion (#44)
- **Context tracking via JSONL transcript** — `sessions/context.py` parses the Claude CLI JSONL transcript for accurate per-step token counts, avoiding the over-reporting from cumulative SDK usage (#44)
- **Registry schema migrations** — Schema changes extracted into `sessions/migrations.py` as the single source of truth. Fresh databases create the v1 baseline and run all migrations. Migrations v1→v2 (spawn tokens, parent session), v2→v3 (canvas columns), v3→v4 (active name index) (#39, #42, #45)
- **CLI module extraction** — Business logic moved from `cli/__init__.py` into focused modules: `cli/start.py`, `cli/stop.py`, `cli/session.py`, `cli/db.py`, `cli/formatting.py`, `cli/helpers.py`, `cli/interactive.py` (#30)
- **Schema versioning and DB CLI** — `summon db` subcommands: `status`, `reset --yes`, `vacuum`, `purge --older-than N --yes`. Migrations apply automatically on connect (#29)
- **`update_status` field validation** — `_UPDATABLE_FIELDS` frozenset guards which columns `update_status()` can modify; `_VALID_STATUSES` frozenset guards valid status values (#31)

### Fixed

- **Session log viewer UX** — Improved log viewer formatting and daemon log hygiene (#36)
- **Registry race window** — Eliminated the race window between v1 schema stamp and migration in fresh databases (#45)

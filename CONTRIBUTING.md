# Contributing to summon-claude

## Commit Message Guidelines

summon-claude follows [Conventional Commits](https://www.conventionalcommits.org/).

### Format

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### Types

| Type | Purpose |
|------|---------|
| feat | Adds new feature |
| fix | Fixes a bug |
| docs | Updates documentation |
| chore | Maintenance (dependencies, configs) |
| refactor | Refactors code (no behavior change) |
| test | Adds or modifies tests |
| perf | Improves performance |
| style | Formatting, whitespace (no code change) |

### Scopes

Project-specific scopes for summon-claude:

**Scope Rules:**
- One scope per commit—no commas (e.g., `fix(auth):` not `fix(auth,session):`)
- Only lowercase letters, numbers, hyphens, underscores
- If a change spans multiple components, use the primary one or omit scope

**Component Scopes:**
- **sessions**: Session lifecycle and management (`sessions/` package)
- **slack**: Slack integration (`slack/` package)
- **daemon**: Daemon process and IPC (`daemon.py`, `event_dispatcher.py`)
- **cli**: CLI entry point and subcommands (`cli/` package)
- **config**: Configuration and settings (`config.py`)
- **auth**: Authentication tokens and short codes (`sessions/auth.py`)
- **permissions**: Permission handling and approval flow (`sessions/permissions.py`)
- **mcp**: MCP server and tools (`slack/mcp.py`)
- **registry**: Session storage and SQLite operations (`sessions/registry.py`)
- **db**: Database maintenance CLI commands (`cli/` — `summon db` group: status, reset, vacuum, purge)
- **hooks**: Lifecycle hooks and Claude Code hook bridge (`sessions/hooks.py`, `cli/hooks.py`)
- **plugin**: Claude Code plugin skill and manifest (`.claude-plugin/`)

**Infrastructure Scopes:**
- **ci**: CI/CD pipelines and GitHub Actions
- **deps**: Dependency updates
- **build**: Build system, pyproject.toml, Makefile
- **repo**: Repository structure, gitignore, misc files

### Examples

**Good:**
```
feat(mcp): adds slack_create_thread tool

Enables Claude to start new threads for organizing long conversations.
```

```
fix(permissions): respects settings.json auto-approve rules

Permission handler was ignoring ToolPermissionContext.suggestions
from the Claude SDK, falling back to hardcoded allowlist only.
```

```
chore(deps): updates slack-bolt to v2.0
```
*No body needed—change is obvious*

**Bad:**
```
updated stuff
```
*No type/scope, vague description*

```
fix(permissions,session): updates handlers and tokens
```
*Multiple scopes—use one scope or omit if cross-cutting*

```
feat(cli): adds new commands

Added three new commands: init, build, and deploy. Each command
has its own handler and validation logic. Updated help text to
include all new commands and their options.
```
*Body describes "what" changed—diff shows this; explain "why" instead*

### Guidelines

**DO:**
- Use present indicative tense ("adds" not "add" or "added")
- Keep subject line ≤50 characters
- Explain WHY in body, not WHAT (diff shows what)
- Keep body to 2-3 lines maximum, each ≤72 characters
- Reference issues: "Closes #123"

**Body structure:**
1. **First line:** Why this change was needed (problem/motivation)
2. **Second line (optional):** Essential technical context if non-obvious
3. **That's it.**

No body needed if the change is obvious from the subject line.

**DON'T:**
- Use emojis or ALL CAPS
- List changed files (git shows this)
- Include statistics (lines changed)
- Add meta-commentary ("Generated with...", "Co-Authored-By...")
- Write verbose explanations or "benefits"
- Describe what changed (the diff shows that)

### Breaking Changes

Add ! after type/scope:

```
feat(sessions)!: changes SessionManager API

BREAKING CHANGE: create_session now requires EventDispatcher reference.
```

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management

### Getting Started

```bash
# Clone and install
git clone https://github.com/summon-claude/summon-claude.git
cd summon-claude
uv sync

# Verify setup
make all  # install → lint → test
```

### Common Commands

```bash
make install    # Install dependencies (uv sync)
make lint       # Run ruff check + format + pyright
make test       # Run pytest
make all        # Full workflow: install → lint → test
```

### Running Tests

```bash
uv run pytest tests/ -v              # All tests
uv run pytest tests/test_auth.py -v  # Single module
uv run pytest -k "test_name" -v      # By name pattern
uv run pytest -n0                    # Disable parallel (serial mode)
```

### Linting

```bash
uv run ruff check . --fix     # Auto-fix lint issues
uv run ruff format .          # Auto-format
uv run pyright                # Type checking
```

### Database Migrations

Schema migrations run automatically when `SessionRegistry` connects — users never need to run a manual step. The migration system lives in `sessions/migrations.py`.

**Adding a migration:**

1. Bump `CURRENT_SCHEMA_VERSION` in `sessions/migrations.py`
2. Write an async migration function that takes `db: aiosqlite.Connection`
3. Add it to `_MIGRATIONS` keyed by the version it migrates *from*

```python
CURRENT_SCHEMA_VERSION = 5  # was 4

async def _migrate_4_to_5(db: aiosqlite.Connection) -> None:
    await db.execute("ALTER TABLE sessions ADD COLUMN tags TEXT")

_MIGRATIONS: dict[int, Any] = {
    ...
    4: _migrate_4_to_5,
}
```

**Rules:**
- Migrations must be **idempotent** — if the process crashes mid-migration, the same migration reruns on next connect
- All pending migrations run within a single `BEGIN IMMEDIATE` / `COMMIT` transaction, with `ROLLBACK` on any error
- `None` means no-op (used for the 0→1 baseline where DDL already matches)
- **Migrations are the single source of truth** — fresh DBs create v1 baseline tables and run all migrations from v1 to current. Do NOT add schema changes to the DDL constants in `registry.py`
- `summon db status` shows the current schema version and whether migration was applied

## Git Workflow

Use feature branches for all work. Don't commit directly to main.

**One-time setup:**
```bash
git config --global push.autoSetupRemote true
```

**Feature workflow:**
```bash
# Create feature branch from latest upstream
git fetch upstream
git switch -c feature/your-feature upstream/main

# Work and commit
git commit -am "feat(scope): description"
git push  # Auto-sets up tracking

# Rebase before PR
git fetch upstream
git rebase upstream/main

# Create PR
gh pr create
```

After PR merges, delete the branch and sync main.

### Commit Workflow

**During Development:**
- Commit freely and often—don't worry about perfection
- WIP commits, debugging attempts, and iterations are all fine
- Focus on making progress, not perfect history

**Before PR/Merge:**
- Review your commits—look at the full diff and commit history
- Group related changes—combine commits that belong together logically
- Use interactive rebase to reorganize, squash, and reword

**Decision criteria for squashing:**
- Do these commits represent one logical change?
- Would a reviewer want to see these as separate steps?
- Does each commit add value independently?

If commits are just iterations toward a solution, squash them.
If commits represent distinct logical changes, keep them separate.

**Commit cleanup with git-branchless:**
```bash
# Review your commits
git log --oneline -10
git sl  # Visual commit graph

# Reword a commit message
git reword -m "fix(ci): configures node 20 and disables cache" jkl3456

# Squash commits together
git branchless move --fixup -x def5678 -d abc1234
git branchless move --fixup -x ghi9012 -d abc1234

# View the result
git sl
```

**You decide** what makes sense for your change—there's no formula.

**Setup:** `brew install git-branchless && git branchless init`

## Pull Request Guidelines

### PR Description

**Keep PR descriptions brief:**
- State what changed and why
- Use bullet points for multiple changes
- Reference related issues/PRs if applicable
- **NO verification sections, file lists, or test result summaries**

The diff shows what changed. CI shows test results. Don't repeat information that's already visible.

**Good example:**
```
## Summary

- Migrates CLI from argparse to click for proper output handling and shell completion
- Resolves print() usage by replacing with click.echo()
```

**Bad example:**
```
## Summary
Migrated CLI framework

## Changes Made
- Updated cli.py to use click decorators
- Modified cli_config.py for click groups
- Added click to dependencies

## Test Results
✅ 332 tests passing

## Files Changed
- src/summon_claude/cli.py
- src/summon_claude/cli_config.py
- pyproject.toml
```

## Project Architecture

```
src/summon_claude/
├── config.py              # SummonConfig (pydantic-settings) with validation
├── daemon.py              # Unix daemon with PID/lock, IPC framing
├── event_dispatcher.py    # Routes Slack events to sessions by channel
├── summon_cli_mcp.py      # MCP tools for session lifecycle management (session_list, _info, _start, _stop)
├── cli/
│   ├── __init__.py        # Click wiring, root group, setup_logging
│   ├── config.py          # Config subcommands (show, set, path, edit, check, google-auth)
│   ├── daemon_client.py   # Typed async client for daemon Unix socket API
│   ├── db.py              # DB subcommand implementations (status, reset, vacuum, purge)
│   ├── formatting.py      # Output formatting (echo, format_json, print_session_table)
│   ├── helpers.py         # Session resolution (resolve_session, pick_session)
│   ├── hooks.py           # Lifecycle hooks CLI (install/uninstall bridge, show/set/clear)
│   ├── interactive.py     # Interactive terminal selection with TTY-aware fallback
│   ├── session.py         # Session subcommand implementations (list, info, logs, cleanup)
│   ├── start.py           # async_start() implementation
│   ├── stop.py            # async_stop() implementation
│   └── update_check.py    # PyPI update checker with 24h cache
├── sessions/
│   ├── auth.py            # Session auth tokens and short codes
│   ├── commands.py        # !-prefixed command dispatch, aliasing, plugin skill registration
│   ├── context.py         # Context window usage tracking via JSONL transcript parsing
│   ├── hook_types.py      # Hook constants (VALID_HOOK_TYPES, INCLUDE_GLOBAL_TOKEN)
│   ├── hooks.py           # Lifecycle hooks runner (worktree_create, project_up, project_down)
│   ├── manager.py         # Session lifecycle, IPC control plane
│   ├── migrations.py      # Schema versioning and migration functions (single source of truth)
│   ├── permissions.py     # Tool permission handling + Slack buttons
│   ├── registry.py        # SQLite session storage (WAL mode)
│   ├── response.py        # Response streaming, turn threads, emoji lifecycle, turn summaries
│   └── session.py         # Session orchestrator (Claude SDK + Slack + pre-send architecture)
└── slack/
    ├── bolt.py            # Bolt app, rate limiter, health monitor
    ├── canvas_store.py    # SQLite-backed canvas markdown state with background Slack sync
    ├── canvas_templates.py # Canvas markdown templates for different agent profiles
    ├── client.py          # Channel-bound Slack output client (post, update, react, canvas)
    ├── formatting.py      # Markdown-to-Slack-mrkdwn conversion
    ├── mcp.py             # MCP tools for Claude to read and interact with Slack
    └── router.py          # Thread-aware message routing (main, turn threads, subagent threads)
```

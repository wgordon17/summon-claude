# Contributing

## Prerequisites

- **Python 3.12+** — required; summon-claude uses modern Python features
- **[uv](https://docs.astral.sh/uv/)** — dependency management and toolchain
- **git** — standard version control
- **[git-branchless](https://github.com/arxanas/git-branchless)** (optional) — commit history cleanup tools (`git sl`, `git reword`, `git branchless move`)

Install git-branchless:
```{ .bash .notest }
brew install git-branchless && git branchless init
```

---

## Getting Started

```{ .bash .notest }
# Clone and install
git clone git@github.com:summon-claude/summon-claude.git
cd summon-claude
uv sync

# Verify setup
make all  # install → lint → test
```

---

## Common Commands

```{ .bash .notest }
make install         # Install all dependencies (uv sync + git hooks)
make lint            # Run ruff check + format (auto-fix, fails if files changed)
make test            # Run full pytest suite
make all             # Complete workflow: install → lint → test
make build           # Build sdist and wheel

make py-lint         # Python lint only
make py-typecheck    # Run pyright type checking
make py-test         # Run full Python test suite
make py-test-quick   # Quick tests only (skips slow + slack markers, fail-fast)
make py-test-slack   # Slack integration tests (requires real credentials)

make docs-serve      # Serve docs locally with live reload
make docs-build      # Build docs in strict mode
```

Full reference:
```{ .bash .notest }
make help  # Lists all targets with descriptions
```

---

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
| `feat` | Adds new feature |
| `fix` | Fixes a bug |
| `docs` | Updates documentation |
| `chore` | Maintenance (dependencies, configs) |
| `refactor` | Refactors code (no behavior change) |
| `test` | Adds or modifies tests |
| `perf` | Improves performance |
| `style` | Formatting, whitespace (no code change) |

### Scopes

**Scope rules:**
- One scope per commit — no commas (e.g., `fix(auth):` not `fix(auth,session):`)
- Lowercase letters, numbers, hyphens, underscores only
- If a change spans multiple components, use the primary one or omit scope

**Component scopes:**

| Scope | Area |
|-------|------|
| `sessions` | Session lifecycle and management (`sessions/` package) |
| `slack` | Slack integration (`slack/` package) |
| `daemon` | Daemon process and IPC (`daemon.py`, `event_dispatcher.py`) |
| `cli` | CLI entry point and subcommands (`cli/` package) |
| `config` | Configuration and settings (`config.py`) |
| `auth` | Authentication tokens and short codes (`sessions/auth.py`) |
| `permissions` | Permission handling and approval flow (`sessions/permissions.py`) |
| `mcp` | MCP server and tools (`slack/mcp.py`) |
| `registry` | Session storage and SQLite operations (`sessions/registry.py`) |
| `db` | Database maintenance CLI (`summon db` group: status, vacuum, purge) |
| `reset` | Reset commands for data and config clearing (`cli/reset.py`) |
| `hooks` | Lifecycle hooks and Claude Code hook bridge (`sessions/hooks.py`, `cli/hooks.py`) |
| `project` | Project lifecycle and management (`cli/project.py`) |
| `scribe` | Scribe monitoring agent |
| `canvas` | Canvas storage and MCP tools (`canvas_mcp.py`, `slack/canvas_store.py`) |
| `diagnostics` | Diagnostic checks and doctor command (`diagnostics.py`, `cli/doctor.py`) |
| `plugin` | Claude Code plugin skill and manifest (`.claude-plugin/`) |

**Infrastructure scopes:**

| Scope | Area |
|-------|------|
| `ci` | CI/CD pipelines and GitHub Actions |
| `deps` | Dependency updates |
| `build` | Build system, pyproject.toml, Makefile |
| `repo` | Repository structure, gitignore, misc files |

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
*No body needed — change is obvious.*

**Bad:**
```
updated stuff
```
*No type/scope, vague description.*

```
fix(permissions,session): updates handlers and tokens
```
*Multiple scopes — use one scope or omit if cross-cutting.*

```
feat(cli): adds new commands

Added three new commands: init, build, and deploy. Each command
has its own handler and validation logic. Updated help text to
include all new commands and their options.
```
*Body describes "what" changed — the diff shows that. Explain "why" instead.*

### Writing commit messages

**DO:**
- Use present indicative tense ("adds" not "add" or "added")
- Keep subject line ≤50 characters
- Explain WHY in body, not WHAT (the diff shows what)
- Keep body to 2-3 lines maximum, each ≤72 characters
- Reference issues: `Closes #123`

**Body structure:**
1. **First line:** Why this change was needed (problem/motivation)
2. **Second line (optional):** Essential technical context if non-obvious
3. That's it.

No body needed if the change is obvious from the subject line.

**DON'T:**
- Use emojis or ALL CAPS
- List changed files (git shows this)
- Include statistics (lines changed)
- Add meta-commentary ("Generated with...", "Co-Authored-By...")
- Write verbose explanations or "benefits"
- Describe what changed (the diff shows that)

### Breaking Changes

Add `!` after type/scope:

```
feat(sessions)!: changes SessionManager API

BREAKING CHANGE: create_session now requires EventDispatcher reference.
```

---

## Git Workflow

Use feature branches for all work. Don't commit directly to main.

**One-time setup:**
```{ .bash .notest }
git config --global push.autoSetupRemote true
```

**Feature workflow:**
```{ .bash .notest }
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

---

## Commit Workflow

**During development:**
- Commit freely and often — don't worry about perfection
- WIP commits, debugging attempts, and iterations are all fine
- Focus on making progress, not perfect history

**Before PR/merge:**
- Review your commits — look at the full diff and commit history
- Group related changes — combine commits that belong together logically
- Use interactive rebase to reorganize, squash, and reword

**Decision criteria for squashing:**
- Do these commits represent one logical change?
- Would a reviewer want to see these as separate steps?
- Does each commit add value independently?

If commits are just iterations toward a solution, squash them. If commits represent distinct logical changes, keep them separate.

### Commit cleanup with git-branchless

```{ .bash .notest }
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

You decide what makes sense for your change — there's no formula.

---

## Pull Request Guidelines

**Keep PR descriptions brief:**
- State what changed and why
- Use bullet points for multiple changes
- Reference related issues/PRs if applicable
- **No verification sections, file lists, or test result summaries**

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

---

## Code Style

### Linting and formatting

```{ .bash .notest }
uv run ruff check . --fix     # Auto-fix lint issues
uv run ruff format .          # Auto-format
uv run pyright                # Type checking
```

Or via make:
```{ .bash .notest }
make lint       # ruff check + format
make py-typecheck  # pyright only
```

**Ruff configuration** (from `pyproject.toml`):
- Line length: 100 characters
- Target: Python 3.12
- Enabled rule sets: pycodestyle, pyflakes, isort, pep8-naming, flake8-bugbear, comprehensions, pyupgrade, bandit, datetimez, pathlib, pylint, and more
- All relative imports are banned — use absolute imports (`from summon_claude.sessions.auth import ...`)

**Pyright configuration:**
- Mode: `standard`
- Checks `src/` only; excludes `tests/`
- `reportMissingImports = false` (claude-agent-sdk lacks py.typed marker)
- `reportMissingTypeStubs = false` (slack-sdk, slack-bolt don't ship stubs)

### Import organization

isort is configured via ruff with `known-first-party = ["summon_claude", "helpers"]`. Imports are grouped: stdlib → third-party → first-party. All imports must be absolute.

---

## Database Migrations

Schema migrations run automatically when `SessionRegistry` connects — users never run a manual step. The migration system lives in `sessions/migrations.py`.

### Adding a migration

1. Bump `CURRENT_SCHEMA_VERSION` in `sessions/migrations.py`
2. Write an async migration function that takes `db: aiosqlite.Connection`
3. Add it to `_MIGRATIONS` keyed by the version it migrates *from*

```{ .python .notest }
CURRENT_SCHEMA_VERSION = 15  # was 14

async def _migrate_14_to_15(db: aiosqlite.Connection) -> None:
    await db.execute("ALTER TABLE sessions ADD COLUMN tags TEXT")

_MIGRATIONS: dict[int, Any] = {
    ...
    14: _migrate_14_to_15,
}
```

### Migration rules

- **Idempotent** — if the process crashes mid-migration, the same migration reruns on next connect; it must not fail or corrupt data
- All pending migrations run within a single `BEGIN IMMEDIATE` / `COMMIT` transaction, with `ROLLBACK` on any error
- `None` means no-op (used for the 0→1 baseline where DDL already matches)
- **Migrations are the single source of truth** — fresh DBs create v1 baseline tables and run all migrations from v1 to current; do not add schema changes to the DDL constants in `registry.py`
- SQLite lacks `IF NOT EXISTS` for `ALTER TABLE` — wrap `ALTER TABLE` statements in `try/except` to handle the "duplicate column name" error on re-run
- `PRAGMA foreign_keys = ON` is set in `_connect()` but cannot be changed inside a transaction — set it before any `BEGIN`; future migrations needing temporary FK violations must use a separate connection

### Checking schema version

```bash
summon db status   # Shows current schema version and migration state
```

---

## Diagnostic Checks

The `summon doctor` command uses a registry pattern in `diagnostics.py`. Each check is a class that implements the `DiagnosticCheck` protocol and is registered in `DIAGNOSTIC_REGISTRY`.

### Adding a check

1. Create a class implementing `DiagnosticCheck` in `diagnostics.py`:

    ```python
    class MySubsystemCheck:
        name = "my_subsystem"
        description = "Checks something important"

        async def run(self, config: SummonConfig | None) -> CheckResult:
            # Return early with "skip" if prerequisites are missing
            if config is None:
                return CheckResult(
                    status="skip",
                    subsystem="my_subsystem",
                    message="Config not available",
                )

            details: list[str] = []
            # ... perform checks, append to details ...

            return CheckResult(
                status="pass",
                subsystem="my_subsystem",
                message="Everything looks good",
                details=details,
            )
    ```

2. Register it and add the subsystem name:

    ```python
    DIAGNOSTIC_REGISTRY["my_subsystem"] = MySubsystemCheck()
    ```

    Add `"my_subsystem"` to the `KNOWN_SUBSYSTEMS` frozenset at the top of the file.

3. Add guard test mappings in `tests/test_diagnostics_guard.py`:

    ```bash
    uv run pytest tests/test_diagnostics_guard.py -v
    ```

    The guard tests verify that every registered check is in `KNOWN_SUBSYSTEMS` and vice versa. Add MCP server names, binary paths, or credential references to the appropriate mapping dicts if your check validates external integrations.

### CheckResult fields

| Field | Type | Purpose |
|-------|------|---------|
| `status` | `"pass"` / `"fail"` / `"warn"` / `"info"` / `"skip"` | Overall result |
| `subsystem` | `str` | Identifier (must match `KNOWN_SUBSYSTEMS` entry) |
| `message` | `str` | One-line summary shown in default output |
| `details` | `list[str]` | Itemized findings (shown with `-v`) |
| `suggestion` | `str \| None` | Actionable next step (shown with `-v`) |
| `collected_logs` | `dict[str, list[str]]` | Log tails keyed by filename (shown with `-v`, included in exports) |

### Guidelines

- **All checks run in parallel** — do not depend on results from other checks
- **Use `config: SummonConfig | None`** — config may be `None` if it failed to load; return `skip` in that case
- **Return `skip` when prerequisites are missing** — not `fail` (e.g., scribe not enabled, no GitHub token)
- **Keep checks fast** — use timeouts for network calls (10s for API calls, 5s for CLI version checks)
- **Never log secrets** — use the `redactor` singleton to sanitize any user-specific data in details or messages
- **Match SEC-003** — don't include workspace names, usernames, or Slack team names in results

---

## Project Architecture

```
src/summon_claude/
├── canvas_mcp.py          # Canvas MCP server (read, write, update_section tools)
├── config.py              # SummonConfig (pydantic-settings) with validation
├── daemon.py              # Unix daemon with PID/lock, IPC framing
├── diagnostics.py         # DiagnosticCheck protocol, CheckResult, Redactor, all check implementations
├── event_dispatcher.py    # Routes Slack events to sessions by channel
├── github_auth.py         # GitHub OAuth App device flow authentication
├── mcp_untrusted_proxy.py # MCP stdio proxy that marks tool results as untrusted
├── security.py            # Prompt injection defense utilities
├── slack_browser.py       # Playwright-based Slack WebSocket monitor for external workspaces
├── summon_cli_mcp.py      # MCP tools for session lifecycle (session_list, _info, _start, _stop)
├── cli/
│   ├── __init__.py        # Click wiring, root group, setup_logging
│   ├── auth.py            # Auth group: unified auth commands for GitHub, Google, Slack
│   ├── config.py          # Config subcommands (show, set, path, edit, check)
│   ├── daemon_client.py   # Typed async client for daemon Unix socket API
│   ├── db.py              # DB subcommand implementations (status, vacuum, purge)
│   ├── doctor.py          # Doctor command logic (check runner, output formatting, export, submit)
│   ├── formatting.py      # Output formatting (echo, format_json, print_session_table)
│   ├── helpers.py         # Session resolution (resolve_session, pick_session)
│   ├── hooks.py           # Lifecycle hooks CLI (install/uninstall bridge, show/set/clear)
│   ├── interactive.py     # Interactive terminal selection with TTY-aware fallback
│   ├── preflight.py       # Claude CLI preflight check (version, path)
│   ├── project.py         # Project lifecycle (add, remove, up, down, workflow)
│   ├── reset.py           # Reset commands (data, config)
│   ├── session.py         # Session subcommand implementations (list, info, logs, cleanup)
│   ├── slack_auth.py      # Slack browser auth CLI helpers
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
│   ├── migrations.py      # Schema versioning and migration functions
│   ├── permissions.py     # Tool permission handling + Slack buttons
│   ├── registry.py        # SQLite session storage (WAL mode)
│   ├── response.py        # Response streaming, turn threads, emoji lifecycle, turn summaries
│   ├── scheduler.py       # Session scheduling (cron tasks, timer injection)
│   ├── session.py         # Session orchestrator (Claude SDK + Slack + pre-send architecture)
│   └── types.py           # Shared session types and dataclasses
└── slack/
    ├── bolt.py            # Bolt app, rate limiter, health monitor
    ├── canvas_store.py    # SQLite-backed canvas markdown state with background Slack sync
    ├── canvas_templates.py # Canvas markdown templates for different agent profiles
    ├── client.py          # Channel-bound Slack output client (post, update, react, canvas)
    ├── formatting.py      # Markdown-to-Slack-mrkdwn conversion
    ├── markdown_split.py  # Markdown-aware message splitting for Slack's length limits
    ├── mcp.py             # MCP tools for Claude to read and interact with Slack
    └── router.py          # Thread-aware message routing (main, turn threads, subagent threads)
```

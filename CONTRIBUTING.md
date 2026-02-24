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
- **session**: Session orchestrator (`session.py`) — lifecycle, Claude SDK integration
- **cli**: CLI entry point and subcommands (`cli.py`, `cli_config.py`)
- **config**: Configuration and settings (`config.py`)
- **auth**: Authentication tokens and short codes (`auth.py`)
- **providers**: Chat provider abstraction and implementations (`providers/`)
- **slack**: Slack-specific provider implementation (`providers/slack.py`)
- **streamer**: Response streaming and message batching (`streamer.py`)
- **router**: Thread routing logic (`thread_router.py`)
- **permissions**: Permission handling and approval flow (`permissions.py`)
- **channels**: Slack channel management (`channel_manager.py`)
- **mcp**: MCP server and tools (`mcp_tools.py`)
- **registry**: Session storage and SQLite operations (`registry.py`)
- **display**: Content formatting and display logic (`content_display.py`)
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
feat(providers)!: changes ChatProvider protocol methods

BREAKING CHANGE: post_message now requires MessageRef return type.
```

## Development Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) for dependency management

### Getting Started

```bash
# Clone and install
git clone https://github.com/wgordon17/summon-claude.git
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
```

### Linting

```bash
uv run ruff check . --fix     # Auto-fix lint issues
uv run ruff format .          # Auto-format
uv run pyright                # Type checking
```

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
├── cli.py              # CLI entry point (argparse subcommands)
├── cli_config.py       # Config subcommands (show, set, path, edit)
├── config.py           # SummonConfig (pydantic-settings)
├── auth.py             # Session auth tokens and short codes
├── session.py          # Session orchestrator (Claude SDK + Slack)
├── registry.py         # SQLite session storage (WAL mode)
├── channel_manager.py  # Slack channel create/archive/header
├── permissions.py      # Tool permission handling + Slack buttons
├── streamer.py         # Response streaming with batched Slack sends
├── thread_router.py    # Routes content to channels/threads
├── content_display.py  # Inline vs file upload decision logic
├── mcp_tools.py        # In-process MCP server for Slack tools
├── rate_limiter.py     # Per-user slash command rate limiting
├── _formatting.py      # Slack mrkdwn text sanitization
└── providers/
    ├── base.py         # ChatProvider protocol
    └── slack.py        # SlackChatProvider implementation
```

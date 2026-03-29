# Lifecycle Hooks

??? info "Prerequisites"
    This guide assumes you've completed the [Quick Start](../getting-started/quickstart.md) and [set up a project](projects.md).

Lifecycle hooks let you run shell commands automatically at key points in the summon-claude lifecycle. Use them to set up environments, install dependencies, or run post-session cleanup.

---

## Hook types

| Hook | When it fires | Example use |
|------|--------------|-------------|
| `worktree_create` | After a session creates a git worktree | `make setup`, `uv sync` |
| `project_up` | After `summon project up` starts a project | Notify a channel, warm caches |
| `project_down` | After `summon project down` stops a project | Archive logs, post summary |

Each hook is a list of shell commands. Commands run sequentially; if one fails, the remaining commands are skipped.

---

## Setting hooks

### Interactive (editor)

```{ .bash .notest }
summon hooks set
```

Opens `$EDITOR` with the current hooks as JSON. Save and close to apply.

### Inline JSON

```{ .bash .notest }
summon hooks set '{"worktree_create": ["uv sync", "make setup"]}'
```

### Per-project hooks

```{ .bash .notest }
summon hooks set --project my-api '{"worktree_create": ["npm install"]}'
```

Per-project hooks override global hooks for that project. To include the global hooks as well, use `$INCLUDE_GLOBAL`:

```{ .bash .notest }
summon hooks set --project my-api '{"worktree_create": ["$INCLUDE_GLOBAL", "npm install"]}'
```

This runs the global `worktree_create` hooks first, then `npm install`.

---

## Viewing hooks

```bash
summon hooks show
```

Shows the currently configured global hooks. Use `--project` for project-specific hooks:

```bash
summon hooks show --project my-api
```

---

## Clearing hooks

```{ .bash .notest }
summon hooks clear
```

Removes global hooks (resets to NULL). For a specific project:

```{ .bash .notest }
summon hooks clear --project my-api
```

Clearing a project's hooks causes it to fall back to the global hooks.

---

## Hook bridge

The hook bridge connects summon's lifecycle hooks to Claude Code's hook system. When installed, Claude Code automatically notifies summon when worktrees are created.

### Install

```{ .bash .notest }
summon hooks install
```

This writes shell wrappers to `~/.claude/hooks/` and registers them in `~/.claude/settings.json`. The command is idempotent — safe to run multiple times.

### Uninstall

```{ .bash .notest }
summon hooks uninstall
```

Removes the shell wrappers and their entries from `settings.json`.

### What the bridge does

When Claude creates a worktree (via the built-in `EnterWorktree` tool), the hook bridge:

1. Checks if the worktree belongs to a summon project
2. Runs the project's `worktree_create` hooks (or global hooks if no project-specific hooks are set)
3. Reports the result back to the session

Without the bridge installed, worktree lifecycle hooks do not fire.

---

## Checking hook status

`summon config check` reports hook status in the Features section:

```text
Features:
  [INFO] Lifecycle hooks: not set (summon hooks set)
  [INFO] Hook bridge: not installed (summon hooks install)
```

Once configured:

```text
Features:
  [PASS] Lifecycle hooks: worktree_create (1 command)
  [PASS] Hook bridge: installed
```

---

## See also

- [Projects](projects.md) — project registration and management
- [Configuration](configuration.md) — config file location and management
- [CLI Reference](../reference/cli.md) — full `summon hooks` command reference

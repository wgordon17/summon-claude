# Projects

??? info "Prerequisites"
    This guide assumes you've completed the [Quick Start](../getting-started/quickstart.md) and have a working `summon config check`.

Projects group related sessions under a shared name, workflow instructions, and a channel-prefix convention. A project can have a dedicated PM agent that coordinates child sessions on your behalf.

---

## What projects are

A project is a named record in the summon-claude registry that links together:

- A **display name** and optional **description**
- A **working directory** — the default `cwd` for sessions created under this project
- **Workflow instructions** — a block of text injected into the system prompt of every session in the project
- A **channel prefix** — the Slack channel-name prefix used when creating session channels for the project

Projects make it easier to keep related work organized: the PM agent knows which sessions belong together, and workflow instructions let you encode team conventions once rather than re-typing them in every session.

---

## Commands

!!! tip "Alias"
    `summon p` is shorthand for `summon project` — all subcommands work with either prefix.

### `summon project add`

Register a new project:

```{ .bash .notest }
summon project add NAME [DIR]
```

`DIR` defaults to the current working directory if omitted.

```{ .bash .notest }
# Register the current directory as a project
summon project add my-api

# Register a different directory
summon project add frontend ~/work/acme-frontend
```

To associate a [Jira JQL filter](jira-integration.md#per-project-jql-filters) with the project:

```{ .bash .notest }
summon project add my-api --jql "project = MYAPI AND status != Done"
```

To enable the [bug hunter](bug-hunter.md) agent for automated security and correctness scanning:

```{ .bash .notest }
summon project add my-api --bug-hunter
```

After registering, set workflow instructions and other project settings using the `summon project workflow` subcommands documented [below](#managing-workflow-instructions).

### `summon project list`

Show all registered projects:

```bash
summon project list
```

The table includes each project's name, directory, PM status (running / stopped / errored / auth...), and ID prefix.

```bash
summon project list --output json
```

### `summon project up`

Start PM agents for all registered projects:

```{ .bash .notest }
summon project up
```

Each project gets one PM session. If a PM is already running for a project, `up` skips it. There is no per-project syntax — `project up` always starts PMs for all projects that need one.

!!! note "Authentication still required"
    Each PM session starts with an authentication code, just like `summon start`. The PM prints its code so you can bind it to a Slack channel with `/summon CODE`.

### `summon project down`

Stop all sessions associated with a project:

```{ .bash .notest }
summon project down my-api
```

`down` stops the PM and all child sessions it manages. All sessions (PM and children) are marked **suspended** rather than completed — this lets `project up` restart them deterministically later (cascade restart). Each session's Slack channel is renamed with a `zzz-` prefix on disconnect (e.g., `my-api-worker-a1b2c3` becomes `zzz-my-api-worker-a1b2c3`), making it easy to visually identify inactive sessions in the Slack sidebar.

```{ .bash .notest }
# Stop all projects (omit NAME to stop all)
summon project down
```

**Cascade restart:** When you run `summon project up` after a `down`, summon-claude finds all suspended sessions for the project and resumes them with full transcript continuity. Rather than creating fresh sessions, it creates new summon sessions that bind to the **existing** Slack channels — the `zzz-` prefix is removed, and the original channel name is restored. Canvas content, conversation history, and the Claude Code session transcript all carry over. The resumed sessions keep the same `cwd` and model they had before, so you can pause and resume an entire multi-session workflow without reconfiguring anything or losing context.

### `summon project remove`

Remove a project from the registry:

```{ .bash .notest }
summon project remove my-api
# or by project ID
summon project remove proj-a1b2c3
```

!!! warning
    Removing a project does not stop running sessions — run `summon project down` first if needed. Suspended sessions are cleaned up (marked completed) automatically as part of removal.

---

## Workflow instructions

Workflow instructions are a block of free-form text that gets injected into the **system prompt** of every session created under a project. Because they live in the system prompt rather than conversation history, they:

- **Persist across sessions** — every new session (PM, child, or ad-hoc) inherits the same instructions automatically.
- **Survive context compaction** — when a long session compacts its conversation history to free up context, system prompt content is preserved. Your conventions stay active for the entire session lifetime.
- **Apply uniformly** — the PM agent, child workers, and any session spawned under the project all receive the same instructions.

No default workflows ship with summon-claude — workflow instructions are entirely yours to define. They encode whatever your project needs: coding standards, tool preferences, repository conventions, review checklists, or operational guardrails.

After registering a project, set its workflow instructions:

```{ .bash .notest }
summon project add my-api ~/code/my-api
summon project workflow set my-api
```

This opens `$EDITOR` where you can write your instructions. For example:

```text
Always run 'uv run pytest' before committing.
Follow the existing module structure in src/.
Open PRs to the 'main' branch.
```

### Example workflows

The following examples show real-world patterns. Use them as starting points and tailor them to your project.

**Python project:**

```text
Use uv for all Python operations (uv run, uv add, uvx).
Run `uv run pytest` before committing — all tests must pass.
Follow the existing module structure in src/myapp/.
Use absolute imports (from myapp.utils import ..., not relative).
Type hints are required on all public functions.
Format with `uvx ruff format` and lint with `uvx ruff check --fix`.
```

**Code review:**

```text
Review all PRs for:
- Security issues (injection, auth bypass, secret leakage)
- Test coverage (new code paths must have tests)
- Adherence to the team style guide in docs/STYLE.md

Post findings as individual PR review comments on specific lines.
Use the "Request changes" status for security issues; "Comment" for style nits.
Always check that CI is green before approving.
```

**Monorepo:**

```text
This is a monorepo. Each service lives in services/<name>/.
Never modify shared libraries in lib/ without explicit approval.
Each service has its own Dockerfile and README.
Run tests only for the affected service: `cd services/<name> && make test`.
Cross-service API changes require updating the OpenAPI spec in api-specs/.
```

### Writing effective workflow instructions

Good workflow instructions are specific and actionable. A few guidelines:

- **Name exact commands.** "Run tests before committing" is vague. "Run `uv run pytest tests/`" is unambiguous.
- **Reference file paths.** Point to style guides, config files, API specs, or directory structures by their actual path in the repo.
- **State conventions explicitly.** If your team uses absolute imports, feature branches, or a particular commit message format, say so. Claude cannot infer unwritten conventions.
- **Include guardrails.** If certain files or directories should not be modified without approval, state that boundary. "Never modify `lib/` without explicit approval" is a clear constraint.
- **Keep it focused.** Workflow instructions are always present in the system prompt. Overly long instructions dilute the important points. Aim for the set of rules that matter on every task, not an exhaustive manual.

### Managing workflow instructions

Use `summon project workflow` to view, edit, and clear workflow instructions after a project is registered:

```{ .bash .notest }
# Show global workflow defaults
summon project workflow show

# Show workflow for a specific project
summon project workflow show my-api

# Edit global workflow (opens $EDITOR)
summon project workflow set

# Edit project-specific workflow (opens $EDITOR)
summon project workflow set my-api

# Clear project-specific workflow (falls back to global defaults)
summon project workflow clear my-api

# Clear global workflow defaults
summon project workflow clear
```

`summon project workflow set` opens your `$EDITOR` with the current instructions pre-filled. Comment lines (starting with `#`) are stripped on save. If you close the editor without changes, nothing is updated.

### Global vs. project-specific workflow

There are two levels of workflow instructions:

- **Global workflow defaults** — applied to all projects that don't have their own override.
- **Project-specific workflow** — set per project, fully replaces the global defaults by default.

To include the global defaults inside a project-specific workflow, use the `$INCLUDE_GLOBAL` token. Place it anywhere in the project's workflow text and it will be expanded to the full global defaults at runtime:

```
# Project-specific rules come first
Always use TypeScript strict mode.
Run tests with `npm test` before committing.

# Include the shared global defaults here
$INCLUDE_GLOBAL
```

Without `$INCLUDE_GLOBAL`, project-specific instructions fully replace the global defaults. Clearing a project's workflow (`summon project workflow clear my-api`) removes the override so the project falls back to global defaults.

---

## See also

- [Bug Hunter](bug-hunter.md) — automated security and correctness scanning
- [Jira Integration](jira-integration.md) — JQL filters and PM triage
- [Scribe](scribe.md) — background monitoring agent
- [Configuration](configuration.md) — project-level config options

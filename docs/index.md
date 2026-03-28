# summon-claude

**Bridge Claude Code sessions to Slack channels**

Run long-running AI agents in the background. Interact, review permissions, and receive responses — all without leaving Slack.

---

<div class="grid cards" markdown>

-   **[Project management](guide/projects.md)**

    ---

    Group sessions into projects with a PM agent that spawns, directs, and monitors multiple Claude sessions on your behalf.

-   **[Real-time streaming](concepts/threading.md)**

    ---

    Responses stream to Slack as Claude types them. No waiting for full completions before you see results.

-   **[Interactive permissions](reference/permissions.md)**

    ---

    Tool-use requests surface as Slack buttons. Approve or deny without switching to a terminal.

-   **[Smart thread organization](concepts/threading.md#subagent-threads)**

    ---

    Each turn gets its own thread. Subagent work is nested automatically so your channel stays readable.

-   **[Canvas integration](guide/canvas.md)**

    ---

    Persistent markdown canvas per session. Claude can read and write structured notes that survive conversation compaction.

-   **[Scheduled jobs and tasks](guide/cron-tasks.md)**

    ---

    Create cron-style recurring jobs and track task progress directly from Slack.

</div>

---

## How it works

**1. Register a project**

```{ .bash .notest }
summon project add my-api ~/code/my-api
```

Link a name, working directory, and Slack channel prefix to a project.

**2. Set workflow instructions**

```{ .bash .notest }
summon project workflow set my-api
```

Encode team conventions, coding standards, or project context into every session's system prompt.

**3. Start your PM agents**

```{ .bash .notest }
summon project up
```

PM agents launch in the background for all registered projects. Authenticate each one in Slack with `/summon CODE`, then give it instructions — the PM spawns, directs, and monitors child sessions on your behalf.

**4. Interact entirely through Slack**

Send messages, review tool permissions with buttons, and receive streaming responses — no terminal required. The PM coordinates everything.

!!! tip "Quick ad-hoc sessions"
    Don't need the full project setup? Run `summon start` to launch a single session directly. See the [Quick Start guide](getting-started/quickstart.md).

---

## Quick install

=== "uv (Recommended)"
    ```{ .bash .notest }
    uv tool install summon-claude
    ```

=== "pipx"
    ```{ .bash .notest }
    pipx install summon-claude
    ```

=== "Homebrew"
    ```{ .bash .notest }
    brew install summon-claude/summon/summon-claude
    ```

Then run the interactive setup wizard:

```{ .bash .notest }
summon init
```

[Get started with the full setup guide](getting-started/quickstart.md){ .md-button .md-button--primary }
[Installation details](getting-started/installation.md){ .md-button }

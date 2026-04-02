# Agent Permissions

When Claude wants to run a tool that could modify files, execute commands, or take other consequential actions, summon pauses and asks for your approval in Slack. This keeps you in control without interrupting every read operation.

![Permission request in Slack](../assets/screenshots/permissions-approval.png)

---

## Read-only by default

Summon sessions start in **read-only mode**. Claude can freely use research tools (Read, Grep, Glob, WebSearch, etc.) but write-capable tools are blocked until containment is active.

**Write-gated tools:** `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Bash` (and the SDK alias `str_replace_editor`)

Containment is activated in two ways depending on whether the session directory is a git repository:

- **Git repository:** Claude must call `EnterWorktree` to create an isolated working copy. The containment root is the worktree directory.
- **Non-git directory:** Containment is activated automatically when the session starts, using the session's working directory as the containment root. No `EnterWorktree` call is needed (or available).

When Claude tries to use a write-gated tool before containment is active, summon automatically denies the request. Once containment is active:

1. The first write-gated tool triggers a **one-time Slack approval** prompt. For non-git sessions, the approval message includes a warning that changes cannot be automatically rolled back.
2. After approval, writes **within the containment root** are auto-approved — CWD containment ensures file-targeting tools (Edit, Write, etc.) can only modify files inside the containment root without additional prompts.
3. Writes **outside the containment root** still require Slack approval, with per-path session caching available.
4. `Bash` always requires HITL on first use, with per-command session caching (exact match).

### Safe-dir exception

You can configure directories where writes are allowed **without entering containment**:

```bash
summon config set SUMMON_SAFE_WRITE_DIRS "hack/,.dev/"
```

Files written to these directories bypass the containment requirement entirely. Paths are resolved with symlink protection (`Path.resolve()` on both sides) to prevent escapes. Setting `safe_write_dirs=.` exempts the entire project directory for file-targeting tools (Bash remains gated regardless).

---

## Auto-approved tools

The following tools are always approved without prompting. They are read-only and cannot modify your system:

| Tool | Description |
|------|-------------|
| `Read` / `Cat` | Read file contents |
| `Grep` | Search file contents |
| `Glob` | List files by pattern |
| `WebSearch` | Search the web |
| `WebFetch` | Fetch a URL |
| `LSP` | Language server protocol queries |
| `ListFiles` | List directory contents |
| `GetSymbolsOverview` | Read code symbols overview |
| `FindSymbol` | Find a symbol definition |
| `FindReferencingSymbols` | Find symbol references |

Summon's own MCP tools (`summon-cli`, `summon-slack`, `summon-canvas`) are also auto-approved — they are internal tools already scoped to the session's permissions.

---

## Approval flow

When Claude requests a tool that needs approval:

1. **Interactive message** — summon posts a message in the session channel with the tool details and action buttons.
2. **You click Approve, Approve for session, or Deny** — the interactive message is deleted and a persistent confirmation is posted in the turn thread.
3. **Claude continues** — if approved, Claude runs the tool; if denied, Claude is told the action was denied and adapts.

```
Claude wants to run:
`Bash`: `npm run build`

[Approve]  [Approve for session]  [Deny]
```

### Approve for session

Clicking **Approve for session** caches the tool for the remainder of the session:

- **Write-gated tools** (Edit, Write, Bash, etc.) — the specific **argument** is cached: the file path for file tools, the command string for Bash. This prevents blanket `Edit(*)` or `Bash(*)` approval. Writes within the containment root are already auto-approved by CWD containment, so this mainly applies to writes outside the containment root and all Bash commands.
- **Other tools** — the tool name is cached; all subsequent uses are auto-approved.

The confirmation message shows what was cached:

```
✅ Approved for session: `Bash`: `git status`
✅ Approved for session: `Edit`: `/etc/config.ini`
```

!!! warning "GitHub write tools are never session-cached"
    Tools in the GitHub MCP require-approval list (merge, delete branch, create PR, etc.) always require explicit Slack approval — even if you click "Approve for session." This is a defense-in-depth measure.

!!! info "CWD containment and `allowedTools`"
    Write-gated tools that target paths outside the containment root always require Slack approval, even if your `~/.claude/settings.json` includes them in `allowedTools`. This is the same defense-in-depth principle used for GitHub tools — `allowedTools` cannot override CWD containment.

### Batched requests

If Claude requests multiple tools within a 2-second window (configurable via `SUMMON_PERMISSION_DEBOUNCE_MS`), they are batched into a single approval message:

```
Claude wants to perform 3 actions:
1. `Edit`: `src/auth/login.py`
2. `Write`: `src/auth/token.py`
3. `Bash`: `python -m pytest tests/test_auth.py`

[Approve]  [Approve for session]  [Deny]
```

Approve or Deny applies to all tools in the batch. "Approve for session" caches each tool's primary argument (file path or command).

!!! tip "Debounce tuning"
    The default 2000ms window catches most batches naturally. Lower it (e.g. `SUMMON_PERMISSION_DEBOUNCE_MS=500`) to reduce latency, or set it to `0` to get a separate message per tool.

---

## Timeout

Permission requests expire after **10 minutes**. If you do not respond:

- The request is automatically denied.
- The interactive message is deleted.
- A timeout message is posted in the turn thread.
- Claude is told the permission timed out and adapts (typically by reporting it could not complete the action).

---

## GitHub MCP permissions

When GitHub is authenticated (via `summon auth github login`), Claude has access to GitHub tools via the remote MCP server. These follow separate permission tiers:

**Auto-approved (read-only):** Any tool with a `get_`, `list_`, or `search_` prefix, plus `pull_request_read` and `get_file_contents`.

**Always require Slack approval** — checked before prefix rules, so no `allowedTools` pattern can bypass them:

| Tool | Reason |
|------|--------|
| `merge_pull_request` | Irreversible |
| `delete_branch` | Irreversible |
| `close_pull_request` | Visible to others |
| `close_issue` | Visible to others |
| `push_files` | Writes to remote |
| `create_or_update_file` | Writes to remote |
| `update_pull_request_branch` | Modifies shared branch |
| `pull_request_review_write` | Visible to others |
| `create_pull_request` | Visible to others |
| `create_issue` | Visible to others |
| `add_issue_comment` | Visible to others |

Any GitHub MCP tool not on either list also requires Slack approval (fail-closed).

!!! warning "Defense in depth"
    The require-approval list is checked before prefix-based auto-approve. Even if your `~/.claude/settings.json` has a broad `allowedTools` pattern that would normally permit these tools, summon still routes them to Slack for approval.

---

## Jira MCP permissions

When Jira is authenticated (via `summon auth jira login`), Claude has access to Jira tools via the Atlassian Rovo MCP server. Jira uses a **strictly read-only** permission model — all write operations are hard-denied, not routed to Slack for approval.

**Auto-approved (read-only):** Tools matching `get*`, `search*`, or `lookup*` prefixes, plus the exact match `atlassianUserInfo`.

**Hard-denied (always blocked)** — checked before auto-approve prefixes:

| Tool | Reason |
|------|--------|
| `createJiraIssue` | Write operation |
| `editJiraIssue` | Write operation |
| `transitionJiraIssue` | Write operation |
| `addCommentToJiraIssue` | Write operation |
| `addWorklogToJiraIssue` | Write operation |
| `createIssueLink` | Write operation |
| `createConfluencePage` | Write operation |
| `createConfluenceFooterComment` | Write operation |
| `createConfluenceInlineComment` | Write operation |
| `updateConfluencePage` | Write operation |
| `fetchAtlassian` | Generic ARI accessor — bypasses per-tool gating |

!!! warning "`fetchAtlassian` is not a read-only tool"
    Despite its name, `fetchAtlassian` is a generic Atlassian Resource Identifier (ARI) accessor that can fetch arbitrary resources across projects and products. It bypasses the per-tool permission model and is always blocked.

Any Jira MCP tool not in either list is denied by default (fail-closed). New tools from future Rovo MCP updates require explicit classification before they can be used.

!!! note "No HITL tier for Jira"
    Unlike GitHub, Jira has no "requires Slack approval" tier. The OAuth scope is `read:jira-work` — write operations would fail at the API level even if summon allowed them. The hard-deny list provides defense-in-depth.

For the full integration guide, see [Jira Integration](../guide/jira-integration.md).

---

## AskUserQuestion

Claude can ask you structured questions mid-task using the `AskUserQuestion` tool. This appears as an interactive message in the session channel:

```
Claude has a question for you

Which database should I use for the session store?
  [SQLite]  [PostgreSQL]  [Redis]  [Other]
```

- **Single-select:** click a button to answer and continue.
- **Multi-select:** toggle options, then click **Done**.
- **Other:** click **Other** to type a free-text answer in the channel.

Your answers are returned to Claude as structured data. The question times out after 5 minutes if unanswered. The interactive message is deleted after all questions are answered.

---

## Auto-mode classifier

After the agent enters a worktree, summon can automatically approve or block tool calls using a secondary Sonnet classifier — without waiting for Slack approval on every action.

The classifier evaluates each pending tool call against configurable prose rules:

- **Allow rules** — actions that are safe to auto-approve (e.g. local file operations, running tests, git status)
- **Deny rules** — actions that should be blocked (e.g. force pushing, production deploys, sending credentials externally)
- **Uncertain** — when the classifier can't confidently decide, the tool falls through to Slack HITL

The classifier only runs **after worktree entry**. Read-only sessions (before `EnterWorktree`) never use it — all tool decisions go through the standard permission flow.

### Activation

1. Session starts in read-only mode — classifier is dormant
2. Agent enters worktree via `EnterWorktree`
3. If `SUMMON_AUTO_CLASSIFIER_ENABLED=true` (default), the classifier activates
4. Tool calls now go through: write gate → static lists → caches → **classifier** → SDK allow → Slack HITL

Use `!auto on/off` to toggle the classifier mid-session. `!auto on` only works after worktree entry.

### Fallback safety

If the classifier blocks too many consecutive tool calls (3) or too many total (20), it automatically pauses and falls back to manual Slack approval. A notification is posted to the channel. Use `!auto on` to re-enable.

The classifier's block reason is never shown to the outer Claude agent — only a generic "Blocked by auto-mode policy" message is returned. This prevents the agent from learning to craft bypass attempts.

### Configuration

See [Auto Mode](environment-variables.md#auto-mode) for the environment variables that control the classifier.

---

## Permission flow (internal)

The full permission evaluation order in `handle()`:

| Step | Check | Result |
|------|-------|--------|
| 1 | AskUserQuestion intercept | Route to interactive UI |
| 2 | Write gate (`_WRITE_GATED_TOOLS`) | SDK deny → Deny; safe-dir → Allow; no containment → Deny; first write → HITL; within containment root → Allow; outside containment root → fall through |
| 3 | SDK deny suggestions | Deny |
| 4 | Static auto-approve (`_AUTO_APPROVE_TOOLS`) | Allow |
| 5 | GitHub deny-list (`_GITHUB_MCP_REQUIRE_APPROVAL`) | Always HITL |
| 6 | GitHub auto-approve (prefix matching) | Allow |
| 6a | Google MCP auto-approve (prefix matching) | Allow |
| 6b | Jira hard-deny (`_JIRA_MCP_HARD_DENY`) | Always Deny |
| 6c | Jira auto-approve (prefix + exact matching) | Allow |
| 7 | Summon MCP auto-approve (prefix matching) | Allow |
| 8 | Session-lifetime cached approvals | Allow (GitHub deny-list excluded) |
| 9 | Per-argument cache (exact match on primary arg) | Allow if arg matches (GitHub deny-list excluded) |
| 10 | Auto-classifier (Sonnet, only active after worktree entry) | Allow, Block, or fall through on uncertain |
| 11 | SDK allow suggestions | Allow (write-gated tools excluded — CWD containment cannot be overridden) |
| 12 | Slack HITL (interactive message, deleted after) | User decides |

---

## Authorization scope

Only the authenticated user for a session can approve or deny permission requests. The authenticated user is the person who claimed the session with `/summon CODE` in Slack.

If a different user clicks the approval buttons, the action is ignored with a warning logged. This prevents other workspace members from approving actions on your behalf.

# GitHub Integration

??? info "Prerequisites"
    This guide assumes you've completed the [Quick Start](../getting-started/quickstart.md) and have a working `summon config check`.

summon-claude can connect Claude sessions to the GitHub remote MCP server, giving Claude access to GitHub tools (read repositories, search code, create issues, review PRs, etc.) directly from Slack.

---

## Setup

Authenticate with GitHub using the device flow:

```{ .bash .notest }
summon auth github login
```

This opens a browser for GitHub OAuth consent. Once complete, the token is stored securely in summon's config directory.

To verify authentication status:

```bash
summon config check
```

!!! note "No Copilot subscription required"
    The GitHub remote MCP server at `api.githubcopilot.com/mcp/` works with OAuth tokens. A GitHub Copilot subscription is not required.

Once authenticated, the GitHub MCP server is wired into **all** sessions automatically — no per-session configuration needed.

To check authentication status:

```{ .bash .notest }
summon auth github status
```

To remove stored credentials:

```{ .bash .notest }
summon auth github logout
```

---

## How it works

When GitHub credentials are configured, summon adds the GitHub remote MCP server to every Claude session's `mcp_servers` list:

```
https://api.githubcopilot.com/mcp/
Authorization: Bearer <your-token>
```

This uses GitHub's HTTP transport (no local binary or Go install required). The MCP connection is lazy — it only connects when Claude first uses a GitHub tool, so startup time is unaffected.

---

## Available tools

Claude gets access to the full GitHub MCP tool set, including:

- Repository browsing and file contents
- Code search across repositories
- Issue and pull request reading and creation
- PR review submission
- Branch and commit inspection
- Security advisory lookups

The specific tools available depend on GitHub's MCP server version. Claude will report any tool-not-found errors if a tool it tries to use is not available.

---

## Permission tiers

Not all GitHub operations require your approval. summon enforces a three-tier permission model:

### Tier 1: Auto-approved (read-only)

These tools are approved automatically without prompting you:

- Any tool with a name starting with `get_`, `list_`, or `search_`
- `pull_request_read`
- `get_file_contents`

### Tier 2: Requires your approval (writes)

These operations are routed through the standard summon HITL (human-in-the-loop) flow — Claude posts a request to Slack and waits for you to approve or deny:

**Destructive or irreversible operations:**

- `merge_pull_request`
- `delete_branch`
- `close_issue`
- `close_pull_request`
- `push_files`

**Visible-to-others operations:**

- `create_pull_request`
- `create_issue`
- `add_issue_comment` (PR and issue comments)
- `pull_request_review_write`

!!! tip "Why are comments in Tier 2?"
    Comments, PR reviews, and new issues are visible to everyone in your repository. summon routes these through HITL so you can review what Claude is about to post before it goes public.

### Tier 3: Unknown tools (fail-closed)

If a GitHub tool is not in either of the above tiers, it is treated as requiring approval. summon never auto-approves unknown tools.

---

## Secret redaction

GitHub tokens are automatically redacted from all Slack output and log files. The following patterns are scrubbed:

| Token type | Pattern |
|---|---|
| Classic PAT | `ghp_...` |
| Fine-grained PAT | `github_pat_...` |
| OAuth token | `gho_...` |
| User token | `ghu_...` |
| Server-to-server | `ghs_...` |
| Refresh token | `ghr_...` |

Redaction happens at the Slack output boundary — tokens cannot appear in messages, file uploads, canvas content, or channel topics.

---

## PR reviewer sessions

The PM agent can spawn dedicated reviewer sessions that use GitHub MCP tools to review pull requests. There are two ways this happens: you ask for a review, or the PM detects a PR automatically during its periodic scan.

### Asking the PM to review a PR

Send the PM a message in its Slack channel:

```
Review PR #42 on myorg/myrepo.
```

The PM reads the PR metadata (branch, status, whether it is draft), then spawns a reviewer session. You can also paste a full URL:

```
Review https://github.com/myorg/myrepo/pull/42 — focus on the auth changes.
```

If the PR is draft or closed, the PM tells you and does not spawn a reviewer.

### Automatic review after child work

The PM also detects PRs without being asked. During each periodic scan (default: every 15 minutes), the PM checks completed child sessions for GitHub PR URLs in their Slack history. If it finds a PR it has not yet reviewed, it spawns a reviewer automatically and notes it on the canvas.

If your project has [workflow instructions](projects.md#managing-workflow-instructions) that define pre-review steps (e.g., "run the test suite before review"), the PM follows those steps first and only spawns a reviewer after they pass.

### What the reviewer does

The PM spawns the reviewer as a new child session with:

- **Model**: Opus (hardcoded for thorough analysis)
- **CWD**: the directory where the PR branch is already checked out (from the child session that created the PR), or a fresh worktree for external PRs
- **Name**: `rv-pr{number}` (e.g., `rv-pr42`)

The reviewer session works through the PR systematically:

1. Reads the full diff, changed files, and existing comments via GitHub MCP tools
2. Checks for bugs, security issues, logic errors, and style problems
3. Fixes issues directly — commits with descriptive messages and pushes to the PR's head branch
4. Runs the project's test suite before every push
5. Applies the "Ready for Review" label when satisfied
6. Posts a detailed summary of findings and fixes to its Slack channel

The reviewer follows strict safety rules: it never pushes to `main` or `master`, never force-pushes, and does not modify files outside the scope of the PR's changes.

When the reviewer finishes, the PM reads its channel for the summary and updates the project canvas (e.g., "PR #42 — reviewed").

### Approval gate

The reviewer can read PR data freely (Tier 1 auto-approved), but posting a review requires your approval. `pull_request_review_write` is a Tier 2 operation — the reviewer posts a permission request to Slack and waits for you to approve or deny before the review is submitted to GitHub. This gives you a chance to read the review content before it becomes visible on the PR.

### Worktree isolation for reviews

When the PM spawns a reviewer for a PR that was not created by one of its child sessions (an "external" PR), it uses worktree isolation:

1. The PM spawns the reviewer at the project root
2. The reviewer enters a worktree using `EnterWorktree(name="review-pr{number}")`
3. Inside the worktree, the reviewer fetches and checks out the PR's head branch
4. All file reads and writes are constrained to the worktree directory

For PRs created by a child session, the reviewer runs in that child's existing CWD where the branch is already checked out — no worktree needed.

The PM cleans up stale review worktrees automatically. During periodic scans, it checks worktrees under `.claude/worktrees/review-pr*` and removes any whose PR has been merged or closed.

---

## Troubleshooting

**Claude says GitHub tools are not available**

Check that GitHub credentials are configured:

```bash
summon config check
```

If GitHub shows as "not set", re-run `summon auth github login`.

**Permission denied errors from GitHub**

The OAuth token may lack the required scopes. Re-run `summon auth github login` to re-authenticate with the correct permissions.

**Tool calls time out**

The GitHub MCP server is remote — network latency affects tool response times. This is expected. If the server is unreachable, Claude will receive an error and adapt its approach without the session failing.

---

## See also

- [Scribe Integrations](scribe-integrations.md) — Google Workspace and Slack browser monitoring
- [PM Agents](pm-agents.md) — spawning reviewer sessions
- [Sessions](sessions.md) — session lifecycle
- [Configuration](configuration.md) — full configuration reference

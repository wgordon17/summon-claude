# Jira Integration

??? info "Prerequisites"
    This guide assumes you've completed the [Quick Start](../getting-started/quickstart.md) and have a working `summon config check`.

summon-claude connects to Jira via the Atlassian Rovo MCP server, giving Claude **read-only** access to Jira issues, projects, Confluence pages, and related metadata directly from Slack sessions.

---

## Setup

Authenticate with Jira using OAuth 2.1 (PKCE + Dynamic Client Registration):

```{ .bash .notest }
summon auth jira login
```

This opens a browser for Atlassian OAuth consent. No Atlassian admin privileges are required — the flow uses your personal Atlassian account. Once complete, the token is stored securely in summon's config directory with `0600` permissions.

### Cloud site selection

If your Atlassian account has access to multiple cloud sites, you'll be prompted to choose one:

```
Multiple Atlassian cloud sites found:
  1. My Company (https://mycompany.atlassian.net)
  2. Side Project (https://sideproject.atlassian.net)
Select a site [1]:
```

To skip the interactive prompt, pass `--site`:

```{ .bash .notest }
# Short form (appends .atlassian.net automatically)
summon auth jira login --site mycompany

# Full hostname
summon auth jira login --site mycompany.atlassian.net
```

!!! note "Cloud site UUID"
    The `--site` flag resolves to an Atlassian cloud UUID via API discovery. If discovery fails (e.g., network issues), the hostname is stored as a fallback. Re-run `summon auth jira login` if Jira tools report site-related errors.

### Verifying authentication

```bash
summon config check
```

Or check Jira specifically:

```{ .bash .notest }
summon auth jira status
```

### Removing credentials

```{ .bash .notest }
summon auth jira logout
```

This removes the stored OAuth token, client credentials, and cloud site metadata.

---

## How it works

When Jira credentials are configured, summon adds the Atlassian Rovo MCP server to every Claude session's `mcp_servers` list. This uses Atlassian's HTTP transport (no local binary required). The MCP connection is lazy — it only connects when Claude first uses a Jira tool, so startup time is unaffected.

### Token refresh

Access tokens are short-lived (typically 1 hour). summon automatically refreshes tokens at session startup using the stored refresh token:

- Refresh happens before building the MCP config — Claude always gets a fresh token.
- Concurrent sessions use file-based locking (`fcntl.flock`) to prevent races.
- If refresh fails, the session proceeds without Jira tools (logged as a warning, not fatal).

You should not need to re-authenticate unless you revoke access in your Atlassian account settings.

---

## Available tools

Claude gets access to the Atlassian Rovo MCP tool set, including:

- **Jira issues:** search via JQL, get issue details, list projects, look up users
- **Confluence pages:** read page content, list spaces, search via CQL
- **Metadata:** issue types, link types, transitions, labels, remote links

The specific tools available depend on the Rovo MCP server version. All 31 currently known tools are classified into [permission tiers](../reference/permissions.md#jira-mcp-permissions).

---

## Permission model

summon enforces a strict read-only permission model for Jira. All tools are classified at startup — no runtime discovery surprises.

**Auto-approved (read-only):** Tools matching `get*`, `search*`, or `lookup*` prefixes, plus `atlassianUserInfo`. These run without user confirmation.

**Hard-denied (write operations):** `createJiraIssue`, `editJiraIssue`, `transitionJiraIssue`, `addCommentToJiraIssue`, `addWorklogToJiraIssue`, and all Confluence write tools. Claude cannot perform write operations even if prompted.

**Hard-denied (security):** `fetchAtlassian` — a generic ARI accessor that bypasses tool-level gating and could access resources outside the intended scope.

**Fail-closed:** Any unknown Jira tool (e.g., from a future Rovo MCP update) is denied by default until explicitly classified.

!!! warning "No HITL tier for Jira"
    Unlike GitHub, Jira has no "requires Slack approval" tier. The OAuth scope is `read:jira-work` — write operations would fail at the API level even if summon allowed them. The hard-deny list provides defense-in-depth.

For the full tool-by-tool breakdown, see the [Permissions reference](../reference/permissions.md#jira-mcp-permissions).

---

## PM agent integration

When [PM agents](pm-agents.md) are configured with Jira, they automatically triage Jira issues during each periodic scan cycle.

### Per-project JQL filters

Associate a JQL filter with a project to control which issues the PM agent triages:

```{ .bash .notest }
# Set a JQL filter when registering a project
summon project add myproject ./myproject --jql "project = MYPROJ AND status != Done"

# Update the filter for an existing project
summon project update myproject --jql "project = MYPROJ AND assignee = currentUser()"

# Clear the filter (PM scans all visible issues)
summon project update myproject --jql ""
```

Without a `--jql` filter, the PM agent scans all issues visible to the authenticated user.

### Common JQL patterns

| Use case | JQL |
|----------|-----|
| Single project | `project = MYPROJ AND status != Done` |
| Assigned to me | `assignee = currentUser() AND status != Done` |
| High priority | `project = MYPROJ AND priority in (Critical, Blocker)` |
| Recently updated | `project = MYPROJ AND updated >= -7d` |
| Sprint backlog | `project = MYPROJ AND sprint in openSprints()` |
| Unassigned bugs | `project = MYPROJ AND type = Bug AND assignee is EMPTY` |

### How PM triage works

On each scan cycle, the PM agent:

1. Calls `searchJiraIssuesUsingJql` with the project's JQL filter and cloud ID.
2. Assesses urgency based on priority, due date, and labels.
3. For high/critical issues: posts a summary to the PM's Slack channel and optionally notifies relevant child sessions via `session_message`.
4. Updates the canvas under a "Jira Issues" section to track triaged issues.
5. For normal-priority issues: updates the canvas only (no Slack notification).

Canvas state tracking prevents re-alerting on previously triaged issues. The PM reads its canvas on startup to restore the triaged-issue set.

!!! note "Prompt injection defense"
    Jira issue content (summaries, descriptions, comments) may contain adversarial text. The PM agent is instructed to treat all issue text as untrusted data — it summarizes and triages, but never follows instructions found in issue content.

---

## Scribe integration

!!! info "Partial integration"
    Scribe sessions are wired to the Jira MCP server and include Jira-aware domain prompts (untrusted-content warnings, Gmail dedup). However, the scribe **scan prompt does not yet include Jira-specific scan instructions** — Jira monitoring in scribe is domain-aware but not yet actively polled each scan cycle.

When both Google Workspace and Jira are active, the scribe system prompt instructs Gmail deduplication:

- The scribe is told to skip Gmail notifications from Jira (from addresses containing `jira@` or `noreply@` at `atlassian.net` domains) when Jira monitoring is also active.

!!! note "Prompt-level dedup"
    Gmail/Jira dedup is a prompt instruction, not application code. Its effectiveness depends on the scribe agent following the instruction. Active Jira scan polling (which would make the dedup instruction actionable) is not yet wired.

---

## Troubleshooting

See the [Jira troubleshooting section](../troubleshooting.md#jira) for common issues and solutions.

---

## See also

- [GitHub Integration](github-integration.md) — GitHub remote MCP setup and permissions
- [Scribe Integrations](scribe-integrations.md) — Google Workspace and Slack browser monitoring
- [Projects](projects.md) — project registration and JQL filters
- [PM Agents](pm-agents.md) — autonomous project management
- [Permissions](../reference/permissions.md#jira-mcp-permissions) — full permission evaluation flow

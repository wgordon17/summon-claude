# MCP Tool Reference

summon provides three internal MCP servers that Claude uses within sessions. These are **not** user-facing CLI commands — they are tools available to Claude's agent loop during a running session.

<!-- mcp:summary -->
| Server | Available to | Tools |
|--------|-------------|-------|
| `summon-slack` | All sessions | 8 tools — Slack actions and reading |
| `summon-cli` | All sessions (8 tools) + PM sessions (8 additional) | 16 tools — Session lifecycle, scheduling, tasks |
| `summon-canvas` | Sessions with a canvas | 3 tools — canvas read/write |
<!-- /mcp:summary -->

---

## summon-slack

Provides Slack posting and reading actions. All tools operate on the session's own channel by default. Cross-channel reads are gated by the allowed-channels list.

### `slack_upload_file`

Upload a file to the Slack session channel.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | Yes | File text content |
| `filename` | string | Yes | Filename with extension (e.g. `output.txt`) |
| `title` | string | Yes | Display title shown in Slack |
| `snippet_type` | string | No | Syntax highlighting type (e.g. `diff`, `python`, `json`) |

**Returns:** Confirmation message with the uploaded filename.

**Notes:** Maximum file size is 10 MB. The `snippet_type` enables Slack's native syntax highlighting.

---

### `slack_create_thread`

Reply in a thread to a specific Slack message.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `parent_ts` | string | Yes | Message timestamp in `seconds.microseconds` format (e.g. `1234567890.123456`) |
| `text` | string | Yes | Reply text (max 3000 characters) |

**Returns:** Confirmation that the thread reply was posted.

---

### `slack_react`

Add an emoji reaction to a Slack message.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `timestamp` | string | Yes | Message timestamp in `seconds.microseconds` format |
| `emoji` | string | Yes | Emoji name without colons (e.g. `thumbsup`, `white_check_mark`) |

**Returns:** Confirmation showing the emoji name added.

---

### `slack_post_snippet`

Post a formatted code snippet with syntax highlighting to the channel.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `code` | string | Yes | Source code content |
| `language` | string | Yes | Slack snippet type for syntax highlighting. Valid values: `python`, `javascript`, `typescript`, `shell`, `go`, `rust`, `ruby`, `java`, `kotlin`, `swift`, `c`, `cpp`, `csharp`, `html`, `css`, `json`, `yaml`, `toml`, `xml`, `sql`, `diff`, `markdown`, `text` |
| `title` | string | Yes | Display title for the snippet |

**Returns:** Confirmation that the snippet was posted.

**Notes:** Content up to 12K characters is posted as a `type: markdown` block. Larger content is uploaded as a file (max 10 MB).

---

### `slack_update_message`

Update an existing Slack message.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `ts` | string | Yes | — | Message timestamp in `seconds.microseconds` format |
| `text` | string | Yes | — | New message text (max 3000 characters) |
| `channel` | string | No | Session channel | Channel ID containing the message |

**Returns:** Confirmation that the message was updated.

---

### `slack_read_history`

Read recent messages from a Slack channel. Returns top-level messages only (no thread replies), in newest-first order.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | No | `50` | Max messages to return (1–200) |
| `oldest` | string | No | — | Only return messages after this Unix timestamp (e.g. `1234567890.123456`) |
| `channel` | string | No | Session channel | Channel ID to read |
| `format` | string | No | `summary` | Output format: `summary` (compact), `raw` (full Slack API data), or `ai` (AI-generated summary using Haiku; slower) |

**Returns:** Formatted message list or AI summary. System messages (joins, leaves, topic changes) are filtered in `summary` format.

---

### `slack_fetch_thread`

Read replies in a Slack message thread. Results include the parent message as the first entry, followed by replies in chronological order.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `parent_ts` | string | Yes | — | Timestamp of the thread's parent message |
| `limit` | integer | No | `50` | Max replies to return (1–200) |
| `channel` | string | No | Session channel | Channel ID |
| `format` | string | No | `summary` | Output format: `summary`, `raw`, or `ai` |

**Returns:** Formatted thread messages.

---

### `slack_get_context`

Get messages surrounding a specific Slack message, identified by URL or channel+timestamp. Makes 2–3 API calls per invocation.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | No | — | Slack message URL (e.g. `https://workspace.slack.com/archives/C0123/p1234567890123456`) |
| `channel` | string | No | — | Channel ID (alternative to URL) |
| `message_ts` | string | No | — | Message timestamp (alternative to URL; requires `channel`) |
| `surrounding` | integer | No | `5` | Number of messages before and after the target (1–20) |
| `format` | string | No | `summary` | Output format: `summary`, `raw`, or `ai` |

**Returns:** Channel context around the target message, plus thread replies if the message has a thread.

**Notes:** Provide either `url`, or both `channel` and `message_ts`. Threaded URLs automatically fetch the full thread.

---

## summon-cli

Provides session lifecycle management, cron scheduling, and task tracking. All sessions receive 8 tools; PM (project manager) sessions receive 5 additional tools.

### Tools available to all sessions

#### `session_list`

List summon-claude sessions.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `filter` | string | No | `active` | One of: `active` (active sessions only), `all` (including completed/errored), `mine` (spawned by the calling session) |

**Returns:** One line per session: `[session_id] name (status) channel=# turns=N cost=$0.0000`

**Notes:** Scope-guarded — only shows sessions belonging to the authenticated user.

---

#### `session_info`

Get detailed information about a specific session.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | Full session ID to look up |

**Returns:** Key-value pairs for all non-sensitive session fields.

**Notes:** Sensitive fields (`pid`, `error_message`, `authenticated_user_id`) are omitted. Scope-guarded to the authenticated user.

---

#### `CronCreate`

Schedule a prompt to be enqueued on a recurring or one-shot basis. Uses standard 5-field cron syntax.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `cron` | string | Yes | — | 5-field cron expression (minute hour day-of-month month day-of-week). Example: `*/5 * * * *` = every 5 minutes |
| `prompt` | string | Yes | — | Prompt text to inject into the session |
| `recurring` | boolean | No | `true` | Whether the job repeats after firing |

**Returns:** Job ID, human-readable schedule description, and whether recurring. Use the job ID with `CronDelete` to cancel.

!!! note
    `CronCreate`, `CronDelete`, and `CronList` are absent from SDK sessions (e.g. worktree child sessions). They are only available in sessions with a `SessionScheduler` (i.e. summon-managed sessions).

---

#### `CronDelete`

Cancel a scheduled job by ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | Yes | Job ID returned by `CronCreate` |

**Returns:** Confirmation that the job was cancelled, or an error if not found.

---

#### `CronList`

List all scheduled jobs in this session.

No parameters.

**Returns:** A markdown table with columns: ID, Schedule, Prompt, Type (System/Agent), Next Fire, Recurring.

---

#### `TaskCreate`

Create a task to track work items. Tasks persist across context compaction and are visible in the channel canvas.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `content` | string | Yes | — | Task description |
| `priority` | string | No | `medium` | Priority level: `high`, `medium`, or `low` |

**Returns:** The created task ID.

**Notes:** Maximum 100 active tasks per session. When the limit is reached, existing tasks must be updated before new ones can be created.

---

#### `TaskUpdate`

Update a task's status, content, or priority. Only provided fields are changed.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | string | Yes | Task ID returned by `TaskCreate` |
| `status` | string | No | New status: `pending`, `in_progress`, or `completed` |
| `content` | string | No | New task description |
| `priority` | string | No | New priority: `high`, `medium`, or `low` |

**Returns:** Confirmation that the task was updated.

---

#### `TaskList`

List all tasks in this session. PM sessions can also query child session tasks.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | string | No | _(all)_ | Filter by status: `pending`, `in_progress`, or `completed` |
| `session_ids` | string | No | — | Comma-separated session IDs to query (PM sessions only; max 20) |

**Returns:** A markdown table of tasks with ID, status, priority, content, and last-updated time. Cross-session results are grouped by session.

---

### Additional tools for PM sessions only

PM (project manager) sessions are started via `summon project up`. They receive 6 additional tools, plus a 7th (`session_status_update`) when a pinned status message exists.

#### `session_start`

Start a new summon-claude session. Generates a spawn token and creates a pre-authenticated session via daemon IPC.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | — | Session name. Must be lowercase alphanumeric + hyphens, 1–20 characters, starting with alphanumeric |
| `cwd` | string | No | Calling session's cwd | Working directory for the new session. Must be within the calling session's directory (symlink-safe ancestor check) |
| `model` | string | No | _(inherited)_ | Model override |
| `system_prompt` | string | No | — | Additional system prompt text appended to the session (max 10,000 characters) |
| `initial_prompt` | string | No | — | First message injected into the session after startup (max 10,000 characters). Eliminates the need for a follow-up `session_message` call |

**Returns:** The new session ID and a note that the Slack channel will appear shortly. If the active session cap is reached and the caller is a PM, the session is queued and starts automatically when a slot opens.

**Constraints:**
- Cannot spawn sessions outside the calling session's directory tree
- Spawn depth is capped at 2 (root → child → grandchild)
- Maximum active children per PM session is enforced (PM sessions queue when at cap; non-PM sessions receive an error)
- Project ID is auto-propagated from the calling session

---

#### `session_stop`

Stop a running session. Cannot stop your own session.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | Session ID to stop |

**Returns:** Confirmation that the session was stopped.

**Notes:** Scope-guarded to the authenticated user. The target session must be `pending_auth` or `active`.

---

#### `session_log_status`

Log a status update to the session registry audit trail. Does not post to Slack — use `slack_post_snippet` or the main channel for that.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | string | Yes | — | One of: `active`, `idle`, `blocked`, `error` |
| `summary` | string | Yes | — | Brief status summary (max 500 characters) |
| `details` | string | No | — | Structured details in markdown (max 2000 characters) |

**Returns:** Confirmation that the status was recorded.

---

#### `session_message`

Send a message to a running session. The message is injected into the target session's processing queue as a new turn. Also posts an observability message to the target session's Slack channel with source attribution.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | Target session ID |
| `text` | string | Yes | Message text (max 10,000 characters; truncated if longer) |

**Returns:** Confirmation with the target session name and Slack channel.

**Notes:** Can only message sessions that the calling session spawned (parent-child scope guard). Target must be `active`.

---

#### `session_clear`

Clear a child session's conversation context. The session stays active but starts fresh — no prior messages or tool history.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | The session ID to clear |

**Returns:** Confirmation that context was cleared.

**Notes:** Can only clear sessions that the calling session spawned (parent-child scope guard). Target must be `active` and have a triage session name (`gh-triage` or `jira-triage`). Not available to Global PM sessions.

---

#### `session_resume`

Resume a completed or errored session. Creates a new summon session connected to the same Slack channel with Claude SDK transcript continuity.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | ID of the stopped session to resume |
| `model` | string | No | Model override for the resumed session |

**Returns:** The new session ID and the Slack channel link.

**Notes:** Can only resume sessions that the calling session spawned. Target must be `completed` or `errored`. The Slack channel is reused — all resumes continue the same conversation history.

---

#### `session_status_update`

Update the pinned status message in the PM channel. Only available when the PM session has a pinned status message (`pm_status_ts`).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `summary` | string | Yes | — | Brief status text (max 500 characters) |
| `details` | string | No | — | Detailed breakdown (max 2000 characters) |

**Returns:** Confirmation that the status message was updated with a preview of the summary.

**Notes:** Updates the existing pinned message in-place using `chat_update`. Mentions (`@channel`, `@here`, `@everyone`, user mentions, group mentions) are sanitized before posting. The message is formatted as `*Project Manager Status*` with a timestamp. Secrets are redacted at the output boundary.

---

#### `get_workflow_instructions`

Retrieve workflow instructions for a project or the global defaults. Only available to the Global PM session (`is_global_pm=True`).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `project` | string | No | — | Project name or project_id to look up (accepts either). If omitted, returns global default instructions. |

**Returns:** `[Source: project-specific|global default]\n\n<instructions>`, or "No workflow instructions configured." when none are set. Instruction content is passed through output validation.

**Notes:** Uses `registry.get_project()` for indexed name/ID lookup. The source label distinguishes between project-specific overrides and global fallback. `$INCLUDE_GLOBAL` tokens in project instructions are expanded by the underlying `get_effective_workflow()` call.

---

## summon-canvas

Provides canvas read/write operations for sessions with a `CanvasStore`. Available to all sessions (not just PM) when a canvas exists for the session's Slack channel.

### `summon_canvas_read`

Read the channel canvas. Returns the full markdown content of the persistent work-tracking document.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `channel` | string | No | Session channel | Channel ID to read another channel's canvas |

**Returns:** Full markdown content of the canvas.

**Notes:** Cross-channel reads are scope-guarded — only canvases owned by the same authenticated user are accessible.

---

### `summon_canvas_write`

Replace the entire channel canvas with new markdown content.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `markdown` | string | Yes | Full canvas content (max 100,000 characters; whitespace-only is rejected) |

**Returns:** Confirmation that the canvas was updated.

**Warning:** This overwrites all existing canvas content. Use `summon_canvas_update_section` for partial updates.

---

### `summon_canvas_update_section`

Update a single section of the channel canvas by heading name. If the section exists, its content is replaced. If not found, a new section is appended.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `heading` | string | Yes | Section heading text WITHOUT the `##` prefix |
| `markdown` | string | Yes | New section body content (max 100,000 characters; pass empty string to clear the section while keeping the heading) |

**Returns:** Confirmation that the section was updated.

See [Canvas](../guide/canvas.md) for details on how the canvas is structured and synced to Slack.

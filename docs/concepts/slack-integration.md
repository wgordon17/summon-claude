# Slack Integration

summon-claude uses the Slack Bolt framework with Socket Mode for bidirectional communication. All Slack input flows through a single `BoltRouter`; all output goes through per-session `SlackClient` instances.

## BoltRouter

`BoltRouter` owns exactly one `AsyncApp` + `AsyncSocketModeHandler` pair for the lifetime of the daemon. This means a single WebSocket connection to Slack handles all concurrent sessions.

Bolt handlers are registered in `_register_handlers()`:

| Event / Action | Handler |
|---------------|---------|
| `/summon` slash command | `_on_summon_command` → rate limit check → `EventDispatcher.dispatch_command` |
| `message` event | `_on_message` → `EventDispatcher.dispatch_message` |
| `reaction_added` event | `_on_reaction_added` → `EventDispatcher.dispatch_reaction` |
| `file_shared` event | `_on_file_shared` → `EventDispatcher.dispatch_file_shared` |
| `app_home_opened` event | `_on_app_home_opened` → `EventDispatcher.dispatch_app_home` |
| `permission_approve` / `permission_deny` actions | `_on_dispatch_action` → `EventDispatcher.dispatch_action` |
| `ask_user_*` actions | `_on_dispatch_action` → `EventDispatcher.dispatch_action` |
| `turn_overflow` action | `_on_dispatch_action` → `EventDispatcher.dispatch_action` |
| `ask_user_other` view submission | `_on_view_submission` → `EventDispatcher.dispatch_view_submission` |

Handlers are re-registered on every `reconnect()` call because `AsyncApp` instances are created fresh — Bolt does not support attaching handlers to an existing app after construction.

## Socket Mode

Socket Mode connects to Slack over a bidirectional WebSocket (no public HTTP endpoint required). The `AsyncSocketModeHandler` manages the WebSocket lifecycle. `BoltRouter.start()` calls `handler.connect_async()` and caches the bot's `user_id` via `auth.test`.

The `_RateLimiter` class enforces a 2-second per-user cooldown on `/summon` commands to prevent brute-force short-code guessing.

The `AsyncWebClient` is created once and shared across reconnects — it uses `AsyncRateLimitErrorRetryHandler` and `AsyncServerErrorRetryHandler` from the Slack SDK for automatic retry on HTTP 429 and 5xx responses.

## EventDispatcher

`EventDispatcher` maintains a `dict[channel_id, SessionHandle]` — the in-memory registry of running sessions. It routes incoming Slack events by channel ID.

Events for channels with no registered session are silently dropped. This is intentional: channels from previous sessions, bot DMs, and unrelated workspace activity are all ignored.

Each `SessionHandle` contains:
- `message_queue`: asyncio queue the session reads user messages from
- `permission_handler`: handles button-click actions for tool approval
- `abort_callback`: zero-argument callable that cancels the current Claude turn
- `authenticated_user_id`: the Slack user who owns the session
- `pending_turns`: asyncio queue for file upload injection (text and image content)

## SlackClient

`SlackClient` is a channel-bound output client created after a session channel exists. All session output goes through it.

Output methods:

| Method | Description |
|--------|-------------|
| `post()` | Post a message (with optional thread_ts and Block Kit blocks) |
| `update()` | Edit an existing message by timestamp |
| `react()` | Add a reaction emoji to a message |
| `unreact()` | Remove a reaction emoji |
| `upload_file()` | Upload a file snippet (for large outputs) |
| `post_ephemeral()` | Post a message visible only to one user |
| `post_interactive()` | Post a message with interactive buttons (for permission prompts, deleted after interaction) |
| `delete_message()` | Delete a message by timestamp (best-effort) |
| `canvas_create()` | Create a channel canvas |
| `canvas_sync()` | Replace canvas body content |
| `canvas_rename()` | Update canvas title |
| `get_canvas_id()` | Look up the canvas ID for the channel |
| `views_open()` | Open a modal view (for AskUserQuestion "Other" input) |
| `open_chat_stream()` | Open a `chat_stream` in a thread for progressive rendering |

Every output method calls `redact_secrets()` before sending to Slack. See [Security](security.md) for the redaction pattern.

## ThreadRouter

`ThreadRouter` provides three routing destinations within a session channel:

| Destination | When used |
|------------|-----------|
| **Main channel** | Text output before any tool use in a turn; conclusion text after tool use (with `@mention` prefix on first chunk) |
| **Active turn thread** | Tool use, tool results, permission requests, streaming tool output |
| **Subagent thread** | Activity from `Task` tool subagents (nested Claude instances) |

Each Claude turn opens a thread starter message (`Turn N: re: <snippet>`). The starter updates with a summary on completion: file counts, tool call count, context usage (`42k/200k (21%)`).

Turn starter messages include an **overflow menu** with contextual actions:

- **Stop Turn** — cancels the active Claude turn (same as the `:octagonal_sign:` reaction)
- **Copy Session ID** — posts the session ID as an ephemeral message
- **View Cost** — posts a cost hint as an ephemeral message

## Hybrid Streaming (chat_stream)

Thread-based tool progress uses Slack's native `chat_stream` API for progressive message rendering. The main channel response stays as `chat.update` (since `chat_stream` requires `thread_ts`).

When Claude starts using tools, `ResponseStreamer` opens a `chat_stream` in the turn thread. Each tool call sends a `TaskUpdateChunk` with status `in_progress`, and each result sends a completion update. The stream is finalized with `stop(blocks=summary_blocks)` at turn end.

If `chat_stream` fails (unsupported workspace plan, API error), the streamer falls back to `chat_postMessage` — the same behavior as before streaming was added. Both `recipient_team_id` and `recipient_user_id` are required by the Slack API.

## File Handling

Users can drag-drop files into a session channel. The `file_handler` module (`file_handler.py`) classifies, downloads, and prepares files for Claude:

| Type | Extensions | Handling |
|------|-----------|----------|
| **Text** | `.py`, `.js`, `.ts`, `.md`, `.json`, `.yaml`, `.toml`, `.csv`, `.log`, `.sh`, `.html`, `.css`, `.xml`, `.sql`, `.go`, `.rs`, `.rb`, `.java`, `.kt`, `.swift`, `.c`, `.cpp`, `.h`, `.jsx`, `.tsx`, and more | Decoded to UTF-8, wrapped in a fenced code block, injected as a user message |
| **Image** | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` | Base64-encoded, sent as multimodal content blocks via the Claude SDK |
| **Unsupported** | Everything else | Silently dropped (logged as debug) |

**Security controls:**

- Files larger than 10 MB are rejected before download
- Files 1-10 MB trigger a warning but proceed
- Text content is truncated at 100K characters
- Filenames are sanitized (path separators and newlines stripped, 200-char limit)
- Download URLs are validated against `files.slack.com` — no external fetches
- Bot self-uploads are filtered (prevents feedback loops from snippet uploads)
- Only the authenticated session owner's uploads are accepted

## App Home

The App Home tab shows a dashboard of the user's active sessions. Published via `views.publish` whenever a user opens the Summon Claude app in Slack.

The dashboard shows each active session's name, model, channel, status, and context usage percentage. Capped at 20 sessions. Per-user debouncing (60 seconds) prevents redundant API calls on rapid tab switches.

The App Home is only available while the daemon is running — the Bolt instance must be active to receive `app_home_opened` events.

## Rate Limiting and Retry

The `AsyncWebClient` has automatic retry built in via `AsyncRateLimitErrorRetryHandler` (respects Slack's `Retry-After` header on HTTP 429) and `AsyncServerErrorRetryHandler` (retries on 5xx responses).

The `/summon` slash command has an additional in-process rate limiter: 2-second cooldown per Slack user ID, enforced before any database lookup.

## Markdown Conversion

Claude responses are formatted as CommonMark markdown. Before posting to Slack, they are converted to Slack mrkdwn format using the `markdown-to-mrkdwn` library (`slack/formatting.py`).

Conversion handles:
- Headers (`# H1` → `*H1*` bold)
- Bold and italic (`**text**`, `_text_`)
- Inline code and fenced code blocks (preserved as ` `` ` or triple-backtick blocks)
- Lists (bullets and numbered)
- Links (`[text](url)` → `<url|text>`)

Large outputs (over `SUMMON_MAX_INLINE_CHARS`, default 2500 characters) are uploaded as file snippets instead of posted inline.

## Canvas Integration

Each session channel can have one Slack canvas. `CanvasStore` (`slack/canvas_store.py`) maintains a local SQLite copy of the canvas markdown content and synchronizes to Slack in the background.

**Write path:**
1. Claude calls `summon_canvas_write` or `summon_canvas_update_section` via MCP.
2. `CanvasStore` updates the local SQLite record immediately (synchronous from Claude's perspective).
3. The store marks itself dirty and schedules a sync.

**Sync path:**
- A background asyncio task runs continuously.
- On dirty state: waits 2 seconds (debounce), then calls `SlackClient.canvas_sync()`.
- Periodic sync: every 60 seconds regardless of dirty state.
- On failure: after 3 consecutive failures, switches from 60-second to 300-second intervals. Resets on success.

**Canvas API constraints:**

!!! note "Free plan limitation"
    On free Slack workspaces, `canvases.edit` with `changes` array must have exactly 1 item. `canvases.create` with a `channel_id` is required — standalone canvases (no channel) are not available on free plans.

Canvas reads are served from the local SQLite copy — there is no Slack API endpoint that returns canvas content as markdown. This eliminates a round-trip and avoids the HTML-based read API.

## Emoji Lifecycle

Each user message goes through a lifecycle tracked by emoji reactions on the original message:

| Emoji | Meaning |
|-------|---------|
| `:inbox_tray:` | summon received the message (pre-Claude acknowledgement) |
| `:gear:` | Claude is actively processing the turn |
| `:white_check_mark:` | Turn completed successfully |
| `:octagonal_sign:` | Turn was cancelled via `!stop` |
| `:warning:` | An error occurred during the turn |

The `:gear:` emoji replaces `:inbox_tray:` when Claude starts, and is itself replaced by one of the completion states when the turn finishes.

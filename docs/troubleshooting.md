# Troubleshooting & FAQ

This page covers common problems and their solutions. Issues are grouped by category.

---

## Installation

???+ tip "Claude CLI not found"
    **Symptom:** `summon start` fails with "claude: command not found" or similar error.

    **Cause:** The Claude Code CLI is not installed or not on your `PATH`.

    **Fix:** Install the Claude Code CLI globally:
    ```{ .bash .notest }
    npm install -g @anthropic-ai/claude-code
    ```
    Then verify:
    ```{ .bash .notest }
    claude --version
    ```

???+ tip "Python version mismatch"
    **Symptom:** Installation fails with "requires Python >=3.12" or similar.

    **Cause:** summon-claude requires Python 3.12 or later.

    **Fix:** Check your Python version and upgrade if needed:
    ```{ .bash .notest }
    python3 --version
    ```
    Install Python 3.12+ via your package manager or from [python.org](https://python.org). If using `uv`, it manages Python versions for you:
    ```{ .bash .notest }
    uv python install 3.12
    ```

???+ tip "Should I use uv or pip?"
    **Use `uv`.** summon-claude is tested and distributed with `uv`. The recommended install is:
    ```{ .bash .notest }
    uv tool install summon-claude
    ```
    If you install with pip and encounter import or dependency issues, try switching to `uv tool install`.

    To upgrade:
    ```{ .bash .notest }
    uv tool upgrade summon-claude
    ```

???+ tip "google or slack-browser extras not found"
    **Symptom:** `ImportError` for `workspace_mcp` or `playwright`.

    **Cause:** Optional extras were not installed.

    **Fix:** Install with the appropriate extra:
    ```{ .bash .notest }
    uv tool install "summon-claude[google]"        # Google Workspace integration
    uv tool install "summon-claude[slack-browser]" # Slack browser-based auth
    uv tool install "summon-claude[all]"           # All extras
    ```

---

## Slack Setup

???+ tip "Wrong Slack scopes — bot can't post or read messages"
    **Symptom:** Slack returns `missing_scope` errors in logs, or summon cannot post to channels.

    **Cause:** The Slack app is missing required OAuth scopes.

    **Fix:** Go to **api.slack.com/apps → Your App → OAuth & Permissions → Scopes** and add the required bot token scopes. After adding scopes, reinstall the app to your workspace. See [Slack Setup](getting-started/slack-setup.md) for the full scope list.

???+ tip "Messages sent but Claude never responds"
    **Symptom:** You can authenticate with `/summon`, the session channel is created, but messages you type in the channel get no response from Claude. `summon session info` shows `Turns: 0`.

    **Cause:** Event Subscriptions are disabled in the Slack app settings. Without events enabled, Slack delivers slash commands (so `/summon` works) but does NOT deliver `message.channels` events, so the daemon never sees your messages.

    **Fix:**

    1. Go to **api.slack.com/apps → Your App → Event Subscriptions**
    2. Toggle **Enable Events** to **On** (Socket Mode means no Request URL is needed)
    3. Expand **Subscribe to bot events** and verify these events are listed: `message.channels`, `message.groups`, `reaction_added`, `app_home_opened`, `file_shared`
    4. Click **Save Changes**
    5. If prompted, **reinstall the app** to the workspace

    !!! note "The manifest should set this automatically"
        If you created the app from `slack-app-manifest.yaml`, events should be pre-configured. If the toggle is off despite using the manifest, you may need to reinstall the app or re-apply the manifest.

    **How to verify:** Run `summon config check` — it validates Claude CLI availability, Slack API connectivity, token formats, database integrity, Google Workspace status, and feature inventory. If it reports "Slack API reachable" but sessions show 0 turns, event subscriptions are the likely cause.

???+ tip "Socket Mode not enabled"
    **Symptom:** The daemon starts but does not receive Slack events. No messages appear in session channels.

    **Cause:** Socket Mode is not enabled in the Slack app settings.

    **Fix:** Go to **api.slack.com/apps → Your App → Socket Mode** and toggle it on. You also need an app-level token (starts with `xapp-`) — generate one under **Basic Information → App-Level Tokens** with the `connections:write` scope.

???+ tip "Missing app-level token or connections:write scope"
    **Symptom:** Daemon fails to start with an authentication or connection error related to the app-level token.

    **Cause:** The `SUMMON_SLACK_APP_TOKEN` is missing, or the token lacks the `connections:write` scope.

    **Fix:**
    1. In your Slack app settings, go to **Basic Information → App-Level Tokens**.
    2. Create or select a token — it must have the `connections:write` scope.
    3. Set it in your config:
    ```{ .bash .notest }
    summon config set SUMMON_SLACK_APP_TOKEN xapp-...
    ```

???+ tip "Bot not added to channel"
    **Symptom:** summon starts a session but posts nothing to the expected channel, or returns a `not_in_channel` error.

    **Cause:** The Slack bot user has not been invited to the channel.

    **Fix:** In Slack, open the channel and type `/invite @your-bot-name`. The bot must be a member of every channel it uses.

---

## Authentication

???+ tip "Auth code expired"
    **Symptom:** Pasting the auth code into Slack shows "code expired" or the session never activates.

    **Cause:** Auth codes expire after 5 minutes by default.

    **Fix:** Run `summon start` again to get a fresh code. Codes are single-use and time-limited.

???+ tip "Auth code locked after failed attempts"
    **Symptom:** The session shows "code locked" and won't accept new attempts.

    **Cause:** 5 consecutive failed verification attempts lock the code as a security measure.

    **Fix:** The locked code cannot be unlocked. Run `summon start` again to generate a new session with a fresh code.

???+ tip "/summon not recognized in Slack"
    **Symptom:** Typing `/summon` in Slack shows "unknown command" or nothing happens.

    **Cause:** The `/summon` slash command is not configured in the Slack app, or the app has not been reinstalled after adding it.

    **Fix:** Go to **api.slack.com/apps → Your App → Slash Commands** and add the `/summon` command. Then reinstall the app to the workspace. With Socket Mode enabled, no Request URL is needed.

---

## Sessions

???+ tip "Session won't start"
    **Symptom:** `summon start` hangs, exits with an error, or the session never appears in `summon session list`.

    **Cause:** Common causes include: daemon not running, Claude CLI not found, bad config, or a port/socket conflict.

    **Fix:**
    1. Check for active sessions: `summon session list`
    2. Clean up stale sessions: `summon session cleanup`
    3. Check logs for errors: `summon session logs <session-name>`
    4. Verify Claude CLI is available: `claude --version`
    5. Validate config: `summon config check`

???+ tip "Stale daemon / can't start new session"
    **Symptom:** `summon start` fails or sessions aren't working despite config being valid.

    **Cause:** A stale daemon process from a previous crash. The daemon starts automatically with `summon start` and stops when all sessions end.

    **Fix:**
    ```{ .bash .notest }
    summon stop --all
    ```
    If that fails, find and kill the daemon process manually:
    ```{ .bash .notest }
    # Find the daemon process
    pgrep -f "summon.*start"
    ```
    Then start a fresh session with `summon start`.

???+ tip "Stale sessions in session list"
    **Symptom:** `summon session list` shows sessions that are no longer running (status stuck at "active").

    **Cause:** Sessions from a previous daemon instance were not cleaned up when the daemon stopped or crashed.

    **Fix:** Run the cleanup command:
    ```{ .bash .notest }
    summon session cleanup
    ```
    This marks orphaned sessions (present in the database but not tracked by the current daemon) as errored.

???+ tip "Session list shows wrong status"
    **Symptom:** A session shows "active" but the Claude process is not responding.

    **Cause:** The Claude subprocess may have crashed without the daemon detecting it.

    **Fix:**
    ```{ .bash .notest }
    summon session cleanup   # Mark orphaned sessions
    summon session list      # Verify status updated
    ```
    If the session persists, stop it explicitly:
    ```{ .bash .notest }
    summon stop <session-name>
    ```

---

## Permissions

???+ tip "Approval buttons not appearing in Slack"
    **Symptom:** Claude requests a tool use that should require approval, but no Approve/Deny buttons appear in Slack.

    **Cause:** The Slack app is missing the `chat:write` scope, interactivity is not enabled, or the bot is not in the channel.

    **Fix:**
    1. Verify the bot has `chat:write` scope.
    2. Enable interactivity: **api.slack.com/apps → Your App → Interactivity & Shortcuts → On**.
    3. Ensure the bot is in the channel (`/invite @your-bot-name`).

???+ tip "Permission request times out"
    **Symptom:** A pending permission request disappears after a while without being acted on, and Claude proceeds or aborts.

    **Cause:** Permission requests have a timeout. After the timeout, summon defaults to denying the request (fail-safe).

    **Fix:** Respond to permission requests promptly. The debounce interval can be adjusted via `SUMMON_PERMISSION_DEBOUNCE_MS` (default: 500ms):
    ```{ .bash .notest }
    summon config set SUMMON_PERMISSION_DEBOUNCE_MS 1000
    ```

???+ tip "Ephemeral permission messages visible to wrong people"
    **Symptom:** Permission request messages are visible only to you, not to other team members who should see them.

    **Cause:** Permission requests are posted as ephemeral messages in Slack — only visible to the session owner. A separate ping is posted to the main channel to trigger a notification.

    **Behavior:** This is by design. The ephemeral message contains the Approve/Deny buttons. The main channel ping alerts you to check for it. See [Permissions](reference/permissions.md) for details.

---

## Canvas

???+ tip "Canvas not created on free Slack plan"
    **Symptom:** Canvas creation fails with an error like `free_team_canvas_tab_already_exists` or `free_teams_cannot_create_non_tabbed_canvases`.

    **Cause:** Slack's free plan allows only one canvas per channel, and standalone (non-channel) canvases are not allowed.

    **Fix:** summon automatically uses the existing channel canvas if one already exists. If creation still fails:
    1. Check that the channel doesn't already have a canvas associated with it.
    2. If a canvas exists, summon will sync to it automatically once discovered.

    Note: On the free plan, you cannot have more than one canvas per channel. Plan your channel usage accordingly.

???+ tip "Canvas not syncing / content outdated"
    **Symptom:** The Slack canvas for a session shows stale content and isn't updating.

    **Cause:** Canvas syncs are debounced (2-second dirty delay, 60-second background interval) to avoid hitting Slack API rate limits.

    **Fix:**
    - Wait up to 60 seconds for the next sync cycle.
    - If it has been longer than a few minutes, check for errors in the session logs:
      ```{ .bash .notest }
      summon session logs <session-name>
      ```
    - After 3 consecutive sync failures, the sync interval increases to 5 minutes. Check for Slack API errors in the logs.

???+ tip "Canvas edits trigger unwanted Slack channel notifications"
    **Symptom:** The channel receives update messages each time the canvas is edited.

    **Cause:** Slack sends a channel notification when a canvas is edited, with some consolidation within a 4-hour window.

    **Fix:** This is Slack platform behavior and cannot be fully suppressed from the client side. Workspace admins can disable canvas edit notifications in workspace settings.

---

## Daemon

???+ tip "Checking daemon status and health"
    The daemon starts automatically with `summon start` and stops when all sessions end. To check if it's running:
    ```{ .bash .notest }
    summon session list
    ```
    This shows whether the daemon is running, its PID, uptime, and all active sessions.

???+ tip "Finding logs"
    Session logs are stored in the data directory (typically `~/.local/share/summon/logs/`). To view logs for a specific session:
    ```{ .bash .notest }
    summon session logs <session-name>
    ```

    The daemon log is at `<data-dir>/logs/daemon.log`. Check the data directory with:
    ```bash
    summon config check
    ```

???+ tip "Enabling verbose logging for debugging"
    Pass `-v` as a top-level flag:
    ```{ .bash .notest }
    summon -v start --name my-session
    ```
    Verbose logs include SDK events, Slack API calls, and permission flow details.

???+ tip "Daemon won't stop cleanly"
    **Symptom:** `summon stop --all` hangs or the daemon process remains after stopping.

    **Cause:** Active sessions may be taking time to shut down gracefully, or the daemon is waiting for in-flight Slack API calls.

    **Fix:** Wait a few seconds — the daemon performs a graceful shutdown that stops all active sessions first. If it hangs for more than 30 seconds, kill the daemon process directly:
    ```{ .bash .notest }
    pgrep -f "summon.*start" | xargs kill
    ```

---

## Google Workspace

???+ tip "Google OAuth flow fails or never completes"
    **Symptom:** `summon auth google login` hangs, fails with an auth error, or the browser window doesn't open.

    **Cause:** Missing or invalid `client_secret.json`, or the OAuth redirect URI is not configured.

    **Fix:**
    1. Download `client_secret.json` from the Google Cloud Console for your OAuth app.
    2. Place it in your summon data directory (check `summon config path` for the location).
    3. Run the auth flow:
    ```{ .bash .notest }
    summon auth google login
    ```
    4. Complete the browser-based consent flow.

???+ tip "Google scope validation fails"
    **Symptom:** summon reports that required Google scopes are missing even after authorizing.

    **Cause:** The OAuth consent was granted with insufficient scopes, or the stored credentials don't include all required scopes.

    **Fix:** Re-run the auth flow — it will request all required scopes:
    ```{ .bash .notest }
    summon auth google login
    ```
    If scope issues persist, revoke the app's access in your Google account settings and re-authorize.

???+ tip "Google credentials not found"
    **Symptom:** Google Workspace tools fail with a credentials or authentication error after setup appeared to succeed.

    **Cause:** Credentials are stored in `~/.summon/google-credentials/` (or the XDG data directory equivalent). If this path differs from what workspace-mcp expects, credentials won't be found.

    **Fix:** Check where summon stores data (including credentials):
    ```bash
    summon version
    ```
    The `Data dir` line shows the base path. Google credentials are stored under `<data-dir>/google-credentials/`. If credentials are in a different location, re-run `summon auth google login`.

---

## Scribe

???+ tip "Scribe not starting"
    **Symptom:** Scribe agent does not appear in `summon session list` after running `summon project up`.

    **Cause:** Scribe is disabled by default and must be explicitly enabled.

    **Fix:**
    1. Enable scribe in your config:
    ```{ .bash .notest }
    summon config set SUMMON_SCRIBE_ENABLED true
    ```
    2. Scribe auto-starts with `summon project up`. Verify it's running:
    ```{ .bash .notest }
    summon session list
    ```
    Look for a scribe session in the output.

???+ tip "Google Workspace collector not working"
    **Symptom:** Scribe is running but not collecting Google Workspace data (Gmail, Calendar, Drive).

    **Cause:** The Google collector requires separate enablement and authentication.

    **Fix:**
    1. Enable the Google collector:
    ```{ .bash .notest }
    summon config set SUMMON_SCRIBE_GOOGLE_ENABLED true
    ```
    2. Verify Google authentication status:
    ```bash
    summon auth google status
    ```
    3. If not authenticated, run the auth flow:
    ```{ .bash .notest }
    summon auth google login
    ```
    4. Ensure the `workspace-mcp` binary is available. If missing, install the Google extra:
    ```{ .bash .notest }
    uv tool install "summon-claude[google]"
    ```

???+ tip "Slack browser monitoring not working"
    **Symptom:** Scribe is running but not capturing messages from external Slack workspaces.

    **Cause:** The Slack browser monitor requires separate enablement, Playwright, and browser authentication.

    **Fix:**
    1. Enable the Slack browser monitor:
    ```{ .bash .notest }
    summon config set SUMMON_SCRIBE_SLACK_ENABLED true
    ```
    2. Check Slack browser authentication status:
    ```bash
    summon auth slack status
    ```
    3. If not authenticated, run the interactive auth flow:
    ```{ .bash .notest }
    summon auth slack login WORKSPACE_NAME
    ```
    4. Ensure Playwright is installed:
    ```{ .bash .notest }
    uv tool install "summon-claude[slack-browser]"
    ```
    5. The browser-authenticated user must be a member of the channels you want to monitor. Invite the user to any missing channels in the external workspace.

???+ tip "Enterprise Grid Slack monitoring"
    **Symptom:** Slack browser monitor fails to load workspace or shows a workspace picker instead of the client.

    **Cause:** Enterprise Grid workspaces serve a workspace picker page at their enterprise domain (e.g., `gtest.enterprise.slack.com`). The actual Slack SPA client lives at `app.slack.com/client/{TEAM_ID}`.

    **Fix:** Use `summon auth slack login` to authenticate — it handles Enterprise Grid URL resolution automatically. The monitor extracts team IDs from localStorage in the saved browser state and navigates directly to `app.slack.com/client/{TEAM_ID}`, bypassing the workspace picker.

---

## Project Lifecycle

???+ tip "Channels named zzz-..."
    **Symptom:** Slack channels for your sessions have been renamed with a `zzz-` prefix.

    **Cause:** These are suspended channels from running `summon project down`. The `zzz-` prefix is added to visually sort suspended channels to the bottom of the channel list.

    **Fix:** Run `summon project up` to resume sessions. This removes the `zzz-` prefix and restores the channels to their original names.

???+ tip "Project up doesn't resume sessions"
    **Symptom:** Running `summon project up` starts new PM sessions but does not resume previously running child sessions.

    **Cause:** Only sessions in `suspended` status are eligible for automatic resume. Sessions that completed normally (`completed`) or failed (`errored`) are not resumed.

    **Fix:** Check the status of your sessions:
    ```{ .bash .notest }
    summon session list --all
    ```
    Sessions must show `suspended` status to be resumed by `project up`. If sessions are `completed` or `errored`, they will not auto-resume — start new sessions instead.

---

## Getting More Help

If your issue isn't covered here:

1. Run diagnostics: `summon doctor` (checks environment, daemon, database, Slack, logs, and MCP integrations)
2. Run with verbose output: `summon -v doctor` (shows detailed findings and log tails)
3. Check the session logs: `summon session logs <session-name>`
4. Enable verbose logging: `summon -v start --name my-session`
5. Run config validation: `summon config check`
6. Check auth status across all providers: `summon auth status`
7. Export a diagnostic report: `summon doctor --export report.json`
8. Submit a report: `summon doctor --submit` (creates a redacted GitHub issue — requires `gh` CLI)
9. Reset data or config if things are corrupted: `summon reset data` or `summon reset config`
10. Open an issue at [github.com/summon-claude/summon-claude/issues](https://github.com/summon-claude/summon-claude/issues)

See [Diagnostics](guide/doctor.md) for full details on `summon doctor`.

# Scribe Integrations

??? info "Prerequisites"
    This guide assumes you've completed the [Quick Start](../getting-started/quickstart.md), have a working `summon config check`, and have [enabled the Scribe](scribe.md#setup).

The [Scribe agent](scribe.md) can monitor external data sources and surface important information to your Slack channel. Each integration is optional — enable whichever ones are useful for your workflow.

| Integration | What it provides | Extra required |
|---|---|---|
| [Google Workspace](#google-workspace) | Gmail, Calendar, Drive monitoring | `summon-claude[google]` |
| [Slack Browser Monitoring](#slack-browser-monitoring) | External Slack workspace monitoring | `summon-claude[slack-browser]` |

---

## Google Workspace

The Scribe can monitor Gmail, Google Calendar, and Google Drive for important updates.

### Setup

Install the Google extra if you haven't already:

=== "uv"
    ```{ .bash .notest }
    uv pip install 'summon-claude[google]'
    ```

=== "pipx"
    ```{ .bash .notest }
    pipx inject summon-claude workspace-mcp
    ```

Then authenticate with Google:

```{ .bash .notest }
summon auth google login
```

This opens a browser for OAuth consent. Grant access to the Google services you want the scribe to monitor. Credentials are stored in summon's config directory (`google-credentials/`).

To verify authentication status:

```bash
summon auth google status
```

This shows whether credentials exist, which scopes are granted, and whether the token is still valid.

### Enabling the Google collector

Once authenticated, enable the Google data collector in the scribe:

```{ .bash .notest }
summon config set SUMMON_SCRIBE_GOOGLE_ENABLED true
```

By default the scribe monitors `gmail`, `calendar`, and `drive`. To customize:

```{ .bash .notest }
# Monitor only Gmail and Calendar, not Drive
summon config set SUMMON_SCRIBE_GOOGLE_SERVICES gmail,calendar
```

The full set of supported services is: `gmail`, `calendar`, `drive`, `docs`, `sheets`, `chat`, `forms`, `slides`, `tasks`, `contacts`, `search`, `appscript`.

---

## Slack Browser Monitoring

The Scribe can monitor an external Slack workspace using browser-based WebSocket interception. This is separate from the native Slack bot integration used for session interaction — it watches a different workspace (e.g., your company's Slack) via a real browser session.

!!! warning "Browser-based monitoring"
    Slack channel monitoring uses Playwright to capture WebSocket frames from your Slack workspace. This requires the `slack-browser` extra and a Chromium-based browser installed on the host.

### Setup

Install the Slack browser extra if you haven't already:

=== "uv"
    ```{ .bash .notest }
    uv pip install 'summon-claude[slack-browser]'
    ```

=== "pipx"
    ```{ .bash .notest }
    pipx inject summon-claude playwright
    ```

### Authenticate with a Slack workspace

```{ .bash .notest }
summon auth slack login myteam
```

This opens a visible browser window at your Slack workspace. Log in normally — the browser closes automatically after detecting your session. Auth state (cookies and localStorage) is saved to summon's data directory.

The `WORKSPACE` argument accepts:

- A workspace name: `myteam` (becomes `https://myteam.slack.com`)
- An enterprise name: `acme.enterprise` (becomes `https://acme.enterprise.slack.com`)
- A full URL: `https://myteam.slack.com`

After login, the command prompts you to select which channels to monitor using an interactive picker.

!!! tip "Enterprise Grid workspaces"
    Enterprise Grid workspaces serve a workspace picker at their enterprise URL. The scribe handles this automatically by extracting team IDs from the saved browser state and navigating directly to `app.slack.com/client/{TEAM_ID}`.

### Select monitored channels

To change which channels are monitored without re-authenticating:

```{ .bash .notest }
summon auth slack channels
```

This uses the cached channel list from the last authentication. To refresh the channel list from Slack:

```{ .bash .notest }
summon auth slack channels --refresh
```

### Check auth status

```bash
summon auth slack status
```

Shows the configured workspace URL, user ID, auth state age, and monitored channels.

### Remove auth state

```{ .bash .notest }
summon auth slack logout
```

Removes saved browser auth state and workspace config. This cannot be undone.

### Enabling the Slack collector

Once authenticated, enable the Slack data collector in the scribe:

```{ .bash .notest }
summon config set SUMMON_SCRIBE_SLACK_ENABLED true
```

Optionally configure monitored channels and browser:

```{ .bash .notest }
summon config set SUMMON_SCRIBE_SLACK_BROWSER chrome
summon config set SUMMON_SCRIBE_SLACK_MONITORED_CHANNELS C01ABC123,C02DEF456
```

DMs and @mentions are always captured regardless of the channel list. The channel list controls which channels have *all* messages monitored.

### How it works

The browser user must be a member of any channel being monitored — the WebSocket only delivers messages for channels the authenticated user belongs to.

The primary auth cookie (`d`) has a roughly 1-year TTL, so re-authentication is rarely needed. The `x` cookie (CSRF) is not required.

---

## See also

- [Scribe](scribe.md) — the background monitoring agent
- [GitHub Integration](github-integration.md) — GitHub tools for all sessions
- [Configuration](configuration.md) — full configuration reference

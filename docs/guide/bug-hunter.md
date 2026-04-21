# Bug Hunter

??? info "Prerequisites"
    This guide assumes you've completed the [Quick Start](../getting-started/quickstart.md) and [set up a project](projects.md).

The bug hunter is an automated security and correctness scanning agent that runs inside a sandboxed VM. It periodically analyzes your project for vulnerabilities, logic bugs, and anti-patterns, then reports findings to a dedicated Slack channel canvas.

---

## What bug hunter does

The bug hunter runs as a persistent Claude session that wakes up on a configurable interval (default: every 60 minutes) and scans the project for issues. Each scan cycle follows a structured sequence:

1. **Bootstrap** -- read prior scan context and check git log for what changed
2. **Static analysis** -- run linters and type checkers on changed files (ruff, mypy, semgrep, bandit, etc.)
3. **LLM analysis** -- review changed code for security issues, logic bugs, and anti-patterns that static tools miss
4. **Runtime validation** -- run the test suite for changed modules
5. **Report** -- update findings, scan log, and the Slack canvas

The bug hunter is not interactive -- it runs unattended in the background. You read its findings in the Slack canvas and suppress false positives through its memory directory.

---

## Prerequisites

Bug hunter requires [Matchlock](https://github.com/jingkaihe/matchlock), a lightweight VM sandbox tool:

```bash
brew install jingkaihe/essentials/matchlock
```

Matchlock provides the isolated VM environment where the bug hunter's Claude Code instance runs. Without it, bug hunter cannot start.

---

## Enabling bug hunter

Bug hunter is enabled per-project at registration time:

```{ .bash .notest }
summon project add my-api --bug-hunter
```

The bug hunter starts automatically the next time you run `summon project up`. A confirmation message appears in the PM channel when it starts.

If Matchlock is not installed when you pass `--bug-hunter`, the CLI prints an error and does not enable it. If Matchlock is missing at `project up` time, a warning is posted to the PM's Slack channel with install instructions.

---

## Configuration

### Scan interval

The scan interval controls how often the bug hunter checks for new changes. The default is 60 minutes.

**Per-project override** (at registration time):

```{ .bash .notest }
summon project add my-api --bug-hunter --bug-hunter-scan-interval 30
```

**Global default** (applies to all projects without a per-project override):

Set via `summon config set` or the environment variable:

```{ .bash .notest }
SUMMON_BUG_HUNTER_SCAN_INTERVAL_MINUTES=30
```

The minimum interval is 1 minute. In practice, 30--60 minutes is a good balance between responsiveness and resource usage.

### Network allowlist

By default, the bug hunter VM can only reach a small set of domains required for operation:

- `api.anthropic.com` -- Claude API
- `pypi.org` / `files.pythonhosted.org` -- Python packages
- `github.com` -- git operations
- `registry.npmjs.org` -- npm packages

Project-specific additions are additive to these defaults. Configure them through the project's bug hunter settings.

### Credential proxy

The bug hunter uses a credential proxy to securely pass API keys into the VM without exposing them in the filesystem. By default, `ANTHROPIC_API_KEY` is proxied for Claude API access. Additional credentials can be configured for projects that need them (e.g., private registry tokens).

Each credential is mapped to a specific domain -- the VM can only use the credential when connecting to that domain, preventing exfiltration.

---

## How it works

### VM isolation

The bug hunter runs inside a Matchlock VM with strict isolation:

- **Read-only workspace**: the project directory is mounted read-only -- the bug hunter cannot modify your code
- **Restricted network**: only allowlisted domains are reachable
- **Credential proxy**: API keys are proxied per-domain, not injected as environment variables
- **Non-root execution**: Claude Code runs as a non-root user inside the VM
- **Resource limits**: configurable CPU and memory caps (default: 4 CPUs, 4GB RAM)

### Principal hierarchy

The bug hunter follows a strict principal hierarchy for prompt injection defense:

1. System prompt (highest authority)
2. Scan trigger messages from summon-claude
3. User messages posted in the bug hunter channel
4. Code and files being scanned (lowest authority -- treated as data, never as instructions)

Code comments, strings, docstrings, and commit messages are treated as untrusted data. If scanned content attempts to instruct the bug hunter to change behavior, it is classified as suspicious.

### Scan phases

Each scan cycle runs through five phases:

| Phase | What happens |
|-------|-------------|
| Bootstrap | Read SETUP.md, SCAN_LOG.md, check git changes |
| Static analysis | Run linters/SAST on changed files |
| LLM analysis | Review code for issues static tools miss |
| Runtime validation | Run tests for changed modules |
| Report | Update FINDINGS.md, canvas, scan log |

The entire scan cycle has a 30-minute timeout. If exceeded, partial results are recorded and the next cycle resumes from where it left off.

---

## Reading findings

### Slack canvas

The bug hunter maintains a canvas in its Slack channel with:

- **Findings table** -- one row per open finding with severity, file, line, description, confidence, and category
- **Suppressions** -- count of suppressed findings and last review date
- **Last Scan** -- git hash, timestamp, files scanned, new findings added
- **Session Status** -- current state (Scanning / Idle)

### Memory directory

The bug hunter maintains persistent state in `.bug-hunter-memory/` within the project workspace:

| File | Purpose |
|------|---------|
| `SETUP.md` | Project structure notes (created on first scan) |
| `PATTERNS.md` | Project-specific bug patterns and recurring concerns |
| `FINDINGS.md` | Active findings with full details |
| `SUPPRESSIONS.md` | Reviewed findings marked as accepted risk or false positives |
| `SCAN_LOG.md` | Log of completed scans with timestamps and git hashes |

### Finding format

Each finding in `FINDINGS.md` includes:

- **ID** -- sequential identifier (e.g., `BH-001`)
- **Severity** -- CRITICAL, HIGH, MEDIUM, LOW, or INFO
- **Confidence** -- HIGH, MEDIUM, or LOW
- **Category** -- security, correctness, performance, or maintainability
- **File and line** -- exact location in the codebase
- **Description** -- clear explanation of the issue
- **Evidence** -- relevant code snippet
- **Remediation** -- concrete fix recommendation

---

## Suppressing false positives

When the bug hunter reports a finding that is an accepted risk or a false positive, add it to `SUPPRESSIONS.md` in the `.bug-hunter-memory/` directory. Include:

- The finding ID
- Rationale for suppression
- Who reviewed it (user or agent)

The bug hunter checks `SUPPRESSIONS.md` before reporting new findings and skips anything that matches a suppressed pattern.

---

## PM integration

The PM agent can read the bug hunter's canvas using the `summon_canvas_read` MCP tool. During scan cycles, the PM can monitor bug hunter findings and incorporate them into task planning -- for example, spawning a child session to fix a critical finding.

The bug hunter gets its own Slack channel, named with the project's channel prefix (e.g., `my-api-bug-hunter-...`).

---

## Security model

The bug hunter's security design follows a defense-in-depth approach:

- **VM sandbox** -- complete process isolation via Matchlock
- **Read-only workspace** -- no modification of source code
- **Network restriction** -- only allowlisted domains reachable
- **Credential proxy** -- per-domain credential binding prevents exfiltration
- **Principal hierarchy** -- scanned code is treated as untrusted data
- **Non-root user** -- Claude Code runs without elevated privileges
- **Prompt injection defense** -- explicit rules prevent scanned content from being treated as instructions

---

## See also

- [Projects](projects.md) -- setting up and managing projects
- [PM Agents](pm-agents.md) -- how the PM interacts with bug hunter findings
- [Canvas](canvas.md) -- persistent markdown in Slack channel tabs
- [Configuration](configuration.md) -- project-level config options

# summon-claude

[![PyPI](https://img.shields.io/pypi/v/summon-claude)](https://pypi.org/project/summon-claude/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/summon-claude/summon-claude/actions/workflows/ci.yaml/badge.svg)](https://github.com/summon-claude/summon-claude/actions/workflows/ci.yaml)
[![Docs](https://img.shields.io/badge/docs-summon--claude.github.io-blue)](https://summon-claude.github.io/summon-claude/)

Bridge Claude Code sessions to Slack channels. Run `summon start` in a terminal, authenticate from Slack, and interact with Claude entirely through a dedicated Slack channel. Includes PM agents for multi-session coordination, a scribe for inbox monitoring, and a bug hunter for automated security scanning in sandboxed VMs.

## Install

```bash
uv tool install summon-claude
```

## Quick Start

```bash
# Set up your Slack app and configure tokens
summon init

# Start a session
summon start

# Authenticate in Slack
/summon <code>
```

## Documentation

Full documentation at **[summon-claude.github.io/summon-claude](https://summon-claude.github.io/summon-claude/)**.

- [Installation](https://summon-claude.github.io/summon-claude/latest/getting-started/installation/) — uv, pipx, or Homebrew
- [Slack Setup](https://summon-claude.github.io/summon-claude/latest/getting-started/slack-setup/) — app manifest, tokens
- [Quick Start](https://summon-claude.github.io/summon-claude/latest/getting-started/quickstart/) — first session walkthrough
- [Guides](https://summon-claude.github.io/summon-claude/latest/guide/sessions/) — sessions, commands, configuration
- [CLI Reference](https://summon-claude.github.io/summon-claude/latest/reference/cli/) — auto-generated command docs
- [Concepts](https://summon-claude.github.io/summon-claude/latest/concepts/overview/) — system design

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT

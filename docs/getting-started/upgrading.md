# Upgrading

## Auto-update notifications

summon-claude checks for new versions at startup. When a newer version is available, it prints a notice:

```
Update available: 1.2.3 → 1.3.0
Run: uv tool upgrade summon-claude
```

The check is non-blocking and does not delay session startup. To disable it permanently:

```{ .bash .notest }
summon config set SUMMON_NO_UPDATE_CHECK true
```

---

## Checking your current version

```bash
summon --version
# or
summon version
```

---

## Upgrade commands

| Install method | Upgrade command |
|----------------|-----------------|
| uv | `uv tool upgrade summon-claude` |
| pipx | `pipx upgrade summon-claude` |
| Homebrew | `brew upgrade summon-claude` |

!!! tip "Upgrading with uv"
    `uv tool upgrade summon-claude` upgrades to the latest release and updates the isolated environment in a single step. No virtualenv management needed.

---

## Breaking changes policy

summon-claude follows [semantic versioning](https://semver.org/):

- **Patch releases** (1.2.x): Bug fixes. Always safe to upgrade.
- **Minor releases** (1.x.0): New features, backward-compatible. Upgrade freely.
- **Major releases** (x.0.0): May include breaking changes. Read the changelog before upgrading.

Breaking changes are documented in the [GitHub releases](https://github.com/summon-claude/summon-claude/releases) page with migration instructions.

!!! warning "Config file changes"
    Occasionally a major release changes the config file format. summon-claude will warn you at startup if your config needs updating, and provide the command to migrate it automatically.

---

## Upgrading active sessions

Upgrading summon-claude while sessions are running is safe — existing sessions continue using the version they started with. The new version takes effect for sessions started after the upgrade.

To apply an upgrade to all sessions:

```{ .bash .notest }
summon stop --all
uv tool upgrade summon-claude
summon start
```

# Release Process

## Versioning

Package version is derived from git tags via `hatch-vcs` — no manual version file to update.

- **Tagged releases:** `v1.2.3` → package version `1.2.3`
- **Dev builds on main:** auto-suffixed (e.g. `1.2.3.dev5+gabcdef`)

The `local_scheme = "no-local-version"` setting in `pyproject.toml` strips the local part from published builds, keeping PyPI versions clean.

---

## How to Release

The `make release` target handles the full flow interactively:

```{ .bash .notest }
make release
```

This script:
1. Verifies you are on `main` and not in a worktree
2. Fetches existing tags
3. Shows the current version and prompts for the new version (X.Y.Z format)
4. Validates the version is a valid semver and not a downgrade
5. Creates and pushes an annotated git tag (`v<version>`)
6. Creates a GitHub Release with auto-generated notes

Alternatively, use the GitHub CLI directly:
```{ .bash .notest }
gh release create v0.2.0 --generate-notes
```

Or via the GitHub UI: **Releases → Draft a new release → Create tag → Publish release**.

---

## What Happens After Release

1. The GitHub Release creates the git tag
2. The `release: published` event triggers the `publish.yaml` workflow
3. The workflow checks out with `fetch-depth: 0` (required for hatch-vcs to read all tags)
4. `uv build` runs — `hatch-vcs` reads the tag → produces a versioned sdist and wheel
5. Artifacts are published to PyPI via OIDC trusted publisher (no stored API tokens)
6. After PyPI publish succeeds, a `repository_dispatch` event triggers the Homebrew tap update

---

## CI Workflows

### `ci.yaml` — PR Checks

Triggers: pull requests to `main`, manual dispatch

Runs `make py-lint` (ruff check + format) and `make py-test` (full pytest suite) on every PR. Cancels in-progress runs for the same PR to avoid resource waste.

### `publish.yaml` — Build & Publish

Triggers:
- **Push to `main`** → builds and publishes to TestPyPI
- **GitHub Release published** → builds and publishes to production PyPI, then triggers Homebrew update

The build job produces a versioned sdist and wheel, generates SHA-256 checksums, and uploads both as artifacts. Publish jobs download the artifacts, verify checksums, and publish via OIDC.

### `renovate.yaml` — Dependency Updates

Triggers: weekly (Saturday 6 AM EST), manual dispatch

Runs self-hosted Renovate to create dependency update PRs. Updates Python dependencies, GitHub Actions SHAs, and pre-commit hook versions. Configuration lives in `.github/renovate.json`.

Renovate requires a PAT (`RENOVATE_TOKEN` secret) with `repo` and `workflow` scopes.

---

## One-Time Setup (New Maintainers)

The following setup is required before the first publish. It is already done for the `summon-claude` project — this is here for reference if the project is forked or moved.

### 1. Create GitHub Environments

Go to: **GitHub repo → Settings → Environments → New environment**

Create two environments:
- `testpypi` — for TestPyPI publishing (triggered by push to main)
- `pypi` — for production PyPI publishing (triggered by GitHub Release)

Optional: add deployment protection rules (e.g. required reviewers) on the `pypi` environment for extra safety.

### 2. Configure Trusted Publishers on PyPI

OIDC trusted publishers allow publishing without storing API tokens as secrets.

**PyPI (production):** Go to [pypi.org/manage/account/publishing/](https://pypi.org/manage/account/publishing/)

- Project name: `summon-claude`
- GitHub owner: `summon-claude`
- Repository: `summon-claude`
- Workflow: `publish.yaml`
- Environment: `pypi`

**TestPyPI:** Go to [test.pypi.org/manage/account/publishing/](https://test.pypi.org/manage/account/publishing/)

- Project name: `summon-claude`
- GitHub owner: `summon-claude`
- Repository: `summon-claude`
- Workflow: `publish.yaml`
- Environment: `testpypi`

For first-time packages, use "pending publishers" — no existing PyPI project is needed. After the first successful publish, the project moves from "pending" to "active" publisher automatically.

### 3. Create Renovate PAT

Create a classic personal access token for self-hosted Renovate:

1. Go to: **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)**
2. Create a token with scopes:
   - **`repo`** — create branches, update files, create/update PRs
   - **`workflow`** — update pinned action SHAs in `.github/workflows/`
3. Add as repo secret: **GitHub repo → Settings → Secrets and variables → Actions → New repository secret**
   - Name: `RENOVATE_TOKEN`
   - Value: the PAT

### 4. Repository Default Permissions

Go to: **GitHub repo → Settings → Actions → General → Workflow permissions**

Set to **Read repository contents and packages permissions** (read-only default). The publish jobs request `id-token: write` permission explicitly via `permissions:` in the workflow.

---

## Homebrew Formula

The Homebrew formula lives in a separate tap repository: [summon-claude/homebrew-summon](https://github.com/summon-claude/homebrew-summon).

After each PyPI release, the `publish.yaml` workflow sends a `repository_dispatch` event to `homebrew-summon` with the release version:

```{ .bash .notest }
gh api repos/summon-claude/homebrew-summon/dispatches \
  --method POST \
  -f event_type=pypi-release \
  -f 'client_payload[version]=v1.2.3'
```

The tap repository receives this event and runs its formula update workflow, which:
1. Fetches the new release artifacts from PyPI
2. Computes SHA-256 checksums
3. Updates the formula with the new version and checksums
4. Opens a PR (or auto-merges, depending on tap config)

**Homebrew and sdist vs. wheel:**
`brew update-python-resources` prefers sdists over wheels by default. For packages with broken sdists (e.g., missing files), the tap's `prefer-wheels.py` script swaps specific packages to use their wheels instead. The list of packages with broken sdists is maintained in the tap's update workflow.

The Homebrew tap update requires a `HOMEBREW_TAP_TOKEN` secret with `repo` scope, stored in the main repo's secrets.

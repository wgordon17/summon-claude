# GitHub Actions Workflows

## Workflows

### `ci.yaml` — PR Checks

Triggers: pull requests to `main`, manual dispatch

Runs lint (`make py-lint`) and tests (`make py-test`) on every PR. Cancels in-progress runs for the same PR.

### `renovate.yaml` — Dependency Updates

Triggers: weekly (Saturday 6 AM EST), manual dispatch

Runs self-hosted Renovate to create dependency update PRs. Updates Python dependencies, GitHub Actions SHAs, and pre-commit hook versions. Config in `.github/renovate.json`.

### `publish.yaml` — Build & Publish

Triggers:
- **Push to `main`** → builds and publishes to TestPyPI
- **GitHub Release published** → builds and publishes to production PyPI

Uses OIDC trusted publishers — no API tokens stored as secrets.

## Setup Required Before First Publish

### 1. Create GitHub Environments

Go to: **GitHub repo → Settings → Environments → New environment**

Create two environments:
- `testpypi` — for TestPyPI publishing (push to main)
- `pypi` — for production PyPI publishing (GitHub Release)

Optional: add deployment protection rules (e.g. required reviewers) on the `pypi` environment.

### 2. Configure Trusted Publishers on PyPI

For first-time packages, use "pending publishers" — no existing project needed.

**PyPI (production):** Go to [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/)
- Project name: `summon-claude`
- GitHub owner: `summon-claude`
- Repository: `summon-claude`
- Workflow: `publish.yaml`
- Environment: `pypi`

**TestPyPI:** Go to [test.pypi.org/manage/account/publishing](https://test.pypi.org/manage/account/publishing/)
- Project name: `summon-claude`
- GitHub owner: `summon-claude`
- Repository: `summon-claude`
- Workflow: `publish.yaml`
- Environment: `testpypi`

After first successful publish, the project moves from "pending" to "active" publisher automatically.

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

Set to **Read repository contents and packages permissions** (read-only default).

## Release Process

### Versioning

Package version is derived from git tags via `hatch-vcs` — no manual version bumping needed.

- **Tagged releases:** `v1.2.3` → package version `1.2.3`
- **Dev builds on main:** auto-suffixed (e.g. `1.2.3.dev5+gabcdef`)

### How to Release

```bash
gh release create v0.2.0 --generate-notes
```

Or via GitHub UI: **Releases → Draft a new release → Create tag → Publish release**

### What Happens

1. GitHub Release creates the git tag
2. `release: published` event fires the publish workflow
3. Workflow checks out with `fetch-depth: 0` (required for hatch-vcs to read tags)
4. `uv build` → `hatch-vcs` reads the tag → produces versioned sdist and wheel
5. Artifacts published to PyPI via OIDC trusted publisher

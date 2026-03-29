# Testing Guide

## Test Strategy

The test suite uses `pytest` with `pytest-asyncio` for async tests and `pytest-xdist` for parallel execution. Tests mock Slack and Claude SDK interactions so the full suite runs without real credentials.

Key patterns:
- **Async fixtures** — most fixtures are `async def` and use `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed)
- **Mock Slack** — `AsyncWebClient` and Bolt app are mocked; tests verify calls and responses without hitting Slack's API
- **Mock Claude SDK** — `ClaudeSDKClient` is mocked to return controlled event sequences
- **SQLite in-memory** — registry tests use a fresh in-memory database per test function

---

## Running Tests

### Full suite

```{ .bash .notest }
make test
# or
uv run pytest tests/ -v
```

### Quick (skip slow + slack, fail-fast)

```{ .bash .notest }
make py-test-quick
# or
uv run pytest --maxfail=1 -q -m "not slow and not slack"
```

### Single module

```{ .bash .notest }
uv run pytest tests/test_auth.py -v
```

### By name pattern

```{ .bash .notest }
uv run pytest -k "test_session_cleanup" -v
```

### Serial mode (disable parallel)

```{ .bash .notest }
uv run pytest -n0
```
Useful when debugging — parallel workers can obscure output and interleave logs.

---

## Integration Tests

Slack integration tests hit the real Slack API. They require live workspace credentials set as environment variables:

```{ .bash .notest }
export SUMMON_TEST_SLACK_BOT_TOKEN=xoxb-...
export SUMMON_TEST_SLACK_APP_TOKEN=xapp-...
export SUMMON_TEST_SLACK_CHANNEL_ID=C...
```

Run with:
```{ .bash .notest }
make py-test-slack
# or
uv run pytest tests/integration/ -v -m slack -n0
```

Note: `-n0` (serial) is required for Slack integration tests — parallel runs can hit rate limits and cause flaky failures.

These tests are excluded from the default `make test` run. They are not run in CI on PRs; they can be run manually or in a dedicated CI job with secrets.

---

## pytest Configuration

From `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
pythonpath = ["tests"]
testpaths = ["tests"]
python_files = "test_*.py"
python_classes = "Test*"
python_functions = "test_*"
addopts = [
    "--strict-markers",
    "--tb=short",
    "-p", "no:pastebin",
    "-p", "no:junitxml",
    "-n4",
    "--dist=loadgroup",
]
cache_dir = ".cache/pytest"
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "slack: marks tests requiring real Slack workspace credentials",
    "xdist_group: groups tests to run on the same worker (pytest-xdist)",
]
```

Key settings:
- `asyncio_mode = "auto"` — all `async def` test functions and fixtures are treated as async without requiring `@pytest.mark.asyncio`
- `asyncio_default_fixture_loop_scope = "function"` — each test function gets a fresh event loop
- `"-n4"` — four parallel workers by default
- `"--dist=loadgroup"` — tests marked with `@pytest.mark.xdist_group("name")` always run on the same worker (required for tests that share state)
- `--strict-markers` — unregistered markers cause an error; always declare new markers in `pyproject.toml`

---

## Writing Tests

### File and naming conventions

- Test files: `tests/test_<module>.py`
- Test classes: `Test<Feature>` (optional — flat functions are fine)
- Test functions: `test_<behavior>`
- Integration tests: `tests/integration/test_<feature>.py`, marked with `@pytest.mark.slack`

### Async test pattern

```{ .python .notest }
import pytest
from unittest.mock import AsyncMock, MagicMock

async def test_session_starts():
    registry = AsyncMock()
    registry.get_session.return_value = {"name": "test", "status": "active"}
    # ... test body
    result = await some_async_function(registry)
    assert result.status == "active"
```

No `@pytest.mark.asyncio` decorator needed — `asyncio_mode = "auto"` handles it.

### Fixture patterns

```{ .python .notest }
import pytest

@pytest.fixture
async def mock_registry():
    registry = AsyncMock()
    registry.get_session.return_value = None
    yield registry
    # cleanup if needed

@pytest.fixture
async def fresh_db(tmp_path):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as db:
        # setup schema
        yield db
```

### conftest.py

Shared fixtures live in `tests/conftest.py`. The `pythonpath = ["tests"]` setting means `from helpers import ...` works in test files without relative imports.

### Testing database migrations

Migration tests use a real SQLite database (not in-memory) to test the migration path:

```{ .python .notest }
async def test_migration_adds_column(tmp_path):
    db_path = str(tmp_path / "test.db")
    registry = SessionRegistry(db_path)
    await registry._connect()
    # verify schema is at expected version
    version = await registry.get_schema_version()
    assert version == CURRENT_SCHEMA_VERSION
```

### xdist group isolation

If multiple tests share a resource (e.g., a named socket), group them so they run serially on the same worker:

```{ .python .notest }
@pytest.mark.xdist_group("daemon_socket")
async def test_daemon_connects(): ...

@pytest.mark.xdist_group("daemon_socket")
async def test_daemon_disconnects(): ...
```

### Marking slow tests

```{ .python .notest }
@pytest.mark.slow
async def test_full_session_lifecycle():
    # takes several seconds
    ...
```

Excluded from quick runs: `make py-test-quick` passes `-m "not slow and not slack"`.

---

## Documentation Validation Tests

Documentation tests (`pytest.mark.docs`) verify that docs stay in sync with the codebase and that code examples are valid.

### Running doc tests

```{ .bash .notest }
make docs-test       # Guard tests + Python blocks (fast, no credentials)
make docs-test-full  # All doc tests including link validation (slower)
```

### Test categories

| Test file | What it checks |
|-----------|---------------|
| `test_cli_commands.py` | CLI commands in docs match Click definitions |
| `test_env_vars.py` | `SUMMON_*` env vars in docs match `SummonConfig` fields |
| `test_mcp_tools.py` | MCP tool docs match source tool schemas and counts |
| `test_bash_codeblocks.py` | `summon` commands in bash blocks execute successfully |
| `test_links.py` | External URLs in docs return 2xx/3xx (rejects redirects) |

### `notest` markers

Non-executable code blocks are marked with `notest` after the language identifier:

````markdown
```{ .bash .notest }
# This block is skipped by the test executor
summon start --model opus
```
````

Add `notest` to blocks that:

- Show command **output** (not commands to run)
- Show file contents, config examples, or environment variables
- Contain interactive or daemon-dependent commands (`summon start`, `summon init`, etc.)
- Contain only non-`summon` commands (git, brew, make, npm) — these are already ignored by the executor but `notest` makes intent explicit

The `notest` attribute is invisible to documentation readers — markdown renderers ignore unknown attributes after the language identifier.

### CI integration

- **`checks` job (PRs):** Runs `make docs-test` — guard tests and Tier 1 bash commands (`summon --version`, `summon --help`)
- **`slack-integration` job:** Runs bash CLI tests with real credentials (Tier 2 commands like `summon config show`)
- **`make py-test`:** Includes doc guard tests (excludes `slow` link tests)
- **`make py-test-quick`:** Excludes all doc tests for speed

---

## CI Testing

The `ci.yaml` workflow runs on every PR to `main`:

1. `make py-lint` — ruff check + format (fails if files were modified by auto-fix)
2. `make py-test` — full pytest suite (4 parallel workers)

Lint runs first; tests only run if lint passes.

**Debugging CI failures:**

- **Flaky parallel test:** Run locally with `-n0` to get clean output: `uv run pytest tests/test_<module>.py -v -n0`
- **Missing marker:** `--strict-markers` means any test with an undeclared marker fails; add the marker to `pyproject.toml`
- **Asyncio fixture scope error:** Check `asyncio_default_fixture_loop_scope` — function scope means fixtures cannot be session/module scoped unless they manage their own loop
- **Import error in tests:** Verify `pythonpath = ["tests"]` is set and the import is from a file in `tests/`, not a package import that should use `summon_claude.*`

Type checking is a separate target (`make py-typecheck`) and is not currently enforced in CI — it can be run locally during development.

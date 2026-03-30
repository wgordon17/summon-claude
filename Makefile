# summon-claude Development Makefile
#
# Common tasks for development workflow.
#
# Lint targets auto-fix and fail if files were modified (for CI/hooks).

CURRENT_BRANCH := $(shell git branch --show-current)

.PHONY: help
.PHONY: install lint test build clean all release
.PHONY: py-install py-lint py-typecheck py-test py-test-slack py-test-quick py-build py-clean py-all
.PHONY: repo-hooks-install repo-hooks-clean
.PHONY: docs-prompts docs-serve docs-build docs-check docs-screenshots docs-terminal docs-test

# Default target - auto-generated from inline ## comments
help:
	@echo "summon-claude Development Commands ($(CURRENT_BRANCH))"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ============================================================================
# CORE TARGETS
# ============================================================================

install: py-install repo-hooks-install ## Install all dependencies

lint: py-lint ## Run all linters (auto-fix + verify)

test: py-test ## Run all tests

build: py-build ## Build sdist and wheel

clean: py-clean repo-hooks-clean ## Remove all build artifacts

all: py-all ## Complete workflow: install → lint → test

# ============================================================================
# PYTHON
# ============================================================================

py-install: ## Install Python dependencies
	uv sync

py-lint: ## Lint Python (auto-fix ruff check + format)
	@echo "Running ruff check (auto-fix)..."
	uv run ruff check . --fix --exit-non-zero-on-fix
	@echo "Running ruff format (auto-fix)..."
	uv run ruff format . --exit-non-zero-on-format

py-typecheck: ## Run pyright type checking
	@echo "Running pyright..."
	uv run pyright

py-test: ## Run full Python test suite (excludes Slack integration)
	@echo "Running pytest..."
	uv run pytest tests/ -v -m "not slack"

py-test-slack: ## Run Slack integration tests (requires credentials)
	@echo "Running Slack integration tests..."
	uv run pytest tests/integration/ -v -m slack -n0

py-test-quick: ## Run quick Python tests (exclude slow, fail-fast)
	@echo "Running quick pytest..."
	uv run pytest --maxfail=1 -q -m "not slow and not slack and not docs"

py-build: ## Build sdist and wheel
	uv build

py-clean: ## Remove Python cache files
	rm -rf .cache dist

py-all: py-install py-lint py-test ## Python workflow: install → lint → test

# ============================================================================
# DOCS
# ============================================================================

docs-prompts: ## Regenerate docs/reference/prompts.md from source constants
	uv run python scripts/generate_prompt_docs.py

docs-serve: ## Serve docs locally with live reload
	uv run mkdocs serve

docs-build: ## Build docs site
	uv run mkdocs build

docs-check: ## Verify docs build (strict mode, catches broken links)
	uv run mkdocs build --strict

docs-screenshots: ## Generate documentation screenshots (all sections)
	uv run python scripts/docs-screenshots.py --output docs/assets/screenshots/

docs-terminal: ## Capture terminal output and inject into docs
	uv run python scripts/docs-screenshots.py --section terminal

docs-test: ## Run doc validation tests (guard tests + Python blocks, no credentials)
	uv run pytest --markdown-docs docs/ tests/docs/ -v -m "docs and not slow" -n0

docs-test-full: ## Run all doc tests including link validation (slower, needs network)
	uv run pytest --markdown-docs docs/ tests/docs/ -v -m docs -n0

# ============================================================================
# REPO HOOKS
# ============================================================================

repo-hooks-install: ## Install git hooks (prek)
	uvx prek auto-update
	uvx prek install --install-hooks

repo-hooks-clean: ## Remove git hooks and cache
	uvx prek uninstall || true
	uvx prek cache clean

repo-branches-clean: ## Clean up unused development branches and worktrees except current
	@echo "Removing worktrees..."
	git worktree list --porcelain | grep "^worktree" | cut -d" " -f2 | grep -v "^"$$(git rev-parse --show-toplevel)"$$" | xargs -I {} git worktree remove {}
	@echo "Removing local branches..."
	git branch | grep -v "main" | grep -v "$(CURRENT_BRANCH)" | grep -v "^\*" | xargs -I {} git branch -D {}
	@echo "Syncing remote branch state..."
	git fetch --all --prune
	@echo "Removing remote-origin branches..."
	git branch -r | grep "origin/" | grep -v "origin/main" | grep -v "origin/$(CURRENT_BRANCH)" | grep -v "origin/HEAD" | sed 's|origin/||' | xargs -I {} git push origin --delete {}

release: ## Tag and publish a new release (interactive, main branch only)
	@set -e; \
	GIT_DIR=$$(git rev-parse --git-dir); \
	if [ "$$GIT_DIR" != ".git" ]; then \
		echo "ERROR: Cannot release from a worktree. Run from the repo root."; \
		exit 1; \
	fi; \
	BRANCH=$$(git branch --show-current); \
	if [ "$$BRANCH" != "main" ]; then \
		echo "ERROR: Must be on main branch (currently on $$BRANCH)."; \
		exit 1; \
	fi; \
	git fetch --tags; \
	LATEST=$$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0"); \
	LATEST_CLEAN=$${LATEST#v}; \
	echo "Current version: $$LATEST"; \
	read -p "New version (X.Y.Z): " VERSION; \
	if ! echo "$$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		echo "ERROR: Invalid semver format. Expected X.Y.Z (e.g., 1.2.3)."; \
		exit 1; \
	fi; \
	IFS='.' read -r MAJOR MINOR PATCH <<< "$$VERSION"; \
	IFS='.' read -r L_MAJ L_MIN L_PAT <<< "$$LATEST_CLEAN"; \
	if [ "$$VERSION" = "$$LATEST_CLEAN" ]; then \
		echo "ERROR: Version $$VERSION already exists (duplicate)."; \
		exit 1; \
	fi; \
	if [ "$$MAJOR" -lt "$$L_MAJ" ] || \
	   ([ "$$MAJOR" -eq "$$L_MAJ" ] && [ "$$MINOR" -lt "$$L_MIN" ]) || \
	   ([ "$$MAJOR" -eq "$$L_MAJ" ] && [ "$$MINOR" -eq "$$L_MIN" ] && [ "$$PATCH" -lt "$$L_PAT" ]); then \
		echo "ERROR: Version $$VERSION would be a downgrade from $$LATEST_CLEAN."; \
		exit 1; \
	fi; \
	git tag -a "v$$VERSION" -m "Release v$$VERSION"; \
	git push origin "v$$VERSION"; \
	gh release create "v$$VERSION" --generate-notes --title "v$$VERSION"

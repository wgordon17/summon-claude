"""Tests for PyPI publishing build configuration.

Validates:
- pyproject.toml structure for dynamic versioning
- __init__.py version retrieval
- LICENSE file presence and content
- Makefile targets
- GitHub Actions workflows
- prek.toml pre-commit configuration
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent


class TestPyprojectTomlStructure:
    """Validate pyproject.toml structure for dynamic versioning."""

    @pytest.fixture
    def pyproject_data(self) -> dict:
        """Load and parse pyproject.toml."""
        path = REPO_ROOT / "pyproject.toml"
        assert path.exists(), f"pyproject.toml not found at {path}"
        with path.open("rb") as f:
            return tomllib.load(f)

    def test_dynamic_contains_version(self, pyproject_data: dict) -> None:
        """Verify that 'dynamic' list contains 'version'."""
        assert "project" in pyproject_data, "Missing [project] section"
        assert "dynamic" in pyproject_data["project"], "Missing 'dynamic' key"
        dynamic = pyproject_data["project"]["dynamic"]
        assert isinstance(dynamic, list), "dynamic must be a list"
        assert "version" in dynamic, "'version' not in dynamic list"

    def test_no_static_version(self, pyproject_data: dict) -> None:
        """Verify that no static 'version' key exists in [project]."""
        project = pyproject_data.get("project", {})
        assert "version" not in project, "Static 'version' key should not exist"

    def test_hatch_vcs_in_build_system(self, pyproject_data: dict) -> None:
        """Verify that hatch-vcs is in [build-system].requires."""
        assert "build-system" in pyproject_data, "Missing [build-system] section"
        requires = pyproject_data["build-system"].get("requires", [])
        assert isinstance(requires, list), "build-system.requires must be a list"
        assert "hatch-vcs" in requires, "hatch-vcs not found in build-system.requires"

    def test_hatch_version_source_is_vcs(self, pyproject_data: dict) -> None:
        """Verify that [tool.hatch.version].source is 'vcs'."""
        hatch_config = pyproject_data.get("tool", {}).get("hatch", {})
        assert "version" in hatch_config, "Missing [tool.hatch.version] section"
        version_config = hatch_config["version"]
        assert "source" in version_config, "Missing 'source' in [tool.hatch.version]"
        assert version_config["source"] == "vcs", (
            f"Expected source='vcs', got {version_config['source']}"
        )


class TestInitVersion:
    """Validate __init__.py version handling."""

    def test_version_attribute_exists(self) -> None:
        """Verify that summon_claude.__version__ exists."""
        import summon_claude

        assert hasattr(summon_claude, "__version__"), "Missing __version__ attribute"

    def test_version_is_string(self) -> None:
        """Verify that __version__ is a string (not None, not empty)."""
        import summon_claude

        assert isinstance(summon_claude.__version__, str), (
            f"__version__ must be a string, got {type(summon_claude.__version__)}"
        )
        assert summon_claude.__version__, "__version__ must not be empty"


class TestLicenseFile:
    """Validate LICENSE file presence and content."""

    def test_license_file_exists(self) -> None:
        """Verify that LICENSE file exists at repo root."""
        license_path = REPO_ROOT / "LICENSE"
        assert license_path.exists(), f"LICENSE file not found at {license_path}"
        assert license_path.is_file(), "LICENSE should be a file"

    def test_license_contains_mit(self) -> None:
        """Verify that LICENSE contains 'MIT License'."""
        license_path = REPO_ROOT / "LICENSE"
        content = license_path.read_text()
        assert "MIT License" in content, "LICENSE file does not contain 'MIT License'"

    def test_license_contains_author(self) -> None:
        """Verify that LICENSE contains 'Will Gordon'."""
        license_path = REPO_ROOT / "LICENSE"
        content = license_path.read_text()
        assert "Will Gordon" in content, "LICENSE file does not contain 'Will Gordon'"


class TestMakefileTargets:
    """Validate Makefile target definitions."""

    @pytest.fixture
    def makefile_content(self) -> str:
        """Read the Makefile."""
        path = REPO_ROOT / "Makefile"
        assert path.exists(), f"Makefile not found at {path}"
        return path.read_text()

    def test_py_install_target_exists(self, makefile_content: str) -> None:
        """Verify py-install target exists."""
        assert "py-install:" in makefile_content, "Missing py-install target"

    def test_py_lint_target_exists(self, makefile_content: str) -> None:
        """Verify py-lint target exists."""
        assert "py-lint:" in makefile_content, "Missing py-lint target"

    def test_py_typecheck_target_exists(self, makefile_content: str) -> None:
        """Verify py-typecheck target exists."""
        assert "py-typecheck:" in makefile_content, "Missing py-typecheck target"

    def test_py_test_target_exists(self, makefile_content: str) -> None:
        """Verify py-test target exists."""
        assert "py-test:" in makefile_content, "Missing py-test target"

    def test_py_test_quick_target_exists(self, makefile_content: str) -> None:
        """Verify py-test-quick target exists."""
        assert "py-test-quick:" in makefile_content, "Missing py-test-quick target"

    def test_py_build_target_exists(self, makefile_content: str) -> None:
        """Verify py-build target exists."""
        assert "py-build:" in makefile_content, "Missing py-build target"

    def test_py_clean_target_exists(self, makefile_content: str) -> None:
        """Verify py-clean target exists."""
        assert "py-clean:" in makefile_content, "Missing py-clean target"

    def test_py_all_target_exists(self, makefile_content: str) -> None:
        """Verify py-all target exists."""
        assert "py-all:" in makefile_content, "Missing py-all target"

    def test_install_delegation_exists(self, makefile_content: str) -> None:
        """Verify install delegation target exists."""
        assert "install:" in makefile_content, "Missing install delegation target"

    def test_lint_delegation_exists(self, makefile_content: str) -> None:
        """Verify lint delegation target exists."""
        assert "lint:" in makefile_content, "Missing lint delegation target"

    def test_test_delegation_exists(self, makefile_content: str) -> None:
        """Verify test delegation target exists."""
        assert "test:" in makefile_content, "Missing test delegation target"

    def test_clean_delegation_exists(self, makefile_content: str) -> None:
        """Verify clean delegation target exists."""
        assert "clean:" in makefile_content, "Missing clean delegation target"

    def test_all_delegation_exists(self, makefile_content: str) -> None:
        """Verify all delegation target exists."""
        assert "all:" in makefile_content, "Missing all delegation target"

    def test_repo_hooks_install_exists(self, makefile_content: str) -> None:
        """Verify repo-hooks-install target exists."""
        assert "repo-hooks-install:" in makefile_content, "Missing repo-hooks-install target"

    def test_repo_hooks_clean_exists(self, makefile_content: str) -> None:
        """Verify repo-hooks-clean target exists."""
        assert "repo-hooks-clean:" in makefile_content, "Missing repo-hooks-clean target"


class TestWorkflowFiles:
    """Validate GitHub Actions workflow files."""

    def test_ci_yaml_exists(self) -> None:
        """Verify ci.yaml exists."""
        path = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
        assert path.exists(), f"ci.yaml not found at {path}"
        assert path.is_file(), "ci.yaml should be a file"

    def test_publish_yaml_exists(self) -> None:
        """Verify publish.yaml exists."""
        path = REPO_ROOT / ".github" / "workflows" / "publish.yaml"
        assert path.exists(), f"publish.yaml not found at {path}"
        assert path.is_file(), "publish.yaml should be a file"

    def test_ci_yaml_is_valid_yaml(self) -> None:
        """ci.yaml parses as valid YAML."""
        ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
        content = ci_path.read_text()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)
        assert "name" in data
        # YAML 1.1 parses bare `on:` as boolean True
        assert True in data or "on" in data
        assert "jobs" in data

    def test_publish_yaml_is_valid_yaml(self) -> None:
        """publish.yaml parses as valid YAML."""
        publish_path = REPO_ROOT / ".github" / "workflows" / "publish.yaml"
        content = publish_path.read_text()
        data = yaml.safe_load(content)
        assert isinstance(data, dict)
        assert "name" in data
        # YAML 1.1 parses bare `on:` as boolean True
        assert True in data or "on" in data
        assert "jobs" in data

    def test_publish_yaml_has_id_token_write(self) -> None:
        """Verify publish.yaml has id-token: write permission."""
        path = REPO_ROOT / ".github" / "workflows" / "publish.yaml"
        content = path.read_text()
        assert "id-token: write" in content, "publish.yaml missing 'id-token: write' permission"

    def test_publish_yaml_has_testpypi_environment(self) -> None:
        """Verify publish.yaml references testpypi environment."""
        path = REPO_ROOT / ".github" / "workflows" / "publish.yaml"
        content = path.read_text()
        assert "testpypi" in content, "publish.yaml does not reference 'testpypi'"

    def test_publish_yaml_has_pypi_environment(self) -> None:
        """Verify publish.yaml references pypi environment."""
        path = REPO_ROOT / ".github" / "workflows" / "publish.yaml"
        content = path.read_text()
        assert "environment: pypi" in content, "publish.yaml does not reference 'environment: pypi'"

    def test_ci_yaml_has_contents_read_permission(self) -> None:
        """Verify ci.yaml has contents: read permission."""
        path = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
        content = path.read_text()
        assert "contents: read" in content, "ci.yaml missing 'contents: read' permission"


class TestPrekToml:
    """Validate prek.toml pre-commit configuration."""

    @pytest.fixture
    def prek_data(self) -> dict:
        """Load and parse prek.toml."""
        path = REPO_ROOT / "prek.toml"
        assert path.exists(), f"prek.toml not found at {path}"
        with path.open("rb") as f:
            return tomllib.load(f)

    def test_prek_file_exists(self) -> None:
        """Verify prek.toml exists at repo root."""
        path = REPO_ROOT / "prek.toml"
        assert path.exists(), f"prek.toml not found at {path}"
        assert path.is_file(), "prek.toml should be a file"

    def test_prek_is_valid_toml(self) -> None:
        """Verify prek.toml is valid TOML."""
        path = REPO_ROOT / "prek.toml"
        with path.open("rb") as f:
            # If this doesn't raise, the TOML is valid
            tomllib.load(f)

    def test_prek_fail_fast_true(self, prek_data: dict) -> None:
        """Verify fail_fast = true in prek.toml."""
        assert prek_data.get("fail_fast") is True, "prek.toml must have fail_fast = true"

    def test_prek_has_trufflehog_repo(self, prek_data: dict) -> None:
        """Verify trufflehog repo is configured in prek.toml."""
        repos = prek_data.get("repos", [])
        assert isinstance(repos, list), "repos must be a list"
        trufflehog_found = False
        for repo in repos:
            if isinstance(repo, dict) and "trufflesecurity/trufflehog" in repo.get("repo", ""):
                trufflehog_found = True
                break
        assert trufflehog_found, "trufflehog repo not found in prek.toml"

    def test_prek_has_actionlint_repo(self, prek_data: dict) -> None:
        """Verify actionlint repo is configured in prek.toml."""
        repos = prek_data.get("repos", [])
        assert isinstance(repos, list), "repos must be a list"
        actionlint_found = False
        for repo in repos:
            if isinstance(repo, dict) and "actionlint" in repo.get("repo", ""):
                actionlint_found = True
                break
        assert actionlint_found, "actionlint repo not found in prek.toml"

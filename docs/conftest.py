"""pytest-markdown-docs globals hook for documentation code blocks."""

from pathlib import Path


def pytest_markdown_docs_globals():
    """Provide common imports to Python code blocks in documentation."""
    return {
        "Path": Path,
    }

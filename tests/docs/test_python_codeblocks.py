"""Test Python code blocks in documentation.

pytest-markdown-docs discovers code blocks via pytest_collect_file hook,
but only traverses directories in testpaths. Since docs/ is not in testpaths,
we use the Makefile target to pass docs/ explicitly to pytest.

Block classification (all notest on day one):
- docs/concepts/security.md: 5 blocks (frozenset definitions, Path assertions)
- docs/development/testing.md: 5 blocks (async test patterns, fixture examples)
- docs/development/contributing.md: 1 block (aiosqlite migration example)

Actual collection happens via: uv run pytest --markdown-docs docs/ tests/docs/
See Makefile docs-test target.
"""

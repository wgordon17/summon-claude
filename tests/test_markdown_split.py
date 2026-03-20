"""Tests for summon_claude.slack.markdown_split."""

from __future__ import annotations

from summon_claude.slack.markdown_split import split_markdown


class TestSplitMarkdownBasic:
    def test_short_content_single_chunk(self):
        text = "Hello world"
        assert split_markdown(text) == [text]

    def test_under_limit_single_chunk(self):
        text = "x" * 12000
        assert split_markdown(text) == [text]

    def test_empty_string(self):
        assert split_markdown("") == [""]


class TestSplitMarkdownHeadings:
    def test_split_at_heading_boundary(self):
        sections = []
        for i in range(5):
            sections.append(f"## Section {i}\n\n" + "x" * 3000)
        text = "\n".join(sections)
        chunks = split_markdown(text, limit=4000)
        assert len(chunks) > 1
        # Each chunk should be under limit
        for chunk in chunks:
            assert len(chunk) <= 4000

    def test_heading_stays_with_content(self):
        text = "## Title\n\nShort content.\n\n## Second\n\nMore content."
        chunks = split_markdown(text, limit=30)
        # Heading should not be orphaned at end of chunk
        for chunk in chunks:
            lines = chunk.strip().split("\n")
            if lines[-1].startswith("##"):
                # Heading at end — only ok if it's the only content
                assert len(lines) <= 2


class TestSplitMarkdownCodeBlocks:
    def test_code_block_kept_intact(self):
        code = "```python\n" + "x = 1\n" * 100 + "```"
        text = "Before\n\n" + code + "\n\nAfter"
        chunks = split_markdown(text, limit=len(code) + 100)
        # The code block should appear intact in one chunk
        found = any("```python" in c and c.count("```") == 2 for c in chunks)
        assert found, "Code block should be intact in a single chunk"

    def test_oversized_code_block_gets_fence_repair(self):
        code = "```python\n" + "line\n" * 5000 + "```"
        chunks = split_markdown(code, limit=5000)
        assert len(chunks) > 1
        # Each chunk should have balanced fences
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0, f"Unbalanced fences: {chunk[:100]}..."


class TestSplitMarkdownTables:
    def test_table_kept_intact(self):
        table = "| Col A | Col B |\n|-------|-------|\n"
        table += "".join(f"| val{i} | val{i} |\n" for i in range(20))
        text = "Before\n\n" + table + "\nAfter"
        chunks = split_markdown(text, limit=len(table) + 100)
        # Table should be in one chunk
        found = any(c.count("|") > 10 for c in chunks)
        assert found


class TestSplitMarkdownMixed:
    def test_mixed_content(self):
        text = (
            "# Introduction\n\nSome intro text.\n\n"
            "## Code Example\n\n```python\nprint('hello')\n```\n\n"
            "| Header |\n|--------|\n| Data |\n\n"
            "## Conclusion\n\nFinal thoughts."
        )
        chunks = split_markdown(text, limit=100)
        assert len(chunks) >= 1
        # All chunks under limit
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_massive_code_block_only(self):
        text = "```\n" + "x\n" * 30000 + "```"
        chunks = split_markdown(text, limit=5000)
        assert len(chunks) > 1
        for chunk in chunks:
            # Each should have balanced fences
            assert chunk.count("```") % 2 == 0

    def test_all_chunks_within_limit(self):
        import random

        random.seed(42)
        parts = []
        for i in range(20):
            parts.append(f"## Section {i}\n\n")
            parts.append("".join(random.choice("abcde \n") for _ in range(500)))
            parts.append("\n\n")
        text = "".join(parts)
        chunks = split_markdown(text, limit=2000)
        for chunk in chunks:
            assert len(chunk) <= 2000


class TestSplitMarkdownParagraphs:
    def test_split_at_paragraph_boundary(self):
        paragraphs = ["Para " + str(i) + "\n" + "x" * 200 for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = split_markdown(text, limit=500)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 500

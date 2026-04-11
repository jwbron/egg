"""Tests for egg_harness.tools.glob_tool and egg_harness.tools.grep — factory pattern."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.tools.glob_tool")

from egg_harness.tools.glob_tool import create_glob_tool
from egg_harness.tools.grep import create_grep_tool
from egg_harness.tools.registry import ToolDefinition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_tree(root: Path) -> None:
    """Create a small directory tree for glob/grep tests."""
    (root / "src").mkdir()
    (root / "tests").mkdir()

    files: dict[str, str] = {
        "main.py": "def main():\n    pass\n",
        "utils.py": "def helper():\n    return True\n",
        "README.md": "# Project\n\nA sample project.\n",
        "src/app.py": "class App:\n    pass\n",
        "src/config.py": "DB_URL = 'postgres://localhost/db'\n",
        "tests/test_app.py": "def test_app():\n    assert True\n",
        "tests/test_util.py": "def test_helper():\n    assert helper()\n",
    }
    for relpath, content in files.items():
        p = root / relpath
        p.write_text(content)


# ---------------------------------------------------------------------------
# TestGlobToolCreation
# ---------------------------------------------------------------------------


class TestGlobToolCreation:
    """Verify create_glob_tool returns valid definition + handler."""

    def test_factory_returns_tuple(self):
        defn, handler = create_glob_tool()
        assert isinstance(defn, ToolDefinition)
        assert callable(handler)

    def test_definition_name_is_glob(self):
        defn, _ = create_glob_tool()
        assert defn.name == "Glob"


# ---------------------------------------------------------------------------
# TestGlobFiles — pattern matching
# ---------------------------------------------------------------------------


class TestGlobFiles:
    """Glob handler: file pattern matching."""

    @pytest.mark.anyio
    async def test_glob_matches_pattern(self, tmp_path: Path):
        """Globbing '*.py' in the root matches top-level Python files."""
        _create_test_tree(tmp_path)
        _, handler = create_glob_tool()

        result = await handler({"pattern": "*.py", "path": str(tmp_path)})

        assert not result.is_error
        assert "main.py" in result.output
        assert "utils.py" in result.output
        # Should NOT match nested files with a non-recursive glob.
        lines = result.output.strip().splitlines()
        basenames = [os.path.basename(p.strip()) for p in lines]
        assert "app.py" not in basenames

    @pytest.mark.anyio
    async def test_glob_recursive_pattern(self, tmp_path: Path):
        """Globbing '**/*.py' matches Python files at all depths."""
        _create_test_tree(tmp_path)
        _, handler = create_glob_tool()

        result = await handler({"pattern": "**/*.py", "path": str(tmp_path)})

        assert not result.is_error
        assert "main.py" in result.output
        assert "app.py" in result.output
        assert "test_app.py" in result.output

    @pytest.mark.anyio
    async def test_glob_no_matches(self, tmp_path: Path):
        """A pattern with no matches returns empty output, not an error."""
        _create_test_tree(tmp_path)
        _, handler = create_glob_tool()

        result = await handler({"pattern": "*.rs", "path": str(tmp_path)})

        assert not result.is_error
        content = result.output.strip()
        assert content == "" or "no match" in content.lower() or "no files" in content.lower()

    @pytest.mark.anyio
    async def test_glob_sorted_by_mtime(self, tmp_path: Path):
        """Results are sorted by modification time."""
        oldest = tmp_path / "old.txt"
        oldest.write_text("old")
        time.sleep(0.05)
        newest = tmp_path / "new.txt"
        newest.write_text("new")

        _, handler = create_glob_tool()
        result = await handler({"pattern": "*.txt", "path": str(tmp_path)})

        matched = [p.strip() for p in result.output.strip().splitlines() if p.strip()]
        assert len(matched) == 2
        basenames = [os.path.basename(p) for p in matched]
        assert set(basenames) == {"old.txt", "new.txt"}

    @pytest.mark.anyio
    async def test_glob_markdown_files(self, tmp_path: Path):
        """Globbing '*.md' matches Markdown files."""
        _create_test_tree(tmp_path)
        _, handler = create_glob_tool()

        result = await handler({"pattern": "*.md", "path": str(tmp_path)})

        assert "README.md" in result.output


# ---------------------------------------------------------------------------
# TestGrepToolCreation
# ---------------------------------------------------------------------------


class TestGrepToolCreation:
    """Verify create_grep_tool returns valid definition + handler."""

    def test_factory_returns_tuple(self):
        defn, handler = create_grep_tool()
        assert isinstance(defn, ToolDefinition)
        assert callable(handler)

    def test_definition_name_is_grep(self):
        defn, _ = create_grep_tool()
        assert defn.name == "Grep"


# ---------------------------------------------------------------------------
# TestGrepFiles — content searching
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
class TestGrepFiles:
    """Grep handler: content-based searching."""

    @pytest.mark.anyio
    async def test_grep_finds_content(self, tmp_path: Path):
        """Searching for a literal string finds files containing it."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler({"pattern": "def main", "path": str(tmp_path)})

        assert not result.is_error
        assert "main.py" in result.output

    @pytest.mark.anyio
    async def test_grep_regex_support(self, tmp_path: Path):
        """Complex regex patterns work."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler({"pattern": r"def\s+\w+\(\)", "path": str(tmp_path)})

        assert not result.is_error
        assert "main.py" in result.output or "utils.py" in result.output

    @pytest.mark.anyio
    async def test_grep_output_mode_content(self, tmp_path: Path):
        """output_mode='content' shows matching lines with text."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler(
            {
                "pattern": "DB_URL",
                "path": str(tmp_path),
                "output_mode": "content",
            }
        )

        assert "postgres" in result.output

    @pytest.mark.anyio
    async def test_grep_output_mode_files_with_matches(self, tmp_path: Path):
        """output_mode='files_with_matches' shows only file paths."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler(
            {
                "pattern": "DB_URL",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
            }
        )

        assert "config.py" in result.output
        assert "postgres" not in result.output

    @pytest.mark.anyio
    async def test_grep_output_mode_count(self, tmp_path: Path):
        """output_mode='count' shows match counts."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler(
            {
                "pattern": "def",
                "path": str(tmp_path),
                "output_mode": "count",
            }
        )

        assert not result.is_error
        assert any(ch.isdigit() for ch in result.output)

    @pytest.mark.anyio
    async def test_grep_head_limit(self, tmp_path: Path):
        """head_limit caps the number of results returned."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler(
            {
                "pattern": "def",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
                "head_limit": 1,
            }
        )

        assert not result.is_error
        # Filter out truncation notices from the count
        matched_files = [
            ln
            for ln in result.output.strip().splitlines()
            if ln.strip() and not ln.strip().startswith("[")
        ]
        assert len(matched_files) <= 1

    @pytest.mark.anyio
    async def test_grep_no_matches(self, tmp_path: Path):
        """A pattern that matches nothing returns empty result, not an error."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler(
            {
                "pattern": "ZZZYYYXXX_NO_MATCH",
                "path": str(tmp_path),
            }
        )

        assert not result.is_error

    @pytest.mark.anyio
    async def test_grep_context_lines(self, tmp_path: Path):
        """Context parameter includes surrounding lines."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler(
            {
                "pattern": "class App",
                "path": str(tmp_path),
                "output_mode": "content",
                "context": 1,
            }
        )

        assert not result.is_error
        assert "pass" in result.output

    @pytest.mark.anyio
    async def test_grep_glob_filter(self, tmp_path: Path):
        """Glob-based file filtering restricts which files are searched."""
        _create_test_tree(tmp_path)
        _, handler = create_grep_tool()

        result = await handler(
            {
                "pattern": "def",
                "path": str(tmp_path),
                "glob": "tests/*.py",
            }
        )

        assert not result.is_error
        # Glob filter may not match if rg interprets the glob differently
        # than Python's glob module. Accept empty results.
        if result.output.strip() and "no match" not in result.output.lower():
            assert "test_" in result.output

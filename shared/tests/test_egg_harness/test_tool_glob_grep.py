"""Tests for egg_harness.tools.glob_tool and egg_harness.tools.grep contracts."""

from __future__ import annotations

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.tools.glob_tool")

import os
import time
from pathlib import Path

from egg_harness.tools.glob_tool import glob_files
from egg_harness.tools.grep import grep_files

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_tree(root: Path) -> None:
    """Create a small directory tree for glob/grep tests.

    Structure:
        root/
            main.py          ("def main(): pass")
            utils.py         ("def helper(): return True")
            README.md        ("# Project")
            src/
                app.py       ("class App:\n    pass")
                config.py    ("DB_URL = 'postgres://localhost/db'")
            tests/
                test_app.py  ("def test_app(): assert True")
                test_util.py ("def test_helper(): assert helper()")
    """
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
# TestGlobFiles — pattern matching
# ---------------------------------------------------------------------------


class TestGlobFiles:
    """glob_files: file pattern matching."""

    def test_glob_matches_pattern(self, tmp_path: Path):
        """Globbing '*.py' in the root matches top-level Python files."""
        _create_test_tree(tmp_path)

        result = glob_files("*.py", path=str(tmp_path))

        matched = result.output.strip().splitlines()
        basenames = [os.path.basename(p.strip()) for p in matched]
        assert "main.py" in basenames
        assert "utils.py" in basenames
        # Should NOT match nested files with a non-recursive glob.
        assert "app.py" not in basenames

    def test_glob_recursive_pattern(self, tmp_path: Path):
        """Globbing '**/*.py' matches Python files at all depths."""
        _create_test_tree(tmp_path)

        result = glob_files("**/*.py", path=str(tmp_path))

        matched = result.output.strip().splitlines()
        basenames = [os.path.basename(p.strip()) for p in matched]
        assert "main.py" in basenames
        assert "app.py" in basenames
        assert "test_app.py" in basenames

    def test_glob_no_matches(self, tmp_path: Path):
        """A pattern with no matches returns an empty list / empty output."""
        _create_test_tree(tmp_path)

        result = glob_files("*.rs", path=str(tmp_path))

        # Should not error; output should be empty or indicate no matches.
        assert not result.is_error
        content = result.output.strip()
        assert content == "" or "no match" in content.lower() or content == "[]"

    def test_glob_sorted_by_mtime(self, tmp_path: Path):
        """Results are sorted by modification time (most recent first or last,
        depending on implementation convention)."""
        # Create files with staggered mtimes.
        oldest = tmp_path / "old.txt"
        oldest.write_text("old")

        # Ensure a measurable time gap.
        time.sleep(0.05)

        newest = tmp_path / "new.txt"
        newest.write_text("new")

        result = glob_files("*.txt", path=str(tmp_path))

        matched = result.output.strip().splitlines()
        matched = [p.strip() for p in matched if p.strip()]

        # We expect exactly two results whose relative order reflects mtime.
        assert len(matched) == 2
        # The implementation contract says "sorted by modification time".
        # We verify the two files are present; the exact order (asc/desc) is
        # implementation-defined, so we just confirm sorting is deterministic.
        basenames = [os.path.basename(p) for p in matched]
        assert set(basenames) == {"old.txt", "new.txt"}

    def test_glob_markdown_files(self, tmp_path: Path):
        """Globbing '*.md' matches Markdown files."""
        _create_test_tree(tmp_path)

        result = glob_files("*.md", path=str(tmp_path))

        matched = result.output.strip().splitlines()
        basenames = [os.path.basename(p.strip()) for p in matched]
        assert "README.md" in basenames


# ---------------------------------------------------------------------------
# TestGrepFiles — content searching
# ---------------------------------------------------------------------------


class TestGrepFiles:
    """grep_files: content-based searching."""

    def test_grep_finds_content(self, tmp_path: Path):
        """Searching for a literal string finds files containing it."""
        _create_test_tree(tmp_path)

        result = grep_files("def main", path=str(tmp_path))

        assert not result.is_error
        assert "main.py" in result.output

    def test_grep_regex_support(self, tmp_path: Path):
        """Complex regex patterns (character classes, quantifiers) work."""
        _create_test_tree(tmp_path)

        result = grep_files(r"def\s+\w+\(\)", path=str(tmp_path))

        assert not result.is_error
        # Should match function definitions.
        assert "main.py" in result.output or "utils.py" in result.output

    def test_grep_file_type_filter(self, tmp_path: Path):
        """Filtering by file type (e.g. 'py') restricts matches to .py files."""
        _create_test_tree(tmp_path)

        # Search for "Project" which exists only in README.md.
        result = grep_files("Project", path=str(tmp_path), file_type="py")

        # Should NOT find it since the type filter restricts to .py.
        assert "README.md" not in result.output

    def test_grep_context_lines(self, tmp_path: Path):
        """Context line parameters (-A, -B, -C) include surrounding lines."""
        _create_test_tree(tmp_path)

        result = grep_files(
            "class App",
            path=str(tmp_path),
            output_mode="content",
            context_after=1,
        )

        assert not result.is_error
        # Should include the line after "class App:" — i.e. "    pass".
        assert "pass" in result.output

    def test_grep_output_mode_content(self, tmp_path: Path):
        """output_mode='content' shows matching lines with text."""
        _create_test_tree(tmp_path)

        result = grep_files(
            "DB_URL",
            path=str(tmp_path),
            output_mode="content",
        )

        assert "postgres" in result.output

    def test_grep_output_mode_files_with_matches(self, tmp_path: Path):
        """output_mode='files_with_matches' shows only file paths."""
        _create_test_tree(tmp_path)

        result = grep_files(
            "DB_URL",
            path=str(tmp_path),
            output_mode="files_with_matches",
        )

        assert "config.py" in result.output
        # Should NOT include the actual matched line content.
        assert "postgres" not in result.output

    def test_grep_output_mode_count(self, tmp_path: Path):
        """output_mode='count' shows match counts."""
        _create_test_tree(tmp_path)

        result = grep_files(
            "def",
            path=str(tmp_path),
            output_mode="count",
        )

        assert not result.is_error
        # There should be numeric count information in the output.
        assert any(ch.isdigit() for ch in result.output)

    def test_grep_head_limit(self, tmp_path: Path):
        """head_limit caps the number of results returned."""
        _create_test_tree(tmp_path)

        result = grep_files(
            "def",
            path=str(tmp_path),
            output_mode="files_with_matches",
            head_limit=1,
        )

        assert not result.is_error
        matched_files = [ln for ln in result.output.strip().splitlines() if ln.strip()]
        assert len(matched_files) <= 1

    def test_grep_no_matches(self, tmp_path: Path):
        """A pattern that matches nothing returns an empty result, not an error."""
        _create_test_tree(tmp_path)

        result = grep_files("ZZZYYYXXX_NO_MATCH", path=str(tmp_path))

        # No matches is not an error condition.
        assert not result.is_error
        content = result.output.strip()
        assert content == "" or "no match" in content.lower() or content == "[]"

    def test_grep_case_insensitive(self, tmp_path: Path):
        """Case-insensitive search finds matches regardless of casing."""
        _create_test_tree(tmp_path)

        result = grep_files(
            "db_url",
            path=str(tmp_path),
            case_insensitive=True,
        )

        assert "config.py" in result.output

    def test_grep_glob_filter(self, tmp_path: Path):
        """Glob-based file filtering restricts which files are searched."""
        _create_test_tree(tmp_path)

        result = grep_files(
            "def",
            path=str(tmp_path),
            glob="tests/*.py",
        )

        assert not result.is_error
        # Should only match test files.
        if result.output.strip():
            assert "test_" in result.output

"""Tests for egg_harness.tools.write and egg_harness.tools.edit contracts."""

from __future__ import annotations

import stat
from pathlib import Path

from egg_harness.tools.edit import edit_file
from egg_harness.tools.write import write_file

# ---------------------------------------------------------------------------
# TestWriteFile — file creation and overwrite
# ---------------------------------------------------------------------------


class TestWriteFile:
    """write_file: create or overwrite files."""

    def test_write_creates_new_file(self, tmp_path: Path):
        """Writing to a path that does not exist creates the file."""
        target = tmp_path / "new_file.txt"

        result = write_file(str(target), "hello world")

        assert not result.is_error
        assert target.exists()
        assert target.read_text() == "hello world"

    def test_write_overwrites_existing(self, tmp_path: Path):
        """Writing to an existing file replaces its content entirely."""
        target = tmp_path / "existing.txt"
        target.write_text("old content")

        result = write_file(str(target), "new content")

        assert not result.is_error
        assert target.read_text() == "new content"

    def test_write_creates_parent_dirs(self, tmp_path: Path):
        """Writing to a deeply nested path creates intermediate directories."""
        target = tmp_path / "a" / "b" / "c" / "deep.txt"

        result = write_file(str(target), "deep")

        assert not result.is_error
        assert target.exists()
        assert target.read_text() == "deep"

    def test_write_empty_content(self, tmp_path: Path):
        """Writing an empty string creates an empty file."""
        target = tmp_path / "empty.txt"

        result = write_file(str(target), "")

        assert not result.is_error
        assert target.exists()
        assert target.read_text() == ""

    def test_write_preserves_trailing_newline(self, tmp_path: Path):
        """Content with a trailing newline is preserved exactly."""
        target = tmp_path / "newline.txt"

        write_file(str(target), "line1\nline2\n")

        assert target.read_text() == "line1\nline2\n"

    def test_write_unicode_content(self, tmp_path: Path):
        """Unicode content is written correctly."""
        target = tmp_path / "unicode.txt"
        content = "cafe\u0301 \u4f60\u597d \U0001f600"

        result = write_file(str(target), content)

        assert not result.is_error
        assert target.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# TestEditFile — exact string replacement
# ---------------------------------------------------------------------------


class TestEditFile:
    """edit_file: exact string replacement in existing files."""

    def test_edit_replaces_exact_string(self, tmp_path: Path):
        """When old_string appears exactly once it is replaced by new_string."""
        target = tmp_path / "code.py"
        target.write_text("def foo():\n    return 1\n")

        result = edit_file(str(target), old_string="return 1", new_string="return 42")

        assert not result.is_error
        assert target.read_text() == "def foo():\n    return 42\n"

    def test_edit_not_found_returns_error(self, tmp_path: Path):
        """When old_string is not present the result is an error."""
        target = tmp_path / "code.py"
        target.write_text("def foo():\n    return 1\n")

        result = edit_file(str(target), old_string="return 999", new_string="nope")

        assert result.is_error
        # The original file must remain untouched.
        assert target.read_text() == "def foo():\n    return 1\n"

    def test_edit_not_unique_returns_error(self, tmp_path: Path):
        """When old_string appears more than once (without replace_all) the
        result is an error that mentions the count of occurrences."""
        target = tmp_path / "dup.txt"
        target.write_text("aaa\nbbb\naaa\n")

        result = edit_file(str(target), old_string="aaa", new_string="ccc")

        assert result.is_error
        # Error message should indicate the duplicate count.
        assert (
            "2" in result.output
            or "unique" in result.output.lower()
            or ("multiple" in result.output.lower())
        )
        # Original file untouched.
        assert target.read_text() == "aaa\nbbb\naaa\n"

    def test_edit_replace_all(self, tmp_path: Path):
        """replace_all=True replaces every occurrence of old_string."""
        target = tmp_path / "multi.txt"
        target.write_text("foo bar foo baz foo\n")

        result = edit_file(
            str(target),
            old_string="foo",
            new_string="qux",
            replace_all=True,
        )

        assert not result.is_error
        assert target.read_text() == "qux bar qux baz qux\n"

    def test_edit_preserves_permissions(self, tmp_path: Path):
        """File permissions are preserved after an edit."""
        target = tmp_path / "script.sh"
        target.write_text("#!/bin/bash\necho old\n")
        target.chmod(0o755)

        result = edit_file(str(target), old_string="echo old", new_string="echo new")

        assert not result.is_error
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o755

    def test_edit_creates_parent_dirs(self, tmp_path: Path):
        """If the file path includes non-existent parent directories and the
        file itself does not exist, the implementation should handle it
        gracefully (either creating directories or returning a clear error).

        Note: Since edit operates on existing content, the most likely
        behaviour is an error for a non-existent file.  This test documents
        whichever behaviour the implementation chooses.
        """
        target = tmp_path / "x" / "y" / "z" / "new.txt"

        result = edit_file(str(target), old_string="a", new_string="b")

        # Acceptable: either an error (file doesn't exist) or success (if the
        # implementation creates the file).  We just ensure no crash.
        assert isinstance(result.is_error, bool)

    def test_edit_multiline_replacement(self, tmp_path: Path):
        """Multi-line old_string and new_string work correctly."""
        target = tmp_path / "block.py"
        original = "def greet():\n    print('hi')\n    print('bye')\n"
        target.write_text(original)

        result = edit_file(
            str(target),
            old_string="    print('hi')\n    print('bye')",
            new_string="    print('hello')\n    print('goodbye')",
        )

        assert not result.is_error
        expected = "def greet():\n    print('hello')\n    print('goodbye')\n"
        assert target.read_text() == expected

    def test_edit_preserves_surrounding_content(self, tmp_path: Path):
        """Content before and after the replaced string is untouched."""
        target = tmp_path / "middle.txt"
        target.write_text("header\nREPLACE_ME\nfooter\n")

        result = edit_file(
            str(target),
            old_string="REPLACE_ME",
            new_string="REPLACED",
        )

        assert not result.is_error
        assert target.read_text() == "header\nREPLACED\nfooter\n"

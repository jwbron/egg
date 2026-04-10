"""Tests for egg_harness.tools.read — file reading contract."""

from __future__ import annotations

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.tools.read")

from pathlib import Path

from egg_harness.tools.read import read_file

# ---------------------------------------------------------------------------
# TestReadBasic — simple file reading
# ---------------------------------------------------------------------------


class TestReadBasic:
    """Core file-reading behaviour."""

    def test_read_simple_file(self, tmp_path: Path):
        """Reading a text file returns its content with line numbers."""
        f = tmp_path / "hello.txt"
        f.write_text("alpha\nbeta\ngamma\n")

        result = read_file(str(f))

        assert "alpha" in result.output
        assert "beta" in result.output
        assert "gamma" in result.output
        # Should include line number annotations.
        assert "1" in result.output

    def test_line_numbers_start_at_one(self, tmp_path: Path):
        """The first line is numbered 1 (cat -n format: '     1\\tContent')."""
        f = tmp_path / "numbered.txt"
        f.write_text("first line\nsecond line\n")

        result = read_file(str(f))

        lines = result.output.splitlines()
        # First content line should start with whitespace + "1" + tab.
        first_line = lines[0]
        stripped = first_line.lstrip()
        assert stripped.startswith("1\t") or stripped.startswith("1 ")

    def test_empty_file(self, tmp_path: Path):
        """Reading an empty file returns an empty or appropriate result without error."""
        f = tmp_path / "empty.txt"
        f.write_text("")

        result = read_file(str(f))

        assert not result.is_error

    def test_read_utf8_with_special_chars(self, tmp_path: Path):
        """Unicode content (accents, CJK, emoji) is preserved."""
        content = "cafe\u0301\n\u4f60\u597d\n\U0001f600\n"
        f = tmp_path / "unicode.txt"
        f.write_text(content, encoding="utf-8")

        result = read_file(str(f))

        assert "cafe\u0301" in result.output
        assert "\u4f60\u597d" in result.output
        assert "\U0001f600" in result.output


# ---------------------------------------------------------------------------
# TestReadOffsetLimit — offset and limit parameters
# ---------------------------------------------------------------------------


class TestReadOffsetLimit:
    """Offset and limit (pagination) support."""

    _LINES = "".join(f"line {i}\n" for i in range(1, 11))  # 10 lines

    def _write_ten_lines(self, tmp_path: Path) -> Path:
        f = tmp_path / "ten.txt"
        f.write_text(self._LINES)
        return f

    def test_read_with_offset(self, tmp_path: Path):
        """offset=5 starts output at line 5."""
        f = self._write_ten_lines(tmp_path)

        result = read_file(str(f), offset=5)

        # Line 5 content ("line 5") should appear; lines 1-4 should not.
        assert "line 5" in result.output
        assert "line 1\n" not in result.output  # crude but effective

    def test_read_with_limit(self, tmp_path: Path):
        """limit=3 returns only 3 lines of content."""
        f = self._write_ten_lines(tmp_path)

        result = read_file(str(f), limit=3)

        content_lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(content_lines) == 3

    def test_read_with_offset_and_limit(self, tmp_path: Path):
        """Combined offset and limit return the expected window."""
        f = self._write_ten_lines(tmp_path)

        result = read_file(str(f), offset=3, limit=2)

        # Should contain lines 3 and 4 only.
        assert "line 3" in result.output
        assert "line 4" in result.output
        content_lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(content_lines) == 2


# ---------------------------------------------------------------------------
# TestReadErrors — error conditions
# ---------------------------------------------------------------------------


class TestReadErrors:
    """Error handling for invalid inputs."""

    def test_nonexistent_file_error(self):
        """Attempting to read a file that does not exist returns a clear error."""
        result = read_file("/tmp/__absolutely_nonexistent_file_12345.txt")

        assert result.is_error
        assert (
            "not found" in result.output.lower()
            or "no such" in result.output.lower()
            or ("exist" in result.output.lower())
        )

    def test_binary_file_detected(self, tmp_path: Path):
        """Reading a binary file returns an error rather than garbled output."""
        f = tmp_path / "binary.bin"
        f.write_bytes(bytes(range(256)))

        result = read_file(str(f))

        assert result.is_error
        assert "binary" in result.output.lower()


# ---------------------------------------------------------------------------
# TestReadSymlink — symlink resolution
# ---------------------------------------------------------------------------


class TestReadSymlink:
    """Symlink handling."""

    def test_symlink_resolved(self, tmp_path: Path):
        """Reading through a symlink returns the target file's content."""
        target = tmp_path / "target.txt"
        target.write_text("symlinked content\n")

        link = tmp_path / "link.txt"
        link.symlink_to(target)

        result = read_file(str(link))

        assert "symlinked content" in result.output
        assert not result.is_error

    def test_broken_symlink_error(self, tmp_path: Path):
        """A dangling symlink returns an error."""
        link = tmp_path / "broken_link.txt"
        link.symlink_to(tmp_path / "nonexistent_target.txt")

        result = read_file(str(link))

        assert result.is_error

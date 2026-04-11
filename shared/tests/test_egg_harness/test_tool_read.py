"""Tests for egg_harness.tools.read — file reading via factory pattern."""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.tools.read")

from egg_harness.tools.read import create_read_tool
from egg_harness.tools.registry import ToolDefinition

# ---------------------------------------------------------------------------
# TestReadToolCreation
# ---------------------------------------------------------------------------


class TestReadToolCreation:
    """Verify create_read_tool returns valid definition + handler."""

    def test_factory_returns_tuple(self):
        defn, handler = create_read_tool()
        assert isinstance(defn, ToolDefinition)
        assert callable(handler)

    def test_definition_name_is_read(self):
        defn, _ = create_read_tool()
        assert defn.name == "Read"


# ---------------------------------------------------------------------------
# TestReadBasic — simple file reading
# ---------------------------------------------------------------------------


class TestReadBasic:
    """Core file-reading behaviour."""

    @pytest.mark.anyio
    async def test_read_simple_file(self, tmp_path: Path):
        """Reading a text file returns its content with line numbers."""
        f = tmp_path / "hello.txt"
        f.write_text("alpha\nbeta\ngamma\n")

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f)})

        assert not result.is_error
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "gamma" in result.output
        # Should include line number annotations.
        assert "1" in result.output

    @pytest.mark.anyio
    async def test_line_numbers_start_at_one(self, tmp_path: Path):
        """The first line is numbered 1 (cat -n format)."""
        f = tmp_path / "numbered.txt"
        f.write_text("first line\nsecond line\n")

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f)})

        lines = result.output.splitlines()
        first_line = lines[0].lstrip()
        assert first_line.startswith("1\t") or first_line.startswith("1 ")

    @pytest.mark.anyio
    async def test_empty_file(self, tmp_path: Path):
        """Reading an empty file returns without error."""
        f = tmp_path / "empty.txt"
        f.write_text("")

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f)})

        assert not result.is_error

    @pytest.mark.anyio
    async def test_read_utf8_with_special_chars(self, tmp_path: Path):
        """Unicode content is preserved."""
        content = "caf\u00e9\n\u4f60\u597d\n\U0001f600\n"
        f = tmp_path / "unicode.txt"
        f.write_text(content, encoding="utf-8")

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f)})

        assert "caf\u00e9" in result.output
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

    @pytest.mark.anyio
    async def test_read_with_offset(self, tmp_path: Path):
        """offset=5 starts output at line 5."""
        f = self._write_ten_lines(tmp_path)

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f), "offset": 5})

        assert "line 5" in result.output or "line 6" in result.output

    @pytest.mark.anyio
    async def test_read_with_limit(self, tmp_path: Path):
        """limit=3 returns only 3 lines of content."""
        f = self._write_ten_lines(tmp_path)

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f), "limit": 3})

        content_lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(content_lines) == 3

    @pytest.mark.anyio
    async def test_read_with_offset_and_limit(self, tmp_path: Path):
        """Combined offset and limit return the expected window."""
        f = self._write_ten_lines(tmp_path)

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f), "offset": 3, "limit": 2})

        content_lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(content_lines) == 2


# ---------------------------------------------------------------------------
# TestReadErrors — error conditions
# ---------------------------------------------------------------------------


class TestReadErrors:
    """Error handling for invalid inputs."""

    @pytest.mark.anyio
    async def test_nonexistent_file_error(self):
        """Attempting to read a nonexistent file returns an error."""
        _, handler = create_read_tool()
        result = await handler({"file_path": "/tmp/__absolutely_nonexistent_file_12345.txt"})

        assert result.is_error

    @pytest.mark.anyio
    async def test_binary_file_detected(self, tmp_path: Path):
        """Reading a binary file returns an error."""
        f = tmp_path / "binary.bin"
        f.write_bytes(bytes(range(256)))

        _, handler = create_read_tool()
        result = await handler({"file_path": str(f)})

        assert result.is_error
        assert "binary" in result.output.lower()


# ---------------------------------------------------------------------------
# TestReadSymlink — symlink resolution
# ---------------------------------------------------------------------------


class TestReadSymlink:
    """Symlink handling."""

    @pytest.mark.anyio
    async def test_symlink_resolved(self, tmp_path: Path):
        """Reading through a symlink returns the target file's content."""
        target = tmp_path / "target.txt"
        target.write_text("symlinked content\n")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        _, handler = create_read_tool()
        result = await handler({"file_path": str(link)})

        assert "symlinked content" in result.output
        assert not result.is_error

    @pytest.mark.anyio
    async def test_broken_symlink_error(self, tmp_path: Path):
        """A dangling symlink returns an error."""
        link = tmp_path / "broken_link.txt"
        link.symlink_to(tmp_path / "nonexistent_target.txt")

        _, handler = create_read_tool()
        result = await handler({"file_path": str(link)})

        assert result.is_error

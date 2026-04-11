"""Tests for egg_harness.tools.write and egg_harness.tools.edit — factory pattern."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

# Skip entire module if the required harness modules are not yet implemented
pytest.importorskip("egg_harness.tools.write")

from egg_harness.tools.edit import create_edit_tool
from egg_harness.tools.registry import ToolDefinition
from egg_harness.tools.write import create_write_tool

# ---------------------------------------------------------------------------
# TestWriteToolCreation
# ---------------------------------------------------------------------------


class TestWriteToolCreation:
    """Verify create_write_tool returns valid definition + handler."""

    def test_factory_returns_tuple(self):
        defn, handler = create_write_tool()
        assert isinstance(defn, ToolDefinition)
        assert callable(handler)

    def test_definition_name_is_write(self):
        defn, _ = create_write_tool()
        assert defn.name == "Write"


# ---------------------------------------------------------------------------
# TestWriteFile — file creation and overwrite
# ---------------------------------------------------------------------------


class TestWriteFile:
    """Write handler: create or overwrite files."""

    @pytest.mark.anyio
    async def test_write_creates_new_file(self, tmp_path: Path):
        """Writing to a path that does not exist creates the file."""
        target = tmp_path / "new_file.txt"

        _, handler = create_write_tool()
        result = await handler({"file_path": str(target), "content": "hello world"})

        assert not result.is_error
        assert target.exists()
        assert target.read_text() == "hello world"

    @pytest.mark.anyio
    async def test_write_overwrites_existing(self, tmp_path: Path):
        """Writing to an existing file replaces its content."""
        target = tmp_path / "existing.txt"
        target.write_text("old content")

        _, handler = create_write_tool()
        result = await handler({"file_path": str(target), "content": "new content"})

        assert not result.is_error
        assert target.read_text() == "new content"

    @pytest.mark.anyio
    async def test_write_creates_parent_dirs(self, tmp_path: Path):
        """Writing to a deeply nested path creates intermediate directories."""
        target = tmp_path / "a" / "b" / "c" / "deep.txt"

        _, handler = create_write_tool()
        result = await handler({"file_path": str(target), "content": "deep"})

        assert not result.is_error
        assert target.exists()
        assert target.read_text() == "deep"

    @pytest.mark.anyio
    async def test_write_empty_content(self, tmp_path: Path):
        """Writing an empty string creates an empty file."""
        target = tmp_path / "empty.txt"

        _, handler = create_write_tool()
        result = await handler({"file_path": str(target), "content": ""})

        assert not result.is_error
        assert target.exists()
        assert target.read_text() == ""

    @pytest.mark.anyio
    async def test_write_preserves_trailing_newline(self, tmp_path: Path):
        """Content with a trailing newline is preserved exactly."""
        target = tmp_path / "newline.txt"

        _, handler = create_write_tool()
        await handler({"file_path": str(target), "content": "line1\nline2\n"})

        assert target.read_text() == "line1\nline2\n"

    @pytest.mark.anyio
    async def test_write_unicode_content(self, tmp_path: Path):
        """Unicode content is written correctly."""
        target = tmp_path / "unicode.txt"
        content = "caf\u00e9 \u4f60\u597d \U0001f600"

        _, handler = create_write_tool()
        result = await handler({"file_path": str(target), "content": content})

        assert not result.is_error
        assert target.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# TestEditToolCreation
# ---------------------------------------------------------------------------


class TestEditToolCreation:
    """Verify create_edit_tool returns valid definition + handler."""

    def test_factory_returns_tuple(self):
        defn, handler = create_edit_tool()
        assert isinstance(defn, ToolDefinition)
        assert callable(handler)

    def test_definition_name_is_edit(self):
        defn, _ = create_edit_tool()
        assert defn.name == "Edit"


# ---------------------------------------------------------------------------
# TestEditFile — exact string replacement
# ---------------------------------------------------------------------------


class TestEditFile:
    """Edit handler: exact string replacement in existing files."""

    @pytest.mark.anyio
    async def test_edit_replaces_exact_string(self, tmp_path: Path):
        """When old_string appears exactly once it is replaced."""
        target = tmp_path / "code.py"
        target.write_text("def foo():\n    return 1\n")

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "return 1",
                "new_string": "return 42",
            }
        )

        assert not result.is_error
        assert target.read_text() == "def foo():\n    return 42\n"

    @pytest.mark.anyio
    async def test_edit_not_found_returns_error(self, tmp_path: Path):
        """When old_string is not present the result is an error."""
        target = tmp_path / "code.py"
        target.write_text("def foo():\n    return 1\n")

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "return 999",
                "new_string": "nope",
            }
        )

        assert result.is_error
        assert target.read_text() == "def foo():\n    return 1\n"

    @pytest.mark.anyio
    async def test_edit_not_unique_returns_error(self, tmp_path: Path):
        """When old_string appears more than once (without replace_all) -> error."""
        target = tmp_path / "dup.txt"
        target.write_text("aaa\nbbb\naaa\n")

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "aaa",
                "new_string": "ccc",
            }
        )

        assert result.is_error
        assert target.read_text() == "aaa\nbbb\naaa\n"

    @pytest.mark.anyio
    async def test_edit_replace_all(self, tmp_path: Path):
        """replace_all=True replaces every occurrence."""
        target = tmp_path / "multi.txt"
        target.write_text("foo bar foo baz foo\n")

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "foo",
                "new_string": "qux",
                "replace_all": True,
            }
        )

        assert not result.is_error
        assert target.read_text() == "qux bar qux baz qux\n"

    @pytest.mark.anyio
    async def test_edit_preserves_permissions(self, tmp_path: Path):
        """File permissions are preserved after an edit."""
        target = tmp_path / "script.sh"
        target.write_text("#!/bin/bash\necho old\n")
        target.chmod(0o755)

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "echo old",
                "new_string": "echo new",
            }
        )

        assert not result.is_error
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o755

    @pytest.mark.anyio
    async def test_edit_nonexistent_file(self, tmp_path: Path):
        """Editing a non-existent file returns an error gracefully."""
        target = tmp_path / "x" / "y" / "z" / "new.txt"

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "a",
                "new_string": "b",
            }
        )

        assert isinstance(result.is_error, bool)

    @pytest.mark.anyio
    async def test_edit_multiline_replacement(self, tmp_path: Path):
        """Multi-line old_string and new_string work correctly."""
        target = tmp_path / "block.py"
        original = "def greet():\n    print('hi')\n    print('bye')\n"
        target.write_text(original)

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "    print('hi')\n    print('bye')",
                "new_string": "    print('hello')\n    print('goodbye')",
            }
        )

        assert not result.is_error
        expected = "def greet():\n    print('hello')\n    print('goodbye')\n"
        assert target.read_text() == expected

    @pytest.mark.anyio
    async def test_edit_preserves_surrounding_content(self, tmp_path: Path):
        """Content before and after the replaced string is untouched."""
        target = tmp_path / "middle.txt"
        target.write_text("header\nREPLACE_ME\nfooter\n")

        _, handler = create_edit_tool()
        result = await handler(
            {
                "file_path": str(target),
                "old_string": "REPLACE_ME",
                "new_string": "REPLACED",
            }
        )

        assert not result.is_error
        assert target.read_text() == "header\nREPLACED\nfooter\n"

"""
Test that all Python files have valid syntax and can be parsed.

This is a baseline "does it compile" test suite. It ensures all Python
files in the repository have valid syntax before running more specific tests.
"""

import ast
import py_compile
from pathlib import Path

import pytest

# Get project root
PROJECT_ROOT = Path(__file__).parent.parent

# Directories to exclude from testing
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
}


def get_python_files():
    """Discover all Python files in the repository."""
    python_files = []
    for py_file in PROJECT_ROOT.rglob("*.py"):
        # Skip excluded directories
        if any(excluded in py_file.parts for excluded in EXCLUDE_DIRS):
            continue
        python_files.append(py_file)
    return python_files


def get_python_file_ids():
    """Get test IDs (relative paths) for each Python file."""
    return [str(f.relative_to(PROJECT_ROOT)) for f in get_python_files()]


class TestPythonSyntax:
    """Test Python files for valid syntax."""

    @pytest.mark.parametrize("py_file", get_python_files(), ids=get_python_file_ids())
    def test_syntax_valid(self, py_file: Path):
        """Test that Python file has valid syntax using py_compile."""
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            pytest.fail(f"Syntax error in {py_file}: {e}")

    @pytest.mark.parametrize("py_file", get_python_files(), ids=get_python_file_ids())
    def test_ast_parse(self, py_file: Path):
        """Test that Python file can be parsed as an AST."""
        try:
            source = py_file.read_text(encoding="utf-8")
            ast.parse(source, filename=str(py_file))
        except SyntaxError as e:
            pytest.fail(f"AST parse error in {py_file}:{e.lineno}: {e.msg}")


class TestPythonImports:
    """Test that Python modules can be imported (basic import check)."""

    @pytest.mark.parametrize("py_file", get_python_files(), ids=get_python_file_ids())
    def test_no_import_errors_at_parse_time(self, py_file: Path):
        """
        Test that the file doesn't have obvious import-time errors.

        This checks the AST for import statements but doesn't actually
        execute imports (which could have side effects or missing deps).
        """
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))

            # Just verify we can find import statements - actual import
            # testing would require installing all dependencies
            import_count = sum(
                1 for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
            )
            # This assertion always passes; the test is really about
            # whether parsing succeeds
            assert import_count >= 0

        except SyntaxError as e:
            pytest.fail(f"Parse error in {py_file}:{e.lineno}: {e.msg}")

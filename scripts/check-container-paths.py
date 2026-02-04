#!/usr/bin/env python3
"""
Lint check: Detect problematic sys.path patterns that may fail in container environments.

This script catches patterns that have historically caused issues when scripts run
in the jib container and construct sys.path entries incorrectly. The critical issue is:

    sys.path.insert(0, str(Path.home() / "khan" / repo_name / "shared"))
                                                 ^^^^^^^^^^
                                                 WRONG: variable repo name

When a script in egg tries to import its own modules (e.g., weekly_analyzer),
it should NOT construct the path using a user-provided repo_name variable. Instead:

CORRECT patterns:
    1. sys.path.insert(0, str(Path(__file__).resolve().parents[N] / "shared"))
    2. sys.path.insert(0, str(Path.home() / "khan" / "egg" / "shared"))
    3. sys.path.insert(0, "/opt/jib-runtime/shared")  # Container-specific path

INCORRECT patterns (flagged by this linter - JIB001):
    1. sys.path.insert(..., str(Path.home() / "khan" / variable / ...))
    2. sys.path.append(str(Path.home() / "khan" / variable / ...))

NOTE: Using `Path.home() / "khan" / repo_name` for working directories (cwd) is FINE.
The issue is specifically when this pattern is used to modify sys.path for imports.

False positive handling:
    - Add `# noqa: JIB001` to suppress specific lines
    - Safe subdirectories like "egg" are allowlisted

Usage:
    python3 scripts/check-container-paths.py

Exit codes:
    0 - No problematic patterns found
    1 - Found patterns that may cause path resolution failures
"""

import ast
import sys
from pathlib import Path

# Directories that are known to exist and are safe to combine with Path.home()/"khan"
SAFE_SUBDIRECTORIES = {
    "egg",  # This repo itself
}


class PathPatternVisitor(ast.NodeVisitor):
    """AST visitor that detects problematic sys.path modification patterns."""

    def __init__(self, file_path: Path, source_lines: list[str]):
        self.file_path = file_path
        self.source_lines = source_lines
        self.violations: list[tuple[int, str, str]] = []  # (line, code, explanation)

    def _get_line_text(self, lineno: int) -> str:
        """Get the source line text."""
        if 1 <= lineno <= len(self.source_lines):
            return self.source_lines[lineno - 1].strip()
        return ""

    def _has_noqa(self, lineno: int, code: str = "JIB001") -> bool:
        """Check if line has noqa comment for this rule."""
        line = self._get_line_text(lineno)
        return f"noqa: {code}" in line or "noqa" in line

    def _is_safe_khan_subdir(self, node: ast.expr) -> bool:
        """Check if the node represents a safe subdirectory under ~/repos/."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value in SAFE_SUBDIRECTORIES
        return False

    def _is_variable(self, node: ast.expr) -> bool:
        """Check if node is a variable (Name)."""
        return isinstance(node, ast.Name)

    def _get_variable_name(self, node: ast.expr) -> str | None:
        """Get variable name if node is a Name, else None."""
        if isinstance(node, ast.Name):
            return node.id
        return None

    def _collect_div_chain(self, node: ast.expr) -> list[ast.expr]:
        """Recursively collect all parts of a / chain."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._collect_div_chain(node.left) + [node.right]
        return [node]

    def _is_path_home_call(self, node: ast.expr) -> bool:
        """Check if node is Path.home() call."""
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "home"
            and isinstance(node.func.value, ast.Name)
        ):
            return node.func.value.id == "Path"
        return False

    def _is_sys_path_call(self, node: ast.Call) -> bool:
        """Check if this is a sys.path.insert() or sys.path.append() call."""
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("insert", "append")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and isinstance(node.func.value.value, ast.Name)
        ):
            return node.func.value.value.id == "sys"
        return False

    def _check_for_dynamic_khan_path(self, node: ast.expr) -> tuple[bool, str | None]:
        """Check if node contains Path.home() / 'khan' / variable pattern.

        Returns:
            Tuple of (is_problematic, variable_name)
        """
        # Handle str() wrapper
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and node.args
        ):
            return self._check_for_dynamic_khan_path(node.args[0])

        # Check for / chain
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = self._collect_div_chain(node)

            # Look for Path.home() / "khan" / <variable> pattern
            if len(parts) >= 3:
                first = parts[0]
                if self._is_path_home_call(first):
                    second = parts[1]
                    if isinstance(second, ast.Constant) and second.value == "khan":
                        third = parts[2]
                        if self._is_variable(third) and not self._is_safe_khan_subdir(third):
                            return True, self._get_variable_name(third)

        return False, None

    def visit_Call(self, node: ast.Call) -> None:
        """Detect sys.path.insert/append with dynamic khan paths."""
        if self._is_sys_path_call(node):
            # Check the path argument (last arg for insert, first arg for append)
            path_arg = None
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == "insert" and len(node.args) >= 2:
                    path_arg = node.args[1]
                elif node.func.attr == "append" and len(node.args) >= 1:
                    path_arg = node.args[0]

            if path_arg:
                is_problematic, var_name = self._check_for_dynamic_khan_path(path_arg)
                if is_problematic and not self._has_noqa(node.lineno):
                    line_text = self._get_line_text(node.lineno)
                    self.violations.append(
                        (
                            node.lineno,
                            line_text,
                            f"sys.path modification with dynamic repo path (variable: {var_name})\n"
                            f"         This can cause 'ModuleNotFoundError' when repo_name != 'egg'\n"
                            f"         Fix: Use explicit path: Path.home() / 'khan' / 'egg' / ...",
                        )
                    )

        self.generic_visit(node)


def check_file(file_path: Path) -> list[tuple[int, str, str]]:
    """Check a single Python file for problematic path patterns."""
    try:
        content = file_path.read_text()
        tree = ast.parse(content, filename=str(file_path))
        lines = content.split("\n")

        visitor = PathPatternVisitor(file_path, lines)
        visitor.visit(tree)
        return visitor.violations

    except SyntaxError as e:
        print(f"Warning: Syntax error in {file_path}: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Warning: Could not check {file_path}: {e}", file=sys.stderr)
        return []


def main() -> int:
    """Run the path pattern lint check."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    # Check both container and host-services directories
    directories_to_check = [
        repo_root / "sandbox",
        repo_root / "host-services",
    ]

    all_violations: list[tuple[Path, list[tuple[int, str, str]]]] = []

    for check_dir in directories_to_check:
        if not check_dir.exists():
            continue

        for py_file in check_dir.rglob("*.py"):
            violations = check_file(py_file)
            if violations:
                rel_path = py_file.relative_to(repo_root)
                all_violations.append((rel_path, violations))

    if all_violations:
        print("ERROR: Found problematic sys.path patterns!\n")
        print("=" * 76)
        print("These patterns can cause 'ModuleNotFoundError' in the container when")
        print("sys.path is modified with a dynamic repo path (e.g., repo_name variable).")
        print()
        print("Example of the bug:")
        print("  sys.path.insert(0, str(Path.home() / 'khan' / repo_name / 'shared'))")
        print("  # If repo_name='webapp', looks for 'weekly_analyzer' in webapp/shared/")
        print("  # But weekly_analyzer is in egg/shared/!")
        print("=" * 76)
        print()

        for file_path, violations in all_violations:
            print(f"File: {file_path}")
            for lineno, code, explanation in violations:
                print(f"  Line {lineno}: {code}")
                print(f"         {explanation}")
            print()

        print("How to fix:")
        print("  1. Use explicit path for egg modules:")
        print("     sys.path.insert(0, str(Path.home() / 'khan' / 'egg' / 'shared'))")
        print()
        print("  2. Use dynamic discovery relative to script location:")
        print("     sys.path.insert(0, str(Path(__file__).resolve().parents[N] / 'shared'))")
        print()
        print("  3. Use container path for runtime:")
        print("     sys.path.insert(0, '/opt/jib-runtime/shared')")
        print()
        print("  4. To suppress a false positive, add: # noqa: JIB001")
        print()

        return 1
    else:
        print("OK: No problematic sys.path patterns found")
        return 0


if __name__ == "__main__":
    sys.exit(main())

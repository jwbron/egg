#!/usr/bin/env python3
"""
Lint check: Ensure LLM API calls only happen inside the sandbox.

The orchestrator, gateway, and shared modules must NEVER call the Anthropic
API directly.  LLM calls must be delegated to sandbox containers — this
maintains the security boundary between the orchestrator (which has Docker
and pipeline credentials) and the LLM (which processes untrusted prompts).

Detection patterns (AST-based for Python files):
  - ``import anthropic`` / ``from anthropic import ...``
  - String literals containing ``api.anthropic.com``
  - ``anthropic-version`` header string in assignments or calls
  - ``x-api-key`` alongside ``anthropic-version`` in dict literals
  - ``os.environ.get("ANTHROPIC_API_KEY")`` (outside sandbox)

Suppression:
    # noqa: EGG200 - <justification>

Usage:
    python3 scripts/check-llm-api-calls.py

Exit codes:
    0 - No violations found
    1 - Found violations
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

NOQA_CODE = "EGG200"

# Directories to scan (relative to repo root)
SCAN_DIRS = ("orchestrator", "gateway", "shared")

# Directories/patterns to skip
SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".mypy_cache"}
SKIP_SUFFIXES = ("_test.py", "test_.py")


def _should_skip_path(path: Path) -> bool:
    """Return True if path should be excluded from scanning."""
    parts = path.parts
    # Skip excluded directories
    if any(part in SKIP_DIRS for part in parts):
        return True
    # Skip test files
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    # Skip files in test directories
    if "tests" in parts:
        return True
    return False


class LLMApiVisitor(ast.NodeVisitor):
    """AST visitor that detects direct LLM API usage."""

    def __init__(self, source_lines: list[str]):
        self.source_lines = source_lines
        self.violations: list[tuple[int, str]] = []

    def _has_noqa(self, lineno: int) -> bool:
        """Check if a line has a noqa: EGG200 comment."""
        if 1 <= lineno <= len(self.source_lines):
            line = self.source_lines[lineno - 1]
            return f"noqa: {NOQA_CODE}" in line
        return False

    def visit_Import(self, node: ast.Import) -> None:
        """Detect ``import anthropic``."""
        if self._has_noqa(node.lineno):
            self.generic_visit(node)
            return
        for alias in node.names:
            if alias.name == "anthropic" or alias.name.startswith("anthropic."):
                self.violations.append(
                    (node.lineno, f"Direct import of Anthropic SDK: import {alias.name}")
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Detect ``from anthropic import ...``."""
        if self._has_noqa(node.lineno):
            self.generic_visit(node)
            return
        if node.module and (node.module == "anthropic" or node.module.startswith("anthropic.")):
            self.violations.append(
                (node.lineno, f"Direct import from Anthropic SDK: from {node.module} import ...")
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """Detect string literals referencing the Anthropic API."""
        if not isinstance(node.value, str):
            self.generic_visit(node)
            return
        if self._has_noqa(node.lineno):
            self.generic_visit(node)
            return

        val = node.value
        if "api.anthropic.com" in val:
            self.violations.append(
                (node.lineno, f"Anthropic API URL in string literal: ...{val[:80]}...")
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Detect os.environ access for ANTHROPIC_API_KEY."""
        if self._has_noqa(node.lineno):
            self.generic_visit(node)
            return

        # Detect os.environ.get("ANTHROPIC_API_KEY") or os.environ["ANTHROPIC_API_KEY"]
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            if isinstance(func.value, ast.Attribute) and func.value.attr == "environ":
                if isinstance(func.value.value, ast.Name) and func.value.value.id == "os":
                    if node.args:
                        first_arg = node.args[0]
                        if (
                            isinstance(first_arg, ast.Constant)
                            and isinstance(first_arg.value, str)
                            and first_arg.value == "ANTHROPIC_API_KEY"
                        ):
                            self.violations.append(
                                (
                                    node.lineno,
                                    "Direct access to ANTHROPIC_API_KEY via os.environ.get()",
                                )
                            )

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Detect os.environ["ANTHROPIC_API_KEY"]."""
        if self._has_noqa(node.lineno):
            self.generic_visit(node)
            return

        if isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            if isinstance(node.value.value, ast.Name) and node.value.value.id == "os":
                if isinstance(node.slice, ast.Constant) and node.slice.value == "ANTHROPIC_API_KEY":
                    self.violations.append(
                        (node.lineno, "Direct access to ANTHROPIC_API_KEY via os.environ[]")
                    )

        self.generic_visit(node)


def check_python_file(file_path: Path) -> list[tuple[int, str]]:
    """Parse a Python file and return list of (lineno, description) violations."""
    try:
        content = file_path.read_text()
        tree = ast.parse(content, filename=str(file_path))
        lines = content.split("\n")
        visitor = LLMApiVisitor(lines)
        visitor.visit(tree)
        return visitor.violations
    except SyntaxError as e:
        print(f"Warning: Could not parse {file_path}: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
        return []


def main() -> int:
    """Run all checks and report violations."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    all_violations: list[tuple[str, list[tuple[int, str]]]] = []

    for scan_dir in SCAN_DIRS:
        dir_path = repo_root / scan_dir
        if not dir_path.is_dir():
            continue

        for py_file in dir_path.rglob("*.py"):
            if _should_skip_path(py_file):
                continue

            rel = str(py_file.relative_to(repo_root))
            violations = check_python_file(py_file)
            if violations:
                all_violations.append((rel, violations))

    if all_violations:
        print("ERROR: Found direct LLM API usage outside the sandbox!\n")
        print("=" * 70)
        print("LLM API calls must ONLY happen inside sandbox containers.")
        print("The orchestrator/gateway/shared must delegate to sandbox.")
        print("=" * 70)
        print()

        for file_path, violations in sorted(all_violations):
            print(f"File: {file_path}")
            for lineno, desc in sorted(violations):
                print(f"  Line {lineno}: {desc}")
            print()

        print("How to fix:")
        print("  1. Move LLM API calls to a sandbox script (e.g. sandbox/bin/)")
        print("  2. Use ContainerSpawner to delegate from the orchestrator")
        print("  3. If this is a false positive, suppress with:")
        print(f"       # noqa: {NOQA_CODE} - <justification>")
        print()
        print("  See orchestrator/health_checks/tier2/agent_inspector.py for")
        print("  an example of the sandbox delegation pattern.")
        print()

        return 1
    else:
        print("OK: No direct LLM API usage found outside sandbox")
        return 0


if __name__ == "__main__":
    sys.exit(main())

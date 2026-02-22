#!/usr/bin/env python3
"""  # noqa: EGG201 - linter script references model IDs in examples
Lint check: Enforce Claude model alias form.

Model references should use short aliases (``sonnet``, ``opus``, ``haiku``)
rather than full model identifiers (``claude-sonnet-4``,
``claude-sonnet-4-20250514``, etc.).  Using aliases ensures automatic
adoption of the latest model version without code changes.

Detection patterns (AST-based for Python files):
  - String literals matching ``claude-<family>-*`` patterns used as model IDs
  - Both date-pinned (``claude-sonnet-4-20250514``) and unpinned
    (``claude-sonnet-4``) full identifiers are flagged

Suppression:
    # noqa: EGG201 - <justification>

Usage:
    python3 scripts/check-model-versions.py

Exit codes:
    0 - No violations found
    1 - Found violations
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

NOQA_CODE = "EGG201"

# Regex matching full Claude model identifiers.
# Matches patterns like:
#   claude-sonnet-4-20250514  → should be "sonnet"
#   claude-sonnet-4           → should be "sonnet"
#   claude-opus-4-5-20251101  → should be "opus"
#   claude-opus-4-5           → should be "opus"
#   claude-opus-4             → should be "opus"
#   claude-haiku-4-5-20251001 → should be "haiku"
#   claude-haiku-4-5          → should be "haiku"
#   claude-3-haiku-20240307   → should be "haiku"
#   claude-3-5-sonnet-20241022 → should be "sonnet"
# Does NOT match non-model references like "claude code" or "claude --print"
_MODEL_ID_RE = re.compile(
    r"\bclaude-(?:(\d+-\d+-|)(\d+-|))"     # optional version prefix (3-5-, 3-)
    r"(sonnet|opus|haiku)"                   # model family
    r"(?:-[\d]+(?:-[\d]+)?)?(?:-\d{8})?\b"   # optional version/date suffix
)

# Map full model ID families to their short alias
_FAMILY_ALIAS = {
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
}

# Directories/patterns to skip
SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules", ".mypy_cache"}

# Directories to scan (relative to repo root)
SCAN_DIRS = ("orchestrator", "gateway", "shared", "sandbox", "config", "bin", "scripts")


def _should_skip_path(path: Path) -> bool:
    """Return True if path should be excluded from scanning."""
    parts = path.parts
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


def _suggest_alias(model_id: str) -> str:
    """Suggest the short alias for a full model identifier."""
    for family, alias in _FAMILY_ALIAS.items():
        if family in model_id:
            return alias
    return "sonnet"  # fallback


class ModelAliasVisitor(ast.NodeVisitor):
    """AST visitor that detects non-alias Claude model references."""

    def __init__(self, source_lines: list[str]):
        self.source_lines = source_lines
        self.violations: list[tuple[int, str]] = []

    def _has_noqa(self, lineno: int) -> bool:
        """Check if a line has a noqa: EGG201 comment."""
        if 1 <= lineno <= len(self.source_lines):
            line = self.source_lines[lineno - 1]
            return f"noqa: {NOQA_CODE}" in line
        return False

    def visit_Constant(self, node: ast.Constant) -> None:
        """Detect string literals containing full model identifiers."""
        if not isinstance(node.value, str):
            self.generic_visit(node)
            return
        if self._has_noqa(node.lineno):
            self.generic_visit(node)
            return

        match = _MODEL_ID_RE.search(node.value)
        if match:
            model_id = match.group(0)
            alias = _suggest_alias(model_id)
            self.violations.append(
                (
                    node.lineno,
                    f"Use model alias: {model_id} → use \"{alias}\" instead",
                )
            )
        self.generic_visit(node)


def check_python_file(file_path: Path) -> list[tuple[int, str]]:
    """Parse a Python file and return list of (lineno, description) violations."""
    try:
        content = file_path.read_text()
        tree = ast.parse(content, filename=str(file_path))
        lines = content.split("\n")
        visitor = ModelAliasVisitor(lines)
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
        print("ERROR: Found non-alias Claude model references!\n")
        print("=" * 70)
        print("Use short model aliases for automatic version adoption.")
        print('E.g. "sonnet", "opus", "haiku"')
        print("=" * 70)
        print()

        for file_path, violations in sorted(all_violations):
            print(f"File: {file_path}")
            for lineno, desc in sorted(violations):
                print(f"  Line {lineno}: {desc}")
            print()

        print("How to fix:")
        print('  1. Replace the model identifier with its alias:')
        print('     "claude-sonnet-4-20250514" → "sonnet"')  # noqa: EGG201 - help text example
        print('     "claude-opus-4"            → "opus"')  # noqa: EGG201 - help text example
        print('     "claude-haiku-4-5"         → "haiku"')  # noqa: EGG201 - help text example
        print("  2. If a full identifier is genuinely required, suppress with:")
        print(f"       # noqa: {NOQA_CODE} - <justification>")
        print()

        return 1
    else:
        print("OK: No non-alias Claude model references found")
        return 0


if __name__ == "__main__":
    sys.exit(main())

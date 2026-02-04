#!/usr/bin/env python3
"""
Lint check: Ensure gh CLI WRITE operations are not used directly in host-services.

The gh CLI WRITE operations should ONLY be run inside the egg container because:
1. The container has GITHUB_TOKEN (GitHub App token) configured
2. Actions using gh CLI in the container appear under egg's identity
3. Running gh CLI on the host uses the human user's gh auth (wrong identity)

IMPORTANT: READ operations (gh run view, gh pr view, gh api GET, etc.) are ALLOWED
on the host because they don't affect identity - it doesn't matter who reads data.

CORRECT pattern for host-services:
    - Use egg_exec() to delegate WRITE operations to container-side handlers
    - Container handlers (in sandbox/egg-tasks/) run gh CLI
    - Example: egg_exec("github_pr_create", {...})

INCORRECT pattern:
    - subprocess.run(["gh", "pr", "create", ...])  # WRITE - creates PR as wrong user
    - subprocess.run(["gh", "pr", "comment", ...]) # WRITE - comments as wrong user
    - subprocess.run(["gh", "pr", "close", ...])   # WRITE - closes as wrong user
    - subprocess.run(["gh", "issue", "create", ...]) # WRITE - creates as wrong user

ALLOWED pattern (READ operations):
    - subprocess.run(["gh", "run", "view", ...])   # READ - fetches logs
    - subprocess.run(["gh", "pr", "view", ...])    # READ - fetches PR data
    - subprocess.run(["gh", "api", ...])           # READ - API calls (GET only)

Note: `gh api -X POST/PUT/DELETE/PATCH` are WRITE operations and will be flagged.

Limitations:
    - Variable-based commands are NOT detected. The following will NOT be caught:
        cmd = ["gh", "pr", "create", ...]
        subprocess.run(cmd)  # Not detected because cmd is a variable
    - Commands built dynamically at runtime are not detected

Available container handlers for WRITE operations:
    - github_pr_create: Create a PR
    - github_pr_comment: Add comment to PR
    - github_pr_close: Close a PR

This script is intended to be run as a CI check or pre-commit hook.

Usage:
    python3 scripts/check-gh-cli-usage.py

Exit codes:
    0 - No forbidden gh CLI usage found
    1 - Found forbidden gh CLI usage in host-services
"""

import ast
import re
import sys
from pathlib import Path

# gh CLI subcommands that perform WRITE operations (affect identity)
# These must go through egg container to use egg's identity
WRITE_SUBCOMMANDS = {
    # PR write operations
    ("pr", "create"),
    ("pr", "comment"),
    ("pr", "close"),
    ("pr", "merge"),
    ("pr", "edit"),
    ("pr", "review"),
    ("pr", "ready"),
    # Issue write operations
    ("issue", "create"),
    ("issue", "comment"),
    ("issue", "close"),
    ("issue", "edit"),
    ("issue", "delete"),
    ("issue", "reopen"),
    ("issue", "transfer"),
    # Repo write operations
    ("repo", "create"),
    ("repo", "delete"),
    ("repo", "edit"),
    ("repo", "fork"),
    ("repo", "rename"),
    # Release write operations
    ("release", "create"),
    ("release", "delete"),
    ("release", "edit"),
    ("release", "upload"),
    # Gist write operations
    ("gist", "create"),
    ("gist", "delete"),
    ("gist", "edit"),
    # Label write operations
    ("label", "create"),
    ("label", "delete"),
    ("label", "edit"),
    # Project write operations (v2)
    ("project", "create"),
    ("project", "delete"),
    ("project", "edit"),
    ("project", "item-create"),
    ("project", "item-delete"),
    ("project", "item-edit"),
    # Workflow dispatch
    ("workflow", "run"),
}

# HTTP methods that indicate write operations when used with `gh api -X`
API_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def is_write_operation(subcommand1: str, subcommand2: str | None) -> bool:
    """Check if a gh CLI command is a write operation."""
    if subcommand2:
        return (subcommand1, subcommand2) in WRITE_SUBCOMMANDS
    # If we can't determine the full subcommand, be conservative
    # and only flag known write patterns
    return False


class GhCliVisitor(ast.NodeVisitor):
    """AST visitor that detects gh CLI WRITE calls in subprocess invocations."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.violations: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        """Check subprocess.run and similar calls for gh CLI WRITE usage."""
        # Check if this is a subprocess call
        func_name = self._get_call_name(node)
        if func_name not in (
            "subprocess.run",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "subprocess.Popen",
            "run",  # Could be imported directly
        ):
            self.generic_visit(node)
            return

        # Check the first argument (command)
        if not node.args:
            self.generic_visit(node)
            return

        first_arg = node.args[0]

        # Check for list literals like ["gh", "pr", "create", ...]
        if isinstance(first_arg, ast.List) and first_arg.elts:
            first_elem = first_arg.elts[0]
            if isinstance(first_elem, ast.Constant) and first_elem.value == "gh":
                # Get the subcommands to check if it's a write operation
                subcommand1 = None
                subcommand2 = None
                if len(first_arg.elts) > 1:
                    second_elem = first_arg.elts[1]
                    if isinstance(second_elem, ast.Constant) and isinstance(second_elem.value, str):
                        subcommand1 = second_elem.value
                if len(first_arg.elts) > 2:
                    third_elem = first_arg.elts[2]
                    if isinstance(third_elem, ast.Constant) and isinstance(third_elem.value, str):
                        subcommand2 = third_elem.value

                # Only flag write operations
                if subcommand1 and is_write_operation(subcommand1, subcommand2):
                    cmd_str = f"gh {subcommand1}"
                    if subcommand2:
                        cmd_str += f" {subcommand2}"
                    self.violations.append(
                        (node.lineno, f"subprocess WRITE call: [{cmd_str}, ...]")
                    )
                # Check for gh api -X POST/PUT/DELETE/PATCH
                elif subcommand1 == "api":
                    self._check_api_write_operation(node, first_arg)

        # Check for string commands like "gh pr create ..."
        elif isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            cmd = first_arg.value
            if cmd.startswith("gh "):
                # Parse subcommands from string
                parts = cmd.split()
                if len(parts) >= 3:
                    subcommand1 = parts[1]
                    subcommand2 = parts[2]
                    if is_write_operation(subcommand1, subcommand2):
                        self.violations.append(
                            (node.lineno, f"subprocess WRITE call: '{cmd[:50]}...'")
                        )
                    # Check for gh api -X POST/PUT/DELETE/PATCH in string form
                    elif subcommand1 == "api":
                        for i, part in enumerate(parts):
                            if part in ("-X", "--method") and i + 1 < len(parts):
                                method = parts[i + 1].upper()
                                if method in API_WRITE_METHODS:
                                    self.violations.append(
                                        (
                                            node.lineno,
                                            f"subprocess WRITE call: 'gh api -X {method}...'",
                                        )
                                    )
                                    break

        self.generic_visit(node)

    def _check_api_write_operation(self, node: ast.Call, list_arg: ast.List) -> None:
        """Check for gh api -X POST/PUT/DELETE/PATCH patterns."""
        # Look for -X or --method followed by a write method
        elts = list_arg.elts
        for i, elem in enumerate(elts):
            if not (isinstance(elem, ast.Constant) and isinstance(elem.value, str)):
                continue
            if elem.value not in ("-X", "--method"):
                continue
            # Check if next element is a write method
            if i + 1 >= len(elts):
                continue
            next_elem = elts[i + 1]
            if not (isinstance(next_elem, ast.Constant) and isinstance(next_elem.value, str)):
                continue
            method = next_elem.value.upper()
            if method in API_WRITE_METHODS:
                self.violations.append(
                    (
                        node.lineno,
                        f"subprocess WRITE call: [gh api -X {method}, ...]",
                    )
                )
                return

    def _get_call_name(self, node: ast.Call) -> str:
        """Extract the full name of a function call."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            parts = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        return ""


def check_file_with_ast(file_path: Path) -> list[tuple[int, str]]:
    """Check a Python file for gh CLI usage using AST parsing."""
    try:
        content = file_path.read_text()
        tree = ast.parse(content, filename=str(file_path))
        visitor = GhCliVisitor(file_path)
        visitor.visit(tree)
        return visitor.violations
    except SyntaxError as e:
        print(f"Warning: Could not parse {file_path}: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
        return []


def check_file_with_regex(file_path: Path) -> list[tuple[int, str]]:
    """Fallback regex check for non-Python files or additional patterns.

    Only flags WRITE operations, not READ operations.
    """
    violations = []

    # Build patterns for known write operations
    # Format: gh <category> <action> where (category, action) is in WRITE_SUBCOMMANDS
    write_patterns = []
    for category, action in WRITE_SUBCOMMANDS:
        # Pattern for list format: ["gh", "pr", "create", ...]
        write_patterns.append(
            (
                r'\[\s*["\']gh["\']\s*,\s*["\']'
                + category
                + r'["\']\s*,\s*["\']'
                + action
                + r'["\']',
                f"subprocess WRITE call: gh {category} {action}",
            )
        )
        # Pattern for string format: "gh pr create ..."
        write_patterns.append(
            (
                rf'["\']gh\s+{category}\s+{action}\b',
                f"subprocess WRITE call: gh {category} {action}",
            )
        )

    # Add patterns for gh api -X POST/PUT/DELETE/PATCH
    for method in API_WRITE_METHODS:
        # Pattern for list format: ["gh", "api", ..., "-X", "POST", ...]
        write_patterns.append(
            (
                r'\[\s*["\']gh["\']\s*,\s*["\']api["\'].*["\'](-X|--method)["\']\s*,\s*["\']'
                + method
                + r'["\']',
                f"subprocess WRITE call: gh api -X {method}",
            )
        )
        # Pattern for string format: "gh api -X POST ..."
        write_patterns.append(
            (
                r'["\']gh\s+api\s+.*(-X|--method)\s+' + method + r"\b",
                f"subprocess WRITE call: gh api -X {method}",
            )
        )

    try:
        content = file_path.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines, start=1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            for pattern, description in write_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    violations.append((i, description))
                    break

    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)

    return violations


def check_file(file_path: Path) -> list[tuple[int, str]]:
    """Check a file for forbidden gh CLI usage."""
    if file_path.suffix == ".py":
        # Use AST for Python files (more accurate)
        violations = check_file_with_ast(file_path)
        # Also run regex as backup for edge cases
        regex_violations = check_file_with_regex(file_path)
        # Deduplicate by line number
        seen_lines = {v[0] for v in violations}
        for v in regex_violations:
            if v[0] not in seen_lines:
                violations.append(v)
        return violations
    else:
        # Use regex for other files
        return check_file_with_regex(file_path)


def main():
    """Run the lint check on host-services directory."""
    # Get repo root (parent of scripts/)
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    host_services_dir = repo_root / "host-services"

    if not host_services_dir.exists():
        print(f"Warning: host-services directory not found at {host_services_dir}")
        return 0

    # Directories excluded from this check - these are authorized to use gh CLI
    # gateway: This IS the authorized proxy for GitHub operations from containers
    excluded_dirs = {
        "gateway",
    }

    # Find all Python files in host-services (excluding authorized directories)
    python_files = [
        f
        for f in host_services_dir.rglob("*.py")
        if not any(excluded in f.parts for excluded in excluded_dirs)
    ]

    all_violations = []

    for file_path in python_files:
        violations = check_file(file_path)
        if violations:
            rel_path = file_path.relative_to(repo_root)
            all_violations.append((rel_path, violations))

    # Report results
    if all_violations:
        print("ERROR: Found forbidden gh CLI WRITE operations in host-services!\n")
        print("=" * 70)
        print("gh CLI WRITE operations should ONLY run inside the egg container.")
        print("Host services must use egg_exec() to delegate GitHub WRITE operations.")
        print("")
        print("NOTE: READ operations (gh run view, gh pr view, etc.) are allowed")
        print("on the host because they don't affect identity.")
        print("=" * 70)
        print()

        for file_path, violations in all_violations:
            print(f"File: {file_path}")
            for line_num, description in violations:
                print(f"  Line {line_num}: {description}")
            print()

        print("How to fix:")
        print("  1. Import egg_exec: from egg_exec import egg_exec")
        print("  2. Replace subprocess.run(['gh', 'pr', 'create', ...]) with egg_exec(...)")
        print()
        print("Available egg_exec handlers:")
        print("  - egg_exec('github_pr_create', {'repo': ..., 'title': ..., ...})")
        print("  - egg_exec('github_pr_comment', {'repo': ..., 'pr_number': ..., 'body': ...})")
        print("  - egg_exec('github_pr_close', {'repo': ..., 'pr_number': ...})")
        print()
        print("See host-services/analysis/feature-analyzer/pr_creator.py for examples.")
        print()

        return 1
    else:
        print("OK: No forbidden gh CLI WRITE operations found in host-services")
        return 0


if __name__ == "__main__":
    sys.exit(main())

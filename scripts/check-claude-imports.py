#!/usr/bin/env python3
"""
Lint check: Ensure Claude/Anthropic is not used in host-services.

Host services MUST NOT call Claude or the Anthropic API directly because:
1. Security: Host services have access to credentials that should not be exposed to LLMs
2. Architecture: The container boundary provides security isolation
3. Prompt injection: Direct API calls from host could allow prompt injection to escalate to host access

CORRECT pattern for host-services:
    - Use jib_exec to delegate to a container-side processor
    - The processor (in sandbox/egg-tasks/) can import and use claude/anthropic

INCORRECT patterns:
    - Importing 'from claude import ...' in host-services files
    - Importing 'import anthropic' or 'from anthropic import ...'
    - Adding 'anthropic' as a dependency in host-services/pyproject.toml

This script is intended to be run as a CI check or pre-commit hook.

See: sandbox/.claude/rules/host-container-boundary.md

Usage:
    python3 scripts/check-claude-imports.py

Exit codes:
    0 - No forbidden imports found
    1 - Found forbidden Claude/Anthropic imports or dependencies in host-services
"""

import re
import sys
from pathlib import Path


def check_file_for_claude_import(file_path: Path) -> list[tuple[int, str]]:
    """Check a file for forbidden Claude imports.

    Returns:
        List of (line_number, line_content) tuples for each violation.
    """
    violations = []

    # Patterns that indicate direct Claude/Anthropic/LLM usage
    forbidden_patterns = [
        # LLM module (container-only - unified interface)
        r"^\s*from\s+llm\s+import",  # from llm import ...
        r"^\s*import\s+llm\b",  # import llm
        r"^\s*from\s+llm\.",  # from llm.module import ...
        # Claude module (container-only - backward compat wrapper)
        r"^\s*from\s+claude\s+import",  # from claude import ...
        r"^\s*import\s+claude\b",  # import claude
        r"^\s*from\s+claude\.",  # from claude.module import ...
        # Anthropic SDK (SECURITY VIOLATION - see host-container-boundary.md)
        r"^\s*import\s+anthropic\b",  # import anthropic
        r"^\s*from\s+anthropic\s+import",  # from anthropic import ...
        r"^\s*from\s+anthropic\.",  # from anthropic.module import ...
    ]

    try:
        content = file_path.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines, start=1):
            for pattern in forbidden_patterns:
                if re.match(pattern, line):
                    violations.append((i, line.strip()))
                    break

    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)

    return violations


def check_pyproject_for_anthropic(pyproject_path: Path) -> list[tuple[int, str]]:
    """Check pyproject.toml for forbidden anthropic dependency.

    Returns:
        List of (line_number, line_content) tuples for each violation.
    """
    violations = []

    if not pyproject_path.exists():
        return violations

    try:
        content = pyproject_path.read_text()
        lines = content.split("\n")

        # Look for anthropic in dependencies - handles various TOML formats:
        # - "anthropic>=0.39.0"  (double quotes)
        # - 'anthropic>=0.39.0'  (single quotes)
        # - anthropic>=0.39.0   (no quotes, array syntax)
        anthropic_pattern = r'^\s*["\']?anthropic\b'

        for i, line in enumerate(lines, start=1):
            if re.search(anthropic_pattern, line):
                violations.append((i, line.strip()))

    except Exception as e:
        print(f"Warning: Could not read {pyproject_path}: {e}", file=sys.stderr)

    return violations


def main():
    """Run the lint check on host-services directory."""
    # Get repo root (parent of scripts/)
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    host_services_dir = repo_root / "host-services"

    if not host_services_dir.exists():
        print(f"Warning: host-services directory not found at {host_services_dir}")
        return 0

    # Find all Python files in host-services
    python_files = list(host_services_dir.rglob("*.py"))

    all_violations = []

    for file_path in python_files:
        violations = check_file_for_claude_import(file_path)
        if violations:
            rel_path = file_path.relative_to(repo_root)
            all_violations.append((rel_path, violations))

    # Also check pyproject.toml for anthropic dependency
    pyproject_path = host_services_dir / "pyproject.toml"
    pyproject_violations = check_pyproject_for_anthropic(pyproject_path)
    if pyproject_violations:
        rel_path = pyproject_path.relative_to(repo_root)
        all_violations.append((rel_path, pyproject_violations))

    # Report results
    if all_violations:
        print("ERROR: Found forbidden Claude/Anthropic usage in host-services!\n")
        print("=" * 70)
        print("SECURITY VIOLATION: Host services MUST NOT call Claude or Anthropic directly.")
        print()
        print("Why this matters:")
        print("  - Host services have access to credentials that should not be exposed to LLMs")
        print("  - The container boundary provides security isolation")
        print("  - Direct API calls from host could allow prompt injection to escalate")
        print("=" * 70)
        print()

        for file_path, violations in all_violations:
            print(f"File: {file_path}")
            for line_num, line_content in violations:
                print(f"  Line {line_num}: {line_content}")
            print()

        print("How to fix:")
        print("  1. Remove anthropic from host-services dependencies")
        print("  2. Create a processor in sandbox/egg-tasks/ for LLM work")
        print("  3. Use jib_exec to delegate to the container-side processor")
        print("  4. See host-services/shared/jib_exec.py for the delegation pattern")
        print()
        print("Documentation: sandbox/.claude/rules/host-container-boundary.md")
        print()

        return 1
    else:
        print("OK: No forbidden Claude/Anthropic usage found in host-services")
        return 0


if __name__ == "__main__":
    sys.exit(main())

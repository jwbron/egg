#!/usr/bin/env python3
"""
Lint check: Ensure sandbox does not import from host-services.

The container and host are separate security domains with different trust levels.
Container code should NOT import from host-services because:

1. Architecture: The container is meant to be self-contained and portable
2. Docker builds: Container code is baked into the Docker image; host-services is not
3. Security boundary: host-services may contain code that assumes host-level access
4. Clarity: Makes dependencies explicit and prevents accidental coupling

CORRECT patterns:
    - Import from within sandbox/ (e.g., from llm import run_agent)
    - Import from sandbox/egg-tasks/analysis/utilities/ for shared utilities
    - Standard library and pip-installed packages

INCORRECT patterns:
    - Importing from host-services/ directory
    - Using sys.path manipulation to access host-services

This script is intended to be run as a CI check or pre-commit hook.

See: sandbox/.claude/rules/host-container-boundary.md

Usage:
    python3 scripts/check-container-host-boundary.py

Exit codes:
    0 - No forbidden imports found
    1 - Found forbidden host-services imports in sandbox
"""

import re
import sys
from pathlib import Path


def check_file_for_host_imports(file_path: Path) -> list[tuple[int, str]]:
    """Check a file for forbidden host-services imports.

    Returns:
        List of (line_number, line_content) tuples for each violation.
    """
    violations = []

    # Patterns that indicate ACTUAL importing or path usage from host-services
    # We need to catch:
    # - sys.path manipulation that adds host-services paths
    # - Dynamic imports using spec_from_file_location with host-services paths
    # - Path() construction that builds host-services paths for imports
    #
    # We do NOT want to flag:
    # - Documentation strings mentioning "host-services"
    # - Dictionary keys/values that reference "host-services" for descriptions
    # - List items naming directories for scanning/documentation purposes

    import_patterns = [
        # sys.path.insert/append that adds host-services paths
        r"sys\.path\.(insert|append)\([^)]*host-services",
        # spec_from_file_location with host-services path
        r"spec_from_file_location\([^)]*host-services",
        # Path construction for module loading: Path(...) / "host-services" / ... / ".py"
        # This catches patterns like: jib_path / "host-services" / "analysis" / "index-generator.py"
        r'Path[^=]*[/\\]\s*["\']host-services["\'].*\.py',
        # Direct path division with host-services leading to .py file
        r'/\s*["\']host-services["\'][^"\']*\.py',
    ]

    try:
        content = file_path.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines, start=1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            for pattern in import_patterns:
                if re.search(pattern, line):
                    violations.append((i, line.strip()))
                    break

    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)

    return violations


def main():
    """Run the lint check on sandbox directory."""
    # Get repo root (parent of scripts/)
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    sandbox_dir = repo_root / "sandbox"

    if not sandbox_dir.exists():
        print(f"Warning: sandbox directory not found at {sandbox_dir}")
        return 0

    # Find all Python files in sandbox
    python_files = list(sandbox_dir.rglob("*.py"))

    all_violations = []

    for file_path in python_files:
        violations = check_file_for_host_imports(file_path)
        if violations:
            rel_path = file_path.relative_to(repo_root)
            all_violations.append((rel_path, violations))

    # Report results
    if all_violations:
        print("ERROR: Found forbidden host-services imports in sandbox!\n")
        print("=" * 70)
        print("ARCHITECTURE VIOLATION: Container code MUST NOT import from host-services.")
        print()
        print("Why this matters:")
        print("  - Container code is baked into Docker images; host-services is not")
        print("  - Container should be self-contained and portable")
        print("  - Prevents accidental coupling between security domains")
        print("=" * 70)
        print()

        for file_path, violations in all_violations:
            print(f"File: {file_path}")
            for line_num, line_content in violations:
                print(f"  Line {line_num}: {line_content}")
            print()

        print("How to fix:")
        print("  1. If the utility is container-only, move it to sandbox/")
        print("     (e.g., sandbox/egg-tasks/analysis/utilities/)")
        print("  2. If code needs to be shared, consider if it truly belongs in both places")
        print("  3. Use direct imports from the container-local path")
        print()
        print("Documentation: sandbox/.claude/rules/host-container-boundary.md")
        print()

        return 1
    else:
        print("OK: No forbidden host-services imports found in sandbox")
        return 0


if __name__ == "__main__":
    sys.exit(main())

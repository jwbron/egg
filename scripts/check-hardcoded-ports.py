#!/usr/bin/env python3
"""
Lint check: Detect hardcoded gateway/proxy port numbers in code.

Port values should be imported from shared/egg_config/constants.py, not
hardcoded throughout the codebase. This prevents port drift and makes
the configuration easier to maintain.

Approved locations for port definitions:
- shared/egg_config/constants.py (the source of truth)
- gateway/*.sh (shell scripts that cannot import Python modules)
- gateway/squid.conf.template (proxy configuration)
- .github/workflows/*.yml (CI/CD workflows)

All other code should import GATEWAY_PORT and GATEWAY_PROXY_PORT from
egg_config.constants or egg_config (which re-exports them).

Usage:
    python3 scripts/check-hardcoded-ports.py

Exit codes:
    0 - No violations found
    1 - Found hardcoded port numbers
"""

import re
import sys
from pathlib import Path

# Ports to check for
GATEWAY_PORT = 9848
PROXY_PORT = 3129

# Files/directories where hardcoded ports are allowed
ALLOWLIST_PATHS = [
    # Source of truth
    "shared/egg_config/constants.py",
    # Shell scripts cannot import Python modules
    "gateway/entrypoint.sh",
    "gateway/start-gateway.sh",
    "gateway/setup.sh",
    # Gateway Python module has its own DEFAULT_PORT (source of truth for gateway)
    "gateway/gateway.py",
    # Gateway tests may need hardcoded values
    "gateway/tests/",
    # Squid configuration template
    "gateway/squid.conf.template",
    "gateway/squid.conf",
    # Docker compose files
    "docker-compose.yml",
    "docker-compose.yaml",
    # Integration test infrastructure (compose files, conftest, network tests)
    "integration_tests/conftest.py",
    "integration_tests/docker-compose.yml",
    "integration_tests/test_network_",
    # CI/CD workflows (YAML cannot import Python)
    ".github/workflows/",
    # This lint script itself
    "scripts/check-hardcoded-ports.py",
    # Documentation is allowed to have examples
    "docs/",
    # Test data and fixtures (mock data may need specific values)
    "tests/fixtures/",
    # Test files that validate config defaults or use hardcoded values in assertions
    "tests/egg_config/test_configs.py",
    "tests/functional/conftest.py",
    "tests/shared/egg_container/test_build_cmd.py",
    # Integration tests that need hardcoded values
    "integration_tests/test_network_isolation.py",
    "integration_tests/test_network_security.py",
    # Contract state files (generated JSON)
    ".egg-state/",
]

# Additional patterns that are allowed (e.g., version numbers, line numbers)
ALLOWLIST_PATTERNS = [
    # Port in a comment explaining the constant
    r"#.*GATEWAY_PORT.*=.*9848",
    r"#.*GATEWAY_PROXY_PORT.*=.*3129",
    # Port as part of a larger number (e.g., 39848, 19848)
    r"\d{5,}",
]


def is_allowlisted(file_path: Path, repo_root: Path) -> bool:
    """Check if file is in the allowlist."""
    rel_path = str(file_path.relative_to(repo_root))

    for allowed in ALLOWLIST_PATHS:
        if allowed.endswith("/"):
            if rel_path.startswith(allowed):
                return True
        else:
            if rel_path == allowed:
                return True

    return False


def check_line(line: str, lineno: int) -> list[tuple[int, str, str]]:
    """Check a line for hardcoded ports.

    Returns list of (lineno, port_found, context) tuples.
    """
    violations = []

    # Skip comment-only lines and noqa lines
    stripped = line.strip()
    if stripped.startswith("#") or stripped.startswith("//"):
        return violations
    if "noqa: EGG002" in line or "noqa: hardcoded-port" in line:
        return violations

    # Check for allowlisted patterns
    for pattern in ALLOWLIST_PATTERNS:
        if re.search(pattern, line):
            return violations

    # Check for gateway port
    if str(GATEWAY_PORT) in line:
        # Make sure it's not part of a larger number
        if re.search(rf"(?<!\d){GATEWAY_PORT}(?!\d)", line):
            violations.append((lineno, str(GATEWAY_PORT), line.strip()))

    # Check for proxy port
    if str(PROXY_PORT) in line:
        if re.search(rf"(?<!\d){PROXY_PORT}(?!\d)", line):
            violations.append((lineno, str(PROXY_PORT), line.strip()))

    return violations


def check_file(file_path: Path) -> list[tuple[int, str, str]]:
    """Check a file for hardcoded ports."""
    violations = []

    try:
        content = file_path.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines, 1):
            line_violations = check_line(line, i)
            violations.extend(line_violations)

    except UnicodeDecodeError:
        pass  # Skip binary files
    except Exception as e:
        print(f"Warning: Could not check {file_path}: {e}", file=sys.stderr)

    return violations


def main() -> int:
    """Run the hardcoded ports lint check."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    # File extensions to check
    extensions = {".py", ".sh", ".ts", ".js", ".tsx", ".jsx", ".json", ".yml", ".yaml"}

    all_violations: list[tuple[Path, list[tuple[int, str, str]]]] = []

    # Walk the repository
    for file_path in repo_root.rglob("*"):
        # Skip directories
        if file_path.is_dir():
            continue

        # Skip files not in our extension list
        if file_path.suffix.lower() not in extensions:
            continue

        # Skip files in .git, .venv, node_modules
        rel_parts = file_path.relative_to(repo_root).parts
        if any(
            part in {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache"}
            for part in rel_parts
        ):
            continue

        # Skip allowlisted files
        if is_allowlisted(file_path, repo_root):
            continue

        violations = check_file(file_path)
        if violations:
            rel_path = file_path.relative_to(repo_root)
            all_violations.append((rel_path, violations))

    if all_violations:
        print("ERROR: Found hardcoded gateway/proxy port numbers!\n")
        print("=" * 76)
        print("Port values should be imported from shared/egg_config/constants.py")
        print("to ensure consistency across the codebase.")
        print()
        print("Approved sources of truth:")
        print("  - shared/egg_config/constants.py (Python code)")
        print("  - gateway/*.sh (shell scripts document they match Python constants)")
        print("=" * 76)
        print()

        for file_path, violations in sorted(all_violations):
            print(f"File: {file_path}")
            for lineno, port, context in violations:
                print(f"  Line {lineno}: Found port {port}")
                print(f"    {context}")
            print()

        print("How to fix:")
        print("  1. For Python code, import from egg_config:")
        print("     from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT")
        print()
        print("  2. For shell scripts in sandbox/, use GATEWAY_URL env var:")
        print("     GATEWAY_URL is set by the container launcher")
        print()
        print("  3. For test code, use TEST_GATEWAY_PORT from egg_config.constants")
        print()
        print("  4. To suppress a false positive, add: # noqa: EGG002")
        print()

        return 1
    else:
        print("OK: No hardcoded port numbers found")
        return 0


if __name__ == "__main__":
    sys.exit(main())

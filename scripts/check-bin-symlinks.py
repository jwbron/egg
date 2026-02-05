#!/usr/bin/env python3
"""
Lint check: Validate sandbox/bin/ symlinks point to existing targets.

The sandbox/bin/ directory contains symlinks to gateway wrapper scripts in
sandbox/scripts/. These wrappers redirect git/gh operations through the
gateway sidecar for policy enforcement (branch ownership, merge blocking).

If a symlink is broken (target missing), git/gh commands inside the container
will fail silently or with confusing errors.

This script is intended to be run as a CI check or pre-commit hook.

Usage:
    python3 scripts/check-bin-symlinks.py

Exit codes:
    0 - All symlinks are valid
    1 - Found broken symlinks or missing expected entries
"""

import sys
from pathlib import Path

# Expected symlinks in sandbox/bin/ and their targets (relative to sandbox/bin/)
EXPECTED_SYMLINKS = {
    "gh": "../scripts/gh",
    "git": "../scripts/git",
    "git-credential-github-token": "../scripts/git-credential-github-token",
}


def main():
    """Validate sandbox/bin/ symlinks."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    bin_dir = repo_root / "sandbox" / "bin"

    if not bin_dir.exists():
        print(f"Warning: sandbox/bin/ directory not found at {bin_dir}")
        return 0

    errors = []

    # Check expected symlinks exist and point to valid targets
    for name, expected_target in EXPECTED_SYMLINKS.items():
        link_path = bin_dir / name

        if not link_path.exists() and not link_path.is_symlink():
            errors.append(f"Missing: sandbox/bin/{name} (expected -> {expected_target})")
            continue

        if not link_path.is_symlink():
            errors.append(
                f"Not a symlink: sandbox/bin/{name} (expected symlink -> {expected_target})"
            )
            continue

        actual_target = str(link_path.readlink())
        if actual_target != expected_target:
            errors.append(
                f"Wrong target: sandbox/bin/{name} -> {actual_target} "
                f"(expected -> {expected_target})"
            )
            continue

        # Verify the resolved target actually exists
        resolved = link_path.resolve()
        if not resolved.exists():
            errors.append(
                f"Broken symlink: sandbox/bin/{name} -> {expected_target} "
                f"(target does not exist at {resolved})"
            )
            continue

    # Check for unexpected entries in sandbox/bin/
    for entry in sorted(bin_dir.iterdir()):
        if entry.name not in EXPECTED_SYMLINKS:
            errors.append(f"Unexpected entry: sandbox/bin/{entry.name}")

    if errors:
        print("ERROR: sandbox/bin/ symlink validation failed!\n")
        print("=" * 60)
        print("Container bin symlinks must point to valid gateway wrappers.")
        print("=" * 60)
        print()
        for error in errors:
            print(f"  {error}")
        print()
        print("How to fix:")
        print("  Recreate symlinks in sandbox/bin/:")
        for name, target in EXPECTED_SYMLINKS.items():
            print(f"    ln -sf {target} sandbox/bin/{name}")
        print()
        return 1

    print("OK: All sandbox/bin/ symlinks are valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())

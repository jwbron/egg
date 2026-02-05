#!/usr/bin/env python3
"""
Lint check: Validate sandbox/bin/ symlinks are correct.

The sandbox/bin/ directory contains symlinks that redirect git and gh
commands through the gateway sidecar for policy enforcement. These symlinks
are critical for security:

1. git → ../scripts/git (gateway wrapper)
2. gh → ../scripts/gh (gateway wrapper)
3. git-credential-github-token → ../scripts/git-credential-github-token

If these symlinks are broken, missing, or point to wrong targets,
the gateway policy enforcement is bypassed and containers can access
GitHub directly without branch ownership checks or merge blocking.

This script is intended to be run as a CI check or pre-commit hook.

Usage:
    python3 scripts/check-bin-symlinks.py

Exit codes:
    0 - All symlinks are valid
    1 - Found broken, missing, or incorrect symlinks
"""

import sys
from pathlib import Path

# Expected symlinks in sandbox/bin/
# Format: {name: expected_target}
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
        print(f"ERROR: sandbox/bin/ directory not found at {bin_dir}")
        return 1

    errors = []

    for name, expected_target in EXPECTED_SYMLINKS.items():
        symlink_path = bin_dir / name

        if not symlink_path.exists() and not symlink_path.is_symlink():
            errors.append(f"  MISSING: {name} (expected symlink to {expected_target})")
            continue

        if not symlink_path.is_symlink():
            errors.append(
                f"  NOT A SYMLINK: {name} is a regular file, should be a symlink to {expected_target}"
            )
            continue

        actual_target = str(symlink_path.readlink())
        if actual_target != expected_target:
            errors.append(f"  WRONG TARGET: {name} -> {actual_target} (expected {expected_target})")
            continue

        # Verify the target actually exists (resolve relative to symlink location)
        resolved = (bin_dir / expected_target).resolve()
        if not resolved.exists():
            errors.append(
                f"  BROKEN: {name} -> {expected_target} (target does not exist at {resolved})"
            )
            continue

    # Check for unexpected files in sandbox/bin/
    for entry in sorted(bin_dir.iterdir()):
        if entry.name not in EXPECTED_SYMLINKS:
            errors.append(f"  UNEXPECTED: {entry.name} (not in expected symlinks list)")

    if errors:
        print("ERROR: sandbox/bin/ symlink validation failed!\n")
        print("=" * 70)
        print("SECURITY: These symlinks route git/gh through the gateway sidecar.")
        print("Broken or missing symlinks bypass policy enforcement.")
        print("=" * 70)
        print()
        for error in errors:
            print(error)
        print()
        print("How to fix:")
        print("  cd sandbox/bin/")
        print("  ln -sf ../scripts/gh gh")
        print("  ln -sf ../scripts/git git")
        print("  ln -sf ../scripts/git-credential-github-token git-credential-github-token")
        print()
        return 1
    else:
        print("OK: All sandbox/bin/ symlinks are valid")
        return 0


if __name__ == "__main__":
    sys.exit(main())

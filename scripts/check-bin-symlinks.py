#!/usr/bin/env python3
"""
Lint check: Validate bin/ symlinks point to existing targets.

The bin/ directory contains convenience symlinks to commands in gateway/
and sandbox/. Broken symlinks cause confusing runtime failures.

This script checks that:
1. All symlinks in bin/ resolve to existing files
2. Symlink targets are within the repository (no external references)

Usage:
    python3 scripts/check-bin-symlinks.py

Exit codes:
    0 - All symlinks are valid
    1 - Found broken or invalid symlinks
"""

import sys
from pathlib import Path


def main() -> int:
    """Validate all symlinks in the bin/ directory."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    bin_dir = repo_root / "bin"

    if not bin_dir.exists():
        print("Warning: bin/ directory not found")
        return 0

    violations: list[tuple[str, str]] = []
    checked = 0

    for entry in sorted(bin_dir.iterdir()):
        # Skip non-symlinks (e.g., README.md)
        if not entry.is_symlink():
            continue

        checked += 1
        target = entry.readlink()

        # Resolve relative to bin/ directory
        resolved = (bin_dir / target).resolve()

        if not resolved.exists():
            violations.append((entry.name, f"broken symlink -> {target} (target does not exist)"))
            continue

        # Ensure target resolves within the repo
        try:
            resolved.relative_to(repo_root.resolve())
        except ValueError:
            violations.append((entry.name, f"symlink target is outside repository -> {resolved}"))

    if violations:
        print("ERROR: Found invalid symlinks in bin/\n")
        print("=" * 70)
        print("INTEGRITY VIOLATION: All bin/ symlinks must point to existing files")
        print("within the repository.")
        print("=" * 70)
        print()

        for name, reason in violations:
            print(f"  bin/{name}: {reason}")

        print()
        print("How to fix:")
        print("  1. If the target was moved, update the symlink:")
        print("     cd bin && ln -sf ../new/path command-name")
        print("  2. If the target was removed, delete the symlink:")
        print("     rm bin/command-name")
        print("  3. Update bin/README.md if the command list changed")
        print()

        return 1
    else:
        print(f"OK: All {checked} bin/ symlinks are valid")
        return 0


if __name__ == "__main__":
    sys.exit(main())

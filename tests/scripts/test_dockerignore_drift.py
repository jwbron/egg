"""Drift guard for the two .dockerignore files.

Per-Dockerfile `<Dockerfile>.dockerignore` (BuildKit) does NOT merge with the
root `.dockerignore` — when both exist, BuildKit reads only the per-Dockerfile
one for that build. `sandbox/Dockerfile.dockerignore` mirrors the root file
MINUS the single `repo-deps/` exclusion (the sandbox build uniquely needs that
directory in context for `COPY repo-deps/ /tmp/repo-deps/`).

If a future change adds an exclusion to the root file but forgets the sandbox
override, the sandbox build context silently bloats — undoing the
1.91 GB → 23 MB savings the override exists to preserve. This test mechanically
enforces the KEEP-IN-SYNC contract documented in the comment headers of both
files (see #2999).
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
ROOT_DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
SANDBOX_DOCKERIGNORE = PROJECT_ROOT / "sandbox" / "Dockerfile.dockerignore"

# The single line that legitimately differs between the two files. The sandbox
# build needs repo-deps/ in its context; every other build context excludes it.
ALLOWED_SANDBOX_DELTA: set[str] = {"repo-deps/"}


def _read_patterns(path: Path) -> set[str]:
    """Return the set of effective pattern lines (skip blanks and comments)."""
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_sandbox_dockerignore_mirrors_root() -> None:
    """sandbox/Dockerfile.dockerignore must equal root .dockerignore minus repo-deps/."""
    root = _read_patterns(ROOT_DOCKERIGNORE)
    sandbox = _read_patterns(SANDBOX_DOCKERIGNORE)

    missing_from_sandbox = (root - sandbox) - ALLOWED_SANDBOX_DELTA
    extra_in_sandbox = sandbox - root

    assert not missing_from_sandbox, (
        "Drift between root .dockerignore and sandbox/Dockerfile.dockerignore: "
        f"these patterns are in the root file but missing from the sandbox "
        f"override: {sorted(missing_from_sandbox)}. Add them to "
        "sandbox/Dockerfile.dockerignore or update ALLOWED_SANDBOX_DELTA in "
        "this test if the divergence is intentional. See the KEEP-IN-SYNC "
        "comment in both files."
    )
    assert not extra_in_sandbox, (
        "Drift between root .dockerignore and sandbox/Dockerfile.dockerignore: "
        f"these patterns are in the sandbox override but not in the root file: "
        f"{sorted(extra_in_sandbox)}. The sandbox override should be a strict "
        "subset of the root file (minus repo-deps/)."
    )

    assert ALLOWED_SANDBOX_DELTA.issubset(root), (
        f"ALLOWED_SANDBOX_DELTA expects {sorted(ALLOWED_SANDBOX_DELTA)} to be "
        "excluded by the root .dockerignore. If the sandbox-only inclusion no "
        "longer applies, delete sandbox/Dockerfile.dockerignore entirely "
        "instead of leaving it out of sync."
    )
    assert not ALLOWED_SANDBOX_DELTA & sandbox, (
        f"sandbox/Dockerfile.dockerignore must NOT exclude "
        f"{sorted(ALLOWED_SANDBOX_DELTA)} — the sandbox build needs it in "
        "context (`COPY repo-deps/ /tmp/repo-deps/`)."
    )

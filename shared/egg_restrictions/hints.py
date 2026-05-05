"""Actionable hints for path-restricted git push denials.

When the gateway rejects a push because a file violates the pushing role's
write boundary, the response carries a ``hint`` field meant to point the
operator at the right remediation channel. The mapping below is keyed on
the *blocked path's category* (not the role), so the same hint applies
regardless of which role attempted the push — every role blocked from
``.egg-state/contracts/`` should be told about the egg-contract CLI, not
just the implementer.

Patterns are evaluated top-to-bottom against
:func:`egg_restrictions.matchers.match_pattern`; the first row whose
glob matches at least one blocked path wins. The list is ordered
most-specific first so that if a future broader pattern (e.g. a
catch-all ``.egg-state/`` row) is added at the end, it does not shadow
the specific anchors / contracts / drafts / reviews hints above it.
"""

from __future__ import annotations

from .matchers import match_pattern

__all__ = ["BLOCKED_HINTS", "derive_hint"]


BLOCKED_HINTS: list[tuple[str, str]] = [
    (
        ".egg-state/contracts/",
        "Use egg-contract CLI commands to update contract state.",
    ),
    (
        ".egg-state/agent-anchors/",
        "Anchor writes go through the orchestrator API (`mcp__sdlc__update_anchor`), not git push.",
    ),
    (
        ".egg-state/drafts/",
        "These directories are owned by a different agent role for this phase.",
    ),
    (
        ".egg-state/reviews/",
        "These directories are owned by a different agent role for this phase.",
    ),
    (
        ".github/",
        "These paths are owned by infrastructure; ask a human reviewer to merge changes here.",
    ),
    (
        "docs/",
        "Documentation changes belong to the documenter role.",
    ),
    # `**/*.md` covers any depth, including top-level (e.g. `README.md`). A
    # narrower `**/README.md` row would always be shadowed here, so it's
    # omitted.
    (
        "**/*.md",
        "Documentation changes belong to the documenter role.",
    ),
    # Test directory globs. The `**/<dir>/` form matches both top-level
    # (`tests/foo.py`) and nested (`gateway/tests/foo.py`) paths via the
    # path-segment match in `match_pattern`, so bare `tests/` / `test/`
    # rows would be redundant.
    (
        "**/tests/",
        "Test changes belong to the tester role.",
    ),
    (
        "**/test/",
        "Test changes belong to the tester role.",
    ),
    # Python test file globs.
    (
        "**/test_*.py",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*_test.py",
        "Test changes belong to the tester role.",
    ),
    (
        "**/conftest.py",
        "Test changes belong to the tester role.",
    ),
    # Go test file globs (mirror TESTER_PATTERNS in patterns.py).
    (
        "**/*_test.go",
        "Test changes belong to the tester role.",
    ),
    (
        "**/test_*.go",
        "Test changes belong to the tester role.",
    ),
    # JS/TS test file globs (mirror TESTER_PATTERNS in patterns.py).
    (
        "**/*.test.ts",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*.test.tsx",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*.test.js",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*.test.jsx",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*.spec.ts",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*.spec.tsx",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*.spec.js",
        "Test changes belong to the tester role.",
    ),
    (
        "**/*.spec.jsx",
        "Test changes belong to the tester role.",
    ),
]


def derive_hint(blocked_files: list[str]) -> str | None:
    """Pick the first hint whose glob matches any of ``blocked_files``.

    Returns ``None`` when no row matches; callers should fall back to the
    role-scope ``blocked_reason`` already carried on the error response
    rather than substituting a misleading default.
    """
    if not blocked_files:
        return None
    for pattern, hint in BLOCKED_HINTS:
        for path in blocked_files:
            if match_pattern(path, pattern):
                return hint
    return None

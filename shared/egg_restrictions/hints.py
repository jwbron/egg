"""Actionable hints for path-restricted git push denials.

When the gateway rejects a push because a file violates the pushing role's
write boundary, the response carries a ``hint`` field meant to point the
operator at the right remediation channel. The mapping below is keyed on
the *blocked path's category* (not the role), so the same hint applies
regardless of which role attempted the push — every role blocked from
``.egg-state/contracts/`` should be told about the egg-contract CLI, not
just the implementer.

Patterns are evaluated top-to-bottom against
``AgentFilePattern.matches_pattern``; the first row whose glob matches at
least one blocked path wins. The list is therefore ordered most-specific
first. Order matters where prefixes overlap — the
``.egg-state/agent-anchors/`` row must appear above any broader
``.egg-state/`` row.
"""

from __future__ import annotations

from .patterns import AgentFilePattern

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
    (
        "**/*.md",
        "Documentation changes belong to the documenter role.",
    ),
    (
        "**/README.md",
        "Documentation changes belong to the documenter role.",
    ),
    (
        "tests/",
        "Test changes belong to the tester role.",
    ),
    (
        "test/",
        "Test changes belong to the tester role.",
    ),
    (
        "**/tests/",
        "Test changes belong to the tester role.",
    ),
    (
        "**/test/",
        "Test changes belong to the tester role.",
    ),
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
            if AgentFilePattern.matches_pattern(path, pattern):
                return hint
    return None

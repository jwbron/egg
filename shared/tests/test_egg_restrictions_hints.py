"""Tests for egg_restrictions.hints — push-denial hint derivation (#2355)."""

from __future__ import annotations

import pytest
from egg_restrictions import BLOCKED_HINTS, derive_hint
from egg_restrictions.patterns import AgentFilePattern


def test_no_blocked_files_returns_none() -> None:
    assert derive_hint([]) is None


def test_unmatched_path_returns_none() -> None:
    # No row in BLOCKED_HINTS covers a top-level source file under gateway/,
    # so the helper falls back to None — caller uses blocked_reason instead.
    assert derive_hint(["gateway/foo.py"]) is None


@pytest.mark.parametrize(
    ("blocked_path", "expected_substring"),
    [
        (".egg-state/contracts/foo.json", "egg-contract CLI"),
        (".egg-state/agent-anchors/coder.json", "mcp__sdlc__update_anchor"),
        (".egg-state/drafts/plan.md", "different agent role"),
        (".egg-state/reviews/code.md", "different agent role"),
        (".github/workflows/ci.yml", "owned by infrastructure"),
        (".github/CODEOWNERS", "owned by infrastructure"),
        ("docs/index.md", "documenter role"),
        ("README.md", "documenter role"),
        ("orchestrator/foo/README.md", "documenter role"),
        # Test directory globs — `**/<dir>/` covers top-level and nested.
        ("tests/test_foo.py", "tester role"),
        ("test/test_foo.py", "tester role"),
        ("gateway/tests/test_gateway.py", "tester role"),
        # Python test file globs.
        ("orchestrator/test_foo.py", "tester role"),
        ("orchestrator/foo_test.py", "tester role"),
        ("conftest.py", "tester role"),
        # Go test file globs.
        ("internal/foo_test.go", "tester role"),
        ("internal/test_foo.go", "tester role"),
        # JS/TS test file globs (Jest / Vitest conventions).
        ("frontend/foo.test.ts", "tester role"),
        ("frontend/foo.test.tsx", "tester role"),
        ("frontend/foo.test.js", "tester role"),
        ("frontend/foo.test.jsx", "tester role"),
        ("frontend/foo.spec.ts", "tester role"),
        ("frontend/foo.spec.tsx", "tester role"),
        ("frontend/foo.spec.js", "tester role"),
        ("frontend/foo.spec.jsx", "tester role"),
    ],
)
def test_each_category_yields_its_hint(blocked_path: str, expected_substring: str) -> None:
    hint = derive_hint([blocked_path])
    assert hint is not None
    assert expected_substring in hint


def test_first_match_wins_when_multiple_files_blocked() -> None:
    # The contracts row is listed before the .github row, so when a single
    # push violates both, the contracts hint is returned.
    hint = derive_hint([".github/workflows/ci.yml", ".egg-state/contracts/foo.json"])
    assert hint is not None
    assert "egg-contract CLI" in hint


def test_anchor_pattern_does_not_match_sibling_egg_state_paths() -> None:
    # The `.egg-state/agent-anchors/` row is a sibling-prefix to the
    # contracts/drafts/reviews rows — none of the four patterns should match
    # an anchor path except the anchor row itself. Without this property,
    # ordering of the four rows in BLOCKED_HINTS would matter; with it, the
    # anchor row's relative position is irrelevant for anchor-only paths.
    anchor_path = ".egg-state/agent-anchors/coder.json"
    sibling_patterns = [
        ".egg-state/contracts/",
        ".egg-state/drafts/",
        ".egg-state/reviews/",
    ]
    for pattern in sibling_patterns:
        assert not AgentFilePattern.matches_pattern(anchor_path, pattern), (
            f"sibling pattern {pattern!r} should not match anchor path"
        )
    assert AgentFilePattern.matches_pattern(anchor_path, ".egg-state/agent-anchors/")


def test_blocked_hints_table_is_well_formed() -> None:
    # Every entry is a (glob, hint) pair with non-empty strings.
    assert BLOCKED_HINTS, "BLOCKED_HINTS should not be empty"
    for pattern, hint in BLOCKED_HINTS:
        assert pattern, "patterns must be non-empty"
        assert hint, "hints must be non-empty"

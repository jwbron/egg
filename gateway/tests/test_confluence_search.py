"""
Tests for the conservative CQL space-scope extractor in
``gateway/confluence_search.py``.

Covers Phase 1 / Task 4-4 acceptance:

- Positive cases: ``space = KEY`` and ``space IN (KEY, ...)`` (optionally
  combined with arbitrary AND clauses).
- Adversarial / negative cases: every shape the extractor must reject —
  OR boolean, capitalisation variants, quoted keys, CQL functions,
  semicolons, comment markers, missing space clause, non-allowlisted keys,
  unicode homoglyphs, bare id / content / title clauses without a space
  anchor.
- Type errors: non-string CQL, malformed string literals.
"""

from __future__ import annotations

import pytest

# Loaded via conftest.
from confluence_search import ScopeResult, extract_search_spaces

ALLOWED = frozenset({"ENG", "DOCS"})


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


class TestPositive:
    def test_simple_space_equals(self):
        result = extract_search_spaces("space = ENG", ALLOWED)
        assert result == ScopeResult(frozenset({"ENG"}), "")

    def test_space_in_list_all_allowed(self):
        result = extract_search_spaces("space IN (ENG, DOCS)", ALLOWED)
        assert result.spaces == frozenset({"ENG", "DOCS"})
        assert result.reason == ""

    def test_space_combined_with_text_and(self):
        result = extract_search_spaces('space = ENG AND text ~ "RFC"', ALLOWED)
        assert result.spaces == frozenset({"ENG"})

    def test_space_in_lowercase_in_operator(self):
        """``space in (...)`` (lowercase ``in``) is still accepted."""
        result = extract_search_spaces("space in (ENG, DOCS)", ALLOWED)
        assert result.spaces == frozenset({"ENG", "DOCS"})

    def test_single_key_in_in_list(self):
        result = extract_search_spaces("space IN (ENG)", ALLOWED)
        assert result.spaces == frozenset({"ENG"})

    def test_space_combined_with_label(self):
        result = extract_search_spaces("space = ENG AND label = architecture", ALLOWED)
        assert result.spaces == frozenset({"ENG"})


# ---------------------------------------------------------------------------
# Adversarial / negative cases
# ---------------------------------------------------------------------------


class TestAdversarialNegatives:
    @pytest.mark.parametrize(
        "cql, expected_reason_substr",
        [
            # 1. OR boolean operator at top level.
            ("space = ENG OR space = SEC", "or"),
            # 2. OR mixing a bare id clause.
            ('space = ENG OR id = "12345"', "or"),
            # 3. Capitalisation variant.
            ("SPACE = ENG", "cannot prove"),
            # 4. Quoted key — even if allowlisted.
            ('space = "ENG"', "cannot prove"),
            # 5. CQL function on RHS.  The parser reads ``currentUser`` as
            #    a candidate space key and then rejects it via the allowlist
            #    check; the trailing ``()`` is never seen as a function call.
            #    Either ``cannot prove`` (parse-level) or ``not allowlisted``
            #    (allowlist-level) is an acceptable rejection — both fail
            #    closed.  See the non-blocking note in the search test
            #    review for hardening the parser to reject ``()`` syntax
            #    explicitly.
            ("space = currentUser()", "not allowlisted"),
            ("space = recentlyViewedContent()", "not allowlisted"),
            # 6. Semicolon / statement chaining.
            ("space = ENG ; drop table", "forbidden"),
            # 7. Block comment marker.
            ("space = ENG /* comment */", "comment"),
            # 8. Line-comment markers.
            ("space = ENG -- hidden", "comment"),
            ("space = ENG // hidden", "comment"),
            # 9. IN list containing a non-allowlisted key.
            ("space IN (ENG, SEC)", "not allowlisted"),
            # 10. Missing space clause entirely.
            ('text ~ "RFC"', "no space clause"),
            # 11. Bare id clause without a space anchor.
            ('id = "12345"', "id, content, and title clauses are not supported"),
            # 12. Bare title clause without a space anchor.
            ('title ~ "RFC"', "id, content, and title clauses are not supported"),
            # 13. Bare content clause without space anchor.
            ('content = "12345"', "id, content, and title clauses are not supported"),
            # 14. Unicode homoglyph (Cyrillic Е U+0415, Н U+041D, Г U+0413).
            ("space = ЕНG", "non-ASCII"),
            # 15. Negated comparator (extractor cannot prove containment).
            ("space != SEC", "cannot prove"),
            # 16. Wildcard comparator on space.
            ("space ~ ENG", "cannot prove"),
            # 17. Empty / whitespace-only.
            ("", "empty"),
            ("   \t  ", "empty"),
        ],
    )
    def test_rejected_with_reason(self, cql: str, expected_reason_substr: str):
        result = extract_search_spaces(cql, ALLOWED)
        assert result.spaces is None, f"{cql!r} should have been rejected"
        assert expected_reason_substr.lower() in result.reason.lower(), (
            f"Expected reason to mention {expected_reason_substr!r}; got {result.reason!r}"
        )


# ---------------------------------------------------------------------------
# Type errors
# ---------------------------------------------------------------------------


class TestTypeErrors:
    def test_non_string_rejected(self):
        result = extract_search_spaces(None, ALLOWED)  # type: ignore[arg-type]
        assert result.spaces is None
        assert "string" in result.reason.lower()

    def test_int_rejected(self):
        result = extract_search_spaces(42, ALLOWED)  # type: ignore[arg-type]
        assert result.spaces is None

    def test_mismatched_string_literal_rejected(self):
        """A stray quote with no close is a parse error — reject."""
        result = extract_search_spaces('space = ENG AND text ~ "unclosed', ALLOWED)
        assert result.spaces is None
        assert "malformed" in result.reason.lower() or "string" in result.reason.lower()


# ---------------------------------------------------------------------------
# Edge cases for the extractor surface
# ---------------------------------------------------------------------------


class TestExtractorEdges:
    def test_unknown_key_alone_rejected(self):
        """Allowlist must also gate the simple ``space = X`` shape."""
        result = extract_search_spaces("space = SEC", ALLOWED)
        assert result.spaces is None
        assert "not allowlisted" in result.reason.lower()

    def test_allowlist_change_invalidates_acceptance(self):
        """A previously accepted query becomes rejected when allowlist shrinks."""
        smaller = frozenset({"DOCS"})
        result = extract_search_spaces("space = ENG", smaller)
        assert result.spaces is None
        assert "not allowlisted" in result.reason.lower()

    def test_extra_whitespace_tolerated(self):
        result = extract_search_spaces("  space   =   ENG  ", ALLOWED)
        assert result.spaces == frozenset({"ENG"})

    def test_in_with_mixed_whitespace(self):
        result = extract_search_spaces("space   IN (   ENG ,    DOCS   )", ALLOWED)
        assert result.spaces == frozenset({"ENG", "DOCS"})

    def test_two_space_clauses_both_must_be_allowlisted(self):
        """Multiple ``space = K`` clauses combined with AND are accepted only
        when every key is allowlisted."""
        result = extract_search_spaces("space = ENG AND space = DOCS", ALLOWED)
        assert result.spaces == frozenset({"ENG", "DOCS"})

    def test_two_space_clauses_one_not_allowed(self):
        result = extract_search_spaces("space = ENG AND space = SEC", ALLOWED)
        assert result.spaces is None
        assert "not allowlisted" in result.reason.lower()

    def test_id_clause_rejected_even_with_space_anchor(self):
        """``id`` / ``content`` / ``title`` clauses are rejected regardless of
        any accompanying ``space`` anchor — the static extractor cannot prove
        how those filters interact with the space scope, so the conservative
        stance is to refuse them outright (the rejection message points
        agents at ``text ~ ...`` which is the supported alternative)."""
        result = extract_search_spaces('space = ENG AND id = "12345"', ALLOWED)
        assert result.spaces is None
        assert "id, content, and title" in result.reason.lower()
        assert "text ~" in result.reason.lower()

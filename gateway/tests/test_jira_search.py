"""
Tests for the conservative JQL project-scope extractor in ``gateway/jira_search.py``.

The extractor rejects any JQL it cannot statically prove is scoped to
allowlisted project keys.  This file enumerates the positive cases and the
full adversarial-suite that the plan-phase TASK-2-2 acceptance calls out.
"""

from __future__ import annotations

import pytest

# Loaded via conftest.
from jira_search import ScopeResult, extract_search_projects

ALLOWED = frozenset({"ENG", "DEVOPS"})


class TestPositive:
    """Queries the static extractor can prove scoped to allowlisted projects."""

    def test_simple_project_equals(self):
        result = extract_search_projects("project = ENG", ALLOWED)
        assert result == ScopeResult(frozenset({"ENG"}), "")

    def test_project_in_list_all_allowed(self):
        result = extract_search_projects("project in (ENG, DEVOPS)", ALLOWED)
        assert result.projects == frozenset({"ENG", "DEVOPS"})
        assert result.reason == ""

    def test_project_combined_with_status_and(self):
        result = extract_search_projects('project = ENG AND status = "Open"', ALLOWED)
        assert result.projects == frozenset({"ENG"})

    def test_project_in_uppercase_in_operator(self):
        """``project IN (...)`` with uppercase ``IN`` is still acceptable."""
        result = extract_search_projects("project IN (ENG, DEVOPS)", ALLOWED)
        assert result.projects == frozenset({"ENG", "DEVOPS"})


class TestAdversarialNegatives:
    """Every case here must be rejected with a non-empty reason."""

    @pytest.mark.parametrize(
        "jql, expected_reason_substr",
        [
            # 1. OR in project clauses: explicit rejection.
            ("project = ENG OR project = SEC", "or"),
            # 2. Mixed project + bare key scope.
            ("project = ENG OR key = SEC-1", "or"),
            # 3. Uppercase PROJECT (case-variant).
            ("PROJECT = ENG", "project"),
            # 4. Quoted project key, even when key is allowlisted.
            ('project = "ENG"', "project"),
            # 5. JQL function on the RHS.
            ("project = projectsLeadByUser()", "project"),
            # 6. Semicolon / statement chaining.
            ("project = ENG ; drop table", "forbidden"),
            # 7. Nested OR via parens.
            ("project = ENG AND (project = DEVOPS OR project = ENG)", "or"),
            # 8. IN list containing a non-allowlisted key.
            ("project IN (ENG, SEC)", "not allowlisted"),
            # 9. Missing clause entirely.
            ("status = Open", "no project clause"),
            # 10. Unicode homoglyph: Cyrillic 'Е' (U+0415) in place of 'E'.
            ("project = ЕNG", "non-ASCII"),
            # 11. JQL block comment.
            ("project = ENG /* inject */", "comment"),
            # 12. JQL line comment.
            ("project = ENG // hidden", "comment"),
            # 13. key = clause without project scope.
            ('key = "ENG-1"', "without project scope"),
            # 14. Not equal / negated comparator on project — extractor rejects.
            ("project != SEC", "cannot prove"),
            # 15. Empty JQL.
            ("", "empty"),
            # 16. Wildcard-style comparator.
            ("project ~ ENG", "cannot prove"),
        ],
    )
    def test_rejected_with_reason(self, jql: str, expected_reason_substr: str):
        result = extract_search_projects(jql, ALLOWED)
        assert result.projects is None, f"{jql!r} should have been rejected"
        assert expected_reason_substr.lower() in result.reason.lower(), (
            f"Expected reason to mention {expected_reason_substr!r}; got {result.reason!r}"
        )


class TestTypeErrors:
    def test_non_string_rejected(self):
        result = extract_search_projects(None, ALLOWED)  # type: ignore[arg-type]
        assert result.projects is None

    def test_mismatched_string_literal_rejected(self):
        """A stray quote with no close is a parse error — reject."""
        result = extract_search_projects('project = ENG AND summary ~ "unclosed', ALLOWED)
        assert result.projects is None


class TestCaseSensitivityOfIn:
    def test_lowercase_in_accepted(self):
        """``project in (...)`` must be accepted (plan-phase pattern is
        ``project IN (...)`` but lowercase ``in`` is valid JQL)."""
        result = extract_search_projects("project in (ENG, DEVOPS)", ALLOWED)
        assert result.projects == frozenset({"ENG", "DEVOPS"})

"""Review-graph wiring for the security / concurrency lens reviewers.

Asserts the four lens edges added to ``get_default_implement_graph()``:

- ``("reviewer_security", "coder", CRITICAL)``
- ``("reviewer_security", "tester", CRITICAL)``
- ``("reviewer_concurrency", "coder", CRITICAL)``
- ``("reviewer_concurrency", "tester", CRITICAL)``

…and that the existing CRITICAL edges
(``reviewer_code → coder/tester``, ``reviewer_contract → coder``,
``tester → coder``) are unchanged.

Issue #2139 promoted both lens reviewers from ADVISORY to CRITICAL: a
NACK from either now blocks consensus until the producer re-proposes.
"""

from __future__ import annotations

from review_graph import (
    ReviewCriticality,
    get_default_implement_graph,
)


class TestLensReviewersCriticalEdges:
    def test_security_reviews_coder_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("reviewer_security", "coder")
        assert edge is not None, "reviewer_security → coder edge missing"
        assert edge.criticality is ReviewCriticality.CRITICAL

    def test_security_reviews_tester_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("reviewer_security", "tester")
        assert edge is not None, "reviewer_security → tester edge missing"
        assert edge.criticality is ReviewCriticality.CRITICAL

    def test_concurrency_reviews_coder_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("reviewer_concurrency", "coder")
        assert edge is not None, "reviewer_concurrency → coder edge missing"
        assert edge.criticality is ReviewCriticality.CRITICAL

    def test_concurrency_reviews_tester_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("reviewer_concurrency", "tester")
        assert edge is not None, "reviewer_concurrency → tester edge missing"
        assert edge.criticality is ReviewCriticality.CRITICAL


class TestLensReviewersInReviewersForProducer:
    def test_coder_reviewers_include_lens(self) -> None:
        graph = get_default_implement_graph()
        reviewers = graph.reviewers_for("coder")
        assert "reviewer_security" in reviewers
        assert "reviewer_concurrency" in reviewers

    def test_tester_reviewers_include_lens(self) -> None:
        graph = get_default_implement_graph()
        reviewers = graph.reviewers_for("tester")
        assert "reviewer_security" in reviewers
        assert "reviewer_concurrency" in reviewers

    def test_critical_reviewers_for_coder_include_lens(self) -> None:
        graph = get_default_implement_graph()
        critical = graph.critical_reviewers_for("coder")
        assert "reviewer_security" in critical
        assert "reviewer_concurrency" in critical

    def test_critical_reviewers_for_tester_include_lens(self) -> None:
        graph = get_default_implement_graph()
        critical = graph.critical_reviewers_for("tester")
        assert "reviewer_security" in critical
        assert "reviewer_concurrency" in critical


class TestExistingCriticalEdgesUnchanged:
    """Regression guard: previous CRITICAL edges must remain CRITICAL."""

    def test_reviewer_code_coder_still_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("reviewer_code", "coder")
        assert edge is not None
        assert edge.criticality is ReviewCriticality.CRITICAL

    def test_reviewer_code_tester_still_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("reviewer_code", "tester")
        assert edge is not None
        assert edge.criticality is ReviewCriticality.CRITICAL

    def test_reviewer_contract_coder_still_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("reviewer_contract", "coder")
        assert edge is not None
        assert edge.criticality is ReviewCriticality.CRITICAL

    def test_tester_coder_still_critical(self) -> None:
        graph = get_default_implement_graph()
        edge = graph.get_edge("tester", "coder")
        assert edge is not None
        assert edge.criticality is ReviewCriticality.CRITICAL


class TestLensReviewersDoNotReviewDocumenter:
    """Lens edges cover coder + tester only; documenter is unaffected."""

    def test_documenter_reviewer_security_absent(self) -> None:
        graph = get_default_implement_graph()
        assert graph.get_edge("reviewer_security", "documenter") is None

    def test_documenter_reviewer_concurrency_absent(self) -> None:
        graph = get_default_implement_graph()
        assert graph.get_edge("reviewer_concurrency", "documenter") is None


class TestLensReviewersOnlyOnImplementGraph:
    """The lens edges live on the implement graph, not on plan or refine."""

    def test_lens_reviewers_absent_from_plan_graph(self) -> None:
        from review_graph import get_default_plan_graph

        plan_graph = get_default_plan_graph()
        roles = plan_graph.all_roles()
        assert "reviewer_security" not in roles
        assert "reviewer_concurrency" not in roles

    def test_lens_reviewers_absent_from_refine_graph(self) -> None:
        from review_graph import get_default_refine_graph

        refine_graph = get_default_refine_graph()
        roles = refine_graph.all_roles()
        assert "reviewer_security" not in roles
        assert "reviewer_concurrency" not in roles

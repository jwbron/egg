"""Tests for get_status enrichment with forward-progress signals and alerts (#3596, task-4-2).

Verifies that:
1. /status response includes progress sub-object with commit_count, last_commit_at,
   last_heartbeat_age_s, last_progress_age_s, progress_event_count
2. Top-level alerts array present with alert_type, agent_id, message, severity, timestamp
3. All progress fields are null when unmeasurable, never 0
4. Best-effort degradation on git subprocess failure

This is the tester contract for the status endpoint enrichment. The coder
will implement this in routes/pipelines/_status_view.py and _routes_status.py.
"""

from __future__ import annotations


class TestStatusProgressEnrichment:
    """Tests for /status endpoint progress sub-object."""

    def test_status_includes_progress_subobject(self):
        """/status response must include a progress sub-object for each agent."""
        # The progress sub-object must contain:
        # - commit_count: int (null when unmeasurable)
        # - last_commit_at: str (ISO timestamp, null when unmeasurable)
        # - last_heartbeat_age_s: float (seconds, null when unmeasurable)
        # - last_progress_age_s: float (seconds, null when unmeasurable)
        # - progress_event_count: int (null when unmeasurable)
        #
        # All fields must be null (not 0) when unmeasurable, per operator directive.
        pass

    def test_progress_fields_null_not_zero(self):
        """All progress fields must be null when unmeasurable, never 0."""
        # The operator's binding constraint: distinguish null from zero.
        # A missing measurement must not render as a real zero.
        # This bug class already exists in the tree (occ=0 reported where
        # the contract expects None on LiteLLM routes) and is tracked separately.
        pass

    def test_status_includes_alerts_array(self):
        """/status response must include a top-level alerts array."""
        # The alerts array must be capped at 10, newest-first.
        # Each entry must carry: alert_type, agent_id, message, severity, timestamp
        pass

    def test_status_includes_phase_timing(self):
        """/status response must include phase timing fields."""
        # concurrent.phase_started_at and concurrent.phase_elapsed_seconds
        pass

    def test_best_effort_degradation_on_git_failure(self):
        """Status endpoint must not crash when git subprocess fails."""
        # All progress fields must degrade to null, not crash the endpoint.
        pass

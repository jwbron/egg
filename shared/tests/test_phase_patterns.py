"""Unit tests for the phase-layer file patterns (#2968).

The companion parity test in ``gateway/tests/test_phase_filter_restrictions.py``
asserts this module stays equivalent to the live gateway config; these tests
pin the behaviour the ``check_file_restriction`` MCP tool relies on.
"""

from __future__ import annotations

from egg_restrictions.phase_patterns import PHASE_FILE_PATTERNS, phase_file_verdict


class TestPhaseFileVerdict:
    _PLAN = ".egg-state/drafts/pipeline-8cf1f000-plan.md"
    _ANALYSIS = ".egg-state/drafts/pipeline-8cf1f000-analysis.md"
    _CONTRACT = ".egg-state/contracts/pipeline-8cf1f000.json"

    def test_refine_blocks_plan_draft(self):
        allowed, reason = phase_file_verdict("refine", self._PLAN)
        assert allowed is False
        assert reason and "does not match any allowed pattern" in reason

    def test_refine_allows_analysis_draft(self):
        allowed, reason = phase_file_verdict("refine", self._ANALYSIS)
        assert allowed is True
        assert reason is None

    def test_refine_allows_contracts(self):
        assert phase_file_verdict("refine", self._CONTRACT)[0] is True

    def test_plan_allows_plan_draft(self):
        assert phase_file_verdict("plan", self._PLAN)[0] is True

    def test_plan_blocks_analysis_draft(self):
        # The inverse of refine: a plan-phase push of an analysis draft is
        # not in the plan whitelist.
        assert phase_file_verdict("plan", self._ANALYSIS)[0] is False

    def test_implement_blocks_contracts_and_drafts(self):
        assert phase_file_verdict("implement", self._CONTRACT)[0] is False
        assert phase_file_verdict("implement", self._PLAN)[0] is False

    def test_implement_allows_code(self):
        assert phase_file_verdict("implement", "src/app.py")[0] is True

    def test_case_insensitive_phase(self):
        assert phase_file_verdict("REFINE", self._PLAN)[0] is False

    def test_unknown_phase_is_unrestricted(self):
        # apply has no row, and any unknown/garbage phase string is a no-op.
        assert phase_file_verdict("apply", self._PLAN) == (True, None)
        assert phase_file_verdict("not-a-phase", self._PLAN) == (True, None)

    def test_empty_phase_is_unrestricted(self):
        assert phase_file_verdict(None, self._PLAN) == (True, None)
        assert phase_file_verdict("", self._PLAN) == (True, None)

    def test_path_escape_is_blocked(self):
        allowed, reason = phase_file_verdict("refine", "../../etc/passwd")
        assert allowed is False
        assert reason and "escapes repository" in reason


class TestPhaseFilePatternData:
    def test_only_restricted_phases_present(self):
        # pr (removed in #2777) and apply (unrestricted live) must not appear.
        assert set(PHASE_FILE_PATTERNS) == {"refine", "plan", "implement"}

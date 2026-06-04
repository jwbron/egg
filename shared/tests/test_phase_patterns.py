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

    def test_refine_blocks_contracts(self):
        # #2979: contracts mutate through the contract API, not git, so a
        # refine-phase push of a contract file is rejected at the phase layer.
        allowed, reason = phase_file_verdict("refine", self._CONTRACT)
        assert allowed is False
        assert reason and "does not match any allowed pattern" in reason

    def test_plan_allows_plan_draft(self):
        assert phase_file_verdict("plan", self._PLAN)[0] is True

    def test_plan_blocks_contracts(self):
        # #2979: same as refine — contracts are not in the plan whitelist.
        allowed, reason = phase_file_verdict("plan", self._CONTRACT)
        assert allowed is False
        assert reason and "does not match any allowed pattern" in reason

    def test_plan_blocks_analysis_draft(self):
        # The inverse of refine: a plan-phase push of an analysis draft is
        # not in the plan whitelist.
        assert phase_file_verdict("plan", self._ANALYSIS)[0] is False

    def test_implement_blocks_contracts_and_drafts(self):
        assert phase_file_verdict("implement", self._CONTRACT)[0] is False
        assert phase_file_verdict("implement", self._PLAN)[0] is False

    def test_implement_allows_code(self):
        assert phase_file_verdict("implement", "src/app.py")[0] is True

    def test_off_canonical_case_fails_closed(self):
        # The mirror coerces via ``PipelinePhase(phase)``, which is
        # case-sensitive — same as the gateway. Off-canonical case fails
        # closed instead of silently lowercasing through to a verdict.
        allowed, reason = phase_file_verdict("REFINE", self._PLAN)
        assert allowed is False
        assert reason and "Unknown phase 'REFINE'" in reason

    def test_known_phase_with_no_restriction_is_unrestricted(self):
        # ``apply`` is a valid PipelinePhase but the deployed JSON carries
        # no row, so the gateway's per-phase lookup misses and the call
        # falls through to allow. The mirror matches that path.
        assert phase_file_verdict("apply", self._PLAN) == (True, None)

    def test_unknown_phase_fails_closed(self):
        # Off-enum strings (truly unknown phases, the dead "pr" from #2777,
        # garbage from a misconfigured caller) fail closed at the mirror —
        # the gateway would reject the push with "Unknown phase ...
        # blocking by default", and the mirror's verdict matches so a
        # phase-blind caller can't slip a false can_write:true through.
        allowed, reason = phase_file_verdict("not-a-phase", self._PLAN)
        assert allowed is False
        assert reason and "Unknown phase 'not-a-phase'" in reason
        # The dead "pr" key from .egg/phase-permissions.json takes the
        # same path (PipelinePhase("pr") raises after #2777 deletion).
        allowed, reason = phase_file_verdict("pr", self._PLAN)
        assert allowed is False

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

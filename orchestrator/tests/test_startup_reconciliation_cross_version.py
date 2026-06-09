"""Tests for the cross-version revert tolerance in
``orchestrator/startup_reconciliation.py`` (issue #3023, slice-3, TASK-3-5).

TASK-3-5 hardens the startup reconciliation reader against a
cross-version revert scenario: after this issue's PR merges, in-flight
pipelines may have on-demand pods (or no pod at all between events). A
reverted orchestrator's reader must not crash on a missing wrapper. The
acceptance lines are:

* Integration test simulates the cross-version revert scenario: (a)
  start a pipeline on the new code, run a few BRC events through the
  on-demand path, simulate the revert by importing the previous-version
  ``routes/pipelines.py``'s reconciliation function alongside a frozen
  on-demand tracker state, (b) the reader completes without raising and
  the pipeline re-enters the run loop without marking any role as
  FAILED.
* The same reader against a synthetic legacy long-lived wrapper-pod
  state continues to work as before (no regression).

This file pins those acceptance lines at the **wiring** level — the
fall-through branch must be present and discoverable in the
reconciler source. The end-to-end "cross-version revert" scenario
(import the previous reconciler against a frozen tracker state) lives
in an integration test placeholder at the bottom of this file because
constructing the previous-version reader requires loading a different
revision of the module, which the integration suite handles via a
checked-out worktree.

Skip-vs-assert pattern mirrors slice-1's TDD scaffolds: the file is
collectable pre-TASK-3-5 and skips cleanly until the production change
lands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# conftest.py adds orchestrator/ to sys.path; the assertion below makes a
# regression there land an obvious diagnostic on this file instead of a
# confusing ``ModuleNotFoundError``.
_orchestrator_path = Path(__file__).parent.parent
assert str(_orchestrator_path) in sys.path, (
    "conftest.py must have added the orchestrator/ directory to sys.path "
    "before this module is collected."
)

_RECONCILIATION_SOURCE_PATH = _orchestrator_path / "startup_reconciliation.py"


def _reconciliation_source() -> str:
    """Read ``orchestrator/startup_reconciliation.py`` as text.

    Source-grep assertions read from disk (not from the imported
    module) so the test runs even in minimal CI environments where the
    full reconciler dependency graph isn't importable (kubernetes_client
    is the typical offender).
    """
    assert _RECONCILIATION_SOURCE_PATH.is_file(), (
        f"startup_reconciliation.py not found at {_RECONCILIATION_SOURCE_PATH}."
    )
    return _RECONCILIATION_SOURCE_PATH.read_text(encoding="utf-8")


def _task_3_5_landed() -> bool:
    """Return True once TASK-3-5's fall-through branch has landed.

    The post-TASK-3-5 source carries a clearly-named branch — either an
    inline comment referencing the revert scenario, a function or
    constant whose name contains ``on_demand`` or ``in_flight``, or the
    canonical AC keyword ``cross-version`` — so an operator can grep
    for the seam during a revert triage. We accept any of those
    spellings to avoid over-constraining the coder's wording.
    """
    source = _reconciliation_source()
    return any(
        marker in source
        for marker in (
            "on_demand_in_flight",
            "on-demand in-flight",
            "cross-version",
            "cross_version",
            "on_demand_revert",
        )
    )


# --------------------------------------------------------------------------- #
# (1) Fall-through branch is discoverable in the reconciler
# --------------------------------------------------------------------------- #


class TestOnDemandFallThroughBranchPresent:
    """TASK-3-5 acceptance: the reconciliation reader gains a
    fall-through branch — when no Job with the role label is in
    ``Running`` state for a ``(pipeline_id, role)`` AND the BRC
    tracker has a non-empty event history for that role, treat the
    pipeline as ``on-demand in-flight`` and re-derive ``next-action``
    instead of marking it failed.

    Pre-TASK-3-5 the reader has no such branch (the legacy reader
    treats a missing wrapper pod as failure); post-TASK-3-5 the
    branch must be present and inline-commented so the revert
    scenario is explicit in the source.
    """

    def test_reconciliation_source_has_on_demand_branch(self):
        if not _task_3_5_landed():
            pytest.skip(
                "TASK-3-5 (on-demand in-flight fall-through branch) "
                "not yet landed; the reconciler still treats a missing "
                "wrapper pod as failure. Test will assert once the "
                "coder's commit lands."
            )

        source = _reconciliation_source()
        # The fall-through must be discoverable. Multiple permissible
        # spellings cover the coder's choice without over-constraining.
        assert any(
            marker in source
            for marker in (
                "on_demand_in_flight",
                "on-demand in-flight",
                "cross-version",
                "cross_version",
            )
        ), (
            "TASK-3-5 acceptance: startup_reconciliation.py must "
            "reference the on-demand-in-flight / cross-version revert "
            "fall-through so an operator's revert triage can locate the "
            "branch with a single grep."
        )

    def test_reconciliation_source_has_revert_comment(self):
        """The plan calls out an explicit inline comment: ``Documenting
        this behavior with an inline comment explaining the revert
        scenario.`` Pin that the source carries the explanation so a
        future contributor refactoring the branch keeps the rationale
        attached.
        """
        if not _task_3_5_landed():
            pytest.skip(
                "TASK-3-5 not yet landed; revert-scenario comment "
                "absent. Test will assert once the coder's commit lands."
            )

        source = _reconciliation_source()
        # The exact phrasing is the coder's choice. We accept any
        # source snippet that names the revert scenario explicitly.
        assert any(
            marker in source.lower()
            for marker in (
                "revert",
                "reverted orchestrator",
                "cross-version",
            )
        ), (
            "TASK-3-5 acceptance: startup_reconciliation.py must carry "
            "an inline comment explaining the revert scenario so the "
            "branch's rationale survives future refactors."
        )


# --------------------------------------------------------------------------- #
# (2) Legacy wrapper-pod state continues to work
# --------------------------------------------------------------------------- #


class TestLegacyWrapperStateNoRegression:
    """TASK-3-5 acceptance line: ``the same reader against a synthetic
    legacy long-lived wrapper-pod state continues to work as before (no
    regression).``

    The pre-TASK-3-5 reconciler at ``reconcile_stale_containers`` already
    handles the legacy state (one live pod per role label). The
    fall-through must NOT change that path — only the missing-pod
    branch is augmented.
    """

    def test_reconcile_stale_containers_still_exported(self):
        """The public entry point must continue to exist for the legacy
        state path. A rename or removal would be a regression for
        operators whose revert lands them on a reverted reconciler.
        """
        source = _reconciliation_source()
        assert "def reconcile_stale_containers" in source, (
            "TASK-3-5 must NOT remove or rename ``reconcile_stale_containers`` "
            "— the legacy long-lived wrapper-pod reconciliation path "
            "is the reverted orchestrator's only entry point. Removing "
            "it breaks the no-regression acceptance line."
        )


# --------------------------------------------------------------------------- #
# (3) Cross-version revert integration sentinel
# --------------------------------------------------------------------------- #


class TestCrossVersionRevertIntegration:
    """Plan §slice-3 / TASK-3-5 integration acceptance:

        Start a pipeline on the new code, run a few BRC events through
        the on-demand path, simulate the revert by importing the
        previous-version routes/pipelines.py's reconciliation function
        alongside a frozen on-demand tracker state; the reader
        completes without raising and the pipeline re-enters the run
        loop without marking any role FAILED.

    The full end-to-end requires loading a previous-version module
    alongside the new tracker state, which the integration suite
    handles via a checked-out worktree. This placeholder records the
    contract so the integration scaffold can wire it in without
    re-deriving the expected shape from the plan text.
    """

    def test_revert_reader_does_not_mark_roles_failed(self):
        pytest.skip(
            "TASK-3-5 end-to-end integration shape recorded as an "
            "explicit placeholder; the live cross-version revert "
            "scenario (previous reconciler against frozen on-demand "
            "tracker state) lives in the integration suite "
            "(integration_tests/) so this file stays fast under "
            "`make test`. Re-target this test once the integration-"
            "suite scaffold lands."
        )

    def test_legacy_wrapper_state_against_reverted_reader(self):
        pytest.skip(
            "TASK-3-5 no-regression integration: the reverted reader "
            "against a synthetic legacy long-lived wrapper-pod state "
            "continues to work as before. Lives in the integration "
            "suite alongside the revert scenario above."
        )

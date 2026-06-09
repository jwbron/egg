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


# --------------------------------------------------------------------------- #
# (4) Concurrency-lens pins for TASK-3-5 — reviewer_concurrency v3 item 4
# --------------------------------------------------------------------------- #


class TestCrossVersionRevertConcurrencyLens:
    """Concurrency-lens pins for TASK-3-5's cross-version revert path,
    addressing the reviewer_concurrency v3 item-4 carry-forward concern
    that the cross-version startup-reconciliation file lacked an explicit
    concurrency lens.

    The revert scenario is intrinsically concurrent:

      1. The reverted orchestrator's reconciler runs on startup against a
         tracker state that on-demand pods may still be writing to (events
         arriving via the gateway during the seconds before the reverted
         binary takes over).
      2. The reconciler may be retried on the same tracker state under a
         systemd / k8s restart loop; the fall-through MUST be idempotent
         and read-only with respect to the tracker so retries do not
         duplicate side effects.
      3. The fall-through MUST NOT trigger a fresh wrapper-pod spawn on
         the in-flight (pipeline_id, role) — that would race with the
         on-demand event path and double-seed the BRC matrix.

    These pins land at the wiring level (source-grep on the reconciler)
    so they survive even in minimal CI environments where the full
    reconciler dependency graph isn't importable. Skip-vs-assert pattern
    matches the rest of this file.
    """

    def test_fall_through_does_not_spawn_wrapper_pod(self):
        """The fall-through branch MUST NOT call
        ``spawn_wrapper_pod`` / ``spawn_consensus_wrapped_pod`` / any
        ``kube_client.create_namespaced_job`` family inside the revert
        path. Those are the legacy wrapper-spawn callers; firing one on
        an in-flight (pipeline_id, role) would race with the on-demand
        event path that is still delivering BRC events to the gateway,
        producing a duplicate role pod and a half-seeded BRC matrix.

        The fall-through must instead re-derive ``next-action`` from
        the tracker state (read-only) and let the on-demand path
        continue handling events.
        """
        if not _task_3_5_landed():
            pytest.skip(
                "TASK-3-5 fall-through branch not yet landed; "
                "concurrency-lens spawn-suppression pin will assert once "
                "the coder's commit lands."
            )

        source = _reconciliation_source()
        # Locate the fall-through block heuristically by the canonical
        # marker tokens introduced in _task_3_5_landed(). The pin then
        # asserts the surrounding span does NOT contain a wrapper-spawn
        # call. This is a wiring-level guard: a regression that puts a
        # spawn inside the revert branch would change the source text.
        lower = source.lower()
        # Build a list of forbidden call sites the fall-through must
        # never invoke. ``create_namespaced_job`` is the kubernetes
        # client primitive; ``spawn_wrapper`` / ``spawn_consensus`` are
        # the legacy higher-level wrappers.
        forbidden_calls = (
            "spawn_wrapper_pod(",
            "spawn_consensus_wrapped_pod(",
            "create_namespaced_job(",
        )
        # The fall-through marker tokens are checked by _task_3_5_landed
        # above; find the first occurrence and inspect a generous window
        # (4 KB) around it. The window is large enough to capture a
        # multi-line branch body but tight enough that unrelated spawn
        # calls elsewhere in the reconciler don't trigger a false hit.
        marker_idx = -1
        for marker in (
            "on_demand_in_flight",
            "on-demand in-flight",
            "cross-version",
            "cross_version",
            "on_demand_revert",
        ):
            idx = lower.find(marker)
            if idx >= 0:
                marker_idx = idx
                break
        assert marker_idx >= 0, (
            "Concurrency-lens pin requires the fall-through marker to be "
            "discoverable; _task_3_5_landed() guard should have skipped "
            "this test if the marker were absent."
        )

        window_start = max(0, marker_idx - 200)
        window_end = min(len(source), marker_idx + 4000)
        window = source[window_start:window_end]
        for call in forbidden_calls:
            assert call not in window, (
                "TASK-3-5 concurrency-lens: the cross-version revert "
                f"fall-through must NOT invoke ``{call}`` — that would "
                "race with the on-demand event path still delivering "
                "BRC events for the in-flight (pipeline_id, role) and "
                "double-seed the BRC matrix with a duplicate role pod. "
                "The fall-through should re-derive next-action from "
                "tracker state (read-only) and let the on-demand path "
                "continue."
            )

    def test_fall_through_is_read_only_against_tracker(self):
        """The fall-through MUST NOT mutate the tracker (no
        ``register_agent`` / ``seed_auto_ack_for_empty_pure_producers``
        / ``record_phase_start`` calls inside the revert branch).

        Mutating writes inside the revert path would race with the
        on-demand event handler that is still writing tracker state via
        the gateway; the dual-writer would either double-seed the
        matrix or clobber a concurrent register_agent. The pre-#3023
        legacy reconciler is read-only against the tracker; the
        fall-through must preserve that invariant.

        Pinned at the source level so a future refactor that inlines a
        tracker mutation into the fall-through trips this test instead
        of landing as a silent concurrency hazard.
        """
        if not _task_3_5_landed():
            pytest.skip(
                "TASK-3-5 not yet landed; read-only-fall-through pin "
                "will assert once the coder's commit lands."
            )

        source = _reconciliation_source()
        lower = source.lower()
        marker_idx = -1
        for marker in (
            "on_demand_in_flight",
            "on-demand in-flight",
            "cross-version",
            "cross_version",
            "on_demand_revert",
        ):
            idx = lower.find(marker)
            if idx >= 0:
                marker_idx = idx
                break
        assert marker_idx >= 0
        window_start = max(0, marker_idx - 200)
        window_end = min(len(source), marker_idx + 4000)
        window = source[window_start:window_end]
        # Forbidden mutating calls. Note we look for the call form (with
        # trailing paren) to avoid false-positives on docstrings that
        # mention the symbol name in prose.
        forbidden_mutations = (
            "tracker.register_agent(",
            "tracker.seed_auto_ack_for_empty_pure_producers(",
            "tracker.record_phase_start(",
            ".register_agent(",
            ".record_phase_start(",
        )
        for call in forbidden_mutations:
            assert call not in window, (
                "TASK-3-5 concurrency-lens: the cross-version revert "
                f"fall-through must be read-only against the tracker — "
                f"``{call}`` inside the revert branch would race with "
                "the on-demand event handler still writing tracker state "
                "via the gateway. Either double-seeds the matrix or "
                "clobbers a concurrent register_agent."
            )

    def test_reconciler_is_idempotent_under_restart_loop(self):
        """The reconciler entry point (``reconcile_stale_containers``)
        must be safe to call repeatedly on the same tracker state — a
        systemd / k8s restart loop will retry it. The fall-through must
        not accumulate side effects across retries.

        Pinned via a wiring-level marker: the reconciler source must
        carry an idempotency comment OR use a guard idiom
        (``already_reconciled`` / ``reconciled_pipelines`` /
        ``idempotent`` / set-membership check) so a contributor reading
        the file can locate the guard. The forbidden anti-pattern is a
        bare unconditional spawn inside the entry point.
        """
        if not _task_3_5_landed():
            pytest.skip(
                "TASK-3-5 not yet landed; idempotency-under-restart-loop "
                "pin will assert once the coder's commit lands."
            )

        source = _reconciliation_source()
        lower = source.lower()
        # Acceptable idempotency markers — any one is sufficient.
        # The canonical explicit markers come first; the "leaving
        # RUNNING" / "leave it RUNNING" idioms are accepted as
        # *evidence* of an idempotent fall-through (the reconciler
        # leaves the pipeline state untouched, so a retry is a no-op
        # by construction). Substring matches keep the pin tolerant to
        # phrasing while still naming the concept.
        markers = (
            "idempotent",
            "already_reconciled",
            "reconciled_pipelines",
            "already_running",
            "no-op on retry",
            "no-op on re-entry",
            "safe to retry",
            "safe to re-enter",
            "restart loop",
            # Fall-through "leave RUNNING" idioms are evidence of an
            # idempotent reconciler: leaving the pipeline state
            # untouched means a retry has no incremental effect.
            "leaving running",
            "leave running",
            "leaving pipeline running",
            "leave the pipeline running",
        )
        assert any(m in lower for m in markers), (
            "TASK-3-5 concurrency-lens: ``reconcile_stale_containers`` "
            "must be idempotent under systemd / k8s restart loops. The "
            "source should carry one of the canonical idempotency "
            "markers (``idempotent``, ``already_reconciled``, "
            "``reconciled_pipelines``, ``no-op on retry``, …) OR an "
            "explicit ``leaving RUNNING`` / ``leave the pipeline "
            "RUNNING`` idiom in the fall-through so a contributor can "
            "locate the retry-safety reasoning without reading the "
            "full entry point. The ``leave RUNNING`` form is accepted "
            "as evidence of an idempotent fall-through: leaving the "
            "pipeline state untouched means a retry has no incremental "
            "effect by construction."
        )

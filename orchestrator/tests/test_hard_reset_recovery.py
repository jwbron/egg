"""Tests for the #2979 non-destructive worktree-divergence reconcile surface.

Covers:

* ``WorktreeSyncOutcome`` shape returned by
  :func:`_sync_worktree_with_remote` on the non-divergence branches
  (already-in-sync, behind-only, push-ahead, clean rebase) and on the
  unreconciled-divergence branch (no hard reset, ``diverged_unreconciled``).
* The reconcile HITL question text + options + the abort detector.
* :func:`_emit_divergence_reconcile_hitl` (the non-blocking
  populate_contract path → AWAITING_HUMAN, not FAILED).
* :func:`_fail_pipeline_after_divergence_abort` (the abort → FAILED path).
* :func:`_sync_worktree_reconciling_divergence` (the in-loop
  pause→reconcile→resume / abort pause loop).
* The ``populate_contract`` route surfacing an unreconciled divergence as
  a 409 + AWAITING_HUMAN pause.

The end-to-end sync helper subprocess scenarios live in
``test_sync_worktree.py``; this file focuses on the pause/HITL/route layer
so a regression is caught without a real git worktree.

This replaces the pre-#2979 ``test_hard_reset_recovery.py``: the
destructive ``git reset --hard`` recovery, its FAILED+HITL helper, the
``hard_reset_recovery:`` dispatch hook, and the ``restart_phase``-based
resume helper were all removed in #2979 (the sync is now non-destructive
and the in-loop callers resume inline).
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from gateway_client import PushResult  # noqa: E402
from models import PipelinePhase, PipelineStatus  # noqa: E402
from routes.pipelines import (  # noqa: E402
    _DIVERGENCE_RECONCILE_ABORT,
    _DIVERGENCE_RECONCILE_HITL_OPTIONS,
    _DIVERGENCE_RECONCILE_RESUME,
    WorktreeSyncOutcome,
    _build_sync_recovery_backup_ref,
    _divergence_reconcile_hitl_question,
    _divergence_reconcile_is_abort,
    _emit_divergence_reconcile_hitl,
    _fail_pipeline_after_divergence_abort,
    _sync_worktree_reconciling_divergence,
    _sync_worktree_with_remote,
)

_PUSH_OK = PushResult(ok=True, category="", detail="")


def _make_spawner(fetch_ok: bool = True, push_ok: bool = True) -> MagicMock:
    spawner = MagicMock()
    spawner.gateway.fetch_worktree_branch.return_value = fetch_ok
    spawner.gateway.push_worktree_branch.return_value = (
        _PUSH_OK if push_ok else PushResult(ok=False, category="t", detail="d")
    )
    return spawner


def _make_subprocess_result(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _diverged_outcome() -> WorktreeSyncOutcome:
    return WorktreeSyncOutcome(
        case="divergence_unreconciled",
        diverged_unreconciled=True,
        backup_ref="refs/egg-backup/sync-recovery/pipe-1/123",
        local_only_commit_shas=("abc1234 add foo",),
    )


class TestBackupRefName:
    """``refs/egg-backup/sync-recovery/<pid>/<ts>`` is the contract."""

    def test_ref_name_layout(self):
        ref = _build_sync_recovery_backup_ref("pipeline-abc", 1717000000)
        assert ref == "refs/egg-backup/sync-recovery/pipeline-abc/1717000000"

    def test_ref_name_supports_for_each_ref_filter(self):
        """A ``for-each-ref refs/egg-backup/sync-recovery/<pid>`` filter
        must select only this pipeline's backups (the slash-segment
        layout, not a flat name with dashes)."""
        ref = _build_sync_recovery_backup_ref("pipeline-xyz", 100)
        assert ref.startswith("refs/egg-backup/sync-recovery/pipeline-xyz/")


class TestSyncOutcomeShape:
    """Non-divergence branches report diverged_unreconciled=False."""

    def test_already_in_sync_returns_outcome(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t0\n"),
            ]
            outcome = _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert isinstance(outcome, WorktreeSyncOutcome)
        assert outcome.case == "already_in_sync"
        assert outcome.diverged_unreconciled is False
        assert outcome.backup_ref is None
        assert outcome.local_only_commit_shas == ()

    def test_behind_only_returns_reset_succeeded(self):
        spawner = _make_spawner()
        with patch("routes.pipelines.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="0\t3\n"),
                _make_subprocess_result(returncode=0),
            ]
            outcome = _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert outcome.case == "reset_succeeded"
        assert outcome.diverged_unreconciled is False

    def test_divergence_rebased_returns_outcome(self):
        spawner = _make_spawner()
        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines._rebase_with_agent_output_autoresolve") as mock_rebase,
        ):
            mock_run.side_effect = [
                _make_subprocess_result(stdout="egg/issue-42\n"),
                _make_subprocess_result(returncode=0),
                _make_subprocess_result(stdout="2\t3\n"),
            ]
            mock_rebase.return_value = PushResult(ok=True, category="", detail="")
            outcome = _sync_worktree_with_remote(spawner, "pipe-1", Path("/tmp/repo"))
        assert outcome.case == "divergence_rebased"
        assert outcome.diverged_unreconciled is False


class TestDivergenceReconcileHitlQuestion:
    """The reconcile HITL names the backup ref, lists local-only commits,
    exposes the two options, and describes the NON-destructive pause."""

    def test_lists_backup_ref_and_local_only_commits(self):
        question = _divergence_reconcile_hitl_question(
            pipeline_id="pipeline-zzz",
            phase=PipelinePhase.PLAN,
            backup_ref="refs/egg-backup/sync-recovery/pipeline-zzz/123",
            local_only_commit_shas=("abc1234 add foo", "def5678 add bar"),
        )
        assert "refs/egg-backup/sync-recovery/pipeline-zzz/123" in question
        assert "abc1234 add foo" in question
        assert "def5678 add bar" in question
        assert "plan" in question
        assert "pipeline-zzz" in question
        # Both option labels present in the prose so the SDLC skill renders
        # them when no separate options list is shown.
        assert _DIVERGENCE_RECONCILE_RESUME in question
        assert _DIVERGENCE_RECONCILE_ABORT in question
        # The wording must make clear nothing was discarded / not failed.
        assert "Nothing was discarded" in question
        assert "paused (not failed)" in question

    def test_handles_missing_backup_ref(self):
        question = _divergence_reconcile_hitl_question(
            pipeline_id="pipeline-zzz",
            phase=PipelinePhase.PLAN,
            backup_ref=None,
            local_only_commit_shas=(),
        )
        assert "Backup ref write failed" in question
        assert "could not be enumerated" in question

    def test_options_are_two_distinct_strings(self):
        assert _DIVERGENCE_RECONCILE_HITL_OPTIONS == [
            _DIVERGENCE_RECONCILE_RESUME,
            _DIVERGENCE_RECONCILE_ABORT,
        ]
        assert len(_DIVERGENCE_RECONCILE_HITL_OPTIONS) == 2


class TestDivergenceReconcileIsAbort:
    """Only an explicit abort resolution fails the pipeline; everything
    else (resume label, free text, empty) re-attempts the sync."""

    def test_abort_label(self):
        assert _divergence_reconcile_is_abort(_DIVERGENCE_RECONCILE_ABORT) is True

    def test_abort_synonyms(self):
        assert _divergence_reconcile_is_abort("abort") is True
        assert _divergence_reconcile_is_abort("Cancel") is True

    def test_abort_json_envelope(self):
        assert _divergence_reconcile_is_abort('{"action": "abort"}') is True

    def test_resume_and_freetext_are_not_abort(self):
        assert _divergence_reconcile_is_abort(_DIVERGENCE_RECONCILE_RESUME) is False
        assert _divergence_reconcile_is_abort("I rebased it, go") is False
        assert _divergence_reconcile_is_abort("") is False


class TestEmitDivergenceReconcileHitl:
    """The non-blocking populate path pauses (AWAITING_HUMAN) + persists
    the reconcile HITL — it does NOT fail the pipeline."""

    def test_sets_awaiting_human_and_persists_decision(self):
        pipeline = MagicMock()
        phase_exec = MagicMock()
        pipeline.get_phase_execution.return_value = phase_exec
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._persist_hitl_decision") as mock_persist,
            patch("routes.pipelines.report_pipeline_status") as mock_report,
            patch("routes.pipelines._emit_pipeline_event"),
        ):
            mock_persist.return_value = MagicMock(id="decision-1")
            decision = _emit_divergence_reconcile_hitl(
                "pipe-1",
                store,
                phase=PipelinePhase.PLAN,
                backup_ref="refs/egg-backup/sync-recovery/pipe-1/9",
                local_only_commit_shas=("abc1234 foo",),
            )

        # AWAITING_HUMAN, NOT FAILED.
        assert pipeline.status == PipelineStatus.AWAITING_HUMAN
        assert phase_exec.status == PipelineStatus.AWAITING_HUMAN
        store.save_pipeline.assert_called()
        # The persisted decision carries the canonical options + a question
        # that names the backup ref (no dispatch context — resume is manual
        # re-run for the route path).
        persist_kwargs = mock_persist.call_args.kwargs
        assert persist_kwargs["options"] == _DIVERGENCE_RECONCILE_HITL_OPTIONS
        assert "refs/egg-backup/sync-recovery/pipe-1/9" in persist_kwargs["question"]
        assert persist_kwargs.get("context") is None
        mock_report.assert_called_once()
        assert decision.id == "decision-1"


class TestFailPipelineAfterDivergenceAbort:
    """The abort path pins FAILED + broadcasts pipeline.failed, with no
    HITL emission (the reconcile decision was already resolved)."""

    def test_sets_failed_and_runs_pre_event_hook_before_broadcast(self):
        pipeline = MagicMock()
        phase_exec = MagicMock()
        pipeline.get_phase_execution.return_value = phase_exec
        store = MagicMock()
        store.load_pipeline.return_value = pipeline
        order: list[str] = []

        def _hook() -> None:
            order.append("hook")

        with (
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines.report_pipeline_status") as mock_report,
            patch("routes.pipelines._emit_pipeline_event") as mock_emit,
        ):
            mock_emit.side_effect = lambda *a, **k: order.append("event")
            _fail_pipeline_after_divergence_abort(
                "pipe-1",
                store,
                phase=PipelinePhase.PLAN,
                backup_ref="refs/egg-backup/sync-recovery/pipe-1/9",
                local_only_commit_shas=("abc1234 foo",),
                pre_event_hook=_hook,
            )

        assert pipeline.status == PipelineStatus.FAILED
        assert phase_exec.status == PipelineStatus.FAILED
        store.save_pipeline.assert_called()
        # Pre-event hook (overseer teardown) runs before the public event.
        assert order == ["hook", "event"]
        mock_report.assert_called_once()
        assert mock_report.call_args.kwargs["event_type"] == "pipeline.failed"


class TestSyncWorktreeReconcilingDivergence:
    """The in-loop pause→reconcile→resume / abort loop (#2979)."""

    def _patch_ctx(self):
        """Common patches: lock, HITL persist, status report, event emit."""
        return (
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._persist_hitl_decision", return_value=MagicMock(id="d1")),
            patch("routes.pipelines.report_pipeline_status"),
            patch("routes.pipelines._emit_pipeline_event"),
        )

    def test_no_divergence_returns_outcome_without_pausing(self):
        outcome = WorktreeSyncOutcome(case="reset_succeeded")
        store = MagicMock()
        dq = MagicMock()
        with (
            patch("routes.pipelines._sync_worktree_with_remote", return_value=outcome) as mock_sync,
            patch("routes.pipelines.get_decision_queue", return_value=dq),
        ):
            result, aborted = _sync_worktree_reconciling_divergence(
                MagicMock(),
                "pipe-1",
                store,
                Path("/repo"),
                worktree_repo_path=Path("/wt"),
                phase=PipelinePhase.PLAN,
            )
        assert result is outcome
        assert aborted is False
        mock_sync.assert_called_once()
        dq.wait_for_decision.assert_not_called()

    def test_resume_re_runs_sync_and_continues(self):
        """Operator reconciles → 'Reconciled — resume' → sync re-runs and
        succeeds → returns (reconciled, aborted=False)."""
        reconciled = WorktreeSyncOutcome(case="divergence_rebased")
        store = MagicMock()
        dq = MagicMock()
        dq.get_decision.return_value = MagicMock(resolution=_DIVERGENCE_RECONCILE_RESUME)
        lock_p, persist_p, report_p, emit_p = self._patch_ctx()
        with (
            patch(
                "routes.pipelines._sync_worktree_with_remote",
                side_effect=[_diverged_outcome(), reconciled],
            ) as mock_sync,
            patch("routes.pipelines.get_decision_queue", return_value=dq),
            lock_p,
            persist_p,
            report_p,
            emit_p,
        ):
            result, aborted = _sync_worktree_reconciling_divergence(
                MagicMock(),
                "pipe-1",
                store,
                Path("/repo"),
                worktree_repo_path=Path("/wt"),
                phase=PipelinePhase.PLAN,
            )
        assert aborted is False
        assert result is reconciled
        assert mock_sync.call_count == 2
        dq.wait_for_decision.assert_called_once_with("d1")

    def test_abort_returns_aborted(self):
        """Operator chooses 'Abort pipeline' → returns (outcome, aborted=True),
        sync runs only once (not re-attempted)."""
        store = MagicMock()
        dq = MagicMock()
        dq.get_decision.return_value = MagicMock(resolution=_DIVERGENCE_RECONCILE_ABORT)
        lock_p, persist_p, report_p, emit_p = self._patch_ctx()
        with (
            patch(
                "routes.pipelines._sync_worktree_with_remote",
                return_value=_diverged_outcome(),
            ) as mock_sync,
            patch("routes.pipelines.get_decision_queue", return_value=dq),
            lock_p,
            persist_p,
            report_p,
            emit_p,
        ):
            result, aborted = _sync_worktree_reconciling_divergence(
                MagicMock(),
                "pipe-1",
                store,
                Path("/repo"),
                worktree_repo_path=Path("/wt"),
                phase=PipelinePhase.PLAN,
            )
        assert aborted is True
        assert result.diverged_unreconciled is True
        mock_sync.assert_called_once()

    def test_reconcile_budget_exhausted_aborts(self):
        """If every resume re-diverges, the bounded budget eventually
        aborts rather than pausing forever."""
        store = MagicMock()
        dq = MagicMock()
        dq.get_decision.return_value = MagicMock(resolution=_DIVERGENCE_RECONCILE_RESUME)
        lock_p, persist_p, report_p, emit_p = self._patch_ctx()
        with (
            patch(
                "routes.pipelines._sync_worktree_with_remote",
                return_value=_diverged_outcome(),
            ) as mock_sync,
            patch("routes.pipelines.get_decision_queue", return_value=dq),
            lock_p,
            persist_p,
            report_p,
            emit_p,
        ):
            result, aborted = _sync_worktree_reconciling_divergence(
                MagicMock(),
                "pipe-1",
                store,
                Path("/repo"),
                worktree_repo_path=Path("/wt"),
                phase=PipelinePhase.PLAN,
                max_reconcile_pauses=2,
            )
        assert aborted is True
        assert result.diverged_unreconciled is True
        # initial sync + 2 resume re-runs = 3 sync calls; then budget hit.
        assert mock_sync.call_count == 3
        assert dq.wait_for_decision.call_count == 2


class TestEmptyContractHitlWordingNoLongerNamesPriorPopulator:
    """#2792: drop the misleading 'populate-from-plan step silently
    failed earlier' wording from :func:`_empty_contract_hitl_question`.

    (Unrelated to the #2979 reconcile change — kept here because it
    asserts the empty-contract HITL wording.)
    """

    def test_question_text_does_not_blame_phantom_earlier_failure(self):
        from routes.pipelines import _empty_contract_hitl_question

        question = _empty_contract_hitl_question(
            pipeline_id="p-x",
            reason="plan_draft_missing_on_local",
            draft_slice_count=None,
            gate="plan_complete",
        )
        assert "populate-from-plan step silently failed earlier" not in question
        assert "diverged" in question


class TestPopulateContractDivergenceSurfacing:
    """#2979: ``populate_contract`` surfaces an unreconciled divergence as
    a non-destructive pause — AWAITING_HUMAN (not FAILED) + reconcile HITL
    + HTTP 409 ``divergence_reconcile_unacked`` — and refuses to populate.
    """

    def _make_app_client(self):
        from flask import Flask
        from routes.phases import phases_bp

        app = Flask(__name__)
        app.register_blueprint(phases_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def _make_pipeline(self):
        from models import Pipeline

        pipeline = Pipeline(
            id="issue-2979",
            issue_number=2979,
            repo="owner/repo",
            branch="egg/issue-2979",
        )
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        return pipeline

    def test_unreconciled_divergence_returns_409_and_pauses(self):
        client = self._make_app_client()
        pipeline = self._make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")

        outcome = WorktreeSyncOutcome(
            case="divergence_unreconciled",
            diverged_unreconciled=True,
            backup_ref="refs/egg-backup/sync-recovery/issue-2979/123",
            local_only_commit_shas=("abc1234 add foo",),
        )

        with (
            patch(
                "routes.phases.get_state_store_for_pipeline",
                return_value=(mock_store, pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=Path("/home/egg/.egg-worktrees/issue-2979/egg"),
            ),
            patch("routes.pipelines._sync_worktree_with_remote", return_value=outcome),
            patch("routes.pipelines._compute_gateway_mode", return_value=("public", None)),
            patch("routes.pipelines._get_spawner", return_value=MagicMock()),
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._emit_divergence_reconcile_hitl") as mock_emit,
        ):
            resp = client.post("/api/v1/pipelines/issue-2979/phase/populate-contract")

        assert resp.status_code == 409
        import json

        body = json.loads(resp.data)
        assert body["success"] is False
        assert body["reason"] == "divergence_reconcile_unacked"
        assert body["details"]["diverged_unreconciled"] is True
        assert body["details"]["backup_ref"] == "refs/egg-backup/sync-recovery/issue-2979/123"
        assert body["details"]["local_only_commit_shas"] == ["abc1234 add foo"]

        # Populator MUST NOT run against an un-reconciled worktree.
        mock_populate.assert_not_called()
        # The reconcile HITL (AWAITING_HUMAN pause) MUST have been emitted.
        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["phase"] == PipelinePhase.IMPLEMENT
        assert kwargs["backup_ref"] == "refs/egg-backup/sync-recovery/issue-2979/123"
        assert list(kwargs["local_only_commit_shas"]) == ["abc1234 add foo"]

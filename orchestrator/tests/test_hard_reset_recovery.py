"""Tests for the #2792 sync-helper hard-reset recovery surface.

Covers:

* ``WorktreeSyncOutcome`` shape returned by
  :func:`_sync_worktree_with_remote` on the non-recovery branches
  (already-in-sync, behind-only, push-ahead).
* The dedicated HITL question text, options, context discriminator.
* The decision-resolution dispatch hook that wires
  ``hard_reset_recovery:<phase>`` resolutions to
  :func:`resume_pipeline_after_hard_reset_ack` /
  :func:`abort_pipeline_after_hard_reset_ack`.

The end-to-end sync helper subprocess scenarios already live in
``test_sync_worktree.py``; this file focuses on the HITL layer and the
dispatch wiring so a regression in either is caught by a unit test that
doesn't need a real git worktree.
"""

import subprocess
import sys
import threading
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
from routes.pipelines import (  # noqa: E402
    WorktreeSyncOutcome,
    _build_sync_recovery_backup_ref,
    _emit_hard_reset_recovery_hitl,
    _fail_pipeline_and_emit_hard_reset_recovery,
    _hard_reset_recovery_hitl_options,
    _hard_reset_recovery_hitl_question,
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
    """Non-recovery branches return outcomes with hard_reset_performed=False."""

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
        assert outcome.hard_reset_performed is False
        assert outcome.backup_ref is None
        assert outcome.discarded_commit_shas == ()

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
        assert outcome.hard_reset_performed is False

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
        assert outcome.hard_reset_performed is False


class TestHardResetHitlQuestion:
    """The HITL question text names the backup ref, lists discarded
    SHAs, and exposes exactly two options."""

    def test_lists_backup_ref_and_discarded_commits(self):
        from routes.pipelines import PipelinePhase

        question = _hard_reset_recovery_hitl_question(
            pipeline_id="pipeline-zzz",
            phase=PipelinePhase.PLAN,
            backup_ref="refs/egg-backup/sync-recovery/pipeline-zzz/123",
            discarded_commit_shas=("abc1234 add foo", "def5678 add bar"),
        )
        assert "refs/egg-backup/sync-recovery/pipeline-zzz/123" in question
        assert "abc1234 add foo" in question
        assert "def5678 add bar" in question
        assert "plan" in question
        assert "pipeline-zzz" in question
        # Both option labels present in the prose so the SDLC skill
        # renders them when no separate options list is shown.
        assert "Continue with post-reset state" in question
        assert "Abort pipeline" in question

    def test_handles_missing_backup_ref(self):
        from routes.pipelines import PipelinePhase

        question = _hard_reset_recovery_hitl_question(
            pipeline_id="pipeline-zzz",
            phase=PipelinePhase.PLAN,
            backup_ref=None,
            discarded_commit_shas=(),
        )
        # The "backup ref not written" branch must NOT claim a fake ref.
        assert "not written" in question
        # The "couldn't enumerate" branch is the fallback when rev-list
        # itself failed.
        assert "could not be enumerated" in question

    def test_options_are_two_distinct_strings(self):
        from routes.pipelines import _HARD_RESET_RECOVERY_HITL_OPTIONS

        assert _HARD_RESET_RECOVERY_HITL_OPTIONS == [
            "Continue with post-reset state",
            "Abort pipeline",
        ]
        assert len(_HARD_RESET_RECOVERY_HITL_OPTIONS) == 2


class TestHardResetHitlEmission:
    """The emission helper persists with the canonical context discriminator."""

    def test_context_prefix_used_for_dispatch(self):
        """The context must be ``hard_reset_recovery:<phase>`` so the
        decisions dispatch hook routes on a stable string, not prose."""
        from routes.pipelines import PipelinePhase

        captured: dict = {}

        def fake_persist(
            pipeline_id, pipeline, store, *, question, options, phase=None, context=None
        ):  # noqa: ANN001
            captured["context"] = context
            captured["options"] = options
            captured["question"] = question
            return MagicMock(id="decision-9", context=context)

        with patch("routes.pipelines._persist_hitl_decision", side_effect=fake_persist):
            _emit_hard_reset_recovery_hitl(
                "pipeline-xyz",
                MagicMock(),
                MagicMock(),
                phase=PipelinePhase.PLAN,
                backup_ref="refs/egg-backup/sync-recovery/pipeline-xyz/100",
                discarded_commit_shas=("abc1234 foo",),
            )

        assert captured["context"] == "hard_reset_recovery:plan"
        assert captured["options"] == [
            "Continue with post-reset state",
            "Abort pipeline",
        ]
        assert "abc1234 foo" in captured["question"]


class TestDispatchResolution:
    """``_handle_hard_reset_recovery_resolution`` routes Continue/Abort."""

    def test_continue_triggers_resume_helper(self):
        from routes.decisions import _handle_hard_reset_recovery_resolution

        with (
            patch(
                "routes.pipelines.resume_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_resume,
            patch(
                "routes.pipelines.abort_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_abort,
        ):
            _handle_hard_reset_recovery_resolution(
                "pipeline-abc",
                "hard_reset_recovery:plan",
                "Continue with post-reset state",
            )

        mock_resume.assert_called_once()
        kwargs = mock_resume.call_args.kwargs
        assert kwargs["phase_value"] == "plan"
        mock_abort.assert_not_called()

    def test_abort_triggers_abort_helper(self):
        from routes.decisions import _handle_hard_reset_recovery_resolution

        with (
            patch(
                "routes.pipelines.resume_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_resume,
            patch(
                "routes.pipelines.abort_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_abort,
        ):
            _handle_hard_reset_recovery_resolution(
                "pipeline-abc",
                "hard_reset_recovery:plan",
                "Abort pipeline",
            )

        mock_abort.assert_called_once_with("pipeline-abc")
        mock_resume.assert_not_called()

    def test_unknown_resolution_is_logged_and_skipped(self):
        from routes.decisions import _handle_hard_reset_recovery_resolution

        mock_store = MagicMock()
        with (
            patch(
                "routes.pipelines.resume_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_resume,
            patch(
                "routes.pipelines.abort_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_abort,
            patch("routes.decisions.logger") as mock_logger,
            patch("message_store.get_message_store", return_value=mock_store),
        ):
            _handle_hard_reset_recovery_resolution(
                "pipeline-abc",
                "hard_reset_recovery:plan",
                "Something else entirely",
            )

        mock_resume.assert_not_called()
        mock_abort.assert_not_called()
        # N5: unknown resolution now logs at WARN and emits an
        # OVERSEER_ALERT so the operator notices the stuck pipeline.
        mock_logger.warning.assert_called()
        mock_store.add_message.assert_called_once()
        sent_msg = mock_store.add_message.call_args.args[0]
        assert sent_msg.message_type == "OVERSEER_ALERT"
        assert sent_msg.metadata.get("anomaly") == ("hard_reset_recovery_unknown_resolution")
        assert sent_msg.metadata.get("priority") == "high"

    def test_continue_rejected_when_not_in_valid_options(self):
        """#2797 follow-up: a "Continue with post-reset state" resolution
        on a doubly-failed HITL whose options list collapsed to
        ``["Abort pipeline"]`` only must not route to the resume helper
        (which would loop straight back into the same divergence).
        The cross-check routes the call into the unknown-resolution
        path: WARN log + OVERSEER_ALERT, no dispatch.
        """
        from routes.decisions import _handle_hard_reset_recovery_resolution

        mock_store = MagicMock()
        with (
            patch(
                "routes.pipelines.resume_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_resume,
            patch(
                "routes.pipelines.abort_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_abort,
            patch("routes.decisions.logger") as mock_logger,
            patch("message_store.get_message_store", return_value=mock_store),
        ):
            _handle_hard_reset_recovery_resolution(
                "pipeline-abc",
                "hard_reset_recovery:plan",
                "Continue with post-reset state",
                valid_options=["Abort pipeline"],
            )

        mock_resume.assert_not_called()
        mock_abort.assert_not_called()
        mock_logger.warning.assert_called()
        mock_store.add_message.assert_called_once()
        sent_msg = mock_store.add_message.call_args.args[0]
        assert sent_msg.message_type == "OVERSEER_ALERT"
        assert sent_msg.metadata.get("anomaly") == "hard_reset_recovery_unknown_resolution"
        assert sent_msg.metadata.get("priority") == "high"
        # The alert body should call out the options-list mismatch
        # so the operator sees why dispatch was suppressed.
        assert "options list" in sent_msg.body
        assert "Abort pipeline" in sent_msg.body

    def test_abort_accepted_when_in_valid_options(self):
        """The valid-options cross-check must not block a legitimate
        Abort on a doubly-failed HITL — "Abort pipeline" is the only
        option offered in that branch and must still dispatch.
        """
        from routes.decisions import _handle_hard_reset_recovery_resolution

        with (
            patch(
                "routes.pipelines.resume_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_resume,
            patch(
                "routes.pipelines.abort_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_abort,
        ):
            _handle_hard_reset_recovery_resolution(
                "pipeline-abc",
                "hard_reset_recovery:plan",
                "Abort pipeline",
                valid_options=["Abort pipeline"],
            )

        mock_abort.assert_called_once_with("pipeline-abc")
        mock_resume.assert_not_called()

    def test_continue_accepted_when_in_valid_options(self):
        """Successful-recovery HITLs offer both options — Continue must
        still dispatch to the resume helper when it's in the list.
        """
        from routes.decisions import _handle_hard_reset_recovery_resolution

        with (
            patch(
                "routes.pipelines.resume_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_resume,
            patch(
                "routes.pipelines.abort_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_abort,
        ):
            _handle_hard_reset_recovery_resolution(
                "pipeline-abc",
                "hard_reset_recovery:plan",
                "Continue with post-reset state",
                valid_options=["Continue with post-reset state", "Abort pipeline"],
            )

        mock_resume.assert_called_once()
        mock_abort.assert_not_called()

    def test_valid_options_none_keeps_legacy_behavior(self):
        """``valid_options=None`` (the default) skips the cross-check —
        legacy callers that don't pass options keep dispatching on the
        known whitelist alone.
        """
        from routes.decisions import _handle_hard_reset_recovery_resolution

        with (
            patch(
                "routes.pipelines.resume_pipeline_after_hard_reset_ack",
                return_value=True,
            ) as mock_resume,
            patch(
                "routes.pipelines.abort_pipeline_after_hard_reset_ack",
                return_value=True,
            ),
        ):
            _handle_hard_reset_recovery_resolution(
                "pipeline-abc",
                "hard_reset_recovery:plan",
                "Continue with post-reset state",
                # valid_options omitted → defaults to None
            )

        mock_resume.assert_called_once()


class TestEmptyContractHitlWordingNoLongerNamesPriorPopulator:
    """#2792: drop the misleading 'populate-from-plan step silently
    failed earlier' wording from :func:`_empty_contract_hitl_question`.

    Reason: when the hard-reset recovery fires inline at sync time, the
    populator never ran in the first place — there's no 'earlier' step
    that silently failed.  The current wording invents a phantom
    earlier failure that confuses operators investigating the HITL.
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
        # The replacement wording must still tell the operator that
        # state and contract have diverged so the next action picker
        # has the context it needs.
        assert "diverged" in question


class TestResumeHelperResetsConsensusAndHealth:
    """#2792 review B1: ``resume_pipeline_after_hard_reset_ack`` must
    mirror ``restart_phase``'s consensus / restart-count / health-monitor
    cleanup so a re-spawn after a post-phase hard reset does not
    short-circuit against the prior round's CONFIRMED tracker state and
    does not fire stale-elapsed Tier-1 health alerts (#2084 bug class).
    """

    def _make_pipeline_with_agents(self):
        from models import (
            AgentExecution,
            AgentExecutionStatus,
            AgentRole,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        pipeline = Pipeline(
            id="issue-2792",
            issue_number=2792,
            repo="owner/repo",
            branch="egg/issue-2792",
            status=PipelineStatus.FAILED,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        pipeline.phases = {
            PipelinePhase.IMPLEMENT.value: PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.FAILED,
                error="hard-reset recovery pending",
                review_cycles=2,
                agents=[
                    AgentExecution(role=AgentRole.CODER, status=AgentExecutionStatus.RUNNING),
                    AgentExecution(role=AgentRole.TESTER, status=AgentExecutionStatus.RUNNING),
                    AgentExecution(role=AgentRole.DOCUMENTER, status=AgentExecutionStatus.RUNNING),
                ],
            ),
        }
        return pipeline

    def test_resume_clears_tracker_evaluator_restart_counts_health(self):
        pipeline = self._make_pipeline_with_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        mock_spawner = MagicMock()
        mock_tracker = MagicMock()
        mock_evaluator = MagicMock()
        mock_hm = MagicMock()

        with (
            patch("routes.pipelines.get_repo_path", return_value=Path("/repo")),
            patch("routes.pipelines._resolve_pipeline", return_value=(mock_store, pipeline)),
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._get_spawner", return_value=mock_spawner),
            patch("routes.pipelines._spawn_pipeline_run_thread") as mock_spawn_thread,
            patch.dict(
                "sys.modules",
                {
                    "peer_consensus": MagicMock(
                        get_peer_consensus_tracker=MagicMock(return_value=mock_tracker)
                    ),
                    "consensus": MagicMock(
                        get_consensus_evaluator=MagicMock(return_value=mock_evaluator)
                    ),
                    "health_monitor": MagicMock(get_health_monitor=MagicMock(return_value=mock_hm)),
                },
            ),
        ):
            from routes.pipelines import resume_pipeline_after_hard_reset_ack

            ok = resume_pipeline_after_hard_reset_ack(
                "issue-2792",
                phase_value="implement",
            )

        assert ok is True
        mock_tracker.clear.assert_called_once()
        mock_evaluator.clear.assert_called_once_with("issue-2792")
        mock_spawner.reset_restart_counts.assert_called_once_with("issue-2792")
        reset_calls = {call.args[0] for call in mock_hm.reset_agent.call_args_list}
        assert reset_calls == {"coder", "tester", "documenter"}
        mock_spawn_thread.assert_called_once()

    def test_resume_falls_back_to_role_table_when_phase_agents_empty(self):
        """When ``phase_exec.agents`` is empty (phase-start hard reset
        path), the resume helper must fall back to the deterministic
        per-phase roster source so health-monitor cleanup still covers
        the roles the next spawn will create."""
        from models import (
            AgentRole,
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        pipeline = Pipeline(
            id="issue-2792b",
            issue_number=2792,
            repo="owner/repo",
            branch="egg/issue-2792b",
            status=PipelineStatus.FAILED,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        pipeline.phases = {
            PipelinePhase.IMPLEMENT.value: PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.FAILED,
                error="hard-reset recovery pending",
                agents=[],
            ),
        }

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        mock_spawner = MagicMock()
        mock_hm = MagicMock()
        fake_roles_module = MagicMock()
        fake_roles_module.get_roles_for_phase.return_value = [
            AgentRole.CODER,
            AgentRole.TESTER,
        ]

        with (
            patch("routes.pipelines.get_repo_path", return_value=Path("/repo")),
            patch("routes.pipelines._resolve_pipeline", return_value=(mock_store, pipeline)),
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._get_spawner", return_value=mock_spawner),
            patch("routes.pipelines._spawn_pipeline_run_thread"),
            patch.dict(
                "sys.modules",
                {
                    "peer_consensus": MagicMock(
                        get_peer_consensus_tracker=MagicMock(return_value=None)
                    ),
                    "consensus": MagicMock(
                        get_consensus_evaluator=MagicMock(return_value=MagicMock())
                    ),
                    "health_monitor": MagicMock(get_health_monitor=MagicMock(return_value=mock_hm)),
                    "egg_contracts.agent_roles": fake_roles_module,
                },
            ),
        ):
            from routes.pipelines import resume_pipeline_after_hard_reset_ack

            ok = resume_pipeline_after_hard_reset_ack(
                "issue-2792b",
                phase_value="implement",
            )

        assert ok is True
        reset_calls = {call.args[0] for call in mock_hm.reset_agent.call_args_list}
        assert reset_calls == {"coder", "tester"}

    def test_resume_returns_false_on_phase_mismatch(self):
        """Phase-mismatch (operator resolved a stale recovery decision
        after the pipeline already advanced) must not clear consensus —
        the active phase would lose live tracker state."""
        pipeline = self._make_pipeline_with_agents()

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline
        mock_store.repo_path = Path("/repo")

        mock_tracker = MagicMock()
        mock_evaluator = MagicMock()

        with (
            patch("routes.pipelines.get_repo_path", return_value=Path("/repo")),
            patch("routes.pipelines._resolve_pipeline", return_value=(mock_store, pipeline)),
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._get_spawner") as mock_get_spawner,
            patch("routes.pipelines._spawn_pipeline_run_thread") as mock_spawn,
            patch.dict(
                "sys.modules",
                {
                    "peer_consensus": MagicMock(
                        get_peer_consensus_tracker=MagicMock(return_value=mock_tracker)
                    ),
                    "consensus": MagicMock(
                        get_consensus_evaluator=MagicMock(return_value=mock_evaluator)
                    ),
                },
            ),
        ):
            from routes.pipelines import resume_pipeline_after_hard_reset_ack

            # Pipeline is currently on IMPLEMENT, ack names PLAN.
            ok = resume_pipeline_after_hard_reset_ack(
                "issue-2792",
                phase_value="plan",
            )

        assert ok is False
        mock_tracker.clear.assert_not_called()
        mock_evaluator.clear.assert_not_called()
        mock_get_spawner.assert_not_called()
        mock_spawn.assert_not_called()


class TestPopulateContractHardResetSurfacing:
    """#2797 review B4 / N3: ``populate_contract`` must surface the
    destructive recovery the same way the phase-boundary sites do —
    pin pipeline+phase to FAILED, emit the hard-reset HITL, broadcast
    ``pipeline.failed``, and return 409 — so the operator's ack surface
    is uniform across all three triggers (phase-start, post-phase,
    populate_contract).
    """

    def _make_app_client(self):
        from flask import Flask
        from routes.phases import phases_bp

        app = Flask(__name__)
        app.register_blueprint(phases_bp)
        app.config["TESTING"] = True
        return app.test_client()

    def _make_pipeline(self):
        from models import Pipeline, PipelinePhase

        pipeline = Pipeline(
            id="issue-2792",
            issue_number=2792,
            repo="owner/repo",
            branch="egg/issue-2792",
        )
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        return pipeline

    def test_hard_reset_recovery_returns_409_and_emits_hitl(self):
        """When ``_sync_worktree_with_remote`` reports
        ``hard_reset_performed=True``, the route must call the shared
        failure-and-HITL helper (so the operator gets the same ack
        surface as the phase-boundary sites), skip the populator, and
        return HTTP 409 with ``reason="hard_reset_recovery_unacked"``.
        """
        from routes.pipelines import WorktreeSyncOutcome

        client = self._make_app_client()
        pipeline = self._make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")

        outcome = WorktreeSyncOutcome(
            case="divergence_recovered_via_reset",
            hard_reset_performed=True,
            backup_ref="refs/egg-backup/sync-recovery/issue-2792/123",
            discarded_commit_shas=("abc1234 add foo",),
        )

        with (
            patch(
                "routes.phases.get_state_store_for_pipeline",
                return_value=(mock_store, pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=Path("/home/egg/.egg-worktrees/issue-2792/egg"),
            ),
            patch("routes.pipelines._sync_worktree_with_remote", return_value=outcome),
            patch("routes.pipelines._compute_gateway_mode", return_value=("public", None)),
            patch("routes.pipelines._get_spawner", return_value=MagicMock()),
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._fail_pipeline_and_emit_hard_reset_recovery") as mock_fail_emit,
        ):
            resp = client.post("/api/v1/pipelines/issue-2792/phase/populate-contract")

        assert resp.status_code == 409
        import json

        body = json.loads(resp.data)
        assert body["success"] is False
        assert body["reason"] == "hard_reset_recovery_unacked"
        assert body["details"]["hard_reset_performed"] is True
        assert body["details"]["backup_ref"] == "refs/egg-backup/sync-recovery/issue-2792/123"
        assert body["details"]["discarded_commit_shas"] == ["abc1234 add foo"]

        # Populator MUST NOT run on a worktree that was just hard-reset.
        mock_populate.assert_not_called()
        # The shared failure-and-HITL helper MUST have been invoked so
        # the operator sees the same recovery HITL as the phase-boundary
        # sites — without this the 409 body would lie about a HITL that
        # was never emitted (the original B4 wording-vs-reality bug).
        mock_fail_emit.assert_called_once()
        from models import PipelinePhase

        kwargs = mock_fail_emit.call_args.kwargs
        assert kwargs["phase"] == PipelinePhase.IMPLEMENT
        assert kwargs["backup_ref"] == "refs/egg-backup/sync-recovery/issue-2792/123"
        # Helper accepts ``tuple`` or ``list``; the route normalises to
        # list (to share the value with the JSON response body).
        assert list(kwargs["discarded_commit_shas"]) == ["abc1234 add foo"]

    def test_doubly_failed_returns_409_and_emits_hitl(self):
        """When ``_sync_worktree_with_remote`` raises
        ``SyncRebaseAndResetFailedError`` (rebase AND hard-reset both
        failed — worktree still divergent), the route must call the
        shared failure-and-HITL helper, skip the populator, and return
        HTTP 409 with ``reason="sync_rebase_and_reset_failed"``.
        """
        from routes.pipelines import SyncRebaseAndResetFailedError

        client = self._make_app_client()
        pipeline = self._make_pipeline()
        mock_store = MagicMock()
        mock_store.repo_path = Path("/home/egg/repos/egg")

        terminal_err = SyncRebaseAndResetFailedError(
            "rebase failed (conflicts) and hard-reset failed (rc=128, stderr=…)",
            backup_ref="refs/egg-backup/sync-recovery/issue-2792/456",
            discarded_commit_shas=("def5678 add bar",),
        )

        with (
            patch(
                "routes.phases.get_state_store_for_pipeline",
                return_value=(mock_store, pipeline),
            ),
            patch(
                "routes.resolve_worktree_path",
                return_value=Path("/home/egg/.egg-worktrees/issue-2792/egg"),
            ),
            patch(
                "routes.pipelines._sync_worktree_with_remote",
                side_effect=terminal_err,
            ),
            patch("routes.pipelines._compute_gateway_mode", return_value=("public", None)),
            patch("routes.pipelines._get_spawner", return_value=MagicMock()),
            patch("routes.pipelines._populate_contract_from_plan") as mock_populate,
            patch("routes.pipelines._fail_pipeline_and_emit_hard_reset_recovery") as mock_fail_emit,
        ):
            resp = client.post("/api/v1/pipelines/issue-2792/phase/populate-contract")

        assert resp.status_code == 409
        import json

        body = json.loads(resp.data)
        assert body["success"] is False
        assert body["reason"] == "sync_rebase_and_reset_failed"
        # ``hard_reset_performed`` is False on this branch because the
        # hard reset itself failed (it was attempted but did not
        # complete) — distinct from the unacked-recovery case above.
        assert body["details"]["hard_reset_performed"] is False
        assert body["details"]["backup_ref"] == "refs/egg-backup/sync-recovery/issue-2792/456"
        assert body["details"]["discarded_commit_shas"] == ["def5678 add bar"]

        # Populator MUST NOT run on a worktree that is still divergent.
        mock_populate.assert_not_called()
        # The shared failure-and-HITL helper MUST have been invoked so
        # the operator gets the same recovery surface across all three
        # triggers of the hard reset (#2797 B4).
        mock_fail_emit.assert_called_once()
        from models import PipelinePhase

        kwargs = mock_fail_emit.call_args.kwargs
        assert kwargs["phase"] == PipelinePhase.IMPLEMENT
        assert kwargs["backup_ref"] == "refs/egg-backup/sync-recovery/issue-2792/456"
        assert kwargs["discarded_commit_shas"] == ("def5678 add bar",)
        # Doubly-failed branch must pass reset_succeeded=False so the
        # HITL wording reflects the still-divergent state and the
        # "Continue" option is suppressed.
        assert kwargs["reset_succeeded"] is False


class TestHardResetHitlDoublyFailedBranch:
    """#2797 follow-up: when ``SyncRebaseAndResetFailedError`` fires the
    HITL question must reflect the still-divergent state and the options
    must suppress "Continue" — otherwise the operator is offered a
    restart that would loop back into the same failure.
    """

    def test_options_drop_continue_when_reset_failed(self):
        # Success branch keeps both options.
        assert _hard_reset_recovery_hitl_options(reset_succeeded=True) == [
            "Continue with post-reset state",
            "Abort pipeline",
        ]
        # Doubly-failed branch only offers the abort option.
        assert _hard_reset_recovery_hitl_options(reset_succeeded=False) == ["Abort pipeline"]

    def test_question_text_branches_on_reset_succeeded(self):
        from routes.pipelines import PipelinePhase

        succeeded = _hard_reset_recovery_hitl_question(
            pipeline_id="pipeline-xyz",
            phase=PipelinePhase.PLAN,
            backup_ref="refs/egg-backup/sync-recovery/pipeline-xyz/100",
            discarded_commit_shas=("abc1234 add foo",),
            reset_succeeded=True,
        )
        failed = _hard_reset_recovery_hitl_question(
            pipeline_id="pipeline-xyz",
            phase=PipelinePhase.PLAN,
            backup_ref="refs/egg-backup/sync-recovery/pipeline-xyz/100",
            discarded_commit_shas=("abc1234 add foo",),
            reset_succeeded=False,
        )
        # Success wording claims reconciliation completed; failed
        # wording says the reset itself failed and the worktree is
        # still divergent.
        assert "hard-reset HEAD to origin to keep downstream" in succeeded
        assert "still divergent" not in succeeded
        assert "subsequent hard-reset to origin ALSO failed" in failed
        assert "still divergent" in failed
        # The Continue option label MUST NOT appear in the doubly-failed
        # prose — the helper suppresses it and the question shouldn't
        # advertise it either (operators copy-paste resolutions).
        assert "Continue with post-reset state" not in failed
        assert "Abort pipeline" in failed
        # Success prose still lists both options.
        assert "Continue with post-reset state" in succeeded
        assert "Abort pipeline" in succeeded

    def test_emit_passes_reset_succeeded_to_options_and_question(self):
        """The emission helper must thread ``reset_succeeded`` through to
        both the question builder and the options list — otherwise the
        operator could see doubly-failed wording with a "Continue"
        option, or vice versa."""
        from routes.pipelines import PipelinePhase

        captured: dict = {}

        def fake_persist(
            pipeline_id, pipeline, store, *, question, options, phase=None, context=None
        ):  # noqa: ANN001
            captured["options"] = options
            captured["question"] = question
            return MagicMock(id="decision-9", context=context)

        with patch("routes.pipelines._persist_hitl_decision", side_effect=fake_persist):
            _emit_hard_reset_recovery_hitl(
                "pipeline-xyz",
                MagicMock(),
                MagicMock(),
                phase=PipelinePhase.PLAN,
                backup_ref="refs/egg-backup/sync-recovery/pipeline-xyz/100",
                discarded_commit_shas=("abc1234 foo",),
                reset_succeeded=False,
            )

        assert captured["options"] == ["Abort pipeline"]
        assert "still divergent" in captured["question"]


class TestFailPipelineAndEmitHelper:
    """#2797 follow-up: direct unit test of
    :func:`_fail_pipeline_and_emit_hard_reset_recovery` — the previous
    tests mocked the helper out at the call sites, so the lock /
    save / emit / hook / event sequence had no isolated coverage.
    """

    def _make_pipeline(self):
        from models import (
            PhaseExecution,
            Pipeline,
            PipelinePhase,
            PipelineStatus,
        )

        pipeline = Pipeline(
            id="issue-2792",
            issue_number=2792,
            repo="owner/repo",
            branch="egg/issue-2792",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        pipeline.phases = {
            PipelinePhase.IMPLEMENT.value: PhaseExecution(
                phase=PipelinePhase.IMPLEMENT,
                status=PipelineStatus.RUNNING,
            ),
        }
        return pipeline

    def test_writes_failed_under_lock_then_emits_hitl_and_events(self):
        """Happy path: helper acquires the pipeline state lock, writes
        ``status=FAILED`` on both pipeline and phase_exec, persists the
        HITL (under the same reentrant lock), then broadcasts the
        ``pipeline.failed`` event via both the StatusReporter and the
        pipeline event stream.
        """
        from models import PipelinePhase, PipelineStatus

        pipeline = self._make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        call_order: list[str] = []

        def fake_save(p):  # noqa: ANN001
            call_order.append("save_pipeline")
            assert p.status == PipelineStatus.FAILED
            assert p.error == "boom"
            phase_exec = p.get_phase_execution(PipelinePhase.IMPLEMENT)
            assert phase_exec is not None
            assert phase_exec.status == PipelineStatus.FAILED
            assert phase_exec.error == "boom"
            assert phase_exec.completed_at is not None

        mock_store.save_pipeline.side_effect = fake_save

        def fake_emit(*args, **kwargs):  # noqa: ANN001
            call_order.append("emit_hitl")
            return MagicMock()

        def fake_report(*args, **kwargs):  # noqa: ANN001
            call_order.append("report_pipeline_status")

        def fake_event(*args, **kwargs):  # noqa: ANN001
            call_order.append("emit_pipeline_event")

        with (
            patch(
                "routes.pipelines._emit_hard_reset_recovery_hitl", side_effect=fake_emit
            ) as m_emit,
            patch("routes.pipelines.report_pipeline_status", side_effect=fake_report) as m_report,
            patch("routes.pipelines._emit_pipeline_event", side_effect=fake_event) as m_event,
        ):
            _fail_pipeline_and_emit_hard_reset_recovery(
                "issue-2792",
                mock_store,
                phase=PipelinePhase.IMPLEMENT,
                error_message="boom",
                backup_ref="refs/egg-backup/sync-recovery/issue-2792/42",
                discarded_commit_shas=("abc1234 add foo",),
            )

        # Order matters: FAILED must be persisted before the HITL is
        # written, and the HITL must be written before the public
        # pipeline.failed event so subscribers reading state on the
        # event see both the FAILED status and the pending decision.
        assert call_order == [
            "save_pipeline",
            "emit_hitl",
            "report_pipeline_status",
            "emit_pipeline_event",
        ]

        emit_kwargs = m_emit.call_args.kwargs
        assert emit_kwargs["phase"] == PipelinePhase.IMPLEMENT
        assert emit_kwargs["backup_ref"] == "refs/egg-backup/sync-recovery/issue-2792/42"
        assert emit_kwargs["discarded_commit_shas"] == ("abc1234 add foo",)
        # Default is reset_succeeded=True.
        assert emit_kwargs["reset_succeeded"] is True

        report_kwargs = m_report.call_args.kwargs
        assert report_kwargs["event_type"] == "pipeline.failed"
        assert "boom" in report_kwargs["message"]

        m_event.assert_called_once()
        event_args = m_event.call_args.args
        assert event_args[1] == "pipeline.failed"

    def test_pre_event_hook_runs_between_hitl_and_public_event(self):
        """The post-phase ``_run_pipeline`` sites pass a hook that tears
        down the per-phase overseer container — it must run after the
        HITL is persisted (so the operator sees the decision before the
        container disappears) and before the public event (so observers
        don't race the teardown)."""
        from models import PipelinePhase

        pipeline = self._make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        call_order: list[str] = []
        mock_store.save_pipeline.side_effect = lambda p: call_order.append("save")
        hook = MagicMock(side_effect=lambda: call_order.append("hook"))

        with (
            patch(
                "routes.pipelines._emit_hard_reset_recovery_hitl",
                side_effect=lambda *a, **k: call_order.append("emit_hitl"),
            ),
            patch(
                "routes.pipelines.report_pipeline_status",
                side_effect=lambda *a, **k: call_order.append("report"),
            ),
            patch(
                "routes.pipelines._emit_pipeline_event",
                side_effect=lambda *a, **k: call_order.append("event"),
            ),
        ):
            _fail_pipeline_and_emit_hard_reset_recovery(
                "issue-2792",
                mock_store,
                phase=PipelinePhase.IMPLEMENT,
                error_message="boom",
                backup_ref=None,
                discarded_commit_shas=(),
                pre_event_hook=hook,
            )

        hook.assert_called_once()
        assert call_order == ["save", "emit_hitl", "hook", "report", "event"]

    def test_reset_succeeded_false_threads_through_to_emit(self):
        """The helper must forward ``reset_succeeded=False`` to the HITL
        emission so the question/options reflect the doubly-failed
        state — otherwise the operator could see "Continue" on a
        worktree that's still divergent."""
        from models import PipelinePhase

        pipeline = self._make_pipeline()
        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._emit_hard_reset_recovery_hitl") as m_emit,
            patch("routes.pipelines.report_pipeline_status"),
            patch("routes.pipelines._emit_pipeline_event"),
        ):
            _fail_pipeline_and_emit_hard_reset_recovery(
                "issue-2792",
                mock_store,
                phase=PipelinePhase.IMPLEMENT,
                error_message="rebase+reset both failed",
                backup_ref="refs/egg-backup/sync-recovery/issue-2792/99",
                discarded_commit_shas=("def5678 add bar",),
                reset_succeeded=False,
            )

        assert m_emit.call_args.kwargs["reset_succeeded"] is False

    def test_failed_write_and_hitl_persist_held_under_same_lock(self):
        """#2797 follow-up: the helper must hold
        ``get_pipeline_state_lock(pipeline_id)`` across both the outer
        ``store.save_pipeline`` write that pins ``status=FAILED`` *and*
        the inner ``_persist_hitl_decision`` save that writes the
        decision.  A concurrent reader from another thread attempting
        to acquire the same lock between the two writes must be
        blocked until both have landed — otherwise an observer could
        see ``status=FAILED`` without the pending decision (the
        invariant the helper exists to enforce).

        This test exercises the real ``threading.RLock`` from
        ``state_store.get_pipeline_state_lock`` and the real
        ``_persist_hitl_decision`` (only ``store`` and the event
        broadcasters are mocked), so a future refactor that
        accidentally drops the lock-spanning behavior would be caught
        here even if the mock-based call-order tests still pass.
        """
        from models import PipelinePhase, PipelineStatus
        from routes.pipelines import (
            _persist_hitl_decision,  # noqa: F401  # ensure real symbol present
        )
        from state_store import get_pipeline_state_lock

        # A unique pipeline_id keeps the per-pipeline lock isolated
        # from any other test that may share the module-global lock
        # registry in ``state_store``.
        pipeline_id = "real-lock-test-issue-2792"
        pipeline = self._make_pipeline()
        pipeline.id = pipeline_id

        # Real per-pipeline RLock — same instance the helper acquires.
        lock = get_pipeline_state_lock(pipeline_id)

        # At every ``save_pipeline`` call, spawn a worker thread that
        # tries to acquire the lock non-blocking.  RLock allows
        # reentrance from the same thread but blocks others — so if
        # the helper still holds the lock at that point, the worker's
        # ``acquire(blocking=False)`` returns False.
        acquisitions_during_save: list[bool] = []

        def probe_other_thread() -> None:
            result = {"acquired": False}

            def attempt() -> None:
                if lock.acquire(blocking=False):
                    try:
                        result["acquired"] = True
                    finally:
                        lock.release()

            t = threading.Thread(target=attempt)
            t.start()
            t.join(timeout=2.0)
            acquisitions_during_save.append(result["acquired"])

        mock_store = MagicMock()
        mock_store.load_pipeline.return_value = pipeline

        def fake_save(_p):  # noqa: ANN001
            probe_other_thread()

        mock_store.save_pipeline.side_effect = fake_save

        # Patch only the event broadcasters — let ``_persist_hitl_decision``
        # run for real so its inner ``load → add_decision → save`` is
        # the second save call we probe.
        with (
            patch("routes.pipelines.report_pipeline_status"),
            patch("routes.pipelines._emit_pipeline_event"),
        ):
            _fail_pipeline_and_emit_hard_reset_recovery(
                pipeline_id,
                mock_store,
                phase=PipelinePhase.IMPLEMENT,
                error_message="boom",
                backup_ref="refs/egg-backup/sync-recovery/test/123",
                discarded_commit_shas=("abc1234 add foo",),
            )

        # Two save_pipeline calls: outer pins FAILED, inner persists
        # the HITL.  The lock must have been held (un-acquirable from
        # another thread) at *both* points.
        assert mock_store.save_pipeline.call_count == 2, (
            f"expected 2 save_pipeline calls (outer FAILED + inner HITL); "
            f"got {mock_store.save_pipeline.call_count}"
        )
        assert acquisitions_during_save == [False, False], (
            f"lock was acquirable from another thread during save_pipeline; "
            f"per-call results: {acquisitions_during_save} "
            f"(False=held by helper, True=lock dropped between writes)"
        )

        # After the helper returns, the lock must be released so the
        # next operator action (e.g. resolve_decision) can acquire it.
        assert lock.acquire(blocking=False), (
            "helper did not release pipeline state lock after returning"
        )
        lock.release()

        # Sanity-check the persisted state mirrors what the helper
        # claimed to write: FAILED + a decision visible on the same
        # pipeline object the inner save received.
        assert pipeline.status == PipelineStatus.FAILED
        assert len(pipeline.decisions) == 1
        assert pipeline.decisions[0].context == "hard_reset_recovery:implement"

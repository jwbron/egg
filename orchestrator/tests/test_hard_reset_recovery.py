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

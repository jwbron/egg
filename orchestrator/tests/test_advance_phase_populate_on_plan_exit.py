"""Regression tests for #1941: advance_phase must populate the contract from
the plan draft when leaving the plan phase.

Without this, a force=true advance out of `plan` (the supported recovery
hammer) silently skips the ``yaml-tasks`` → ``contract.pr`` transformation,
and the PR phase's auto-PR path falls back to placeholder title/body.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing models
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

try:
    from flask import Flask
    from routes.phases import phases_bp

    _HAS_FLASK = True
except ImportError:
    _HAS_FLASK = False


@pytest.fixture
def app():
    if not _HAS_FLASK:
        pytest.skip("Flask not available")
    app = Flask(__name__)
    app.register_blueprint(phases_bp)
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_pipeline(
    pipeline_id="issue-1882",
    phase=PipelinePhase.PLAN,
    phase_status=PipelineStatus.COMPLETE,
    issue_number=1882,
):
    pipeline = Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo="owner/repo",
        branch=f"egg/issue-{issue_number}",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )
    phase_exec = pipeline.get_phase_execution(phase)
    phase_exec.status = phase_status
    phase_exec.completed_at = datetime.now(UTC)
    return pipeline


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestAdvancePhasePopulatesOnPlanExit:
    """advance_phase must invoke the populate step when leaving the plan phase."""

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan_safe")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_force_advance_out_of_plan_calls_populate(
        self,
        mock_get_store,
        mock_get_lock,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_thread_cls,
        client,
    ):
        """force=true advance from plan→pr routes through the populate helper."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_thread_cls.return_value = MagicMock()

        resp = client.post(
            "/api/v1/pipelines/issue-1882/phase",
            json={"target_phase": "pr", "force": True},
        )

        assert resp.status_code == 200
        mock_populate.assert_called_once()
        args = mock_populate.call_args[0]
        assert args[0] == Path("/tmp/wt")
        assert args[1] == "issue-1882"
        # pipeline_mode defaults to "issue"
        assert args[2] == "issue"
        assert args[3] == 1882

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan_safe")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_populate_is_followed_by_commit(
        self,
        mock_get_store,
        mock_get_lock,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_thread_cls,
        client,
    ):
        """Populate is followed by a scoped commit so _sync_worktree_with_remote
        in the new thread pushes the change rather than resetting it."""
        # Use a shared call tracker to verify ordering across mocks.
        call_order = []
        mock_populate.side_effect = lambda *a, **kw: call_order.append("populate")
        orig_commit_side_effect = mock_commit.side_effect

        def _track_commit(*args, **kwargs):
            call_order.append("commit")
            if orig_commit_side_effect:
                return orig_commit_side_effect(*args, **kwargs)

        mock_commit.side_effect = _track_commit

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_thread_cls.return_value = MagicMock()

        resp = client.post(
            "/api/v1/pipelines/issue-1882/phase",
            json={"target_phase": "pr", "force": True},
        )

        assert resp.status_code == 200
        # _persist_phase_brc_history (called earlier in advance_phase) also
        # hits _commit_statefiles_to_worktree; the populate-exit commit is
        # the one that targets the resolved worktree path with pipeline_id.
        populate_commits = [
            c
            for c in mock_commit.call_args_list
            if c.kwargs.get("pipeline_id") == "issue-1882"
            and "plan-phase exit" in (c.args[1] if len(c.args) > 1 else "")
        ]
        assert len(populate_commits) == 1
        assert populate_commits[0].args[0] == Path("/tmp/wt")
        assert populate_commits[0].kwargs["pipeline_identifier"] is not None

        # Verify populate was called *before* the plan-exit commit.
        # call_order tracks all populate and commit invocations; the
        # populate entry must precede the first plan-exit commit.
        assert "populate" in call_order, "populate was never called"
        populate_idx = call_order.index("populate")
        commit_indices = [i for i, v in enumerate(call_order) if v == "commit"]
        assert any(ci > populate_idx for ci in commit_indices), (
            f"no commit followed populate; call_order={call_order}"
        )

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan_safe")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_advance_from_non_plan_phase_skips_populate(
        self,
        mock_get_store,
        mock_get_lock,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_thread_cls,
        client,
    ):
        """An advance that does not leave plan must not call the populate helper."""
        pipeline = _make_pipeline(
            phase=PipelinePhase.IMPLEMENT,
            phase_status=PipelineStatus.COMPLETE,
        )
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_thread_cls.return_value = MagicMock()

        resp = client.post(
            "/api/v1/pipelines/issue-1882/phase",
            json={"target_phase": "pr"},
        )

        assert resp.status_code == 200
        mock_populate.assert_not_called()
        # _persist_phase_brc_history also invokes _commit_statefiles_to_worktree;
        # we only care that the plan-exit commit didn't fire.
        exit_commits = [
            c
            for c in mock_commit.call_args_list
            if "plan-phase exit" in (c.args[1] if len(c.args) > 1 else "")
        ]
        assert exit_commits == []

    @patch("routes.pipelines._spawn_pipeline_run_thread")
    @patch("routes.pipelines._commit_statefiles_to_worktree")
    @patch("routes.pipelines._populate_contract_from_plan_safe")
    @patch("routes.resolve_worktree_path")
    @patch("routes.phases.get_pipeline_state_lock")
    @patch("routes.phases.get_state_store_for_pipeline")
    def test_populate_failure_does_not_block_advance(
        self,
        mock_get_store,
        mock_get_lock,
        mock_resolve_wt,
        mock_populate,
        mock_commit,
        mock_thread_cls,
        client,
    ):
        """A crash in the populate path must not block a force=true advance.

        The advance-phase hammer is used to unstick pipelines; blocking it on
        a populate failure would defeat the purpose.
        """
        # _populate_contract_from_plan_safe is already exception-swallowing
        # in production, but guard against regressions where a caller moves
        # to the non-safe variant.
        mock_populate.side_effect = RuntimeError("parse failure")

        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        mock_store = MagicMock()
        mock_store.repo_path = Path("/tmp/repo")
        mock_store.load_pipeline.return_value = pipeline
        mock_get_store.return_value = (mock_store, pipeline)
        mock_get_lock.return_value = MagicMock()
        mock_resolve_wt.return_value = Path("/tmp/wt")
        mock_thread_cls.return_value = MagicMock()

        resp = client.post(
            "/api/v1/pipelines/issue-1882/phase",
            json={"target_phase": "pr", "force": True},
        )

        assert resp.status_code == 200
        # thread still spawned — pipeline continues
        mock_thread_cls.assert_called_once()


@pytest.mark.skipif(not _HAS_FLASK, reason="Flask not available")
class TestPopulateFromPlanSafeIntegration:
    """End-to-end check that the populate helper writes contract.pr from the
    plan draft's yaml-tasks appendix.  Guards against the #1941 symptom where
    contract.pr remained empty after a force-advance out of plan.
    """

    def test_populate_from_plan_safe_populates_contract_pr(self, tmp_path):
        """Given a plan draft with a yaml-tasks appendix and an empty contract.pr,
        the helper writes the pr block into the contract — exactly the
        transformation that was being skipped on force=true advances out of
        plan (#1941).
        """
        import textwrap

        from egg_contracts.loader import create_contract, load_contract
        from routes.pipelines import _populate_contract_from_plan_safe

        pipeline_id = "issue-1882"
        issue_number = 1882

        # Blank contract — no pr metadata.  Mirrors the state observed on
        # PR #1937 in #1938.
        create_contract(pipeline_id=pipeline_id, title="Test", repo_root=tmp_path)

        # Plan draft with a populated yaml-tasks appendix.
        drafts_dir = tmp_path / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        plan_path = drafts_dir / f"{issue_number}-plan.md"
        plan_path.write_text(
            textwrap.dedent("""\
                # Plan

                ## Phase 1

                ```yaml
                # yaml-tasks
                pr:
                  title: "Fix the thing"
                  description: |
                    Closes #1882 with the thing fixed.
                phases:
                  - id: 1
                    name: Implement
                    goal: Fix the thing
                    tasks:
                      - id: TASK-1-1
                        description: "Fix the thing"
                        acceptance: "It is fixed"
                        files:
                          - src/thing.py
                ```
            """)
        )

        _populate_contract_from_plan_safe(tmp_path, pipeline_id, "issue", issue_number)

        reloaded = load_contract(pipeline_id, tmp_path)
        assert reloaded.pr is not None
        assert reloaded.pr.title == "Fix the thing"
        assert "Closes #1882" in reloaded.pr.description

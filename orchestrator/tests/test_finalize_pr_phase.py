"""
Tests for ``_finalize_pr_phase_failed`` — the PR-phase finalizer that creates
the PR (possibly against a stale remote HEAD) and persists the result.

These tests cover the fallback path added for jwbron/egg#1731:
when the orchestrator's push fails but the agents' work is already on
origin, the pipeline should still open the PR rather than failing.

The ``TestFinalizePrPhaseStateWriteback`` class at the bottom covers #1911:
after a successful PR creation, the finalizer must write ``pr_number`` and
``pr_head_sha`` back onto the pipeline (inside the lock+reload+save
transaction) so downstream consumers — in particular the overseer's
post-consensus stall detector — can tell that the implement phase is done
transitioning.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from models import Pipeline, PipelinePhase, PipelineStatus
from routes.pipelines import _finalize_pr_phase_failed


def _make_pipeline(
    issue_number=42,
    repo="owner/repo",
    branch="egg/issue-42",
):
    return Pipeline(
        id=f"issue-{issue_number}",
        issue_number=issue_number,
        repo=repo,
        branch=branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.PR,
    )


class TestFinalizePrPhase:
    def test_push_ok_and_pr_url_stores_artifact_and_reports_no_failure(self):
        """Happy path: push succeeded, _auto_create_pr returns URL,
        artifacts captured, returns False (not failed)."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            mock_create.return_value = "https://github.com/owner/repo/pull/99"
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        assert failed is False
        phase_execution = pipeline.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.artifacts == {"pr_url": "https://github.com/owner/repo/pull/99"}
        store.save_pipeline.assert_called_once_with(pipeline)

    def test_push_failed_but_fallback_pr_url_still_stores_artifact(self):
        """#1731 fallback: push failed, but PR is opened against remote
        HEAD and the URL is returned — treat as success."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            mock_create.return_value = "https://github.com/owner/repo/pull/100"
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=False,  # push failed
            )

        assert failed is False
        phase_execution = pipeline.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.artifacts == {"pr_url": "https://github.com/owner/repo/pull/100"}
        # _auto_create_pr was called despite push_ok=False
        mock_create.assert_called_once()

    def test_push_ok_but_pr_url_none_uses_generic_reason(self):
        """Push succeeded but gateway.create_pr returned None — use the
        generic ``no PR URL returned`` reason."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._handle_pr_creation_failure") as mock_fail,
        ):
            mock_create.return_value = None
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        assert failed is True
        mock_fail.assert_called_once()
        call_kwargs = mock_fail.call_args.kwargs
        assert call_kwargs["reason"] == "no PR URL returned"

    def test_push_failed_and_fallback_failed_uses_actionable_reason(self):
        """Both reconcile and fallback PR creation failed — the caller gets
        a specific reason naming both failure modes."""
        pipeline = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = pipeline

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines.get_pipeline_state_lock"),
            patch("routes.pipelines._handle_pr_creation_failure") as mock_fail,
        ):
            mock_create.return_value = None
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=False,
            )

        assert failed is True
        mock_fail.assert_called_once()
        reason = mock_fail.call_args.kwargs["reason"]
        assert "gateway push rejected" in reason
        assert "fetch+rebase reconcile failed" in reason
        assert "fallback PR against remote HEAD" in reason


# ---------------------------------------------------------------------------
# #1911: pipeline.pr_number / pipeline.pr_head_sha writeback after auto-PR
# ---------------------------------------------------------------------------


class TestFinalizePrPhaseStateWriteback:
    """Regression tests for jwbron/egg#1911 task-1-1.

    After successful auto-PR creation the finalizer must populate
    ``pipeline.pr_number`` (parsed from the returned URL) and
    ``pipeline.pr_head_sha`` (fetched via ``_fetch_pr_state``) inside the
    existing lock+reload+save transaction, so the overseer's post-consensus
    stall detector can short-circuit once the implement phase is genuinely
    done transitioning. Both writes land on the ``reloaded`` pipeline via
    ``store.save_pipeline``; callers that still hold the original
    ``pipeline`` reference don't see the mutation (by design — the lock
    protects against concurrent writes).
    """

    def test_writeback_pr_number_and_head_sha_on_success(self):
        """Happy path: PR URL parseable + _fetch_pr_state returns a valid
        head_sha — both fields persisted on the reloaded pipeline."""
        pipeline = _make_pipeline()
        # ``reloaded`` is what the production code writes to inside the lock;
        # that's the object whose attributes we assert on.  Matching ID
        # guarantees validation semantics parity with a real reload.
        reloaded = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = reloaded

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            mock_create.return_value = "https://github.com/owner/repo/pull/99"
            mock_fetch.return_value = {"head_sha": "abc1234def"}
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        assert failed is False
        # Writeback landed on the reloaded pipeline (the one the code
        # actually calls save_pipeline on).
        assert reloaded.pr_number == 99
        assert reloaded.pr_head_sha == "abc1234def"
        # pr_url artifact preserved (existing behavior must not regress).
        phase_execution = reloaded.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.artifacts == {"pr_url": "https://github.com/owner/repo/pull/99"}
        store.save_pipeline.assert_called_once_with(reloaded)
        # Fetch was called with the parsed PR number + the pipeline's repo.
        mock_fetch.assert_called_once()
        fetch_args, fetch_kwargs = mock_fetch.call_args
        all_args = list(fetch_args) + list(fetch_kwargs.values())
        assert 99 in all_args
        assert pipeline.repo in all_args

    def test_writeback_graceful_when_fetch_pr_state_returns_empty(self):
        """Graceful degradation: _fetch_pr_state returns {} (gh unavailable
        or PR not viewable) — pr_number still captured, pr_head_sha stays
        None, phase does NOT fail."""
        pipeline = _make_pipeline()
        reloaded = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = reloaded

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            mock_create.return_value = "https://github.com/owner/repo/pull/99"
            mock_fetch.return_value = {}
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        # Phase still succeeds — graceful degradation, not a failure.
        assert failed is False
        assert reloaded.pr_number == 99
        assert reloaded.pr_head_sha is None
        phase_execution = reloaded.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.artifacts == {"pr_url": "https://github.com/owner/repo/pull/99"}
        store.save_pipeline.assert_called_once_with(reloaded)

    def test_writeback_rejects_invalid_head_sha(self):
        """pr_head_sha is only written when the value matches the
        [0-9a-f]{7,40} regex gate — protects against bogus strings in
        the gh response (non-hex, too short, etc.)."""
        pipeline = _make_pipeline()
        reloaded = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = reloaded

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            mock_create.return_value = "https://github.com/owner/repo/pull/99"
            # "NOT-HEX!" fails the [0-9a-f]{7,40} gate.
            mock_fetch.return_value = {"head_sha": "NOT-HEX!"}
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        assert failed is False
        assert reloaded.pr_number == 99
        # Invalid SHA dropped — no bogus data leaks into the model.
        assert reloaded.pr_head_sha is None

    def test_writeback_skipped_when_pr_url_unparseable(self):
        """Unparseable pr_url (no /pull/<n> segment) — writeback is
        no-op'd and the phase still succeeds with the artifact captured.
        This protects against gh/URL-shape changes that we can't predict."""
        pipeline = _make_pipeline()
        reloaded = _make_pipeline()
        store = MagicMock()
        store.load_pipeline.return_value = reloaded

        with (
            patch("routes.pipelines._auto_create_pr") as mock_create,
            patch("routes.pipelines._fetch_pr_state") as mock_fetch,
            patch("routes.pipelines.get_pipeline_state_lock"),
        ):
            # No ``/pull/<n>`` segment — re.search returns None.
            mock_create.return_value = "https://github.com/owner/repo/pulls?weird"
            mock_fetch.return_value = {"head_sha": "abc1234def"}
            failed = _finalize_pr_phase_failed(
                pipeline=pipeline,
                worktree_repo_path=Path("/tmp/wt"),
                spawner=MagicMock(),
                store=store,
                pipeline_id=pipeline.id,
                current_phase=PipelinePhase.PR,
                gateway_mode="public",
                push_ok=True,
            )

        # The phase still succeeds — the existing pr_url artifact write is
        # the durable part of the contract.  pr_number stays None because
        # we couldn't parse it.
        assert failed is False
        mock_fetch.assert_not_called()
        assert reloaded.pr_number is None
        assert reloaded.pr_head_sha is None
        phase_execution = reloaded.get_phase_execution(PipelinePhase.PR)
        assert phase_execution.artifacts == {"pr_url": "https://github.com/owner/repo/pulls?weird"}

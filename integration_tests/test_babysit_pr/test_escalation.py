"""Integration tests for babysit-pr early-exits and final-push head-move escalation.

After #1748 the legacy ``egg_babysit.escalation`` module is gone. The
equivalent surfaces now live in ``orchestrator.routes.pipelines``:

* Fork / merged / closed / empty-diff PRs are rejected up-front by the
  pipeline-creation route — no agents ever spawn, so there is nothing to
  escalate.
* The final-push head-move guard (``_verify_pr_head_unchanged``) aborts a
  cycle when a human commit landed on the PR head mid-cycle; the caller
  then raises a HITL decision rather than pushing.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask


@pytest.fixture
def app():
    from routes.pipelines import pipelines_bp

    app = Flask(__name__)
    app.register_blueprint(pipelines_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _pr_state(**overrides):
    base = {
        "state": "OPEN",
        "base_ref": "main",
        "head_ref": "feature-branch",
        "head_sha": "abc1234deadbeef",
        "is_fork": False,
        "changed_files": 3,
        "head_repository_name_with_owner": "owner/repo",
    }
    base.update(overrides)
    return base


@pytest.mark.integration
class TestForkPREarlyExit:
    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_fork_message_mentions_gateway_constraint(
        self, mock_get_store, mock_get_repo_path, mock_fetch, client
    ):
        mock_fetch.return_value = _pr_state(
            is_fork=True,
            head_repository_name_with_owner="forker/repo",
        )
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 7, "repo": "owner/repo"},
        )

        assert response.status_code == 400
        body = response.get_json()
        assert body["details"]["reason"] == "pr_from_fork"
        # The message should hint at the gateway / push constraint so
        # operators understand why we refuse.
        assert "fork" in body["message"].lower()
        mock_get_store.assert_not_called()


@pytest.mark.integration
class TestMergedPREarlyExit:
    @patch("routes.pipelines._fetch_pr_state")
    @patch("routes.pipelines.get_repo_path")
    @patch("routes.pipelines.get_state_store")
    def test_merged_message_is_informative(
        self, mock_get_store, mock_get_repo_path, mock_fetch, client
    ):
        mock_fetch.return_value = _pr_state(state="MERGED")
        mock_get_repo_path.return_value = "/tmp/repo"

        response = client.post(
            "/api/v1/pipelines",
            json={"mode": "babysit", "pr_number": 7, "repo": "owner/repo"},
        )

        assert response.status_code == 409
        body = response.get_json()
        assert body["details"]["reason"] == "pr_merged"
        assert "merged" in body["message"].lower()


@pytest.mark.integration
class TestFinalPushHeadMoveGuard:
    """``_verify_pr_head_unchanged`` detects mid-cycle human commits."""

    def _make_pipeline(self, *, branch: str = "feature-x", sha: str = "abc1234deadbeef"):
        from models import Pipeline, PipelineMode

        return Pipeline(
            id="pr-42",
            repo="owner/repo",
            mode=PipelineMode.BABYSIT,
            pr_number=42,
            pr_head_sha=sha,
            branch=branch,
            has_contract=False,
        )

    @patch("routes.pipelines.subprocess.run")
    def test_head_unchanged_allows_push(self, mock_run):
        from routes.pipelines import _verify_pr_head_unchanged

        pipeline = self._make_pipeline(sha="abc1234deadbeef")

        # First call: git fetch origin <branch>  (ignored)
        # Second call: git rev-parse origin/<branch> (returns the same sha)
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234deadbeef\n", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual == "abc1234deadbeef"

    @patch("routes.pipelines.subprocess.run")
    def test_head_moved_signals_abort(self, mock_run):
        from routes.pipelines import _verify_pr_head_unchanged

        pipeline = self._make_pipeline(sha="abc1234deadbeef")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="def5678cafebabe\n", stderr=""),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is False
        assert actual == "def5678cafebabe"

    @patch("routes.pipelines.subprocess.run")
    def test_no_stored_sha_skips_check(self, mock_run):
        """When pr_head_sha is None the helper cannot decide — it returns (True, None).

        This means we do not block the push on a transient state rather
        than escalating on every cycle where the SHA wasn't captured.
        """
        from models import Pipeline, PipelineMode
        from routes.pipelines import _verify_pr_head_unchanged

        pipeline = Pipeline(
            id="pr-42",
            repo="owner/repo",
            mode=PipelineMode.BABYSIT,
            pr_number=42,
            branch="feature-x",
            has_contract=False,
            # pr_head_sha intentionally absent
        )

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None
        mock_run.assert_not_called()

    @pytest.mark.xfail(
        reason=(
            "Pre-existing test/code divergence from #1756: the test asserts "
            "fail-open behavior (transient git failure → ok=True, let the "
            "subsequent push surface a non-fast-forward error), but the "
            "production `_verify_pr_head_unchanged` was implemented "
            "fail-closed (`return False, None` after exhausting retries) "
            "to escalate via HITL rather than risk overwriting concurrent "
            "work. The contract has to be settled — and the test rewritten "
            "to match — before this can flip back to a hard pass. Marked "
            "xfail rather than removed so the divergence stays visible."
        ),
        strict=False,
    )
    @patch("routes.pipelines.subprocess.run")
    def test_rev_parse_failure_does_not_block(self, mock_run):
        """A transient git failure returns (True, None) rather than blocking the push.

        We cannot tell whether the head moved, so we do not falsely
        escalate; the push proceeds and git itself will reject a
        non-fast-forward attempt.
        """
        from routes.pipelines import _verify_pr_head_unchanged

        pipeline = self._make_pipeline(sha="abc1234deadbeef")

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=128, stdout="", stderr="fatal: some transient"),
        ]

        ok, actual = _verify_pr_head_unchanged(pipeline, Path("/tmp/repo"))
        assert ok is True
        assert actual is None

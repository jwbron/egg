"""Tests for the branch-divergence ``OVERSEER_ALERT`` (#2224 PR 3).

Detects the contamination shape from #2222: pipeline branch is
materially ahead of base AND the ahead-commits contain merged-PR
subject signatures (``(#NNNN)``).  The detector is intentionally
cheap and false-positive-tolerant.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from models import (
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes.pipelines import (
    BRANCH_DIVERGENCE_THRESHOLD,
    _check_branch_divergence_for_alert,
    _publish_branch_divergence_alert,
)


def _make_pipeline(pipeline_id: str = "issue-2222") -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        issue_number=2222,
        repo="owner/repo",
        branch=f"egg/{pipeline_id}",
        base_branch="main",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=PipelineConfig(),
    )


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestCheckBranchDivergenceForAlert:
    """Unit tests for ``_check_branch_divergence_for_alert``."""

    def _patch_subprocess(self, side_effects: list[MagicMock]):
        return patch(
            "routes.pipelines.subprocess.run",
            side_effect=list(side_effects),
        )

    def test_returns_empty_when_branch_not_ahead(self):
        """No commits ahead → empty list, no log call needed."""
        with self._patch_subprocess([_completed(0, "0\n")]):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert result == []

    def test_returns_empty_when_below_threshold(self):
        """Threshold is exclusive — at-threshold count returns empty."""
        with self._patch_subprocess([_completed(0, f"{BRANCH_DIVERGENCE_THRESHOLD}\n")]):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert result == []

    def test_returns_empty_when_above_threshold_but_no_signatures(self):
        """Far ahead but no ``(#N)`` signatures → no contamination."""
        log_output = (
            "abc1234\trefine: add analysis for #2222\n"
            "def5678\timplement: scaffold helper\n"
            "ghi9012\tfix: typo in docstring\n"
        )
        with self._patch_subprocess(
            [
                _completed(0, "30\n"),  # rev-list --count
                _completed(0, log_output),  # log
            ]
        ):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert result == []

    def test_returns_offenders_when_pr_signatures_present(self):
        """Ahead + ``(#NNNN)`` subjects → offenders list."""
        log_output = (
            "abc1234\trefine: add analysis for #2222\n"
            "def5678\tFix #2150: tester race condition (#2152)\n"
            "ghi9012\timplement: scaffold helper\n"
            "jkl3456\tFix #2179: another merged thing (#2179)\n"
        )
        with self._patch_subprocess(
            [
                _completed(0, "30\n"),
                _completed(0, log_output),
            ]
        ):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert len(result) == 2
        shas = [sha for sha, _ in result]
        assert shas == ["def5678", "jkl3456"]

    def test_returns_empty_when_rev_list_fails(self):
        """Best-effort: rev-list rc!=0 → empty (no alert)."""
        with self._patch_subprocess([_completed(128, "", "fatal: ...")]):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert result == []

    def test_returns_empty_when_log_fails(self):
        """Best-effort: log rc!=0 after a positive count → empty."""
        with self._patch_subprocess(
            [
                _completed(0, "30\n"),
                _completed(128, "", "fatal: ..."),
            ]
        ):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert result == []

    def test_returns_empty_on_subprocess_timeout(self):
        """``TimeoutExpired`` is swallowed (best-effort observability)."""
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15),
        ):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert result == []

    def test_returns_empty_when_branch_equals_base(self):
        """No-op when caller mistakenly passes branch == base."""
        # No subprocess calls should be made.
        with patch("routes.pipelines.subprocess.run") as mock_run:
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="main",
                base_branch="main",
            )
        assert result == []
        mock_run.assert_not_called()

    def test_threshold_override(self):
        """Caller can lower threshold for testing / aggressive monitoring."""
        log_output = "abc1234\tFix #2150 (#2152)\n"
        with self._patch_subprocess(
            [
                _completed(0, "5\n"),  # 5 commits ahead
                _completed(0, log_output),
            ]
        ):
            result = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                threshold=3,  # below the 5-ahead count
            )
        assert len(result) == 1


class TestPublishBranchDivergenceAlert:
    """Unit tests for ``_publish_branch_divergence_alert``."""

    def test_publishes_overseer_alert_on_offenders(self):
        """OVERSEER_ALERT is published with offenders + ahead count in metadata."""
        pipeline = _make_pipeline()
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            _publish_branch_divergence_alert(
                pipeline,
                "issue-2222",
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                ahead_count=65,
                offenders=[
                    ("abc1234", "Fix #2150 (#2152)"),
                    ("def5678", "Other thing (#2179)"),
                ],
            )

        assert msg_store.add_message.call_count == 1
        msg = msg_store.add_message.call_args.args[0]
        assert msg.message_type == "OVERSEER_ALERT"
        assert msg.subject.startswith("branch-divergence:")
        assert msg.metadata["anomaly_type"] == "branch-divergence"
        assert msg.metadata["ahead_count"] == 65
        assert msg.metadata["offending_shas"] == ["abc1234", "def5678"]
        assert "65 commits" in msg.body
        assert "abc1234"[:12] in msg.body
        assert "#2222" in msg.body  # ties back to the root cause

    def test_swallows_message_store_unavailable(self):
        """Missing message store → log + return, no exception."""
        pipeline = _make_pipeline()
        with patch("routes.pipelines._get_message_store", return_value=None):
            # Must not raise.
            _publish_branch_divergence_alert(
                pipeline,
                "issue-2222",
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                ahead_count=65,
                offenders=[("abc1234", "Fix (#2152)")],
            )

    def test_swallows_message_store_exception(self):
        """add_message raising → log + return, no exception."""
        pipeline = _make_pipeline()
        msg_store = MagicMock()
        msg_store.add_message.side_effect = RuntimeError("redis down")
        store_factory = MagicMock(return_value=msg_store)

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            # Must not raise.
            _publish_branch_divergence_alert(
                pipeline,
                "issue-2222",
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                ahead_count=65,
                offenders=[("abc1234", "Fix (#2152)")],
            )

    def test_truncates_offender_render_above_ten(self):
        """Body shows first 10 offenders + ``... and N more``."""
        pipeline = _make_pipeline()
        offenders = [(f"sha{i:04d}", f"Fix #{i} (#{i})") for i in range(15)]
        msg_store = MagicMock()
        store_factory = MagicMock(return_value=msg_store)

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            _publish_branch_divergence_alert(
                pipeline,
                "issue-2222",
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                ahead_count=200,
                offenders=offenders,
            )

        msg = msg_store.add_message.call_args.args[0]
        assert "and 5 more" in msg.body
        # All 15 SHAs in metadata even though body truncates.
        assert len(msg.metadata["offending_shas"]) == 15

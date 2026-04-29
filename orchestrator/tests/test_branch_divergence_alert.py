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
    _branch_divergence_tick,
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
        """No commits ahead → ``(0, [])``, no log call needed."""
        with self._patch_subprocess([_completed(0, "0\n")]):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 0
        assert offenders == []

    def test_returns_empty_when_at_threshold(self):
        """Threshold is exclusive — at-threshold count returns no offenders.

        ``ahead`` is still surfaced so the caller can log / publish if desired.
        """
        with self._patch_subprocess([_completed(0, f"{BRANCH_DIVERGENCE_THRESHOLD}\n")]):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == BRANCH_DIVERGENCE_THRESHOLD
        assert offenders == []

    def test_returns_empty_when_below_threshold(self):
        """Below-threshold ahead count returns no offenders, log is skipped."""
        with self._patch_subprocess([_completed(0, f"{BRANCH_DIVERGENCE_THRESHOLD - 1}\n")]):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == BRANCH_DIVERGENCE_THRESHOLD - 1
        assert offenders == []

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
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 30
        assert offenders == []

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
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 30
        assert len(offenders) == 2
        shas = [sha for sha, _ in offenders]
        assert shas == ["def5678", "jkl3456"]

    def test_returns_empty_when_rev_list_fails(self):
        """Best-effort: rev-list rc!=0 → ``(0, [])`` (no alert)."""
        with self._patch_subprocess([_completed(128, "", "fatal: ...")]):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 0
        assert offenders == []

    def test_returns_empty_when_log_fails(self):
        """Best-effort: log rc!=0 after a positive count → empty offenders.

        ``ahead`` from the successful ``rev-list`` step is preserved so
        the caller can still log telemetry if it wants.
        """
        with self._patch_subprocess(
            [
                _completed(0, "30\n"),
                _completed(128, "", "fatal: ..."),
            ]
        ):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 30
        assert offenders == []

    def test_returns_empty_on_subprocess_timeout(self):
        """``TimeoutExpired`` is swallowed (best-effort observability)."""
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15),
        ):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 0
        assert offenders == []

    def test_returns_empty_when_branch_equals_base(self):
        """No-op when caller mistakenly passes branch == base."""
        # No subprocess calls should be made.
        with patch("routes.pipelines.subprocess.run") as mock_run:
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="main",
                base_branch="main",
            )
        assert ahead == 0
        assert offenders == []
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
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                threshold=3,  # below the 5-ahead count
            )
        assert ahead == 5
        assert len(offenders) == 1


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
        assert "abc1234" in msg.body  # rendered SHA prefix appears in body
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


class TestBranchDivergenceTick:
    """Integration tests for the polling-thread tick helper.

    Exercises the dedupe set, the empty-offenders reset, the
    pipeline re-load each tick, and the best-effort error swallow.
    """

    def _make_store(self, pipeline: Pipeline) -> MagicMock:
        store = MagicMock()
        store.load_pipeline = MagicMock(return_value=pipeline)
        return store

    def test_publishes_alert_on_first_tick_with_offenders(self):
        pipeline = _make_pipeline()
        store = self._make_store(pipeline)
        alerted: set[str] = set()

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
                return_value=(30, [("abc1234", "Fix (#1)"), ("def5678", "Fix (#2)")]),
            ),
            patch("routes.pipelines._publish_branch_divergence_alert") as mock_publish,
        ):
            _branch_divergence_tick(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                store=store,
                alerted_shas=alerted,
            )

        store.load_pipeline.assert_called_once_with("issue-2222")
        assert mock_publish.call_count == 1
        kwargs = mock_publish.call_args.kwargs
        assert kwargs["ahead_count"] == 30
        assert [sha for sha, _ in kwargs["offenders"]] == ["abc1234", "def5678"]
        assert alerted == {"abc1234", "def5678"}

    def test_dedupes_same_sha_across_ticks(self):
        """Same SHA on the next tick → no second publish."""
        pipeline = _make_pipeline()
        store = self._make_store(pipeline)
        alerted: set[str] = {"abc1234"}

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
                return_value=(30, [("abc1234", "Fix (#1)")]),
            ),
            patch("routes.pipelines._publish_branch_divergence_alert") as mock_publish,
        ):
            _branch_divergence_tick(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                store=store,
                alerted_shas=alerted,
            )

        mock_publish.assert_not_called()
        assert alerted == {"abc1234"}

    def test_publishes_only_new_shas_when_set_partially_overlaps(self):
        """New offender alongside known one → publish only the new one."""
        pipeline = _make_pipeline()
        store = self._make_store(pipeline)
        alerted: set[str] = {"abc1234"}

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
                return_value=(31, [("abc1234", "Fix (#1)"), ("new9999", "Fix (#2)")]),
            ),
            patch("routes.pipelines._publish_branch_divergence_alert") as mock_publish,
        ):
            _branch_divergence_tick(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                store=store,
                alerted_shas=alerted,
            )

        assert mock_publish.call_count == 1
        offenders_arg = mock_publish.call_args.kwargs["offenders"]
        assert [sha for sha, _ in offenders_arg] == ["new9999"]
        assert alerted == {"abc1234", "new9999"}

    def test_resets_dedupe_when_offenders_clear(self):
        """Empty offenders this tick → dedupe set is cleared so a
        re-introduced SHA fires again on a later tick."""
        pipeline = _make_pipeline()
        store = self._make_store(pipeline)
        alerted: set[str] = {"abc1234", "def5678"}

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
                return_value=(0, []),
            ),
            patch("routes.pipelines._publish_branch_divergence_alert") as mock_publish,
        ):
            _branch_divergence_tick(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                store=store,
                alerted_shas=alerted,
            )

        mock_publish.assert_not_called()
        assert alerted == set()

    def test_re_introduction_after_reset_re_fires(self):
        """Tick 1: offenders fire.  Tick 2: cleared (reset).  Tick 3:
        same SHA reappears → publishes again (over-alert posture)."""
        pipeline = _make_pipeline()
        store = self._make_store(pipeline)
        alerted: set[str] = set()

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
                side_effect=[
                    (30, [("abc1234", "Fix (#1)")]),
                    (0, []),
                    (30, [("abc1234", "Fix (#1)")]),
                ],
            ),
            patch("routes.pipelines._publish_branch_divergence_alert") as mock_publish,
        ):
            for _ in range(3):
                _branch_divergence_tick(
                    pipeline_id="issue-2222",
                    worktree_repo_path=Path("/tmp/repo"),
                    store=store,
                    alerted_shas=alerted,
                )

        assert mock_publish.call_count == 2
        assert alerted == {"abc1234"}

    def test_skips_when_branch_or_base_missing(self):
        """No publish + no helper call when pipeline lacks branch/base."""
        pipeline = _make_pipeline()
        pipeline.branch = ""  # missing
        store = self._make_store(pipeline)
        alerted: set[str] = set()

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
            ) as mock_check,
            patch("routes.pipelines._publish_branch_divergence_alert") as mock_publish,
        ):
            _branch_divergence_tick(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                store=store,
                alerted_shas=alerted,
            )

        mock_check.assert_not_called()
        mock_publish.assert_not_called()

    def test_swallows_load_pipeline_exception(self):
        """``store.load_pipeline`` raising must not propagate."""
        store = MagicMock()
        store.load_pipeline.side_effect = RuntimeError("state file gone")
        alerted: set[str] = set()

        # Must not raise.
        _branch_divergence_tick(
            pipeline_id="issue-2222",
            worktree_repo_path=Path("/tmp/repo"),
            store=store,
            alerted_shas=alerted,
        )
        assert alerted == set()

    def test_re_loads_pipeline_each_tick(self):
        """Branch updates mid-pipeline are picked up because the helper
        re-loads on every tick rather than capturing once."""
        pipeline_a = _make_pipeline()
        pipeline_b = _make_pipeline()
        pipeline_b.branch = "egg/renamed"
        store = MagicMock()
        store.load_pipeline.side_effect = [pipeline_a, pipeline_b]
        alerted: set[str] = set()

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
                return_value=(0, []),
            ) as mock_check,
            patch("routes.pipelines._publish_branch_divergence_alert"),
        ):
            _branch_divergence_tick(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                store=store,
                alerted_shas=alerted,
            )
            _branch_divergence_tick(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                store=store,
                alerted_shas=alerted,
            )

        assert store.load_pipeline.call_count == 2
        # The second call's pipeline_branch reflects the updated branch.
        branches = [call.kwargs["pipeline_branch"] for call in mock_check.call_args_list]
        assert branches == ["egg/issue-2222", "egg/renamed"]

"""Tests for the branch-divergence ``OVERSEER_ALERT`` (#2222 / #2224, #2270 §2c).

Detects the contamination shape from #2222: the pipeline branch has absorbed
merged-main commits.  Slice-7 of #2270 (task-7-5) **replaces the cheap
``(#NNNN)`` subject heuristic** (``_BRANCH_DIVERGENCE_PR_RE``) with a patch-id
test — an ahead-commit is contamination iff its patch-id matches a commit already
in ``origin/<base>`` history — and caps the scan window
(``_BRANCH_DIVERGENCE_SCAN_CAP``).  The subject heuristic produced #2222 false
positives whenever an agent legitimately wrote a ``(#NNNN)`` reference into a
commit subject; the patch-id test cannot.  (The companion snapshot detector
``detect_branch_divergence`` also honors the ancestor-of-base signal; that facet
is pinned by the slice-1 calibration corpus, see ``test_overseer_calibration``.)

Test layout:

* ``TestCheckBranchDivergenceForAlert`` — the impl-agnostic best-effort contract
  (branch==base no-op, rev-list failure, timeout) that holds under both the old
  and the new detector.
* ``TestBranchDivergencePatchId`` — the slice-7 §2c contract: patch-id collision
  classification and the #2222 subject-false-positive fix.  Skipped on the tester
  branch alone (the new impl lands with the coder reconcile, gated on the
  ``_BRANCH_DIVERGENCE_SCAN_CAP`` sentinel).
* ``TestBranchDivergenceContract`` — module-level invariants: the scan-window cap
  exists and the subject regex is retired.
* ``TestPublishBranchDivergenceAlert`` / ``TestBranchDivergenceTick`` — the alert
  publish + polling-thread dedupe behavior, which operate on the
  ``(ahead, offenders)`` return shape and are independent of HOW offenders are
  classified.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from models import (
    Pipeline,
    PipelineConfig,
    PipelinePhase,
    PipelineStatus,
)
from routes import pipelines as pipelines_mod
from routes.pipelines import (
    _branch_divergence_tick,
    _check_branch_divergence_for_alert,
    _publish_branch_divergence_alert,
)

# The slice-7 scan-window cap is the sentinel that the patch-id detector has
# landed. On the tester branch alone it is absent and the §2c behavioral tests
# skip; the coder reconcile adds it alongside the new classification.
_SCAN_CAP = getattr(pipelines_mod, "_BRANCH_DIVERGENCE_SCAN_CAP", None)
_HAS_NEW_IMPL = isinstance(_SCAN_CAP, int)
_requires_new_impl = pytest.mark.skipif(
    not _HAS_NEW_IMPL,
    reason="patch-id branch-divergence lands with the slice-7 coder reconcile (#2270 §2c)",
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


# ---------------------------------------------------------------------------
# Arg-aware git responder — order-independent so the test does not encode the
# exact call sequence. It models the patch-id pipeline the slice-7 detector runs:
# ``git log -p … <range>`` piped to ``git patch-id --stable`` yields a
# ``patch_id -> sha`` map for both base history and the ahead range; an
# ahead-commit is contamination iff its patch-id collides with a base patch-id.
# ---------------------------------------------------------------------------


def _git_responder(
    *,
    ahead: int,
    base_patch_ids: list[str],
    ahead_rows: list[tuple[str, str]],
    subjects: dict[str, str] | None = None,
):
    """Build a ``subprocess.run`` side-effect that answers git probes by content.

    * ``rev-list --count BASE..BRANCH``         → ``ahead``.
    * ``log -p … BASE``        + ``patch-id``   → base ``patch_id sha`` lines.
    * ``log -p … BASE..BRANCH`` + ``patch-id``  → ahead ``patch_id sha`` lines
      (from ``ahead_rows`` as ``(patch_id, sha)``).
    * ``log --pretty=format … BASE..BRANCH``    → ``sha\\tsubject`` for the body.

    Contamination = an ahead patch-id present in ``base_patch_ids``; subjects are
    irrelevant to classification (the #2222 false-positive fix).
    """
    subjects = subjects or {}
    calls: list[list[str]] = []
    state = {"last_logp_range": None}

    def _run(cmd, *args, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)]
        calls.append(argv)
        is_log = any(str(x) == "log" for x in argv)
        is_log_p = is_log and any(str(x) == "-p" for x in argv)
        is_pretty_log = is_log and any(str(x).startswith("--pretty=format") for x in argv)
        text = " ".join(str(x) for x in argv)

        if "rev-list" in text and "--count" in text:
            return _completed(0, f"{ahead}\n")
        if is_log_p:
            # The rev-range is the final arg; remember whether it is the base
            # history or the ahead range so the following patch-id call answers
            # for the right set.
            rng = str(argv[-1])
            state["last_logp_range"] = "ahead" if ".." in rng else "base"
            return _completed(0, f"DIFF::{state['last_logp_range']}\n")
        if "patch-id" in text:
            if state["last_logp_range"] == "base":
                out = "".join(f"{pid} basesha{i}\n" for i, pid in enumerate(base_patch_ids))
            else:
                out = "".join(f"{pid} {sha}\n" for pid, sha in ahead_rows)
            return _completed(0, out)
        if is_pretty_log:
            out = "".join(f"{sha}\t{subjects.get(sha, 'subject')}\n" for _, sha in ahead_rows)
            return _completed(0, out)
        return _completed(0, "")

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# ---------------------------------------------------------------------------
# Impl-agnostic best-effort contract — holds under both old and new detectors.
# ---------------------------------------------------------------------------


class TestCheckBranchDivergenceForAlert:
    """Best-effort / early-exit contract for ``_check_branch_divergence_for_alert``."""

    def test_returns_empty_when_branch_not_ahead(self):
        """No commits ahead → ``(0, [])`` after a single rev-list call."""
        with patch("routes.pipelines.subprocess.run", side_effect=[_completed(0, "0\n")]):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 0
        assert offenders == []

    def test_returns_empty_when_rev_list_fails(self):
        """Best-effort: rev-list rc!=0 → ``(0, [])`` (no alert)."""
        with patch("routes.pipelines.subprocess.run", side_effect=[_completed(128, "", "fatal")]):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 0
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
        """No-op (no git calls) when caller passes branch == base."""
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


# ---------------------------------------------------------------------------
# Slice-7 §2c — ancestor-of-base OR patch-id classification (#2222/#2224).
# ---------------------------------------------------------------------------


@_requires_new_impl
class TestBranchDivergencePatchId:
    """The structural detector: patch-id collision against base, NOT subjects."""

    def test_clean_branch_with_pr_subjects_is_not_flagged(self):
        """#2222 fix: ahead-commits whose subjects look like merged PRs but whose
        patch-ids do NOT collide with base history → NO offenders.

        This is the exact false positive the subject regex produced — an agent
        legitimately referencing ``(#NNNN)`` in a commit subject.
        """
        responder = _git_responder(
            ahead=30,
            base_patch_ids=["basepidA", "basepidB"],
            ahead_rows=[("aheadpid1", "abc1234"), ("aheadpid2", "def5678")],
            subjects={
                "abc1234": "refine: analysis referencing (#2222)",
                "def5678": "implement: port fix from (#2152)",
            },
        )
        with patch("routes.pipelines.subprocess.run", side_effect=responder):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 30
        assert offenders == [], "subject (#NNNN) signatures must no longer trigger offenders"

    def test_patch_id_collision_is_flagged(self):
        """An ahead-commit whose patch-id matches a base commit (reabsorbed
        merged-main) IS contamination — regardless of its subject.
        """
        responder = _git_responder(
            ahead=30,
            base_patch_ids=["basepidA", "collidepid"],
            ahead_rows=[("aheadpid1", "clean001"), ("collidepid", "leaked02")],
            subjects={"leaked02": "ordinary-looking subject"},
        )
        with patch("routes.pipelines.subprocess.run", side_effect=responder):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert ahead == 30
        offender_shas = [sha for sha, _ in offenders]
        assert offender_shas == ["leaked02"]

    def test_below_threshold_skips_classification(self):
        """A branch only a few commits ahead is never contamination — the
        patch-id classification (and its git probes) is skipped under the threshold.
        """
        responder = _git_responder(ahead=3, base_patch_ids=["basepidA"], ahead_rows=[])
        with patch("routes.pipelines.subprocess.run", side_effect=responder):
            ahead, offenders = _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        assert offenders == []
        # No patch-id probing happened below threshold.
        probed = [
            c
            for c in responder.calls  # type: ignore[attr-defined]
            if any("patch-id" in str(x) for x in c)
        ]
        assert probed == []

    def test_scan_window_is_capped(self):
        """The patch-id enumeration is bounded by ``_BRANCH_DIVERGENCE_SCAN_CAP``.

        A pathologically-diverged branch (thousands ahead) must not unbounded-scan;
        the cap is applied to the ``git log -p`` enumeration (``--max-count``).
        """
        responder = _git_responder(
            ahead=5000,
            base_patch_ids=["basepidA"],
            ahead_rows=[("aheadpid1", "clean001")],
        )
        with patch("routes.pipelines.subprocess.run", side_effect=responder):
            _check_branch_divergence_for_alert(
                pipeline_id="issue-2222",
                worktree_repo_path=Path("/tmp/repo"),
                pipeline_branch="egg/issue-2222",
                base_branch="main",
            )
        logp_calls = [
            c
            for c in responder.calls  # type: ignore[attr-defined]
            if any(str(x) == "log" for x in c) and any(str(x) == "-p" for x in c)
        ]
        assert logp_calls, "expected a git log -p enumeration call"
        for call in logp_calls:
            flat = " ".join(str(x) for x in call)
            assert (
                f"--max-count={_SCAN_CAP}" in flat
                or f"-n {_SCAN_CAP}" in flat
                or str(_SCAN_CAP) in flat
            ), f"git log -p must be capped by _BRANCH_DIVERGENCE_SCAN_CAP ({_SCAN_CAP})"


class TestBranchDivergenceContract:
    """Module-level invariants for the slice-7 §2c rewrite."""

    @_requires_new_impl
    def test_scan_cap_is_a_positive_int(self):
        assert isinstance(_SCAN_CAP, int) and _SCAN_CAP > 0

    @_requires_new_impl
    def test_subject_regex_is_retired(self):
        """``_BRANCH_DIVERGENCE_PR_RE`` must no longer gate divergence (#2222).

        The structural patch-id test replaces it; leaving the regex as the firing
        condition would reintroduce the false-positive flood.
        """
        regex = getattr(pipelines_mod, "_BRANCH_DIVERGENCE_PR_RE", None)
        assert regex is None, "the (#NNNN) subject regex must be removed in favor of patch-id"


# ---------------------------------------------------------------------------
# Alert publish — independent of how offenders are classified.
# ---------------------------------------------------------------------------


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
                    ("abc1234", "Some merged change"),
                    ("def5678", "Another merged change"),
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
            _publish_branch_divergence_alert(
                pipeline,
                "issue-2222",
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                ahead_count=65,
                offenders=[("abc1234", "Merged change")],
            )

    def test_swallows_message_store_exception(self):
        """add_message raising → log + return, no exception."""
        pipeline = _make_pipeline()
        msg_store = MagicMock()
        msg_store.add_message.side_effect = RuntimeError("redis down")
        store_factory = MagicMock(return_value=msg_store)

        with patch("routes.pipelines._get_message_store", return_value=store_factory):
            _publish_branch_divergence_alert(
                pipeline,
                "issue-2222",
                pipeline_branch="egg/issue-2222",
                base_branch="main",
                ahead_count=65,
                offenders=[("abc1234", "Merged change")],
            )

    def test_truncates_offender_render_above_ten(self):
        """Body shows first 10 offenders + ``... and N more``."""
        pipeline = _make_pipeline()
        offenders = [(f"sha{i:04d}", f"Merged change {i}") for i in range(15)]
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


# ---------------------------------------------------------------------------
# Polling-thread tick — dedupe / reset, independent of classification.
# ---------------------------------------------------------------------------


class TestBranchDivergenceTick:
    """Integration tests for the polling-thread tick helper."""

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
                return_value=(30, [("abc1234", "Merged a"), ("def5678", "Merged b")]),
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
                return_value=(30, [("abc1234", "Merged a")]),
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
                return_value=(31, [("abc1234", "Merged a"), ("new9999", "Merged b")]),
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
        """Empty offenders this tick → dedupe set cleared so a re-introduced SHA
        fires again on a later tick."""
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
        """Tick 1: offenders fire.  Tick 2: cleared (reset).  Tick 3: same SHA
        reappears → publishes again (over-alert posture)."""
        pipeline = _make_pipeline()
        store = self._make_store(pipeline)
        alerted: set[str] = set()

        with (
            patch(
                "routes.pipelines._check_branch_divergence_for_alert",
                side_effect=[
                    (30, [("abc1234", "Merged a")]),
                    (0, []),
                    (30, [("abc1234", "Merged a")]),
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
            patch("routes.pipelines._check_branch_divergence_for_alert") as mock_check,
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

        _branch_divergence_tick(
            pipeline_id="issue-2222",
            worktree_repo_path=Path("/tmp/repo"),
            store=store,
            alerted_shas=alerted,
        )
        assert alerted == set()

    def test_re_loads_pipeline_each_tick(self):
        """Branch updates mid-pipeline are picked up because the helper re-loads
        on every tick rather than capturing once."""
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
        branches = [call.kwargs["pipeline_branch"] for call in mock_check.call_args_list]
        assert branches == ["egg/issue-2222", "egg/renamed"]

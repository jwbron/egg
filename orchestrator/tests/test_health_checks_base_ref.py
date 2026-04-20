"""Tests for parameterized base-ref resolution in health-check modules.

Covers:
- PipelineHealthContext._resolve_base_ref (context.py)
- PipelineHealthContext.git_diff_stat (uses _resolve_base_ref)
- PhaseOutputPresenceCheck._resolve_base_ref (tier1/phase_output.py)
- PhaseOutputPresenceCheck._branch_has_new_commits (uses _resolve_base_ref)
- The user-visible reasoning string in PhaseOutputPresenceCheck
"""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from health_checks.context import PipelineHealthContext
from health_checks.tier1.phase_output import PhaseOutputPresenceCheck
from health_checks.types import HealthStatus
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)


def _make_pipeline(base_branch: str | None = None) -> Pipeline:
    return Pipeline(
        id="issue-99",
        issue_number=99,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        base_branch=base_branch,
    )


def _make_context(pipeline: Pipeline) -> PipelineHealthContext:
    return PipelineHealthContext(
        pipeline=pipeline,
        repo_path=Path("/tmp/test-repo"),
        trigger="on_demand",
    )


# ===========================================================================
# PipelineHealthContext._resolve_base_ref
# ===========================================================================


class TestPipelineHealthContextBaseRef:
    def test_base_branch_develop_returns_origin_develop(self):
        """pipeline.base_branch='develop' -> 'origin/develop', no subprocess probe."""
        pipeline = _make_pipeline(base_branch="develop")
        ctx = _make_context(pipeline)
        with patch.object(ctx, "_run_git") as mock_run:
            result = ctx._resolve_base_ref()
            assert result == "origin/develop"
            mock_run.assert_not_called()

    def test_base_branch_release_branch(self):
        """Release-style branch names pass through unchanged."""
        pipeline = _make_pipeline(base_branch="release-2026-04")
        ctx = _make_context(pipeline)
        with patch.object(ctx, "_run_git") as mock_run:
            result = ctx._resolve_base_ref()
            assert result == "origin/release-2026-04"
            mock_run.assert_not_called()

    def test_base_branch_none_probes_origin_head(self):
        """base_branch=None -> probe origin/HEAD; probe returns 'origin/develop'."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)
        with patch.object(ctx, "_run_git", return_value="origin/develop") as mock_run:
            result = ctx._resolve_base_ref()
            assert result == "origin/develop"
            mock_run.assert_called_once_with("symbolic-ref", "refs/remotes/origin/HEAD", "--short")

    def test_base_branch_none_empty_probe_falls_back_to_main(self):
        """base_branch=None, probe returns '' -> final fallback 'origin/main'."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)
        with patch.object(ctx, "_run_git", return_value="") as mock_run:
            result = ctx._resolve_base_ref()
            assert result == "origin/main"
            mock_run.assert_called_once()

    def test_base_branch_empty_string_falls_through_to_probe(self):
        """Empty string base_branch is treated as unset (falls through to probe)."""
        pipeline = _make_pipeline(base_branch="")
        ctx = _make_context(pipeline)
        with patch.object(ctx, "_run_git", return_value="origin/main") as mock_run:
            result = ctx._resolve_base_ref()
            assert result == "origin/main"
            mock_run.assert_called_once()

    def test_base_branch_whitespace_falls_through_to_probe(self):
        """Whitespace-only base_branch is treated as unset (falls through to probe)."""
        pipeline = _make_pipeline(base_branch="   ")
        ctx = _make_context(pipeline)
        with patch.object(ctx, "_run_git", return_value="origin/develop") as mock_run:
            result = ctx._resolve_base_ref()
            assert result == "origin/develop"
            mock_run.assert_called_once()


# ===========================================================================
# PipelineHealthContext.git_diff_stat (uses _resolve_base_ref)
# ===========================================================================


class TestGitDiffStatUsesBaseRef:
    def test_git_diff_stat_uses_configured_base_branch(self):
        """git_diff_stat should diff against origin/<base_branch> when set."""
        pipeline = _make_pipeline(base_branch="develop")
        ctx = _make_context(pipeline)
        with patch.object(ctx, "_run_git", return_value="1 file changed\n") as mock_run:
            _ = ctx.git_diff_stat
            # Collect all positional-arg tuples across calls
            call_args_list = [call.args for call in mock_run.call_args_list]
            assert ("diff", "--stat", "origin/develop...HEAD") in call_args_list

    def test_git_diff_stat_uses_probed_base_ref(self):
        """base_branch=None -> probe origin/HEAD, use that ref in diff command."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)

        def fake_run_git(*args: str) -> str:
            # First call: symbolic-ref probe; subsequent: diff
            if args and args[0] == "symbolic-ref":
                return "origin/main"
            return "stat-output\n"

        with patch.object(ctx, "_run_git", side_effect=fake_run_git) as mock_run:
            _ = ctx.git_diff_stat
            call_args_list = [call.args for call in mock_run.call_args_list]
            assert ("diff", "--stat", "origin/main...HEAD") in call_args_list
            assert (
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "--short",
            ) in call_args_list


# ===========================================================================
# PhaseOutputPresenceCheck._resolve_base_ref
# ===========================================================================


class TestPhaseOutputPresenceCheckBaseRef:
    def test_base_branch_develop(self):
        """pipeline.base_branch='develop' -> 'origin/develop' with no subprocess."""
        pipeline = _make_pipeline(base_branch="develop")
        ctx = _make_context(pipeline)
        with patch("health_checks.tier1.phase_output.subprocess.run") as mock_run:
            result = PhaseOutputPresenceCheck._resolve_base_ref(ctx, git_dir=Path("/tmp/repo"))
            assert result == "origin/develop"
            mock_run.assert_not_called()

    def test_base_branch_none_probe_success(self):
        """base_branch=None -> probe origin/HEAD via subprocess.run, returns its output."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)
        with patch("health_checks.tier1.phase_output.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="origin/develop\n", returncode=0)
            result = PhaseOutputPresenceCheck._resolve_base_ref(ctx, git_dir=Path("/tmp/repo"))
            assert result == "origin/develop"
            mock_run.assert_called_once()

    def test_base_branch_none_probe_failure_returncode(self):
        """Probe returncode != 0 -> fall back to origin/main."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)
        with patch("health_checks.tier1.phase_output.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            result = PhaseOutputPresenceCheck._resolve_base_ref(ctx, git_dir=Path("/tmp/repo"))
            assert result == "origin/main"

    def test_base_branch_none_no_git_dir_skips_probe(self):
        """Without git_dir, the probe is skipped and we go straight to origin/main."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)
        with patch("health_checks.tier1.phase_output.subprocess.run") as mock_run:
            result = PhaseOutputPresenceCheck._resolve_base_ref(ctx, git_dir=None)
            assert result == "origin/main"
            mock_run.assert_not_called()

    def test_base_branch_none_probe_exception(self):
        """subprocess.run raising is swallowed and falls back to origin/main."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)
        with patch(
            "health_checks.tier1.phase_output.subprocess.run",
            side_effect=OSError("git not found"),
        ):
            result = PhaseOutputPresenceCheck._resolve_base_ref(ctx, git_dir=Path("/tmp/repo"))
            assert result == "origin/main"


# ===========================================================================
# PhaseOutputPresenceCheck._branch_has_new_commits
# ===========================================================================


class TestBranchHasNewCommitsUsesBaseRef:
    def test_new_commits_with_configured_base_branch(self):
        """With base_branch set, rev-list should use origin/<base_branch>..HEAD."""
        pipeline = _make_pipeline(base_branch="develop")
        ctx = _make_context(pipeline)
        # With base_branch set, _resolve_base_ref never calls subprocess,
        # so only the rev-list call hits subprocess.run.
        with patch("health_checks.tier1.phase_output.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="3\n", returncode=0)
            result = PhaseOutputPresenceCheck._branch_has_new_commits(ctx)
            assert result is True
            # The one invocation must be the rev-list count with origin/develop..HEAD
            mock_run.assert_called_once()
            call_args = mock_run.call_args.args[0]
            assert call_args == [
                "git",
                "rev-list",
                "--count",
                "origin/develop..HEAD",
            ]

    def test_no_new_commits_with_probed_base_ref(self):
        """base_branch=None, probe returns origin/main, rev-list returns 0 -> False."""
        pipeline = _make_pipeline(base_branch=None)
        ctx = _make_context(pipeline)

        call_log: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            call_log.append(cmd)
            if "symbolic-ref" in cmd:
                return MagicMock(stdout="origin/main\n", returncode=0)
            # rev-list
            return MagicMock(stdout="0\n", returncode=0)

        with patch("health_checks.tier1.phase_output.subprocess.run", side_effect=fake_run):
            result = PhaseOutputPresenceCheck._branch_has_new_commits(ctx)
            assert result is False
            # Verify the rev-list invocation used origin/main..HEAD
            rev_list_calls = [c for c in call_log if "rev-list" in c]
            assert rev_list_calls == [["git", "rev-list", "--count", "origin/main..HEAD"]]

    def test_zero_commits_returns_false(self):
        """rev-list count '0' -> no new commits."""
        pipeline = _make_pipeline(base_branch="develop")
        ctx = _make_context(pipeline)
        with patch("health_checks.tier1.phase_output.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="0\n", returncode=0)
            result = PhaseOutputPresenceCheck._branch_has_new_commits(ctx)
            assert result is False

    def test_subprocess_exception_returns_false(self):
        """Any exception in subprocess.run -> returns False."""
        pipeline = _make_pipeline(base_branch="develop")
        ctx = _make_context(pipeline)
        with patch(
            "health_checks.tier1.phase_output.subprocess.run",
            side_effect=RuntimeError("boom"),
        ):
            result = PhaseOutputPresenceCheck._branch_has_new_commits(ctx)
            assert result is False


# ===========================================================================
# PhaseOutputPresenceCheck reasoning string mentions the right base ref
# ===========================================================================


class TestPhaseOutputReasoningMessageMentionsBaseRef:
    def test_reasoning_mentions_configured_base_branch(self):
        """HEALTHY 'new commits beyond origin/<base>' message uses configured ref."""
        pipeline = _make_pipeline(base_branch="develop")

        # Build a phase execution where an agent COMPLETEd but reported no commit.
        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.now(UTC)
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="c1",
                started_at=datetime.now(UTC),
                commit=None,
            )
        )

        ctx = _make_context(pipeline)

        # The check path: _check_implement_outputs -> _branch_has_new_commits
        # (subprocess rev-list) -> _resolve_base_ref (no subprocess since
        # base_branch is set). Simulate "branch has new commits".
        with patch("health_checks.tier1.phase_output.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="2\n", returncode=0)
            result = PhaseOutputPresenceCheck().run(ctx)

        assert result.status == HealthStatus.HEALTHY
        assert result.reasoning == "Branch has new commits beyond origin/develop."

    def test_reasoning_falls_back_to_origin_main_when_base_branch_unset(self):
        """When base_branch=None, the display-side _resolve_base_ref is invoked
        without a git_dir (see phase_output.py line ~121), so the probe is
        skipped and the reasoning string uses the 'origin/main' fallback even
        if a rev-list probe would have succeeded against a different ref.
        """
        pipeline = _make_pipeline(base_branch=None)

        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.now(UTC)
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="c1",
                started_at=datetime.now(UTC),
                commit=None,
            )
        )

        ctx = _make_context(pipeline)

        def fake_run(cmd, **kwargs):
            if "symbolic-ref" in cmd:
                return MagicMock(stdout="origin/develop\n", returncode=0)
            # rev-list call -> has commits
            return MagicMock(stdout="5\n", returncode=0)

        with patch("health_checks.tier1.phase_output.subprocess.run", side_effect=fake_run):
            result = PhaseOutputPresenceCheck().run(ctx)

        assert result.status == HealthStatus.HEALTHY
        # Display-side _resolve_base_ref is called without git_dir; probe is
        # skipped; fallback is origin/main regardless of what rev-list saw.
        assert result.reasoning == "Branch has new commits beyond origin/main."

    def test_reasoning_default_main_when_no_base_branch_and_no_probe(self):
        """base_branch=None + probe fails -> reasoning mentions origin/main."""
        pipeline = _make_pipeline(base_branch=None)

        phase_exec = pipeline.get_phase_execution(PipelinePhase.IMPLEMENT)
        phase_exec.status = PipelineStatus.RUNNING
        phase_exec.started_at = datetime.now(UTC)
        phase_exec.agents.append(
            AgentExecution(
                role=AgentRole.CODER,
                status=AgentExecutionStatus.COMPLETE,
                container_id="c1",
                started_at=datetime.now(UTC),
                commit=None,
            )
        )

        ctx = _make_context(pipeline)

        def fake_run(cmd, **kwargs):
            if "symbolic-ref" in cmd:
                # Probe failure
                return MagicMock(stdout="", returncode=1)
            # rev-list call -> has commits
            return MagicMock(stdout="1\n", returncode=0)

        with patch("health_checks.tier1.phase_output.subprocess.run", side_effect=fake_run):
            result = PhaseOutputPresenceCheck().run(ctx)

        assert result.status == HealthStatus.HEALTHY
        assert result.reasoning == "Branch has new commits beyond origin/main."

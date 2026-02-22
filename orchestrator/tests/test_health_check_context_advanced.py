"""Advanced tests for PipelineHealthContext lazy properties and edge cases."""

import sys
import tempfile
from datetime import datetime
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
from models import Pipeline, PipelinePhase, PipelineStatus


def _make_pipeline(
    status: PipelineStatus = PipelineStatus.RUNNING,
    phase: PipelinePhase = PipelinePhase.IMPLEMENT,
    repo: str = "owner/repo",
) -> Pipeline:
    return Pipeline(
        id="issue-99",
        issue_number=99,
        repo=repo,
        branch="egg/issue-99",
        mode="issue",
        status=status,
        current_phase=phase,
    )


# ===========================================================================
# Tests: Constructor and cheap accessors
# ===========================================================================


class TestContextConstructor:
    def test_defaults(self):
        """Phase defaults to pipeline.current_phase when not provided."""
        pipeline = _make_pipeline(phase=PipelinePhase.PLAN)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        assert ctx.current_phase == PipelinePhase.PLAN
        assert ctx.wave_number is None
        assert ctx.docker_client is None
        assert ctx.state_store is None
        assert isinstance(ctx.timestamp, datetime)

    def test_explicit_phase_override(self):
        """Explicit phase overrides pipeline.current_phase."""
        pipeline = _make_pipeline(phase=PipelinePhase.IMPLEMENT)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
            phase=PipelinePhase.PLAN,
        )
        assert ctx.current_phase == PipelinePhase.PLAN

    def test_wave_number_stored(self):
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="wave_complete",
            wave_number=3,
        )
        assert ctx.wave_number == 3

    def test_state_store_stored(self):
        mock_store = MagicMock()
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
            state_store=mock_store,
        )
        assert ctx.state_store is mock_store

    def test_pipeline_id_accessor(self):
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        assert ctx.pipeline_id == "issue-99"

    def test_branch_accessor(self):
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        assert ctx.branch == "egg/issue-99"


# ===========================================================================
# Tests: git_log lazy property
# ===========================================================================


class TestContextGitLog:
    def test_git_log_caching(self):
        """git_log should call subprocess only once, then return cached value."""
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        assert ctx._git_log is None

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123 first commit\n", returncode=0)
            first = ctx.git_log
            second = ctx.git_log
            assert first == second
            assert "abc123" in first
            mock_run.assert_called_once()

    def test_git_log_subprocess_error(self):
        """Subprocess failure returns empty string."""
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = ctx.git_log
            assert result == ""

    def test_git_log_timeout(self):
        """Subprocess timeout returns empty string."""
        import subprocess as sp

        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        with patch("subprocess.run", side_effect=sp.TimeoutExpired("git", 10)):
            result = ctx.git_log
            assert result == ""

    def test_git_log_repo_subdir_resolution(self):
        """When pipeline.repo is set, git_dir should resolve to repo_path/name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create the repo subdirectory
            repo_dir = Path(tmpdir) / "repo"
            repo_dir.mkdir()

            pipeline = _make_pipeline(repo="owner/repo")
            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="log output\n", returncode=0)
                _ = ctx.git_log
                # Verify git was run in the repo subdirectory
                call_kwargs = mock_run.call_args
                assert str(repo_dir) == call_kwargs.kwargs.get("cwd", call_kwargs[1].get("cwd"))


# ===========================================================================
# Tests: git_diff_stat lazy property
# ===========================================================================


class TestContextGitDiffStat:
    def test_git_diff_stat_caching(self):
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        assert ctx._git_diff_stat is None
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="5 files changed\n", returncode=0)
            first = ctx.git_diff_stat
            second = ctx.git_diff_stat
            assert first == second
            mock_run.assert_called_once()

    def test_git_diff_stat_error(self):
        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
        )
        with patch("subprocess.run", side_effect=RuntimeError("fail")):
            result = ctx.git_diff_stat
            assert result == ""


# ===========================================================================
# Tests: agent_outputs lazy property
# ===========================================================================


class TestContextAgentOutputs:
    def test_agent_outputs_reads_files(self):
        """Should read .json/.md/.yaml/.yml from drafts/ and contracts/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / ".egg-state"
            drafts = state_dir / "drafts"
            contracts = state_dir / "contracts"
            drafts.mkdir(parents=True)
            contracts.mkdir(parents=True)

            (drafts / "plan.md").write_text("# Plan content")
            (contracts / "850.json").write_text('{"tasks": []}')
            (drafts / "notes.yaml").write_text("key: value")
            # Non-matching extension should be ignored
            (drafts / "binary.bin").write_text("should be ignored")

            pipeline = Pipeline(
                id="issue-99",
                issue_number=99,
                repo="",
                branch="egg/issue-99",
                mode="issue",
                status=PipelineStatus.RUNNING,
                current_phase=PipelinePhase.IMPLEMENT,
            )
            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            outputs = ctx.agent_outputs
            assert "plan.md" in outputs
            assert "850.json" in outputs
            assert "notes.yaml" in outputs
            assert "binary.bin" not in outputs

    def test_agent_outputs_caching(self):
        """agent_outputs should only scan filesystem once."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = Pipeline(
                id="issue-99",
                issue_number=99,
                repo="",
                branch="egg/issue-99",
                mode="issue",
                status=PipelineStatus.RUNNING,
                current_phase=PipelinePhase.IMPLEMENT,
            )
            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            assert ctx._agent_outputs is None
            first = ctx.agent_outputs
            second = ctx.agent_outputs
            assert first is second  # Same cached object

    def test_agent_outputs_no_state_dir(self):
        """Missing .egg-state returns empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = Pipeline(
                id="issue-99",
                issue_number=99,
                repo="",
                branch="egg/issue-99",
                mode="issue",
                status=PipelineStatus.RUNNING,
                current_phase=PipelinePhase.IMPLEMENT,
            )
            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            assert ctx.agent_outputs == {}

    def test_agent_outputs_repo_subdir(self):
        """State dir resolution with pipeline.repo set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / "repo" / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "output.json").write_text('{"key": "value"}')

            pipeline = _make_pipeline(repo="owner/repo")
            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            outputs = ctx.agent_outputs
            assert "output.json" in outputs

    def test_agent_outputs_truncation(self):
        """Content longer than 4000 chars should be truncated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / ".egg-state" / "drafts"
            state_dir.mkdir(parents=True)
            (state_dir / "big.md").write_text("x" * 10000)

            pipeline = Pipeline(
                id="issue-99",
                issue_number=99,
                repo="",
                branch="egg/issue-99",
                mode="issue",
                status=PipelineStatus.RUNNING,
                current_phase=PipelinePhase.IMPLEMENT,
            )
            ctx = PipelineHealthContext(
                pipeline=pipeline,
                repo_path=Path(tmpdir),
                trigger="on_demand",
            )
            content = ctx.agent_outputs["big.md"]
            assert len(content) == 4000


# ===========================================================================
# Tests: live_container_ids lazy property
# ===========================================================================


class TestContextLiveContainerIds:
    def test_caching(self):
        """live_container_ids should query Docker only once."""
        mock_docker = MagicMock()
        mock_c = MagicMock()
        mock_c.container_id = "abc"
        mock_docker.list_containers.return_value = [mock_c]

        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
            docker_client=mock_docker,
        )
        assert ctx._live_container_ids is None
        first = ctx.live_container_ids
        second = ctx.live_container_ids
        assert first == second == {"abc"}
        mock_docker.list_containers.assert_called_once()

    def test_docker_exception_returns_empty(self):
        """Docker client exceptions should return empty set."""
        mock_docker = MagicMock()
        mock_docker.list_containers.side_effect = RuntimeError("Docker unavailable")

        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
            docker_client=mock_docker,
        )
        assert ctx.live_container_ids == set()

    def test_multiple_containers(self):
        """Should return all container IDs."""
        mock_docker = MagicMock()
        containers = []
        for cid in ["a1", "b2", "c3"]:
            c = MagicMock()
            c.container_id = cid
            containers.append(c)
        mock_docker.list_containers.return_value = containers

        pipeline = _make_pipeline()
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=Path("/tmp/test"),
            trigger="on_demand",
            docker_client=mock_docker,
        )
        assert ctx.live_container_ids == {"a1", "b2", "c3"}

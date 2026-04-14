"""
Tests for ``_cleanup_agent_outputs_for_pr``.

This helper runs at PR-phase entry to drop the ephemeral coder→tester
handoff artifacts in ``.egg-state/agent-outputs/`` before the PR is
created.  See jwbron/egg#1731.
"""

import subprocess
import sys
from unittest.mock import MagicMock, patch

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)


def _run_result(returncode=0, stdout="", stderr=""):
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestCleanupAgentOutputsForPr:
    def test_noop_when_agent_outputs_missing(self, tmp_path):
        """When .egg-state/agent-outputs doesn't exist, still runs git rm
        --ignore-unmatch (cheap) and exits cleanly without committing."""
        from routes.pipelines import _cleanup_agent_outputs_for_pr

        # No directory created — the helper should still run git rm
        # --ignore-unmatch (which no-ops) and skip the commit via the
        # diff --cached --quiet check.
        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # git rm -rf --ignore-unmatch
                _run_result(returncode=0),  # diff --cached --quiet → empty index
            ],
        ) as mock_run:
            _cleanup_agent_outputs_for_pr(tmp_path, "issue-42")

        # No commit attempted
        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any("commit" in c for c in all_cmds)

    def test_removes_and_commits_when_tracked(self, tmp_path):
        """When agent-outputs files are tracked, git rm stages removal and we commit."""
        from routes.pipelines import _cleanup_agent_outputs_for_pr

        (tmp_path / ".egg-state" / "agent-outputs").mkdir(parents=True)
        (tmp_path / ".egg-state" / "agent-outputs" / "coder-test-changes.patch").write_text(
            "diff --git a/... b/...\n"
        )

        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # git rm -rf
                _run_result(returncode=1),  # diff --cached --quiet → staged changes present
                _run_result(),  # commit
            ],
        ) as mock_run:
            _cleanup_agent_outputs_for_pr(tmp_path, "issue-42")

        all_cmds = [c.args[0] for c in mock_run.call_args_list]
        # git rm was called with --ignore-unmatch and targeted at agent-outputs
        rm_cmd = all_cmds[0]
        assert "rm" in rm_cmd
        assert "--ignore-unmatch" in rm_cmd
        assert ".egg-state/agent-outputs" in rm_cmd
        # Commit was called with the canonical message
        commit_cmd = all_cmds[-1]
        assert "commit" in commit_cmd
        assert any(
            "Remove ephemeral agent-output handoff artifacts" in str(arg) for arg in commit_cmd
        )

    def test_swallows_rm_failure(self, tmp_path):
        """A failing git rm is logged and swallowed — cleanup is best-effort."""
        from routes.pipelines import _cleanup_agent_outputs_for_pr

        (tmp_path / ".egg-state" / "agent-outputs").mkdir(parents=True)

        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                returncode=1, cmd="git rm", stderr="fatal: unable to remove"
            ),
        ) as mock_run:
            # Must not raise.
            _cleanup_agent_outputs_for_pr(tmp_path, "issue-42")

        # Only the rm attempt — no diff check, no commit.
        assert mock_run.call_count == 1

    def test_swallows_rm_timeout(self, tmp_path):
        """A timed-out git rm is logged and swallowed."""
        from routes.pipelines import _cleanup_agent_outputs_for_pr

        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git rm", timeout=30),
        ):
            _cleanup_agent_outputs_for_pr(tmp_path, "issue-42")

    def test_swallows_commit_failure(self, tmp_path):
        """A failing commit is logged and swallowed; helper returns normally."""
        from routes.pipelines import _cleanup_agent_outputs_for_pr

        (tmp_path / ".egg-state" / "agent-outputs").mkdir(parents=True)

        with patch(
            "routes.pipelines.subprocess.run",
            side_effect=[
                _run_result(),  # rm
                _run_result(returncode=1),  # diff: has staged
                subprocess.CalledProcessError(
                    returncode=1, cmd="git commit", stderr="nothing to commit"
                ),
            ],
        ):
            # Must not raise.
            _cleanup_agent_outputs_for_pr(tmp_path, "issue-42")

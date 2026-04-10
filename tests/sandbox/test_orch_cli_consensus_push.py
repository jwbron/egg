"""Tests for consensus propose --push passing EGG_CONSENSUS_PUSH=1 (#1669).

When `egg-orch consensus propose --push` is used, the orch_cli should pass
`EGG_CONSENSUS_PUSH=1` in the subprocess environment when executing `git push`.
This marker flows through the git wrapper to the gateway, signaling that the
push is part of the consensus protocol and should not be blocked in concurrent
mode.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.orch_cli import cmd_consensus_propose


def _make_args(
    push: bool = False,
    summary: str = "test proposal",
    artifacts: list[str] | None = None,
    commit_sha: str = "abc123",
    risk: str = "",
    file: str | None = None,
    changed_artifacts: list[str] | None = None,
    json_output: bool = False,
) -> argparse.Namespace:
    """Create argparse.Namespace for cmd_consensus_propose."""
    return argparse.Namespace(
        pipeline_id=None,  # Will use env var
        role=None,  # Will use env var
        push=push,
        summary=summary,
        artifacts=artifacts or [],
        commit_sha=commit_sha,
        risk=risk,
        file=file,
        changed_artifacts=changed_artifacts,
        json=json_output,
    )


@pytest.fixture
def env_vars():
    """Set up required environment variables for orch_cli."""
    with patch.dict(
        os.environ,
        {
            "EGG_PIPELINE_ID": "issue-1669",
            "EGG_AGENT_ROLE": "coder",
            "EGG_REPO_PATH": "/home/egg/repos/test-repo",
            "EGG_ORCHESTRATOR_URL": "http://localhost:9849",
        },
    ):
        yield


class TestConsensusProposeWithPush:
    """Verify that --push flag passes EGG_CONSENSUS_PUSH=1 to git subprocess."""

    def test_push_passes_consensus_env_var(self, env_vars):
        """cmd_consensus_propose with --push should set EGG_CONSENSUS_PUSH=1 in env."""
        args = _make_args(push=True)

        captured_env = {}

        def mock_check_output(cmd, text=False, cwd=None, stderr=None, env=None):
            if cmd == ["git", "push"]:
                # Capture the environment passed to git push
                captured_env.update(env or {})
                return "Everything up-to-date"
            elif cmd == ["git", "rev-parse", "HEAD"]:
                return "abc123\n"
            return ""

        mock_response = {"success": True, "data": {"consensus": {"agents": {}}}}

        with (
            patch("subprocess.check_output", side_effect=mock_check_output),
            patch("egg_lib.orch_cli.orch_request", return_value=mock_response),
        ):
            result = cmd_consensus_propose(args)
            assert result == 0
            assert captured_env.get("EGG_CONSENSUS_PUSH") == "1", (
                "Expected EGG_CONSENSUS_PUSH=1 in git push subprocess environment"
            )

    def test_push_without_flag_no_consensus_env(self, env_vars):
        """cmd_consensus_propose without --push should NOT call git push at all."""
        args = _make_args(push=False)

        mock_response = {"success": True, "data": {"consensus": {"agents": {}}}}

        with (
            patch("subprocess.check_output") as mock_co,
            patch("egg_lib.orch_cli.orch_request", return_value=mock_response),
        ):
            result = cmd_consensus_propose(args)
            assert result == 0
            # Should not have called git push
            for call in mock_co.call_args_list:
                cmd = call[0][0] if call[0] else call[1].get("args", [])
                assert cmd != ["git", "push"], (
                    "git push should not be called when --push is not set"
                )

    def test_push_failure_returns_error(self, env_vars):
        """If git push fails, cmd_consensus_propose should return 1."""
        args = _make_args(push=True)

        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(
                1, "git push", output="error: remote hung up"
            ),
        ):
            result = cmd_consensus_propose(args)
            assert result == 1

    def test_push_env_includes_parent_env(self, env_vars):
        """The env passed to git push should include the parent environment plus the marker."""
        args = _make_args(push=True)

        captured_env = {}

        def mock_check_output(cmd, text=False, cwd=None, stderr=None, env=None):
            if cmd == ["git", "push"]:
                captured_env.update(env or {})
                return "Everything up-to-date"
            elif cmd == ["git", "rev-parse", "HEAD"]:
                return "abc123\n"
            return ""

        mock_response = {"success": True, "data": {"consensus": {"agents": {}}}}

        with (
            patch("subprocess.check_output", side_effect=mock_check_output),
            patch("egg_lib.orch_cli.orch_request", return_value=mock_response),
        ):
            result = cmd_consensus_propose(args)
            assert result == 0
            # Should contain the parent environment vars plus the marker
            assert captured_env.get("EGG_CONSENSUS_PUSH") == "1"
            # Should also contain the parent environment (at least some of it)
            assert captured_env.get("EGG_PIPELINE_ID") == "issue-1669"

    def test_push_uses_correct_cwd(self, env_vars):
        """The git push should use EGG_REPO_PATH as cwd."""
        args = _make_args(push=True)

        captured_cwd = None

        def mock_check_output(cmd, text=False, cwd=None, stderr=None, env=None):
            nonlocal captured_cwd
            if cmd == ["git", "push"]:
                captured_cwd = cwd
                return "Everything up-to-date"
            elif cmd == ["git", "rev-parse", "HEAD"]:
                return "abc123\n"
            return ""

        mock_response = {"success": True, "data": {"consensus": {"agents": {}}}}

        with (
            patch("subprocess.check_output", side_effect=mock_check_output),
            patch("egg_lib.orch_cli.orch_request", return_value=mock_response),
        ):
            result = cmd_consensus_propose(args)
            assert result == 0
            assert captured_cwd == "/home/egg/repos/test-repo"

    def test_git_not_found_returns_error(self, env_vars):
        """If git binary is not found, cmd_consensus_propose should return 1."""
        args = _make_args(push=True)

        with patch("subprocess.check_output", side_effect=FileNotFoundError):
            result = cmd_consensus_propose(args)
            assert result == 1

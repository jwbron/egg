"""Tests for _consensus_push() and cmd_consensus_propose --push (#1669).

When `egg-orch consensus propose --push` is used, the orch_cli calls
_consensus_push() which either:
  1. Calls the gateway API directly with consensus_push=true in the payload
     (when GATEWAY_URL is set), or
  2. Falls back to `git push` with EGG_CONSENSUS_PUSH=1 in the subprocess
     environment (when GATEWAY_URL is not set, e.g. local development).

This ensures the push is marked as originating from the consensus protocol
so the gateway allows it in concurrent mode.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.orch_cli import _consensus_push, cmd_consensus_propose


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
def base_env():
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


@pytest.fixture
def gateway_env(base_env):
    """Set up environment with GATEWAY_URL for direct API push path."""
    with patch.dict(
        os.environ,
        {
            "GATEWAY_URL": "http://egg-gateway:9848",
            "EGG_SESSION_TOKEN": "test-session-token",
            "CONTAINER_ID": "test-container-123",
        },
    ):
        yield


@pytest.fixture
def no_gateway_env(base_env):
    """Set up environment without GATEWAY_URL for fallback git push path."""
    env_overrides = {"GATEWAY_URL": ""}
    with patch.dict(os.environ, env_overrides):
        os.environ.pop("GATEWAY_URL", None)
        yield


# ---------------------------------------------------------------------------
# _consensus_push: Direct gateway API path (GATEWAY_URL set)
# ---------------------------------------------------------------------------


class TestConsensusPushDirectGatewayAPI:
    """Test _consensus_push() when GATEWAY_URL is set — calls gateway directly."""

    def test_sends_consensus_push_in_payload(self, gateway_env):
        """The gateway API request should include consensus_push=true in payload."""
        captured_request = {}

        def mock_urlopen(req, **kwargs):
            captured_request["url"] = req.full_url
            captured_request["data"] = json.loads(req.data)
            captured_request["headers"] = dict(req.headers)
            resp = MagicMock()
            resp.read.return_value = json.dumps({"success": True, "data": {}}).encode()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        def mock_check_output(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                return "egg/issue-1669-tester/work\n"
            if cmd == ["git", "config", "branch.egg/issue-1669-tester/work.merge"]:
                return "refs/heads/egg/issue-1669\n"
            return ""

        with (
            patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output),
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
        ):
            result = _consensus_push()
            assert result == 0
            assert captured_request["data"]["consensus_push"] is True
            assert captured_request["data"]["remote"] == "origin"
            assert captured_request["data"]["repo_path"] == "/home/egg/repos/test-repo"
            assert captured_request["data"]["container_id"] == "test-container-123"

    def test_resolves_tracking_branch_refspec(self, gateway_env):
        """Should resolve local:remote refspec from git tracking config."""
        captured_request = {}

        def mock_urlopen(req, **kwargs):
            captured_request["data"] = json.loads(req.data)
            resp = MagicMock()
            resp.read.return_value = json.dumps({"success": True, "data": {}}).encode()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        def mock_check_output(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                return "local-branch\n"
            if cmd == ["git", "config", "branch.local-branch.merge"]:
                return "refs/heads/remote-branch\n"
            return ""

        with (
            patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output),
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
        ):
            result = _consensus_push()
            assert result == 0
            assert captured_request["data"]["refspec"] == "local-branch:remote-branch"

    def test_uses_branch_name_when_no_tracking(self, gateway_env):
        """When no tracking branch, uses the local branch name as refspec."""
        captured_request = {}

        def mock_urlopen(req, **kwargs):
            captured_request["data"] = json.loads(req.data)
            resp = MagicMock()
            resp.read.return_value = json.dumps({"success": True, "data": {}}).encode()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        def mock_check_output(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                return "egg/my-feature\n"
            if "config" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return ""

        with (
            patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output),
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
        ):
            result = _consensus_push()
            assert result == 0
            assert captured_request["data"]["refspec"] == "egg/my-feature"

    def test_includes_auth_header(self, gateway_env):
        """The request should include Authorization: Bearer header."""
        captured_headers = {}

        def mock_urlopen(req, **kwargs):
            captured_headers.update(dict(req.headers))
            resp = MagicMock()
            resp.read.return_value = json.dumps({"success": True, "data": {}}).encode()
            resp.__enter__ = lambda s: resp
            resp.__exit__ = lambda s, *a: None
            return resp

        def mock_check_output(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                return "egg/feature\n"
            if "config" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return ""

        with (
            patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output),
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
        ):
            result = _consensus_push()
            assert result == 0
            assert captured_headers.get("Authorization") == "Bearer test-session-token"

    def test_http_error_returns_1(self, gateway_env):
        """HTTP errors from the gateway should return 1."""

        def mock_check_output(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                return "egg/feature\n"
            if "config" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return ""

        error_body = json.dumps(
            {"message": "Direct push blocked", "data": {"mode": "concurrent"}}
        ).encode()
        http_error = urllib.error.HTTPError(
            url="http://egg-gateway:9848/api/v1/git/push",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=io.BytesIO(error_body),
        )

        with (
            patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output),
            patch("urllib.request.urlopen", side_effect=http_error),
        ):
            result = _consensus_push()
            assert result == 1

    def test_url_error_returns_1(self, gateway_env):
        """URLError (gateway unreachable) should return 1."""

        def mock_check_output(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                return "egg/feature\n"
            if "config" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return ""

        with (
            patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output),
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("Connection refused"),
            ),
        ):
            result = _consensus_push()
            assert result == 1

    def test_no_branch_returns_1(self, gateway_env):
        """If git branch --show-current fails, should return 1."""

        def mock_check_output(cmd, **kwargs):
            if cmd == ["git", "branch", "--show-current"]:
                raise subprocess.CalledProcessError(1, cmd)
            return ""

        with patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output):
            result = _consensus_push()
            assert result == 1


# ---------------------------------------------------------------------------
# _consensus_push: Fallback git push path (no GATEWAY_URL)
# ---------------------------------------------------------------------------


class TestConsensusPushFallbackGitPush:
    """Test _consensus_push() when GATEWAY_URL is not set — falls back to git push."""

    def test_fallback_passes_consensus_env_var(self, no_gateway_env):
        """Fallback git push should set EGG_CONSENSUS_PUSH=1 in the subprocess env."""
        captured_env = {}

        def mock_check_output(cmd, text=False, cwd=None, stderr=None, env=None):
            if cmd == ["git", "push"]:
                captured_env.update(env or {})
                return "Everything up-to-date"
            return ""

        with patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output):
            result = _consensus_push()
            assert result == 0
            assert captured_env.get("EGG_CONSENSUS_PUSH") == "1"

    def test_fallback_includes_parent_env(self, no_gateway_env):
        """Fallback git push env should include parent env plus marker."""
        captured_env = {}

        def mock_check_output(cmd, text=False, cwd=None, stderr=None, env=None):
            if cmd == ["git", "push"]:
                captured_env.update(env or {})
                return "Everything up-to-date"
            return ""

        with patch("egg_lib.orch_cli.subprocess.check_output", side_effect=mock_check_output):
            result = _consensus_push()
            assert result == 0
            assert captured_env.get("EGG_CONSENSUS_PUSH") == "1"
            assert captured_env.get("EGG_PIPELINE_ID") == "issue-1669"

    def test_fallback_push_failure_returns_1(self, no_gateway_env):
        """Fallback git push CalledProcessError should return 1."""
        with patch(
            "egg_lib.orch_cli.subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, "git push", output="remote rejected"),
        ):
            result = _consensus_push()
            assert result == 1

    def test_fallback_git_not_found_returns_1(self, no_gateway_env):
        """Fallback when git binary not found should return 1."""
        with patch(
            "egg_lib.orch_cli.subprocess.check_output",
            side_effect=FileNotFoundError,
        ):
            result = _consensus_push()
            assert result == 1


# ---------------------------------------------------------------------------
# cmd_consensus_propose integration with _consensus_push
# ---------------------------------------------------------------------------


class TestConsensusProposeWithPush:
    """Verify cmd_consensus_propose calls _consensus_push when --push is set."""

    def test_push_flag_calls_consensus_push(self, base_env):
        """cmd_consensus_propose with --push calls _consensus_push."""
        args = _make_args(push=True)

        mock_response = {"success": True, "data": {"consensus": {"agents": {}}}}

        with (
            patch("egg_lib.orch_cli._consensus_push", return_value=0) as mock_push,
            patch("egg_lib.orch_cli.orch_request", return_value=mock_response),
        ):
            result = cmd_consensus_propose(args)
            assert result == 0
            mock_push.assert_called_once()

    def test_no_push_flag_skips_consensus_push(self, base_env):
        """cmd_consensus_propose without --push does NOT call _consensus_push."""
        args = _make_args(push=False)

        mock_response = {"success": True, "data": {"consensus": {"agents": {}}}}

        with (
            patch("egg_lib.orch_cli._consensus_push", return_value=0) as mock_push,
            patch("egg_lib.orch_cli.orch_request", return_value=mock_response),
        ):
            result = cmd_consensus_propose(args)
            assert result == 0
            mock_push.assert_not_called()

    def test_push_failure_aborts_propose(self, base_env):
        """If _consensus_push returns 1, cmd_consensus_propose should return 1."""
        args = _make_args(push=True)

        with (
            patch("egg_lib.orch_cli._consensus_push", return_value=1),
            patch("egg_lib.orch_cli.orch_request") as mock_request,
        ):
            result = cmd_consensus_propose(args)
            assert result == 1
            # proposal should NOT have been sent
            mock_request.assert_not_called()

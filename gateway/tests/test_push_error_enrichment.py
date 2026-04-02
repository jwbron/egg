"""Tests for enriched push rejection errors (#1527).

When the gateway blocks a push due to agent-role file restrictions,
the error response should include:
- blocked_files listing which files violated the policy
- allowed_patterns listing what the agent IS allowed to write
- remediation steps guiding the agent to recover

This complements test_agent_restrictions_enforce.py which already covers
the allow/block decision — here we focus on the error *content*.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import session_manager
from phase_filter import FileRestrictionResult
from policy import PolicyResult
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_session(role: str = "coder"):
    """Create a mock session with the given agent role."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = None
    return mock_session


def _push_context(mock_session, blocked_files: list[str]):
    """Return context managers for a push that violates agent restrictions."""
    import auth

    mock_result = SessionValidationResult(valid=True, session=mock_session)
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode",
        visibility="public",
    )

    auth._session_manager = None
    auth._rate_limiter = None
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_sm = sys.modules.get("session_manager", session_manager)

    def run_side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "remote" in cmd and "get-url" in cmd:
            result.stdout = "https://github.com/owner/repo.git\n"
        elif "branch" in cmd and "--show-current" in cmd:
            result.stdout = "egg-feature\n"
        elif "push" in cmd:
            result.stdout = "Everything up-to-date\n"
        else:
            result.stdout = ""
        return result

    agent_result = FileRestrictionResult.block(
        message=f"agent role '{mock_session.agent_role}' cannot modify: {', '.join(blocked_files)}",
        role=mock_session.agent_role,
        blocked_files=blocked_files,
        blocked_reason="Files belong to another agent role",
    )

    return (
        patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
        patch("subprocess.run", side_effect=run_side_effect),
        patch.object(
            gateway,
            "get_policy_engine",
            return_value=MagicMock(
                check_branch_ownership=MagicMock(
                    return_value=PolicyResult(
                        allowed=True,
                        reason="OK",
                        details={"branch": "egg-feature"},
                    )
                ),
            ),
        ),
        patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
        patch.object(gateway, "get_changed_files_in_push", return_value=(blocked_files, None)),
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        patch.object(gateway, "check_agent_restrictions", return_value=agent_result),
    )


def _do_push(client):
    """Send a push request."""
    return client.post(
        "/api/v1/git/push",
        headers={"Authorization": "Bearer test-session-token"},
        data=json.dumps(
            {
                "repo_path": "/home/egg/repos/test-repo",
                "remote": "origin",
                "refspec": "egg-feature",
            }
        ),
        content_type="application/json",
    )


class TestPushErrorEnrichment:
    """Verify enriched error response when push is denied by agent-role restrictions."""

    def test_response_includes_blocked_files(self, client):
        """The 403 response data include which files were blocked."""
        session = _make_session("tester")
        blocked_files = ["docs/guide.md", "src/main.py"]
        patches = _push_context(session, blocked_files)

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                # make_error puts details into response["data"]
                resp_data = data.get("data", {})
                assert "blocked_files" in resp_data
                assert resp_data["blocked_files"] == blocked_files

    def test_response_includes_allowed_patterns(self, client):
        """The 403 response data include patterns the agent CAN write."""
        session = _make_session("tester")
        patches = _push_context(session, ["docs/guide.md"])

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                resp_data = data.get("data", {})
                assert "allowed_patterns" in resp_data
                # Tester allowed_patterns should include test-related patterns
                patterns = resp_data["allowed_patterns"]
                assert isinstance(patterns, list)
                assert len(patterns) > 0
                assert "tests/" in patterns

    def test_response_includes_remediation(self, client):
        """The 403 response data include remediation guidance."""
        session = _make_session("coder")
        patches = _push_context(session, ["tests/test_foo.py"])

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                resp_data = data.get("data", {})
                assert "remediation" in resp_data
                remediation = resp_data["remediation"]
                # Should mention git reset recovery steps
                assert "git reset HEAD~1" in remediation
                # Should mention the scope-filter alternative
                assert "scope-filter" in remediation

    def test_response_includes_role(self, client):
        """The 403 response data include the agent's role."""
        session = _make_session("documenter")
        patches = _push_context(session, ["src/main.py"])

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                resp_data = data.get("data", {})
                assert resp_data["role"] == "documenter"

    def test_error_message_mentions_role_and_files(self, client):
        """The top-level error message mentions the agent's role."""
        session = _make_session("tester")
        patches = _push_context(session, ["docs/guide.md"])

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                assert "tester" in data["message"]

    def test_allowed_patterns_match_role_from_registry(self, client):
        """allowed_patterns in response should match the role's actual patterns."""
        from egg_restrictions import get_agent_pattern

        session = _make_session("coder")
        patches = _push_context(session, ["tests/test_foo.py"])

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                resp_data = data.get("data", {})
                # Verify the patterns match the actual registry
                coder_pattern = get_agent_pattern("coder")
                assert resp_data["allowed_patterns"] == coder_pattern.allowed_patterns

    def test_unknown_role_returns_empty_patterns(self, client):
        """An unrecognized role still returns a 403 with empty allowed_patterns.

        get_agent_pattern returns None for unknown roles — the code should
        handle this gracefully.
        """
        session = _make_session("unknown_role_xyz")
        # For unknown roles, check_agent_restrictions would normally allow,
        # but we're mocking it to block to test the error enrichment code path
        patches = _push_context(session, ["src/main.py"])

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                resp_data = data.get("data", {})
                # For unknown roles, get_agent_pattern returns None,
                # so allowed_patterns should be []
                assert resp_data["allowed_patterns"] == []
                # remediation should still be present
                assert "remediation" in resp_data

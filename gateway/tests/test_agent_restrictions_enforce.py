"""Tests for agent-role restriction enforcement behavior.

Validates the EGG_AGENT_RESTRICTIONS_ENFORCE flag controlling whether
agent-role file restriction violations block pushes (enforce mode)
or only log warnings (warn-only mode, default).
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


def _make_coder_session():
    """Create a mock session with 'coder' agent role."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = "coder"
    mock_session.phase = None
    return mock_session


def _push_context(mock_session, agent_blocked=True):
    """Return a context manager that sets up all mocking for a push request.

    Args:
        mock_session: Mock session object.
        agent_blocked: If True, check_agent_restrictions returns a blocked result.
    """
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

    if agent_blocked:
        agent_result = FileRestrictionResult.block(
            message="Coder cannot modify test files",
            role="coder",
            blocked_files=["tests/test_foo.py"],
            blocked_reason="Test files belong to tester role",
        )
    else:
        agent_result = FileRestrictionResult.allow("All files allowed for role")

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
        patch.object(
            gateway, "get_changed_files_in_push", return_value=(["tests/test_foo.py"], None)
        ),
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        patch.object(gateway, "check_agent_restrictions", return_value=agent_result),
    )


def _do_push(client):
    """Send a push request and return the response."""
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


class TestAgentRestrictionsWarnOnly:
    """Default warn-only mode: violations logged but push proceeds."""

    def test_warn_mode_allows_push(self, client):
        """In default warn-only mode, agent restriction violations allow push."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
            # Ensure enforce is off (default)
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"}):
                response = _do_push(client)
                assert response.status_code == 200

    def test_warn_mode_is_default(self, client):
        """Without the env var set, warn-only mode is used."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
            # Remove the env var entirely
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EGG_AGENT_RESTRICTIONS_ENFORCE", None)
                response = _do_push(client)
                assert response.status_code == 200


class TestAgentRestrictionsEnforceMode:
    """Enforce mode: violations block pushes."""

    def test_enforce_mode_blocks_push(self, client):
        """With EGG_AGENT_RESTRICTIONS_ENFORCE=true, violations return 403."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
                assert "coder" in data["message"]

    def test_enforce_mode_allows_clean_push(self, client):
        """Enforce mode allows push when agent restrictions pass."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=False)

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
                assert response.status_code == 200

    def test_enforce_accepts_yes_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=yes works as enforce."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "yes"}):
                response = _do_push(client)
                assert response.status_code == 403

    def test_enforce_accepts_1_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=1 works as enforce."""
        session = _make_coder_session()
        patches = _push_context(session, agent_blocked=True)

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
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "1"}):
                response = _do_push(client)
                assert response.status_code == 403


class TestAgentRestrictionsUnknownRole:
    """Unknown agent roles should pass (unknown roles handled by check_agent_restrictions)."""

    def test_unknown_role_passes_when_allowed(self, client):
        """Unknown roles that pass check_agent_restrictions are allowed."""
        session = _make_coder_session()
        session.agent_role = "unknown_role"
        patches = _push_context(session, agent_blocked=False)

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
                assert response.status_code == 200


class TestAgentRestrictionsNoRole:
    """Sessions without agent_role skip agent restriction checks."""

    def test_no_role_skips_check(self, client):
        """Sessions without agent_role bypass agent restriction checks."""
        session = _make_coder_session()
        session.agent_role = None
        patches = _push_context(session, agent_blocked=True)

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
                # Should succeed because agent_role is None, so the check is skipped
                assert response.status_code == 200

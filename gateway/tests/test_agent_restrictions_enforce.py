"""Tests for agent-role restriction enforcement behavior.

Validates the EGG_AGENT_RESTRICTIONS_ENFORCE flag controlling whether
agent-role file restriction violations block pushes (enforce mode)
or only log warnings (warn-only mode). Enforce mode is the default.
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
    """Explicit warn-only mode: violations logged but push proceeds."""

    def test_warn_mode_allows_push(self, client):
        """With EGG_AGENT_RESTRICTIONS_ENFORCE=false, violations allow push."""
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
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"}):
                response = _do_push(client)
                assert response.status_code == 200

    def test_warn_mode_accepts_0_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=0 works as warn-only."""
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
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "0"}):
                response = _do_push(client)
                assert response.status_code == 200

    def test_warn_mode_accepts_no_value(self, client):
        """EGG_AGENT_RESTRICTIONS_ENFORCE=no works as warn-only."""
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
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "no"}):
                response = _do_push(client)
                assert response.status_code == 200

    def test_enforce_mode_is_default(self, client):
        """Without the env var set, enforce mode is used (blocks violations)."""
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
            # Remove the env var entirely — default should enforce
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("EGG_AGENT_RESTRICTIONS_ENFORCE", None)
                response = _do_push(client)
                assert response.status_code == 403


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


def _push_context_real_check(mock_session, changed_files):
    """Like _push_context but does NOT mock check_agent_restrictions.

    Used for TASK-5-3 end-to-end push-rejection scenarios that drive the
    real gateway check_agent_restrictions → validate_agent_push code path.
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
        patch.object(gateway, "get_changed_files_in_push", return_value=(changed_files, None)),
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        # Intentionally NOT mocking check_agent_restrictions — drive the real
        # validate_agent_push path against the new blocklist-complement
        # CODER_PATTERNS from #1901.
    )


def _make_role_session(role):
    """Create a mock session with the given agent role."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = None
    return mock_session


class TestCoderEndToEndPushRejection1901:
    """TASK-5-3 (#1901): end-to-end push rejection via the real
    check_agent_restrictions code path for session_role='coder'.

    These tests assert the real gateway response — they don't mock the
    agent-restriction decision — so they catch regressions in
    CODER_PATTERNS, validate_agent_push, the FileRestrictionResult
    bridge, and the gateway's response shaping in one shot.
    """

    def _coder_session(self):
        mock_session = MagicMock()
        mock_session.mode = "public"
        mock_session.container_id = "test-container"
        mock_session.expires_at = None
        mock_session.agent_role = "coder"
        mock_session.phase = None
        return mock_session

    def test_coder_can_push_extensionless_bin_egg(self, client):
        """bin/egg was previously blocked under the legacy allowlist; now allowed."""
        session = self._coder_session()
        patches = _push_context_real_check(session, ["bin/egg"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200, response.data

    def test_coder_blocked_from_docs_md(self, client):
        session = self._coder_session()
        patches = _push_context_real_check(session, ["docs/x.md"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                # Format: "...agent role 'coder' cannot modify ... docs/x.md"
                msg = data["message"].lower()
                assert "coder" in msg
                assert "cannot modify" in msg
                assert "docs/x.md" in data["message"]

    def test_coder_blocked_from_tests(self, client):
        session = self._coder_session()
        patches = _push_context_real_check(session, ["tests/test_x.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                msg = data["message"].lower()
                assert "coder" in msg
                assert "cannot modify" in msg
                assert "tests/test_x.py" in data["message"]

    def test_coder_blocked_from_contracts(self, client):
        session = self._coder_session()
        patches = _push_context_real_check(session, [".egg-state/contracts/foo.json"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                msg = data["message"].lower()
                assert "coder" in msg
                assert "cannot modify" in msg
                assert ".egg-state/contracts/foo.json" in data["message"]


class TestTesterEndToEndPushRejection1901:
    """TASK-5-3 (#1901): end-to-end push rejection via the real
    check_agent_restrictions code path for session_role='tester'.
    """

    def test_tester_can_push_test_files(self, client):
        """Tester is allowed to push test files."""
        session = _make_role_session("tester")
        patches = _push_context_real_check(session, ["tests/test_foo.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200, response.data

    def test_tester_blocked_from_source_code(self, client):
        """Tester cannot push source code files."""
        session = _make_role_session("tester")
        patches = _push_context_real_check(session, ["shared/egg_restrictions/patterns.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                msg = data["message"].lower()
                assert "tester" in msg
                assert "cannot modify" in msg

    def test_tester_blocked_from_docs(self, client):
        """Tester cannot push documentation files."""
        session = _make_role_session("tester")
        patches = _push_context_real_check(session, ["docs/guide.md"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                msg = data["message"].lower()
                assert "tester" in msg
                assert "cannot modify" in msg


class TestDocumenterEndToEndPushRejection1901:
    """TASK-5-3 (#1901): end-to-end push rejection via the real
    check_agent_restrictions code path for session_role='documenter'.
    """

    def test_documenter_can_push_docs(self, client):
        """Documenter is allowed to push documentation files."""
        session = _make_role_session("documenter")
        patches = _push_context_real_check(session, ["docs/guide.md"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 200, response.data

    def test_documenter_blocked_from_source_code(self, client):
        """Documenter cannot push source code files."""
        session = _make_role_session("documenter")
        patches = _push_context_real_check(session, ["gateway/auth.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                msg = data["message"].lower()
                assert "documenter" in msg
                assert "cannot modify" in msg

    def test_documenter_blocked_from_tests(self, client):
        """Documenter cannot push test files."""
        session = _make_role_session("documenter")
        patches = _push_context_real_check(session, ["tests/test_x.py"])
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                msg = data["message"].lower()
                assert "documenter" in msg
                assert "cannot modify" in msg

"""Tests for concurrent-mode push blocking (#1669).

When EGG_CONCURRENT_MODE=true, direct git pushes from pipeline agents must be
blocked. Agents must use `egg-orch consensus propose --push` instead, which
sets a `consensus_push` marker in the request payload.

The gateway enforces this AFTER the push-target enforcement and BEFORE the
branch ownership check. Infrastructure pushes (checkpoints, pipeline state)
are always exempt.

Test scenarios:
  1. Push blocked in concurrent mode without consensus_push marker
  2. Push allowed with consensus_push=true in payload
  3. Push allowed when EGG_CONCURRENT_MODE is not "true"
  4. Push allowed for sessions without a pipeline_id
  5. Killswitch CONCURRENT_PUSH_ENFORCEMENT=false disables enforcement
  6. Infrastructure pushes are exempt from concurrent-mode blocking
  7. Edge cases: case-insensitive concurrent mode, falsy consensus_push values
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import session_manager
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


def _make_session(
    role: str = "coder",
    pipeline_id: str | None = "issue-1669",
    assigned_branch: str | None = "egg/issue-1669",
) -> MagicMock:
    """Create a mock session with the given agent role and pipeline context."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = "implement"
    mock_session.pipeline_id = pipeline_id
    mock_session.assigned_branch = assigned_branch
    return mock_session


def _push_context(mock_session):
    """Return context managers for a push request that reaches the concurrent check.

    Mocks everything that happens before the concurrent-mode enforcement
    so the request reaches that code path.
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
            result.stdout = "egg/issue-1669\n"
        elif "push" in cmd:
            result.stdout = "Everything up-to-date\n"
        elif "diff" in cmd:
            result.stdout = ""
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
                        details={"branch": "egg/issue-1669"},
                    )
                ),
            ),
        ),
        patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
        patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
        patch.object(
            gateway,
            "check_file_restrictions",
            return_value=MagicMock(allowed=True, blocked=False),
        ),
        patch.object(
            gateway,
            "check_agent_restrictions",
            return_value=MagicMock(allowed=True, blocked=False),
        ),
    )


def _do_push(client, consensus_push: bool | None = None, refspec: str = "egg/issue-1669"):
    """Send a push request, optionally including the consensus_push marker."""
    payload = {
        "repo_path": "/home/egg/repos/test-repo",
        "remote": "origin",
        "refspec": refspec,
    }
    if consensus_push is not None:
        payload["consensus_push"] = consensus_push
    return client.post(
        "/api/v1/git/push",
        headers={"Authorization": "Bearer test-session-token"},
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestConcurrentPushBlock:
    """Verify that direct pushes are blocked in concurrent mode (#1669)."""

    def test_push_blocked_without_consensus_marker(self, client):
        """Direct push in concurrent mode without consensus_push should return 403."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                assert data["success"] is False
                assert "concurrent mode" in data["message"].lower()
                assert "consensus" in data["message"].lower()

    def test_push_allowed_with_consensus_marker(self, client):
        """Push with consensus_push=true should be allowed in concurrent mode."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client, consensus_push=True)
                # Should not be blocked by concurrent check — may succeed or
                # fail later for other reasons, but NOT 403 concurrent block
                if response.status_code == 403:
                    data = json.loads(response.data)
                    assert "concurrent mode" not in data["message"].lower()

    def test_push_allowed_when_not_concurrent_mode(self, client):
        """Push without concurrent mode should not be blocked by this check."""
        session = _make_session("coder")
        patches = _push_context(session)

        # EGG_CONCURRENT_MODE not set or "false"
        env = {
            "EGG_CONCURRENT_MODE": "false",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client)
                if response.status_code == 403:
                    data = json.loads(response.data)
                    assert "concurrent mode" not in data["message"].lower()

    def test_push_allowed_without_pipeline_id(self, client):
        """Push in concurrent mode but without pipeline_id should not be blocked."""
        session = _make_session("coder", pipeline_id=None)
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client)
                if response.status_code == 403:
                    data = json.loads(response.data)
                    assert "concurrent mode" not in data["message"].lower()

    def test_killswitch_disables_enforcement(self, client):
        """CONCURRENT_PUSH_ENFORCEMENT=false should bypass the check."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
            "CONCURRENT_PUSH_ENFORCEMENT": "false",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client)
                if response.status_code == 403:
                    data = json.loads(response.data)
                    assert "concurrent mode" not in data["message"].lower()

    def test_infrastructure_push_exempt(self, client):
        """Pushes to infrastructure branches should bypass concurrent check."""
        from egg_config.constants import CHECKPOINT_BRANCH

        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                # Push to infrastructure branch
                response = _do_push(client, refspec=CHECKPOINT_BRANCH)
                if response.status_code == 403:
                    data = json.loads(response.data)
                    assert "concurrent mode" not in data["message"].lower()


class TestConcurrentPushBlockEdgeCases:
    """Edge cases for concurrent-mode push blocking."""

    def test_concurrent_mode_case_insensitive(self, client):
        """EGG_CONCURRENT_MODE should be case-insensitive (TRUE, True, etc.)."""
        session = _make_session("coder")
        patches = _push_context(session)

        for variant in ["TRUE", "True", "true"]:
            env = {
                "EGG_CONCURRENT_MODE": variant,
            }

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
                with patch.dict(os.environ, env):
                    response = _do_push(client)
                    assert response.status_code == 403, (
                        f"Expected 403 for EGG_CONCURRENT_MODE={variant}"
                    )

    def test_consensus_push_false_still_blocks(self, client):
        """consensus_push=false in payload should still be blocked."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client, consensus_push=False)
                assert response.status_code == 403
                data = json.loads(response.data)
                assert "concurrent mode" in data["message"].lower()

    def test_error_response_includes_details(self, client):
        """The 403 error should include mode, pipeline_id, and requirement details."""
        session = _make_session("coder", pipeline_id="issue-1669")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                resp_data = data.get("data", {})
                # Per the plan, details should include mode, pipeline_id, requirement
                assert "mode" in resp_data or "pipeline_id" in resp_data

    def test_error_message_suggests_consensus_propose(self, client):
        """The 403 error message should mention 'egg-orch consensus propose --push'."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                response = _do_push(client)
                assert response.status_code == 403
                data = json.loads(response.data)
                assert "egg-orch consensus propose --push" in data["message"]

    def test_concurrent_mode_not_set_allows_push(self, client):
        """When EGG_CONCURRENT_MODE is not set at all, push should not be blocked."""
        session = _make_session("coder")
        patches = _push_context(session)

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
            # Ensure EGG_CONCURRENT_MODE is not in the environment
            env_clear = {"EGG_CONCURRENT_MODE": ""}
            with patch.dict(os.environ, env_clear):
                os.environ.pop("EGG_CONCURRENT_MODE", None)
                response = _do_push(client)
                if response.status_code == 403:
                    data = json.loads(response.data)
                    assert "concurrent mode" not in data["message"].lower()

    def test_killswitch_values(self, client):
        """Killswitch should accept '0' and 'no' in addition to 'false'."""
        session = _make_session("coder")
        patches = _push_context(session)

        for killswitch_val in ["false", "0", "no"]:
            env = {
                "EGG_CONCURRENT_MODE": "true",
                "CONCURRENT_PUSH_ENFORCEMENT": killswitch_val,
            }

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
                with patch.dict(os.environ, env):
                    response = _do_push(client)
                    if response.status_code == 403:
                        data = json.loads(response.data)
                        assert "concurrent mode" not in data["message"].lower(), (
                            f"Killswitch value '{killswitch_val}' did not disable enforcement"
                        )

    def test_different_agent_roles_all_blocked(self, client):
        """All agent roles (coder, tester, documenter) should be blocked equally."""
        for role in ["coder", "tester", "documenter", "reviewer_code"]:
            session = _make_session(role)
            patches = _push_context(session)

            env = {
                "EGG_CONCURRENT_MODE": "true",
            }

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
                with patch.dict(os.environ, env):
                    response = _do_push(client)
                    assert response.status_code == 403, (
                        f"Expected 403 for role '{role}' in concurrent mode"
                    )

    def test_audit_log_emitted_on_block(self, client):
        """An audit log event should be emitted when push is blocked."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "EGG_CONCURRENT_MODE": "true",
        }

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
            with patch.dict(os.environ, env):
                with patch.object(gateway, "audit_log") as mock_audit:
                    response = _do_push(client)
                    assert response.status_code == 403
                    # Verify audit_log was called with concurrent push denial
                    found_concurrent_deny = False
                    for call in mock_audit.call_args_list:
                        args = call[0] if call[0] else []
                        if args and "concurrent" in str(args[0]).lower():
                            found_concurrent_deny = True
                            break
                    assert found_concurrent_deny, (
                        "Expected audit_log to be called with a concurrent push denial event"
                    )

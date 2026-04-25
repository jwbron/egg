"""Tests for pipeline-session push blocking (#1669, #1994, #2028).

All SDLC producer phases (refine/plan/implement) are BRC phases, so every
pipeline-session push must route through ``mcp__brc__propose`` (or the
fallback CLI ``egg-orch consensus propose --push``), which sets a
``consensus_push`` marker in the request payload.  Direct ``git push``
from a pipeline session — regardless of ``EGG_CONCURRENT_MODE`` — is
rejected with a single actionable error pointing at the right tool,
instead of the three-layer error cascade (#2028).

The gateway enforces this BEFORE the push-target enforcement so BRC agents
on per-role work branches see the actionable "use mcp__brc__propose" error
first rather than a misleading wrong-branch message (#1994). Infrastructure
pushes (checkpoints, pipeline state) are always exempt.

Test scenarios:
  1. Push blocked for pipeline session without consensus_push marker
  2. Push allowed with consensus_push=true in payload
  3. Push allowed for sessions without a pipeline_id (non-pipeline)
  4. Killswitch PIPELINE_PUSH_ENFORCEMENT=false disables enforcement
     (and legacy alias CONCURRENT_PUSH_ENFORCEMENT=false)
  5. Infrastructure pushes are exempt
  6. Edge case: falsy consensus_push values still block
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
    """Return context managers for a push request that reaches the pipeline-push check.

    Mocks everything that happens before pipeline-push enforcement
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


class TestPipelinePushBlock:
    """Verify that direct pushes from pipeline sessions are blocked (#2028)."""

    def test_push_blocked_without_consensus_marker(self, client):
        """Direct push from a pipeline session without consensus_push should return 403."""
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
            response = _do_push(client)
            assert response.status_code == 403
            data = json.loads(response.data)
            assert data["success"] is False
            assert "pipeline sessions" in data["message"].lower()
            assert "mcp__brc__propose" in data["message"]

    def test_push_allowed_with_consensus_marker(self, client):
        """Push with consensus_push=true should be allowed."""
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
            response = _do_push(client, consensus_push=True)
            assert response.status_code == 200, (
                f"Expected 200 for consensus push, got {response.status_code}"
            )

    def test_push_blocked_without_concurrent_mode(self, client):
        """Pipeline-session push is blocked regardless of EGG_CONCURRENT_MODE (#2028)."""
        session = _make_session("coder")
        patches = _push_context(session)

        # EGG_CONCURRENT_MODE explicitly false — still blocked.
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
                assert response.status_code == 403, (
                    f"Expected 403 for pipeline push without consensus_push, got {response.status_code}"
                )

    def test_push_allowed_without_pipeline_id(self, client):
        """Non-pipeline sessions (no pipeline_id) bypass pipeline-push enforcement."""
        session = _make_session("coder", pipeline_id=None)
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
            response = _do_push(client)
            assert response.status_code == 200, (
                f"Expected 200 without pipeline_id, got {response.status_code}"
            )

    def test_killswitch_disables_enforcement(self, client):
        """PIPELINE_PUSH_ENFORCEMENT=false should bypass the check."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
            "PIPELINE_PUSH_ENFORCEMENT": "false",
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
                assert response.status_code == 200, (
                    f"Expected 200 with killswitch, got {response.status_code}"
                )

    def test_legacy_killswitch_alias_disables_enforcement(self, client):
        """Legacy CONCURRENT_PUSH_ENFORCEMENT=false alias should still bypass."""
        session = _make_session("coder")
        patches = _push_context(session)

        env = {
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
                assert response.status_code == 200, (
                    f"Expected 200 with legacy killswitch, got {response.status_code}"
                )

    def test_infrastructure_push_exempt(self, client):
        """Pushes to infrastructure branches bypass pipeline-push enforcement."""
        from egg_config.constants import CHECKPOINT_BRANCH

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
            # Push to infrastructure branch — should not be blocked by
            # pipeline-push enforcement (infrastructure is exempt).
            # Note: We don't assert status_code == 200 here because
            # infrastructure pushes may be rejected by other enforcement
            # layers (e.g., branch ownership). We only verify that the
            # pipeline-push check specifically does not block it.
            response = _do_push(client, refspec=CHECKPOINT_BRANCH)
            assert response.status_code != 403 or (
                "pipeline sessions" not in json.loads(response.data)["message"].lower()
            ), "Infrastructure push should not be blocked by pipeline-push enforcement"


class TestPipelinePushBlockEdgeCases:
    """Edge cases for pipeline-session push blocking."""

    def test_consensus_push_false_still_blocks(self, client):
        """consensus_push=false in payload should still be blocked."""
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
            response = _do_push(client, consensus_push=False)
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "pipeline sessions" in data["message"].lower()

    def test_error_response_includes_details(self, client):
        """The 403 error should include pipeline_id, requirement, and recommended_tool."""
        session = _make_session("coder", pipeline_id="issue-1669")
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
            response = _do_push(client)
            assert response.status_code == 403
            data = json.loads(response.data)
            resp_data = data.get("data", {})
            assert resp_data.get("pipeline_id") == "issue-1669"
            assert resp_data.get("requirement") == "consensus_push"
            assert resp_data.get("recommended_tool") == "mcp__brc__propose"

    def test_error_message_suggests_consensus_propose(self, client):
        """The 403 error message should point at mcp__brc__propose (primary)
        and list the CLI fallback (#1994)."""
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
            response = _do_push(client)
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "mcp__brc__propose" in data["message"]
            assert "egg-orch consensus propose --push" in data["message"]
            assert data.get("data", {}).get("recommended_tool") == "mcp__brc__propose"

    def test_killswitch_values(self, client):
        """Killswitch should accept '0' and 'no' in addition to 'false'."""
        session = _make_session("coder")
        patches = _push_context(session)

        for killswitch_val in ["false", "0", "no"]:
            env = {
                "PIPELINE_PUSH_ENFORCEMENT": killswitch_val,
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
                    assert response.status_code == 200, (
                        f"Expected 200 with killswitch='{killswitch_val}', got {response.status_code}"
                    )

    def test_different_agent_roles_all_blocked(self, client):
        """All agent roles (coder, tester, documenter) should be blocked equally."""
        for role in ["coder", "tester", "documenter", "reviewer_code"]:
            session = _make_session(role)
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
                response = _do_push(client)
                assert response.status_code == 403, (
                    f"Expected 403 for role '{role}' pipeline session"
                )

    def test_audit_log_emitted_on_block(self, client):
        """An audit log event should be emitted when push is blocked."""
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
            with patch.object(gateway, "audit_log") as mock_audit:
                response = _do_push(client)
                assert response.status_code == 403
                found_pipeline_deny = False
                for call in mock_audit.call_args_list:
                    args = call[0] if call[0] else []
                    if args and "pipeline_session" in str(args[0]).lower():
                        found_pipeline_deny = True
                        break
                assert found_pipeline_deny, (
                    "Expected audit_log to be called with a pipeline-session push denial event"
                )

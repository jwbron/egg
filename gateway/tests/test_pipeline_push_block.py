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
    synthetic: bool = False,
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
    mock_session.synthetic = synthetic
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
        # Slot 6 of the returned tuple is reserved for the agent-role
        # restriction patch.  After #2489 the gateway no longer calls
        # the legacy whole-push-diff ``check_file_restrictions`` from
        # ``git_push`` (the attribution-aware ``check_agent_restrictions``
        # path is the sole agent-role enforcer), so we no longer patch
        # the dead symbol here.
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


def _launcher_auth_context():
    """Patches that let a launcher-authed push reach the success path.

    Mirrors ``_push_context`` but skips session validation — launcher auth
    sets ``g.session = None`` and is recognised before session-token
    validation runs.  The git subprocess shim and policy stubs let the
    push run end-to-end so we can assert the request was accepted.
    """
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode",
        visibility="public",
    )

    def run_side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "remote" in cmd and "get-url" in cmd:
            result.stdout = "https://github.com/owner/repo.git\n"
        elif "branch" in cmd and "--show-current" in cmd:
            result.stdout = "egg/issue-2051\n"
        elif "push" in cmd:
            result.stdout = "Everything up-to-date\n"
        elif "diff" in cmd:
            result.stdout = ""
        else:
            result.stdout = ""
        return result

    return (
        patch.object(gateway, "get_launcher_secret", return_value="test-launcher-secret"),
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
                        details={"branch": "egg/issue-2051"},
                    )
                ),
            ),
        ),
        patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
        patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
    )


def _do_launcher_push(client, refspec: str = "egg/issue-2051", **extra_payload):
    """Send a launcher-authed push (no session token, launcher secret bearer)."""
    payload = {
        "repo_path": "/home/egg/repos/test-repo",
        "remote": "origin",
        "refspec": refspec,
        **extra_payload,
    }
    return client.post(
        "/api/v1/git/push",
        headers={"Authorization": "Bearer test-launcher-secret"},
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestOrchestratorLauncherAuthPush:
    """Verify that launcher-authed pushes bypass agent-targeted enforcement (#2051).

    The orchestrator has a different trust boundary than sandboxed agents
    — it holds the launcher secret already used by ``/api/v1/sessions/create``.
    Push requests authenticated with that secret are orchestrator-trusted
    (programmatic contract init / state-sync / completion pushes) and skip
    the pipeline-push enforcement that would otherwise block them.
    """

    def test_launcher_push_to_pipeline_branch_bypasses_block(self, client):
        """A launcher-authed push to a pipeline branch is NOT blocked.

        Regression for #2051: the orchestrator's contract-init push targets
        ``egg/issue-<N>`` (a pipeline branch) and previously got a 403
        because temp sessions inherit ``pipeline_id``.  With launcher auth
        there is no session, and the push is allowed.
        """
        patches = _launcher_auth_context()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            response = _do_launcher_push(client)
            assert response.status_code == 200, (
                f"Expected 200 for launcher-auth push, got {response.status_code}: "
                f"{response.data!r}"
            )

    def test_launcher_push_skips_consensus_push_requirement(self, client):
        """Launcher-auth push needs no ``consensus_push`` marker.

        The marker exists for the BRC-on-agent-side path; launcher-auth
        identifies the orchestrator directly.  No marker is required.
        """
        patches = _launcher_auth_context()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            response = _do_launcher_push(client)
            assert response.status_code == 200
            data = json.loads(response.data)
            # Should NOT carry the agent-targeted "use mcp__brc__propose" hint.
            assert "mcp__brc__propose" not in data.get("message", "")

    def test_launcher_push_audited_as_orchestrator_authenticated(self, client):
        """Launcher-auth pushes emit a distinct audit event for traceability."""
        patches = _launcher_auth_context()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with patch.object(gateway, "audit_log") as mock_audit:
                response = _do_launcher_push(client)
                assert response.status_code == 200
                events = [call.args[0] if call.args else None for call in mock_audit.call_args_list]
                assert "push_orchestrator_authenticated" in events, (
                    f"Expected push_orchestrator_authenticated audit event, got {events}"
                )

    def test_session_token_path_still_blocks_pipeline_push(self, client):
        """The session-token branch of the auth decorator still enforces #2028.

        Adding launcher auth must NOT loosen enforcement for agent
        sessions — only sandbox agents are subject to the BRC routing.
        """
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
        ):
            response = _do_push(client)
            assert response.status_code == 403, (
                "Session-token push to a pipeline branch must still be blocked"
            )

    def test_launcher_push_invalid_secret_falls_back_to_session_auth(self, client):
        """A bearer that is neither the launcher secret nor a valid session
        token returns 401 from session-auth (the fallback path)."""
        # Launcher secret patched to a known value; we send a different bearer.
        with patch.object(gateway, "get_launcher_secret", return_value="real-secret"):
            response = client.post(
                "/api/v1/git/push",
                headers={"Authorization": "Bearer wrong-bearer"},
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test-repo",
                        "remote": "origin",
                        "refspec": "egg/issue-2051",
                    }
                ),
                content_type="application/json",
            )
            # session_manager rejects the unknown token via the fallback
            # require_session_auth path → 401, not 403.
            assert response.status_code == 401

    def test_launcher_secret_not_configured_falls_back_to_session_auth(self, client):
        """When the gateway has no launcher secret configured at all, every
        bearer falls through to session auth and is rejected as 401 cleanly
        (the ``LauncherSecretNotConfiguredError`` is swallowed inside the
        decorator)."""
        # get_launcher_secret raises — simulates an unconfigured gateway.
        with patch.object(
            gateway,
            "get_launcher_secret",
            side_effect=gateway.LauncherSecretNotConfiguredError("Launcher secret not configured"),
        ):
            response = client.post(
                "/api/v1/git/push",
                headers={"Authorization": "Bearer any-bearer"},
                data=json.dumps(
                    {
                        "repo_path": "/home/egg/repos/test-repo",
                        "remote": "origin",
                        "refspec": "egg/issue-2051",
                    }
                ),
                content_type="application/json",
            )
            # No launcher secret means we can't match — fall through to
            # session-auth, which rejects the unknown token cleanly.
            assert response.status_code == 401

    def test_launcher_push_invalid_mode_rejected(self, client):
        """A launcher-auth push with a non-{public,private} ``mode`` returns 400.

        The orchestrator's ``_do_push`` is typed
        ``Literal["public", "private"]``, but if the launcher secret were ever
        used by another caller, an unknown value would silently degrade to
        public-mode policy in ``check_private_repo_access``.  Explicit
        validation closes that gap.
        """
        patches = _launcher_auth_context()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            response = _do_launcher_push(client, mode="banana")
            assert response.status_code == 400
            data = json.loads(response.data)
            assert "mode" in data["message"].lower()

    def test_launcher_push_missing_mode_uses_session_default(self, client):
        """A launcher-auth push without ``mode`` in the body is not rejected by
        the new validator (only invalid values are).

        Note that in production ``check_private_repo_access`` would still 403
        with "No session mode specified" — this test exercises the validator in
        isolation (``check_private_repo_access`` is mocked to allow); the
        orchestrator's ``_do_push`` always sends mode in practice.
        """
        patches = _launcher_auth_context()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            response = _do_launcher_push(client)  # no mode kwarg
            assert response.status_code == 200, (
                f"Expected 200 with no mode, got {response.status_code}: {response.data!r}"
            )


class TestSliceIntegrationBranchExemption:
    """Slice integration-branch creation exemption (#2368).

    The orchestrator's ``create_slice_integration_branch`` registers a
    synthetic, launcher-authed session and pushes
    ``parent:refs/heads/egg/<base>/slice-N`` so the slice PR's diff is
    non-empty before agents spawn.  That push is orchestrator
    infrastructure and must bypass the #2028 pipeline-session block.
    The exemption is keyed on the session's ``synthetic`` flag — only
    the launcher can set it — and the slice integration-branch shape.
    """

    def test_synthetic_session_slice_branch_push_allowed(self, client):
        """Synthetic-session push to ``egg/issue-N/slice-M`` is allowed."""
        session = _make_session(synthetic=True, assigned_branch="egg/issue-2261/slice-7")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="egg/issue-2261:refs/heads/egg/issue-2261/slice-7",
            )
            assert response.status_code == 200, (
                f"Expected 200 for synthetic slice integration push, "
                f"got {response.status_code}: {response.data!r}"
            )

    def test_synthetic_session_qualified_slice_branch_push_allowed(self, client):
        """Qualifier-suffixed branches (#2368 bonus) — ``egg/issue-N-v3/slice-M`` — pass."""
        session = _make_session(synthetic=True, assigned_branch="egg/issue-2261-v3/slice-7")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="egg/issue-2261-v3:refs/heads/egg/issue-2261-v3/slice-7",
            )
            assert response.status_code == 200

    def test_synthetic_session_jira_slice_branch_push_allowed(self, client):
        """JIRA-driven branches — ``egg/KORE-1234/slice-M`` — pass."""
        session = _make_session(synthetic=True, assigned_branch="egg/KORE-1234/slice-3")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="egg/KORE-1234:refs/heads/egg/KORE-1234/slice-3",
            )
            assert response.status_code == 200

    def test_synthetic_session_legacy_phase_branch_push_allowed(self, client):
        """Legacy ``phase-N`` slice IDs (pre-#2137) still flow through the loader,
        so the integration-branch exemption must accept them too."""
        session = _make_session(synthetic=True, assigned_branch="egg/issue-2261/phase-1")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="egg/issue-2261:refs/heads/egg/issue-2261/phase-1",
            )
            assert response.status_code == 200

    def test_non_synthetic_session_slice_branch_push_blocked(self, client):
        """Agent (non-synthetic) push to a slice integration branch is still blocked.

        Agents reach a slice's integration branch via ``mcp__brc__propose``
        (``consensus_push=true``); a direct push is the very pattern #2028 is
        designed to catch.  The exemption MUST gate on ``synthetic=True`` —
        if the regex alone is enough, agents can use slice-shaped branch
        names to bypass enforcement.
        """
        session = _make_session(synthetic=False, assigned_branch="egg/issue-2261/slice-7")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="egg/issue-2261:refs/heads/egg/issue-2261/slice-7",
            )
            assert response.status_code == 403, (
                "Non-synthetic session push to slice integration branch must still "
                "be blocked by pipeline-session enforcement"
            )
            data = json.loads(response.data)
            assert "pipeline sessions" in data["message"].lower()

    def test_synthetic_session_non_slice_branch_still_blocked(self, client):
        """Synthetic flag alone is not enough — branch must match the slice shape.

        Defends against future code paths that mark a session synthetic for
        unrelated reasons but would bypass the agent-push block if the
        branch-shape gate were missing.
        """
        session = _make_session(synthetic=True, assigned_branch="egg/issue-2261")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(client, refspec="egg/issue-2261")
            assert response.status_code == 403

    def test_synthetic_session_multi_segment_base_blocked(self, client):
        """Multi-segment base shapes (``egg/foo/bar/slice-N``) are not produced
        by the orchestrator and the regex MUST reject them.

        The documented branch shape is ``egg/<single-segment>/(slice|phase)-N``;
        accepting multi-segment bases would widen the exemption surface beyond
        what the orchestrator actually emits.
        """
        session = _make_session(synthetic=True, assigned_branch="egg/foo/bar/slice-1")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="egg/foo:refs/heads/egg/foo/bar/slice-1",
            )
            assert response.status_code == 403

    def test_audit_event_records_slice_integration_exempt_type(self, client):
        """Exemption emits a distinct audit event so operators can trace
        synthetic-session pushes separately from checkpoint/state writes."""
        session = _make_session(synthetic=True, assigned_branch="egg/issue-2261/slice-7")
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.object(gateway, "audit_log") as mock_audit:
                response = _do_push(
                    client,
                    refspec="egg/issue-2261:refs/heads/egg/issue-2261/slice-7",
                )
                assert response.status_code == 200
                events = [
                    (call.args[0] if call.args else None, call.kwargs.get("details") or {})
                    for call in mock_audit.call_args_list
                ]
                slice_events = [e for e in events if e[0] == "push_slice_integration_exempt"]
                assert slice_events, (
                    f"Expected push_slice_integration_exempt event, got: {[e[0] for e in events]}"
                )
                infra_events = [
                    e
                    for e in events
                    if e[0] == "push_infrastructure_exempt"
                    and e[1].get("exempt_type") == "slice_integration_branch"
                ]
                assert infra_events, (
                    "Expected push_infrastructure_exempt with exempt_type=slice_integration_branch"
                )

    def test_synthetic_slice_branch_skips_role_path_allowlist(self, client):
        """Role-based path-allowlist check is bypassed for synthetic slice pushes (#2372).

        ``create_slice_integration_branch`` pushes
        ``parent:refs/heads/egg/<base>/slice-N`` with ``agent_role="coder"``.
        ``get_changed_files_in_push`` falls back to a ``main``-based diff
        when the target ref doesn't exist yet, surfacing every file
        modified on the parent branch's history (drafts, contracts,
        brc-history) — none of which ``coder`` can write.  The role
        check at gateway/gateway.py must skip this branch-creation push
        the same way the anchor/phase/agent-restriction checks already
        do, otherwise a logical no-op push gets falsely blocked.

        After #2489 the role check lives in the attribution-aware block
        and is gated on ``not is_infrastructure_push`` —
        ``get_changed_files_in_push`` is the single observable that
        proves the gate fired.  Asserting it was never called pins the
        infrastructure-bypass behavior end-to-end (the assertion would
        also have caught the legacy duplicate ``check_file_restrictions``
        if it had remained, since that path also consumed
        ``changed_files``).
        """
        session = _make_session(synthetic=True, assigned_branch="egg/issue-2261-v3/slice-2")
        patches = _push_context(session)
        forbidden_files = [
            ".egg-state/brc-history/issue-2261-v3-2026-04-30.jsonl",
            ".egg-state/contracts/issue-2261-v3.json",
            ".egg-state/drafts/issue-2261-v3-plan.md",
        ]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            # Override patches[5] (get_changed_files_in_push) so the
            # attribution-aware role check would 403 if it ran.
            patch.object(
                gateway,
                "get_changed_files_in_push",
                MagicMock(return_value=(forbidden_files, None)),
            ) as mock_changed_files,
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="egg/issue-2261-v3:refs/heads/egg/issue-2261-v3/slice-2",
            )
            assert response.status_code == 200, (
                f"Synthetic slice integration push must bypass the role-based "
                f"path allowlist (#2372). Got {response.status_code}: {response.data!r}"
            )
            mock_changed_files.assert_not_called()

    def test_role_path_allowlist_still_enforced_for_non_infrastructure_pushes(self, client):
        """Regression guard: the gate added in #2372 must NOT weaken the role
        check for ordinary (non-infrastructure) pushes.

        Use a non-pipeline session so the pipeline-session and push-target
        enforcers don't fire first; the role check should still 403 with the
        canonical attribution-aware ``restricted_path_modified`` rejection
        (#2039) — replaces the legacy ``push_denied_protected_files`` event,
        which fired from a duplicate naive check the gateway no longer runs
        (#2489).  When attribution lookup returns no commits the handler
        fails closed and treats every file in the diff as own-authored, so
        the 403 still fires for restricted paths.
        """
        session = _make_session(
            role="coder",
            pipeline_id=None,
            assigned_branch="egg/some-branch",
            synthetic=False,
        )
        patches = _push_context(session)
        forbidden_files = [".egg-state/contracts/some.json"]
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patch.object(
                gateway,
                "get_changed_files_in_push",
                MagicMock(return_value=(forbidden_files, None)),
            ),
            patches[6],
        ):
            response = _do_push(client, refspec="egg/some-branch")
            assert response.status_code == 403, (
                f"Non-infrastructure pushes must still hit the role path-allowlist "
                f"check. Got {response.status_code}: {response.data!r}"
            )
            body = json.loads(response.data)
            data = body.get("data") or {}
            assert data.get("error") == "restricted_path_modified", body
            assert data.get("role") == "coder", body
            assert ".egg-state/contracts/some.json" in (data.get("blocked_paths") or []), body


class TestContextBranchExemption:
    """Context-branch creation exemption (#2548).

    Mirror of :class:`TestSliceIntegrationBranchExemption` but for the
    new ``egg/<base>/context`` shape.  The orchestrator's
    ``create_context_branch`` registers a synthetic, launcher-authed
    session and pushes ``base:refs/heads/egg/<id>/context`` so the doc-
    only context PR has a target branch before any agent runs.  That
    push is orchestrator infrastructure and must bypass the #2028
    pipeline-session block the same way slice integration pushes do.
    """

    def test_synthetic_session_context_branch_push_allowed(self, client):
        """Synthetic-session push to ``egg/issue-N/context`` is allowed."""
        session = _make_session(
            synthetic=True,
            pipeline_id="issue-2548",
            assigned_branch="egg/issue-2548/context",
        )
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="main:refs/heads/egg/issue-2548/context",
            )
            assert response.status_code == 200, (
                f"Expected 200 for synthetic context branch push, "
                f"got {response.status_code}: {response.data!r}"
            )

    def test_non_synthetic_session_context_branch_push_blocked(self, client):
        """Agent (non-synthetic) push to a context branch is still blocked.

        Same trust gate as the slice integration branch exemption — the
        regex alone is never enough; the synthetic flag must be set, and
        only the launcher can set it.
        """
        session = _make_session(
            synthetic=False,
            pipeline_id="issue-2548",
            assigned_branch="egg/issue-2548/context",
        )
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="main:refs/heads/egg/issue-2548/context",
            )
            assert response.status_code == 403, (
                "Non-synthetic session push to context branch must still "
                "be blocked by pipeline-session enforcement"
            )

    def test_synthetic_session_qualified_context_branch_push_allowed(self, client):
        """Qualifier-suffixed pipelines — ``egg/issue-N-v3/context`` — pass."""
        session = _make_session(
            synthetic=True,
            pipeline_id="issue-2474-v2",
            assigned_branch="egg/issue-2474-v2/context",
        )
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="main:refs/heads/egg/issue-2474-v2/context",
            )
            assert response.status_code == 200

    def test_synthetic_session_context_multi_segment_blocked(self, client):
        """Multi-segment shapes (``egg/foo/bar/context``) are not produced
        by the orchestrator and the regex MUST reject them — same shape
        constraint as the slice integration branch regex."""
        session = _make_session(
            synthetic=True,
            pipeline_id="issue-2548",
            assigned_branch="egg/foo/bar/context",
        )
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            response = _do_push(
                client,
                refspec="main:refs/heads/egg/foo/bar/context",
            )
            assert response.status_code == 403

    def test_audit_event_records_context_branch_exempt_type(self, client):
        """Context-branch pushes emit ``push_infrastructure_exempt`` with
        ``exempt_type="context_branch"`` (distinct from
        ``slice_integration_branch``) so SIEM filters keying on
        ``exempt_type`` can tell them apart (#2548 review)."""
        session = _make_session(
            synthetic=True,
            pipeline_id="issue-2548",
            assigned_branch="egg/issue-2548/context",
        )
        patches = _push_context(session)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            with patch.object(gateway, "audit_log") as mock_audit:
                response = _do_push(
                    client,
                    refspec="main:refs/heads/egg/issue-2548/context",
                )
                assert response.status_code == 200
                events = [
                    (call.args[0] if call.args else None, call.kwargs.get("details") or {})
                    for call in mock_audit.call_args_list
                ]
                # The orchestrator-specific audit event still fires for context pushes,
                # carrying a context-branch reason in its details.
                slice_events = [e for e in events if e[0] == "push_slice_integration_exempt"]
                assert slice_events, (
                    f"Expected push_slice_integration_exempt event, got: {[e[0] for e in events]}"
                )
                assert any("context branch" in (e[1].get("reason") or "") for e in slice_events), (
                    "push_slice_integration_exempt detail must identify context-branch pushes"
                )
                # The generic infra exemption event MUST use the
                # context_branch exempt_type — this is the regression
                # the test pins.
                infra_events = [
                    e
                    for e in events
                    if e[0] == "push_infrastructure_exempt"
                    and e[1].get("exempt_type") == "context_branch"
                ]
                assert infra_events, (
                    "Expected push_infrastructure_exempt with exempt_type=context_branch; "
                    f"got: {[(e[0], e[1].get('exempt_type')) for e in events]}"
                )

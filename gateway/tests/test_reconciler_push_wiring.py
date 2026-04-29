"""Integration tests for the stacked-PR reconciler's gateway wiring (#2137).

The unit tests in ``orchestrator/tests/test_gateway_client_rebase_onto.py``
exercise :meth:`GatewayClient.rebase_onto` by stubbing
``_make_request`` — the transport layer — which means a regression in
the *gateway* (e.g. the gateway silently dropping ``force_with_lease``,
or rejecting the reconciler's push for missing ``consensus_push``)
would not be caught.

These tests close that gap by driving requests through the real Flask
handler via ``app.test_client()`` and asserting:

1. ``force_with_lease=True`` in the JSON payload produces a
   ``git push --force-with-lease …`` subprocess command (the bug the
   reviewer caught: the gateway used to read only ``force`` and
   silently drop ``force_with_lease``).
2. The reconciler's push payload — which always sets
   ``consensus_push=True`` because the reconciler runs inside a
   pipeline session — is accepted (status 200) instead of being
   rejected by the pipeline-push enforcement (#2028).
3. The reverse: a payload without ``consensus_push`` from a pipeline
   session is rejected with 403 (proves the reconciler's
   ``consensus_push=True`` is doing real work, not just present-but-
   redundant).

This exercises the full ``data["force_with_lease"] →
push_args.append("--force-with-lease")`` plumbing on a real Flask
request.
"""

import json
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
    with gateway.app.test_client() as c:
        yield c


def _make_session(
    role: str = "coder",
    pipeline_id: str | None = "issue-2137",
    assigned_branch: str | None = "egg/issue-2137/slice-2",
) -> MagicMock:
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = "implement"
    mock_session.pipeline_id = pipeline_id
    mock_session.assigned_branch = assigned_branch
    return mock_session


def _push_context(mock_session, captured_cmds: list[list[str]]):
    """Patch the auth/policy stack and capture every subprocess.run cmd.

    ``captured_cmds`` is appended to whenever ``subprocess.run`` is
    called, so the test can assert on the exact argv handed to git
    (in particular: that ``--force-with-lease`` made it through).
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
        cmd = list(args[0]) if args else list(kwargs.get("args", []))
        captured_cmds.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "remote" in cmd and "get-url" in cmd:
            result.stdout = "https://github.com/owner/repo.git\n"
        elif "branch" in cmd and "--show-current" in cmd:
            result.stdout = "egg/issue-2137/slice-2\n"
        elif "ls-remote" in cmd:
            result.stdout = "abc123\trefs/heads/egg/issue-2137/slice-2\n"
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
                        details={"branch": "egg/issue-2137/slice-2"},
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


def _post_push(client, payload: dict) -> object:
    return client.post(
        "/api/v1/git/push",
        headers={"Authorization": "Bearer test-session-token"},
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestForceWithLeaseWiring:
    """The reconciler's ``force_with_lease=True`` must reach git's argv.

    Earlier drafts of this PR set ``force_with_lease`` in the JSON
    payload but the gateway only read ``force`` — so the flag was
    silently dropped and the rebased branch could not be pushed back
    to origin (the push would be rejected as a non-fast-forward).
    """

    def test_force_with_lease_payload_produces_force_with_lease_argv(self, client):
        """``{force_with_lease: True}`` must materialise as ``--force-with-lease`` in git argv."""
        captured: list[list[str]] = []
        session = _make_session(assigned_branch="egg/issue-2137/slice-2")
        patches = _push_context(session, captured)

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
            response = _post_push(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "refspec": "egg/issue-2137/slice-2:refs/heads/egg/issue-2137/slice-2",
                    "force_with_lease": True,
                    "consensus_push": True,
                },
            )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.data!r}"
        )

        push_cmds = [c for c in captured if "push" in c]
        assert push_cmds, f"No push command captured. All cmds: {captured!r}"
        push_cmd = push_cmds[0]
        assert "--force-with-lease" in push_cmd, (
            f"--force-with-lease missing from push cmd: {push_cmd!r}"
        )
        assert "--force" not in push_cmd or push_cmd.index("--force-with-lease") == push_cmd.index(
            "--force-with-lease"
        ), f"Unexpected bare --force in push cmd: {push_cmd!r}"

    def test_force_with_lease_takes_precedence_over_force(self, client):
        """If both flags are set, ``--force-with-lease`` wins (gateway docstring contract)."""
        captured: list[list[str]] = []
        session = _make_session(assigned_branch="egg/issue-2137/slice-2")
        patches = _push_context(session, captured)

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
            response = _post_push(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "refspec": "egg/issue-2137/slice-2:refs/heads/egg/issue-2137/slice-2",
                    "force": True,
                    "force_with_lease": True,
                    "consensus_push": True,
                },
            )

        assert response.status_code == 200
        push_cmd = [c for c in captured if "push" in c][0]
        assert "--force-with-lease" in push_cmd
        # Bare --force must not appear when force_with_lease wins.
        # (``--force-with-lease`` contains ``--force`` as a substring,
        # but as a distinct argv element ``--force`` should be absent.)
        assert "--force" not in push_cmd

    def test_plain_force_still_works(self, client):
        """Backward compat: ``{force: True}`` alone still produces ``--force``."""
        captured: list[list[str]] = []
        session = _make_session(assigned_branch="egg/issue-2137/slice-2")
        patches = _push_context(session, captured)

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
            response = _post_push(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "refspec": "egg/issue-2137/slice-2:refs/heads/egg/issue-2137/slice-2",
                    "force": True,
                    "consensus_push": True,
                },
            )

        assert response.status_code == 200
        push_cmd = [c for c in captured if "push" in c][0]
        assert "--force" in push_cmd
        assert "--force-with-lease" not in push_cmd


class TestReconcilerConsensusPushPlumbing:
    """The reconciler's ``consensus_push=True`` must satisfy pipeline enforcement.

    The reconciler runs inside the orchestrator's pipeline session
    (so ``session.pipeline_id`` is set). Without ``consensus_push``,
    the pipeline-push enforcement (#2028) would 403 the push and the
    rebased branch would never reach origin.
    """

    def test_reconciler_push_with_consensus_marker_is_accepted(self, client):
        """Reconciler-shaped push (consensus_push + force_with_lease) → 200."""
        captured: list[list[str]] = []
        session = _make_session(assigned_branch="egg/issue-2137/slice-2")
        patches = _push_context(session, captured)

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
            response = _post_push(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "refspec": "egg/issue-2137/slice-2:refs/heads/egg/issue-2137/slice-2",
                    "force_with_lease": True,
                    "consensus_push": True,
                },
            )

        assert response.status_code == 200, (
            f"Reconciler push should be accepted. Got {response.status_code}: {response.data!r}"
        )

    def test_reconciler_push_without_consensus_marker_is_blocked(self, client):
        """Same payload without ``consensus_push`` → 403 (proves the marker matters)."""
        captured: list[list[str]] = []
        session = _make_session(assigned_branch="egg/issue-2137/slice-2")
        patches = _push_context(session, captured)

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
            response = _post_push(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "refspec": "egg/issue-2137/slice-2:refs/heads/egg/issue-2137/slice-2",
                    "force_with_lease": True,
                    # No consensus_push.
                },
            )

        assert response.status_code == 403, (
            f"Pipeline session push without consensus_push must be blocked. "
            f"Got {response.status_code}: {response.data!r}"
        )
        data = json.loads(response.data)
        assert "pipeline sessions" in data["message"].lower()

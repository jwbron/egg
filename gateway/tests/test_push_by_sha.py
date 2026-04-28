"""Tests for the detached-HEAD-tolerant push-by-SHA path (#2200).

When a BRC producer agent ends up on detached HEAD (e.g. after
``git rebase origin/<assigned>`` advances HEAD without reattaching the
local branch), it cannot read ``git branch --show-current`` and the
existing helper used to bail with ``"could not determine current
branch for push"`` — trapping the agent with no way to publish its
proposal.  The fix lets the helper send ``commit_sha`` instead of
``refspec``; the gateway derives the refspec server-side from the
session's assigned branch.

Covers:
- ``commit_sha`` push without ``consensus_push=true`` is rejected (400).
- ``commit_sha`` push with malformed SHA is rejected (400).
- ``commit_sha`` push without an assigned branch is rejected (400).
- ``commit_sha`` push with a valid SHA + assigned branch builds the
  ``<sha>:refs/heads/<assigned>`` refspec server-side and reaches the
  push subprocess unchanged.
- Existing refspec-based pushes are unaffected (commit_sha is optional).
"""

from __future__ import annotations

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
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_session(
    pipeline_id: str | None = "issue-2200",
    assigned_branch: str | None = "egg/issue-2200",
) -> MagicMock:
    s = MagicMock()
    s.mode = "public"
    s.container_id = "issue-2200-coder"
    s.expires_at = None
    s.agent_role = "coder"
    s.phase = "implement"
    s.pipeline_id = pipeline_id
    s.assigned_branch = assigned_branch
    return s


def _push_context(mock_session: MagicMock, captured_cmd: list[list[str]]):
    """Mock everything around the push handler so we can observe the final ``git push``."""
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
        elif "ls-remote" in cmd:
            result.stdout = ""
        elif "push" in cmd:
            captured_cmd.append(list(cmd))
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
                        details={"branch": "egg/issue-2200"},
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


def _post(client, payload: dict) -> object:
    return client.post(
        "/api/v1/git/push",
        headers={"Authorization": "Bearer test-session-token"},
        data=json.dumps(payload),
        content_type="application/json",
    )


class TestCommitShaPush:
    def test_commit_sha_without_consensus_marker_rejected(self, client):
        """commit_sha push must require consensus_push=true (defense in depth)."""
        session = _make_session()
        captured: list[list[str]] = []
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
            response = _post(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "commit_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                },
            )
        assert response.status_code == 400
        body = json.loads(response.data)
        assert "consensus_push" in body.get("message", "")

    def test_commit_sha_invalid_format_rejected(self, client):
        """Non-hex / wrong-length commit_sha must be rejected before any subprocess work."""
        session = _make_session()
        captured: list[list[str]] = []
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
            response = _post(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "commit_sha": "not-a-sha",
                    "consensus_push": True,
                },
            )
        assert response.status_code == 400
        body = json.loads(response.data)
        assert "commit_sha" in body.get("message", "")

    def test_commit_sha_without_assigned_branch_rejected(self, client):
        """A session without assigned_branch cannot use the SHA path (no target)."""
        session = _make_session(pipeline_id=None, assigned_branch=None)
        captured: list[list[str]] = []
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
            response = _post(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "commit_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                    "consensus_push": True,
                },
            )
        assert response.status_code == 400
        body = json.loads(response.data)
        assert "assigned branch" in body.get("message", "").lower()

    def test_commit_sha_builds_refspec_server_side(self, client):
        """A valid commit_sha + assigned_branch produces ``<sha>:refs/heads/<assigned>``."""
        session = _make_session()
        captured: list[list[str]] = []
        patches = _push_context(session, captured)
        sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
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
            response = _post(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "commit_sha": sha,
                    "consensus_push": True,
                },
            )
        assert response.status_code == 200, response.data
        # Find the actual `git push` invocation in the captured commands.
        push_invocations = [c for c in captured if "push" in c and "--no-verify" in c]
        assert len(push_invocations) == 1, push_invocations
        cmd = push_invocations[0]
        # The constructed refspec must be <sha>:refs/heads/<assigned>.
        expected_refspec = f"{sha}:refs/heads/egg/issue-2200"
        assert expected_refspec in cmd, (cmd, expected_refspec)

    def test_refspec_path_unaffected(self, client):
        """Existing refspec-based pushes still work — commit_sha is purely additive."""
        session = _make_session()
        captured: list[list[str]] = []
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
            response = _post(
                client,
                {
                    "repo_path": "/home/egg/repos/test-repo",
                    "remote": "origin",
                    "refspec": "egg/issue-2200",
                    "consensus_push": True,
                },
            )
        assert response.status_code == 200, response.data
        push_invocations = [c for c in captured if "push" in c and "--no-verify" in c]
        assert len(push_invocations) == 1
        # No SHA-style refspec should appear; the original refspec passes through.
        cmd = push_invocations[0]
        assert "egg/issue-2200" in cmd
        assert not any(":refs/heads/" in arg for arg in cmd)

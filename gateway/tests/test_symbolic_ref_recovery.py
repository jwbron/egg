"""Tests for the symbolic-ref reattach primitive (issue #2200).

``git symbolic-ref HEAD <ref>`` is the canonical low-level reattach
primitive: it rewrites HEAD's symref without changing branch contents,
so it does not need the broader ``switch``/``checkout`` allowance that
pipeline sessions intentionally deny.

Covers:
- ``symbolic-ref HEAD refs/heads/<assigned>`` is allowed.
- ``symbolic-ref HEAD refs/heads/egg/<container_id>/work`` (the per-role
  local work branch) is allowed.
- Other ``symbolic-ref`` targets are rejected with 403.
- Sessions without an assigned branch cannot use ``symbolic-ref``.
- Read forms (one-arg) are rejected.
- Disallowed flags (``-d``, ``--short``, ``-q``) are rejected by the
  allowlist.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import auth
import pytest
import session_manager as session_manager_module
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_session(assigned_branch, container_id="issue-2200-coder"):
    s = MagicMock()
    s.mode = "private"
    s.container_id = container_id
    s.expires_at = None
    s.phase = "implement"
    s.agent_role = "coder"
    s.assigned_branch = assigned_branch
    s.pipeline_id = "issue-2200" if assigned_branch else None
    s.last_branch = assigned_branch
    s.checkpoint_repo = None
    s.last_repo_path = None
    return s


def _setup_auth(session):
    mock_result = SessionValidationResult(valid=True, session=session)
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
    current_sm = sys.modules.get("session_manager", session_manager_module)
    return (
        {"Authorization": "Bearer test-session-token"},
        mock_result,
        mock_policy_result,
        current_sm,
    )


def _execute(client, headers, args, container_id="issue-2200-coder"):
    return client.post(
        "/api/v1/git/execute",
        json={
            "repo_path": "/home/egg/repos/myrepo",
            "operation": "symbolic-ref",
            "args": args,
            "container_id": container_id,
        },
        headers=headers,
    )


class TestSymbolicRefScope:
    @pytest.fixture
    def auth_with_branch(self):
        return _setup_auth(_make_session("egg/issue-2200"))

    @pytest.fixture
    def auth_without_branch(self):
        return _setup_auth(_make_session(None))

    def test_symbolic_ref_to_assigned_branch_allowed(self, client, auth_with_branch):
        """symbolic-ref HEAD refs/heads/<assigned> reaches subprocess."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            response = _execute(
                client,
                headers,
                ["HEAD", "refs/heads/egg/issue-2200"],
            )
            assert response.status_code == 200, response.data

    def test_symbolic_ref_to_local_work_branch_allowed(self, client, auth_with_branch):
        """The per-role local work branch (egg/<container_id>/work) is allowed."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            response = _execute(
                client,
                headers,
                ["HEAD", "refs/heads/egg/issue-2200-coder/work"],
            )
            assert response.status_code == 200, response.data

    def test_symbolic_ref_to_main_blocked(self, client, auth_with_branch):
        """Targets outside the agent's assigned/local pair are rejected with 403."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(client, headers, ["HEAD", "refs/heads/main"])
            assert response.status_code == 403
            data = json.loads(response.data)
            msg = data.get("message", "")
            assert "refs/heads/main" in msg
            assert "egg/issue-2200" in msg

    def test_symbolic_ref_non_head_source_blocked(self, client, auth_with_branch):
        """The source must be literal HEAD; retargeting other symrefs is denied."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(
                client,
                headers,
                ["FETCH_HEAD", "refs/heads/egg/issue-2200"],
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "Only HEAD" in data.get("message", "")

    def test_symbolic_ref_read_form_blocked(self, client, auth_with_branch):
        """The read form (`symbolic-ref HEAD`) — one positional — is rejected.

        The agent already has ``git rev-parse --abbrev-ref HEAD`` for that.
        Allowing the read form would just enlarge the surface area without
        improving recovery flow.
        """
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(client, headers, ["HEAD"])
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "symbolic-ref HEAD" in data.get("message", "")

    def test_symbolic_ref_with_no_assigned_branch_blocked(self, client, auth_without_branch):
        """Sessions without assigned_branch cannot use symbolic-ref at all."""
        headers, mock_result, mock_policy, current_sm = auth_without_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(client, headers, ["HEAD", "refs/heads/main"])
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "pipeline session" in data.get("message", "").lower()

    def test_symbolic_ref_delete_flag_blocked_by_allowlist(self, client, auth_with_branch):
        """-d (delete symref) is not in allowed_flags and is rejected during arg validation."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(client, headers, ["-d", "HEAD"])
            assert response.status_code == 400
            data = json.loads(response.data)
            assert "-d" in data.get("message", "")

    def test_symbolic_ref_short_flag_blocked_by_allowlist(self, client, auth_with_branch):
        """--short (print short form) is not in allowed_flags."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = _execute(client, headers, ["--short", "HEAD"])
            assert response.status_code == 400
            data = json.loads(response.data)
            assert "--short" in data.get("message", "")

    def test_symbolic_ref_request_container_id_does_not_widen_scope(self, client):
        """An attacker-supplied ``container_id`` in the request body cannot
        widen the symbolic-ref scope past the session's own per-role branch.

        Defense in depth: the allowlist is computed from
        ``session.container_id`` (canonical, set by the orchestrator at
        session registration), not ``data.get("container_id")``.  Mismatched
        values reach the same gate as any other unrelated target — 403.
        """
        session = _make_session("egg/issue-2200", container_id="issue-2200-coder")
        headers, mock_result, mock_policy, current_sm = _setup_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            # Request claims a different container_id than the session holds.
            response = _execute(
                client,
                headers,
                ["HEAD", "refs/heads/egg/other-pipeline-coder/work"],
                container_id="other-pipeline-coder",
            )
            assert response.status_code == 403, response.data
            data = json.loads(response.data)
            msg = data.get("message", "")
            # The attempted target must appear in the denial; the allowed set
            # must reflect the session's container_id, not the request's.
            assert "refs/heads/egg/other-pipeline-coder/work" in msg
            assert "refs/heads/egg/issue-2200-coder/work" in msg

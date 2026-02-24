"""Tests for Session.assigned_branch field and push-target enforcement.

Validates that:
- assigned_branch field is added to Session and serializes correctly
- Pipeline sessions with assigned_branch block branch switching
- Non-pipeline sessions are not affected
- Register session populates assigned_branch for pipeline sessions
- Push-target enforcement blocks pipeline agents from pushing to wrong branch
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import auth
import pytest
import session_manager as session_manager_module
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import (
    Session,
    SessionManager,
    SessionValidationResult,
    _hash_token,
)

import gateway
import policy


class TestSessionAssignedBranchField:
    """Tests for the assigned_branch field on Session."""

    def test_defaults_to_none(self):
        """assigned_branch defaults to None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.assigned_branch is None

    def test_can_set_assigned_branch(self):
        """assigned_branch can be set to a branch name."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            assigned_branch="egg/c1/work",
        )
        assert session.assigned_branch == "egg/c1/work"

    def test_to_dict_includes_assigned_branch(self):
        """to_dict_for_persistence includes assigned_branch when set."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            assigned_branch="egg/c1/work",
        )
        d = session.to_dict_for_persistence()
        assert d["assigned_branch"] == "egg/c1/work"

    def test_to_dict_excludes_none_assigned_branch(self):
        """to_dict_for_persistence omits assigned_branch when None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        d = session.to_dict_for_persistence()
        assert "assigned_branch" not in d

    def test_from_persistence_with_assigned_branch(self):
        """from_persistence restores assigned_branch."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": _hash_token("test"),
            "container_id": "c1",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "assigned_branch": "egg/c1/work",
        }
        session = Session.from_persistence(data)
        assert session.assigned_branch == "egg/c1/work"

    def test_from_persistence_without_assigned_branch(self):
        """from_persistence defaults assigned_branch to None for old data."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": _hash_token("test"),
            "container_id": "c1",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        session = Session.from_persistence(data)
        assert session.assigned_branch is None

    def test_roundtrip_with_assigned_branch(self, tmp_path):
        """assigned_branch survives save/load cycle via SessionManager."""
        persistence_file = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persistence_file)
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            pipeline_id="issue-42",
            branch="egg/c1/work",
        )
        assert session.assigned_branch == "egg/c1/work"

        # Reload from disk
        manager2 = SessionManager(persistence_file=persistence_file)
        result = manager2.validate_session(token, source_ip="172.18.0.5")
        assert result.session is not None
        assert result.session.assigned_branch == "egg/c1/work"


class TestRegisterSessionAssignedBranch:
    """Tests for register_session populating assigned_branch."""

    def test_pipeline_session_with_branch_gets_assigned_branch(self, tmp_path):
        """Pipeline sessions with branch get assigned_branch set."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            pipeline_id="issue-42",
            branch="egg/c1/work",
        )
        assert session.assigned_branch == "egg/c1/work"

    def test_non_pipeline_session_no_assigned_branch(self, tmp_path):
        """Non-pipeline sessions do not get assigned_branch."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            branch="egg/feature",
        )
        assert session.assigned_branch is None

    def test_pipeline_without_branch_no_assigned_branch(self, tmp_path):
        """Pipeline session without branch doesn't set assigned_branch."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            pipeline_id="issue-42",
        )
        assert session.assigned_branch is None


# ---------------------------------------------------------------------------
# Push-target enforcement tests (TASK-2-2)
# ---------------------------------------------------------------------------

def _make_pipeline_session(
    assigned_branch: str | None = "egg/issue-42",
    pipeline_id: str | None = "issue-42",
    phase: str | None = "implement",
    agent_role: str | None = "coder",
):
    """Create a mock session for push-target enforcement tests."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.phase = phase
    mock_session.agent_role = agent_role
    mock_session.assigned_branch = assigned_branch
    mock_session.pipeline_id = pipeline_id
    mock_session.last_branch = assigned_branch
    mock_session.checkpoint_repo = None
    mock_session.last_repo_path = None
    mock_session.complexity_tier = None
    return mock_session


def _setup_push_auth(session):
    """Set up auth mocking for a push test. Returns (headers, mock_result, mock_policy, sm)."""
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


def _mock_subprocess_for_push():
    """Return a side_effect for subprocess.run that handles git_push subprocess calls."""

    def side_effect(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""

        if any("remote" in str(c) for c in cmd) and any("get-url" in str(c) for c in cmd):
            result.stdout = "https://github.com/owner/repo.git\n"
        elif any("push" in str(c) for c in cmd):
            result.stdout = "Everything up-to-date\n"
        elif any("diff" in str(c) for c in cmd):
            result.stdout = ""
        elif any("rev-list" in str(c) for c in cmd):
            result.stdout = ""
        else:
            result.stdout = ""
        return result

    return side_effect


@pytest.fixture
def push_client():
    """Flask test client for push tests."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


@pytest.fixture
def mock_push_policy():
    """Mock policy engine allowing all branch ownership checks."""
    with patch.object(gateway, "get_policy_engine") as mock_get:
        engine = MagicMock()
        engine.check_branch_ownership.return_value = policy.PolicyResult(
            allowed=True,
            reason="Test allowed",
            details={},
        )
        mock_get.return_value = engine
        yield engine


def _do_push(client, headers, refspec="egg/issue-42"):
    """Send a push request."""
    return client.post(
        "/api/v1/git/push",
        json={
            "repo_path": "/home/egg/repos/test-repo",
            "remote": "origin",
            "refspec": refspec,
        },
        headers=headers,
    )


class TestPushTargetEnforcement:
    """Push-target enforcement: pipeline sessions must push to assigned branch."""

    def test_pipeline_push_to_assigned_branch_succeeds(self, push_client, mock_push_policy):
        """(a) Pipeline session pushing to assigned branch should succeed."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
        ):
            response = _do_push(push_client, headers, refspec="egg/issue-42")
            assert response.status_code == 200

    def test_pipeline_push_to_different_branch_returns_403(self, push_client, mock_push_policy):
        """(b) Pipeline session pushing to a different branch should be rejected."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
        ):
            response = _do_push(push_client, headers, refspec="egg/wrong-branch")
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "egg/issue-42" in data["message"]
            assert data["data"]["attempted_branch"] == "egg/wrong-branch"

    def test_refspec_local_remote_matching_assigned_succeeds(self, push_client, mock_push_policy):
        """(c) local:remote refspec where remote matches assigned branch succeeds."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
        ):
            response = _do_push(push_client, headers, refspec="HEAD:egg/issue-42")
            assert response.status_code == 200

    def test_refspec_local_remote_mismatched_returns_403(self, push_client, mock_push_policy):
        """(d) local:remote refspec where remote does NOT match returns 403."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
        ):
            response = _do_push(push_client, headers, refspec="HEAD:refs/heads/egg/other-branch")
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "egg/issue-42" in data["message"]

    def test_non_pipeline_session_push_any_branch_unaffected(self, push_client, mock_push_policy):
        """(e) Non-pipeline session can push to any branch."""
        session = _make_pipeline_session(pipeline_id=None, assigned_branch=None)
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
        ):
            response = _do_push(push_client, headers, refspec="egg/any-branch")
            assert response.status_code == 200

    def test_pipeline_session_no_assigned_branch_skips_check(self, push_client, mock_push_policy):
        """(f) Session with pipeline_id but no assigned_branch skips enforcement."""
        session = _make_pipeline_session(pipeline_id="issue-42", assigned_branch=None)
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
        ):
            response = _do_push(push_client, headers, refspec="egg/different-branch")
            assert response.status_code == 200

    def test_killswitch_disables_enforcement(self, push_client, mock_push_policy):
        """(g) PUSH_TARGET_ENFORCEMENT=false disables the check."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
            patch.dict(os.environ, {"PUSH_TARGET_ENFORCEMENT": "false"}),
        ):
            # Push to wrong branch should succeed when killswitch is off
            response = _do_push(push_client, headers, refspec="egg/wrong-branch")
            assert response.status_code == 200

    def test_auto_commit_session_without_assigned_branch_succeeds(self, push_client, mock_push_policy):
        """(h) Auto-commit/failsafe session (no assigned_branch) push succeeds."""
        # push_worktree_branch creates temp sessions without a branch param,
        # so assigned_branch is None — enforcement is skipped.
        session = _make_pipeline_session(
            pipeline_id="issue-42-failsafe-push",
            assigned_branch=None,
        )
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
        ):
            response = _do_push(push_client, headers, refspec="egg/issue-42")
            assert response.status_code == 200

    @pytest.mark.parametrize("env_value", ["0", "no"])
    def test_killswitch_values_0_and_no(self, push_client, mock_push_policy, env_value):
        """(i) PUSH_TARGET_ENFORCEMENT='0' and 'no' also disable the check."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
            patch.dict(os.environ, {"PUSH_TARGET_ENFORCEMENT": env_value}),
        ):
            response = _do_push(push_client, headers, refspec="egg/wrong-branch")
            assert response.status_code == 200

    def test_push_denial_logs_audit_event(self, push_client, mock_push_policy):
        """(j) Push denied due to branch mismatch logs audit event."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log") as mock_audit,
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
        ):
            response = _do_push(push_client, headers, refspec="egg/wrong-branch")
            assert response.status_code == 403
            # Verify audit_log was called with the push_denied_wrong_branch event
            mock_audit.assert_called()
            call_args = mock_audit.call_args
            assert call_args[0][0] == "push_denied_wrong_branch"
            assert call_args[1]["success"] is False
            assert call_args[1]["details"]["assigned_branch"] == "egg/issue-42"
            assert call_args[1]["details"]["branch"] == "egg/wrong-branch"

    def test_checkpoint_push_bypasses_enforcement(self, push_client, mock_push_policy):
        """(k) Checkpoint push skips push-target enforcement entirely."""
        session = _make_pipeline_session(assigned_branch="egg/issue-42")
        # Set checkpoint_repo so the push is recognized as a checkpoint push
        session.checkpoint_repo = "owner/repo"
        headers, mock_result, mock_policy, current_sm = _setup_push_auth(session)

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/tmp/repo"),
            patch.object(gateway, "resolve_remote_url", return_value=("https://github.com/owner/repo.git", None)),
            patch.object(gateway, "get_auth_mode", return_value="local"),
            patch.object(gateway, "get_token_for_repo", return_value=("ghp_test", "app", None)),
            patch.object(gateway, "get_authenticated_remote_target", return_value="https://x-access-token:ghp_test@github.com/owner/repo.git"),
            patch.object(gateway, "get_changed_files_in_push", return_value=([], None)),
            patch("subprocess.run", side_effect=_mock_subprocess_for_push()),
        ):
            # Push to checkpoint branch (egg/checkpoints/v2) — different from assigned
            response = _do_push(push_client, headers, refspec="egg/checkpoints/v2")
            assert response.status_code == 200

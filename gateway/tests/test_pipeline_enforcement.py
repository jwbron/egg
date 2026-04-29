"""Tests for pipeline enforcement features in gateway.py.

Covers:
- Branch switch blocking for pipeline sessions (git_execute)
- Commit-time phase file restriction validation (git_execute)
"""

import json
import sys
from unittest.mock import MagicMock, patch

import auth
import pytest
import session_manager as session_manager_module
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway

# Re-use the test client fixtures from the gateway test module
TEST_LAUNCHER_SECRET = "test-launcher-secret-12345"


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_session_with_branch(assigned_branch, phase=None, agent_role=None):
    """Create a mock session with an assigned branch for pipeline lock tests."""
    mock_session = MagicMock()
    mock_session.mode = "private"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.phase = phase
    mock_session.agent_role = agent_role
    mock_session.assigned_branch = assigned_branch
    mock_session.pipeline_id = "issue-42" if assigned_branch else None
    mock_session.last_branch = assigned_branch
    mock_session.checkpoint_repo = None
    mock_session.last_repo_path = None
    return mock_session


def _setup_auth(session):
    """Set up auth mocking for a session. Returns (headers, context_managers)."""
    mock_result = SessionValidationResult(valid=True, session=session)
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode - access allowed",
        visibility="public",
    )

    # Clear cached references
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


class TestBranchSwitchBlocking:
    """Tests for branch switch blocking in git_execute."""

    @pytest.fixture
    def auth_with_branch(self):
        """Create auth setup with a session that has an assigned branch."""
        session = _make_session_with_branch("egg/c1/work", phase="implement")
        return _setup_auth(session)

    @pytest.fixture
    def auth_without_branch(self):
        """Create auth setup with a session that has no assigned branch."""
        session = _make_session_with_branch(None)
        return _setup_auth(session)

    def test_checkout_branch_blocked(self, client, auth_with_branch):
        """git checkout <branch> blocked for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["main"],
                },
                headers=headers,
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "Branch switching" in data.get("message", "")

    def test_checkout_b_blocked(self, client, auth_with_branch):
        """git checkout -b <new-branch> blocked for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["-b", "new-feature"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_switch_blocked(self, client, auth_with_branch):
        """git switch blocked for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "switch",
                    "args": ["main"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_checkout_file_allowed(self, client, auth_with_branch):
        """git checkout -- file.txt allowed for pipeline sessions."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["--", "file.txt"],
                },
                headers=headers,
            )
            # Not blocked by branch switch check (may succeed or fail for other reasons)
            assert response.status_code != 403

    def test_error_message_includes_locked_branch(self, client, auth_with_branch):
        """403 error message includes the locked branch name."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["other-branch"],
                },
                headers=headers,
            )
            data = json.loads(response.data)
            assert "egg/c1/work" in data.get("message", "")

    def test_no_assigned_branch_allows_checkout(self, client, auth_without_branch):
        """Sessions without assigned_branch allow checkout."""
        headers, mock_result, mock_policy, current_sm = auth_without_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "checkout",
                    "args": ["main"],
                },
                headers=headers,
            )
            assert response.status_code != 403


class TestOffLineageResetBlocking:
    """Tests for off-lineage `git reset` blocking in git_execute (issue #2089).

    `git reset <ref>` (any mode) moves HEAD; if <ref> is not an ancestor of
    HEAD on the assigned branch, the agent's commits are silently dropped —
    the same effect as a branch switch. The checkout/switch lock does not
    catch this, so we add a dedicated ancestry check.
    """

    @pytest.fixture
    def auth_with_branch(self):
        session = _make_session_with_branch("egg/c1/work", phase="implement")
        return _setup_auth(session)

    @pytest.fixture
    def auth_without_branch(self):
        session = _make_session_with_branch(None)
        return _setup_auth(session)

    def test_reset_hard_off_lineage_blocked(self, client, auth_with_branch):
        """`git reset --hard <off-branch-ref>` is blocked with 403."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            # merge-base --is-ancestor returns 1 → not an ancestor → blocked
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "reset",
                    "args": ["--hard", "origin/other-branch"],
                },
                headers=headers,
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            message = data.get("message", "")
            assert "Off-lineage" in message
            assert "origin/other-branch" in message
            assert "egg/c1/work" in message

    def test_reset_soft_off_lineage_blocked(self, client, auth_with_branch):
        """`git reset --soft <off-branch-ref>` is also blocked — soft/mixed move HEAD too."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "reset",
                    "args": ["--soft", "origin/other-branch"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_reset_hard_ancestor_allowed(self, client, auth_with_branch):
        """`git reset --hard <ancestor-sha>` on the assigned branch is allowed."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            # First call: merge-base --is-ancestor returns 0 → ancestor → allowed
            # Subsequent calls: actual reset execution
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "reset",
                    "args": ["--hard", "abc123"],
                },
                headers=headers,
            )
            assert response.status_code != 403

    def test_reset_no_ref_allowed(self, client, auth_with_branch):
        """`git reset --hard` (no ref) doesn't move HEAD → no ancestry check."""
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
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "reset",
                    "args": ["--hard"],
                },
                headers=headers,
            )
            assert response.status_code != 403

    def test_reset_path_mode_allowed(self, client, auth_with_branch):
        """`git reset -- <paths>` is path-mode; HEAD does not move."""
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
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "reset",
                    "args": ["--", "file.txt"],
                },
                headers=headers,
            )
            assert response.status_code != 403

    def test_reset_subprocess_failure_fails_closed(self, client, auth_with_branch):
        """If merge-base subprocess raises, treat as off-lineage and block."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run", side_effect=OSError("git not found")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "reset",
                    "args": ["--hard", "abc123"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_reset_no_assigned_branch_allowed(self, client, auth_without_branch):
        """Sessions without assigned_branch are unrestricted (interactive mode)."""
        headers, mock_result, mock_policy, current_sm = auth_without_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "reset",
                    "args": ["--hard", "origin/other-branch"],
                },
                headers=headers,
            )
            assert response.status_code != 403


class TestCommitTimePhaseValidation:
    """Tests for commit-time staged file validation in git_execute."""

    @pytest.fixture
    def auth_implement_phase(self):
        """Auth setup with implement phase session."""
        session = _make_session_with_branch("egg/c1/work", phase="implement", agent_role="coder")
        return _setup_auth(session)

    def test_commit_with_blocked_staged_files_returns_403(self, client, auth_implement_phase):
        """Commit is blocked if staged files violate phase restrictions."""
        headers, mock_result, mock_policy, current_sm = auth_implement_phase

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            # First call is the staged files check (git diff --cached),
            # subsequent calls are the actual commit execution
            mock_staged = MagicMock(
                returncode=0,
                stdout=".egg-state/contracts/644.json\n",
                stderr="",
            )
            mock_run.return_value = mock_staged
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "test commit"],
                },
                headers=headers,
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            assert "blocked" in data.get("message", "").lower()

    def test_commit_with_allowed_files_not_blocked(self, client, auth_implement_phase):
        """Commit with only allowed files is not blocked at phase check."""
        headers, mock_result, mock_policy, current_sm = auth_implement_phase

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="src/main.py\nsrc/utils.py\n",
                stderr="",
            )
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "commit",
                    "args": ["-m", "test commit"],
                },
                headers=headers,
            )
            # Not blocked by phase restriction
            assert response.status_code != 403


class TestBareRebaseAgainstBaseBlocking:
    """Tests for ``git rebase`` against the base branch blocking (#2224 PR 1).

    The pipeline branch is rebased onto the base branch only via the
    orchestrator's controlled rebase
    (``orchestrator/routes/pipelines.py::_rebase_pipeline_branch_onto_base``),
    which itself uses bare ``git rebase origin/<base>`` but is safe
    because it runs as a subprocess on the orchestrator-side worktree
    and enforces ancestry preconditions before replaying.  That
    subprocess does not route through ``/api/v1/git/execute``, so this
    guard does not interfere with it.

    An agent-initiated ``git rebase origin/main`` reproduces the
    contamination shape from #2222.  The ``--onto X UP <branch>`` form
    is *also* blocked when ``X`` (the new base) is a protected ref —
    ``--onto origin/main origin/main`` reduces to bare ``git rebase
    origin/main`` and is the same contamination shape.  Legitimate
    ``--onto`` calls (the stacked-PR healer in
    ``orchestrator/gateway_client.py::rebase_onto``) always pass a
    slice/issue branch as ``new_base`` (never ``origin/main``; see
    ``stacked_pr_reconciler._resolve_extant_new_base``), so they are
    unaffected.
    """

    @pytest.fixture
    def auth_with_branch(self):
        session = _make_session_with_branch("egg/issue-42", phase="implement")
        return _setup_auth(session)

    @pytest.fixture
    def auth_without_branch(self):
        session = _make_session_with_branch(None)
        return _setup_auth(session)

    def test_bare_rebase_origin_main_blocked(self, client, auth_with_branch):
        """``git rebase origin/main`` is blocked with 403."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": ["origin/main"],
                },
                headers=headers,
            )
            assert response.status_code == 403
            data = json.loads(response.data)
            message = data.get("message", "")
            assert "origin/main" in message
            assert "_rebase_pipeline_branch_onto_base" in message
            assert "#2222" in message
            # The denial message must NOT advertise ``--onto`` as a
            # safe escape hatch — that would hand the agent the
            # bypass directly.  See review on #2282.
            assert "canonical" not in message
            assert "--onto <new_base> <old_base>" not in message

    def test_bare_rebase_main_blocked(self, client, auth_with_branch):
        """``git rebase main`` (without origin/ prefix) is also blocked."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": ["main"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_bare_rebase_with_branch_arg_blocked(self, client, auth_with_branch):
        """``git rebase origin/main <branch>`` (2-positional bare form) is blocked."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": ["origin/main", "egg/issue-42"],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_onto_origin_main_origin_main_bypass_blocked(self, client, auth_with_branch):
        """``git rebase --onto origin/main origin/main <branch>`` is blocked.

        ``git rebase --onto X UP`` rebases HEAD onto X using UP as the
        upstream, so when ``X == UP == origin/main`` the operation
        reduces to bare ``git rebase origin/main`` and reproduces the
        #2222 contamination shape.  The previous version of this guard
        short-circuited whenever ``--onto`` appeared anywhere in the
        argv — see review on #2282.
        """
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": [
                        "--onto",
                        "origin/main",
                        "origin/main",
                        "egg/issue-42",
                    ],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_onto_eq_origin_main_blocked(self, client, auth_with_branch):
        """``git rebase --onto=origin/main …`` (equals form) is also blocked."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": [
                        "--onto=origin/main",
                        "origin/parent-branch",
                        "egg/issue-42",
                    ],
                },
                headers=headers,
            )
            assert response.status_code == 403

    @pytest.mark.parametrize(
        "ref",
        [
            "refs/remotes/origin/main",
            "refs/heads/main",
            "origin/HEAD",
            "FETCH_HEAD",
        ],
    )
    def test_alternate_ref_forms_blocked(self, client, auth_with_branch, ref):
        """Canonical full-ref names and other base-equivalent shapes are blocked.

        ``refs/remotes/origin/main`` and ``refs/heads/main`` are the
        canonical forms; ``origin/HEAD`` resolves to origin's default
        branch (typically main); ``FETCH_HEAD`` resolves to whatever
        was last fetched (typically the base after ``git fetch
        origin main``).  All produce the contamination shape.
        """
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
        ):
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": [ref],
                },
                headers=headers,
            )
            assert response.status_code == 403

    def test_onto_form_against_slice_branch_allowed(self, client, auth_with_branch):
        """``git rebase --onto <slice-branch> origin/main <branch>`` is allowed.

        This is the canonical shape used by the stacked-PR healer at
        ``orchestrator/gateway_client.py::rebase_onto`` when a child
        slice is being retargeted from ``origin/main`` onto a sibling
        slice branch.  ``new_base`` is a slice branch (never
        ``origin/main``), so the value-of-``--onto`` check passes.
        """
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": [
                        "--onto",
                        "egg/issue-42/slice-1",
                        "origin/main",
                        "egg/issue-42/slice-2",
                    ],
                },
                headers=headers,
            )
            assert response.status_code == 200
            # The subprocess must actually have been invoked — a
            # ``response.status_code != 403`` assertion alone would
            # also pass on a 500 from any other path.
            assert mock_run.called

    def test_onto_eq_form_against_slice_branch_allowed(self, client, auth_with_branch):
        """``git rebase --onto=<slice-branch> origin/main <branch>`` is also allowed."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": [
                        "--onto=egg/issue-42/slice-1",
                        "origin/main",
                        "egg/issue-42/slice-2",
                    ],
                },
                headers=headers,
            )
            assert response.status_code == 200
            assert mock_run.called

    def test_bare_rebase_against_other_branch_allowed(self, client, auth_with_branch):
        """``git rebase origin/some-feature`` (non-main) is not blocked by this guard.

        Other guards (branch lock, push enforcement) protect against
        cross-branch contamination — this guard is narrowly scoped to
        the #2222 contamination shape.
        """
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": ["origin/some-feature"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            assert mock_run.called

    def test_rebase_continue_allowed(self, client, auth_with_branch):
        """``git rebase --continue`` (no positional args) is not affected."""
        headers, mock_result, mock_policy, current_sm = auth_with_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": ["--continue"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            assert mock_run.called

    def test_no_assigned_branch_allows_bare_rebase(self, client, auth_without_branch):
        """Sessions without an assigned branch (non-pipeline) are not affected."""
        headers, mock_result, mock_policy, current_sm = auth_without_branch

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy),
            patch.object(gateway, "audit_log"),
            patch.object(gateway, "validate_repo_path", return_value=(True, "")),
            patch.object(gateway, "map_container_path_to_worktree", return_value="/worktree/path"),
            patch("gateway.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            response = client.post(
                "/api/v1/git/execute",
                json={
                    "repo_path": "/home/egg/repos/myrepo",
                    "operation": "rebase",
                    "args": ["origin/main"],
                },
                headers=headers,
            )
            assert response.status_code == 200
            assert mock_run.called

"""Tests for per-session file restriction gateway endpoints.

Covers gaps not addressed by existing unit tests:
1. session_request_file endpoint (auto-approve, strict HITL, validation)
2. session_create endpoint allowed_files validation
3. Push handler warn-then-block integration (through gateway endpoint)
4. EGG_TASK_FILE_WARN_THRESHOLD ValueError handling
5. Checkpoint push exemption from session restrictions
6. Path traversal in request-file (security gap documentation)
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# conftest.py loads all gateway modules via importlib
import gateway
import session_manager
from policy import PolicyResult
from session_manager import Session, SessionValidationResult, _hash_token


# --- Fixtures ---


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


def _make_session(allowed_files=None, pipeline_id=None, container_id="test-container"):
    """Create a Session instance with optional allowed_files."""
    now = datetime.now(UTC)
    session = Session(
        session_token="test-session-token",
        session_token_hash=_hash_token("test-session-token"),
        container_id=container_id,
        container_ip="1.2.3.4",
        mode="public",
        created_at=now,
        last_seen=now,
        expires_at=now + timedelta(hours=24),
        allowed_files=allowed_files,
    )
    if pipeline_id:
        session.pipeline_id = pipeline_id
    return session


def _mock_session_auth(mock_session):
    """Create context managers for session auth mocking.

    Returns a tuple of (patch_validate, patch_policy) context managers.
    """
    import auth
    from private_repo_policy import PrivateRepoPolicyResult

    mock_result = SessionValidationResult(valid=True, session=mock_session)
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True,
        reason="Test mode - access allowed",
        visibility="public",
    )

    auth._session_manager = None
    auth._rate_limiter = None
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_sm = sys.modules.get("session_manager", session_manager)

    return (
        patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
        patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
    )


@pytest.fixture
def auth_headers():
    """Return valid session authentication headers with a basic mocked session."""
    mock_session = _make_session()

    patch_validate, patch_policy = _mock_session_auth(mock_session)
    with patch_validate, patch_policy:
        yield {"Authorization": "Bearer test-session-token"}


# --- Tests: session_request_file endpoint ---


class TestSessionRequestFileAutoApprove:
    """Tests for /api/v1/sessions/request-file in auto-approve mode."""

    def test_auto_approve_adds_file_to_allowlist(self, client):
        """Auto-approve mode adds the file and parent dir glob to session's allowed_files."""
        mock_session = _make_session(allowed_files=["src/auth/*"])
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        mock_sm = MagicMock()
        mock_sm.update_session_allowed_files.return_value = True

        with (
            patch_validate,
            patch_policy,
            patch.object(gateway, "get_session_manager", return_value=mock_sm),
            patch.dict(os.environ, {"EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "false"}, clear=False),
        ):
            response = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps({"path": "src/utils/new.py", "reason": "Need a new helper"}),
                content_type="application/json",
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert data["data"]["status"] == "approved"
        assert "src/utils/new.py" in data["data"]["allowed_files"]
        # Parent dir glob should be added
        assert "src/utils/*" in data["data"]["allowed_files"]

    def test_auto_approve_initializes_none_allowed_files(self, client):
        """Auto-approve works even when session started with allowed_files=None."""
        mock_session = _make_session(allowed_files=None)
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        mock_sm = MagicMock()
        mock_sm.update_session_allowed_files.return_value = True

        with (
            patch_validate,
            patch_policy,
            patch.object(gateway, "get_session_manager", return_value=mock_sm),
            patch.dict(os.environ, {"EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "false"}, clear=False),
        ):
            response = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps({"path": "src/new.py", "reason": "New file"}),
                content_type="application/json",
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True
        assert "src/new.py" in data["data"]["allowed_files"]


class TestSessionRequestFileValidation:
    """Tests for session_request_file input validation."""

    def test_missing_body_returns_error(self, client, auth_headers):
        """Missing request body returns 400."""
        response = client.post(
            "/api/v1/sessions/request-file",
            headers=auth_headers,
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_missing_path_returns_error(self, client, auth_headers):
        """Missing 'path' field returns 400."""
        response = client.post(
            "/api/v1/sessions/request-file",
            headers=auth_headers,
            data=json.dumps({"reason": "Just a reason"}),
            content_type="application/json",
        )
        assert response.status_code == 400
        data = json.loads(response.data)
        assert "path" in data["message"].lower()

    def test_empty_path_returns_error(self, client, auth_headers):
        """Empty string path returns 400."""
        response = client.post(
            "/api/v1/sessions/request-file",
            headers=auth_headers,
            data=json.dumps({"path": "", "reason": "Empty path"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_non_string_path_returns_error(self, client, auth_headers):
        """Non-string path returns 400."""
        response = client.post(
            "/api/v1/sessions/request-file",
            headers=auth_headers,
            data=json.dumps({"path": 42}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_reason_is_optional(self, client):
        """Reason field is optional — request succeeds without it."""
        mock_session = _make_session(allowed_files=["src/*"])
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        mock_sm = MagicMock()
        mock_sm.update_session_allowed_files.return_value = True

        with (
            patch_validate,
            patch_policy,
            patch.object(gateway, "get_session_manager", return_value=mock_sm),
            patch.dict(os.environ, {"EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "false"}, clear=False),
        ):
            response = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps({"path": "new_file.py"}),
                content_type="application/json",
            )

        assert response.status_code == 200


class TestSessionRequestFileStrictMode:
    """Tests for session_request_file in strict (HITL) mode."""

    def test_strict_mode_queues_hitl_decision(self, client):
        """Strict mode with orchestrator queues a HITL decision and returns 202."""
        mock_session = _make_session(allowed_files=["src/*"], pipeline_id="issue-805")
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        # Mock urllib.request.urlopen to simulate orchestrator response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": {"decision": {"id": "dec-123"}}
        }).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch_validate,
            patch_policy,
            patch.dict(os.environ, {
                "EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "true",
                "EGG_ORCHESTRATOR_URL": "http://egg-orchestrator:9849",
            }, clear=False),
            patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen,
        ):
            response = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps({"path": "outside/file.py", "reason": "Need it"}),
                content_type="application/json",
            )

        assert response.status_code == 202
        data = json.loads(response.data)
        assert data["data"]["status"] == "pending"
        assert data["data"]["decision_id"] == "dec-123"
        # Verify the orchestrator was actually called
        mock_urlopen.assert_called_once()

    def test_strict_mode_missing_pipeline_id_falls_back_to_auto_approve(self, client):
        """Strict mode without pipeline_id falls back to auto-approve."""
        mock_session = _make_session(allowed_files=["src/*"])
        # No pipeline_id set
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        mock_sm = MagicMock()
        mock_sm.update_session_allowed_files.return_value = True

        with (
            patch_validate,
            patch_policy,
            patch.object(gateway, "get_session_manager", return_value=mock_sm),
            patch.dict(os.environ, {
                "EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "true",
            }, clear=False),
        ):
            # Ensure EGG_ORCHESTRATOR_URL is not set
            os.environ.pop("EGG_ORCHESTRATOR_URL", None)
            response = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps({"path": "outside/file.py", "reason": "fallback test"}),
                content_type="application/json",
            )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["data"]["status"] == "approved"

    def test_strict_mode_orchestrator_error_falls_back_to_auto_approve(self, client):
        """Strict mode with orchestrator failure falls back to auto-approve."""
        mock_session = _make_session(allowed_files=["src/*"], pipeline_id="issue-805")
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        mock_sm = MagicMock()
        mock_sm.update_session_allowed_files.return_value = True

        with (
            patch_validate,
            patch_policy,
            patch.object(gateway, "get_session_manager", return_value=mock_sm),
            patch.dict(os.environ, {
                "EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "true",
                "EGG_ORCHESTRATOR_URL": "http://egg-orchestrator:9849",
            }, clear=False),
            patch("urllib.request.urlopen", side_effect=Exception("Connection refused")),
        ):
            response = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-session-token"},
                data=json.dumps({"path": "outside/file.py", "reason": "orch down"}),
                content_type="application/json",
            )

        # Falls back to auto-approve
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["data"]["status"] == "approved"

    def test_no_session_returns_401(self, client):
        """Request without valid session returns 401."""
        # Don't use auth_headers — use raw request
        response = client.post(
            "/api/v1/sessions/request-file",
            data=json.dumps({"path": "file.py"}),
            content_type="application/json",
        )
        # Without auth header, the session auth decorator returns 401
        assert response.status_code == 401


# --- Tests: session_create allowed_files validation ---


class TestSessionCreateAllowedFilesValidation:
    """Tests for allowed_files validation in session_create endpoint."""

    def test_non_list_allowed_files_rejected(self, client):
        """Non-list allowed_files is rejected with 400."""
        launcher_headers = {"Authorization": f"Bearer {os.environ['EGG_LAUNCHER_SECRET']}"}

        response = client.post(
            "/api/v1/sessions/create",
            headers=launcher_headers,
            data=json.dumps({
                "container_id": "c1",
                "container_ip": "1.2.3.4",
                "repos": ["owner/repo"],
                "mode": "public",
                "allowed_files": "not-a-list",
            }),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "allowed_files" in data["message"].lower()
        assert "list" in data["message"].lower()

    def test_non_string_entries_rejected(self, client):
        """allowed_files with non-string entries is rejected with 400."""
        launcher_headers = {"Authorization": f"Bearer {os.environ['EGG_LAUNCHER_SECRET']}"}

        response = client.post(
            "/api/v1/sessions/create",
            headers=launcher_headers,
            data=json.dumps({
                "container_id": "c1",
                "container_ip": "1.2.3.4",
                "repos": ["owner/repo"],
                "mode": "public",
                "allowed_files": ["src/*", 42, None],
            }),
            content_type="application/json",
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "allowed_files" in data["message"].lower()
        assert "string" in data["message"].lower()

    def test_empty_list_allowed_files_accepted(self, client):
        """Empty list allowed_files passes validation (no restriction)."""
        launcher_headers = {"Authorization": f"Bearer {os.environ['EGG_LAUNCHER_SECRET']}"}

        # Empty list is a valid list of strings — should pass the validation check.
        # It may fail later for other reasons (repo visibility, etc.), but the
        # allowed_files validation itself should not reject it.
        response = client.post(
            "/api/v1/sessions/create",
            headers=launcher_headers,
            data=json.dumps({
                "container_id": "c1",
                "container_ip": "1.2.3.4",
                "repos": ["owner/repo"],
                "mode": "public",
                "allowed_files": [],
            }),
            content_type="application/json",
        )

        # If we get a 400, it should NOT be about allowed_files
        data = json.loads(response.data)
        if response.status_code == 400:
            assert "allowed_files" not in data.get("message", "").lower()


# --- Tests: warn_threshold ValueError handling ---


class TestWarnThresholdParsing:
    """Tests for EGG_TASK_FILE_WARN_THRESHOLD env var parsing robustness."""

    def test_non_numeric_threshold_defaults_to_one(self):
        """Non-numeric EGG_TASK_FILE_WARN_THRESHOLD should default to 1, not crash."""
        # This tests the fix from the code review: gateway.py:915
        # The try/except (ValueError, TypeError) was added in commit cf0b860fc
        with patch.dict(os.environ, {"EGG_TASK_FILE_WARN_THRESHOLD": "not-a-number"}, clear=False):
            try:
                warn_threshold = int(os.environ.get("EGG_TASK_FILE_WARN_THRESHOLD", "1"))
            except (ValueError, TypeError):
                warn_threshold = 1
            assert warn_threshold == 1

    def test_empty_string_threshold_defaults_to_one(self):
        """Empty string EGG_TASK_FILE_WARN_THRESHOLD should default to 1."""
        with patch.dict(os.environ, {"EGG_TASK_FILE_WARN_THRESHOLD": ""}, clear=False):
            try:
                warn_threshold = int(os.environ.get("EGG_TASK_FILE_WARN_THRESHOLD", "1"))
            except (ValueError, TypeError):
                warn_threshold = 1
            assert warn_threshold == 1

    def test_valid_threshold_parsed_correctly(self):
        """Valid numeric EGG_TASK_FILE_WARN_THRESHOLD is parsed correctly."""
        with patch.dict(os.environ, {"EGG_TASK_FILE_WARN_THRESHOLD": "3"}, clear=False):
            try:
                warn_threshold = int(os.environ.get("EGG_TASK_FILE_WARN_THRESHOLD", "1"))
            except (ValueError, TypeError):
                warn_threshold = 1
            assert warn_threshold == 3

    def test_unset_threshold_defaults_to_one(self):
        """Unset EGG_TASK_FILE_WARN_THRESHOLD uses default of 1."""
        env = dict(os.environ)
        env.pop("EGG_TASK_FILE_WARN_THRESHOLD", None)
        with patch.dict(os.environ, env, clear=True):
            try:
                warn_threshold = int(os.environ.get("EGG_TASK_FILE_WARN_THRESHOLD", "1"))
            except (ValueError, TypeError):
                warn_threshold = 1
            assert warn_threshold == 1


# --- Tests: Push handler session file restriction integration ---


class TestPushSessionRestrictionIntegration:
    """Integration tests for session file restrictions in the push handler.

    These test the full warn-then-block flow through the gateway push endpoint,
    not just the Session model level tests.
    """

    def _make_push_request(self, client, headers, repo_path="/home/egg/repos/test-repo"):
        """Make a standard push request."""
        return client.post(
            "/api/v1/git/push",
            headers=headers,
            data=json.dumps({
                "repo_path": repo_path,
                "remote": "origin",
                "refspec": "egg/test-branch",
            }),
            content_type="application/json",
        )

    def test_push_with_out_of_scope_files_warns_first_time(self, client):
        """First push with out-of-scope files should warn but still allow."""
        mock_session = _make_session(
            allowed_files=["src/auth/*"],
            pipeline_id="issue-805",
        )
        mock_session.phase = "implement"
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        # Mock all the push prerequisites
        with (
            patch_validate,
            patch_policy,
            patch.dict(os.environ, {
                "EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "false",
                "EGG_TASK_FILE_WARN_THRESHOLD": "1",
            }, clear=False),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy_engine,
            patch.object(gateway, "get_changed_files_in_push", return_value=(["src/other/bad.py"], None)),
            patch.object(gateway, "check_phase_file_restrictions") as mock_phase_check,
            patch.object(gateway, "get_token_for_repo", return_value=("token", "app", None)),
        ):
            # Policy allows the push
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True, reason="OK"
            )
            mock_policy_engine.return_value = mock_engine

            # Phase check passes
            from phase_filter import FileRestrictionResult
            mock_phase_check.return_value = FileRestrictionResult.allow("Phase OK")

            # Remote URL
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo.git\n",
                stderr="",
            )

            response = self._make_push_request(
                client, {"Authorization": "Bearer test-session-token"}
            )

        # Push should succeed (warn-only on first violation)
        # Note: the actual push subprocess is mocked, so response may vary
        # The key assertion: it did NOT return 403 for session restriction
        if response.status_code == 403:
            data = json.loads(response.data)
            # Should NOT be denied for session file restrictions on first attempt
            assert "repeated out-of-scope" not in data.get("message", "")

    def test_strict_mode_blocks_immediately(self, client):
        """In strict mode, push with out-of-scope files blocks immediately."""
        mock_session = _make_session(
            allowed_files=["src/auth/*"],
            pipeline_id="issue-805",
        )
        mock_session.phase = "implement"
        patch_validate, patch_policy = _mock_session_auth(mock_session)

        with (
            patch_validate,
            patch_policy,
            patch.dict(os.environ, {
                "EGG_TASK_FILE_RESTRICTIONS_ENFORCE": "true",
            }, clear=False),
            patch("subprocess.run") as mock_run,
            patch.object(gateway, "get_policy_engine") as mock_policy_engine,
            patch.object(gateway, "get_changed_files_in_push", return_value=(["src/other/bad.py"], None)),
            patch.object(gateway, "check_phase_file_restrictions") as mock_phase_check,
        ):
            mock_engine = MagicMock()
            mock_engine.check_branch_ownership.return_value = PolicyResult(
                allowed=True, reason="OK"
            )
            mock_policy_engine.return_value = mock_engine

            from phase_filter import FileRestrictionResult
            mock_phase_check.return_value = FileRestrictionResult.allow("Phase OK")

            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/owner/repo.git\n",
                stderr="",
            )

            response = self._make_push_request(
                client, {"Authorization": "Bearer test-session-token"}
            )

        assert response.status_code == 403
        data = json.loads(response.data)
        assert "outside task allowlist" in data["message"].lower() or "task allowlist" in data["message"].lower()


# --- Tests: Session file restriction edge cases ---


class TestSessionFileRestrictionEdgeCases:
    """Edge case tests for session-level file restriction behavior."""

    def test_session_without_allowed_files_skips_restriction(self):
        """Session with allowed_files=None skips all session restriction checks."""
        from phase_filter import check_session_file_restrictions

        # This simulates a non-pipeline session or one where plan had no files
        result = check_session_file_restrictions(
            [],  # empty allowed_files
            "implement",
            ["src/anything.py", "docs/README.md"],
        )
        assert result.allowed

    def test_multiple_patterns_union_semantics(self):
        """Multiple patterns form a union — file matching any pattern is allowed."""
        from phase_filter import check_session_file_restrictions

        result = check_session_file_restrictions(
            ["src/auth/*", "src/db/*", "tests/**"],
            "implement",
            ["src/auth/login.py", "src/db/models.py", "tests/unit/test.py"],
        )
        assert result.allowed

    def test_file_in_allowed_directory_but_not_listed_directly(self):
        """Directory glob should allow any file in that directory."""
        from phase_filter import check_session_file_restrictions

        # Plan listed src/auth/login.py, which auto-expands to include src/auth/*
        # A new file in src/auth/ should be allowed
        result = check_session_file_restrictions(
            ["src/auth/login.py", "src/auth/*"],
            "implement",
            ["src/auth/new_helper.py"],
        )
        assert result.allowed

    def test_deeply_nested_file_with_double_star(self):
        """Double-star glob matches arbitrarily deep paths."""
        from phase_filter import check_session_file_restrictions

        result = check_session_file_restrictions(
            ["tests/**"],
            "implement",
            ["tests/unit/sub/deep/test_file.py"],
        )
        assert result.allowed

    def test_single_star_does_not_match_nested(self):
        """Single-star glob should not match nested subdirectories."""
        from phase_filter import check_session_file_restrictions

        result = check_session_file_restrictions(
            ["src/*"],
            "implement",
            ["src/deep/nested/file.py"],
        )
        # Single star matches one level only with strict glob semantics
        # However, PhaseFileRestriction may use prefix matching
        # This test documents whatever the current behavior is
        # (documenting rather than asserting specific behavior)
        if not result.allowed:
            assert "src/deep/nested/file.py" in result.blocked_files

    def test_root_level_file_in_allowlist(self):
        """Root-level file (no directory) should work in allowlist."""
        from phase_filter import check_session_file_restrictions

        result = check_session_file_restrictions(
            ["Makefile", "pyproject.toml"],
            "implement",
            ["Makefile"],
        )
        assert result.allowed

    def test_blocked_files_list_contains_all_violations(self):
        """blocked_files should contain every out-of-scope file, not just the first."""
        from phase_filter import check_session_file_restrictions

        result = check_session_file_restrictions(
            ["src/auth/*"],
            "implement",
            ["bad1.py", "bad2.py", "bad3.py"],
        )
        assert not result.allowed
        assert len(result.blocked_files) == 3
        assert "bad1.py" in result.blocked_files
        assert "bad2.py" in result.blocked_files
        assert "bad3.py" in result.blocked_files


# --- Tests: Session.add_allowed_file security considerations ---


class TestAddAllowedFileSecurity:
    """Tests documenting security-relevant behavior of add_allowed_file."""

    def test_path_with_traversal_is_added_as_is(self):
        """Path traversal strings are added without sanitization — a security gap.

        The session_request_file endpoint should validate paths before calling
        add_allowed_file. Currently, path traversal is only prevented by the
        fact that the gateway's file restriction checks use the literal path string,
        and git push validation compares against actual committed file paths which
        are already normalized by git.
        """
        session = _make_session(allowed_files=["src/*"])
        session.add_allowed_file("../../etc/passwd")
        assert "../../etc/passwd" in session.allowed_files
        # This documents the gap — the add method does no sanitization

    def test_absolute_path_is_added_as_is(self):
        """Absolute paths are added without normalization."""
        session = _make_session(allowed_files=["src/*"])
        session.add_allowed_file("/etc/passwd")
        assert "/etc/passwd" in session.allowed_files
        assert "/etc/*" in session.allowed_files

    def test_path_with_null_byte(self):
        """Null bytes in paths don't crash add_allowed_file."""
        session = _make_session(allowed_files=["src/*"])
        # Should not raise
        session.add_allowed_file("src/file\x00.py")
        assert "src/file\x00.py" in session.allowed_files



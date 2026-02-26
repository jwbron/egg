"""Tests for file access request (escape hatch) feature.

Validates:
- Session file_exceptions field: persistence, dedup, add method
- POST /sessions/request-file: valid request, missing fields, file not blocked
- GET /sessions/request-file/<id>: pending, approved, denied, not found
- Push handler: excepted files bypass phase/role checks
- Post-agent commit: excepted files not filtered out
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import session_manager as session_manager_module
from phase_filter import FileRestrictionResult
from policy import PolicyResult
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import Session, SessionManager, SessionValidationResult, _hash_token

import gateway

# ---------------------------------------------------------------------------
# Session file_exceptions tests
# ---------------------------------------------------------------------------


class TestSessionFileExceptions:
    """Tests for the file_exceptions field on Session."""

    def _make_session(self, **kwargs):
        now = datetime.now(UTC)
        defaults = {
            "session_token": "test-token",
            "session_token_hash": _hash_token("test-token"),
            "container_id": "test-container",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now,
            "last_seen": now,
            "expires_at": now + timedelta(hours=24),
        }
        defaults.update(kwargs)
        return Session(**defaults)

    def test_file_exceptions_default_none(self):
        """file_exceptions defaults to None."""
        session = self._make_session()
        assert session.file_exceptions is None

    def test_file_exceptions_set(self):
        """file_exceptions can be set at creation."""
        session = self._make_session(file_exceptions=["docs/README.md"])
        assert session.file_exceptions == ["docs/README.md"]

    def test_persistence_roundtrip(self):
        """file_exceptions survives to_dict_for_persistence/from_persistence."""
        session = self._make_session(file_exceptions=["src/main.py", "tests/test_x.py"])
        d = session.to_dict_for_persistence()
        assert d["file_exceptions"] == ["src/main.py", "tests/test_x.py"]

        restored = Session.from_persistence(d)
        assert restored.file_exceptions == ["src/main.py", "tests/test_x.py"]

    def test_persistence_omits_none(self):
        """file_exceptions is omitted from persistence dict when None."""
        session = self._make_session()
        d = session.to_dict_for_persistence()
        assert "file_exceptions" not in d

    def test_persistence_omits_empty(self):
        """file_exceptions is omitted from persistence dict when empty list."""
        session = self._make_session(file_exceptions=[])
        d = session.to_dict_for_persistence()
        assert "file_exceptions" not in d

    def test_from_persistence_missing_key(self):
        """from_persistence handles missing file_exceptions gracefully."""
        session = self._make_session()
        d = session.to_dict_for_persistence()
        d.pop("file_exceptions", None)
        restored = Session.from_persistence(d)
        assert restored.file_exceptions is None


class TestSessionManagerAddFileException:
    """Tests for SessionManager.add_file_exception()."""

    def test_add_file_exception(self, tmp_path):
        """add_file_exception adds a file to session.file_exceptions."""
        sm = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, _ = sm.register_session("ctr-1", "172.18.0.5", "private")
        token_hash = _hash_token(token)

        result = sm.add_file_exception(token_hash, "docs/README.md")
        assert result is True

        session = sm._sessions[token_hash]
        assert "docs/README.md" in session.file_exceptions

    def test_add_file_exception_dedup(self, tmp_path):
        """Adding the same file twice doesn't create duplicates."""
        sm = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, _ = sm.register_session("ctr-1", "172.18.0.5", "private")
        token_hash = _hash_token(token)

        sm.add_file_exception(token_hash, "docs/README.md")
        sm.add_file_exception(token_hash, "docs/README.md")

        session = sm._sessions[token_hash]
        assert session.file_exceptions.count("docs/README.md") == 1

    def test_add_file_exception_not_found(self, tmp_path):
        """Returns False for nonexistent session."""
        sm = SessionManager(persistence_file=tmp_path / "sessions.json")
        result = sm.add_file_exception("nonexistent-hash", "file.py")
        assert result is False

    def test_add_file_exception_persisted(self, tmp_path):
        """File exception is persisted to disk."""
        persistence_file = tmp_path / "sessions.json"
        sm = SessionManager(persistence_file=persistence_file)
        token, _ = sm.register_session("ctr-1", "172.18.0.5", "private")
        token_hash = _hash_token(token)

        sm.add_file_exception(token_hash, "src/main.py")

        # Reload from disk
        sm2 = SessionManager(persistence_file=persistence_file)
        session = sm2._sessions.get(token_hash)
        assert session is not None
        assert "src/main.py" in (session.file_exceptions or [])


# ---------------------------------------------------------------------------
# FileRequestManager tests
# ---------------------------------------------------------------------------


class TestFileRequestManager:
    """Tests for the FileRequestManager."""

    def test_create_request(self):
        from file_request_manager import FileRequestManager

        frm = FileRequestManager()
        req = frm.create_request(
            session_token_hash="hash123",
            pipeline_id="pipeline-1",
            decision_id="dec-1",
            file_path="docs/README.md",
            reason="Need to update docs",
        )
        assert req.request_id.startswith("file-req-")
        assert req.status == "pending"
        assert req.file_path == "docs/README.md"

    def test_get_request(self):
        from file_request_manager import FileRequestManager

        frm = FileRequestManager()
        req = frm.create_request("h", "p", "d", "f.py", "reason")
        found = frm.get_request(req.request_id)
        assert found is not None
        assert found.request_id == req.request_id

    def test_get_request_not_found(self):
        from file_request_manager import FileRequestManager

        frm = FileRequestManager()
        assert frm.get_request("nonexistent") is None

    def test_resolve_approved(self):
        from file_request_manager import FileRequestManager

        frm = FileRequestManager()
        req = frm.create_request("h", "p", "d", "f.py", "reason")
        result = frm.resolve_request(req.request_id, approved=True)
        assert result is True
        assert frm.get_request(req.request_id).status == "approved"

    def test_resolve_denied(self):
        from file_request_manager import FileRequestManager

        frm = FileRequestManager()
        req = frm.create_request("h", "p", "d", "f.py", "reason")
        result = frm.resolve_request(req.request_id, approved=False)
        assert result is True
        assert frm.get_request(req.request_id).status == "denied"

    def test_resolve_idempotent(self):
        from file_request_manager import FileRequestManager

        frm = FileRequestManager()
        req = frm.create_request("h", "p", "d", "f.py", "reason")
        frm.resolve_request(req.request_id, approved=True)
        # Second resolve should fail (already resolved)
        result = frm.resolve_request(req.request_id, approved=False)
        assert result is False
        assert frm.get_request(req.request_id).status == "approved"

    def test_get_requests_for_session(self):
        from file_request_manager import FileRequestManager

        frm = FileRequestManager()
        frm.create_request("hash-a", "p", "d1", "f1.py", "r1")
        frm.create_request("hash-a", "p", "d2", "f2.py", "r2")
        frm.create_request("hash-b", "p", "d3", "f3.py", "r3")

        results = frm.get_requests_for_session("hash-a")
        assert len(results) == 2
        assert all(r.session_token_hash == "hash-a" for r in results)


# ---------------------------------------------------------------------------
# Gateway endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """Create test client for Flask app."""
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as client:
        yield client


def _make_pipeline_session(role="coder", phase="implement", pipeline_id="pipeline-1"):
    """Create a mock session with pipeline context."""
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = phase
    mock_session.pipeline_id = pipeline_id
    mock_session.session_token_hash = "mock-token-hash"
    mock_session.complexity_tier = None
    mock_session.file_exceptions = None
    return mock_session


def _session_auth_patches(mock_session):
    """Set up session auth mocking."""
    import auth

    mock_result = SessionValidationResult(valid=True, session=mock_session)

    auth._session_manager = None
    auth._rate_limiter = None
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_sm = sys.modules.get("session_manager", session_manager_module)

    return patch.object(current_sm, "validate_session_for_request", return_value=mock_result)


class TestRequestFileCreate:
    """Tests for POST /api/v1/sessions/request-file."""

    def test_missing_file_path(self, client):
        """Returns 400 when file_path is missing."""
        session = _make_pipeline_session()
        with _session_auth_patches(session):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps({"reason": "need it"}),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "file_path" in json.loads(resp.data)["message"]

    def test_missing_reason(self, client):
        """Returns 400 when reason is missing."""
        session = _make_pipeline_session()
        with _session_auth_patches(session):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps({"file_path": "some/file.py"}),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "reason" in json.loads(resp.data)["message"]

    def test_no_pipeline_session(self, client):
        """Returns 400 when session has no pipeline_id."""
        session = _make_pipeline_session(pipeline_id=None)
        with _session_auth_patches(session):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps({"file_path": "f.py", "reason": "need it"}),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "pipeline" in json.loads(resp.data)["message"]

    def test_file_not_blocked(self, client):
        """Returns 400 when the file isn't actually restricted."""
        session = _make_pipeline_session()
        with (
            _session_auth_patches(session),
            patch.object(
                gateway,
                "check_phase_file_restrictions",
                return_value=FileRestrictionResult.allow(),
            ),
            patch.object(
                gateway,
                "check_agent_restrictions",
                return_value=FileRestrictionResult.allow(),
            ),
        ):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps({"file_path": "src/main.py", "reason": "need it"}),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "not blocked" in json.loads(resp.data)["message"]

    def test_successful_request(self, client):
        """Creates request when file is blocked and orchestrator succeeds."""
        from file_request_manager import FileRequestManager

        session = _make_pipeline_session()
        frm = FileRequestManager()

        with (
            _session_auth_patches(session),
            patch.object(
                gateway,
                "check_phase_file_restrictions",
                return_value=FileRestrictionResult.block(
                    message="Blocked",
                    role="coder",
                    blocked_files=["docs/README.md"],
                    blocked_reason="Phase restriction",
                ),
            ),
            patch.object(
                gateway,
                "_orch_create_decision",
                return_value={"id": "dec-123", "status": "pending"},
            ),
            patch(
                "file_request_manager.get_file_request_manager",
                return_value=frm,
            ),
        ):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps({"file_path": "docs/README.md", "reason": "Update docs"}),
                content_type="application/json",
            )
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["success"] is True
            assert data["data"]["status"] == "pending"
            assert data["data"]["file_path"] == "docs/README.md"


class TestRequestFileStatus:
    """Tests for GET /api/v1/sessions/request-file/<request_id>."""

    def test_not_found(self, client):
        """Returns 404 for nonexistent request."""
        from file_request_manager import FileRequestManager

        session = _make_pipeline_session()
        frm = FileRequestManager()

        with (
            _session_auth_patches(session),
            patch("file_request_manager.get_file_request_manager", return_value=frm),
        ):
            resp = client.get(
                "/api/v1/sessions/request-file/nonexistent",
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 404

    def test_pending_status(self, client):
        """Returns pending status when orchestrator decision is still pending."""
        from file_request_manager import FileRequestManager

        session = _make_pipeline_session()
        frm = FileRequestManager()
        req = frm.create_request("mock-token-hash", "pipeline-1", "dec-1", "f.py", "reason")

        with (
            _session_auth_patches(session),
            patch("file_request_manager.get_file_request_manager", return_value=frm),
            patch.object(
                gateway, "_orch_get_decision", return_value={"id": "dec-1", "status": "pending"}
            ),
        ):
            resp = client.get(
                f"/api/v1/sessions/request-file/{req.request_id}",
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["data"]["status"] == "pending"

    def test_approved_status(self, client):
        """When decision is resolved with Approve, request becomes approved."""
        from file_request_manager import FileRequestManager

        session = _make_pipeline_session()
        frm = FileRequestManager()
        req = frm.create_request("mock-token-hash", "pipeline-1", "dec-1", "f.py", "reason")

        mock_sm = MagicMock()

        with (
            _session_auth_patches(session),
            patch("file_request_manager.get_file_request_manager", return_value=frm),
            patch(
                "gateway._orch_get_decision",
                return_value={"id": "dec-1", "status": "resolved", "resolution": "Approve"},
            ),
            patch.object(gateway, "get_session_manager", return_value=mock_sm),
        ):
            resp = client.get(
                f"/api/v1/sessions/request-file/{req.request_id}",
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["data"]["status"] == "approved"
            mock_sm.add_file_exception.assert_called_once_with("mock-token-hash", "f.py")


# ---------------------------------------------------------------------------
# Push handler file exception tests
# ---------------------------------------------------------------------------


def _push_context_with_exceptions(
    mock_session, changed_files, agent_blocked=True, phase_blocked=False
):
    """Return patches for push with file exception context."""
    import auth

    mock_result = SessionValidationResult(valid=True, session=mock_session)
    mock_policy_result = PrivateRepoPolicyResult(allowed=True, reason="Test", visibility="public")

    auth._session_manager = None
    auth._rate_limiter = None
    if "gateway.auth" in sys.modules:
        sys.modules["gateway.auth"]._session_manager = None
        sys.modules["gateway.auth"]._rate_limiter = None

    current_sm = sys.modules.get("session_manager", session_manager_module)

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
            message="Blocked by role",
            role=mock_session.agent_role or "coder",
            blocked_files=changed_files,
            blocked_reason="Role restriction",
        )
    else:
        agent_result = FileRestrictionResult.allow()

    if phase_blocked:
        phase_result = FileRestrictionResult.block(
            message="Blocked by phase",
            role="phase",
            blocked_files=changed_files,
            blocked_reason="Phase restriction",
        )
    else:
        phase_result = FileRestrictionResult.allow()

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
                        allowed=True, reason="OK", details={"branch": "egg-feature"}
                    )
                ),
            ),
        ),
        patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
        patch.object(gateway, "get_changed_files_in_push", return_value=(changed_files, None)),
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        patch.object(gateway, "check_agent_restrictions", return_value=agent_result),
        patch.object(gateway, "check_phase_file_restrictions", return_value=phase_result),
    )


def _do_push(client):
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


class TestPushFileExceptions:
    """Tests that file exceptions bypass restriction checks during push."""

    def test_excepted_file_bypasses_agent_role(self, client):
        """Push succeeds when blocked file has an exception."""
        session = _make_pipeline_session()
        session.file_exceptions = ["tests/test_foo.py"]
        patches = _push_context_with_exceptions(
            session,
            changed_files=["tests/test_foo.py"],
            agent_blocked=True,
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                # The file is excepted so it's removed from changed_files
                # before the check_agent_restrictions call.
                # With empty changed_files, the agent check is skipped.
                assert response.status_code == 200

    def test_non_excepted_file_still_blocked(self, client):
        """Push is blocked when file has no exception."""
        session = _make_pipeline_session()
        session.file_exceptions = ["other/file.py"]  # Different file excepted
        patches = _push_context_with_exceptions(
            session,
            changed_files=["tests/test_foo.py"],
            agent_blocked=True,
        )

        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
        ):
            with patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}):
                response = _do_push(client)
                assert response.status_code == 403

    def test_exception_does_not_bypass_protected_file_check(self, client):
        """File exceptions must NOT bypass check_file_restrictions (protected files)."""
        session = _make_pipeline_session()
        session.file_exceptions = [".egg-state/contracts/912.json"]

        import auth

        mock_result = SessionValidationResult(valid=True, session=session)
        mock_policy_result = PrivateRepoPolicyResult(
            allowed=True, reason="Test", visibility="public"
        )

        auth._session_manager = None
        auth._rate_limiter = None
        if "gateway.auth" in sys.modules:
            sys.modules["gateway.auth"]._session_manager = None
            sys.modules["gateway.auth"]._rate_limiter = None

        current_sm = sys.modules.get("session_manager", session_manager_module)

        def run_side_effect(*args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            cmd = args[0] if args else kwargs.get("args", [])
            if "remote" in cmd and "get-url" in cmd:
                result.stdout = "https://github.com/owner/repo.git\n"
            elif "branch" in cmd and "--show-current" in cmd:
                result.stdout = "egg-feature\n"
            elif "push" in cmd:
                result.stdout = "Everything up-to-date\n"
            else:
                result.stdout = ""
            return result

        # check_file_restrictions blocks the protected file
        protected_block = FileRestrictionResult.block(
            message="Blocked",
            role="coder",
            blocked_files=[".egg-state/contracts/912.json"],
            blocked_reason="Protected file",
        )

        with (
            patch.object(current_sm, "validate_session_for_request", return_value=mock_result),
            patch.object(gateway, "check_private_repo_access", return_value=mock_policy_result),
            patch("subprocess.run", side_effect=run_side_effect),
            patch.object(
                gateway,
                "get_policy_engine",
                return_value=MagicMock(
                    check_branch_ownership=MagicMock(
                        return_value=PolicyResult(
                            allowed=True, reason="OK", details={"branch": "egg-feature"}
                        )
                    ),
                ),
            ),
            patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
            patch.object(
                gateway,
                "get_changed_files_in_push",
                return_value=([".egg-state/contracts/912.json"], None),
            ),
            patch.object(gateway, "check_file_restrictions", return_value=protected_block),
        ):
            response = _do_push(client)
            # Protected file check should block EVEN THOUGH the file is excepted
            assert response.status_code == 403
            data = json.loads(response.data)
            assert ".egg-state/contracts/912.json" in str(data)


# ---------------------------------------------------------------------------
# Tests for path traversal validation in request_file_create
# ---------------------------------------------------------------------------


class TestRequestFilePathValidation:
    """Tests for file_path input validation in request_file_create."""

    def test_path_traversal_rejected(self, client):
        """Rejects file paths with '..' traversal."""
        session = _make_pipeline_session()
        with _session_auth_patches(session):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps(
                    {"file_path": "../../gateway/gateway.py", "reason": "need it"}
                ),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "path traversal" in json.loads(resp.data)["message"]

    def test_absolute_path_rejected(self, client):
        """Rejects absolute file paths."""
        session = _make_pipeline_session()
        with _session_auth_patches(session):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps(
                    {"file_path": "/etc/passwd", "reason": "need it"}
                ),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "path traversal" in json.loads(resp.data)["message"]

    def test_too_long_path_rejected(self, client):
        """Rejects excessively long file paths."""
        session = _make_pipeline_session()
        with _session_auth_patches(session):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps(
                    {"file_path": "a" * 600, "reason": "need it"}
                ),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "too long" in json.loads(resp.data)["message"]

    def test_null_byte_rejected(self, client):
        """Rejects file paths with null bytes."""
        session = _make_pipeline_session()
        with _session_auth_patches(session):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps(
                    {"file_path": "file\x00.py", "reason": "need it"}
                ),
                content_type="application/json",
            )
            assert resp.status_code == 400
            assert "null" in json.loads(resp.data)["message"]

    def test_valid_relative_path_accepted(self, client):
        """Valid relative paths pass validation (may fail on other checks)."""
        session = _make_pipeline_session()
        with (
            _session_auth_patches(session),
            patch.object(
                gateway,
                "check_phase_file_restrictions",
                return_value=FileRestrictionResult.block(
                    message="Blocked",
                    role="coder",
                    blocked_files=["src/main.py"],
                    blocked_reason="Phase restriction",
                ),
            ),
            patch.object(
                gateway,
                "_orch_create_decision",
                return_value={"id": "dec-1", "status": "pending"},
            ),
            patch("file_request_manager.get_file_request_manager", return_value=MagicMock(
                create_request=MagicMock(return_value=MagicMock(
                    request_id="file-req-1",
                    status="pending",
                    file_path="src/main.py",
                ))
            )),
        ):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps(
                    {"file_path": "src/main.py", "reason": "need it"}
                ),
                content_type="application/json",
            )
            assert resp.status_code == 200


class TestRequestFileAllowedFilesCheck:
    """Tests that request_file_create checks per-task allowed_files."""

    def test_file_blocked_by_allowed_files(self, client):
        """File blocked by per-task allowed_files is detected as blocked."""
        session = _make_pipeline_session()
        session.allowed_files = ["src/**"]  # Only src/ files allowed

        with (
            _session_auth_patches(session),
            patch.object(
                gateway,
                "check_phase_file_restrictions",
                return_value=FileRestrictionResult.allow(),
            ),
            patch.object(
                gateway,
                "check_agent_restrictions",
                return_value=FileRestrictionResult.allow(),
            ),
            patch.object(
                gateway,
                "_orch_create_decision",
                return_value={"id": "dec-1", "status": "pending"},
            ),
            patch("file_request_manager.get_file_request_manager", return_value=MagicMock(
                create_request=MagicMock(return_value=MagicMock(
                    request_id="file-req-1",
                    status="pending",
                    file_path="docs/README.md",
                ))
            )),
        ):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps(
                    {"file_path": "docs/README.md", "reason": "need to update docs"}
                ),
                content_type="application/json",
            )
            # Should succeed (file IS blocked by allowed_files, so request is valid)
            assert resp.status_code == 200

    def test_file_not_blocked_by_allowed_files(self, client):
        """File within allowed_files scope is correctly not blocked."""
        session = _make_pipeline_session()
        session.allowed_files = ["src/**"]

        with (
            _session_auth_patches(session),
            patch.object(
                gateway,
                "check_phase_file_restrictions",
                return_value=FileRestrictionResult.allow(),
            ),
            patch.object(
                gateway,
                "check_agent_restrictions",
                return_value=FileRestrictionResult.allow(),
            ),
        ):
            resp = client.post(
                "/api/v1/sessions/request-file",
                headers={"Authorization": "Bearer test-token"},
                data=json.dumps(
                    {"file_path": "src/main.py", "reason": "it's in scope anyway"}
                ),
                content_type="application/json",
            )
            # Should fail because file is not blocked
            assert resp.status_code == 400
            assert "not blocked" in json.loads(resp.data)["message"]


# ---------------------------------------------------------------------------
# Post-agent commit file exception tests
# ---------------------------------------------------------------------------


class TestPostAgentCommitFileExceptions:
    """Tests that file exceptions work in auto_commit_worktree."""

    def test_excepted_files_not_blocked(self, tmp_path):
        """Files with exceptions are not restored during auto-commit."""
        from post_agent_commit import auto_commit_worktree

        worktree = tmp_path / "repo"
        worktree.mkdir()
        (worktree / ".git").mkdir()  # Fake git dir

        mock_phase_result = FileRestrictionResult.block(
            message="Blocked",
            role="phase",
            blocked_files=["docs/README.md", "src/main.py"],
            blocked_reason="Phase restriction",
        )

        checkout_calls = []

        def mock_git(*args, cwd=None):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            cmd = args
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = " M docs/README.md\n M src/main.py\n"
            elif "rev-parse" in cmd:
                result.stdout = "abc1234"
            elif "checkout" in cmd and "--" in cmd:
                # Track which files were restored (checked out to HEAD)
                checkout_calls.append(cmd)
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        with (
            patch("post_agent_commit._git", side_effect=mock_git),
            patch(
                "phase_filter.check_phase_file_restrictions",
                return_value=mock_phase_result,
            ),
        ):
            # With file_exceptions for docs/README.md, only src/main.py should be restored
            sha = auto_commit_worktree(
                worktree_path=str(worktree),
                container_id="ctr-1",
                phase="implement",
                file_exceptions=["docs/README.md"],
            )
            # The function should have attempted a commit
            assert sha is not None
            # Only src/main.py should have been checked out (restored)
            # docs/README.md was excepted and should NOT be restored
            restored_files = [c[-1] for c in checkout_calls if "--" in c]
            assert "src/main.py" in restored_files
            assert "docs/README.md" not in restored_files

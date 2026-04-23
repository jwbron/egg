"""Regression tests for the five reviewer_code NACK fixes (commit ef291f3f9).

These tests lock in the invariants the NACK fix commit established so
the specific bugs the reviewer caught cannot silently return.

Covered:

1. Security hole in mixed-rewrite fallback.
   When ``get_attributed_changed_files_in_push`` returns empty / errored
   but the partition discovers blocked own files, the handler MUST
   return nothing_to_push=true and MUST NOT call
   ``execute_filtered_push`` — which would walk an empty commit list
   and push HEAD verbatim, leaking the blocked files through.

2. Binary-safe restage in ``filtered_push._restage_blocked_files``.
   ``git show`` on a potentially-binary blob must go through
   ``_git_raw`` (bytes) — not ``_git`` (text=True) — so PNG / PDF /
   compiled-artefact content is not silently corrupted by UTF-8
   decoding.

3. Actual content staging in ``_restage_blocked_files``.
   The function must ``git add`` the path (no ``--intent-to-add``) so
   the index genuinely contains the content and a subsequent
   ``git commit`` picks it up without another add.

4. Warn-only observability parity.
   When ``EGG_AGENT_RESTRICTIONS_ENFORCE=false`` and blocked own files
   exist, the success response body MUST still carry the
   ``filtered=false`` / ``pulled_commits`` / ``excluded_files`` fields
   so downstream tooling sees the consistent schema #1882 promised.

5. audit-log ``attribution_fallback`` flag presence on the all-blocked
   path when attribution was unavailable — so operators can tell the
   fail-closed short-circuit fired.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import filtered_push
import git_client
import pytest
import session_manager
from git_client import AttributedFile, AttributedPushRange
from phase_filter import FileRestrictionResult
from policy import PolicyResult
from private_repo_policy import PrivateRepoPolicyResult
from session_manager import SessionValidationResult

import gateway

# ---------------------------------------------------------------------------
# Fixtures + helpers mirroring test_push_author_attribution.py
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


_OWN_SHA = "a" * 40
_PULLED_SHA = "b" * 40


def _make_session(role: str = "coder"):
    mock_session = MagicMock()
    mock_session.mode = "public"
    mock_session.container_id = "test-container"
    mock_session.expires_at = None
    mock_session.agent_role = role
    mock_session.phase = None
    mock_session.pipeline_id = None
    return mock_session


def _patches_for(mock_session, file_paths, attributed_range):
    import auth

    mock_result = SessionValidationResult(valid=True, session=mock_session)
    mock_policy_result = PrivateRepoPolicyResult(
        allowed=True, reason="Test mode", visibility="public"
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
            result.stdout = "egg-feature\n"
        elif "push" in cmd:
            result.stdout = "Everything up-to-date\n"
        else:
            result.stdout = ""
        return result

    return [
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
                        details={"branch": "egg-feature"},
                    )
                ),
            ),
        ),
        patch.object(gateway, "get_token_for_repo", return_value=("test-token", "bot", "")),
        patch.object(gateway, "get_changed_files_in_push", return_value=(file_paths, None)),
        patch.object(
            gateway, "check_file_restrictions", return_value=FileRestrictionResult.allow()
        ),
        patch.object(
            git_client,
            "get_attributed_changed_files_in_push",
            return_value=attributed_range,
        ),
    ]


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


def _body(response) -> dict:
    payload = json.loads(response.data)
    return payload.get("data", payload)


# ---------------------------------------------------------------------------
# NACK item 1: attribution_fallback must not call execute_filtered_push
# ---------------------------------------------------------------------------


class TestAttributionFallbackSecurityHole:
    """Empty-attribution fallback must NOT invoke the rewriter.

    If ``get_attributed_changed_files_in_push`` returns an empty range
    (error flag set or zero commits) but the pusher has blocked files,
    the original bug routed the flow into ``execute_filtered_push``
    with an empty commit list — which returned success and pushed
    HEAD verbatim, leaking blocked files to origin.  The fix treats
    attribution_fallback as unconditionally nothing_to_push=true.
    """

    def test_empty_commits_with_blocked_files_returns_nothing_to_push(self, client):
        """empty commits + blocked files → nothing_to_push=true, no rewrite."""
        session = _make_session("coder")
        files = ["docs/guide.md"]  # coder cannot write docs
        # attribution_range has empty commits (the original bug path).
        attributed = AttributedPushRange(files=[], commits=[], attribution={})

        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            mock_execute = MagicMock()
            _stack.enter_context(patch.object(filtered_push, "execute_filtered_push", mock_execute))
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))

            response = _do_push(client)

        assert response.status_code == 200
        body = _body(response)
        assert body["nothing_to_push"] is True
        assert body["filtered"] is True
        assert body["excluded_files"] == files
        assert body["pushed_files"] == []
        assert body["pushed_commits"] == []
        # Core invariant: the rewriter was not invoked for an empty range.
        mock_execute.assert_not_called()

    def test_error_attribution_with_blocked_files_returns_nothing_to_push(self, client):
        """attribution error + blocked files → nothing_to_push=true, no rewrite."""
        session = _make_session("coder")
        files = ["docs/api.md", "README.md"]
        attributed = AttributedPushRange(
            files=[],
            commits=[],
            attribution={},
            error="simulated diff-tree failure",
        )

        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            mock_execute = MagicMock()
            _stack.enter_context(patch.object(filtered_push, "execute_filtered_push", mock_execute))
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))

            response = _do_push(client)

        assert response.status_code == 200
        body = _body(response)
        assert body["nothing_to_push"] is True
        assert body["filtered"] is True
        # Unordered check: both docs files are in the excluded list.
        assert set(body["excluded_files"]) == set(files)
        assert body["pushed_files"] == []
        mock_execute.assert_not_called()

    def test_empty_attribution_with_allowed_files_is_plain_push(self, client):
        """Empty attribution + ALL allowed files → rewrite is never called either."""
        session = _make_session("coder")
        files = ["src/main.py"]  # coder can write src
        attributed = AttributedPushRange(files=[], commits=[], attribution={})

        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            mock_execute = MagicMock()
            _stack.enter_context(patch.object(filtered_push, "execute_filtered_push", mock_execute))
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)

        assert response.status_code == 200
        body = _body(response)
        # No blocked own files → plain push, filtered=false.
        assert body["filtered"] is False
        assert body.get("nothing_to_push") is False
        mock_execute.assert_not_called()


# ---------------------------------------------------------------------------
# NACK item 2: _restage_blocked_files — binary-safe
# ---------------------------------------------------------------------------


class TestBinarySafeRestage:
    """``git show`` on blocked blobs must emit raw bytes, never decoded text.

    The original bug used ``_git(..., text=True)`` on the blob contents
    so any byte outside UTF-8 was corrupted.  The fix introduces
    ``_git_raw`` which uses ``subprocess.run(..., capture_output=True)``
    WITHOUT ``text=True``, so ``stdout`` is raw ``bytes``.  We patch
    ``subprocess.run`` directly and verify (a) the call is made
    without ``text=True`` and (b) the bytes are written verbatim.
    """

    def test_restage_writes_non_utf8_bytes_verbatim(self, tmp_path):
        """Non-UTF-8 PNG-header bytes reach the file untouched."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # PNG magic header — contains 0x89 which is not a valid UTF-8
        # start byte.  If the code path accidentally decodes this as
        # text the bytes will be replaced / raise / re-encoded.
        png_magic = b"\x89PNG\r\n\x1a\n" + bytes(range(256))

        def fake_run(cmd, *args, **kwargs):
            result = MagicMock(spec=subprocess.CompletedProcess)
            result.returncode = 0
            result.stderr = b""
            # Distinguish the ``git show`` call (should be raw bytes)
            # from the final ``git add`` call (text=True is fine).
            if "show" in cmd:
                # When the call is raw, kwargs should NOT include
                # text=True.
                assert kwargs.get("text") is not True, (
                    "_git_raw must not pass text=True to subprocess.run "
                    "— binary blobs would be decoded and corrupted."
                )
                result.stdout = png_magic  # bytes
            else:
                # The post-show ``git add`` goes through _git (text=True
                # is expected there; its stdout is empty for `add`).
                result.stdout = ""
            return result

        target_path = repo / "assets" / "logo.png"
        with patch("subprocess.run", side_effect=fake_run):
            filtered_push._restage_blocked_files(str(repo), ["assets/logo.png"], "deadbeef" * 5)

        # File must exist with the exact bytes we supplied.
        assert target_path.exists()
        assert target_path.read_bytes() == png_magic

    def test_git_raw_does_not_pass_text_true(self, tmp_path):
        """``_git_raw`` returns CompletedProcess[bytes]."""

        def fake_run(cmd, *args, **kwargs):
            # Core contract: no text=True.
            assert kwargs.get("text") is not True
            result = MagicMock()
            result.returncode = 0
            result.stdout = b"\x00\x01\x02\x03"
            result.stderr = b""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            result = filtered_push._git_raw(str(tmp_path), "show", "HEAD:foo")
        assert result.returncode == 0
        assert isinstance(result.stdout, bytes)
        assert result.stdout == b"\x00\x01\x02\x03"


# ---------------------------------------------------------------------------
# NACK item 3: restage must actually stage content (git add, not intent-to-add)
# ---------------------------------------------------------------------------


class TestRestageActuallyStages:
    """``_restage_blocked_files`` must ``git add`` the path — NOT
    ``git add --intent-to-add`` which only records the filename and
    leaves the index contentless so the next role's ``git commit``
    produces an empty diff."""

    def test_no_intent_to_add_flag_passed(self, tmp_path):
        calls: list[list] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            result.stderr = b"" if kwargs.get("text") is not True else ""
            result.stdout = b"hello" if "show" in cmd else ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            filtered_push._restage_blocked_files(str(tmp_path), ["blocked.py"], "cafef00d" * 5)

        add_calls = [c for c in calls if "add" in c]
        assert add_calls, "expected at least one git add invocation"
        for call in add_calls:
            assert "--intent-to-add" not in call, (
                "git add must not use --intent-to-add — the index "
                "needs actual blob content, not a bare filename."
            )
            # Sanity: the target path should be in the invocation.
            assert "blocked.py" in call


# ---------------------------------------------------------------------------
# NACK item 4: warn-only path carries pulled_commits + filtered=false
# ---------------------------------------------------------------------------


class TestWarnOnlyObservabilityParity:
    """EGG_AGENT_RESTRICTIONS_ENFORCE=false with blocked own files must
    still surface the auto-filter response schema so downstream tooling
    sees ``filtered=false`` + ``pulled_commits`` consistently."""

    def test_warn_only_with_blocked_own_has_filtered_false(self, client):
        session = _make_session("coder")
        files = ["docs/guide.md"]  # would be blocked under enforce
        attributed = AttributedPushRange(
            files=[AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder")],
            commits=[_OWN_SHA],
            attribution={_OWN_SHA: "coder"},
        )
        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(
                patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"})
            )
            response = _do_push(client)

        assert response.status_code == 200
        body = _body(response)
        # Warn-only passes through, but the auto-filter schema fields
        # must still be populated.
        assert body.get("filtered") is False
        assert "pulled_commits" in body
        assert body.get("nothing_to_push") is False or not body.get("nothing_to_push")

    def test_warn_only_with_pulled_commits_emits_them(self, client):
        """Pulled commit summary should reach the response in warn-only mode too."""
        session = _make_session("coder")
        files = ["docs/guide.md", "tests/test_main.py"]
        attributed = AttributedPushRange(
            files=[
                AttributedFile(path="docs/guide.md", commit_sha=_OWN_SHA, authored_by="coder"),
                AttributedFile(
                    path="tests/test_main.py", commit_sha=_PULLED_SHA, authored_by="tester"
                ),
            ],
            commits=[_OWN_SHA, _PULLED_SHA],
            attribution={_OWN_SHA: "coder", _PULLED_SHA: "tester"},
        )
        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(
                patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "false"})
            )
            response = _do_push(client)

        assert response.status_code == 200
        body = _body(response)
        pulled = body.get("pulled_commits") or []
        assert any(
            p.get("sha") == _PULLED_SHA and p.get("author_role") == "tester" for p in pulled
        ), f"Expected pulled_commits in warn-only response, got {pulled!r}"


# ---------------------------------------------------------------------------
# NACK item 5: audit log carries attribution_fallback flag
# ---------------------------------------------------------------------------


class TestAttributionFallbackAuditLog:
    """The ``push_all_blocked_no_op`` audit event must carry the
    ``attribution_fallback`` flag so operators can tell whether the
    fail-closed branch fired for a legitimate all-blocked push or
    because attribution was unavailable."""

    def test_audit_log_includes_attribution_fallback_true(self, client):
        session = _make_session("coder")
        files = ["docs/guide.md"]
        # Empty attribution triggers the fallback.
        attributed = AttributedPushRange(files=[], commits=[], attribution={})

        audit_calls = []

        def fake_audit(event_type, action, success=True, details=None, **kwargs):
            audit_calls.append((event_type, success, dict(details or {})))

        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            _stack.enter_context(patch.object(gateway, "audit_log", side_effect=fake_audit))

            response = _do_push(client)

        assert response.status_code == 200

        no_op_events = [
            details
            for (event_type, _success, details) in audit_calls
            if event_type == "push_all_blocked_no_op"
        ]
        assert no_op_events, (
            f"Expected at least one push_all_blocked_no_op audit event, got {audit_calls!r}"
        )
        # Exactly one such event per push; it must carry the flag.
        assert any(ev.get("attribution_fallback") is True for ev in no_op_events), (
            f"Expected attribution_fallback=True in push_all_blocked_no_op "
            f"event details, got {no_op_events!r}"
        )

    def test_audit_log_attribution_fallback_false_for_real_all_blocked(self, client):
        """The flag is False when attribution is available but all own files are blocked."""
        session = _make_session("coder")
        files = ["docs/guide.md"]
        attributed = AttributedPushRange(
            files=[AttributedFile(path=files[0], commit_sha=_OWN_SHA, authored_by="coder")],
            commits=[_OWN_SHA],
            attribution={_OWN_SHA: "coder"},
        )

        audit_calls = []

        def fake_audit(event_type, action, success=True, details=None, **kwargs):
            audit_calls.append((event_type, success, dict(details or {})))

        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            _stack.enter_context(patch.object(gateway, "audit_log", side_effect=fake_audit))

            response = _do_push(client)

        assert response.status_code == 200
        no_op_events = [
            details
            for (event_type, _success, details) in audit_calls
            if event_type == "push_all_blocked_no_op"
        ]
        assert no_op_events
        assert all(ev.get("attribution_fallback") is False for ev in no_op_events), (
            f"Expected attribution_fallback=False for real all-blocked push, got {no_op_events!r}"
        )

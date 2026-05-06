"""Regression tests for #1882 NACK invariants ported to #2039 rejection model.

Items 1 and 5 below were ported to assert the new ``restricted_path_modified``
rejection shape after #2039 removed the silent-strip + nothing_to_push arms;
the original NACK items 2 and 3 (binary-safe restage + actual-content staging)
described helpers in ``filtered_push.py`` that have been deleted along with the
rewrite path, so those tests went away with the dead code.

Covered:

1. Security hole in mixed-rewrite fallback (now: 403 reject path).
   When ``get_attributed_changed_files_in_push`` returns empty / errored
   but the partition discovers blocked own files, the handler MUST
   return 403 ``restricted_path_modified``.

4. Warn-only observability parity.
   When ``EGG_AGENT_RESTRICTIONS_ENFORCE=false`` and blocked own files
   exist, the success response body MUST still carry the
   ``filtered=false`` / ``pulled_commits`` / ``excluded_files`` fields
   so downstream tooling sees the consistent schema #1882 promised.

5. audit-log ``attribution_fallback`` flag presence on the rejection
   path when attribution was unavailable — so operators can tell the
   fail-closed short-circuit fired.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure ``gateway/`` is on sys.path so module-top imports below work
# even when pytest collects this file in isolation (the other tests
# that insert the path happen to run before us in full-suite collection
# but not in narrow pytest invocations).
_gateway_path = Path(__file__).parent.parent
if str(_gateway_path) not in sys.path:
    sys.path.insert(0, str(_gateway_path))

import git_client
import pytest
import session_manager
from git_client import AttributedFile, AttributedPushRange
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
        # Legacy ``gateway.check_file_restrictions`` patch removed in
        # #2489 — the gateway no longer calls that function from
        # ``git_push`` (see ``test_filtered_push_blocked_modify``).
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
# NACK item 1: attribution_fallback rejects with 403 (#2039)
# ---------------------------------------------------------------------------


class TestAttributionFallbackSecurityHole:
    """Empty-attribution fallback must reject with 403, not silently succeed.

    If ``get_attributed_changed_files_in_push`` returns an empty range
    (error flag set or zero commits) but the pusher has blocked files,
    the original #1882 bug routed the flow into the rewriter with an
    empty commit list — which returned success and pushed HEAD
    verbatim, leaking blocked files to origin.  Under #2039 the handler
    rejects with 403 ``restricted_path_modified`` and never reaches the
    rewrite path (which has been removed entirely).
    """

    def test_empty_commits_with_blocked_files_rejected(self, client):
        """empty commits + blocked files → 403."""
        session = _make_session("coder")
        files = ["docs/guide.md"]  # coder cannot write docs
        # attribution_range has empty commits (the original bug path).
        attributed = AttributedPushRange(files=[], commits=[], attribution={})

        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))

            response = _do_push(client)

        assert response.status_code == 403, response.data
        body = _body(response)
        assert body["error"] == "restricted_path_modified"
        assert body["role"] == "coder"
        assert "docs/guide.md" in body["blocked_paths"]
        assert body.get("attribution_fallback") is True

    def test_error_attribution_with_blocked_files_rejected(self, client):
        """attribution error + blocked files → 403."""
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
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))

            response = _do_push(client)

        assert response.status_code == 403, response.data
        body = _body(response)
        assert body["error"] == "restricted_path_modified"
        assert set(body["blocked_paths"]) == set(files)

    def test_empty_attribution_with_allowed_files_is_plain_push(self, client):
        """Empty attribution + ALL allowed files → plain push."""
        session = _make_session("coder")
        files = ["src/main.py"]  # coder can write src
        attributed = AttributedPushRange(files=[], commits=[], attribution={})

        with contextlib.ExitStack() as _stack:
            for _p in _patches_for(session, files, attributed):
                _stack.enter_context(_p)
            _stack.enter_context(patch.dict(os.environ, {"EGG_AGENT_RESTRICTIONS_ENFORCE": "true"}))
            response = _do_push(client)

        assert response.status_code == 200
        body = _body(response)
        # No blocked own files → plain push, filtered=false.
        assert body["filtered"] is False
        assert body.get("nothing_to_push") is False


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
    """The ``push_denied_restricted_path_modified`` audit event must
    carry the ``attribution_fallback`` flag so operators can tell whether
    the rejection fired for a legitimate restricted-path push or because
    attribution was unavailable (#2039)."""

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

        assert response.status_code == 403

        reject_events = [
            details
            for (event_type, _success, details) in audit_calls
            if event_type == "push_denied_restricted_path_modified"
        ]
        assert reject_events, (
            f"Expected at least one push_denied_restricted_path_modified audit event, "
            f"got {audit_calls!r}"
        )
        assert any(ev.get("attribution_fallback") is True for ev in reject_events), (
            f"Expected attribution_fallback=True in push_denied_restricted_path_modified "
            f"event details, got {reject_events!r}"
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

        assert response.status_code == 403
        reject_events = [
            details
            for (event_type, _success, details) in audit_calls
            if event_type == "push_denied_restricted_path_modified"
        ]
        assert reject_events
        assert all(ev.get("attribution_fallback") is False for ev in reject_events), (
            f"Expected attribution_fallback=False for real all-blocked push, got {reject_events!r}"
        )

    def test_unregistered_fallback_audit_uses_blocked_paths_key(self, client):
        """The ``push_authorship_unregistered_fallback`` event must report
        the blocked file set under the ``blocked_paths`` key (renamed from
        ``excluded_files`` in #2043 to align with the
        ``push_denied_restricted_path_modified`` shape).  Locks the rename
        in so a typo or revert in ``gateway.py`` would fail loudly."""
        session = _make_session("coder")
        files = ["docs/guide.md"]
        # Empty attribution → unregistered_files == own_files, fires the
        # ``push_authorship_unregistered_fallback`` audit event.
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

        assert response.status_code == 403

        unregistered_events = [
            details
            for (event_type, _success, details) in audit_calls
            if event_type == "push_authorship_unregistered_fallback"
        ]
        assert unregistered_events, (
            f"Expected at least one push_authorship_unregistered_fallback audit event, "
            f"got {audit_calls!r}"
        )
        for ev in unregistered_events:
            assert "blocked_paths" in ev, (
                f"Expected 'blocked_paths' key in unregistered fallback audit event, "
                f"got keys {sorted(ev.keys())!r}"
            )
            assert "excluded_files" not in ev, (
                f"Legacy 'excluded_files' key must not appear in unregistered "
                f"fallback audit event, got {ev!r}"
            )
            assert "docs/guide.md" in ev["blocked_paths"]

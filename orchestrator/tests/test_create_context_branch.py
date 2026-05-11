"""Smoke tests for ``GatewayClient.create_context_branch`` (#2548).

The exhaustive test surface (idempotency, error semantics, base_branch
honoring, session-tagging) is owned by the tester role per the contract
task task-1-3. These tests pin the *coder*-side invariants that the
implementation contract promises so a future refactor can't quietly
break them:

1. Branch absent on origin → push from ``base_sha`` and return ``True``.
2. Branch already exists at exactly ``base_sha`` → return ``True`` without
   re-pushing (idempotent).
3. Branch exists at a different SHA → raise ``GatewayError`` (refuse to
   overwrite divergent state).
4. ``base_branch`` is honored (not hardcoded to ``main``).
5. Synthetic session is registered with the context branch shape and is
   cleaned up on every exit (success, raised, missing-base).
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from gateway_client import GatewayClient, GatewayError, SessionInfo


@pytest.fixture
def gateway_client():
    return GatewayClient(
        gateway_host="localhost",
        gateway_port=19848,
        launcher_secret="test-secret",
        timeout=5,
    )


def _session_info(token: str = "synthetic-tok") -> SessionInfo:
    now = datetime.now()
    return SessionInfo(
        session_token=token,
        container_id="temp",
        container_ip=None,
        mode="public",
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )


class TestCreateContextBranchSuccess:
    def test_pushes_sha_refspec_when_branch_absent(self, gateway_client):
        """First-run path: context branch doesn't exist on origin yet, so
        push from the resolved base SHA. Mirrors the SHA-based push
        rationale from create_slice_integration_branch (#2393)."""
        base_sha = "deadbeef" * 5
        push_payloads: list[dict] = []

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref.endswith("/context"):
                return None  # branch absent on origin
            return base_sha

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_payloads.append(dict(data or {}))
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=fake_get_remote_branch_sha,
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_context_branch(
                "issue-2548",
                "/repo",
                base_branch="main",
            )

        assert ok is True
        assert len(push_payloads) == 1
        push = push_payloads[0]
        assert push["refspec"] == f"{base_sha}:refs/heads/egg/issue-2548/context", (
            "refspec source side must be the resolved SHA, not the base name"
        )
        assert push["remote"] == "origin"

    def test_honors_non_main_base_branch(self, gateway_client):
        """The base branch is parameterised — ``main`` must not be hardcoded.
        Pin that ``develop``, ``master``, qualifier-suffixed bases all
        flow through unchanged."""
        base_sha = "12345678" * 5
        push_payloads: list[dict] = []
        fetch_args_seen: list[list] = []

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref.endswith("/context"):
                return None
            return base_sha

        def fake_fetch_branch(*args, **kwargs):
            fetch_args_seen.append(kwargs.get("args") or [])
            return True

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_payloads.append(dict(data or {}))
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", side_effect=fake_fetch_branch),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=fake_get_remote_branch_sha,
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_context_branch(
                "issue-1234",
                "/repo",
                base_branch="develop",
            )

        assert ok is True
        # Fetched the configured base, not ``main``.
        assert fetch_args_seen == [["+refs/heads/develop:refs/remotes/origin/develop"]]
        assert push_payloads[0]["refspec"] == f"{base_sha}:refs/heads/egg/issue-1234/context"

    def test_idempotent_when_branch_already_at_base_sha(self, gateway_client):
        """Idempotency: if the branch already exists at exactly ``base_sha``,
        return True without re-pushing. Required by the task-1-1 acceptance
        criterion ('Calling it twice in a row is idempotent')."""
        base_sha = "cafebabe" * 5
        push_invoked: list[bool] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_invoked.append(True)
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            # Both lookups (base + context) resolve to the same SHA.
            patch.object(gateway_client, "get_remote_branch_sha", return_value=base_sha),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_context_branch(
                "issue-2548",
                "/repo",
                base_branch="main",
            )

        assert ok is True
        assert push_invoked == [], (
            "must not re-push when context branch is already at base SHA "
            "(idempotent — task-1-1 acceptance)"
        )


class TestCreateContextBranchFailures:
    def test_raises_when_existing_branch_diverges(self, gateway_client):
        """Critical semantic difference from create_slice_integration_branch:
        if the context branch exists at a different SHA than ``base_sha``,
        raise. The context branch is orchestrator-owned; any divergence is
        a bug, not work-in-progress to preserve.

        The raised exception is the typed :class:`ContextBranchDiverged`
        subclass of ``GatewayError`` so the context-PR hook can catch it
        specifically and route through ``_recover_existing_context_pr``
        — the post-push, pre-contract-persist failure mode (PR #2575
        review issue 1).  Existing callers that catch ``GatewayError``
        continue to work via the subclass relationship.
        """
        from gateway_client import ContextBranchDiverged

        base_sha = "deadbeef" * 5
        existing_sha = "feedface" * 5  # diverged

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref.endswith("/context"):
                return existing_sha
            return base_sha

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info("div-tok")),
            patch.object(gateway_client, "delete_session", return_value=True) as delete_spy,
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=fake_get_remote_branch_sha,
            ),
            patch.object(gateway_client, "_make_request") as req_spy,
        ):
            with pytest.raises(ContextBranchDiverged) as excinfo:
                gateway_client.create_context_branch(
                    "issue-2548",
                    "/repo",
                    base_branch="main",
                )
            err = excinfo.value
            assert "different SHA" in str(err) or "already exists" in str(err), (
                "raised message must explain the divergence"
            )
            # Subclass-of-GatewayError preserves the broad-catch
            # contract for callers that don't care about the subtype.
            assert isinstance(err, GatewayError)
            # Recovery-side metadata for the hook caller.
            assert err.context_branch == "egg/issue-2548/context"
            assert err.existing_sha == existing_sha
            assert err.base_sha == base_sha
            assert err.base_branch == "main"
            req_spy.assert_not_called()

        # Synthetic session must still be cleaned up on the error path.
        delete_spy.assert_called_once_with("div-tok")

    def test_raises_when_base_missing_on_origin(self, gateway_client):
        """If ``ls-remote`` returns no SHA for the base branch, raise
        instead of issuing a malformed push."""
        with (
            patch.object(
                gateway_client, "register_session", return_value=_session_info("orphan-tok")
            ),
            patch.object(gateway_client, "delete_session", return_value=True) as delete_spy,
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(gateway_client, "get_remote_branch_sha", return_value=None),
            patch.object(gateway_client, "_make_request") as req_spy,
        ):
            with pytest.raises(GatewayError):
                gateway_client.create_context_branch(
                    "issue-2548",
                    "/repo",
                    base_branch="main",
                )
            req_spy.assert_not_called()

        delete_spy.assert_called_once_with("orphan-tok")

    def test_rejects_empty_pipeline_id_or_base_branch(self, gateway_client):
        """ValueError is raised when called with empty inputs — caller bug,
        not a runtime push attempt."""
        with patch.object(gateway_client, "register_session") as reg:
            with pytest.raises(ValueError):
                gateway_client.create_context_branch("", "/repo", base_branch="main")
            with pytest.raises(ValueError):
                gateway_client.create_context_branch("p", "/repo", base_branch="")
            reg.assert_not_called()


class TestCreateContextBranchSession:
    def test_synthetic_session_carries_context_branch_and_role(self, gateway_client):
        """The synthetic session must be tagged with the context branch
        shape so the gateway's _CONTEXT_BRANCH_RE exemption (#2548) is
        eligible to fire. Same trust pattern as the slice integration
        branch path."""
        register_spy = MagicMock(return_value=_session_info())

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref.endswith("/context"):
                return None
            return "1234abcd" * 5

        with (
            patch.object(gateway_client, "register_session", side_effect=register_spy),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=fake_get_remote_branch_sha,
            ),
            patch.object(
                gateway_client,
                "_make_request",
                return_value={"success": True, "data": {}},
            ),
        ):
            ok = gateway_client.create_context_branch(
                "pipe-1",
                "/repo",
                base_branch="main",
                agent_role="orchestrator",
                mode="private",
            )

        assert ok is True
        register_spy.assert_called_once()
        kwargs = register_spy.call_args.kwargs
        assert kwargs["synthetic"] is True
        assert kwargs["branch"] == "egg/pipe-1/context"
        assert kwargs["agent_role"] == "orchestrator"
        assert kwargs["mode"] == "private"
        assert kwargs["pipeline_id"] == "pipe-1"

"""Regression tests for ``GatewayClient.create_slice_integration_branch``.

#2393 — the original implementation built the push refspec source side
from the ``parent_branch`` name, which resolved against the
orchestrator's per-pipeline worktree's local refs.  That worktree is
checked out on ``<branch>/work`` and has no local ref matching
``<parent_branch>``, only ``refs/remotes/origin/<parent_branch>`` after
a fetch — so every slice push failed with ``src refspec X does not
match any``.  The fix:

1. Fetch the parent ref so its commit object is in the local odb.
2. Resolve the parent to a SHA on origin via ``git ls-remote``.
3. Push ``<sha>:refs/heads/<integration_branch>`` — pushing a SHA
   bypasses local ref-name resolution entirely.

These tests pin that contract so a future refactor can't quietly
re-introduce the local-ref dependency.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from gateway_client import GatewayClient, SessionInfo


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


def _stub_helpers(
    client: GatewayClient,
    *,
    parent_sha: str | None = "deadbeef" * 5,
    fetch_returns: bool = True,
):
    """Return a context manager stack that stubs the helpers
    ``create_slice_integration_branch`` depends on, leaving only the
    push call going through ``_make_request`` (so tests can pin the
    push refspec)."""
    return (
        patch.object(client, "register_session", return_value=_session_info()),
        patch.object(client, "delete_session", return_value=True),
        patch.object(client, "fetch_branch", return_value=fetch_returns),
        patch.object(client, "get_remote_branch_sha", return_value=parent_sha),
    )


class TestCreateSliceIntegrationBranchSuccess:
    def test_pushes_sha_refspec_resolved_from_origin(self, gateway_client):
        """Regression for #2393: refspec source side must be the SHA from
        ``ls-remote``, not the parent branch name (which has no local ref
        in the orchestrator's per-pipeline worktree)."""
        parent_sha = "deadbeef" * 5
        push_payloads: list[dict] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            assert endpoint == "/api/v1/git/push", (
                f"only /api/v1/git/push should reach _make_request once helpers "
                f"are stubbed; got {endpoint}"
            )
            push_payloads.append(dict(data or {}))
            return {"success": True, "data": {}}

        register, delete, fetch, ls = _stub_helpers(gateway_client, parent_sha=parent_sha)
        with (
            register,
            delete,
            fetch,
            ls,
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393/slice-1",
                parent_branch="egg/issue-2393",
            )

        assert ok is True
        assert len(push_payloads) == 1
        push = push_payloads[0]
        assert push["refspec"] == f"{parent_sha}:refs/heads/egg/issue-2393/slice-1", (
            "refspec source side must be the resolved SHA, not the parent branch name"
        )
        assert push["remote"] == "origin"
        assert push["repo_path"] == "/repo"

    def test_fetch_runs_before_sha_lookup_runs_before_push(self, gateway_client):
        """Defensive fetch must run before ``ls-remote`` so the parent's
        commit object is reachable in the local odb before we issue the
        SHA-based push (``git push <sha>:...`` requires it locally)."""
        order: list[str] = []
        parent_sha = "abc12345" * 5

        def fake_fetch_branch(*args, **kwargs):
            order.append("fetch")
            assert kwargs["args"] == [
                "+refs/heads/egg/issue-2393:refs/remotes/origin/egg/issue-2393"
            ]
            return True

        def fake_get_remote_branch_sha(*args, **kwargs):
            order.append("ls-remote")
            return parent_sha

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                order.append("push")
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
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393/slice-1",
                parent_branch="egg/issue-2393",
            )

        assert ok is True
        assert order == ["fetch", "ls-remote", "push"]


class TestCreateSliceIntegrationBranchFailures:
    def test_returns_false_when_parent_missing_on_origin(self, gateway_client):
        """If ``ls-remote`` returns no SHA (parent doesn't exist on
        origin), fail fast — don't issue a push that would emit git's
        confusing ``src refspec X does not match any``."""
        push_invoked: list[bool] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_invoked.append(True)
            return {"success": True, "data": {}}

        register, delete, fetch, ls = _stub_helpers(gateway_client, parent_sha=None)
        with (
            register,
            delete,
            fetch,
            ls,
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393/slice-1",
                parent_branch="egg/issue-2393",
            )

        assert ok is False
        assert push_invoked == [], "push must not be issued when parent is not on origin"

    def test_fetch_returning_false_does_not_short_circuit(self, gateway_client):
        """A defensive fetch that returns ``False`` must not abort
        branch creation — the parent's object may already be locally
        reachable from a prior step, so we still attempt the SHA
        lookup and push.

        Note: this pins control flow only.  In production, a transient
        fetch failure on a fresh worktree leaves the parent SHA absent
        from the local odb, and the subsequent push will fail with
        ``fatal: bad object``.  The semantic guarantee is "don't
        short-circuit on fetch failure", not "fetch failure is
        recoverable in all cases"."""
        push_invoked: list[bool] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_invoked.append(True)
            return {"success": True, "data": {}}

        register, delete, fetch, ls = _stub_helpers(
            gateway_client,
            parent_sha="cafef00d" * 5,
            fetch_returns=False,
        )
        with (
            register,
            delete,
            fetch,
            ls,
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393/slice-1",
                parent_branch="egg/issue-2393",
            )

        assert ok is True
        assert push_invoked == [True]

    def test_push_failure_returns_false_and_cleans_up_session(self, gateway_client):
        """If the push request raises, the function must return False
        and still delete the synthetic session (no gateway-side leak)."""
        delete_calls: list[str] = []

        def fake_delete(token):
            delete_calls.append(token)
            return True

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                raise RuntimeError("simulated push failure")
            return {"success": True, "data": {}}

        with (
            patch.object(
                gateway_client, "register_session", return_value=_session_info("tok-push")
            ),
            patch.object(gateway_client, "delete_session", side_effect=fake_delete),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                return_value="feedface" * 5,
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393/slice-1",
                parent_branch="egg/issue-2393",
            )

        assert ok is False
        assert delete_calls == ["tok-push"]


class TestCreateSliceIntegrationBranchShortCircuits:
    def test_empty_branch_returns_false_without_calls(self, gateway_client):
        with (
            patch.object(gateway_client, "_make_request") as mock_req,
            patch.object(gateway_client, "fetch_branch") as mock_fetch,
            patch.object(gateway_client, "get_remote_branch_sha") as mock_ls,
        ):
            assert (
                gateway_client.create_slice_integration_branch(
                    "pipe-1",
                    "/repo",
                    integration_branch="",
                    parent_branch="egg/issue-2393",
                )
                is False
            )
            assert (
                gateway_client.create_slice_integration_branch(
                    "pipe-1",
                    "/repo",
                    integration_branch="egg/issue-2393/slice-1",
                    parent_branch="",
                )
                is False
            )
            mock_req.assert_not_called()
            mock_fetch.assert_not_called()
            mock_ls.assert_not_called()

    def test_integration_eq_parent_short_circuits(self, gateway_client):
        """No-op: integration branch already exists at parent's tip.
        Must not even fetch / ls-remote."""
        with (
            patch.object(gateway_client, "_make_request") as mock_req,
            patch.object(gateway_client, "fetch_branch") as mock_fetch,
            patch.object(gateway_client, "get_remote_branch_sha") as mock_ls,
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393",
                parent_branch="egg/issue-2393",
            )
        assert ok is True
        mock_req.assert_not_called()
        mock_fetch.assert_not_called()
        mock_ls.assert_not_called()


class TestCreateSliceIntegrationBranchSession:
    def test_synthetic_session_carries_integration_branch_and_role(self, gateway_client):
        """Push session must be ``synthetic=True`` and tagged with the
        integration branch + agent role — those feed the gateway's
        slice integration-branch exemption (#2368) and branch-ownership
        check."""
        register_spy = MagicMock(return_value=_session_info())

        with (
            patch.object(gateway_client, "register_session", side_effect=register_spy),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                return_value="1234abcd" * 5,
            ),
            patch.object(
                gateway_client,
                "_make_request",
                return_value={"success": True, "data": {}},
            ),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393/slice-1",
                parent_branch="egg/issue-2393",
                agent_role="coder",
                mode="private",
            )

        assert ok is True
        register_spy.assert_called_once()
        kwargs = register_spy.call_args.kwargs
        assert kwargs["synthetic"] is True
        assert kwargs["branch"] == "egg/issue-2393/slice-1"
        assert kwargs["agent_role"] == "coder"
        assert kwargs["mode"] == "private"
        assert kwargs["pipeline_id"] == "pipe-1"

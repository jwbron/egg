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
from unittest.mock import MagicMock, call, patch

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
        SHA-based push (``git push <sha>:...`` requires it locally).

        Also pins the #2512 ordering: the existence-check ls-remote on
        the integration branch happens AFTER the parent ls-remote and
        BEFORE the push.  When integration branch is absent (or its
        tip equals parent_sha), no extra fetch is needed and the push
        proceeds as a fast-forward.
        """
        order: list[str] = []
        parent_sha = "abc12345" * 5

        def fake_fetch_branch(*args, **kwargs):
            order.append("fetch")
            assert kwargs["args"] == [
                "+refs/heads/egg/issue-2393:refs/remotes/origin/egg/issue-2393"
            ]
            return True

        def fake_get_remote_branch_sha(*args, **kwargs):
            ref = args[2] if len(args) >= 3 else kwargs.get("ref")
            order.append(f"ls-remote:{ref}")
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
        assert order == [
            "fetch",
            "ls-remote:refs/heads/egg/issue-2393",
            "ls-remote:refs/heads/egg/issue-2393/slice-1",
            "push",
        ]


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

    def test_one_session_shared_across_fetch_lsremote_and_push(self, gateway_client):
        """#2398: the fetch, ls-remote, and push must reuse a single
        synthetic session — exactly one ``register_session`` and one
        ``delete_session`` per call, with the same bearer token
        forwarded to ``fetch_branch``, ``get_remote_branch_sha``, and
        the push request."""
        register_spy = MagicMock(return_value=_session_info("shared-tok"))
        delete_spy = MagicMock(return_value=True)
        fetch_spy = MagicMock(return_value=True)
        ls_spy = MagicMock(return_value="cafebabe" * 5)

        push_tokens: list[str | None] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_tokens.append(kwargs.get("bearer_token"))
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", side_effect=register_spy),
            patch.object(gateway_client, "delete_session", side_effect=delete_spy),
            patch.object(gateway_client, "fetch_branch", side_effect=fetch_spy),
            patch.object(gateway_client, "get_remote_branch_sha", side_effect=ls_spy),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-1",
                "/repo",
                integration_branch="egg/issue-2393/slice-1",
                parent_branch="egg/issue-2393",
            )

        assert ok is True
        assert register_spy.call_count == 1, "session must be registered exactly once"
        assert delete_spy.call_args_list == [call("shared-tok")], (
            "session must be deleted exactly once with the shared token"
        )
        assert fetch_spy.call_args.kwargs.get("bearer_token") == "shared-tok"
        assert ls_spy.call_args.kwargs.get("bearer_token") == "shared-tok"
        assert push_tokens == ["shared-tok"]

    def test_session_cleaned_up_when_parent_missing(self, gateway_client):
        """With the shared-session refactor (#2398) ``register_session``
        runs before the ``ls-remote`` SHA lookup, so a missing parent
        still has to clean up the session it just registered."""
        register_spy = MagicMock(return_value=_session_info("orphan-tok"))
        delete_spy = MagicMock(return_value=True)

        with (
            patch.object(gateway_client, "register_session", side_effect=register_spy),
            patch.object(gateway_client, "delete_session", side_effect=delete_spy),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(gateway_client, "get_remote_branch_sha", return_value=None),
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
            )

        assert ok is False
        assert register_spy.call_count == 1
        assert delete_spy.call_args_list == [call("orphan-tok")]


class TestCreateSliceIntegrationBranchRestartRecovery:
    """#2512 — restart_phase recovery when slice integration branch
    already exists on origin with prior-run commits.

    The pre-#2512 implementation issued ``parent_sha:refs/heads/<int>``
    unconditionally, which is rejected as non-fast-forward when the
    branch already carries commits descended from parent.  decision-3
    advertises "Committed work is preserved on the per-role branch —
    'Retry phase' restarts with artifacts intact"; that promise was
    only honored on the first slice spawn.  These tests pin the
    restart-recovery path: detect the existing branch, verify its
    tip descends from the current parent, and short-circuit success
    instead of pushing.
    """

    def test_existing_branch_descended_from_parent_is_preserved(self, gateway_client):
        """Issue #2512 reproduction: slice-1 branch on origin contains
        coder/tester commits descended from parent (cancel_task with
        cleanup=false followed by restart_phase).  ``create_slice_
        integration_branch`` must detect this and return True without
        pushing — the prior commits are exactly what restart_phase
        promises to preserve."""
        parent_sha = "8a76c30d" * 5  # parent-branch tip
        existing_sha = "0c5c6697" * 5  # slice-1 tip from prior run

        push_invoked: list[bool] = []
        merge_base_calls: list[dict] = []

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref == "refs/heads/egg/issue-2474":
                return parent_sha
            if ref == "refs/heads/egg/issue-2474/slice-1":
                return existing_sha
            return None

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_invoked.append(True)
            elif endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
                # operation=merge-base --is-ancestor parent existing
                # parent IS an ancestor → returncode 0 / success
                return {"success": True, "data": {"returncode": 0}}
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
            ok = gateway_client.create_slice_integration_branch(
                "issue-2474",
                "/repo",
                integration_branch="egg/issue-2474/slice-1",
                parent_branch="egg/issue-2474",
            )

        assert ok is True, "must short-circuit success when prior work descends from parent"
        assert push_invoked == [], (
            "must NOT push when integration branch already descends from "
            "parent — that push would be rejected as non-fast-forward"
        )
        assert len(merge_base_calls) == 1
        mb = merge_base_calls[0]
        assert mb["operation"] == "merge-base"
        assert mb["args"] == ["--is-ancestor", parent_sha, existing_sha], (
            "ancestry direction matters: parent must be ancestor of existing tip"
        )

    def test_existing_branch_diverged_falls_through_to_push(self, gateway_client):
        """If parent_sha is NOT an ancestor of the existing slice tip
        (genuinely diverged history), fall through to the push so
        origin's rejection surfaces as a clear signal.  Better than
        silently overwriting unknown work."""
        parent_sha = "deadbeef" * 5
        existing_sha = "feedface" * 5  # diverged

        push_invoked: list[dict] = []

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref.endswith("/slice-1"):
                return existing_sha
            return parent_sha

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_invoked.append(dict(data or {}))
            elif endpoint == "/api/v1/git/execute":
                # not-ancestor → gateway returns 500 with returncode=1
                raise GatewayError(
                    "git merge-base failed",
                    status_code=500,
                    details={"returncode": 1, "stdout": "", "stderr": ""},
                )
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
            ok = gateway_client.create_slice_integration_branch(
                "pipe-div",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert ok is True, "diverged history → push proceeds and (here) succeeds"
        assert len(push_invoked) == 1, "must attempt the push when histories diverge"
        assert push_invoked[0]["refspec"] == (f"{parent_sha}:refs/heads/egg/issue-1/slice-1")

    def test_diverged_history_push_rejection_surfaces_as_failure(self, gateway_client):
        """The whole point of falling through to the push on diverged
        history is that origin's non-fast-forward rejection surfaces
        as a clear ``ok is False`` signal (rather than silently
        overwriting unknown work).  Pin that the rejection actually
        propagates and that the synthetic session is still cleaned up
        in the failure path."""
        parent_sha = "deadbeef" * 5
        existing_sha = "feedface" * 5  # diverged

        register_spy = MagicMock(return_value=_session_info("diverged-tok"))
        delete_spy = MagicMock(return_value=True)

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref.endswith("/slice-1"):
                return existing_sha
            return parent_sha

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                # Origin rejects the non-fast-forward push.
                raise GatewayError(
                    "git push failed",
                    status_code=500,
                    details={
                        "returncode": 1,
                        "stderr": "! [rejected] (non-fast-forward)",
                    },
                )
            if endpoint == "/api/v1/git/execute":
                # not-ancestor → returncode=1
                raise GatewayError(
                    "git merge-base failed",
                    status_code=500,
                    details={"returncode": 1, "stdout": "", "stderr": ""},
                )
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", side_effect=register_spy),
            patch.object(gateway_client, "delete_session", side_effect=delete_spy),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=fake_get_remote_branch_sha,
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-div-rej",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert ok is False, (
            "non-fast-forward rejection on diverged history must surface as "
            "ok=False — that's the user-visible signal the operator needs"
        )
        assert delete_spy.call_args_list == [call("diverged-tok")], (
            "synthetic session must still be cleaned up in the rejection path"
        )

    def test_existing_branch_equal_to_parent_skips_recovery_path(self, gateway_client):
        """When the integration branch already exists at exactly
        parent_sha (e.g., the slice was created in a prior run but
        no agent commits landed), the recovery path is unnecessary —
        the push is a no-op fast-forward and we don't run merge-base."""
        sha = "cafebabe" * 5
        merge_base_calls: list[dict] = []
        push_invoked: list[bool] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_invoked.append(True)
            elif endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(gateway_client, "get_remote_branch_sha", return_value=sha),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            ok = gateway_client.create_slice_integration_branch(
                "pipe-eq",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert ok is True
        assert push_invoked == [True], "push still runs (it's a fast-forward no-op)"
        assert merge_base_calls == [], (
            "must not run merge-base when existing tip is already at parent_sha"
        )

    def test_existing_branch_absent_skips_recovery_path(self, gateway_client):
        """First-run / branch-absent path: ``get_remote_branch_sha``
        returns None for the integration ref because the branch
        doesn't exist on origin yet.  The recovery path is unnecessary
        — no extra fetch, no merge-base, just the regular push.

        This pins that the new ``if existing_sha and …`` guard
        short-circuits correctly on the first conjunct and doesn't
        accidentally start dereferencing a None ``existing_sha``.
        """
        parent_sha = "abc12345" * 5

        merge_base_calls: list[dict] = []
        push_invoked: list[bool] = []
        fetch_calls: list[tuple] = []

        def fake_fetch_branch(pipeline_id, repo_path, *, args=None, **kwargs):
            fetch_calls.append(tuple(args or ()))
            return True

        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref == "refs/heads/egg/issue-1":
                return parent_sha  # parent exists
            if ref == "refs/heads/egg/issue-1/slice-1":
                return None  # integration branch absent (first run)
            return None

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/push":
                push_invoked.append(True)
            elif endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
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
                "pipe-firstrun",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert ok is True
        assert push_invoked == [True], "push must run on the first-run / branch-absent path"
        assert merge_base_calls == [], (
            "must not run merge-base when integration branch doesn't exist on origin"
        )
        # Only the parent-branch fetch should run; the integration-
        # branch fetch is gated on ``existing_sha`` being truthy.
        assert len(fetch_calls) == 1, (
            "branch-absent path must skip the integration-branch fetch — "
            "no need to fetch a branch that doesn't exist"
        )
        # Tight assertion on the exact parent refspec — guards against a
        # hypothetical "fetched the integration branch instead of the
        # parent" regression that a substring check on ``"egg/issue-1"``
        # (a prefix of ``"egg/issue-1/slice-1"``) wouldn't catch.
        assert fetch_calls[0][0] == ("+refs/heads/egg/issue-1:refs/remotes/origin/egg/issue-1")


def _ancestry_make_request(
    ancestry: dict[tuple[str, str], bool],
    *,
    push_invoked: list[dict],
    merge_base_args: list[list[str]],
):
    """Build a ``_make_request`` side effect that answers
    ``merge-base --is-ancestor`` from an ``(ancestor, descendant) -> bool``
    map and records pushes.

    A ``True`` mapping returns the gateway's success shape; a ``False`` (or
    missing) mapping raises the ``returncode=1`` ``GatewayError`` that
    ``_sha_is_ancestor`` interprets as "not an ancestor" — exactly how the
    real gateway surfaces ``git merge-base --is-ancestor``'s non-zero exit.
    """

    def fake_make_request(endpoint, method=None, data=None, **kwargs):
        if endpoint == "/api/v1/git/push":
            push_invoked.append(dict(data or {}))
            return {"success": True, "data": {}}
        if endpoint == "/api/v1/git/execute":
            args = list((data or {}).get("args", []))
            merge_base_args.append(args)
            ancestor, descendant = args[1], args[2]
            if ancestry.get((ancestor, descendant), False):
                return {"success": True, "data": {"returncode": 0}}
            raise GatewayError(
                "git merge-base failed",
                status_code=500,
                details={"returncode": 1, "stdout": "", "stderr": ""},
            )
        return {"success": True, "data": {}}

    return fake_make_request


class TestCreateSliceIntegrationBranchResumeInPlace:
    """#2947 — crash / ``restart_phase`` mid-slice over a branch that
    already carries the slice's own commits while the parent advanced
    *additively*.

    The #2512 fast-path only preserves a branch whose tip *descends from*
    the parent. The crash-mid-slice incident (2026-06-02
    ``issue-2908-impl2`` slice-3) produced the unhandled fourth state:
    committed slice work + an additively-advanced parent + no live agents.
    The #2914 "treat as fresh to force re-spawn" path then routed it back
    into ``create_slice_integration_branch``, whose plain
    ``parent_sha:refs/heads/<int>`` push is non-fast-forward → slice
    failed → whole phase cascade-failed.

    These tests pin the resume-in-place path: gated on the slice's own
    recorded fork base (#2871), recognise the additive-advance shape and
    short-circuit success WITHOUT pushing (preserving the committed work),
    while still falling through to the rejection for genuinely diverged
    history, parent rebases, and slices with no recorded base.
    """

    _PIPE = "issue-2908-impl2"
    _INT = "egg/issue-2908-impl2/slice-3"
    _PARENT = "egg/issue-2908-impl2/slice-2"

    def _remotes(self, parent_sha: str, existing_sha: str | None):
        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref == f"refs/heads/{self._INT}":
                return existing_sha
            if ref == f"refs/heads/{self._PARENT}":
                return parent_sha
            return None

        return fake_get_remote_branch_sha

    def _create(
        self,
        gateway_client,
        fake_make_request,
        remotes,
        *,
        base_sha,
        delete_calls: list[str] | None = None,
        session_token: str = "tok-resume",
    ):
        def fake_delete(token):
            if delete_calls is not None:
                delete_calls.append(token)
            return True

        with (
            patch.object(
                gateway_client, "register_session", return_value=_session_info(session_token)
            ),
            patch.object(gateway_client, "delete_session", side_effect=fake_delete),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(gateway_client, "get_remote_branch_sha", side_effect=remotes),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            return gateway_client.create_slice_integration_branch(
                self._PIPE,
                "/repo",
                integration_branch=self._INT,
                parent_branch=self._PARENT,
                integration_base_sha=base_sha,
            )

    def test_additively_advanced_parent_with_own_commits_resumes_in_place(self, gateway_client):
        """The incident itself: slice has its own commits built on the
        recorded base, parent advanced additively (base ancestor of both,
        neither tip an ancestor of the other). Must preserve the branch
        and return True WITHOUT pushing."""
        base_sha = "b0b0b0b0" * 5  # slice-2 tip when slice-3 was created
        existing_sha = "e1e1e1e1" * 5  # slice-3 tip (documenter commits)
        parent_sha = "a2a2a2a2" * 5  # slice-2 tip, advanced additively
        push_invoked: list[dict] = []
        merge_base_args: list[list[str]] = []
        delete_calls: list[str] = []
        ancestry = {
            (parent_sha, existing_sha): False,  # #2512: parent NOT ancestor of existing
            (base_sha, existing_sha): True,  # base IS ancestor of existing (our branch)
            (base_sha, parent_sha): True,  # base IS ancestor of parent (additive advance)
        }
        ok = self._create(
            gateway_client,
            _ancestry_make_request(
                ancestry, push_invoked=push_invoked, merge_base_args=merge_base_args
            ),
            self._remotes(parent_sha, existing_sha),
            base_sha=base_sha,
            delete_calls=delete_calls,
            session_token="tok-resume-incident",
        )

        assert ok is True, "additive-advance + own commits must resume in place"
        assert push_invoked == [], (
            "must NOT push — a parent-tip push here is non-fast-forward and "
            "would discard / fail the slice's committed work"
        )
        # #2512 parent->existing first, then base->existing, then base->parent.
        assert [a[1:] for a in merge_base_args] == [
            [parent_sha, existing_sha],
            [base_sha, existing_sha],
            [base_sha, parent_sha],
        ]
        # Symmetric coverage for ``test_push_failure_returns_false_and_cleans
        # _up_session``: the new ``return True`` lives inside the outer
        # ``try``, so ``delete_session`` must still fire from the ``finally``
        # on this success path. Pinning it here freezes that — a future
        # refactor that moved the early-return outside the try (or skipped
        # session cleanup) would leak the synthetic session on every
        # resume-in-place recovery.
        assert delete_calls == ["tok-resume-incident"], (
            "synthetic session must be deleted on the resume-in-place "
            "success path (the ``finally`` must still fire despite the "
            "early ``return True``)"
        )

    def test_parent_rebase_falls_through_to_push(self, gateway_client):
        """If the parent was *rewritten* (rebase) rather than advanced
        additively, the recorded base is no longer an ancestor of the
        parent tip. The harder rewrite class is out of scope — fall
        through to the push so the rejection surfaces."""
        base_sha = "b0b0b0b0" * 5
        existing_sha = "e1e1e1e1" * 5
        parent_sha = "a2a2a2a2" * 5
        push_invoked: list[dict] = []
        merge_base_args: list[list[str]] = []
        ancestry = {
            (parent_sha, existing_sha): False,
            (base_sha, existing_sha): True,  # branch is genuinely ours
            (base_sha, parent_sha): False,  # parent rebased the base out of history
        }
        ok = self._create(
            gateway_client,
            _ancestry_make_request(
                ancestry, push_invoked=push_invoked, merge_base_args=merge_base_args
            ),
            self._remotes(parent_sha, existing_sha),
            base_sha=base_sha,
        )

        assert ok is True, "diverged push (here) succeeds in the stub"
        assert len(push_invoked) == 1, "rebase class must fall through to the push"
        assert push_invoked[0]["refspec"] == f"{parent_sha}:refs/heads/{self._INT}"
        assert [a[1:] for a in merge_base_args] == [
            [parent_sha, existing_sha],
            [base_sha, existing_sha],
            [base_sha, parent_sha],
        ]

    def test_branch_not_descended_from_recorded_base_falls_through(self, gateway_client):
        """A branch that does not even contain its own recorded base is
        unknown/garbage work — fall through to the push (don't silently
        preserve it). The base->parent check is short-circuited."""
        base_sha = "b0b0b0b0" * 5
        existing_sha = "feedface" * 5  # unrelated tip
        parent_sha = "a2a2a2a2" * 5
        push_invoked: list[dict] = []
        merge_base_args: list[list[str]] = []
        ancestry = {
            (parent_sha, existing_sha): False,
            (base_sha, existing_sha): False,  # existing does not descend from our base
        }
        # The push (here) succeeds in the stub; what this test pins is the
        # *fall-through* — return value is incidental.
        self._create(
            gateway_client,
            _ancestry_make_request(
                ancestry, push_invoked=push_invoked, merge_base_args=merge_base_args
            ),
            self._remotes(parent_sha, existing_sha),
            base_sha=base_sha,
        )

        assert len(push_invoked) == 1, "unknown work must fall through to the push"
        # Symmetric with ``test_parent_rebase_falls_through_to_push``: pin the
        # refspec so a hypothetical regression that pushed the wrong SHA in
        # the garbage-branch case (e.g. ``existing_sha`` or ``base_sha``
        # instead of ``parent_sha``) would surface here.
        assert push_invoked[0]["refspec"] == f"{parent_sha}:refs/heads/{self._INT}"
        # base->parent must NOT run — the conjunction short-circuits once
        # base->existing is False.
        assert [a[1:] for a in merge_base_args] == [
            [parent_sha, existing_sha],
            [base_sha, existing_sha],
        ]

    def test_absent_recorded_base_degrades_to_prior_behaviour(self, gateway_client):
        """``integration_base_sha=None`` (slices provisioned before #2871,
        or whose base was never recorded) must NOT enter the resume-in-
        place path — it degrades to the pre-#2947 push-and-surface
        behaviour, running only the #2512 ancestry check."""
        existing_sha = "e1e1e1e1" * 5
        parent_sha = "a2a2a2a2" * 5
        push_invoked: list[dict] = []
        merge_base_args: list[list[str]] = []
        ancestry = {(parent_sha, existing_sha): False}
        self._create(
            gateway_client,
            _ancestry_make_request(
                ancestry, push_invoked=push_invoked, merge_base_args=merge_base_args
            ),
            self._remotes(parent_sha, existing_sha),
            base_sha=None,
        )

        assert len(push_invoked) == 1, "no recorded base → push (prior behaviour)"
        assert [a[1:] for a in merge_base_args] == [[parent_sha, existing_sha]], (
            "with no recorded base the resume-in-place merge-base probes "
            "must not run — only the #2512 check"
        )

    def test_unstarted_branch_at_base_still_fast_forwards(self, gateway_client):
        """An un-started branch still sitting exactly at its recorded base
        (no slice commits yet) must take the fast-forward push path that
        advances it to the new parent tip — NOT resume-in-place, which
        would pin it to the stale base. The ``existing_sha != base`` guard
        is what keeps it off the resume path."""
        base_sha = "b0b0b0b0" * 5
        existing_sha = base_sha  # un-started: tip still at the creation base
        parent_sha = "a2a2a2a2" * 5
        push_invoked: list[dict] = []
        merge_base_args: list[list[str]] = []
        ancestry = {(parent_sha, existing_sha): False}  # parent not ancestor of base
        ok = self._create(
            gateway_client,
            _ancestry_make_request(
                ancestry, push_invoked=push_invoked, merge_base_args=merge_base_args
            ),
            self._remotes(parent_sha, existing_sha),
            base_sha=base_sha,
        )

        assert ok is True
        assert len(push_invoked) == 1, "un-started branch must fast-forward to parent"
        assert push_invoked[0]["refspec"] == f"{parent_sha}:refs/heads/{self._INT}"
        # Only the #2512 probe runs; the resume-in-place probes are gated
        # off by ``existing_sha != integration_base_sha``.
        assert [a[1:] for a in merge_base_args] == [[parent_sha, existing_sha]]


class TestIsSliceBranchMergedIntoParent:
    """#2549 — detect whether a slice's PR has already merged into its
    parent. This is the inverse of the #2512 restart-recovery check:
    when ``existing_sha`` (slice tip on origin) is reachable from
    ``parent_sha`` (parent tip on origin), the slice's commits are
    already in the parent and any attempt to (re)create the slice's
    integration branch via ``parent_sha:refs/heads/<slice>`` would be
    rejected as non-fast-forward.

    The bootstrap reconciliation pass and the run-loop race-protection
    check in ``routes/pipelines._run_implement_phase_slices`` both rely
    on this signal — a False from here lets the slice run normally; a
    True short-circuits the slice to COMPLETE.
    """

    def _setup_remotes(self, parent_sha: str | None, existing_sha: str | None):
        def fake_get_remote_branch_sha(pipeline_id, repo_path, ref, **kwargs):
            if ref.endswith("/slice-1"):
                return existing_sha
            return parent_sha

        return fake_get_remote_branch_sha

    def test_returns_true_when_slice_tip_is_ancestor_of_parent(self, gateway_client):
        """The literal #2549 repro: slice-1 PR was merged into the
        work branch; the slice-1 ref still exists on origin at its
        pre-merge tip, and the work tip now has the merge commit on
        top. ``existing_sha`` is reachable from ``parent_sha`` →
        merged → True."""
        parent_sha = "f3c16e3b" * 5  # work tip after merge
        existing_sha = "ea591ec1" * 5  # pre-merge slice-1 tip

        merge_base_calls: list[dict] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
                # existing IS reachable from parent → returncode 0 → True
                return {"success": True, "data": {"returncode": 0}}
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=self._setup_remotes(parent_sha, existing_sha),
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "issue-2474-v2",
                "/repo",
                integration_branch="egg/issue-2474-v2/slice-1",
                parent_branch="egg/issue-2474-v2/work",
            )

        assert merged is True
        assert len(merge_base_calls) == 1
        mb = merge_base_calls[0]
        assert mb["args"] == ["--is-ancestor", existing_sha, parent_sha], (
            "ancestry direction is the inverse of #2512: existing must be "
            "ancestor of parent, signalling 'slice merged into parent'"
        )

    def test_empty_branch_at_creation_base_is_not_merged(self, gateway_client):
        """#2871 — the slice integration branch never received a commit:
        its tip on origin still equals the recorded ``integration_base_sha``
        (the parent SHA it was forked at). When the parent later advances,
        that tip is trivially an ancestor of the new parent tip — but this
        is *un-started* work, not merged work. The empty-branch guard must
        return False *before* the merge-base call so the slice still runs."""
        base_sha = "abcd1234" * 5  # parent tip at fork == empty branch tip
        parent_sha = "ef99ef99" * 5  # parent has since advanced past the fork

        merge_base_calls: list[dict] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
                # If the guard failed to short-circuit, the empty branch's
                # base IS an ancestor of the advanced parent → would
                # wrongly report merged.
                return {"success": True, "data": {"returncode": 0}}
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                # Integration branch tip == base_sha (never advanced).
                side_effect=self._setup_remotes(parent_sha, base_sha),
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "issue-2777-replan",
                "/repo",
                integration_branch="egg/issue-2777-replan/slice-1",
                parent_branch="egg/issue-2777-replan/work",
                integration_base_sha=base_sha,
            )

        assert merged is False, (
            "an empty slice branch (tip still at its creation base) is "
            "un-started work, not merged — #2871 false-COMPLETE regression"
        )
        assert merge_base_calls == [], (
            "the empty-branch guard must short-circuit before the merge-base ancestry call runs"
        )

    def test_recorded_base_does_not_block_genuinely_merged_branch(self, gateway_client):
        """#2871 guard is additive: when the slice branch tip has moved
        past its recorded base (it carries slice commits) and is an
        ancestor of the parent, the merged signal still fires True. The
        base-SHA check only suppresses the *empty* case."""
        base_sha = "11112222" * 5  # fork base
        existing_sha = "33334444" * 5  # slice tip with commits (!= base)
        parent_sha = "55556666" * 5  # parent that has merged the slice

        merge_base_calls: list[dict] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
                return {"success": True, "data": {"returncode": 0}}
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=self._setup_remotes(parent_sha, existing_sha),
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "p",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
                integration_base_sha=base_sha,
            )

        assert merged is True
        assert len(merge_base_calls) == 1

    def test_returns_false_when_slice_tip_diverged_from_parent(self, gateway_client):
        """Genuinely diverged history (slice has commits parent doesn't,
        or vice versa) → not merged. Caller falls through to the regular
        create path so origin's rejection (if any) surfaces normally."""
        parent_sha = "deadbeef" * 5
        existing_sha = "feedface" * 5

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/execute":
                # not-ancestor → returncode 1
                raise GatewayError(
                    "git merge-base failed",
                    status_code=500,
                    details={"returncode": 1, "stdout": "", "stderr": ""},
                )
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=self._setup_remotes(parent_sha, existing_sha),
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "p",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert merged is False

    def test_returns_false_when_integration_branch_absent(self, gateway_client):
        """First-run / branch-deleted case: ``ls-remote`` returns no
        SHA for the integration branch → can't be merged → False.
        Crucially does NOT run merge-base (no SHA to compare)."""
        parent_sha = "abc12345" * 5

        merge_base_calls: list[dict] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=self._setup_remotes(parent_sha, existing_sha=None),
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "p",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert merged is False
        assert merge_base_calls == [], (
            "must not run merge-base when one of the SHAs is unresolvable"
        )

    def test_returns_false_when_parent_branch_absent(self, gateway_client):
        """If the parent branch can't be resolved on origin we have
        nothing to compare against — return False rather than guess."""
        existing_sha = "feedface" * 5

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=self._setup_remotes(parent_sha=None, existing_sha=existing_sha),
            ),
            patch.object(gateway_client, "_make_request", return_value={"success": True}),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "p",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert merged is False

    def test_returns_false_when_branches_equal(self, gateway_client):
        """Tips equal → no-op state, neither merged nor diverged. Let
        the caller fall through to the regular fast-forward no-op path."""
        sha = "cafebabe" * 5

        merge_base_calls: list[dict] = []

        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            if endpoint == "/api/v1/git/execute":
                merge_base_calls.append(dict(data or {}))
            return {"success": True, "data": {}}

        with (
            patch.object(gateway_client, "register_session", return_value=_session_info()),
            patch.object(gateway_client, "delete_session", return_value=True),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(gateway_client, "get_remote_branch_sha", return_value=sha),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "p",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert merged is False
        assert merge_base_calls == [], "no merge-base when tips are equal"

    def test_session_cleaned_up_on_success_and_failure(self, gateway_client):
        """The synthetic session must be deleted via ``delete_session``
        on both the success path and any exception path — symmetric
        with ``create_slice_integration_branch``."""
        parent_sha = "deadbeef" * 5
        existing_sha = "feedface" * 5

        delete_calls: list = []

        def _delete(token):
            delete_calls.append(token)
            return True

        # Force an exception in merge-base so we exercise the failure path.
        def fake_make_request(endpoint, method=None, data=None, **kwargs):
            raise RuntimeError("kaboom")

        with (
            patch.object(
                gateway_client, "register_session", return_value=_session_info("merged-tok")
            ),
            patch.object(gateway_client, "delete_session", side_effect=_delete),
            patch.object(gateway_client, "fetch_branch", return_value=True),
            patch.object(
                gateway_client,
                "get_remote_branch_sha",
                side_effect=self._setup_remotes(parent_sha, existing_sha),
            ),
            patch.object(gateway_client, "_make_request", side_effect=fake_make_request),
        ):
            merged = gateway_client.is_slice_branch_merged_into_parent(
                "p",
                "/repo",
                integration_branch="egg/issue-1/slice-1",
                parent_branch="egg/issue-1",
            )

        assert merged is False
        assert delete_calls == ["merged-tok"], (
            "synthetic session must be cleaned up even when the call raises"
        )


class TestShaIsAncestor:
    """Unit tests for the ``_sha_is_ancestor`` helper that backs the
    #2512 restart-recovery detection."""

    def test_returns_true_on_success_response(self, gateway_client):
        with patch.object(
            gateway_client,
            "_make_request",
            return_value={"success": True, "data": {"returncode": 0}},
        ) as mock_req:
            ok = gateway_client._sha_is_ancestor(
                "pipe-1", "/repo", "aaaa", "bbbb", bearer_token="tok"
            )
        assert ok is True
        call_data = mock_req.call_args.kwargs["data"]
        assert call_data["operation"] == "merge-base"
        assert call_data["args"] == ["--is-ancestor", "aaaa", "bbbb"]
        assert mock_req.call_args.kwargs["bearer_token"] == "tok"

    def test_returns_false_on_returncode_1(self, gateway_client):
        """``merge-base --is-ancestor`` exits 1 when the relation does
        not hold — surfaces as 500 from the gateway with
        ``returncode: 1`` in the error details."""

        def fake(*args, **kwargs):
            raise GatewayError(
                "git merge-base failed",
                status_code=500,
                details={"returncode": 1, "stderr": ""},
            )

        with patch.object(gateway_client, "_make_request", side_effect=fake):
            ok = gateway_client._sha_is_ancestor("p", "/r", "a", "b")
        assert ok is False

    def test_returns_false_on_unexpected_error(self, gateway_client):
        """Any failure that isn't ``returncode: 1`` (network, missing
        object, gateway down) is treated as non-ancestor so callers
        fall through to the conservative path rather than incorrectly
        preserving prior work on a broken check."""

        def fake(*args, **kwargs):
            raise GatewayError("connection refused")

        with patch.object(gateway_client, "_make_request", side_effect=fake):
            ok = gateway_client._sha_is_ancestor("p", "/r", "a", "b")
        assert ok is False

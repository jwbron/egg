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

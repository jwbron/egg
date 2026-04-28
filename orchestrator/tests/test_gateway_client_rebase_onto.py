"""Tests for ``orchestrator.gateway_client.GatewayClient.rebase_onto`` (#2137 TASK-5-2).

The reconciler in :mod:`orchestrator.stacked_pr_reconciler` accepts a
``rebase_onto: Callable[[str, str, str], bool]`` injection so the
production wiring can swap a fake in unit tests. The orchestrator-side
bridge in :mod:`orchestrator.gateway_client` is what production code
actually injects: it (a) calls
:func:`gateway.git_client.build_rebase_onto_args` to build the canonical
argv, (b) registers a temp session with the gateway, (c) submits the
rebase via the existing per-agent ``/api/v1/git`` endpoint, and (d)
deletes the temp session.

The reconciler test (``test_stacked_pr_reconciler.py``) exercises the
reconciler logic against a callable injection — it does NOT exercise
this bridge code path. These tests close that gap.

Coverage:

* Argv-validation failure (e.g. empty ``branch``) returns ``False``
  WITHOUT registering a session — no temp session leak on the
  fast-fail path.
* HTTP error from the gateway is caught and surfaces as ``False`` —
  the reconciler counts it as ``rebases_failed``. The session token
  is still released so the gateway doesn't accumulate dangling
  sessions across reconciliation cycles.
* Successful path returns ``True``, sends ``operation=rebase`` with
  the canonical argv to ``/api/v1/git``, and tears the session down.
* The temp ``container_id`` is namespaced with the pipeline id to
  avoid collisions across pipelines.
* The default ``agent_role`` is ``coder`` (matches the existing
  per-agent allowlist), and callers can override it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# sys.path setup — orchestrator + shared.
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"
for _p in (_orchestrator_path, _shared_path, _project_root):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
from gateway_client import GatewayClient  # noqa: E402


@pytest.fixture
def client() -> GatewayClient:
    """Bare client; HTTP calls patched per-test."""
    return GatewayClient(
        gateway_host="localhost",
        gateway_port=19999,  # not bound — every test patches the network
        launcher_secret="test-secret",
        timeout=5,
    )


def _fake_session(token: str = "tok-rebase") -> MagicMock:
    """Mimic ``RegisteredSession`` shape used by ``register_session``."""
    s = MagicMock()
    s.session_token = token
    return s


class TestArgvValidation:
    """Validation rejection short-circuits before any HTTP traffic."""

    def test_empty_branch_returns_false_without_register(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session") as register,
            patch.object(client, "_make_request") as make_request,
            patch.object(client, "delete_session") as delete,
        ):
            ok = client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="",
                new_base="main",
                old_base="develop",
            )
        assert ok is False
        register.assert_not_called()
        make_request.assert_not_called()
        delete.assert_not_called()

    def test_whitespace_new_base_returns_false_without_register(
        self, client: GatewayClient
    ) -> None:
        with (
            patch.object(client, "register_session") as register,
            patch.object(client, "_make_request") as make_request,
        ):
            ok = client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="feature",
                new_base="   ",
                old_base="develop",
            )
        assert ok is False
        register.assert_not_called()
        make_request.assert_not_called()


class TestHappyPath:
    """A well-formed call submits the canonical argv via the agent endpoint."""

    def test_returns_true_and_calls_git_endpoint(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session", return_value=_fake_session()) as register,
            patch.object(client, "_make_request", return_value={"success": True}) as make_request,
            patch.object(client, "delete_session", return_value=True) as delete,
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            ok = client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="egg/issue-2137/slice-2",
                new_base="egg/issue-2137",
                old_base="egg/issue-2137/slice-1",
            )

        assert ok is True
        register.assert_called_once()
        make_request.assert_called_once()
        delete.assert_called_once_with("tok-rebase")

        # Inspect the /git request that was submitted.
        endpoint, *_ = make_request.call_args.args
        kwargs = make_request.call_args.kwargs
        assert endpoint == "/api/v1/git"
        assert kwargs["method"] == "POST"
        payload = kwargs["data"]
        assert payload["operation"] == "rebase"
        assert payload["repo_path"] == "/repo"
        # canonical shape: --onto NEW OLD BRANCH
        assert payload["args"] == [
            "--onto",
            "egg/issue-2137",
            "egg/issue-2137/slice-1",
            "egg/issue-2137/slice-2",
        ]
        assert kwargs["bearer_token"] == "tok-rebase"

    def test_default_agent_role_is_coder(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session", return_value=_fake_session()) as register,
            patch.object(client, "_make_request", return_value={"success": True}),
            patch.object(client, "delete_session", return_value=True),
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="feature",
                new_base="main",
                old_base="develop",
            )
        assert register.call_args.kwargs["agent_role"] == "coder"

    def test_agent_role_override_propagates(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session", return_value=_fake_session()) as register,
            patch.object(client, "_make_request", return_value={"success": True}),
            patch.object(client, "delete_session", return_value=True),
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="feature",
                new_base="main",
                old_base="develop",
                agent_role="tester",
            )
        assert register.call_args.kwargs["agent_role"] == "tester"

    def test_pipeline_id_namespaces_temp_container_id(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session", return_value=_fake_session()) as register,
            patch.object(client, "_make_request", return_value={"success": True}),
            patch.object(client, "delete_session", return_value=True),
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="feature",
                new_base="main",
                old_base="develop",
            )

        container_id = register.call_args.kwargs["container_id"]
        # Must include the pipeline id so two simultaneous reconciler
        # invocations on different pipelines do not collide.
        assert "issue-2137" in container_id


class TestErrorPaths:
    """HTTP errors are caught; session is always released."""

    def test_http_error_returns_false_and_releases_session(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session", return_value=_fake_session()),
            patch.object(client, "_make_request", side_effect=Exception("502 bad gateway")),
            patch.object(client, "delete_session", return_value=True) as delete,
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            ok = client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="feature",
                new_base="main",
                old_base="develop",
            )
        assert ok is False
        delete.assert_called_once_with("tok-rebase")

    def test_register_failure_returns_false(self, client: GatewayClient) -> None:
        with (
            patch.object(
                client,
                "register_session",
                side_effect=Exception("session register failed"),
            ),
            patch.object(client, "_make_request") as make_request,
            patch.object(client, "delete_session") as delete,
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            ok = client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="feature",
                new_base="main",
                old_base="develop",
            )
        assert ok is False
        # register_session blew up before _make_request; session was
        # never created so delete_session must NOT be called.
        make_request.assert_not_called()
        delete.assert_not_called()

    def test_delete_session_exception_does_not_propagate(self, client: GatewayClient) -> None:
        """A failing teardown must not turn a successful rebase into a
        failure — the rebase already happened on the gateway."""
        with (
            patch.object(client, "register_session", return_value=_fake_session()),
            patch.object(client, "_make_request", return_value={"success": True}),
            patch.object(
                client,
                "delete_session",
                side_effect=Exception("delete blew up"),
            ),
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            ok = client.rebase_onto(
                "issue-2137",
                "/repo",
                branch="feature",
                new_base="main",
                old_base="develop",
            )
        # The function intentionally swallows delete_session exceptions
        # in its ``finally`` clause; a successful HTTP rebase is still
        # reported as success.
        assert ok is True


class TestPayloadShape:
    """The /git payload shape must match what the gateway expects."""

    def test_payload_keys(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session", return_value=_fake_session()),
            patch.object(client, "_make_request", return_value={"success": True}) as make_request,
            patch.object(client, "delete_session", return_value=True),
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            client.rebase_onto(
                "p",
                "/r",
                branch="b",
                new_base="n",
                old_base="o",
            )
        payload = make_request.call_args.kwargs["data"]
        # The /git endpoint expects exactly these three keys.
        assert set(payload.keys()) == {"operation", "args", "repo_path"}

    def test_args_field_is_list_of_strings(self, client: GatewayClient) -> None:
        with (
            patch.object(client, "register_session", return_value=_fake_session()),
            patch.object(client, "_make_request", return_value={"success": True}) as make_request,
            patch.object(client, "delete_session", return_value=True),
            patch.object(GatewayClient, "self_ip", new="127.0.0.1"),
        ):
            client.rebase_onto("p", "/r", branch="b", new_base="n", old_base="o")
        args = make_request.call_args.kwargs["data"]["args"]
        assert isinstance(args, list)
        assert all(isinstance(a, str) for a in args)

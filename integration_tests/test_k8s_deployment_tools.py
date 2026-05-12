"""Integration regression guards for the #1759 deployment MCP routes.

TASK-4-1 acceptance: every orchestrator endpoint added for the
Kubernetes deployment (``/api/v1/deployment/context``,
``/validate-manifests``, ``/prune-worktrees``,
``/validate-network-isolation``, ``/rebuild-and-rollout``, ``/logs``)
— plus the progress-stream GET route — MUST enforce
``@require_lifecycle_secret`` parity with #1769. A regression that
leaves any of them open would let an in-cluster caller trigger the same
kind of bypass the HITL auto-approval incident exposed.

These tests hit the running orchestrator with NO auth and with an
obviously-wrong bearer and assert:

1. The route exists (status ≠ 404).
2. Auth fails closed — either 401 (secret is configured, header
   missing/wrong) or 503 (orchestrator has no secret configured; the
   decorator still short-circuits before the handler runs).
3. The new tools are never silently bypassed by a path-routing
   regression such as the 1769 one.

The tests deliberately do NOT exercise the happy path — the lifecycle
secret is not exposed through the shared ``EggStack`` fixture
(``integration_tests/conftest.py``) because each deployment controls it
out-of-band (k8s Secret). Happy-path behaviour is covered exhaustively
by the route-level unit tests in
``orchestrator/tests/test_deployment_routes.py`` which can mock the
kubernetes/subprocess layer; this integration file focuses on the thing
unit tests can't catch: a live Flask blueprint that someone forgot to
decorate.
"""

from __future__ import annotations

import pytest
import requests

pytestmark = pytest.mark.integration


# The bearer token must be rejected: pick one that obviously isn't the
# configured lifecycle secret. A random string with a distinctive prefix
# makes it easy to spot in logs if a test regresses.
_BOGUS_BEARER = "bogus-deployment-test-bearer-do-not-accept"


# (path, http_method, json_body_factory) triples for every new orchestrator
# route added by #1759. ``json_body_factory`` is called per-request so the
# same body shape is reused across tests without leaking mutation.
_DEPLOYMENT_ROUTES: list[tuple[str, str, dict | None]] = [
    ("/api/v1/deployment/context", "GET", None),
    (
        "/api/v1/deployment/validate-manifests",
        "POST",
        {"paths": ["k8s/base/gateway.yaml"]},
    ),
    ("/api/v1/deployment/prune-worktrees", "POST", {"dry_run": True}),
    (
        "/api/v1/deployment/validate-network-isolation",
        "POST",
        {"dry_run": True},
    ),
    ("/api/v1/deployment/rebuild-and-rollout", "POST", {"wait": False}),
    # Progress-stream GET: stream_id doesn't need to resolve -- auth must
    # fire before any stream lookup. Use an obviously-synthetic id so a
    # regression that inverts the decorator / handler order would surface
    # a 404 here while the ones above still look correct.
    (
        "/api/v1/deployment/rebuild-and-rollout/streams/nonexistent-stream-0000",
        "GET",
        None,
    ),
    # /logs GET: query-string service is required by the handler, but
    # @require_lifecycle_secret must fire before the handler runs, so
    # the param doesn't need to match the allowlist for auth-reject
    # regression coverage.
    ("/api/v1/deployment/logs?service=gateway", "GET", None),
]


def _call(
    orchestrator_url: str,
    method: str,
    path: str,
    *,
    json_body: dict | None,
    headers: dict[str, str] | None,
) -> requests.Response:
    url = f"{orchestrator_url}{path}"
    kwargs: dict = {"timeout": 15, "headers": headers or {}}
    if json_body is not None:
        kwargs["json"] = json_body
    return requests.request(method, url, **kwargs)


def _assert_auth_rejected(resp: requests.Response, context: str) -> None:
    """Helper: a well-behaved lifecycle endpoint rejects with 401 or 503.

    503 is acceptable because a deployment that forgot to mount
    EGG_LIFECYCLE_SECRET fails *closed* by design (see
    ``orchestrator/lifecycle_auth.py``). Either outcome demonstrates the
    decorator is wired up.
    """
    assert resp.status_code in (401, 503), (
        f"{context}: expected 401 or 503 (lifecycle auth reject path), "
        f"got {resp.status_code}: {resp.text[:500]}"
    )
    # Also guard the shape: a body that happens to 401 for an unrelated
    # reason (e.g. gateway hairpinning) shouldn't count.
    try:
        body = resp.json()
    except ValueError:
        pytest.fail(f"{context}: non-JSON body on {resp.status_code}: {resp.text[:500]}")
    assert body.get("success") is False, f"{context}: expected 'success: false', got body: {body}"
    msg = (body.get("message") or "").lower()
    # The decorator's two error messages + the 503 misconfig message.
    # Accept any of them; the point is that it came from the auth layer.
    assert any(
        hint in msg
        for hint in (
            "authorization",
            "lifecycle",
            "egg_lifecycle_secret",
            "misconfigured",
        )
    ), f"{context}: message doesn't look like an auth-layer reject: {msg!r}"


# ---------------------------------------------------------------------------
# 401 / 503 regression coverage on every new route
# ---------------------------------------------------------------------------


class TestDeploymentRoutesRequireLifecycleSecret:
    """#1769 parity: every new deployment route rejects unauthenticated calls."""

    @pytest.mark.parametrize(
        ("path", "method", "body"),
        _DEPLOYMENT_ROUTES,
        ids=[r[0] for r in _DEPLOYMENT_ROUTES],
    )
    def test_no_auth_header_is_rejected(
        self,
        orchestrator_url: str,
        path: str,
        method: str,
        body: dict | None,
    ) -> None:
        """Missing Authorization header on a deployment route must fail closed."""
        resp = _call(orchestrator_url, method, path, json_body=body, headers=None)
        assert resp.status_code != 404, (
            f"Route {method} {path} returned 404 -- the deployment blueprint "
            "is not registered or the URL was renamed. Regression."
        )
        _assert_auth_rejected(resp, f"{method} {path} (no auth)")

    @pytest.mark.parametrize(
        ("path", "method", "body"),
        _DEPLOYMENT_ROUTES,
        ids=[r[0] for r in _DEPLOYMENT_ROUTES],
    )
    def test_invalid_bearer_is_rejected(
        self,
        orchestrator_url: str,
        path: str,
        method: str,
        body: dict | None,
    ) -> None:
        """A Bearer token that isn't the lifecycle secret must be rejected."""
        resp = _call(
            orchestrator_url,
            method,
            path,
            json_body=body,
            headers={"Authorization": f"Bearer {_BOGUS_BEARER}"},
        )
        assert resp.status_code != 404, (
            f"Route {method} {path} returned 404 -- the deployment blueprint "
            "is not registered or the URL was renamed. Regression."
        )
        _assert_auth_rejected(resp, f"{method} {path} (bogus bearer)")

    @pytest.mark.parametrize(
        ("path", "method", "body"),
        _DEPLOYMENT_ROUTES,
        ids=[r[0] for r in _DEPLOYMENT_ROUTES],
    )
    def test_non_bearer_scheme_is_rejected(
        self,
        orchestrator_url: str,
        path: str,
        method: str,
        body: dict | None,
    ) -> None:
        """``Authorization: <secret>`` (no Bearer prefix) must be rejected.

        This is the exact shape the #1769 incident normalized around -- the
        decorator must require the ``Bearer `` prefix even if the bare
        secret value would otherwise compare equal.
        """
        resp = _call(
            orchestrator_url,
            method,
            path,
            json_body=body,
            headers={"Authorization": _BOGUS_BEARER},
        )
        _assert_auth_rejected(resp, f"{method} {path} (no Bearer prefix)")


# A discovery test that enumerated `/api/v1/_routes` to cross-check the
# manual `_DEPLOYMENT_ROUTES` fixture used to live here but xfailed
# unconditionally because the orchestrator does not (and is not planned
# to) expose a route-listing endpoint. The parametrized regression suite
# above is the actual coverage; the discovery test added no signal.
# Removed in PR #2602.

"""Integration regression guards for the #1759 deployment MCP routes.

TASK-4-1 acceptance: the five new orchestrator endpoints added for the
Kubernetes deployment (``/api/v1/deployment/context``,
``/validate-manifests``, ``/prune-worktrees``,
``/validate-network-isolation``, ``/rebuild-and-rollout``) — plus the
progress-stream GET route — MUST enforce ``@require_lifecycle_secret``
parity with #1769. A regression that leaves any of them open would let
an in-cluster caller trigger the same kind of bypass the HITL
auto-approval incident exposed.

These tests hit the running orchestrator with NO auth and with an
obviously-wrong bearer and assert:

1. The route exists (status ≠ 404).
2. Auth fails closed — either 401 (secret is configured, header
   missing/wrong) or 503 (orchestrator has no secret configured; the
   decorator still short-circuits before the handler runs).
3. The new tools are never silently bypassed by a path-routing
   regression such as the 1769 one.

The tests deliberately do NOT exercise the happy path — the lifecycle
secret is not exposed through ``LocalPipelineStack`` because each
deployment controls it out-of-band (k8s Secret / compose env). Happy-path
behaviour is covered exhaustively by the route-level unit tests in
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
    # Tag every request with the opt-out sentinel — these tests
    # deliberately exercise the unauthenticated / bogus-bearer paths
    # and the `_auto_inject_lifecycle_auth` conftest fixture would
    # otherwise overwrite the test's auth shape with a valid bearer.
    merged_headers = dict(headers or {})
    merged_headers.setdefault("X-Egg-Test-Skip-Auto-Auth", "true")
    kwargs: dict = {"timeout": 15, "headers": merged_headers}
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


# ---------------------------------------------------------------------------
# Discovery guard -- if someone adds a sixth /api/v1/deployment/* route
# without updating _DEPLOYMENT_ROUTES, this fails loudly.
# ---------------------------------------------------------------------------


class TestDeploymentRouteCoverage:
    """Enumerate routes under /api/v1/deployment/ and compare to the fixture.

    Uses the orchestrator's own route-listing endpoint when available.  When
    the endpoint isn't exposed (older orchestrator builds), the test
    xfails cleanly rather than polluting the suite.
    """

    def test_all_deployment_routes_are_covered(self, orchestrator_url: str) -> None:
        # Try a best-effort route discovery endpoint; if it doesn't exist,
        # we accept the coverage gap and only rely on the parametrized
        # regression tests above.
        # Discovery call — go through the same opt-out path as `_call`
        # above (this test class is explicitly auth-agnostic).
        resp = requests.get(
            f"{orchestrator_url}/api/v1/_routes",
            timeout=10,
            headers={"X-Egg-Test-Skip-Auto-Auth": "true"},
        )
        if resp.status_code in (404, 405):
            pytest.xfail("Orchestrator does not expose /_routes; discovery skipped")

        try:
            body = resp.json()
        except ValueError:
            pytest.xfail("Orchestrator /_routes did not return JSON")

        routes = body.get("routes", []) or body.get("data", {}).get("routes", [])
        if not routes:
            pytest.xfail("Orchestrator /_routes returned no payload")

        deployment_rules = sorted(
            r["rule"] if isinstance(r, dict) else r
            for r in routes
            if ("rule" in r if isinstance(r, dict) else True)
            and "/api/v1/deployment" in (r["rule"] if isinstance(r, dict) else r)
        )

        # Every deployment rule the orchestrator exposes should have at least
        # one entry in the fixture. Strip parameter placeholders for the
        # comparison.
        covered_patterns = {path for path, _, _ in _DEPLOYMENT_ROUTES}

        def _normalize(rule: str) -> str:
            # Flask renders path params like ``<stream_id>`` -- strip to a
            # placeholder that matches our parametrize entries.
            import re

            return re.sub(r"<[^>]+>", "nonexistent-stream-0000", rule)

        missing = [r for r in deployment_rules if _normalize(r) not in covered_patterns]
        assert not missing, (
            "New /api/v1/deployment/* routes exist in the orchestrator but "
            "are not covered by the TASK-4-1 401 regression fixture. "
            f"Add them to _DEPLOYMENT_ROUTES: {missing}"
        )

"""Integration coverage for deployment-validation route LOGIC (issue #2641).

The sibling file ``test_k8s_deployment_tools.py`` covers the
``@require_lifecycle_secret`` parity for every #1759 deployment route
(401/503 on missing or wrong bearer). What it deliberately did *not*
cover is the post-auth behaviour of the validation routes, since the
lifecycle bearer wasn't surfaced through the shared ``EggStack``
fixture. This file fills that gap for the two routes called out in
#2641:

* ``POST /api/v1/deployment/validate-manifests``
* ``POST /api/v1/deployment/validate-network-isolation``

(``validate_config`` — the third route named in #2641 — is not an HTTP
route. It is a pure MCP-side handler that runs Pydantic validation in
the orchestrator process and never touches the k3s cluster. Its
coverage lives in ``orchestrator/tests/test_mcp_tools.py`` under
``TestValidateConfig``; reproducing it in the k3s tier would add cost
without adding signal.)

## Bugs surfaced while building the suite (filed as follow-ups)

The default-overlay / probe happy paths in the deployed orchestrator
are currently broken in three independent ways. The tests below lock
in the *observable* behaviour today (so any silent fix would flip the
assertion and force a deliberate test update); the happy-path variants
are marked ``xfail(strict=True)`` and point at the relevant bug.

* **#2647 — orchestrator container has no ``kustomize``/``kubectl`` on
  PATH.** ``orchestrator/Dockerfile`` installs ``git curl gosu`` only,
  so ``_run_kustomize`` falls through both subprocess invocations and
  raises ``kustomize_unavailable``. Any default-overlay validation
  returns HTTP 500.
* **#2646 — orchestrator ServiceAccount cannot list DaemonSets in
  ``kube-system`` or nodes cluster-wide.** ``_detect_cni`` and
  ``_detect_k3s`` both rely on these reads, so ``validate-network-
  isolation`` always short-circuits with
  ``network_policy_enforcement_not_detected`` and ``validate-
  manifests`` always reports ``is_k3s=False`` even on a real k3s
  cluster (the k3s-gated image-tag rule never fires).
* **#2648 — orchestrator ServiceAccount can only ``get`` (not
  ``list``) Deployments in ``egg-system``.** Tangential to #2641 but
  observed in the same audit: ``_collect_egg_image_tags`` always
  returns ``{}`` and ``get_deployment_context`` always sets
  ``images_unavailable: true`` in production.

The tests below do not depend on any of these bugs being fixed.
"""

from __future__ import annotations

import concurrent.futures
import time

import pytest
import requests

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _post(
    orchestrator_url: str,
    path: str,
    *,
    secret: str,
    body: dict,
    timeout: int = 60,
) -> requests.Response:
    return requests.post(
        f"{orchestrator_url}{path}",
        json=body,
        headers={**_auth_headers(secret), "Content-Type": "application/json"},
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# validate_deployment_manifests
# ---------------------------------------------------------------------------


class TestValidateDeploymentManifestsLogic:
    """``POST /api/v1/deployment/validate-manifests`` — post-auth behaviour.

    The orchestrator container ships without ``kustomize``/``kubectl`` and
    without the egg repo bind-mounted, so the happy-path (rendered overlay
    + warnings list) cannot run in CI today (see B1 in the module
    docstring). The tests here cover what is reachable: the 404 / 400
    error paths and the deterministic 500 the missing tooling produces.
    """

    def test_missing_overlay_returns_404(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """A relative ``overlay_path`` that doesn't exist returns 404."""
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-manifests",
            secret=lifecycle_secret,
            body={"overlay_path": "k8s/does-not-exist-2641"},
        )
        assert resp.status_code == 404, (
            f"expected 404 for missing overlay, got {resp.status_code}: {resp.text[:500]}"
        )
        body = resp.json()
        assert body["success"] is False
        assert "not found" in (body.get("message") or "").lower()

    def test_absolute_path_outside_repo_root_returns_400(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """An absolute path that escapes the repo-root scope guard returns 400.

        Regression guard for the auth-gated probe-via-200/404
        differentiation worry called out in
        ``orchestrator/routes/deployment.py``: even an authenticated
        caller must not be able to use the route as an arbitrary
        filesystem-existence probe.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-manifests",
            secret=lifecycle_secret,
            body={"overlay_path": "/etc/passwd"},
        )
        assert resp.status_code == 400, (
            f"expected 400 for traversal attempt, got {resp.status_code}: {resp.text[:500]}"
        )
        body = resp.json()
        assert body["success"] is False
        assert "repo root" in (body.get("message") or "").lower()

    def test_relative_traversal_outside_repo_root_returns_400(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """``../`` segments that resolve outside the repo root return 400.

        The route resolves the overlay path before the in-scope check,
        so a relative traversal must be caught the same way as an
        absolute one. This is a regression guard for the path-traversal
        comment in the route's docstring.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-manifests",
            secret=lifecycle_secret,
            body={"overlay_path": "../../../../../etc"},
        )
        assert resp.status_code == 400, (
            f"expected 400 for relative traversal, got {resp.status_code}: {resp.text[:500]}"
        )

    def test_default_overlay_in_deployed_orchestrator_returns_500_today(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """Default overlay against a real orchestrator pod returns 500 today (B1).

        With the egg repo bind-mounted at ``/home/egg/repos`` (local
        overlay), the route finds ``k8s/overlays/local`` but the
        orchestrator container has neither ``kustomize`` nor ``kubectl``
        installed, so ``_run_kustomize`` raises ``kustomize_unavailable``
        and the route returns 500. Locking in the current observable
        behaviour so the contract is explicit; when B1 is fixed this
        assertion will need to flip to 200.

        The fixture path that mounts the repo only exists under the
        local overlay; in CI the repo isn't mounted at all and the
        route returns 404 instead. Accept either to keep the test
        portable across both deployment shapes.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-manifests",
            secret=lifecycle_secret,
            body={},
        )
        # Two valid shapes today:
        #   - 500 kustomize_unavailable: repo IS mounted (local overlay
        #     pattern), overlay found, kustomize missing.
        #   - 404 overlay not found: repo is NOT mounted (CI default
        #     overlay isn't reachable from the orchestrator pod).
        # Both expose a real gap; 200 would be the post-fix state.
        assert resp.status_code in (404, 500), (
            f"expected 404 or 500 in current deployment, got {resp.status_code}: {resp.text[:500]}"
        )
        body = resp.json()
        assert body["success"] is False
        msg = (body.get("message") or "").lower()
        if resp.status_code == 500:
            assert "kustomize" in msg, (
                f"500 should be the kustomize_unavailable bug (B1); got: {msg!r}"
            )
        else:
            assert "not found" in msg, f"404 should be the overlay-not-found path; got: {msg!r}"

    def test_re_validation_is_idempotent_on_error_path(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """Two identical calls return identical status + message.

        Issue #2641's gap audit calls out "idempotent re-validation" as
        an invariant we should lock in. Exercising it on the 404 error
        path (the only deterministic shape in CI today — see B1/B2 in
        the module docstring) is still a real regression guard: a
        future refactor that adds caching, request-IDs, or transient
        state in the route would surface as a diff between the two
        responses.
        """
        body = {"overlay_path": "k8s/does-not-exist-2641-idempotent"}
        first = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-manifests",
            secret=lifecycle_secret,
            body=body,
        )
        second = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-manifests",
            secret=lifecycle_secret,
            body=body,
        )
        assert first.status_code == second.status_code
        assert first.json()["success"] is second.json()["success"]
        assert first.json().get("message") == second.json().get("message")


# ---------------------------------------------------------------------------
# validate_network_isolation
# ---------------------------------------------------------------------------


class TestValidateNetworkIsolationLogic:
    """``POST /api/v1/deployment/validate-network-isolation`` — post-auth.

    The probe-pod happy path is currently unreachable because the
    orchestrator ServiceAccount can't list ``kube-system`` DaemonSets
    (B2), so ``_detect_cni`` returns ``(None, False)`` and the route
    short-circuits with ``network_policy_enforcement_not_detected``.
    The tests below cover the K8s-label-validation logic (which runs
    before the CNI gate) and the current short-circuit; a
    ``xfail(strict=True)`` test guards the future happy-path shape.
    """

    def test_invalid_pipeline_id_returns_400(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """``pipeline_id`` with K8s-invalid characters (space) returns 400.

        ``_K8S_LABEL_VALUE_RE`` enforces RFC1123-ish label values to
        avoid an opaque 422 from the apiserver when the Job is created.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={"pipeline_id": "bad id with spaces", "role": "coder"},
        )
        assert resp.status_code == 400, (
            f"expected 400 for invalid pipeline_id, got {resp.status_code}: {resp.text[:500]}"
        )
        body = resp.json()
        assert body["success"] is False
        assert "pipeline_id" in (body.get("message") or "")

    def test_invalid_role_returns_400(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """``role`` with K8s-invalid characters returns 400."""
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={"pipeline_id": "p1", "role": "coder!"},
        )
        assert resp.status_code == 400, (
            f"expected 400 for invalid role, got {resp.status_code}: {resp.text[:500]}"
        )
        body = resp.json()
        assert body["success"] is False
        assert "role" in (body.get("message") or "").lower()

    @pytest.mark.parametrize(
        "pipeline_id",
        [
            pytest.param("-leading-hyphen", id="leading-hyphen"),
            pytest.param("trailing-hyphen-", id="trailing-hyphen"),
            pytest.param(".leading-dot", id="leading-dot"),
            pytest.param("x" * 64, id="too-long-64"),
        ],
    )
    def test_pipeline_id_regex_boundary_violations_return_400(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
        pipeline_id: str,
    ) -> None:
        """Boundary cases that violate ``_K8S_LABEL_VALUE_RE`` all reject.

        The regex is ``^[a-z0-9A-Z]([-._a-z0-9A-Z]{0,61}[a-z0-9A-Z])?$``:

        * Bookend chars must be alphanumeric (no leading/trailing ``-``,
          ``.``, ``_``).
        * Middle run is capped at 61 chars → total max 63.

        Each parametrize case is one corner of that envelope. A regex
        regression that loosened any of these would let an apiserver
        422 leak through the route's 400 guard.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={"pipeline_id": pipeline_id, "role": "coder"},
        )
        assert resp.status_code == 400, (
            f"expected 400 for {pipeline_id!r}, got {resp.status_code}: {resp.text[:500]}"
        )

    @pytest.mark.parametrize(
        "pipeline_id",
        [
            pytest.param("a", id="single-char"),
            pytest.param("x" * 63, id="max-length-63"),
            pytest.param("p1.b2_c3-d4", id="middle-dot-underscore-hyphen"),
            pytest.param("Pipe1", id="uppercase-allowed"),
        ],
    )
    def test_pipeline_id_regex_valid_at_boundaries_pass(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
        pipeline_id: str,
    ) -> None:
        """Valid-at-boundary labels pass the regex and reach the CNI gate.

        Companion to ``test_pipeline_id_regex_boundary_violations_return_400``:
        single-char, max-length-63, and the full set of middle-position
        special chars all must NOT reject at the label-validator stage.
        They may then short-circuit at the CNI gate (B2 today) or run
        the probe — but they must not 400.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={"pipeline_id": pipeline_id, "role": "coder"},
        )
        assert resp.status_code == 200, (
            f"expected 200 for valid label {pipeline_id!r}, got {resp.status_code}: "
            f"{resp.text[:500]}"
        )

    def test_default_pipeline_id_and_role_pass_label_validation(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """Omitted body → defaults ``pipeline_id=manual``/``role=coder`` pass.

        The label-validator runs before the CNI gate, so the test
        passes regardless of whether the probe actually launches.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={},
        )
        # 200 either way: either the probe launches (post-B2 fix), or
        # the route short-circuits with ``network_policy_enforcement_
        # not_detected``. A 400 here would mean the default values are
        # rejecting against the label regex — that's the regression.
        assert resp.status_code == 200, (
            f"expected 200 with default labels, got {resp.status_code}: {resp.text[:500]}"
        )

    def test_route_short_circuits_when_cni_not_detected(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """Current (B2) behaviour: route reports ``network_policy_enforcement_not_detected``.

        With the production RBAC the orchestrator can't list ``kube-
        system`` DaemonSets, so the CNI gate fires unconditionally.
        This test locks in the current observable shape so a future
        change to the gate (or a fix to B2) surfaces as a deliberate
        test update.

        When B2 is fixed the route will run the probe instead and this
        test should be replaced with the happy-path probe-output
        assertions in
        ``test_probe_runs_and_returns_expected_shape``.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={"pipeline_id": "test-2641", "role": "coder"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        # B2 is the dominant failure mode in production today. If the
        # probe ever runs, the response shape changes to {probe_id,
        # namespace, result} — let that flip be a hard signal by
        # asserting the error key explicitly.
        assert data.get("error") == "network_policy_enforcement_not_detected", (
            "route stopped short-circuiting; B2 may be fixed — flip this "
            "test to the happy-path assertions"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Blocked on B2: orchestrator SA can't list kube-system "
            "DaemonSets so _detect_cni returns (None, False) and the probe "
            "never launches. Fix the RBAC and this should pass."
        ),
    )
    def test_probe_runs_and_returns_expected_shape(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """Happy path: with enforcement detected the probe runs and reports.

        Marked ``xfail(strict=True)`` until B2 lands — when the
        orchestrator gains RBAC to list kube-system DaemonSets the
        probe will actually launch and this assertion holds.

        Expected probe-output shape (per ``PROBE_COMMAND_TEMPLATE``):

        * ``gateway_reachable: True`` — ``allow-agent-to-gateway``
          permits agent→gateway:9848.
        * ``internet_blocked: True`` — ``default-deny-egress`` blocks
          arbitrary egress (curl example.com).
        * ``agent_pods_unreachable: True`` — no policy allows agent→
          random-peer:80.

        ``orchestrator_direct_blocked`` is deliberately NOT asserted:
        ``allow-agent-to-orchestrator`` permits agent→orchestrator:9849
        for heartbeats, so the field returns ``False`` even when
        isolation is correctly enforced. The field's name is misleading
        — see the bug discussion in the PR.
        """
        resp = _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={"pipeline_id": "happy-2641", "role": "coder"},
            timeout=90,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        # Probe-launched shape, not the short-circuit shape.
        assert "probe_id" in data
        result = data["result"]
        assert result.get("gateway_reachable") is True
        assert result.get("internet_blocked") is True
        assert result.get("agent_pods_unreachable") is True


# ---------------------------------------------------------------------------
# Cross-route invariants
# ---------------------------------------------------------------------------


class TestValidationRouteConcurrency:
    """Both validation routes are read-only / per-call; concurrent calls must not interfere.

    Unlike ``rebuild_and_rollout`` (which has an in-process
    ``_REBUILD_LOCK`` and rejects concurrent invocations with 409),
    ``validate-manifests`` and ``validate-network-isolation`` have no
    shared mutable state per call — concurrent requests must each
    receive a self-consistent response. A regression that wired the
    rebuild-lock into either of these would surface as a 409 here.
    """

    def test_concurrent_validate_manifests_calls_are_independent(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """5 parallel calls return identical (404, success=false) responses."""
        body = {"overlay_path": "k8s/does-not-exist-2641-concurrency"}

        def _call() -> requests.Response:
            return _post(
                orchestrator_url,
                "/api/v1/deployment/validate-manifests",
                secret=lifecycle_secret,
                body=body,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(lambda _: _call(), range(5)))

        # All should be the same shape; no race-induced 5xx or 409.
        statuses = {r.status_code for r in results}
        assert statuses == {404}, f"got mixed/unexpected statuses: {statuses}"
        for r in results:
            body_json = r.json()
            assert body_json["success"] is False
            assert "not found" in (body_json.get("message") or "").lower()

    def test_concurrent_validate_network_isolation_calls_get_distinct_probe_ids(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """Concurrent calls each get a distinct response without 409 contention.

        Under B2 the route short-circuits before submitting a probe Job,
        so this test mostly proves the route is genuinely stateless
        across calls. When B2 is fixed and the probe actually launches,
        the test additionally guards against a probe-id collision
        regression — ``uuid.uuid4().hex[:12]`` is 48 bits of entropy,
        more than enough for the 5-way fan-out used here, but a
        regression that hard-codes the id would surface here.
        """

        def _call(i: int) -> requests.Response:
            return _post(
                orchestrator_url,
                "/api/v1/deployment/validate-network-isolation",
                secret=lifecycle_secret,
                body={"pipeline_id": f"concur-{i}", "role": "coder"},
                timeout=90,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(_call, range(5)))

        for r in results:
            assert r.status_code == 200, (
                f"concurrent call got non-200: {r.status_code}: {r.text[:300]}"
            )
            assert r.json()["success"] is True

        # If the probe ever runs (post-B2), distinct probe_ids confirm
        # no collision. Under the current short-circuit there's no
        # probe_id at all — that's also fine.
        probe_ids = [r.json()["data"].get("probe_id") for r in results]
        non_null = [p for p in probe_ids if p]
        if non_null:
            assert len(set(non_null)) == len(non_null), (
                f"concurrent calls produced duplicate probe_ids: {probe_ids}"
            )


class TestValidationRouteSelfConsistency:
    """Cross-cutting invariants the routes must hold regardless of B1/B2."""

    def test_validation_routes_never_leak_secrets_in_error_messages(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """The orchestrator must not echo the bearer back in any error response.

        The lifecycle secret is the production credential for every
        ``@require_lifecycle_secret`` route; a bug that included the
        Authorization header value in an error body (e.g. via a
        broad ``request.get_data()`` dump) would leak it to anyone who
        already had it — surfacing the regression for the next person
        who runs these tests is the cheap guard.
        """
        secret_snippet = lifecycle_secret[:16]
        # Drive a few error paths and inspect their bodies.
        responses = [
            _post(
                orchestrator_url,
                "/api/v1/deployment/validate-manifests",
                secret=lifecycle_secret,
                body={"overlay_path": "/etc/passwd"},
            ),
            _post(
                orchestrator_url,
                "/api/v1/deployment/validate-manifests",
                secret=lifecycle_secret,
                body={"overlay_path": "k8s/does-not-exist-2641-leak"},
            ),
            _post(
                orchestrator_url,
                "/api/v1/deployment/validate-network-isolation",
                secret=lifecycle_secret,
                body={"pipeline_id": "leak test"},
            ),
        ]
        for resp in responses:
            assert secret_snippet not in resp.text, (
                f"response body contained a prefix of the bearer secret — "
                f"{resp.request.url}: {resp.text[:500]}"
            )

    def test_validation_routes_reject_invalid_json(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        """Both routes tolerate / reject malformed JSON without crashing.

        ``request.get_json(silent=True) or {}`` is the pattern in the
        route handlers; an upstream regression that flipped to a
        non-silent ``get_json()`` would 500 with a Flask traceback.
        Catch that here.
        """
        for path in (
            "/api/v1/deployment/validate-manifests",
            "/api/v1/deployment/validate-network-isolation",
        ):
            resp = requests.post(
                f"{orchestrator_url}{path}",
                data="this is not json",
                headers={
                    **_auth_headers(lifecycle_secret),
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            # The handlers use ``request.get_json(silent=True) or {}`` so a
            # malformed body becomes the same shape as an empty one — no
            # 500 with a Flask traceback should ever reach the caller.
            assert "traceback" not in resp.text.lower(), (
                f"{path}: malformed JSON produced a Flask traceback: {resp.text[:500]}"
            )


# ---------------------------------------------------------------------------
# Probe-job cleanup
# ---------------------------------------------------------------------------


class TestProbeJobCleanup:
    """Belt-and-braces: no orphan probe Jobs after the route returns.

    The route uses a ``try/finally`` to call ``_delete_probe_job`` and
    sets ``ttlSecondsAfterFinished: 0`` on the Job. This test enforces
    that no probe Job persists in ``egg-agents`` after a call — even
    today's short-circuit (B2) path, which never creates one, must not
    leave one behind from a previous run. When B2 is fixed and the
    probe actually launches, the same assertion catches a cleanup
    regression.
    """

    def test_no_orphan_probe_jobs_after_call(
        self,
        orchestrator_url: str,
        lifecycle_secret: str,
    ) -> None:
        import subprocess

        _post(
            orchestrator_url,
            "/api/v1/deployment/validate-network-isolation",
            secret=lifecycle_secret,
            body={"pipeline_id": "cleanup-2641", "role": "coder"},
            timeout=90,
        )

        # Allow up to a few seconds for ttlSecondsAfterFinished=0 to
        # reap any Job that did launch — the API call returned, but
        # the controller may not have run the GC pass yet.
        deadline = time.time() + 15
        leftover: list[str] = []
        while time.time() < deadline:
            result = subprocess.run(
                [
                    "kubectl",
                    "-n",
                    "egg-agents",
                    "get",
                    "jobs",
                    "-l",
                    "egg.probe=true",
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                pytest.skip(f"kubectl listing failed: {result.stderr!r}")
            names = [n for n in result.stdout.strip().split() if n]
            if not names:
                return  # success
            leftover = names
            time.sleep(1)
        pytest.fail(
            f"probe Jobs still present after route returned (route should "
            f"clean up via finally + ttlSecondsAfterFinished=0): {leftover}"
        )

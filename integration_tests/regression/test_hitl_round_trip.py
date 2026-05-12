"""Regression tests for the HITL HTTP round-trip (#2634 / #2430).

#2634 expands integration-test coverage for the human-in-the-loop
pause/resume flow. The full round-trip — pipeline → AWAITING_HUMAN →
``provide_input`` → resume — requires an agent that calls
``register_open_question`` from inside a sandbox pod. That path is
blocked on ScriptedProvider pod-injection (see #2474's constraint
write-up and the ``feedback_scripted_provider_pod_injection`` memo):
deployed agent pods run the real Claude provider with no mechanism to
consume canned trajectories from the test harness.

So this module pins the part of the round-trip that **is** reachable
from the test runner: the orchestrator's ``/api/v1/pipelines/<id>/
decisions/...`` HTTP surface that ``provide_input`` and
``register_open_question`` are layered on. Concretely:

* Every HITL route is registered on the live blueprint (no
  ``404 — route not found``).
* Lifecycle-protected routes (``/resolve``, ``/cancel``) fail closed
  with ``401`` or ``503`` for missing / bogus bearers — the #1769
  HITL auto-approval incident parity check.
* Agent-facing routes (queue / list / get / status) reject malformed
  payloads with structured ``400``\\ s rather than ``500``\\ s.
* Every route returns the canonical ``{"success": false,
  "message": ...}`` envelope on error — a regression that ate the
  envelope would let upstream tooling (sdlc-skill, ``provide_input``)
  silently treat malformed responses as success.

Gaps that **require** pod-level provider injection and are out of
scope for this PR (filed as follow-ups when this lands):

* Pipeline status transition into ``AWAITING_HUMAN`` (driven by an
  agent calling ``register_open_question``).
* Resume out of ``AWAITING_HUMAN`` within #2430's bypass deadline.
* HITL during slice phases / HITL combined with ``restart_agent`` /
  multiple sequential HITLs on a real pipeline.
* HITL timeout behaviour.
"""

from __future__ import annotations

import uuid

import pytest
import requests

from integration_tests.regression.conftest import deterministic_pipeline_id

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Route registry — the live blueprint must expose every HITL endpoint
# ---------------------------------------------------------------------------


def _placeholder_decision_id() -> str:
    """A decision id that is syntactically arbitrary but never resolves.

    The handlers' 404 path runs once the auth + body checks pass, so the
    id only has to be a non-empty string; it intentionally carries no
    structure so an accidental registration of this id in the queue
    would not silently make a 404 assertion pass.
    """
    return f"missing-decision-{uuid.uuid4().hex[:8]}"


# (method, path_template, json_body_factory, requires_lifecycle_secret)
# ``path_template`` uses ``{pipeline_id}`` / ``{decision_id}`` placeholders.
_HITL_ROUTES: list[tuple[str, str, dict | None, bool]] = [
    ("GET", "/api/v1/pipelines/{pipeline_id}/decisions", None, False),
    (
        "POST",
        "/api/v1/pipelines/{pipeline_id}/decisions",
        {"question": "regression-test placeholder"},
        False,
    ),
    (
        "GET",
        "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}",
        None,
        False,
    ),
    (
        "POST",
        "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
        {"resolution": "regression-test placeholder"},
        True,
    ),
    (
        "POST",
        "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/cancel",
        None,
        True,
    ),
    ("GET", "/api/v1/pipelines/{pipeline_id}/decisions/status", None, False),
]


def _format_path(template: str, pipeline_id: str, decision_id: str) -> str:
    return template.format(pipeline_id=pipeline_id, decision_id=decision_id)


def _call(
    orchestrator_url: str,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> requests.Response:
    url = f"{orchestrator_url}{path}"
    kwargs: dict = {"timeout": timeout, "headers": headers or {}}
    if json_body is not None:
        kwargs["json"] = json_body
    return requests.request(method, url, **kwargs)


def _assert_error_envelope(resp: requests.Response, context: str) -> dict:
    """Every error must be ``{"success": false, "message": ...}`` JSON.

    Returns the decoded body so callers can make additional assertions.
    """
    try:
        body = resp.json()
    except ValueError as e:
        pytest.fail(
            f"{context}: response was not JSON (status={resp.status_code}): "
            f"{e}; body[:500]={resp.text[:500]!r}"
        )
    assert isinstance(body, dict), f"{context}: response body must be a JSON object: {body!r}"
    assert body.get("success") is False, (
        f"{context}: error responses must carry success=false; body was {body!r}"
    )
    message = body.get("message")
    assert isinstance(message, str) and message, (
        f"{context}: error envelope must include non-empty 'message'; got {message!r}"
    )
    return body


def _assert_lifecycle_auth_rejected(resp: requests.Response, context: str) -> None:
    """Mirror of ``test_k8s_deployment_tools._assert_auth_rejected`` for HITL.

    503 is acceptable because a deployment that forgot to mount
    ``EGG_LIFECYCLE_SECRET`` fails closed by design (see
    ``orchestrator/lifecycle_auth.py``). Either outcome demonstrates the
    decorator is wired up. 404 specifically must NOT come back here:
    that would mean the route was dropped from the blueprint or the
    decorator fires AFTER routing (which would re-open #1769's bypass).
    """
    assert resp.status_code != 404, (
        f"{context}: route returned 404 — decisions blueprint missing or path renamed (regression)."
    )
    assert resp.status_code in (401, 503), (
        f"{context}: expected 401 or 503 (lifecycle auth reject path), "
        f"got {resp.status_code}: {resp.text[:500]}"
    )
    body = _assert_error_envelope(resp, context)
    msg = (body.get("message") or "").lower()
    assert any(
        hint in msg
        for hint in ("authorization", "lifecycle", "egg_lifecycle_secret", "misconfigured")
    ), f"{context}: message doesn't look like an auth-layer reject: {msg!r}"


class TestHitlRoutesRegistered:
    """Every HITL endpoint resolves on the live orchestrator blueprint.

    Regression for a refactor that drops or renames a decisions route:
    the decomposition table in ``orchestrator/CLAUDE.md`` calls out
    ``_decisions.py`` as a future submodule of the pipelines pre-split,
    which is exactly the change shape that historically broke route
    registration in #2421.
    """

    @pytest.mark.parametrize(
        ("method", "template", "body", "lifecycle"),
        _HITL_ROUTES,
        ids=[f"{m} {t}" for m, t, _, _ in _HITL_ROUTES],
    )
    def test_route_is_not_404(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        method: str,
        template: str,
        body: dict | None,
        lifecycle: bool,
    ) -> None:
        path = _format_path(template, regression_pipeline_id, _placeholder_decision_id())
        # No auth header — the route should reject (auth-required) or
        # process and 404 the unknown pipeline. Either way: NOT 404 from
        # the Flask routing layer (which would mean the path wasn't bound).
        resp = _call(orchestrator_url, method, path, json_body=body)
        if lifecycle:
            # /resolve and /cancel fire the lifecycle decorator FIRST,
            # so an unauthed call returns 401/503 — never 404.
            _assert_lifecycle_auth_rejected(resp, f"{method} {path}")
        else:
            # Agent-facing reads / queue: 404 here means *pipeline*
            # not found (the route DID run), which still proves the
            # route exists. A blueprint regression would surface as
            # a different shape — see the envelope assertion below.
            _assert_error_envelope(resp, f"{method} {path}")
            assert resp.status_code in (200, 400, 404), (
                f"{method} {path}: unexpected status {resp.status_code} for "
                f"unknown pipeline + minimal body: {resp.text[:500]}"
            )


class TestHitlLifecycleAuth:
    """#1769 parity for the HITL ``/resolve`` and ``/cancel`` endpoints.

    The HITL auto-approval incident was specifically that lifecycle-
    state-changing endpoints accepted unauthenticated calls. Pin the
    two routes that mutate decision state here so a future deploy that
    drops the decorator surfaces a red CI before reaching prod.
    """

    _LIFECYCLE_ROUTES = [
        (
            "POST",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
            {"resolution": "regression-test placeholder"},
        ),
        (
            "POST",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/cancel",
            None,
        ),
    ]

    @pytest.mark.parametrize(
        ("method", "template", "body"),
        _LIFECYCLE_ROUTES,
        ids=[f"{m} {t}" for m, t, _ in _LIFECYCLE_ROUTES],
    )
    def test_no_auth_header_is_rejected(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        method: str,
        template: str,
        body: dict | None,
    ) -> None:
        path = _format_path(template, regression_pipeline_id, _placeholder_decision_id())
        resp = _call(orchestrator_url, method, path, json_body=body)
        _assert_lifecycle_auth_rejected(resp, f"{method} {path} (no auth)")

    @pytest.mark.parametrize(
        ("method", "template", "body"),
        _LIFECYCLE_ROUTES,
        ids=[f"{m} {t}" for m, t, _ in _LIFECYCLE_ROUTES],
    )
    def test_invalid_bearer_is_rejected(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        method: str,
        template: str,
        body: dict | None,
    ) -> None:
        path = _format_path(template, regression_pipeline_id, _placeholder_decision_id())
        resp = _call(
            orchestrator_url,
            method,
            path,
            json_body=body,
            headers={"Authorization": "Bearer bogus-hitl-test-bearer-do-not-accept"},
        )
        _assert_lifecycle_auth_rejected(resp, f"{method} {path} (bogus bearer)")

    @pytest.mark.parametrize(
        ("method", "template", "body"),
        _LIFECYCLE_ROUTES,
        ids=[f"{m} {t}" for m, t, _ in _LIFECYCLE_ROUTES],
    )
    def test_non_bearer_scheme_is_rejected(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        method: str,
        template: str,
        body: dict | None,
    ) -> None:
        """``Authorization: <secret>`` (no ``Bearer `` prefix) must be rejected.

        Exact #1769 incident shape — the decorator must require the
        ``Bearer `` prefix even if the bare secret value would otherwise
        compare equal.
        """
        path = _format_path(template, regression_pipeline_id, _placeholder_decision_id())
        # Even with a real-looking-but-prefix-less header value, the
        # decorator must reject. The bare value is intentionally not the
        # actual secret — we just need to prove the prefix check runs.
        resp = _call(
            orchestrator_url,
            method,
            path,
            json_body=body,
            headers={"Authorization": "this-is-not-prefixed-with-bearer"},
        )
        _assert_lifecycle_auth_rejected(resp, f"{method} {path} (non-Bearer scheme)")


class TestHitlUnknownPipelineReturns404:
    """Agent-facing HITL reads return a structured 404 for unknown pipelines.

    These routes are intentionally unauthenticated (see
    ``orchestrator/lifecycle_auth.py`` rationale: agents legitimately
    call them) so the 404 is the only signal that a path-traversal /
    state-store-bypass regression has appeared. ``POST /decisions``
    (queue) is also unauthenticated and must surface the same 404 so a
    compromised agent can't queue decisions on arbitrary pipeline ids.
    """

    @pytest.mark.parametrize(
        ("method", "template", "body"),
        [
            ("GET", "/api/v1/pipelines/{pipeline_id}/decisions", None),
            (
                "POST",
                "/api/v1/pipelines/{pipeline_id}/decisions",
                {"question": "regression placeholder"},
            ),
            (
                "GET",
                "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}",
                None,
            ),
            ("GET", "/api/v1/pipelines/{pipeline_id}/decisions/status", None),
        ],
        ids=lambda v: v if isinstance(v, str) else None,
    )
    def test_unknown_pipeline_404(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        method: str,
        template: str,
        body: dict | None,
    ) -> None:
        path = _format_path(template, regression_pipeline_id, _placeholder_decision_id())
        resp = _call(orchestrator_url, method, path, json_body=body)
        # The unauthenticated routes hit the state-store lookup, which
        # raises PipelineNotFoundError → 404. A regression that bypassed
        # the lookup would either 200 (silently fabricating state) or
        # 500 (uncaught exception) — both red flags.
        assert resp.status_code == 404, (
            f"{method} {path}: expected 404 for unknown pipeline, "
            f"got {resp.status_code}: {resp.text[:500]}"
        )
        body_dict = _assert_error_envelope(resp, f"{method} {path}")
        # The handler embeds the pipeline id in the message — pin it so
        # a refactor that drops the diagnostic breaks loudly. We don't
        # require the exact phrasing, only that the id surfaces.
        assert regression_pipeline_id in (body_dict.get("message") or ""), (
            f"{method} {path}: 404 envelope should reference the pipeline id "
            f"({regression_pipeline_id!r}); got {body_dict!r}"
        )


class TestHitlQueueDecisionPayloadValidation:
    """``POST /decisions`` rejects malformed bodies before touching state.

    The endpoint is unauthenticated — agents in the cluster legitimately
    queue decisions — so a regression that lets a bad payload reach
    ``DecisionQueue.queue_decision`` would surface as a 500 with no
    pipeline-id scope in the error, leaking ``DecisionQueue`` internals
    to callers.
    """

    @pytest.mark.parametrize(
        ("body", "expected_message_hint"),
        [
            ({}, "question"),
            ({"question": ""}, "question"),
            (
                {"question": "q", "decision_type": "not-a-real-type"},
                "decision_type",
            ),
            ({"question": "q", "phase": "not-a-real-phase"}, "phase"),
        ],
        ids=[
            "empty-object",
            "empty-question",
            "invalid-decision-type",
            "invalid-phase",
        ],
    )
    def test_malformed_queue_payload_400(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        body: dict,
        expected_message_hint: str,
    ) -> None:
        path = f"/api/v1/pipelines/{regression_pipeline_id}/decisions"
        # Each case sends a JSON dict that is structurally well-formed
        # but rejected by the handler's body validation.
        resp = _call(orchestrator_url, "POST", path, json_body=body)
        assert resp.status_code == 400, (
            f"POST {path} body={body!r}: expected 400, got {resp.status_code}: {resp.text[:500]}"
        )
        env = _assert_error_envelope(resp, f"POST {path} body={body!r}")
        assert expected_message_hint.lower() in (env.get("message") or "").lower(), (
            f"POST {path}: error message should mention {expected_message_hint!r}; got {env!r}"
        )

    def test_no_body_rejected_at_or_before_handler(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
    ) -> None:
        """A POST with no body must not crash the handler.

        Flask returns 415 when no ``application/json`` Content-Type is
        present, so the orchestrator handler never even sees the
        request. Either 400 (handler-level "Missing request body") or
        415 (Flask-level) is fine — both are structured rejections;
        what we care about is "not 500" and "envelope present" (415s
        from Flask still carry the canonical envelope because Werkzeug
        renders them via the orchestrator's error handlers).
        """
        path = f"/api/v1/pipelines/{regression_pipeline_id}/decisions"
        # No json=, no Content-Type — Flask 415 path.
        resp = requests.post(f"{orchestrator_url}{path}", timeout=15)
        assert resp.status_code in (400, 415), (
            f"POST {path} no-body: expected 400 or 415, got {resp.status_code}: {resp.text[:500]}"
        )
        _assert_error_envelope(resp, f"POST {path} no-body")


class TestHitlResolveRequiresResolution:
    """``/resolve`` requires a non-empty ``resolution`` field.

    With a valid bearer the handler's first validation is "Missing
    resolution" → 400. Without a bearer we hit the lifecycle decorator
    instead (covered by ``TestHitlLifecycleAuth``). This test pins the
    happy-path validation order so a refactor that swapped the checks
    (e.g. running body validation BEFORE the decorator and leaking
    decision-existence info via differential responses) would surface.
    """

    def test_resolve_without_resolution_field_with_auth(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        lifecycle_bearer: str,
    ) -> None:
        path = (
            f"/api/v1/pipelines/{regression_pipeline_id}/decisions/"
            f"{_placeholder_decision_id()}/resolve"
        )
        resp = _call(
            orchestrator_url,
            "POST",
            path,
            json_body={},
            headers={"Authorization": lifecycle_bearer},
        )
        # 400 (missing resolution) is the expected response. 404 here
        # would mean the body validation runs AFTER state-store lookup
        # — that ordering would let a polled-then-deleted decision id
        # leak through with a different status code than a never-existed
        # one.
        assert resp.status_code == 400, (
            f"POST {path} with auth + empty body: expected 400 "
            f"(missing resolution), got {resp.status_code}: {resp.text[:500]}"
        )
        env = _assert_error_envelope(resp, f"POST {path} (empty body)")
        assert "resolution" in (env.get("message") or "").lower(), (
            f"POST {path}: error message should mention resolution; got {env!r}"
        )


class TestHitlPipelineIdValidation:
    """Path-traversal / malformed pipeline IDs are rejected at the route boundary.

    ``state_store._validate_pipeline_id`` enforces a strict regex
    (``issue-N`` / ``pr-N`` / ``pipeline-<hex>`` / ``KA-N`` /
    ``local-<hex>``). Anything else — including the dotted /
    slashed shapes a path-traversal attempt would use — raises
    ``InvalidPipelineIdError``, which the handlers must surface as a
    structured 400 rather than letting it bubble to a 500.
    """

    # Each value is rejected by the regex in ``state_store.py``.  We
    # explicitly include shapes that historically tripped path-traversal
    # bugs (``..`` segments) plus the trivially-empty-segment case.
    @pytest.mark.parametrize(
        "bad_pipeline_id",
        [
            "..",
            "../etc/passwd",
            "foo/bar",
            "regression-too-short",
            "issue-",
            "issue-abc",
        ],
        ids=lambda v: v,
    )
    def test_invalid_pipeline_id_400(
        self,
        orchestrator_url: str,
        bad_pipeline_id: str,
    ) -> None:
        # Use the unauthenticated GET list route — the same validation
        # runs uniformly across the blueprint, so one route is enough
        # to pin the contract.
        path = f"/api/v1/pipelines/{bad_pipeline_id}/decisions"
        resp = _call(orchestrator_url, "GET", path)
        # 400 here proves InvalidPipelineIdError reached the handler's
        # error mapper. A 404 would mean the regex check was bypassed
        # and the handler hit PipelineNotFoundError instead — which
        # masks the path-traversal-attempt signal in operator logs.
        # Flask's path matcher returns 404 for ``..``-bearing URIs
        # before reaching our handler, so accept 404 only for the
        # path-traversal candidates that the URL parser itself rejects.
        if ".." in bad_pipeline_id or "/" in bad_pipeline_id:
            assert resp.status_code in (400, 404), (
                f"GET {path}: expected 400 or 404 for path-traversal-shaped "
                f"id; got {resp.status_code}: {resp.text[:500]}"
            )
        else:
            assert resp.status_code == 400, (
                f"GET {path}: expected 400 for invalid pipeline_id; "
                f"got {resp.status_code}: {resp.text[:500]}"
            )
            env = _assert_error_envelope(resp, f"GET {path}")
            assert "pipeline" in (env.get("message") or "").lower(), (
                f"GET {path}: error message should mention pipeline; got {env!r}"
            )


class TestHitlHttpMethodEnforcement:
    """HITL routes only accept the methods their handlers declare.

    Flask's MethodView routing returns 405 (with the canonical
    Werkzeug error envelope) for unmatched methods. Pin this so a
    refactor that silently widened a route's method list — e.g.
    accidentally adding ``DELETE`` to ``/decisions`` because of a
    blueprint-level ``methods=["GET", "POST", "DELETE"]`` typo — fails
    loud rather than silent.
    """

    # (method, path_template) pairs that the blueprint MUST reject.
    # Each pair targets a route that exists in ``_HITL_ROUTES`` but with
    # the wrong HTTP method.
    _DISALLOWED = [
        ("DELETE", "/api/v1/pipelines/{pipeline_id}/decisions"),
        ("PUT", "/api/v1/pipelines/{pipeline_id}/decisions"),
        ("PATCH", "/api/v1/pipelines/{pipeline_id}/decisions"),
        (
            "DELETE",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}",
        ),
        (
            "PUT",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}",
        ),
        (
            "PATCH",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}",
        ),
        (
            "GET",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
        ),
        (
            "DELETE",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/resolve",
        ),
        (
            "GET",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/cancel",
        ),
        (
            "DELETE",
            "/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}/cancel",
        ),
        ("POST", "/api/v1/pipelines/{pipeline_id}/decisions/status"),
    ]

    @pytest.mark.parametrize(
        ("method", "template"),
        _DISALLOWED,
        ids=[f"{m} {t}" for m, t in _DISALLOWED],
    )
    def test_disallowed_method_405(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        method: str,
        template: str,
    ) -> None:
        path = _format_path(template, regression_pipeline_id, _placeholder_decision_id())
        resp = _call(orchestrator_url, method, path)
        assert resp.status_code == 405, (
            f"{method} {path}: expected 405 (method not allowed), got "
            f"{resp.status_code}: {resp.text[:500]}"
        )


class TestHitlMalformedJsonBody:
    """Bodies that claim ``application/json`` but aren't valid JSON.

    Flask's ``get_json(silent=False)`` raises ``BadRequest`` here, which
    Werkzeug renders as 400. The handler downstream uses ``request.get_json()``
    without ``silent=`` — if that ever switched to ``silent=True``,
    invalid JSON would silently parse as ``None`` and the "Missing
    request body" path would shadow the JSON-decode failure, losing
    operator-visible diagnostic in the audit log.
    """

    @pytest.mark.parametrize(
        "raw_body",
        [
            "this is not json",
            "{not: valid",
            "{",
            '"unterminated string',
        ],
        ids=["plain-text", "unclosed-brace", "single-brace", "unterminated-string"],
    )
    def test_invalid_json_with_json_content_type_400(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        raw_body: str,
    ) -> None:
        path = f"/api/v1/pipelines/{regression_pipeline_id}/decisions"
        resp = requests.post(
            f"{orchestrator_url}{path}",
            data=raw_body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        # Flask's bad-JSON handler returns 400 (the body is
        # ``application/json`` but doesn't parse). Werkzeug 3.x uses the
        # default error renderer, which the orchestrator overrides to
        # the canonical envelope — so 400 with a JSON body is the
        # expected shape. We don't probe the exact message because
        # Werkzeug's wording changes between minor versions.
        assert resp.status_code == 400, (
            f"POST {path} body={raw_body!r}: expected 400, got "
            f"{resp.status_code}: {resp.text[:500]}"
        )

    @pytest.mark.parametrize(
        "non_object_json",
        [
            pytest.param(
                "[1, 2, 3]",
                marks=pytest.mark.xfail(
                    reason="#2656: list body raises AttributeError → 500, should be 400",
                    strict=True,
                ),
                id="array",
            ),
            pytest.param(
                '"a string body"',
                marks=pytest.mark.xfail(
                    reason="#2656: scalar body raises AttributeError → 500, should be 400",
                    strict=True,
                ),
                id="string",
            ),
            pytest.param(
                "42",
                marks=pytest.mark.xfail(
                    reason="#2656: scalar body raises AttributeError → 500, should be 400",
                    strict=True,
                ),
                id="number",
            ),
            pytest.param(
                "true",
                marks=pytest.mark.xfail(
                    reason="#2656: scalar body raises AttributeError → 500, should be 400",
                    strict=True,
                ),
                id="bool",
            ),
            # ``null`` deserialises to ``None``, which the handler's
            # ``data = request.get_json() or {}`` coerces to ``{}`` and
            # the ``Missing question`` 400 branch then catches. That
            # works today; pin it so a refactor that drops the ``or {}``
            # coercion (regressing to the AttributeError path) breaks.
            pytest.param("null", id="null"),
        ],
    )
    def test_non_object_json_body_400(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        non_object_json: str,
    ) -> None:
        """A syntactically-valid JSON body that isn't a dict.

        Tracked as #2656: ``queue_decision`` does ``data =
        request.get_json() or {}`` then ``data.get("question")``. When
        ``data`` is a list / scalar, ``.get`` raises ``AttributeError``
        and the handler's generic ``except Exception`` mapper returns
        500 — leaking ``DecisionQueue`` internals and turning a bad
        client into a noisy log entry. Expected shape is 400 with the
        canonical envelope.

        The four primitive shapes (array / string / number / bool) are
        ``xfail(strict=True)`` so a fix flips them to XPASS and the
        test author has to drop the mark. ``null`` is the one shape
        that works correctly today (coerced to ``{}`` and routed to
        the missing-question 400 branch) — pinning it guards the
        coercion path.
        """
        path = f"/api/v1/pipelines/{regression_pipeline_id}/decisions"
        resp = requests.post(
            f"{orchestrator_url}{path}",
            data=non_object_json.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        assert resp.status_code == 400, (
            f"POST {path} body={non_object_json!r}: expected 400 "
            f"(non-object body), got {resp.status_code}: {resp.text[:500]}"
        )
        _assert_error_envelope(resp, f"POST {path} body={non_object_json!r}")


class TestHitlResolvePayloadEdgeCases:
    """Edge-case ``resolution`` values exercised via the live HTTP path.

    ``resolve_decision`` normalises non-string resolutions to a JSON
    string (``json.dumps(resolution)``) and rejects empty/missing ones
    with 400. Once those checks pass, the handler hits the state-store
    lookup, which 404s for our placeholder decision id — so each test
    here pins what happens *before* the lookup runs.
    """

    @pytest.mark.parametrize(
        ("body", "expect_validation_error"),
        [
            ({"resolution": None}, True),
            ({"resolution": ""}, True),
            ({"resolution": "   "}, False),  # whitespace bypasses ``if not``
            ({"resolution": {"opt": "a"}}, False),  # dict → json.dumps
            ({"resolution": ["a", "b"]}, False),  # list → json.dumps
            ({"resolution": 42}, False),  # int passes the truthiness check
            ({"resolution": False}, True),  # falsy primitive
        ],
        ids=[
            "explicit-null",
            "empty-string",
            "whitespace-only",
            "dict",
            "list",
            "int",
            "bool-false",
        ],
    )
    def test_resolution_edge_case(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        lifecycle_bearer: str,
        body: dict,
        expect_validation_error: bool,
    ) -> None:
        path = (
            f"/api/v1/pipelines/{regression_pipeline_id}/decisions/"
            f"{_placeholder_decision_id()}/resolve"
        )
        resp = _call(
            orchestrator_url,
            "POST",
            path,
            json_body=body,
            headers={"Authorization": lifecycle_bearer},
        )
        if expect_validation_error:
            # The ``if not resolution`` branch catches None, "", 0, False.
            # Anything else passes validation and falls through to the
            # state-store lookup, which 404s for our placeholder
            # decision id.
            assert resp.status_code == 400, (
                f"POST {path} body={body!r}: expected 400 "
                f"(missing resolution), got {resp.status_code}: "
                f"{resp.text[:500]}"
            )
            env = _assert_error_envelope(resp, f"POST {path}")
            assert "resolution" in (env.get("message") or "").lower(), (
                f"POST {path}: 400 envelope should mention resolution; got {env!r}"
            )
        else:
            # Validation passed → lookup → 404. Crucially, NOT 500:
            # the handler must not bubble TypeError / AttributeError
            # from the json.dumps normalisation path. A regression that
            # removed the ``isinstance(resolution, str)`` check before
            # passing to ``queue.resolve_decision`` would surface as a
            # 500 here on dict/list bodies.
            assert resp.status_code == 404, (
                f"POST {path} body={body!r}: expected 404 "
                f"(pipeline or decision not found, validation passed), "
                f"got {resp.status_code}: {resp.text[:500]}"
            )
            env = _assert_error_envelope(resp, f"POST {path}")
            # The pipeline check fires before the decision lookup, so
            # an unknown pipeline surfaces "Pipeline … not found"; an
            # unknown decision on a real pipeline would surface
            # "Decision … not found". Both are acceptable here — we
            # don't have real-pipeline infra (filed as #2657) so we
            # observe the pipeline-not-found path. The point of the
            # assertion is that the envelope names what was missing,
            # not which check fired first.
            msg = (env.get("message") or "").lower()
            assert "pipeline" in msg or "decision" in msg, (
                f"POST {path}: 404 envelope should reference the missing "
                f"pipeline or decision; got {env!r}"
            )


class TestHitlCancelOnUnknownDecision:
    """``/cancel`` with valid auth on a missing decision returns a structured 404.

    The handler is body-less (no payload to validate) so the path
    immediately reaches the state-store lookup after auth. The 404
    envelope must carry the decision id so an operator can trace which
    cancel call missed.
    """

    def test_cancel_unknown_decision_404(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
        lifecycle_bearer: str,
    ) -> None:
        decision_id = _placeholder_decision_id()
        path = f"/api/v1/pipelines/{regression_pipeline_id}/decisions/{decision_id}/cancel"
        resp = _call(
            orchestrator_url,
            "POST",
            path,
            headers={"Authorization": lifecycle_bearer},
        )
        # Pipeline doesn't exist → 404 via PipelineNotFoundError.
        # If the pipeline DID exist we'd 404 via DecisionNotFoundError
        # with the decision id in the message. Both paths must surface
        # the envelope; we only have the no-pipeline path reachable
        # without real-pipeline infra (filed as a follow-up).
        assert resp.status_code == 404, (
            f"POST {path} with auth: expected 404, got {resp.status_code}: {resp.text[:500]}"
        )
        env = _assert_error_envelope(resp, f"POST {path}")
        # The pipeline-not-found message references the pipeline id;
        # a refactor that consolidated 404 paths without preserving the
        # id would break operator triage.
        assert regression_pipeline_id in (env.get("message") or ""), (
            f"POST {path}: 404 envelope should reference the pipeline id; got {env!r}"
        )


class TestHitlOversizedPayload:
    """The orchestrator must reject pathologically large bodies cleanly.

    Werkzeug enforces ``MAX_CONTENT_LENGTH`` if set; without it the
    handler still has to cope. The unauthenticated ``POST /decisions``
    is the most exposed surface (anything in-cluster can queue) so a
    DoS-shaped body — 50 MB of JSON — must surface as a structured 4xx,
    never a 500 or a hang past our test timeout.
    """

    def test_large_question_payload(
        self,
        orchestrator_url: str,
        regression_pipeline_id: str,
    ) -> None:
        # ~5 MB question — big enough to stress the handler without
        # making the test runner OOM under parallel execution. The
        # field has no documented max; if the handler accepts this
        # silently, a follow-up should add a cap. The point of this
        # test is "doesn't hang, doesn't 500" — any structured
        # 4xx/2xx response inside the 30-second budget is acceptable.
        huge_question = "x" * (5 * 1024 * 1024)
        path = f"/api/v1/pipelines/{regression_pipeline_id}/decisions"
        resp = _call(
            orchestrator_url,
            "POST",
            path,
            json_body={"question": huge_question},
            timeout=30,
        )
        assert resp.status_code < 500, (
            f"POST {path} with 5 MB body: handler must not 500; got "
            f"{resp.status_code}: {resp.text[:500]}"
        )
        # The body shape is JSON for any 4xx the handler emits; on a
        # 413 from Werkzeug we'd still expect JSON because the
        # orchestrator's error mapper renders it.
        if resp.status_code >= 400:
            _assert_error_envelope(resp, f"POST {path} (5 MB body)")


def test_deterministic_pipeline_id_is_syntactically_valid() -> None:
    """The helper's output must match ``PIPELINE_ID_PATTERN``.

    A regression here would silently turn every 404 assertion in this
    module into a 400 assertion — the entire HITL-route coverage above
    would still pass with no real signal. The recovered #2474 attempt
    used ``regression-<12hex>`` which trips this exact failure mode and
    is part of why that branch was abandoned.
    """
    # Sample a handful of nodeids to make sure we don't accidentally
    # emit a shape that only works for one input.
    samples = [
        "integration_tests/regression/test_hitl_round_trip.py::test_a",
        "integration_tests/regression/test_hitl_round_trip.py::test_b",
        "tests/x[param-1]",
    ]
    import re as _re

    pattern = _re.compile(
        r"^("
        r"issue-[0-9]+(-[a-z0-9]+)*"
        r"|[A-Z][A-Z0-9]+-[0-9]+(-[a-z0-9]+)*"
        r"|local-[0-9a-f]{8}"
        r"|pipeline-[0-9a-f]{8}"
        r"|pr-[0-9]+"
        r")$"
    )
    for nodeid in samples:
        pid = deterministic_pipeline_id(nodeid)
        assert pattern.match(pid), (
            f"deterministic_pipeline_id({nodeid!r}) = {pid!r} does not match "
            "state_store.PIPELINE_ID_PATTERN — 404 assertions would silently "
            "turn into 400 assertions in regression tests."
        )

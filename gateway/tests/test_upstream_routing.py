"""Tests for the gateway-as-single-router routing policy and fallback chain
(issue #2987).

Two layers:

1. ``routing_policy`` unit tests — YAML parsing (fail-open posture, malformed
   entries dropped), trigger defaults/overrides, and the mtime-invalidated
   manager.
2. ``proxy_anthropic_messages`` / ``proxy_count_tokens`` integration tests
   driven through the Flask test client — the load-bearing behaviors:
   - no-op invariant (empty policy ⇒ byte-identical single-hop send),
   - reactive fallback on a 429 quota trigger (streaming + non-streaming),
   - the MUST-FIX **credential bleed** guard: a litellm→anthropic fallback
     hop must carry Anthropic's ``Authorization`` and NOT the stale litellm
     ``x-api-key``,
   - the model rewrite on a fallback/switchover hop,
   - 5xx defaulting to same-upstream retry (no silent escalation),
   - opt-in 5xx escalation,
   - count_tokens honoring a proactive switchover.
"""

from __future__ import annotations

import json
import os
import types
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic_credentials import AnthropicCredential
from egg_session_placeholder import to_placeholder
from routing_policy import (
    DEFAULT_ADVANCE_ON,
    DEFAULT_RETRY_SAME_MAX,
    DEFAULT_RETRY_SAME_ON,
    EMPTY_POLICY,
    RouteHop,
    RoutingPolicy,
    RoutingPolicyManager,
    TriggerConfig,
    parse_routing_policy,
)

# conftest.py loads the gateway modules under bare names.
import gateway

# ---------------------------------------------------------------------------
# routing_policy — parsing
# ---------------------------------------------------------------------------


class TestParseRoutingPolicy:
    def test_none_yields_empty(self) -> None:
        assert parse_routing_policy(None) is EMPTY_POLICY

    def test_non_mapping_fails_open_to_empty(self) -> None:
        # A list / scalar at the top level is a malformed file — fail open.
        assert parse_routing_policy([1, 2, 3]).is_empty
        assert parse_routing_policy("nope").is_empty

    def test_switchover_and_fallbacks_parse(self) -> None:
        policy = parse_routing_policy(
            {
                "switchover": {
                    "deepseek-v4-flash": {"upstream": "litellm", "model": "deepseek-v4-pro"},
                },
                "fallbacks": {
                    "deepseek-v4-flash": [
                        {"upstream": "litellm", "model": "deepseek-v4-pro"},
                        {"upstream": "anthropic", "model": "claude-opus-4-8"},
                    ],
                },
            }
        )
        assert policy.switchover_for("deepseek-v4-flash") == RouteHop("litellm", "deepseek-v4-pro")
        chain = policy.fallback_chain_for("deepseek-v4-flash")
        assert chain == (
            RouteHop("litellm", "deepseek-v4-pro"),
            RouteHop("anthropic", "claude-opus-4-8"),
        )
        assert not policy.is_empty

    def test_unknown_wire_model_returns_no_route(self) -> None:
        policy = parse_routing_policy(
            {"fallbacks": {"x": [{"upstream": "anthropic", "model": "m"}]}}
        )
        assert policy.switchover_for("other") is None
        assert policy.fallback_chain_for("other") == ()
        assert policy.switchover_for(None) is None
        assert policy.fallback_chain_for(None) == ()

    def test_hop_without_upstream_is_dropped(self) -> None:
        policy = parse_routing_policy(
            {
                "switchover": {"a": {"model": "m"}},  # missing upstream
                "fallbacks": {
                    "b": [
                        {"upstream": "anthropic", "model": "ok"},
                        {"model": "bad"},  # missing upstream — dropped
                    ]
                },
            }
        )
        assert policy.switchover_for("a") is None
        # The well-formed hop survives; the malformed one is dropped.
        assert policy.fallback_chain_for("b") == (RouteHop("anthropic", "ok"),)

    def test_fallback_chain_must_be_a_list(self) -> None:
        policy = parse_routing_policy({"fallbacks": {"b": {"upstream": "anthropic"}}})
        assert policy.fallback_chain_for("b") == ()

    def test_model_optional(self) -> None:
        policy = parse_routing_policy({"switchover": {"a": {"upstream": "litellm"}}})
        assert policy.switchover_for("a") == RouteHop("litellm", None)


class TestParseTriggers:
    def test_defaults_when_absent(self) -> None:
        policy = parse_routing_policy({"fallbacks": {}})
        assert policy.triggers.advance_on == DEFAULT_ADVANCE_ON
        assert policy.triggers.retry_same_on == DEFAULT_RETRY_SAME_ON
        assert policy.triggers.retry_same_max == DEFAULT_RETRY_SAME_MAX

    def test_default_advance_is_quota_only(self) -> None:
        # The whole cost argument rides on this: 5xx must NOT advance by
        # default (no silent Opus spend / masked defect).
        assert DEFAULT_ADVANCE_ON == frozenset({429})
        assert 500 not in DEFAULT_ADVANCE_ON
        assert 500 in DEFAULT_RETRY_SAME_ON

    def test_overrides_parse(self) -> None:
        policy = parse_routing_policy(
            {"triggers": {"advance_on": [429, 500], "retry_same_on": [503], "retry_same_max": 2}}
        )
        assert policy.triggers.advance_on == frozenset({429, 500})
        assert policy.triggers.retry_same_on == frozenset({503})
        assert policy.triggers.retry_same_max == 2

    def test_malformed_triggers_degrade_to_defaults(self) -> None:
        policy = parse_routing_policy({"triggers": {"advance_on": "429", "retry_same_max": -1}})
        assert policy.triggers.advance_on == DEFAULT_ADVANCE_ON
        assert policy.triggers.retry_same_max == DEFAULT_RETRY_SAME_MAX


# ---------------------------------------------------------------------------
# routing_policy — manager (mtime cache, fail-open)
# ---------------------------------------------------------------------------


class TestRoutingPolicyManager:
    def test_missing_file_is_noop(self, tmp_path) -> None:
        mgr = RoutingPolicyManager(policy_path=tmp_path / "absent.yaml")
        assert mgr.get_policy() is EMPTY_POLICY

    def test_valid_file_loads(self, tmp_path) -> None:
        path = tmp_path / "routing-policy.yaml"
        path.write_text(
            "fallbacks:\n  m:\n    - upstream: anthropic\n      model: claude-opus-4-8\n"
        )
        mgr = RoutingPolicyManager(policy_path=path)
        policy = mgr.get_policy()
        assert policy.fallback_chain_for("m") == (RouteHop("anthropic", "claude-opus-4-8"),)

    def test_malformed_yaml_fails_open(self, tmp_path) -> None:
        path = tmp_path / "routing-policy.yaml"
        path.write_text("this: : : not valid yaml\n  - x\n")
        mgr = RoutingPolicyManager(policy_path=path)
        assert mgr.get_policy().is_empty

    def test_mtime_reload(self, tmp_path) -> None:
        path = tmp_path / "routing-policy.yaml"
        path.write_text("switchover:\n  m:\n    upstream: litellm\n")
        mgr = RoutingPolicyManager(policy_path=path)
        assert mgr.get_policy().switchover_for("m") == RouteHop("litellm", None)

        # Rewrite + bump mtime forward so the cache invalidates.
        path.write_text("switchover:\n  m:\n    upstream: anthropic\n    model: claude-opus-4-8\n")
        stat = path.stat()
        os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
        assert mgr.get_policy().switchover_for("m") == RouteHop("anthropic", "claude-opus-4-8")


# ---------------------------------------------------------------------------
# gateway helpers
# ---------------------------------------------------------------------------


class TestRoutingHelpers:
    def test_extract_wire_model(self) -> None:
        assert gateway._extract_wire_model(b'{"model": "m"}') == "m"
        assert gateway._extract_wire_model(b"not json") is None
        assert gateway._extract_wire_model(b'{"no": "model"}') is None
        assert gateway._extract_wire_model(b'{"model": 5}') is None

    def test_rewrite_upstream_model(self) -> None:
        out = gateway._rewrite_upstream_model(b'{"model": "old", "x": 1}', "new")
        assert json.loads(out) == {"model": "new", "x": 1}

    def test_rewrite_upstream_model_parse_miss_returns_original(self) -> None:
        assert gateway._rewrite_upstream_model(b"not json", "new") == b"not json"

    def test_classify_route_status(self) -> None:
        triggers = TriggerConfig(
            advance_on=frozenset({429}),
            retry_same_on=frozenset({500}),
            retry_same_max=1,
        )
        # 429 advances when a fallback exists, accepts when last.
        assert gateway._classify_route_status(429, triggers, 0, is_last_hop=False) == "advance"
        assert gateway._classify_route_status(429, triggers, 0, is_last_hop=True) == "accept"
        # 500 retries same once, then (budget gone) accepts (not in advance_on).
        assert gateway._classify_route_status(500, triggers, 0, is_last_hop=True) == "retry_same"
        assert gateway._classify_route_status(500, triggers, 1, is_last_hop=True) == "accept"
        # 200 always accepts.
        assert gateway._classify_route_status(200, triggers, 0, is_last_hop=False) == "accept"

    def test_classify_retry_precedes_advance(self) -> None:
        # A code in BOTH sets retries same first, then escalates.
        triggers = TriggerConfig(
            advance_on=frozenset({500}),
            retry_same_on=frozenset({500}),
            retry_same_max=1,
        )
        assert gateway._classify_route_status(500, triggers, 0, is_last_hop=False) == "retry_same"
        assert gateway._classify_route_status(500, triggers, 1, is_last_hop=False) == "advance"


# ---------------------------------------------------------------------------
# Proxy integration — fakes
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """Stand-in for an ``httpx`` streamed response."""

    def __init__(
        self,
        status_code: int = 200,
        chunks: tuple[bytes, ...] = (b"data: {}\n\n",),
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._chunks = list(chunks)
        self.headers = dict(headers or {"content-type": "text/event-stream"})
        self.content = b"".join(self._chunks)
        self.closed = False

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    """Records outbound (headers, body) per call; replays a scripted response.

    ``script`` items are ``_FakeStreamResponse`` instances or ``Exception``
    instances to raise (transport failures). The last item repeats if the
    gateway makes more calls than scripted.
    """

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.idx = 0
        self.sent: list[tuple[dict[str, str], bytes | None]] = []

    def _take(self) -> object:
        item = self._script[min(self.idx, len(self._script) - 1)]
        self.idx += 1
        if isinstance(item, Exception):
            raise item
        return item

    def build_request(self, method: str, url: str, headers=None, content=None) -> object:
        self.sent.append((dict(headers or {}), content))
        return MagicMock(name="request")

    def send(self, request: object, stream: bool = False) -> object:
        return self._take()

    def post(self, url: str, headers=None, content=None) -> object:
        self.sent.append((dict(headers or {}), content))
        return self._take()

    @property
    def last_headers(self) -> dict[str, str]:
        return self.sent[-1][0]

    @property
    def last_body(self) -> bytes | None:
        return self.sent[-1][1]


class _FakeRegistry:
    def __init__(self, litellm_client: _FakeClient) -> None:
        self._litellm = litellm_client

    def is_known(self, name: str) -> bool:
        return name in ("anthropic", "litellm")

    def known_upstreams(self) -> tuple[str, ...]:
        return ("anthropic", "litellm")

    def get(self, name: str):
        if name == "litellm":
            return self._litellm, (lambda: None)
        raise gateway.UnknownUpstreamError(name)


class _FakePolicyManager:
    def __init__(self, policy: RoutingPolicy) -> None:
        self._policy = policy

    def get_policy(self) -> RoutingPolicy:
        return self._policy


_ANTHROPIC_CRED = AnthropicCredential("Authorization", "Bearer oauth-tok-xyz")
_LITELLM_CRED = AnthropicCredential("x-api-key", "litellm-master-key-abc")


def _cred_mgr(cred: AnthropicCredential) -> MagicMock:
    m = MagicMock()
    m.get_credential.return_value = cred
    return m


@contextmanager
def _routed(
    policy: RoutingPolicy,
    anthropic_script: list[object] | None = None,
    litellm_script: list[object] | None = None,
):
    """Patch the gateway's upstream clients, credential managers, and routing
    policy for one request, yielding the two fake clients."""
    fake_anthropic = _FakeClient(anthropic_script or [_FakeStreamResponse(200)])
    fake_litellm = _FakeClient(litellm_script or [_FakeStreamResponse(200)])
    with (
        patch.object(gateway, "get_anthropic_client", return_value=fake_anthropic),
        patch.object(gateway, "get_upstream_registry", return_value=_FakeRegistry(fake_litellm)),
        patch.object(gateway, "get_credentials_manager", return_value=_cred_mgr(_ANTHROPIC_CRED)),
        patch.object(
            gateway, "get_litellm_credentials_manager", return_value=_cred_mgr(_LITELLM_CRED)
        ),
        patch.object(
            gateway, "get_routing_policy_manager", return_value=_FakePolicyManager(policy)
        ),
    ):
        yield fake_anthropic, fake_litellm


@pytest.fixture
def client():
    gateway.app.config["TESTING"] = True
    with gateway.app.test_client() as c:
        yield c


def _register_session(upstream: str = "anthropic", mode: str = "public", **session_fields) -> str:
    """Register a session and return the placeholder-wrapped auth value.

    ``session_fields`` passes through to ``register_session`` (e.g.
    ``pipeline_id`` / ``agent_role`` / ``phase`` for the attribution tests).
    """
    sm = gateway.get_session_manager()
    token, _session = sm.register_session(
        container_id=f"c-routing-{upstream}-{os.urandom(4).hex()}",
        upstream=upstream,
        mode=mode,
        **session_fields,
    )
    return to_placeholder(token)


def _messages_body(model: str, *, stream: bool) -> bytes:
    return json.dumps(
        {
            "model": model,
            "stream": stream,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()


# ---------------------------------------------------------------------------
# Proxy integration — behaviors
# ---------------------------------------------------------------------------


class TestNoOpInvariant:
    def test_empty_policy_single_hop_streaming(self, client) -> None:
        auth = _register_session(upstream="anthropic")
        with _routed(EMPTY_POLICY) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("claude-opus-4-8", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert anthropic.idx == 1  # exactly one upstream send
        assert litellm.idx == 0  # litellm never touched
        # Body forwarded unchanged (no model rewrite on the default route).
        assert json.loads(anthropic.last_body)["model"] == "claude-opus-4-8"
        # Anthropic credential injected; no litellm key present.
        assert anthropic.last_headers.get("Authorization") == "Bearer oauth-tok-xyz"
        assert "x-api-key" not in anthropic.last_headers


class TestFallbackOnQuota:
    def _flash_to_opus_policy(self) -> RoutingPolicy:
        return parse_routing_policy(
            {
                "fallbacks": {
                    "deepseek-v4-flash": [{"upstream": "anthropic", "model": "claude-opus-4-8"}],
                }
            }
        )

    def test_streaming_429_advances_to_anthropic(self, client) -> None:
        auth = _register_session(upstream="litellm")
        with _routed(
            self._flash_to_opus_policy(),
            anthropic_script=[_FakeStreamResponse(200, chunks=(b"data: {}\n\n",))],
            litellm_script=[_FakeStreamResponse(429, chunks=(b'{"error":"rate"}',))],
        ) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert litellm.idx == 1  # primary hop tried once, 429
        assert anthropic.idx == 1  # advanced to the fallback
        # Fallback hop rewrote the model to the real Claude id.
        assert json.loads(anthropic.last_body)["model"] == "claude-opus-4-8"
        # Primary kept the original wire model.
        assert json.loads(litellm.last_body)["model"] == "deepseek-v4-flash"

    def test_credential_bleed_guard_on_fallback_hop(self, client) -> None:
        # The MUST-FIX: the open→anthropic hop must carry Anthropic's
        # Authorization and NOT a stale litellm x-api-key from the prior hop.
        auth = _register_session(upstream="litellm")
        with _routed(
            self._flash_to_opus_policy(),
            anthropic_script=[_FakeStreamResponse(200)],
            litellm_script=[_FakeStreamResponse(429, chunks=(b'{"error":"rate"}',))],
        ) as (anthropic, litellm):
            client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        # litellm hop: master key only, no Anthropic Authorization.
        assert litellm.last_headers.get("x-api-key") == "litellm-master-key-abc"
        assert "Authorization" not in litellm.last_headers
        # anthropic hop: OAuth only, NO leaked litellm key.
        assert anthropic.last_headers.get("Authorization") == "Bearer oauth-tok-xyz"
        assert "x-api-key" not in anthropic.last_headers

    def test_non_streaming_429_advances(self, client) -> None:
        auth = _register_session(upstream="litellm")
        with _routed(
            self._flash_to_opus_policy(),
            anthropic_script=[_FakeStreamResponse(200, chunks=(b'{"ok":true}',))],
            litellm_script=[_FakeStreamResponse(429, chunks=(b'{"error":"rate"}',))],
        ) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=False),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert litellm.idx == 1
        assert anthropic.idx == 1
        assert "x-api-key" not in anthropic.last_headers


class TestAttributionHeaders:
    """Gateway-authoritative ``x-egg-*`` attribution on non-Anthropic hops (#3175).

    The litellm hop must carry the session's pipeline/role/phase so the
    egg-litellm cost callback can attribute spend per role; the Anthropic
    path must stay byte-identical (no stamped headers); and agent-supplied
    ``x-egg-*`` headers must never survive onto a non-Anthropic hop.
    """

    _SESSION_FIELDS = {
        "pipeline_id": "pipeline-20260612-abc",
        "agent_role": "reviewer_code",
        "phase": "implement",
    }

    def test_litellm_hop_carries_attribution(self, client) -> None:
        auth = _register_session(upstream="litellm", **self._SESSION_FIELDS)
        with _routed(EMPTY_POLICY) as (_anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert litellm.last_headers.get("x-egg-pipeline-id") == "pipeline-20260612-abc"
        assert litellm.last_headers.get("x-egg-agent-role") == "reviewer_code"
        assert litellm.last_headers.get("x-egg-phase") == "implement"

    def test_count_tokens_hop_carries_attribution(self, client) -> None:
        auth = _register_session(upstream="litellm", **self._SESSION_FIELDS)
        with _routed(EMPTY_POLICY) as (_anthropic, litellm):
            client.post(
                "/v1/messages/count_tokens",
                data=_messages_body("deepseek-v4-flash", stream=False),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert litellm.last_headers.get("x-egg-agent-role") == "reviewer_code"

    def test_unset_session_fields_emit_no_headers(self, client) -> None:
        # A session with only a role: no empty-valued pipeline/phase headers.
        auth = _register_session(upstream="litellm", agent_role="coder")
        with _routed(EMPTY_POLICY) as (_anthropic, litellm):
            client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert litellm.last_headers.get("x-egg-agent-role") == "coder"
        assert "x-egg-pipeline-id" not in litellm.last_headers
        assert "x-egg-phase" not in litellm.last_headers

    def test_agent_supplied_x_egg_headers_are_stripped(self, client) -> None:
        # The sandbox controls its own request headers; a forged x-egg-*
        # value must never masquerade as gateway attribution on the litellm
        # hop — neither overriding a real field nor smuggling a novel key.
        auth = _register_session(upstream="litellm", **self._SESSION_FIELDS)
        with _routed(EMPTY_POLICY) as (_anthropic, litellm):
            client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={
                    "x-api-key": auth,
                    "content-type": "application/json",
                    "x-egg-agent-role": "trusted_user",
                    "x-egg-forged": "1",
                    # Mixed-case forgery: the strip lowercases the key, so an
                    # X-Egg-... header must be dropped too, not slip through.
                    "X-Egg-Pipeline-Id": "forged-pipeline",
                },
            )
        assert litellm.last_headers.get("x-egg-agent-role") == "reviewer_code"
        assert litellm.last_headers.get("x-egg-pipeline-id") == "pipeline-20260612-abc"
        assert "x-egg-forged" not in litellm.last_headers

    def test_anthropic_hop_is_not_stamped(self, client) -> None:
        # No-op invariant: the Claude path carries no attribution headers.
        auth = _register_session(upstream="anthropic", **self._SESSION_FIELDS)
        with _routed(EMPTY_POLICY) as (anthropic, _litellm):
            client.post(
                "/v1/messages",
                data=_messages_body("claude-opus-4-8", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert not [k for k in anthropic.last_headers if k.lower().startswith("x-egg-")]

    def test_fallback_to_anthropic_drops_attribution(self, client) -> None:
        # litellm 429 → anthropic fallback: the litellm hop is stamped, the
        # anthropic hop is not (scoping mirrors the credential-bleed guard).
        auth = _register_session(upstream="litellm", **self._SESSION_FIELDS)
        policy = parse_routing_policy(
            {
                "fallbacks": {
                    "deepseek-v4-flash": [{"upstream": "anthropic", "model": "claude-opus-4-8"}],
                }
            }
        )
        with _routed(
            policy,
            anthropic_script=[_FakeStreamResponse(200)],
            litellm_script=[_FakeStreamResponse(429, chunks=(b'{"error":"rate"}',))],
        ) as (anthropic, litellm):
            client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert litellm.last_headers.get("x-egg-agent-role") == "reviewer_code"
        assert not [k for k in anthropic.last_headers if k.lower().startswith("x-egg-")]

    def test_header_values_are_sanitized(self) -> None:
        # CR/LF (header injection) and control chars dropped; length capped.
        assert gateway._sanitize_attribution_value("a\r\nx-evil: 1") == "ax-evil: 1"
        assert gateway._sanitize_attribution_value("rôle") == "rle"
        assert len(gateway._sanitize_attribution_value("x" * 1000)) == 256

    def test_control_only_value_emits_no_header(self) -> None:
        # A field of only control chars sanitizes to "" — no empty-valued
        # header should be stamped at all.
        session = types.SimpleNamespace(pipeline_id="\r\n", agent_role="coder", phase=None)
        out = gateway._with_attribution_headers({}, session)
        assert "x-egg-pipeline-id" not in out
        assert out["x-egg-agent-role"] == "coder"


class TestTransientFivexx:
    def test_5xx_retries_same_then_surfaces(self, client) -> None:
        # Default triggers: 500 retries the SAME upstream once, then surfaces
        # (no escalation to a different/more-expensive model).
        auth = _register_session(upstream="litellm")
        with _routed(
            EMPTY_POLICY,
            litellm_script=[
                _FakeStreamResponse(500, chunks=(b'{"error":"boom"}',)),
                _FakeStreamResponse(500, chunks=(b'{"error":"boom"}',)),
            ],
        ) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 500  # surfaced, not escalated
        assert litellm.idx == 2  # original + one same-hop retry
        assert anthropic.idx == 0  # never escalated to Opus

    def test_5xx_retry_succeeds(self, client) -> None:
        auth = _register_session(upstream="litellm")
        with _routed(
            EMPTY_POLICY,
            litellm_script=[
                _FakeStreamResponse(503, chunks=(b'{"error":"boom"}',)),
                _FakeStreamResponse(200, chunks=(b"data: {}\n\n",)),
            ],
        ) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("deepseek-v4-flash", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert litellm.idx == 2
        assert anthropic.idx == 0

    def test_opt_in_5xx_escalation(self, client) -> None:
        # Operator opts into escalating 500 to a fallback (no same-hop retry).
        policy = parse_routing_policy(
            {
                "triggers": {"advance_on": [500], "retry_same_on": [], "retry_same_max": 0},
                "fallbacks": {"m": [{"upstream": "anthropic", "model": "claude-opus-4-8"}]},
            }
        )
        auth = _register_session(upstream="litellm")
        with _routed(
            policy,
            anthropic_script=[_FakeStreamResponse(200)],
            litellm_script=[_FakeStreamResponse(500, chunks=(b'{"error":"boom"}',))],
        ) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("m", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert litellm.idx == 1  # no same-hop retry
        assert anthropic.idx == 1  # escalated


class TestTransportFallback:
    def test_streaming_connect_error_advances(self, client) -> None:
        policy = parse_routing_policy(
            {"fallbacks": {"m": [{"upstream": "anthropic", "model": "claude-opus-4-8"}]}}
        )
        auth = _register_session(upstream="litellm")
        with _routed(
            policy,
            anthropic_script=[_FakeStreamResponse(200)],
            litellm_script=[httpx.ConnectError("litellm down")],
        ) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("m", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert anthropic.idx == 1

    def test_last_hop_connect_error_is_502(self, client) -> None:
        # No fallback ⇒ a connect error surfaces as today's 502 contract.
        auth = _register_session(upstream="litellm")
        with _routed(EMPTY_POLICY, litellm_script=[httpx.ConnectError("down")]) as (
            anthropic,
            litellm,
        ):
            resp = client.post(
                "/v1/messages",
                data=_messages_body("m", stream=True),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 502
        assert anthropic.idx == 0


class TestCountTokensSwitchover:
    def test_count_tokens_follows_switchover(self, client) -> None:
        policy = parse_routing_policy(
            {"switchover": {"m": {"upstream": "litellm", "model": "qwen3-coder"}}}
        )
        auth = _register_session(upstream="anthropic")
        with _routed(
            policy,
            litellm_script=[_FakeStreamResponse(200, chunks=(b'{"input_tokens":5}',))],
        ) as (anthropic, litellm):
            resp = client.post(
                "/v1/messages/count_tokens",
                data=json.dumps({"model": "m", "messages": []}).encode(),
                headers={"x-api-key": auth, "content-type": "application/json"},
            )
        assert resp.status_code == 200
        assert litellm.idx == 1  # routed to the switchover target
        assert anthropic.idx == 0
        assert json.loads(litellm.last_body)["model"] == "qwen3-coder"
        assert litellm.last_headers.get("x-api-key") == "litellm-master-key-abc"

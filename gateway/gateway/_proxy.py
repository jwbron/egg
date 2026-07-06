"""Gateway proxy cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple

import httpx
from egg_session_placeholder import from_placeholder as _session_token_from_placeholder
from flask import Response, jsonify, request, stream_with_context

try:
    from ..routing_policy import (
        RouteHop,
    )
    from ..upstream_registry import (
        UnknownUpstreamError,
    )
except ImportError:  # flat/container import mode
    from routing_policy import (  # type: ignore[no-redef, import-untyped]
        RouteHop,
    )
    from upstream_registry import (  # type: ignore[no-redef, import-untyped]
        UnknownUpstreamError,
    )


def _b() -> Any:
    """Return the gateway barrel for call-time lookup of patched symbols.

    Seam getters/validators and gateway-local helpers are patched by tests at
    ``gateway.gateway.<name>``; resolving them on the barrel at call time keeps
    those patches effective after the split.
    """
    import sys

    return sys.modules.get("gateway.gateway") or sys.modules["gateway"]


class _BarrelLogger:
    """Proxy to the barrel ``logger`` so tests patching ``gateway.logger``
    observe log calls emitted from this submodule."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_b().logger, name)


logger: Any = _BarrelLogger()


def _get_forwarded_headers(request_headers: Any) -> dict[str, str]:
    """Forward all headers except blocked ones (blocklist approach)."""
    return {k: v for k, v in request_headers if k.lower() not in _b().ANTHROPIC_BLOCKED_HEADERS}


def _filter_response_headers(headers: Any) -> dict[str, str]:
    """Filter response headers for passthrough."""
    # Preserve important headers like x-request-id for debugging
    skip = {"content-encoding", "transfer-encoding", "connection"}
    return {k: v for k, v in headers.items() if k.lower() not in skip}


def _inject_upstream_credentials(
    headers: dict[str, str],
    upstream: str = "anthropic",
) -> tuple[dict[str, str], tuple[Any, int] | None]:
    """
    Inject upstream credentials into headers.

    Dispatches per-upstream so the gateway can carry both Anthropic and
    LiteLLM credentials side-by-side (issue #2769 cq-7). For the Anthropic
    upstream this is byte-identical to the legacy
    ``_inject_anthropic_credentials`` helper — same OAuth/API-key precedence,
    same 401 error shape on missing credentials, same client-supplied auth
    fall-through. The LiteLLM upstream uses ``x-api-key`` only (no OAuth
    path) and has no client-supplied-auth fall-through because Claude Code
    never carries a LiteLLM master key.

    An upstream the registry does not serve is rejected with a 502 — it
    is never silently treated as Anthropic.

    Args:
        headers: Mutable header dict — credential is appended in place.
        upstream: ``"anthropic"`` (default — back-compat) or ``"litellm"``.

    Returns:
        (headers, None) on success
        (headers, error_response_tuple) on failure - caller should return this
    """
    # Refuse to silently treat an unknown upstream as Anthropic. Falling
    # through to the Anthropic branch produced an observable error-code
    # inconsistency — 401 vs 502 for the same invalid input depending on
    # unrelated Anthropic-credential state. An unregistered upstream now
    # fails closed with a 502, matching the proxy routes' own
    # UnknownUpstreamError handling (issue #2769 review).
    if not _b().get_upstream_registry().is_known(upstream):
        logger.warning(
            "Unknown upstream for credential injection, refusing request",
            upstream=upstream,
        )
        return headers, (
            jsonify(
                {
                    "error": {
                        "type": "api_error",
                        "message": f"Unknown upstream '{upstream}'",
                    }
                }
            ),
            502,
        )

    if upstream == "litellm":
        cred = _b().get_litellm_credentials_manager().get_credential()
        if cred:
            headers[cred.header_name] = cred.header_value
            return headers, None
        logger.warning(
            "No LiteLLM master key available for proxy request",
            upstream=upstream,
        )
        return headers, (
            jsonify(
                {
                    "error": {
                        "type": "authentication_error",
                        "message": "No LiteLLM credentials available",
                    }
                }
            ),
            401,
        )

    # Default: anthropic upstream — preserves the legacy behavior verbatim.
    credentials_manager = _b().get_credentials_manager()
    cred = credentials_manager.get_credential()

    if cred:
        # Credential includes header_name (x-api-key or Authorization)
        # and header_value (raw key or "Bearer <token>")
        headers[cred.header_name] = cred.header_value
        return headers, None

    # No gateway-managed credentials - check if client sent auth
    # This allows OAuth mode where Claude Code manages its own tokens
    client_auth = headers.get("Authorization")
    client_api_key = headers.get("x-api-key")
    if client_auth or client_api_key:
        return headers, None

    logger.warning(
        "No Anthropic credentials available for proxy request",
        has_gateway_cred=False,
        has_client_auth=bool(client_auth),
        has_client_api_key=bool(client_api_key),
    )
    return headers, (
        jsonify(
            {
                "error": {
                    "type": "authentication_error",
                    "message": "No Anthropic credentials available",
                }
            }
        ),
        401,
    )


def _inject_anthropic_credentials(
    headers: dict[str, str],
) -> tuple[dict[str, str], tuple[Any, int] | None]:
    """Back-compat alias delegating to the upstream-aware injector.

    Kept so external test mocks targeting ``_inject_anthropic_credentials``
    continue to work. New code paths should call
    ``_inject_upstream_credentials(headers, upstream)`` directly.
    """
    return _inject_upstream_credentials(headers, upstream="anthropic")


def _extract_wire_model(request_body: bytes) -> str | None:
    """Return the request body's ``"model"`` field, or ``None`` on parse miss."""
    try:
        model = json.loads(request_body).get("model")
    except json.JSONDecodeError, TypeError, AttributeError:
        return None
    return model if isinstance(model, str) else None


def _rewrite_upstream_model(request_body: bytes, model: str) -> bytes:
    """Set ``body["model"] = model`` and re-encode; original bytes on parse miss.

    A narrowly-scoped reintroduction of the helper #2832 retired. #2832
    removed the *unconditional* LiteLLM-path rewrite (Claude Code now sends
    the wire model directly); this version fires ONLY when a routing-policy
    hop names an explicit target model, so the no-policy path is
    byte-identical — the body is never touched unless a switchover or
    fallback hop specifies a ``model``.
    """
    try:
        body = json.loads(request_body)
    except json.JSONDecodeError, TypeError:
        return request_body
    if not isinstance(body, dict):
        return request_body
    body["model"] = model
    return json.dumps(body).encode()


def _resolve_route_chain(
    session_upstream: str,
    request_body: bytes,
) -> tuple[list[RouteHop], Any]:
    """Resolve a request into an ordered ``[RouteHop, ...]`` chain + triggers.

    Hop 0 is the *initial* route: a proactive ``switchover`` remap for the
    wire model if one is configured, else the spawn-time ``session_upstream``
    (no model rewrite — byte-identical to today). Hops 1..N are the reactive
    fallback chain for the wire model, in order. With an empty policy the
    chain is a single hop on ``session_upstream`` and behavior is
    byte-identical to the pre-#2987 path.
    """
    policy = _b().get_routing_policy_manager().get_policy()
    wire_model = _b()._extract_wire_model(request_body)

    switch = policy.switchover_for(wire_model)
    initial = switch if switch is not None else RouteHop(upstream=session_upstream, model=None)
    chain = [initial, *policy.fallback_chain_for(wire_model)]
    return chain, policy.triggers


class _PreparedHop(NamedTuple):
    """A hop ready to send: resolved client, freshly-injected headers, body."""

    client: Any
    headers: dict[str, str]
    body: bytes


class _HopPrepError(Exception):
    """Raised by ``_prepare_hop`` when a hop cannot be prepared.

    Carries the ``(Response, status)`` tuple the proxy route should return if
    this is the last hop (the caller advances to a fallback instead when one
    exists). Modeling the failure as an exception — rather than an optional
    field in the return tuple — lets the success path be a non-optional
    ``_PreparedHop`` that type-checks cleanly at the call sites.
    """

    def __init__(self, response: tuple[Any, int]) -> None:
        super().__init__("hop preparation failed")
        self.response = response


def _sanitize_attribution_value(value: str) -> str:
    """Constrain a session field to a safe HTTP header value.

    The values are orchestrator-authoritative (set via the launcher-secret
    ``register_session``), so this is belt-and-braces: drop anything outside
    printable ASCII (CR/LF would otherwise allow header injection) and cap
    the length so a pathological value cannot bloat every upstream request.
    """
    return "".join(ch for ch in value if 32 <= ord(ch) < 127)[:256]


def _with_attribution_headers(headers: dict[str, str], session: Any) -> dict[str, str]:
    """Stamp gateway-authoritative ``x-egg-*`` attribution onto a non-Anthropic hop.

    The egg-litellm ``cost_callback`` keys its per-session cost/cache log
    lines on ``x-claude-code-session-id``, which maps to a role only by hand
    cross-referencing agent completion logs (issue #3175). The gateway
    resolves the full ``Session`` — ``pipeline_id`` / ``agent_role`` /
    ``phase`` — on every ``/v1/messages`` call anyway, so it stamps them here
    and the callback logs spend per role directly.

    Any client-supplied ``x-egg-*`` header is dropped first: the sandbox
    controls its own request headers (e.g. via ``ANTHROPIC_CUSTOM_HEADERS``),
    so agent-supplied values are untrusted and must never masquerade as
    attribution. Applied only to non-Anthropic hops — the Claude path stays
    byte-identical.
    """
    headers = {k: v for k, v in headers.items() if not k.lower().startswith("x-egg-")}
    if session is None:
        return headers
    for header, value in (
        ("x-egg-pipeline-id", session.pipeline_id),
        ("x-egg-agent-role", session.agent_role),
        ("x-egg-phase", session.phase),
    ):
        if value:
            sanitized = _b()._sanitize_attribution_value(str(value))
            # A value of only control chars sanitizes to "" — don't stamp an
            # empty-valued header (the callback would coerce it to None anyway).
            if sanitized:
                headers[header] = sanitized
    return headers


def _prepare_hop(
    hop: RouteHop,
    request_headers: Any,
    request_body: bytes,
    session: Any = None,
) -> _PreparedHop:
    """Build the (client, headers, body) for one hop.

    Headers are rebuilt with ``_get_forwarded_headers`` from the *original*
    request headers on every call, then this hop's credential is injected —
    so a fallback hop never inherits the previous upstream's auth header.
    Non-Anthropic hops additionally carry ``x-egg-*`` attribution headers
    derived from ``session`` (issue #3175); see ``_with_attribution_headers``.
    Raises ``_HopPrepError`` (carrying the error response) on a credential or
    unknown-upstream failure for this hop; the caller decides whether to
    advance to a fallback or surface it.
    """
    headers = _get_forwarded_headers(request_headers)
    headers, cred_error = _inject_upstream_credentials(headers, upstream=hop.upstream)
    if cred_error:
        raise _HopPrepError(cred_error)
    if hop.upstream != "anthropic":
        headers = _b()._with_attribution_headers(headers, session)

    if hop.upstream == "anthropic":
        client = _b().get_anthropic_client()
    else:
        try:
            client, _ = _b().get_upstream_registry().get(hop.upstream)
        except UnknownUpstreamError:
            logger.warning("Unknown upstream on routing hop, refusing", upstream=hop.upstream)
            raise _HopPrepError(
                (
                    jsonify(
                        {
                            "error": {
                                "type": "api_error",
                                "message": f"Unknown upstream '{hop.upstream}'",
                            }
                        }
                    ),
                    502,
                )
            ) from None

    body = _b()._rewrite_upstream_model(request_body, hop.model) if hop.model else request_body
    return _PreparedHop(client=client, headers=headers, body=body)


def _classify_route_status(
    status: int,
    triggers: Any,
    same_hop_attempts: int,
    is_last_hop: bool,
) -> str:
    """Decide what to do with an upstream's status: retry the same hop,
    advance to the next hop, or accept the response.

    Same-hop retry takes precedence while the budget remains (so a code that
    is in *both* ``retry_same_on`` and ``advance_on`` retries first, then
    escalates). ``advance`` only fires when a fallback hop exists.
    """
    if status in triggers.retry_same_on and same_hop_attempts < triggers.retry_same_max:
        return "retry_same"
    if status in triggers.advance_on and not is_last_hop:
        return "advance"
    return "accept"


_UPSTREAM_TRANSPORT_ERRORS = (
    httpx.ReadError,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.TimeoutException,
)


def _close_quietly(resp: Any) -> None:
    """Close an httpx streaming response, swallowing errors.

    Used to release a discarded upstream connection when the routing loop
    retries the same hop or advances to a fallback — a streaming response we
    are not going to forward must be closed or its connection leaks.
    """
    try:
        resp.close()
    except Exception:
        pass


def _send_and_prime(
    client: Any,
    headers: dict[str, str],
    body: bytes,
) -> tuple[Any, Any, bytes | None]:
    """Send the upstream request and pre-fetch the first chunk.

    Returns ``(upstream_response, iterator, first_chunk)`` where
    ``first_chunk`` is ``None`` if the upstream returned an empty body.
    Raises a transport error (``httpx.ReadError`` / ``RemoteProtocolError`` /
    ``ConnectError`` / ``TimeoutException``) if the connection fails during
    ``send()`` or the first ``iter_bytes()`` call — callers use that signal
    to retry the same hop or advance to a fallback (issues #1907, #2987).
    """
    http_req = client.build_request("POST", "/v1/messages", headers=headers, content=body)
    upstream_resp = client.send(http_req, stream=True)
    try:
        iterator = upstream_resp.iter_bytes()
        try:
            first = next(iterator)
        except StopIteration:
            first = None
        return upstream_resp, iterator, first
    except BaseException:
        # Close the failed upstream so the caller's retry can open a fresh
        # connection without leaking the old one. Broad catch ensures
        # cleanup on *any* exception from iter_bytes() / next().
        try:
            upstream_resp.close()
        except Exception:
            pass
        raise


def _attempt_hop_streaming(
    client: Any,
    headers: dict[str, str],
    body: bytes,
    *,
    container_id: str | None,
) -> tuple[Any, Any, bytes | None]:
    """One streaming hop with the #1907 pre-stream transport-reset retry.

    Retries the *same* upstream once if the connection resets before any byte
    is forwarded downstream, then raises on the second failure. The
    cross-hop routing loop turns that raise into an advance-or-surface
    decision (#2987).
    """
    for attempt in range(2):
        try:
            return _send_and_prime(client, headers, body)
        except _UPSTREAM_TRANSPORT_ERRORS as reset_err:
            if attempt == 0 and isinstance(reset_err, (httpx.ReadError, httpx.RemoteProtocolError)):
                logger.warning(
                    "Upstream connection reset before any byte was forwarded; "
                    "retrying same upstream once",
                    container_id=container_id,
                    error=str(reset_err),
                )
                continue
            # Connect/timeout failures (and the exhausted reset retry) are not
            # retried in place — the routing loop advances to a fallback hop
            # if one exists, else re-raises to the outer 502/504 handler.
            raise
    # Unreachable: ``range(2)`` always returns on success or raises on the
    # second attempt. Present so the type checker sees no fall-through path.
    raise AssertionError("unreachable")  # pragma: no cover


BLOCKED_TOOLS_PRIVATE_MODE = {"web_search", "WebSearch", "web_fetch", "WebFetch"}


def _resolve_proxy_session(
    request_headers: Any,
    remote_addr: str | None,
) -> tuple[Any, tuple[Response, int] | None]:
    """
    Resolve the session for a ``/v1/messages`` (or ``/count_tokens``) proxy
    request.

    Order of resolution (issue #2829):

    1. **Token-keyed.** Extract the session token from ``x-api-key`` /
       ``Authorization`` if the value carries the egg placeholder
       envelope. The orchestrator wraps the session token in this
       placeholder so Claude Code's local OAuth-token format check
       passes while the gateway can still identify the session. This
       is the load-bearing path for agent traffic.
    2. **IP-keyed.** When the placeholder is absent, fall back to
       source-IP lookup. Pod IPs are ephemeral in k8s so this is a
       compat path for non-agent clients only (health probes, host
       dev tools). The slice-1 "no session → anthropic" invariant for
       non-agent probes is preserved.

    Defense-in-depth: when the placeholder IS present but the session
    lookup misses, return a 502. Silently falling through to the
    anthropic default would silently mis-route per-agent inference
    (the routing bug) and disable private-mode web-tool filtering (the
    filter-bypass bug). Both were invisible at runtime before the fix.

    Side effect: ``get_session`` delegates to ``validate_session``,
    which calls ``session.extend_ttl`` on every successful lookup. The
    proxy is therefore no longer a read-only consumer of the session —
    each ``/v1/messages`` (or ``/count_tokens``) call bumps the
    session's ``last_seen``, so active agent inference keeps the
    session alive without a separate heartbeat. The legacy
    ``get_session_by_ip`` fallback is still read-only.

    Returns ``(session_or_none, error_response_or_none)``. On error the
    caller MUST return the error response; on success ``session`` may
    be ``None`` for non-placeholder probes and the caller falls back
    to the anthropic default.
    """
    raw_auth = request_headers.get("x-api-key") or request_headers.get("Authorization")
    placeholder_token = _session_token_from_placeholder(raw_auth)
    session_manager = _b().get_session_manager()

    if placeholder_token:
        session = session_manager.get_session(placeholder_token)
        if session is None:
            # ``validate_session`` (called via ``get_session``) already
            # logs ``event_type=session_auth_failed`` with the token
            # hash; emitting a second warning here would double-count
            # any "auth failure rate" alert keyed off the first event.
            # Caller's ``remote_addr`` shows up in standard request
            # logs for correlation.
            return None, (
                jsonify(
                    {
                        "error": {
                            "type": "api_error",
                            "message": "Unknown or expired session",
                        }
                    }
                ),
                502,
            )
        return session, None

    # Non-agent probe (no placeholder). Try IP-keyed lookup for
    # backwards compatibility but failure here is non-fatal — the
    # caller falls through to the anthropic default.
    return session_manager.get_session_by_ip(remote_addr or ""), None


def _filter_blocked_tools(request_body: bytes, session_mode: str | None) -> bytes:
    """
    Remove blocked tools from API request when in private mode.

    In private mode, WebSearch and WebFetch bypass container network controls
    because they're processed by Anthropic's infrastructure. This creates a
    data exfiltration risk where a compromised agent could encode sensitive
    data in search queries.

    By filtering these tools at the gateway, we enforce the restriction at
    the infrastructure level where the container cannot bypass it.

    Args:
        request_body: Raw JSON request body
        session_mode: The session's mode ("private" or "public"), or None

    Returns:
        Modified request body with blocked tools removed (if in private mode),
        or original body unchanged (if in public mode or on parse error)
    """
    if session_mode != "private":
        return request_body

    try:
        body = json.loads(request_body)
        if "tools" not in body:
            return request_body

        original_tools = body["tools"]
        filtered_tools = [
            t for t in original_tools if t.get("name") not in BLOCKED_TOOLS_PRIVATE_MODE
        ]

        removed_count = len(original_tools) - len(filtered_tools)
        if removed_count > 0:
            removed_names = [
                t.get("name") for t in original_tools if t.get("name") in BLOCKED_TOOLS_PRIVATE_MODE
            ]
            logger.info(
                "Filtered blocked tools in private mode",
                removed_count=removed_count,
                removed_tools=removed_names,
            )
            body["tools"] = filtered_tools
            return json.dumps(body).encode()

    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("Failed to parse request body for tool filtering", error=str(e))

    return request_body


def _is_streaming_request(request_body: bytes) -> bool:
    """
    Check if request body indicates streaming mode.

    Parses JSON properly to avoid false positives from byte string matching.
    """
    try:
        body_json = json.loads(request_body)
        return body_json.get("stream", False) is True
    except json.JSONDecodeError, TypeError:
        return False


def proxy_anthropic_messages() -> tuple[Response, int] | Response:
    """
    Proxy messages API with credential injection and streaming support.

    This endpoint allows Claude Code to use ANTHROPIC_BASE_URL to route
    API traffic through the gateway for credential injection.

    Session lookup is token-keyed via a placeholder embedded in the
    ``x-api-key`` header (issue #2829). The orchestrator wraps the
    session token in ``sk-ant-oat01-PROXY-INJECTED-egg-session-<token>``
    so Claude Code's local format check passes; the gateway extracts
    the token and looks the session up. Non-agent probes (no
    placeholder) keep the legacy IP-keyed compat path.
    """
    session, lookup_error = _resolve_proxy_session(request.headers, request.remote_addr)
    if lookup_error:
        return lookup_error
    session_mode = session.mode if session else None
    container_id = session.container_id if session else None
    # Resolve per-session upstream (issue #2769). With no session, default to
    # "anthropic" so today's Claude path is byte-identical when an unrelated
    # client probes /v1/messages without first registering a session.
    upstream_name = session.upstream if session else "anthropic"

    request_body = request.get_data()
    request_body = _filter_blocked_tools(
        request_body, session_mode
    )  # Remove web tools in private mode
    # Per #2832, Claude Code on the LiteLLM path sends the upstream model
    # name on the wire directly (via ANTHROPIC_CUSTOM_MODEL_OPTION). The
    # gateway only rewrites ``"model"`` when a routing-policy hop names an
    # explicit target (see ``_prepare_hop`` / ``_rewrite_upstream_model``).
    is_streaming = _is_streaming_request(request_body)

    # Resolve the routing chain (issue #2987). Hop 0 is the proactive
    # ``switchover`` remap for this wire model, or — with no switchover entry
    # — the spawn-time ``session.upstream``. Hops 1..N are the reactive
    # ``fallbacks`` chain for the wire model. With an empty routing policy the
    # chain is a single hop on ``session.upstream`` and every step below is
    # byte-identical to the pre-#2987 path. ``triggers`` decides which status
    # codes retry the same upstream vs advance to the next hop; credentials
    # are rebuilt per hop inside ``_prepare_hop`` so a fallback never carries
    # the previous upstream's auth header.
    chain, triggers = _resolve_route_chain(upstream_name, request_body)
    # The upstream actually serving the request, for error/log context. The
    # outer ``except`` handlers below read this so a fallback hop's failure
    # is attributed to the hop that failed, not hop 0.
    serving_upstream = chain[0].upstream

    try:
        if is_streaming:
            # Stream SSE response using httpx's send() with stream=True
            # This gives us direct control over the response lifecycle.
            #
            # Resilience strategy (see #1907, extended for routing in #2987):
            #   (A) Pre-stream retry — if the upstream TCP connection resets
            #       before any byte has been yielded downstream, open a fresh
            #       upstream connection and retry the same hop once
            #       (``_attempt_hop_streaming``). The downstream SDK never
            #       sees the error.
            #   (B) Cross-hop fallback — if the primed upstream returns a
            #       trigger status (quota / opt-in 5xx) or a transport
            #       failure that survives (A), and a fallback hop exists,
            #       advance to it. All of this happens in the *pre-stream*
            #       window, before any byte is forwarded downstream.
            #   (C) Mid-stream synthetic error — once bytes have flowed, a
            #       reset can no longer fall back; emit a well-formed SSE
            #       ``event: error`` frame and close cleanly so the SDK fails
            #       gracefully instead of dying on a truncated socket.
            #
            # Full stream resumption is not attempted — Anthropic's API has
            # no resume tokens, and the partial generation on the wire is
            # orphaned on any mid-stream reset regardless.
            upstream: Any = None
            primed_iterator: Any = None
            first_chunk: bytes | None = None
            hop_idx = 0
            same_hop_attempts = 0
            while True:
                hop = chain[hop_idx]
                is_last_hop = hop_idx == len(chain) - 1
                serving_upstream = hop.upstream
                try:
                    prepared = _prepare_hop(hop, request.headers, request_body, session=session)
                except _HopPrepError as prep_err:
                    if is_last_hop:
                        return prep_err.response
                    logger.warning(
                        "Routing hop failed credential/upstream prep; advancing",
                        upstream=hop.upstream,
                        next_upstream=chain[hop_idx + 1].upstream,
                    )
                    hop_idx += 1
                    same_hop_attempts = 0
                    continue
                try:
                    upstream, primed_iterator, first_chunk = _attempt_hop_streaming(
                        prepared.client, prepared.headers, prepared.body, container_id=container_id
                    )
                except _UPSTREAM_TRANSPORT_ERRORS as hop_err:
                    if is_last_hop:
                        # Last hop — surface via the outer 502/504 handlers,
                        # preserving today's error contract.
                        raise
                    logger.warning(
                        "Routing hop transport failure; advancing to fallback",
                        upstream=hop.upstream,
                        next_upstream=chain[hop_idx + 1].upstream,
                        error=str(hop_err),
                    )
                    hop_idx += 1
                    same_hop_attempts = 0
                    continue

                decision = _b()._classify_route_status(
                    upstream.status_code, triggers, same_hop_attempts, is_last_hop
                )
                if decision == "retry_same":
                    same_hop_attempts += 1
                    logger.warning(
                        "Upstream returned a retryable status; retrying same upstream",
                        upstream=hop.upstream,
                        status=upstream.status_code,
                        attempt=same_hop_attempts,
                    )
                    _close_quietly(upstream)
                    continue
                if decision == "advance":
                    logger.warning(
                        "Upstream returned a fallback-trigger status; advancing",
                        upstream=hop.upstream,
                        status=upstream.status_code,
                        next_upstream=chain[hop_idx + 1].upstream,
                    )
                    _close_quietly(upstream)
                    hop_idx += 1
                    same_hop_attempts = 0
                    continue
                break  # accept this hop's response

            response_headers = _filter_response_headers(upstream.headers)
            # Forward actual Content-Type from upstream (usually text/event-stream)
            content_type = upstream.headers.get("content-type", "text/event-stream")

            def generate() -> Any:
                try:
                    if first_chunk is not None:
                        yield first_chunk
                    yield from primed_iterator
                except (httpx.ReadError, httpx.RemoteProtocolError) as mid_err:
                    # Mid-stream reset: emit a synthetic SSE `error` frame so
                    # the downstream SDK treats this as a clean API error
                    # instead of a truncated socket. The frame shape matches
                    # Anthropic's documented error event.
                    logger.warning(
                        "Upstream stream reset mid-response; emitting synthetic SSE error frame",
                        upstream=serving_upstream,
                        container_id=container_id,
                        error=str(mid_err),
                    )
                    error_payload = {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": "upstream connection reset",
                        },
                    }
                    error_frame = (
                        b"event: error\ndata: "
                        + json.dumps(error_payload).encode("utf-8")
                        + b"\n\n"
                    )
                    yield error_frame
                finally:
                    upstream.close()

            return Response(
                stream_with_context(generate()),
                status=upstream.status_code,
                headers=response_headers,
                content_type=content_type,
            )
        else:
            # Non-streaming: walk the same routing chain without priming.
            # Status-based retry/advance applies; a transport failure on a
            # non-last hop advances (else surfaces via the outer handlers,
            # preserving today's 502/504 contract).
            response: Any = None
            hop_idx = 0
            same_hop_attempts = 0
            while True:
                hop = chain[hop_idx]
                is_last_hop = hop_idx == len(chain) - 1
                serving_upstream = hop.upstream
                try:
                    prepared = _prepare_hop(hop, request.headers, request_body, session=session)
                except _HopPrepError as prep_err:
                    if is_last_hop:
                        return prep_err.response
                    hop_idx += 1
                    same_hop_attempts = 0
                    continue
                try:
                    response = prepared.client.post(
                        "/v1/messages", headers=prepared.headers, content=prepared.body
                    )
                except _UPSTREAM_TRANSPORT_ERRORS:
                    if is_last_hop:
                        raise
                    hop_idx += 1
                    same_hop_attempts = 0
                    continue

                decision = _b()._classify_route_status(
                    response.status_code, triggers, same_hop_attempts, is_last_hop
                )
                if decision == "retry_same":
                    same_hop_attempts += 1
                    continue
                if decision == "advance":
                    hop_idx += 1
                    same_hop_attempts = 0
                    continue
                break  # accept

            return Response(
                response.content,
                status=response.status_code,
                headers=_filter_response_headers(response.headers),
            )

    except httpx.ConnectError as e:
        logger.error("Upstream connection failed", upstream=serving_upstream, error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Failed to connect to {serving_upstream} upstream: {e}",
                }
            }
        ), 502

    except httpx.TimeoutException as e:
        logger.error("Upstream request timed out", upstream=serving_upstream, error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"{serving_upstream} upstream request timed out: {e}",
                }
            }
        ), 504

    except Exception as e:
        logger.exception("Upstream proxy error", upstream=serving_upstream)
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"{serving_upstream} upstream proxy error: {e}",
                }
            }
        ), 502


def proxy_count_tokens() -> tuple[Response, int] | Response:
    """
    Proxy token counting API (non-streaming).

    This endpoint allows Claude Code to use ANTHROPIC_BASE_URL to route
    token counting requests through the gateway.
    """
    # Mirror the per-session lookup used by proxy_anthropic_messages so
    # count_tokens and messages always agree on which backend serves a
    # given agent (issues #2769, #2829).
    session, lookup_error = _resolve_proxy_session(request.headers, request.remote_addr)
    if lookup_error:
        return lookup_error
    upstream_name = session.upstream if session else "anthropic"

    count_tokens_body = request.get_data()

    # Honor the proactive ``switchover`` remap so token-counting hits the
    # same backend (and model) that messages will use (issue #2987). The
    # reactive fallback chain is intentionally NOT walked here — token
    # counting is an informational pre-flight, not load-bearing inference,
    # so a quota miss surfaces rather than escalating. We take only hop 0 of
    # the resolved chain; ``_prepare_hop`` rebuilds clean headers + applies
    # the optional model rewrite for that hop.
    chain, _triggers = _resolve_route_chain(upstream_name, count_tokens_body)
    initial_hop = chain[0]
    serving_upstream = initial_hop.upstream
    try:
        prepared = _prepare_hop(initial_hop, request.headers, count_tokens_body, session=session)
    except _HopPrepError as prep_err:
        return prep_err.response

    try:
        response = prepared.client.post(
            "/v1/messages/count_tokens",
            headers=prepared.headers,
            content=prepared.body,
        )
        return Response(
            response.content,
            status=response.status_code,
            headers=_filter_response_headers(response.headers),
        )

    except httpx.ConnectError as e:
        logger.error("Upstream connection failed", upstream=serving_upstream, error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"Failed to connect to {serving_upstream} upstream: {e}",
                }
            }
        ), 502

    except httpx.TimeoutException as e:
        logger.error("Upstream request timed out", upstream=serving_upstream, error=str(e))
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"{serving_upstream} upstream request timed out: {e}",
                }
            }
        ), 504

    except Exception as e:
        logger.exception("Upstream proxy error", upstream=serving_upstream)
        return jsonify(
            {
                "error": {
                    "type": "api_error",
                    "message": f"{serving_upstream} upstream proxy error: {e}",
                }
            }
        ), 502

"""LiteLLM custom logger: surface real upstream cost + prompt-cache stats
for egg's non-Claude (OpenRouter) routes into the pod log stream.

Why: Anthropic's ``/v1/messages`` response shape has no ``cost`` field, and
LiteLLM strips the upstream OpenRouter ``usage`` block (carrying real
provider-billed cost and the cache read/write counts) when it translates
the response back to Anthropic format for Claude Code. Without this hook
an operator has no way to see, per session, what a LiteLLM-routed agent
actually spent or whether prompt caching is working — the whole point of
the cq-6 model-diversification pilot (#2799) and the cache patches baked
into the ``egg-litellm`` image.

We hook the proxy's success-logging path, pull the raw upstream ``usage``
out of LiteLLM's ``model_call_details`` (raw provider JSON on the
non-streaming path, the assembled response object's usage on the streaming
path), and emit one structured JSON line per call keyed by Claude Code's
session_id (sent on every request as ``x-claude-code-session-id``). The
line carries the per-call delta plus the running session totals, including
the cache-read hit rate — computed as a session aggregate, not a per-turn
snapshot, because the per-turn ratio is noisy on short turns (a single
tool-result message can dominate the prompt budget).

Cost arrives by one of two routes depending on how the call streamed.
Non-streaming, ``original_response`` carries the raw provider JSON and
``usage.cost`` with it. Streaming — which is essentially all agent traffic,
since Claude Code streams its ``/v1/messages`` requests — LiteLLM
reassembles the chunks via ``stream_chunk_builder`` -> ``ChunkProcessor.
calculate_usage``, a rebuild that enumerates the token/cache counts and
originally DROPPED the provider's ``cost`` / ``cost_details`` outright.
That is why this module recorded ``cost: null`` on 1252 of 1252 sampled
calls in run 6. The egg-litellm image's **patch 11** now carries those two
fields across the rebuild (``config/litellm/stream_cost_preservation.py``),
so the billed figure reaches ``_extract_cost`` on the streaming path too
and this module needs no change to read it — the value simply stops being
absent (#3691).

``cost: null`` therefore no longer means "streaming"; it means the cost was
genuinely unavailable — a stock (unpatched) LiteLLM under this callback, or
a provider that does not report one. It is still emitted as null rather
than ``0.0``: a zero would read in the logs as "this route is free", the
exact opposite of the cost-visibility signal this module exists to provide
(#2799).

Each line also carries ``cost_estimated``: LiteLLM's own ``response_cost``,
computed at logging time from the assembled usage and its pricing map
(issue #3175). It is kept strictly separate from ``cost`` — an estimate
from a possibly-stale rate card must never be mistaken for a bill — and
follows the same null-not-zero rule when LiteLLM cannot price the model.
That was the case for every route egg uses until the image's **patch 12**
taught the model-info lookup to read OpenRouter's published rate card; it
remains the case for a model whose prompt-length surcharge lands on a
boundary or component LiteLLM's map has no slot for, which is declined whole
rather than translated in part (see ``openrouter_capabilities``). With
both patches in place the two fields are independent measurements of the
same turn, and a persistent gap between them is a signal in its own right:
a stale rate card, an unexpected provider, or a surcharge tier.

Each line also carries ``request_params``: the decoding configuration that
actually went upstream on that call (issue #3599). Repetition and
degeneration are decoding-sensitive failure modes, so an incident report
that cannot name the temperature / top_p / penalty configuration in play is
unanalysable after the fact — which is precisely what happened in #3598,
where a producer emitted 480 identical tool calls and nothing recorded
whether that was inherent to the model or an artifact of the sampling
config. See ``_extract_request_params`` for the source and its two
non-obvious properties (absent key == provider default; post-``drop_params``,
so config-vs-wire divergence becomes visible).

Each line additionally carries ``pipeline_id`` / ``agent_role`` / ``phase``
read from the gateway-stamped ``x-egg-*`` request headers (issue #3175),
so per-role spend is a log query instead of a hand cross-reference against
agent completion logs. The fields are None for traffic that didn't come
through an attribution-aware gateway hop.

Unlike the host-side ``cost_callback.py`` this is derived from, egg agents
are headless: there is no statusline to read a per-session JSON file, and
the container runs read-only. So we log to stdout (captured by egg's log
stream / ``get_service_logs``) rather than writing files, and hold the
running totals in memory — safe because the LiteLLM Deployment is a single
replica (k8s/base/litellm-deployment.yaml).

Registered via ``litellm_settings.callbacks: cost_callback.cost_logger`` in
the LiteLLM config (k8s/base/litellm-configmap.yaml). LiteLLM resolves a
config-registered callback as a file next to ``--config``, so the
egg-litellm image bakes this at ``/app/cost_callback.py`` (alongside the
mounted ``/app/config.yaml``) — not via PYTHONPATH.

Hook choice: we implement ``async_log_success_event`` — LiteLLM's core
success-logging hook, which fires for BOTH streaming and non-streaming
completions (``litellm_logging.async_success_handler`` awaits it on stream
completion). Claude Code streams its ``/v1/messages`` requests, so the core
logging path is the only seam that sees real agent traffic. The proxy-layer
``async_post_call_success_hook`` does NOT work here: the ``/v1/messages``
route returns the ``text/event-stream`` response before that proxy hook
runs, so it never fires for streaming — i.e. for essentially all agent
traffic.
"""

import collections
import datetime
import json
import math
import threading

from litellm.integrations.custom_logger import CustomLogger

_lock = threading.Lock()
# session_id -> running totals, kept as an LRU so the map cannot grow without
# bound over the pod's lifetime (one entry per agent session, never otherwise
# evicted). The single replica + tiny per-entry footprint make pressure
# unlikely, but the pod was recently OOMKilled into a 1->2Gi bump, so we cap
# defensively and drop the least-recently-updated session first.
_MAX_SESSIONS = 4096
_session_totals: collections.OrderedDict[str, dict[str, float]] = collections.OrderedDict()


def _raw_upstream_usage(mcd):
    """Pull the upstream OpenRouter ``usage`` block out of
    ``model_call_details['original_response']`` (the raw provider JSON).

    Populated with full provider fidelity (cost, cost_details, cache
    read/write counts) on the non-streaming path. For streaming, LiteLLM logs
    only a type marker into ``original_response`` (see ``Logging.post_call``),
    so this returns None and the caller falls back to the assembled response
    object's usage. Returns the parsed ``usage`` dict, or None if anything's
    missing / malformed."""
    try:
        rr = (mcd or {}).get("original_response")
        if isinstance(rr, str):
            try:
                rr = json.loads(rr.strip())
            except Exception:
                return None
        if not isinstance(rr, dict):
            return None
        usage = rr.get("usage")
        return usage if isinstance(usage, dict) else None
    except Exception:
        return None


def _coerce_usage(usage):
    """Normalize a usage value (LiteLLM ``Usage`` pydantic model, OpenAI usage
    object, or plain dict) to a plain dict, preserving nested token-detail
    objects and provider extras. Returns None if it can't."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    for attr in ("model_dump", "dict"):
        fn = getattr(usage, attr, None)
        if callable(fn):
            try:
                d = fn()
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    return None


def _usage_from_response_obj(response_obj):
    """Read ``usage`` off the assembled response object LiteLLM hands the
    success hook. On the streaming path this is the source for the token/cache
    counts (the final usage chunk's counts are folded into
    ``response_obj.usage`` by ``stream_chunk_builder``) and, on the egg-litellm
    image, for cost as well: that reassembly rebuilds a fresh ``Usage`` and
    stock drops ``cost`` / ``cost_details`` with it, which patch 11 restores.
    Under a stock LiteLLM the counts still arrive and ``_extract_cost`` returns
    None — see the module docstring."""
    if response_obj is None:
        return None
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    return _coerce_usage(usage)


def _finite_number(value):
    """True for a real, finite number. ``bool`` is excluded because
    ``isinstance(True, int)`` is True and a boolean is not a measurement —
    ``float(True)`` would silently record a 1 (one dollar, one token) that was
    never billed or sent."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value):
    """True for a real, finite, positive number."""
    return _finite_number(value) and value > 0


def _extract_cost(usage):
    """Prefer OpenRouter's top-level ``cost`` (what they bill you); under
    BYOK that field is zero because billing routes directly to the upstream
    provider, so fall back to ``cost_details.upstream_inference_cost`` (what
    the upstream provider will bill for the same request). Either way, the
    number we record matches real spend on that turn. Returns None when no
    positive cost is present — a provider that reports none, or a stock LiteLLM
    whose chunk reassembly drops it (see ``_usage_from_response_obj``).
    Callers must treat None as "unknown", not "$0".

    ``_positive`` rejects non-finite values as well as non-positive ones: a
    ``+inf`` cost passes a bare ``> 0`` and would then be accumulated into the
    session total, poisoning it as ``Infinity`` for the pod's lifetime AND
    emitting the non-standard token that makes the line invalid JSON. (``NaN``
    is already excluded — ``nan > 0`` is False.)"""
    u = usage or {}
    cost = u.get("cost")
    if _positive(cost):
        return float(cost)
    details = u.get("cost_details") or {}
    upstream = details.get("upstream_inference_cost")
    if _positive(upstream):
        return float(upstream)
    return None


def _extract_cache_stats(usage):
    """Extract input + cache + reasoning counts in a provider-agnostic way.

    Returns (prompt_tokens, cached_input_tokens, cache_write_tokens,
    reasoning_tokens). Each defaults to 0; anything that is not a real,
    finite, non-negative number is treated as 0.

    That guard is ``_extract_cost``'s, and it matters MORE here than it does
    for cost, because ``_record`` emits these counts through ``int(...)``: an
    ``inf`` raises ``OverflowError`` there, ``_record``'s outer handler
    swallows it, and the whole line — cost data included — is dropped. Worse,
    the value has already landed in ``agg[...]`` by then, so the session total
    stays ``inf`` and EVERY subsequent call in that session emits nothing for
    the pod's lifetime (the LRU only evicts under 4096-session pressure). A
    ``bool`` is milder but wrong in the same direction as a boolean cost: one
    token that was never sent.

    Providers expose cache numbers under several competing schemas — we read
    all and merge:
      - Anthropic-passthrough: ``cache_read_input_tokens`` (hits),
        ``cache_creation_input_tokens`` (writes).
      - OpenAI-style: ``prompt_tokens_details.cached_tokens`` (hits).
      - OpenRouter-specific: ``prompt_tokens_details.cache_write_tokens``
        (writes — present on Qwen/DeepSeek routes; reads via ``.cached_tokens``
        are NOT populated on non-BYOK OpenRouter traffic even when writes
        are, so a write-heavy + read-zero pattern is normal here, not a
        parser bug).

    ``reasoning_tokens`` lives at ``completion_tokens_details.reasoning_tokens``.
    We surface it separately so operators can see the hidden-CoT share of
    output spend — billed like other output tokens, but invisible in the
    transcript."""
    u = usage or {}

    def _num(x):
        return float(x) if _finite_number(x) and x >= 0 else 0.0

    prompt = _num(u.get("prompt_tokens"))
    pdet = u.get("prompt_tokens_details") or {}
    cached = _num(u.get("cache_read_input_tokens")) or _num(pdet.get("cached_tokens"))
    cache_write = _num(u.get("cache_creation_input_tokens")) or _num(pdet.get("cache_write_tokens"))
    cdet = u.get("completion_tokens_details") or {}
    reasoning = _num(cdet.get("reasoning_tokens"))
    return prompt, cached, cache_write, reasoning


def _proxy_request_headers(mcd):
    """Return the proxy request headers from ``model_call_details``, lowercased.

    In ``model_call_details`` the request lands under
    ``litellm_params.proxy_server_request`` (``get_litellm_params`` packs it
    there); check the top level too in case a future path surfaces it there.
    Lowercase header keys defensively — ``clean_headers`` preserves the wire
    casing of header keys, so a non-lowercase hop (gateway, HTTP/1.1 client)
    would otherwise make every header lookup miss silently."""
    try:
        mcd = mcd or {}
        psr = mcd.get("proxy_server_request")
        if not isinstance(psr, dict):
            psr = (mcd.get("litellm_params") or {}).get("proxy_server_request")
        headers = (psr or {}).get("headers") or {}
        return {k.lower(): v for k, v in headers.items() if isinstance(k, str)}
    except Exception:
        return {}


def _extract_session_id(mcd):
    """``x-claude-code-session-id`` is sent by Claude Code on every request.

    A lookup miss would collapse every call to ``_no_session`` and silently
    blend the per-session hit rate into a global one."""
    return _proxy_request_headers(mcd).get("x-claude-code-session-id") or None


# Gateway-stamped attribution headers (issue #3175). The gateway resolves the
# agent's Session on every proxied ``/v1/messages`` call and stamps these on
# non-Anthropic hops (it strips any agent-supplied ``x-egg-*`` first, so the
# values are gateway-authoritative). They map a session's spend to a pipeline
# and role directly in the log line — no hand cross-referencing of agent
# completion logs.
_ATTRIBUTION_HEADERS = (
    ("pipeline_id", "x-egg-pipeline-id"),
    ("agent_role", "x-egg-agent-role"),
    ("phase", "x-egg-phase"),
)


def _extract_attribution(mcd):
    """Return ``{pipeline_id, agent_role, phase}`` from the gateway's
    attribution headers; each value is None when absent (pre-#3175 gateway,
    non-agent probe, or a direct non-gateway client)."""
    headers = _proxy_request_headers(mcd)
    out = {}
    for field, header in _ATTRIBUTION_HEADERS:
        value = headers.get(header)
        out[field] = value if isinstance(value, str) and value else None
    return out


def _extract_estimated_cost(mcd):
    """LiteLLM's own computed cost for the call, as an *estimate*.

    ``response_cost`` is computed by LiteLLM's logging layer from the assembled
    usage and its model pricing map, independently of whether the provider
    reported a bill. It is an estimate, not a bill: the pricing map may lag the
    provider's rates or lack cache-discount entries for a model. Returns None —
    never 0.0 — when LiteLLM couldn't price the call, mirroring the billed-cost
    "unknown ≠ free" discipline. On the egg-litellm image patch 12 supplies
    OpenRouter's published rates for slugs the bundled map does not carry, so a
    None here now means a genuinely unpriceable model (an inexpressible
    prompt-length surcharge, or a provider with no live card to read) rather
    than the routine case it was.

    Reads the top-level ``response_cost`` first, then falls back to
    ``standard_logging_object.response_cost`` — the latter is LiteLLM's
    aggregated/finalized metrics object, preferred for streaming, so the
    fallback hardens against a LiteLLM version where the top-level key is
    absent on the streaming path.

    The fallback is gated on ``_positive``, not on mere presence, so it also
    fires when the top-level key is present but unusable. ``response_cost:
    0.0`` is the realistic case — LiteLLM writes it when it cannot price the
    model, which is exactly the situation this fallback exists for, and a
    presence check would let that zero suppress a perfectly good estimate in
    the metrics object. This keeps the gate symmetric with ``_extract_cost``,
    whose ``cost: 0`` (BYOK) falls through to its own second source.

    Non-finite values are rejected for the same reason as in ``_extract_cost``:
    ``+inf`` clears a bare ``> 0`` and would poison the session total and the
    line's JSON validity together."""
    try:
        mcd = mcd or {}
        rc = mcd.get("response_cost")
        if not _positive(rc):
            slo = mcd.get("standard_logging_object")
            rc = slo.get("response_cost") if isinstance(slo, dict) else None
        if _positive(rc):
            return float(rc)
        return None
    except Exception:
        return None


def _extract_model(mcd):
    try:
        model = (mcd or {}).get("model")
        return model if isinstance(model, str) else None
    except Exception:
        return None


# Decoding-relevant request parameters to record per call (#3599).
#
# An ALLOWLIST, not a dump of ``optional_params``: that dict also carries the
# translated ``tools`` schemas (Claude Code sends a dozen-plus per request)
# and other prompt-adjacent payloads. Emitting it whole would bloat every
# line by orders of magnitude and spill task text into a stream that is a
# cost/observability sink, not a transcript sink.
#
# ``stream`` is included because it selects which of the two paths the cost on
# this line came through (see the module docstring), and it was the reason
# ``cost`` read null on every line before patch 11 — worth having next to the
# number rather than inferred, and worth keeping now that the null case is rare
# enough to need explaining when it happens. ``max_tokens`` and ``n`` are not
# sampling knobs but shape the generation, and are cheap to carry.
_REQUEST_PARAM_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "typical_p",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "logit_bias",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "stop_sequences",
    "n",
    "reasoning_effort",
    "reasoning",
    "thinking",
    "stream",
)

# Per-value size cap. Everything on the allowlist is normally a scalar or a
# small object, but ``logit_bias`` / ``stop`` are client-supplied and
# unbounded; one pathological request must not be able to blow up every log
# line for the rest of the session.
_MAX_PARAM_JSON_CHARS = 512

# Value cap for the priority keys below. Larger than the general cap because
# "this entry is load-bearing enough to reorder the dict for" and "this entry
# gets the same size budget as a junk sibling" are in tension: a provider pin
# carrying an `ignore` list of a few dozen backends clears 512 chars easily,
# and collapsing it to a size marker loses precisely the backend identity the
# field exists to record. Still a cap — one entry cannot run away either.
_MAX_PRIORITY_PARAM_JSON_CHARS = 2048

# Key-count cap for the extra_body remainder. Its values are bounded one by one
# (so a bulky sibling can't collapse the small, load-bearing provider pin), which
# leaves the number of keys as the remaining unbounded dimension. Sized against
# the whole-block budget below rather than picked as a round number: 16 keys at
# their name and value caps (one of them a priority key at its larger cap) emit
# 11,289 chars, measured, which leaves the aggregate guard nothing to undo in
# the case this cap already covers. A real extra_body is one or two entries.
_MAX_EXTRA_BODY_KEYS = 16

# Key-NAME cap for the extra_body remainder. Deliberately far tighter than the
# value cap: a key name contributes to the emitted line exactly as a value does,
# so reusing _MAX_PARAM_JSON_CHARS here would double the aggregate bound — and
# no real extra_body key is anywhere near 64 characters, so the extra headroom
# would buy no diagnostic value for that cost.
_MAX_EXTRA_BODY_KEY_CHARS = 64

# extra_body keys hoisted ahead of the key-count cap, and given the larger
# value cap above. The OpenRouter provider pin decides WHICH backend (and so
# which quantization) served the turn, which makes it the single most
# load-bearing entry in the remainder; ordering it first keeps the count cap
# from being positional, so the pin survives no matter where an operator's
# config happens to place it, and the wider size budget keeps it legible when
# it is a real pin rather than the two-line example.
_EXTRA_BODY_PRIORITY_KEYS = ("provider",)

# Sentinel for the truncation marker. Namespaced so it cannot collide with (and
# silently overwrite) a real operator-supplied ``extra_body`` key.
_EXTRA_BODY_TRUNCATED_KEY = "<egg:truncated>"

# Whole-block budget for the emitted request_params. The caps above bound each
# dimension separately but compose multiplicatively — 19 allowlisted keys at the
# 512-char value cap alongside a maximal extra_body clears 20KB with every
# individual cap intact — so the aggregate needs its own ceiling.
#
# 16KiB is the number that matters: Docker's json-file driver and containerd's
# CRI logger both split a stdout line at that size into `P`-marked partials, and
# neither fragment is valid JSON. The documented ``jq -Rc 'fromjson?'`` query
# then drops the line, cost data and all — the same outcome ``allow_nan=False``
# exists to prevent, reached by size instead of by token. 12KiB leaves headroom
# for the cost/attribution/session fields that share the line (~800 bytes on a
# stock line, and themselves bounded only by the model and header names).
_MAX_REQUEST_PARAMS_CHARS = 12288


def _scrub_non_finite(value, _depth=0):
    """Replace non-finite floats *inside* a container with their marker.

    Only ever called on the recovery path in ``_bounded_param``, after a strict
    encode has already told us there is a ``NaN``/``Inf`` in there somewhere.
    Scrubbing rather than rejecting keeps the finite siblings: a 100-entry
    ``logit_bias`` with one ``-inf`` is still 99 entries of usable evidence, and
    the marker preserves the "an operator set an insane value" reading that a
    flat ``<unserializable>`` would conflate with "we couldn't encode this".
    Depth-capped so a self-referential structure can't run away here (the caller
    still degrades it — the retry encode raises on whatever we left behind)."""
    if _depth > 8:
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return f"<non-finite: {value}>"
    if isinstance(value, dict):
        return {k: _scrub_non_finite(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_non_finite(v, _depth + 1) for v in value]
    return value


def _bounded_param(value, max_chars=_MAX_PARAM_JSON_CHARS):
    """Return ``value`` clamped to something safe to embed in the log line.

    Three hazards, all of which would cost us the WHOLE line rather than just
    this field (``_emit`` swallows a serialization failure): non-finite floats,
    non-JSON-serializable values, and unbounded ones. ``NaN``/``Inf`` are the
    subtlest — ``json.dumps`` emits the non-standard ``NaN``/``Infinity`` tokens
    (invalid JSON), so a downstream ``jq 'fromjson?'`` silently drops the line,
    cost data and all; a misconfigured ``temperature``/``top_p`` is enough to
    trigger it, so we map a non-finite scalar to a marker. Nested ones need
    their own handling, and are the MORE reachable half: ``{"5": -inf}`` as a
    ``logit_bias`` is a real idiom for banning a token. ``allow_nan=False``
    makes the round-trip below raise on them rather than emit an invalid token,
    and the retry scrubs them to the same marker in place — so one bad entry
    costs that entry, not the field and not the line. Otherwise scalars pass
    through; everything else is round-tripped through JSON — with
    ``default=str`` so an exotic value degrades to its repr instead of raising —
    and replaced by a size marker when it exceeds ``max_chars``, which callers
    raise for the entries they consider load-bearing."""
    if isinstance(value, float) and not math.isfinite(value):
        return f"<non-finite: {value}>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_chars else f"<{len(value)} chars omitted>"
    try:
        encoded = json.dumps(value, default=str, allow_nan=False)
    except ValueError:
        # Strictly a non-finite float somewhere below the top level — the only
        # ValueError allow_nan=False adds. Scrub those in place and retry;
        # anything still unencodable falls through to the marker.
        try:
            encoded = json.dumps(_scrub_non_finite(value), default=str, allow_nan=False)
        except Exception:
            return "<unserializable>"
    except Exception:
        return "<unserializable>"
    if len(encoded) > max_chars:
        return f"<{len(encoded)} chars omitted>"
    try:
        return json.loads(encoded)
    except Exception:
        return "<unserializable>"


def _bounded_extra_body_key(key):
    """Bound one ``extra_body`` key name for emission.

    Oversized names keep a prefix rather than collapsing to a bare size marker:
    two 600-char keys would otherwise produce the identical marker and one
    would silently overwrite the other, turning a size problem into a data-loss
    problem. The prefix also keeps the name diagnostic, which is the whole
    point of recording the key at all."""
    key = str(key)
    if len(key) <= _MAX_EXTRA_BODY_KEY_CHARS:
        return key
    return f"{key[:_MAX_EXTRA_BODY_KEY_CHARS]}…<{len(key)} chars omitted>"


def _uncollided_key(bounded, emitted_key):
    """Return ``emitted_key``, suffixed if it is already taken.

    Distinct source keys can still normalize to one name (``1`` and ``"1"``, or
    two long keys sharing a prefix AND a length), and the truncation marker can
    land on a key an operator really named that. Suffix rather than overwrite —
    a lost value reads as a key that was never sent, which is exactly the
    inference this field invites."""
    if emitted_key not in bounded:
        return emitted_key
    suffix = 2
    while f"{emitted_key}<{suffix}>" in bounded:
        suffix += 1
    return f"{emitted_key}<{suffix}>"


def _bounded_extra_body(leftover):
    """Bound the ``extra_body`` remainder along every dimension that can grow.

    ``extra_body`` is config-supplied and typically pinned in ``litellm_params``,
    so an unbounded remainder is not one bad line — it is EVERY line for the
    life of the config. Three dimensions, each capped:

    - **Value size** — bounded per value rather than over the dict as a whole,
      so a bulky sibling knob degrades on its own instead of collapsing the
      small, load-bearing provider pin along with it. Priority keys get the
      wider ``_MAX_PRIORITY_PARAM_JSON_CHARS`` budget, so a real provider pin
      (an ``ignore`` list of a few dozen backends clears 512 chars) stays
      legible rather than degrading to the size marker.
    - **Key count** — capped, with the priority keys hoisted first so the cap
      is not positional (a config with the pin at index 40 still records it).
    - **Key name** — capped, and stringified: ``json.dumps``' ``default=`` hook
      applies to values only, so a non-``str`` key would raise out in ``_emit``,
      where the failure is swallowed and the whole line, cost data included, is
      dropped.

    Every emitted key — the truncation marker included — goes through
    ``_uncollided_key``, so the ``len(...) == _MAX_EXTRA_BODY_KEYS + 1``
    invariant on a truncated remainder holds for any input rather than only for
    inputs that happen not to collide with the sentinel."""
    ordered = [(k, v) for k, v in leftover.items() if k in _EXTRA_BODY_PRIORITY_KEYS]
    ordered += [(k, v) for k, v in leftover.items() if k not in _EXTRA_BODY_PRIORITY_KEYS]
    bounded = {}
    for key, value in ordered[:_MAX_EXTRA_BODY_KEYS]:
        cap = (
            _MAX_PRIORITY_PARAM_JSON_CHARS
            if key in _EXTRA_BODY_PRIORITY_KEYS
            else _MAX_PARAM_JSON_CHARS
        )
        emitted_key = _uncollided_key(bounded, _bounded_extra_body_key(key))
        bounded[emitted_key] = _bounded_param(value, cap)
    if len(ordered) > _MAX_EXTRA_BODY_KEYS:
        omitted = len(ordered) - _MAX_EXTRA_BODY_KEYS
        marker_key = _uncollided_key(bounded, _EXTRA_BODY_TRUNCATED_KEY)
        bounded[marker_key] = f"<{omitted} more keys omitted>"
    return bounded


def _fit_request_params(out):
    """Clamp the assembled block to ``_MAX_REQUEST_PARAMS_CHARS``, returning it.

    The per-dimension caps each hold on their own and still compose into a line
    big enough to be split by the container runtime (see the constant), so the
    aggregate gets a ceiling of its own. Every value here has already been
    through ``_bounded_param``, so re-encoding one cannot raise.

    Two ordering choices, both about what a truncated line should still be able
    to answer:

    - **Largest entry first.** The sampling scalars this field exists to record
      are a handful of bytes each; whatever pushed the block over is not one of
      them. They are the last to go, not the first.
    - **``extra_body`` collapses to its priority keys before it collapses
      entirely.** The provider pin is both the most load-bearing entry in the
      remainder and one of the smallest, so there is no reason for it to share
      the fate of the bulk it was sitting next to."""
    if len(json.dumps(out)) <= _MAX_REQUEST_PARAMS_CHARS:
        return out
    for key in sorted(out, key=lambda k: len(json.dumps(out[k])), reverse=True):
        value = out[key]
        if key == "extra_body" and isinstance(value, dict):
            pinned = {k: v for k, v in value.items() if k in _EXTRA_BODY_PRIORITY_KEYS}
            if pinned and len(json.dumps(pinned)) < len(json.dumps(value)):
                omitted = len(value) - len(pinned)
                pinned[_uncollided_key(pinned, _EXTRA_BODY_TRUNCATED_KEY)] = (
                    f"<{omitted} more keys omitted>"
                )
                out[key] = pinned
                if len(json.dumps(out)) <= _MAX_REQUEST_PARAMS_CHARS:
                    break
                value = pinned
        out[key] = f"<{len(json.dumps(value))} chars omitted>"
        if len(json.dumps(out)) <= _MAX_REQUEST_PARAMS_CHARS:
            break
    return out


def _extract_request_params(mcd):
    """Return the decoding configuration this call actually ran under (#3599).

    Source is ``model_call_details['optional_params']``: LiteLLM's
    POST-mapping parameter set, i.e. the dict that becomes the upstream
    request body. Verified against the pinned litellm 1.86.2 on the
    ``anthropic_messages`` route (the route every Claude Code agent request
    takes), streaming and non-streaming — the streaming case matters most,
    since that is essentially all real agent traffic.

    Two properties make this worth having, and both are the difference
    between a line that answers the question and one that misleads:

    - **An absent key was not sent**, so the provider's own server-side
      default applied. That is the answer today for ``temperature`` /
      ``top_p`` / ``top_k`` on every egg route: egg pins none of them, so the
      effective config is the provider's, and it can change under us with no
      egg change. Absence is the signal, so we omit missing keys rather than
      writing nulls that would read as "explicitly unset".
    - **``drop_params`` has already acted**, so a knob an operator set in
      ``litellm_params`` that does NOT appear here was silently discarded (or
      relocated into ``extra_body``) by the mapping layer. Today that
      divergence between what the config says and what the wire carried is
      invisible; here it is a diff.

    We deliberately do NOT read ``standard_logging_object.model_parameters``:
    it filters to OpenAI's declared parameter set, which drops ``top_k``,
    ``stop_sequences`` and ``extra_body`` — silently under-reporting several
    of the anti-repetition knobs this exists to capture.

    Returns a dict holding only the keys that were present (possibly empty:
    "LiteLLM reported the params, none were decoding-relevant"), or None when
    ``optional_params`` is missing or not a dict ("we could not tell") — the
    same unknown-is-not-zero discipline the cost fields follow."""
    try:
        params = (mcd or {}).get("optional_params")
        if not isinstance(params, dict):
            return None
        out = {}
        for key in _REQUEST_PARAM_KEYS:
            if key in params:
                out[key] = _bounded_param(params[key])
        extra = params.get("extra_body")
        if isinstance(extra, dict):
            leftover = {}
            for key, value in extra.items():
                # LiteLLM's param mapper relocates knobs a provider doesn't
                # declare into extra_body rather than dropping them (e.g.
                # top_k on an OpenAI-shaped route), so the same knob lands at
                # different depths depending on model. Hoist allowlisted keys
                # to the flat level so one query finds them wherever LiteLLM
                # put them; keep the rest under extra_body — notably the
                # OpenRouter provider pin, which decides WHICH backend (and
                # so which quantization) served the turn.
                if key in _REQUEST_PARAM_KEYS and key not in out:
                    out[key] = _bounded_param(value)
                else:
                    leftover[key] = value
            if leftover:
                # Size, key count and key names are all bounded there — see
                # _bounded_extra_body for why each dimension needs its own cap.
                out["extra_body"] = _bounded_extra_body(leftover)
        return _fit_request_params(out)
    except Exception:
        return None


def _emit(payload):
    """One structured JSON line to stdout — captured by egg's log stream.
    Mirrors egg_logging's field shape loosely (timestamp/severity/service/
    component/context); egg_logging itself isn't importable in the stock
    LiteLLM container."""
    line = {
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "severity": "INFO",
        "service": "litellm",
        "component": "cost_callback",
        "message": "litellm upstream cost + cache stats",
        "context": payload,
    }
    try:
        print(json.dumps(line), flush=True)
    except Exception:
        pass


class LiteLLMCostLogger(CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        # ``kwargs`` IS LiteLLM's ``model_call_details`` dict here, and
        # ``response_obj`` the assembled response. This hook fires on the core
        # success path for both streaming and non-streaming completions, so it
        # covers Claude Code's streamed /v1/messages traffic (see module
        # docstring for why the proxy-layer success hook does not).
        self._record(kwargs, response_obj)

    def _record(self, mcd, response_obj):
        try:
            if not isinstance(mcd, dict):
                return
            # Raw upstream JSON first (full provider fidelity: cost,
            # cost_details, cache_write counts) — the non-streaming path. For
            # streaming, original_response holds only a type marker, so fall
            # back to the assembled response object's usage.
            usage = _raw_upstream_usage(mcd) or _usage_from_response_obj(response_obj)
            cost = _extract_cost(usage)
            cost_estimated = _extract_estimated_cost(mcd)
            prompt, cached, cache_write, reasoning = _extract_cache_stats(usage)
            if cost is None and prompt == 0 and cached == 0:
                return
            # ``cost`` stays None when the provider reported no cost, or when
            # a stock LiteLLM discarded it in reassembly (see module
            # docstring). We accumulate only known costs and count how many
            # calls contributed one, so a session that never saw a real cost
            # reports ``cost: null`` rather than a misleading ``0.0``.
            # ``cost_estimated`` (LiteLLM's own pricing-map figure) follows the
            # same discipline under its own counters — the two counters are
            # what make a partially-known session readable, since either field
            # can be the one that is missing.
            sid = _extract_session_id(mcd) or "_no_session"
            model = _extract_model(mcd)
            attribution = _extract_attribution(mcd)
            request_params = _extract_request_params(mcd)
            with _lock:
                agg = _session_totals.get(sid)
                if agg is None:
                    agg = {
                        "cost": 0.0,
                        "cost_known_calls": 0.0,
                        "cost_estimated": 0.0,
                        "cost_estimated_known_calls": 0.0,
                        "calls": 0.0,
                        "prompt_tokens": 0.0,
                        "cached_tokens": 0.0,
                        "cache_write_tokens": 0.0,
                        "reasoning_tokens": 0.0,
                    }
                if cost is not None:
                    agg["cost"] += cost
                    agg["cost_known_calls"] += 1
                if cost_estimated is not None:
                    agg["cost_estimated"] += cost_estimated
                    agg["cost_estimated_known_calls"] += 1
                agg["calls"] += 1
                agg["prompt_tokens"] += prompt
                agg["cached_tokens"] += cached
                agg["cache_write_tokens"] += cache_write
                agg["reasoning_tokens"] += reasoning
                _session_totals[sid] = agg
                _session_totals.move_to_end(sid)
                while len(_session_totals) > _MAX_SESSIONS:
                    _session_totals.popitem(last=False)
                totals = dict(agg)
            hit_rate = None
            if totals["prompt_tokens"] > 0:
                # cached_tokens is the cache-read count; prompt_tokens is the
                # OpenAI-style total (cached included), so this is a true hit
                # rate in [0, 100] on the normal OpenRouter path. Clamp the top
                # defensively: if a provider ever reports cache reads under a
                # schema where prompt_tokens excludes cached, the raw ratio
                # could exceed 100 and read as a parser bug in the logs.
                hit_rate = round(
                    min(totals["cached_tokens"] * 100 / totals["prompt_tokens"], 100.0),
                    2,
                )
            # Report session cost as null until at least one call carried a
            # known cost, so a session that never learned one doesn't read as
            # "$0 spent". Note the session total is a sum over the calls that
            # DID report — read it against ``cost_known_calls``/``calls``, not
            # as the session's whole bill, whenever those two differ.
            # Counts (calls and token tallies) are integer-valued — emit them
            # as ``int`` so the log line reads ``cost_known_calls: 1`` rather
            # than ``1.0`` (the aggregate is held as float for uniform +=).
            # ``cost`` and ``cache_hit_rate_pct`` stay float/None.
            session = {
                "cost": totals["cost"] if totals["cost_known_calls"] > 0 else None,
                "cost_known_calls": int(totals["cost_known_calls"]),
                "cost_estimated": (
                    totals["cost_estimated"] if totals["cost_estimated_known_calls"] > 0 else None
                ),
                "cost_estimated_known_calls": int(totals["cost_estimated_known_calls"]),
                "calls": int(totals["calls"]),
                "prompt_tokens": int(totals["prompt_tokens"]),
                "cached_tokens": int(totals["cached_tokens"]),
                "cache_write_tokens": int(totals["cache_write_tokens"]),
                "reasoning_tokens": int(totals["reasoning_tokens"]),
            }
            _emit(
                {
                    "session_id": sid,
                    "model": model,
                    # Gateway-stamped attribution (#3175); None values mean the
                    # request did not come through an attribution-aware gateway
                    # hop. Session-stable, but emitted per line so every log
                    # line is independently queryable by role/pipeline.
                    **attribution,
                    # The decoding config this call actually ran under
                    # (#3599). Top-level, not nested under ``call``, so an
                    # incident query can filter on it the same way it filters
                    # on model/role. Per line rather than once per session
                    # because it is NOT session-stable: these are per-request
                    # values, and a config change or an overlay edit takes
                    # effect mid-session.
                    #
                    # Note ``reasoning_effort`` is normally ABSENT on egg's
                    # /v1/messages route (#3624): stock LiteLLM rewrites the
                    # caller's ``thinking`` block into a bucketed
                    # ``reasoning_effort``, but that bucket is a cap below the
                    # model default, so egg-litellm's patch 9 gates the
                    # synthesis off by default. A missing key here means the
                    # request ran at the model's own reasoning depth, not that
                    # the field failed to record.
                    "request_params": request_params,
                    "call": {
                        "cost": cost,
                        "cost_estimated": cost_estimated,
                        "prompt_tokens": int(prompt),
                        "cached_tokens": int(cached),
                        "cache_write_tokens": int(cache_write),
                        "reasoning_tokens": int(reasoning),
                    },
                    "session": session,
                    "cache_hit_rate_pct": hit_rate,
                }
            )
        except Exception:
            pass


cost_logger = LiteLLMCostLogger()

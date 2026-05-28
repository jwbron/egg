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

Cost on the streaming path is intentionally reported as ``null``, not 0.
Claude Code streams its ``/v1/messages`` requests, and LiteLLM reassembles
the streamed chunks via ``stream_chunk_builder`` -> ``ChunkProcessor.
calculate_usage``, which rebuilds a fresh ``Usage`` carrying only the
token/cache counts and DROPS the upstream provider's ``cost`` /
``cost_details``. So on real agent traffic the upstream-billed cost is not
recoverable at this seam, and we emit ``cost: null`` (per call and in the
session totals) rather than coercing the missing value to ``0.0`` — a
``0.0`` would read in the logs as "this route is free", the exact opposite
of the cost-visibility signal this module exists to provide (#2799). The
cache-read/write and token counts DO survive reassembly, so the
cache-hit-rate metric (the primary cq-6 signal) is unaffected. Real cost is
still captured on the non-streaming path, where ``original_response``
carries the raw provider JSON with ``usage.cost``.

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
    success hook. On the streaming path this is the reliable source for the
    token/cache counts (the final usage chunk's counts are folded into
    ``response_obj.usage`` by ``stream_chunk_builder``), but NOT for cost:
    that reassembly rebuilds a fresh ``Usage`` and drops ``cost`` /
    ``cost_details``, so ``_extract_cost`` returns None here on streaming."""
    if response_obj is None:
        return None
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    return _coerce_usage(usage)


def _extract_cost(usage):
    """Prefer OpenRouter's top-level ``cost`` (what they bill you); under
    BYOK that field is zero because billing routes directly to the upstream
    provider, so fall back to ``cost_details.upstream_inference_cost`` (what
    the upstream provider will bill for the same request). Either way, the
    number we record matches real spend on that turn. Returns None when no
    positive cost is present — notably on the streaming path, where LiteLLM's
    chunk reassembly drops the upstream cost (see ``_usage_from_response_obj``).
    Callers must treat None as "unknown", not "$0"."""
    u = usage or {}
    cost = u.get("cost")
    if isinstance(cost, (int, float)) and cost > 0:
        return float(cost)
    details = u.get("cost_details") or {}
    upstream = details.get("upstream_inference_cost")
    if isinstance(upstream, (int, float)) and upstream > 0:
        return float(upstream)
    return None


def _extract_cache_stats(usage):
    """Extract input + cache + reasoning counts in a provider-agnostic way.

    Returns (prompt_tokens, cached_input_tokens, cache_write_tokens,
    reasoning_tokens). Each defaults to 0; non-numeric values are treated
    as 0.

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
        return float(x) if isinstance(x, (int, float)) else 0.0

    prompt = _num(u.get("prompt_tokens"))
    pdet = u.get("prompt_tokens_details") or {}
    cached = _num(u.get("cache_read_input_tokens")) or _num(pdet.get("cached_tokens"))
    cache_write = _num(u.get("cache_creation_input_tokens")) or _num(pdet.get("cache_write_tokens"))
    cdet = u.get("completion_tokens_details") or {}
    reasoning = _num(cdet.get("reasoning_tokens"))
    return prompt, cached, cache_write, reasoning


def _extract_session_id(mcd):
    """``x-claude-code-session-id`` is sent by Claude Code on every request.

    In ``model_call_details`` it lands under
    ``litellm_params.proxy_server_request`` (``get_litellm_params`` packs it
    there); check the top level too in case a future path surfaces it there.
    Lowercase header keys defensively — ``clean_headers`` preserves the wire
    casing of header keys, so a non-lowercase hop (gateway, HTTP/1.1 client)
    would otherwise collapse every call to ``_no_session`` and silently blend
    the per-session hit rate into a global one."""
    try:
        mcd = mcd or {}
        psr = mcd.get("proxy_server_request")
        if not isinstance(psr, dict):
            psr = (mcd.get("litellm_params") or {}).get("proxy_server_request")
        headers = (psr or {}).get("headers") or {}
        headers = {k.lower(): v for k, v in headers.items() if isinstance(k, str)}
        return headers.get("x-claude-code-session-id") or None
    except Exception:
        return None


def _extract_model(mcd):
    try:
        model = (mcd or {}).get("model")
        return model if isinstance(model, str) else None
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
            prompt, cached, cache_write, reasoning = _extract_cache_stats(usage)
            if cost is None and prompt == 0 and cached == 0:
                return
            # ``cost`` stays None when the upstream cost is unrecoverable
            # (the streaming path — see module docstring). We accumulate only
            # known costs and count how many calls contributed one, so a
            # session that never saw a real cost reports ``cost: null`` rather
            # than a misleading ``0.0``.
            sid = _extract_session_id(mcd) or "_no_session"
            model = _extract_model(mcd)
            with _lock:
                agg = _session_totals.get(sid)
                if agg is None:
                    agg = {
                        "cost": 0.0,
                        "cost_known_calls": 0.0,
                        "calls": 0.0,
                        "prompt_tokens": 0.0,
                        "cached_tokens": 0.0,
                        "cache_write_tokens": 0.0,
                        "reasoning_tokens": 0.0,
                    }
                if cost is not None:
                    agg["cost"] += cost
                    agg["cost_known_calls"] += 1
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
            # known cost, so all-streaming sessions don't read as "$0 spent".
            session = {
                "cost": totals["cost"] if totals["cost_known_calls"] > 0 else None,
                "cost_known_calls": totals["cost_known_calls"],
                "calls": totals["calls"],
                "prompt_tokens": totals["prompt_tokens"],
                "cached_tokens": totals["cached_tokens"],
                "cache_write_tokens": totals["cache_write_tokens"],
                "reasoning_tokens": totals["reasoning_tokens"],
            }
            _emit(
                {
                    "session_id": sid,
                    "model": model,
                    "call": {
                        "cost": cost,
                        "prompt_tokens": prompt,
                        "cached_tokens": cached,
                        "cache_write_tokens": cache_write,
                        "reasoning_tokens": reasoning,
                    },
                    "session": session,
                    "cache_hit_rate_pct": hit_rate,
                }
            )
        except Exception:
            pass


cost_logger = LiteLLMCostLogger()

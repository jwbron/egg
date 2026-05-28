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

We hook the proxy's post-call success path, pull the raw upstream JSON out
of ``litellm_logging_obj.model_call_details.original_response``, and emit
one structured JSON line per call keyed by Claude Code's session_id (sent
on every request as ``x-claude-code-session-id``). The line carries the
per-call delta plus the running session totals, including the cache-read
hit rate — computed as a session aggregate, not a per-turn snapshot,
because the per-turn ratio is noisy on short turns (a single tool-result
message can dominate the prompt budget).

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
mounted ``/app/config.yaml``) — not via PYTHONPATH. The standard
``log_success_event`` hooks don't fire for ``/v1/messages`` — that's why
``async_post_call_success_hook`` is the right seam.
"""

import datetime
import json
import threading

from litellm.integrations.custom_logger import CustomLogger

_lock = threading.Lock()
# session_id -> running totals. Bounded in practice by the number of agent
# sessions the single-replica pod serves over its lifetime; entries are a
# handful of floats each.
_session_totals: dict[str, dict[str, float]] = {}


def _raw_upstream_usage(data):
    """Pull the upstream OpenRouter ``usage`` block out of
    ``data['litellm_logging_obj'].model_call_details['original_response']``.

    Returns the parsed ``usage`` dict, or None if anything's missing /
    malformed. All cost + cache stats below derive from this one source so
    any provider-format quirks land in one place."""
    try:
        lo = data.get("litellm_logging_obj")
        if lo is None:
            return None
        mcd = getattr(lo, "model_call_details", None) or {}
        rr = mcd.get("original_response")
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


def _extract_cost(usage):
    """Prefer OpenRouter's top-level ``cost`` (what they bill you); under
    BYOK that field is zero because billing routes directly to the upstream
    provider, so fall back to ``cost_details.upstream_inference_cost`` (what
    the upstream provider will bill for the same request). Either way, the
    number we record matches real spend on that turn."""
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


def _extract_session_id(data):
    """``x-claude-code-session-id`` lives at data.proxy_server_request.headers
    — Claude Code sends it on every request."""
    try:
        headers = ((data.get("proxy_server_request") or {}).get("headers")) or {}
        sid = headers.get("x-claude-code-session-id")
        return sid or None
    except Exception:
        return None


def _extract_model(data):
    try:
        model = data.get("model")
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
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        try:
            if not isinstance(data, dict):
                return response
            usage = _raw_upstream_usage(data)
            cost = _extract_cost(usage)
            prompt, cached, cache_write, reasoning = _extract_cache_stats(usage)
            if cost is None and prompt == 0 and cached == 0:
                return response
            cost = cost or 0.0
            sid = _extract_session_id(data) or "_no_session"
            model = _extract_model(data)
            with _lock:
                agg = _session_totals.setdefault(
                    sid,
                    {
                        "cost": 0.0,
                        "calls": 0.0,
                        "prompt_tokens": 0.0,
                        "cached_tokens": 0.0,
                        "cache_write_tokens": 0.0,
                        "reasoning_tokens": 0.0,
                    },
                )
                agg["cost"] += cost
                agg["calls"] += 1
                agg["prompt_tokens"] += prompt
                agg["cached_tokens"] += cached
                agg["cache_write_tokens"] += cache_write
                agg["reasoning_tokens"] += reasoning
                totals = dict(agg)
            hit_rate = (
                round(totals["cached_tokens"] * 100 / totals["prompt_tokens"], 2)
                if totals["prompt_tokens"] > 0
                else None
            )
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
                    "session": totals,
                    "cache_hit_rate_pct": hit_rate,
                }
            )
        except Exception:
            pass
        return response


cost_logger = LiteLLMCostLogger()

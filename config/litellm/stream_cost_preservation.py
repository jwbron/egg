"""Carry the provider's billed cost through LiteLLM's stream reassembly.

LiteLLM rebuilds a single ``Usage`` from the streamed chunks in
``ChunkProcessor.calculate_usage``. That rebuild is field-by-field over the
counts it knows about — prompt/completion tokens, the Anthropic cache
read/write pair, the token-detail wrappers — and it ends by re-constructing
the object from its own ``model_dump()``. Anything the provider attached that
LiteLLM did not enumerate is gone at that seam.

For OpenRouter that "anything" is the bill. ``usage.cost`` is what OpenRouter
charges for the turn, and ``cost_details.upstream_inference_cost`` is what the
upstream provider charges under BYOK (where the former is 0 because billing
routes past OpenRouter). Both arrive on the final streamed chunk: stock
``OpenrouterConfig.transform_request`` already sets ``usage: {"include": true}``
on every request, and ``OpenRouterChatCompletionStreamingHandler.chunk_parser``
hands the raw block to ``ModelResponseStream``, whose ``Usage`` constructor
keeps ``cost`` as a declared field and ``cost_details`` as a pydantic extra. So
the number is present, correct, and one function call from the logger that
wants it — and then dropped.

The consequence, measured on egg's run 6 (#3691): 1252 of 1252 sampled
``cost_callback`` lines carried ``cost: null``. Claude Code streams every
``/v1/messages`` request, so this seam is on ~100% of agent traffic, and egg
had no dollar figure at all for its LLM spend. The non-streaming path was
unaffected — ``original_response`` there carries the raw provider JSON — which
is why the module read as "cost is unavailable on this route" rather than as a
transport bug.

This module is the transport half of the fix and nothing more. It copies the
two fields verbatim onto the reassembled usage and leaves every judgement to
the reader:

* **A zero ``cost`` is copied, not skipped.** Under BYOK that zero is the
  literal truth about the OpenRouter bill, and the real number is next to it
  under ``cost_details``. ``cost_callback._extract_cost`` is the component that
  knows to fall through from one to the other; a "positive only" filter here
  would delete the evidence that the fall-through is the right reading.
* **Non-finite values are refused.** ``NaN``/``Inf`` on a cost field is not a
  measurement, and downstream it is worse than absent: it accumulates into
  egg's per-session total and poisons it for the pod's lifetime, and
  ``json.dumps`` renders it as a non-standard token that makes the whole log
  line invalid JSON. Same guard, same reason, as ``_finite_number`` there.
* **An existing value is never overwritten.** If a future LiteLLM starts
  carrying cost through reassembly itself, its answer wins and this becomes a
  no-op rather than a silent second opinion.

Fails soft in the strictest sense: every path is wrapped, and any error leaves
the reassembled usage exactly as LiteLLM built it. A cost figure is
observability; it must never be able to break a response.

Installed into every litellm tree as
``litellm_core_utils/_egg_stream_cost.py`` by
``config/litellm/patch_litellm_cache.py`` (Patch 10 is the call site). Kept as
a real file rather than a string literal in that script so it stays lintable
and unit-testable in the egg repo — which is why it imports no litellm symbols
at all and works structurally, off ``getattr``/``dict`` access.
"""

import math

# The provider-billed fields we carry across reassembly. ``cost`` is a declared
# field on litellm's ``Usage``; ``cost_details`` survives only as a pydantic
# extra, which is precisely why neither is enumerated by ``calculate_usage``.
COST_FIELD = "cost"
COST_DETAILS_FIELD = "cost_details"


def _usage_of(chunk):
    """The usage block on one streamed chunk, or None.

    Mirrors the two sources ``ChunkProcessor._calculate_usage_per_chunk``
    reads, in the same order: the chunk's own ``usage`` (``ModelResponseStream``
    defines ``__contains__``/``__getitem__``, so the mapping-style access works
    on the pydantic object as well as on a plain dict) and, failing that, the
    ``usage`` stashed in ``_hidden_params``. Reading a different set of chunks
    from the function whose output we are amending would make the cost and the
    token counts capable of describing different turns.
    """
    try:
        if "usage" in chunk:
            usage = chunk["usage"]
            if usage is not None:
                return usage
    except Exception:  # noqa: BLE001 - a chunk shape we don't understand is not an error
        pass
    try:
        hidden = getattr(chunk, "_hidden_params", None)
        if isinstance(hidden, dict):
            return hidden.get("usage")
    except Exception:  # noqa: BLE001
        pass
    return None


def _field_of(usage, name):
    """Read ``name`` off a usage block that may be a dict or a pydantic model."""
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def _finite_number(value):
    """True for a real, finite number.

    ``bool`` is excluded because ``isinstance(True, int)`` is True and a
    boolean is not a measurement: ``float(True)`` would record one dollar that
    was never billed.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _extract(chunks):
    """Return ``(cost, cost_details)`` from the streamed chunks.

    Each is None when no chunk carried a usable value. The LAST usable value
    wins: a provider that revises its usage block mid-stream is stating a
    correction, and OpenRouter's single usage chunk makes the choice moot in
    the case that actually runs.
    """
    cost = None
    cost_details = None
    for chunk in chunks or []:
        usage = _usage_of(chunk)
        if usage is None:
            continue
        candidate = _field_of(usage, COST_FIELD)
        if _finite_number(candidate):
            cost = float(candidate)
        candidate = _field_of(usage, COST_DETAILS_FIELD)
        if isinstance(candidate, dict) and candidate:
            cost_details = candidate
    return cost, cost_details


def carry_upstream_cost(chunks, usage):
    """Copy the provider-billed cost from ``chunks`` onto the rebuilt ``usage``.

    Returns ``usage`` so the call site can stay a single expression. Never
    raises: the caller is on the response path, and every field this touches is
    observability rather than payload.
    """
    try:
        cost, cost_details = _extract(chunks)
        if cost is not None and not _finite_number(_field_of(usage, COST_FIELD)):
            setattr(usage, COST_FIELD, cost)
        if cost_details is not None and not isinstance(_field_of(usage, COST_DETAILS_FIELD), dict):
            setattr(usage, COST_DETAILS_FIELD, cost_details)
    except Exception:  # noqa: BLE001 - a missing cost must never break a response
        pass
    return usage

"""Whether LiteLLM may synthesize ``reasoning_effort`` from ``thinking``.

egg's primary route is ``/v1/messages``: Claude Code -> egg-gateway -> LiteLLM
-> OpenRouter, carrying an Anthropic-shaped body with
``thinking: {"type": "enabled", "budget_tokens": N}``. LiteLLM's Anthropic
adapter (``_translate_thinking_to_openai``) turns that into an OpenAI body. For
a Claude model it forwards ``thinking`` unchanged. For anything else —
``is_anthropic_claude_model`` is a substring test for ``anthropic``/``claude``,
so every OpenRouter slug egg routes falls here — it *replaces* the block with a
bucketed ``reasoning_effort``: ``>=10000 -> "high"``, ``>=5000 -> "medium"``,
``>=2000 -> "low"``. Nothing in ``litellm-models.yaml`` is involved; the value
is manufactured per request from the caller's thinking budget.

That synthesized value is not a floor, it is a **cap below the model default**.
Measured directly against OpenRouter (``max_tokens: 16000``, n=4, mean
reasoning tokens):

===================================  ============  ===================
Model                                no parameter  ``effort: "high"``
===================================  ============  ===================
``moonshotai/kimi-k3``               3130          340
``z-ai/glm-5.2``                     1689          1090
===================================  ============  ===================

On kimi-k3 the distributions do not overlap. So sending the adapter's bucket
costs roughly 9x the reasoning depth the model would have produced on its own.

Historically this never mattered: the model-cost map did not carry these slugs,
``OpenrouterConfig`` advertised no reasoning knobs, and ``drop_params`` silently
discarded the synthesized param — which is precisely why these models have been
running at full depth. Patch 7 makes the OpenRouter param gate accurate, which
is right for an *operator-configured* ``reasoning_effort`` and wrong for this
adapter-manufactured one: it would turn a knob nobody set into the effective
setting, with no config file mentioning it and nothing in the logs (Patch 8
only fires on drops, and this param would no longer be dropped).

So Patch 9 gates the synthesis, defaulting it off. ``thinking`` stays out of
the OpenAI body for non-Claude models and only an explicitly configured
``reasoning_effort`` reaches the wire — exactly the property Patch 7 exists to
restore, without the adapter's bucket riding along.

The gate covers the *derived* value only. On an adaptive request
(``thinking: {"type": "adaptive"}`` plus ``output_config: {"effort": ...}``)
the caller states an effort outright; that is an instruction rather than a
manufactured ceiling, and it still reaches the provider with this policy off.
A ``thinking.summary`` request is suppressed along with the derived effort,
because stock carries the summary only as a field of the ``reasoning_effort``
dict — honouring it would mean sending the ceiling. There is no wire shape for
"summary, no effort".

Set ``LITELLM_ANTHROPIC_THINKING_TO_REASONING_EFFORT=1`` to restore stock
behaviour (e.g. for a provider whose models do not reason unless asked, or
after measuring the ``/v1/messages`` path for a specific model). A value that is
neither a recognised on nor off spelling warns once and leaves the policy at its
default, rather than being read as off — off is also the default, so an operator
who typed ``=enabled`` would otherwise have no way to tell "ignored" apart from
"working as configured", on the highest-impact knob in this changeset.
"""

import os

ENV_VAR = "LITELLM_ANTHROPIC_THINKING_TO_REASONING_EFFORT"

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off", "")

# Values already complained about. Bounded by construction: the environment does
# not change mid-process, so this holds at most one entry. Needed because this
# is read once per translated request, and an unconditional warning would be one
# WARNING line per request forever.
_WARNED_VALUES: set[str] = set()


def _log(level: str, message: str, *args: object) -> bool:
    """Log via litellm's ``verbose_logger``, deferring the import.

    Kept out of module scope so this file stays importable — and therefore unit
    testable — where litellm is not installed. Never raises: a diagnostic must
    not be able to break a request.

    Returns whether the call completed without raising, not whether a record
    reached a handler; see the same note in ``openrouter_capabilities._log``.
    """
    try:
        from litellm._logging import verbose_logger

        getattr(verbose_logger, level)(message, *args)
        return True
    except Exception:  # noqa: BLE001 - diagnostics must never break a request
        return False


def should_synthesize_reasoning_effort() -> bool:
    """True when the adapter may derive ``reasoning_effort`` from ``thinking``.

    Defaults to False: on every model egg routes, the derived value measurably
    reduces reasoning depth relative to sending nothing at all.
    """
    raw = os.getenv(ENV_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value not in _FALSY and value not in _WARNED_VALUES:
        # Recorded only once the emit did not raise, for the reason given in
        # ``openrouter_capabilities._warn_env_once``: ``_log`` swallows its own
        # failure, so recording first would let a logger that is not yet in
        # place on the first request suppress the warning permanently.
        if _log(
            "warning",
            "%s=%r is neither an on (%s) nor an off (%s) spelling; leaving "
            "thinking -> reasoning_effort synthesis disabled, which is also the "
            "default — set %s=1 if you meant to enable it.",
            ENV_VAR,
            raw,
            ", ".join(_TRUTHY),
            ", ".join(v for v in _FALSY if v),
            ENV_VAR,
        ):
            _WARNED_VALUES.add(value)
    return False


__all__ = ["ENV_VAR", "should_synthesize_reasoning_effort"]

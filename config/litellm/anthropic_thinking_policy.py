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

Set ``LITELLM_ANTHROPIC_THINKING_TO_REASONING_EFFORT=1`` to restore stock
behaviour (e.g. for a provider whose models do not reason unless asked, or
after measuring the ``/v1/messages`` path for a specific model).
"""

import os

ENV_VAR = "LITELLM_ANTHROPIC_THINKING_TO_REASONING_EFFORT"

_TRUTHY = ("1", "true", "yes", "on")


def should_synthesize_reasoning_effort() -> bool:
    """True when the adapter may derive ``reasoning_effort`` from ``thinking``.

    Defaults to False: on every model egg routes, the derived value measurably
    reduces reasoning depth relative to sending nothing at all.
    """
    raw = os.getenv(ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


__all__ = ["ENV_VAR", "should_synthesize_reasoning_effort"]

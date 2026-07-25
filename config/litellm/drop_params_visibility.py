"""Make ``drop_params`` say what it dropped.

``drop_params`` exists so an unsupported parameter does not fail the whole
request, and that tradeoff is right. But dropping a parameter *changes
generation behaviour*, and in stock LiteLLM 1.86.2 it happens with no signal
at all: the branch that pops them is a bare loop with no logging. A
``reasoning_effort``, ``temperature`` or penalty set in a proxy config simply
never reaches the provider, and nothing in the logs or the response says so.
The config and the wire disagree, silently and indefinitely.

That is not hypothetical here. Every OpenRouter slug egg routes is absent from
LiteLLM's bundled model-cost map, so ``OpenrouterConfig`` advertised no
reasoning knobs and every ``reasoning_effort: high`` in the operator overlay
was discarded before the request body was built. It took a full investigation
to notice (jwbron/egg#3620, #3624). One log line would have made it a
five-minute question.

Patch 7 fixes the OpenRouter false-negative specifically; this covers the rest.
A drop can still be *correct* and worth knowing about: ``poolside/laguna-s-2.1``
genuinely does not accept ``reasoning_effort``, so the knob is dropped on
purpose, and without this the operator has no way to learn why their config
line does nothing.

Mirrors jwbron/litellm#7 (merged into the fork the host proxy runs). The
cluster image pins stock 1.86.2, which predates it, hence this patch.
"""

# Warn-once bookkeeping, keyed by (provider, model, sorted dropped param
# names) so a route that drops the same params on every request warns once
# rather than once per call. Bounded so a long-lived proxy serving many models
# cannot grow it without limit; on overflow the set is CLEARED rather than
# frozen, because a frozen full set stops deduplicating and every subsequent
# request warns again forever. Clearing costs one extra warning per key per
# cycle and keeps both memory and log volume bounded.
_MAX_WARNINGS = 1000
_SEEN: set[tuple[str, str, tuple[str, ...]]] = set()


def _log_warning(message: str, *args: object) -> None:
    """Log via litellm's ``verbose_logger``, deferring the import.

    Kept out of module scope so this file stays importable where litellm is
    not installed — which is what makes it unit testable in the egg repo, the
    stated reason (``config/litellm/Dockerfile``) for keeping it a real file
    rather than a string literal in the patch script.
    """
    from litellm._logging import verbose_logger

    verbose_logger.warning(message, *args)


def warn_dropped_params(
    unsupported_params: dict,
    model: str | None,
    custom_llm_provider: str | None,
) -> None:
    """Log once per (provider, model, param-set) when params are discarded.

    Warns rather than debugs because the caller asked for something and did not
    get it; at debug level it would be invisible in exactly the situation it
    exists for. Never raises: a diagnostic must not be able to break a request.
    """
    try:
        if not unsupported_params:
            return
        dropped = tuple(sorted(unsupported_params.keys()))
        key = (custom_llm_provider or "", model or "", dropped)
        if key in _SEEN:
            return
        if len(_SEEN) >= _MAX_WARNINGS:
            _SEEN.clear()
        _SEEN.add(key)
        # Deliberately states what is known and stops short of prescribing. The
        # param most likely to be dropped on this deployment is
        # ``reasoning_effort``, and it is frequently NOT in any config file:
        # litellm's own Anthropic adapter synthesises it from the caller's
        # `thinking` block on the /v1/messages route. Telling that operator to
        # edit config.yaml sends them looking for a line that does not exist,
        # and telling the operator of a genuinely non-reasoning model to force
        # the param through `allowed_openai_params` converts a correct drop
        # into a provider-side error. A confidently-worded wrong remedy is
        # worse than the silence this replaces.
        _log_warning(
            "litellm.drop_params: dropped %s for model=%s provider=%s — the "
            "provider does not advertise support for them, so they did not "
            "reach it and whatever behaviour they were meant to control is "
            "unchanged. If they came from this model's litellm_params in "
            "config.yaml, remove them or override with `allowed_openai_params: "
            "%s`. If they were synthesized from the request (e.g. "
            "reasoning_effort derived from an Anthropic `thinking` block), the "
            "drop is expected and the model ran at its own default.",
            list(dropped),
            model,
            custom_llm_provider,
            list(dropped),
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break a request
        pass


__all__ = ["warn_dropped_params"]

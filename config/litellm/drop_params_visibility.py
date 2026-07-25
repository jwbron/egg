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

from litellm._logging import verbose_logger

# Warn-once bookkeeping, keyed by (provider, model, sorted dropped param
# names) so a route that drops the same params on every request warns once
# rather than once per call. Bounded so a long-lived proxy serving many models
# cannot grow it without limit; past the cap we stop RECORDING rather than stop
# WARNING, because repeating a warning is the safe direction to fail.
_MAX_WARNINGS = 1000
_SEEN: set[tuple[str, str, tuple[str, ...]]] = set()


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
        if len(_SEEN) < _MAX_WARNINGS:
            _SEEN.add(key)
        verbose_logger.warning(
            "litellm.drop_params: dropping unsupported params %s for model=%s, "
            "provider=%s. They will NOT reach the provider, so whatever behaviour "
            "they were meant to control is unchanged. To send them anyway, add "
            "`allowed_openai_params: %s` to that model's litellm_params in "
            "config.yaml.",
            list(dropped),
            model,
            custom_llm_provider,
            list(dropped),
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break a request
        pass


__all__ = ["warn_dropped_params"]

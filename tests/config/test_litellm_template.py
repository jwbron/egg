"""Regression guard: every litellm ``model_name`` must ship a paired ``[1m]`` alias.

The template at ``config/litellm-models.template.yaml`` is per-operator
config copied to ``~/.config/egg/litellm-models.yaml``. Each routed
``model_name: <x>`` row MUST ship with a sibling
``model_name: <x>[1m]`` row pointing at the same ``litellm_params``
(#2832). The bare and suffixed rows absorb Claude Code startup-probe
suffix leaks — without the alias registered, LiteLLM 400s those probes
with ``Invalid model name``.

This test file enforces five guarantees: every bare row has its
``[1m]`` alias, every ``[1m]`` alias has its bare sibling, paired rows
share equal ``litellm_params`` and equal ``model_info`` (both compared
as parsed YAML) — so probes and real requests can never be quietly
routed through different configs — and no entry sets a reasoning knob
(``reasoning_effort`` / ``thinking``) in a shape LiteLLM silently drops.
It catches a forgetting operator who adds a new backend without its
paired alias or lets the two rows' params drift. Commented-out example
entries in the template are YAML comments, not parsed entries, so
they're naturally excluded.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LITELLM_TEMPLATE = REPO_ROOT / "config" / "litellm-models.template.yaml"

_ALIAS_SUFFIX = "[1m]"

# The reasoning knobs LiteLLM can silently drop. Both are appended under the
# same ``litellm.supports_reasoning`` condition in
# ``OpenrouterConfig.get_supported_openai_params``, so both are dropped in the
# same silence for an unflagged model — guarding only ``reasoning_effort``
# would leave the identical trap open one key over.
_REASONING_KNOBS = ("reasoning_effort", "thinking")

# Provider prefix -> the reasoning knobs that provider's LiteLLM chat config
# advertises ONLY when ``litellm.supports_reasoning(model, provider)`` is true.
#
# For these prefix/knob combinations, and only these, ``model_info:
# {supports_reasoning: true}`` on the entry is the fix: ``Router._create_deployment``
# registers the entry's ``model_info`` into ``litellm.model_cost`` keyed by the
# raw ``litellm_params.model`` string (``litellm/router.py:7911-7923``), which is
# exactly what ``supports_reasoning`` reads back. Nothing in that path is
# OpenRouter-specific.
#
# Verified by reading each ``get_supported_openai_params`` at the pinned litellm
# **v1.86.2** (``config/litellm/Dockerfile``). This set moves on a version bump —
# re-read the configs and update the version noted here when you bump.
#
# Note the knob column: ``zai`` and ``minimax`` gate ``thinking`` only and never
# advertise ``reasoning_effort`` at all, so a per-provider tuple would be wrong
# for them. That is why this maps to knobs rather than to a bare prefix list.
_SUPPORTS_REASONING_GATED_KNOBS: dict[str, tuple[str, ...]] = {
    # llms/openrouter/chat/transformation.py:36-49 — both under one ``if``
    "openrouter/": ("reasoning_effort", "thinking"),
    "groq/": ("reasoning_effort",),  # llms/groq/chat/transformation.py:107-110
    "xai/": ("reasoning_effort",),  # llms/xai/chat/transformation.py:77-80
    "deepinfra/": ("reasoning_effort",),  # llms/deepinfra/chat/transformation.py:80-84
    "cerebras/": ("reasoning_effort",),  # llms/cerebras/chat.py:74-75
    "fireworks_ai/": ("reasoning_effort",),  # llms/fireworks_ai/chat/transformation.py:121-122
    "perplexity/": ("reasoning_effort",),  # llms/perplexity/chat/transformation.py:58-61
    "bedrock_mantle/": ("reasoning_effort",),  # llms/bedrock_mantle/chat/transformation.py:55-59
    "zai/": ("thinking",),  # llms/zai/chat/transformation.py:51-54
    "minimax/": ("thinking",),  # llms/minimax/chat/transformation.py:97
}

# Provider prefix -> reasoning knobs that provider advertises UNCONDITIONALLY,
# i.e. with no ``model_info`` needed and no model-map lookup involved.
#
# EMPTY BY DESIGN: no such provider has been verified yet. This is not the same
# category as the map above — here ``model_info`` is unnecessary, there it is
# required. A provider in neither map is one where the knob is popped silently
# whatever the config says: ``TogetherAIConfig.get_supported_openai_params``
# (``llms/together_ai/chat.py:18-46``) only *subtracts* from
# ``OpenAIGPTConfig``'s base list, and that base list
# (``llms/openai/chat/gpt_transformation.py:138-187``) carries no reasoning knob
# under any condition. Add a prefix here only after reading that provider's
# ``get_supported_openai_params`` against the pinned litellm version, and say
# which version you checked.
_UNCONDITIONAL_REASONING_KNOBS: dict[str, tuple[str, ...]] = {}


def _load_model_list() -> list[dict]:
    """Return the live ``model_list`` entries in the template."""
    outer = yaml.safe_load(LITELLM_TEMPLATE.read_text())
    inner_body = outer["data"]["config.yaml"]
    inner = yaml.safe_load(inner_body)
    return inner.get("model_list", []) or []


def _load_model_names() -> set[str]:
    """Return the set of live ``model_name`` values in the template."""
    return {entry["model_name"] for entry in _load_model_list()}


def _diverged_pairs(entries: list[dict], field: str) -> list[str]:
    """Bare ``model_name``s whose ``[1m]`` sibling carries a different *field*.

    ``field`` is a top-level key of a ``model_list`` entry (``litellm_params``,
    ``model_info``); values are compared as parsed YAML. Rows with no ``[1m]``
    sibling are not this predicate's business — ``test_every_bare_model_has_1m_alias``
    already covers that."""
    values = {entry["model_name"]: entry.get(field) for entry in entries}
    return sorted(
        name
        for name, bare_value in values.items()
        if not name.endswith(_ALIAS_SUFFIX)
        and f"{name}{_ALIAS_SUFFIX}" in values
        and values[f"{name}{_ALIAS_SUFFIX}"] != bare_value
    )


def _entry_model(entry: dict) -> str:
    """The provider-qualified model string for *entry*.

    ``custom_llm_provider`` is an equally valid way to name the provider:
    ``model: qwen/qwen3-max`` + ``custom_llm_provider: openrouter`` routes
    exactly like ``model: openrouter/qwen/qwen3-max``. Normalise both spellings
    to the prefixed form so provider lookup sees the same string either way —
    otherwise the second shape falls through to the unverified-provider bucket
    and gets advice written for a different provider."""
    params = entry.get("litellm_params") or {}
    model = str(params.get("model") or "")
    provider = str(params.get("custom_llm_provider") or "")
    if provider and not model.startswith(f"{provider}/"):
        return f"{provider}/{model}"
    return model


def _reasoning_knobs_set(entry: dict) -> list[str]:
    """Reasoning knobs this entry sets in ``litellm_params`` (possibly none)."""
    params = entry.get("litellm_params") or {}
    return [knob for knob in _REASONING_KNOBS if params.get(knob) is not None]


def _knobs_for_provider(model: str, table: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Knobs *table* records for the provider prefixing *model* (``()`` if none)."""
    for prefix, knobs in table.items():
        if model.startswith(prefix):
            return knobs
    return ()


def _reasoning_knob_offenders(entries: list[dict]) -> list[str]:
    """Names of entries whose ``litellm_params`` reasoning knob is a silent
    no-op: set on a provider that gates it on ``litellm.supports_reasoning``,
    but without ``model_info.supports_reasoning`` to satisfy that gate.

    Keyed off ``_SUPPORTS_REASONING_GATED_KNOBS`` rather than a single provider,
    because the gate is not OpenRouter-specific — nine other provider configs
    use the identical fail-closed mechanism, and for all of them ``model_info``
    is the working fix. Knobs the provider does not gate are not this
    predicate's business; ``_unverified_provider_reasoning_knob`` has them."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if set(_reasoning_knobs_set(entry))
        & set(_knobs_for_provider(_entry_model(entry), _SUPPORTS_REASONING_GATED_KNOBS))
        and not (entry.get("model_info") or {}).get("supports_reasoning")
    )


def _unverified_provider_reasoning_knob(entries: list[dict]) -> list[str]:
    """Names of entries setting a reasoning knob their provider is not known to
    advertise at all — neither gated on ``supports_reasoning`` nor
    unconditional.

    Deliberately ignores ``model_info``: these providers never consult
    ``litellm.supports_reasoning`` for this knob, so accepting the flag here
    would let the guard certify the exact defect it exists to catch."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if set(_reasoning_knobs_set(entry))
        - set(_knobs_for_provider(_entry_model(entry), _SUPPORTS_REASONING_GATED_KNOBS))
        - set(_knobs_for_provider(_entry_model(entry), _UNCONDITIONAL_REASONING_KNOBS))
    )


class TestLitellmAliasInvariant:
    """Every bare ``model_name`` must have a paired ``<name>[1m]`` row,
    and vice versa, and the two rows must share both the same
    ``litellm_params`` and the same ``model_info``. The symmetric naming
    errors — a bare row without its alias, or an ``[1m]`` alias without
    its bare sibling — and a divergence in either block between paired
    rows all mean probes and real requests are served by different
    routing tables.
    """

    def test_every_bare_model_has_1m_alias(self):
        names = _load_model_names()
        bare = sorted(n for n in names if not n.endswith(_ALIAS_SUFFIX))
        missing = [n for n in bare if f"{n}{_ALIAS_SUFFIX}" not in names]
        assert not missing, (
            "model_list entries without a paired `<name>[1m]` alias: "
            f"{missing} — add a sibling row pointing at the same "
            "`litellm_params` (see #2832, #2841)"
        )

    def test_no_orphaned_1m_alias(self):
        names = _load_model_names()
        suffixed = sorted(n for n in names if n.endswith(_ALIAS_SUFFIX))
        orphaned = [n for n in suffixed if n.removesuffix(_ALIAS_SUFFIX) not in names]
        assert not orphaned, (
            "model_list `[1m]` aliases without a bare sibling: "
            f"{orphaned} — add a `<name>` row pointing at the same "
            "`litellm_params` (see #2832, #2841)"
        )

    def test_diverged_pairs_flags_a_divergent_sibling(self):
        # Every template entry's ``model_info`` is ``None`` today, so the two
        # template-level parity tests below pass vacuously — exercise the
        # shared predicate directly so a regression in it cannot hide.
        entries = [
            {"model_name": "m", "model_info": {"supports_reasoning": True}},
            {"model_name": f"m{_ALIAS_SUFFIX}"},
        ]
        assert _diverged_pairs(entries, "model_info") == ["m"]

    def test_diverged_pairs_accepts_matching_siblings(self):
        entries = [
            {"model_name": "m", "model_info": {"supports_reasoning": True}},
            {"model_name": f"m{_ALIAS_SUFFIX}", "model_info": {"supports_reasoning": True}},
        ]
        assert _diverged_pairs(entries, "model_info") == []

    def test_diverged_pairs_ignores_rows_without_a_sibling(self):
        entries = [{"model_name": "lonely", "litellm_params": {"model": "openrouter/x/y"}}]
        assert _diverged_pairs(entries, "litellm_params") == []

    def test_paired_rows_share_model_info(self):
        # ``model_info`` is a sibling of ``litellm_params``, so the params
        # comparison below does not see it — but it carries
        # ``supports_reasoning``, which decides whether ``reasoning_effort``
        # reaches the wire at all. Divergence here means the probe alias and
        # the real row run at different reasoning depths.
        diverged = _diverged_pairs(_load_model_list(), "model_info")
        assert not diverged, (
            "paired bare/`[1m]` rows with diverging `model_info`: "
            f"{diverged} — the `[1m]` alias must carry the same `model_info` "
            "as its bare sibling (notably `supports_reasoning`, which gates "
            "whether `reasoning_effort` is sent at all)"
        )

    def test_paired_rows_share_litellm_params(self):
        diverged = _diverged_pairs(_load_model_list(), "litellm_params")
        assert not diverged, (
            "paired bare/`[1m]` rows with diverging `litellm_params`: "
            f"{diverged} — the `[1m]` alias must point at the same "
            "`litellm_params` as its bare sibling, or probes and real "
            "requests are quietly served by different configs (see #2832, #2841)"
        )


class TestReasoningEffortIsNotASilentNoop:
    """On a ``supports_reasoning``-gated provider, ``litellm_params``'
    reasoning knob reaches the wire only when LiteLLM believes the model is
    reasoning-capable.

    ``OpenrouterConfig.get_supported_openai_params`` advertises
    ``reasoning_effort`` / ``thinking`` — both under the same ``if`` — only when
    ``litellm.supports_reasoning`` is true, and that reads LiteLLM's built-in
    model-cost map. A model absent from the map answers False, so the gate fails
    closed and ``drop_params: true`` then discards the parameter with no error
    and no log line.

    Absent is the normal state for an OpenRouter slug — verified against the
    pinned litellm 1.86.2, ``qwen/qwen3-max`` and
    ``moonshotai/kimi-k2-thinking`` are both absent and answer False, while
    ``deepseek/deepseek-r1`` is present and answers True. So the failure mode
    is not exotic: set ``reasoning_effort`` on a new slug and the agent
    silently runs at the provider's default depth.

    The fix is ``model_info: {supports_reasoning: true}`` on the same entry
    (verified to put ``reasoning_effort`` on the wire), or OpenRouter's native
    ``extra_body.reasoning.effort``, which bypasses the mapper entirely. This
    guard pins the first form so the template cannot ship the silent no-op.

    The gate is **not** OpenRouter-specific: nine other provider configs use the
    identical fail-closed mechanism, and the ``model_info`` fix works for all of
    them because ``Router._create_deployment`` registers ``model_info`` into
    ``litellm.model_cost`` under the raw ``litellm_params.model`` string
    whatever the provider. ``_SUPPORTS_REASONING_GATED_KNOBS`` records which
    knob each of them gates. Providers outside that map get the separate check
    below, where ``model_info`` genuinely is a placebo."""

    def test_predicate_flags_the_silent_noop(self):
        # A guard that cannot fire is not a guard. Exercise it against the
        # exact shape #3599 reports in the wild: reasoning_effort set in
        # litellm_params, model not flagged.
        assert _reasoning_knob_offenders(
            [
                {
                    "model_name": "laguna-s-2.1",
                    "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
                }
            ]
        ) == ["laguna-s-2.1"]

    def test_predicate_accepts_the_flagged_form(self):
        assert (
            _reasoning_knob_offenders(
                [
                    {
                        "model_name": "ok",
                        "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
                        "model_info": {"supports_reasoning": True},
                    }
                ]
            )
            == []
        )

    def test_predicate_ignores_entries_not_setting_reasoning_effort(self):
        # The extra_body.reasoning form bypasses the mapper, so it needs no
        # model_info flag and must not be flagged as an offender.
        assert (
            _reasoning_knob_offenders(
                [
                    {
                        "model_name": "native",
                        "litellm_params": {
                            "model": "openrouter/x/y",
                            "extra_body": {"reasoning": {"effort": "high"}},
                        },
                    }
                ]
            )
            == []
        )

    def test_predicate_flags_thinking_too(self):
        # `thinking` sits under the same `if litellm.supports_reasoning(...)`
        # in OpenrouterConfig, so an unflagged model drops it just as quietly.
        assert _reasoning_knob_offenders(
            [
                {
                    "model_name": "thinky",
                    "litellm_params": {"model": "openrouter/x/y", "thinking": {"budget_tokens": 1}},
                }
            ]
        ) == ["thinky"]

    def test_predicate_ignores_providers_that_never_advertise_the_knob(self):
        # `TogetherAIConfig` does not consult `supports_reasoning` at all, so
        # this entry is not an offender *by this predicate's rule* — demanding
        # `model_info` here would be demanding a placebo. It is caught by
        # TestReasoningKnobOnUnverifiedProvider below instead.
        assert (
            _reasoning_knob_offenders(
                [
                    {
                        "model_name": "together",
                        "litellm_params": {
                            "model": "together_ai/Qwen/Qwen2.5-Coder-32B-Instruct",
                            "reasoning_effort": "high",
                        },
                    }
                ]
            )
            == []
        )

    def test_predicate_flags_non_openrouter_gated_providers(self):
        # The gate is not OpenRouter-specific: groq/xai/deepinfra/cerebras/
        # fireworks_ai/perplexity/bedrock_mantle gate `reasoning_effort` on the
        # same `litellm.supports_reasoning` call, so an unflagged model there is
        # the identical silent no-op — and `model_info` is the identical fix.
        entries = [
            {
                "model_name": f"m-{prefix.strip('/')}",
                "litellm_params": {"model": f"{prefix}vendor/model", "reasoning_effort": "high"},
            }
            for prefix in ("groq/", "xai/", "deepinfra/", "cerebras/")
        ]
        assert _reasoning_knob_offenders(entries) == [
            "m-cerebras",
            "m-deepinfra",
            "m-groq",
            "m-xai",
        ]

    def test_predicate_accepts_model_info_on_a_non_openrouter_gated_provider(self):
        # The fix must work off OpenRouter too: `Router._create_deployment`
        # registers `model_info` under the raw `groq/...` string, which is what
        # `supports_reasoning(model, custom_llm_provider="groq")` reads back.
        assert (
            _reasoning_knob_offenders(
                [
                    {
                        "model_name": "r1-distill",
                        "litellm_params": {
                            "model": "groq/deepseek-r1-distill-llama-70b",
                            "reasoning_effort": "high",
                        },
                        "model_info": {"supports_reasoning": True},
                    }
                ]
            )
            == []
        )

    def test_predicate_is_per_knob_not_per_provider(self):
        # `zai` gates `thinking` only and never advertises `reasoning_effort`,
        # so `model_info` is the fix for the first knob and a placebo for the
        # second. A per-provider rule would give one of them the wrong advice.
        zai = {"model_name": "glm", "litellm_params": {"model": "zai/glm-4.6"}}
        thinking = {**zai, "litellm_params": {**zai["litellm_params"], "thinking": {"type": "e"}}}
        effort = {**zai, "litellm_params": {**zai["litellm_params"], "reasoning_effort": "high"}}
        assert _reasoning_knob_offenders([thinking]) == ["glm"]
        assert _unverified_provider_reasoning_knob([thinking]) == []
        assert _reasoning_knob_offenders([effort]) == []
        assert _unverified_provider_reasoning_knob([effort]) == ["glm"]

    def test_predicate_reads_custom_llm_provider_form(self):
        # `model: qwen/qwen3-max` + `custom_llm_provider: openrouter` routes
        # exactly like the prefixed spelling and must land in the same bucket.
        entry = {
            "model_name": "qwen3-max",
            "litellm_params": {
                "model": "qwen/qwen3-max",
                "custom_llm_provider": "openrouter",
                "reasoning_effort": "high",
            },
        }
        assert _reasoning_knob_offenders([entry]) == ["qwen3-max"]
        assert _unverified_provider_reasoning_knob([entry]) == []

    def test_reasoning_effort_requires_supports_reasoning(self):
        offenders = _reasoning_knob_offenders(_load_model_list())
        assert not offenders, (
            "model_list entries setting `litellm_params.reasoning_effort` (or "
            "`thinking`) on a provider that gates it on "
            f"`litellm.supports_reasoning`, without `model_info`: {offenders} "
            "— LiteLLM drops the parameter silently for any model missing from "
            "its built-in model-cost map, so as written this is a no-op and "
            "the agent runs at the provider's default depth. Add "
            "`model_info: {supports_reasoning: true}` to the entry (it is "
            "registered into the model-cost map under the raw "
            "`litellm_params.model` string, so it satisfies the gate on every "
            "provider in `_SUPPORTS_REASONING_GATED_KNOBS`), or on "
            "`openrouter/*` use the native `extra_body.reasoning.effort` "
            "instead. Pin the flag even for a model currently in the map: map "
            "membership changes under you on a litellm bump, the config does not."
        )


class TestReasoningKnobOnUnverifiedProvider:
    """Where a provider does not advertise the knob at all,
    ``model_info.supports_reasoning`` is not the fix — it is not read.

    This is the complement of ``_SUPPORTS_REASONING_GATED_KNOBS``, not "anything
    that isn't OpenRouter". ``TogetherAIConfig.get_supported_openai_params``
    (``llms/together_ai/chat.py:18-46``) only *subtracts* ``response_format`` /
    tool params from ``OpenAIGPTConfig``'s base list, and that base list carries
    no reasoning knob under any condition — nothing there ever calls
    ``litellm.supports_reasoning``. So on a ``together_ai/*`` route the
    parameter is popped silently whatever ``model_info`` says, which is why
    these entries need a separate guard rather than the gated-provider one
    waved over them.

    There is no config-shaped fix to assert here, so the guard demands a
    human-verified entry in ``_UNCONDITIONAL_REASONING_KNOBS`` instead: someone
    has to read that provider's ``get_supported_openai_params`` against the
    pinned litellm before the build goes green. Note the two escape hatches are
    not interchangeable — a provider that gates the knob belongs in
    ``_SUPPORTS_REASONING_GATED_KNOBS`` (where ``model_info`` is then
    *required*); listing it here instead would stop checking it entirely and let
    the genuine silent no-op through."""

    def test_predicate_flags_an_unverified_provider(self):
        assert _unverified_provider_reasoning_knob(
            [
                {
                    "model_name": "qwen3-coder-30b",
                    "litellm_params": {
                        "model": "together_ai/Qwen/Qwen2.5-Coder-32B-Instruct",
                        "reasoning_effort": "high",
                    },
                    # Present and useless — the point of the separate guard.
                    "model_info": {"supports_reasoning": True},
                }
            ]
        ) == ["qwen3-coder-30b"]

    def test_predicate_ignores_gated_provider_entries(self):
        # These belong to the other guard, whose `model_info` advice works.
        assert (
            _unverified_provider_reasoning_knob(
                [
                    {
                        "model_name": "or",
                        "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
                    },
                    {
                        "model_name": "groq",
                        "litellm_params": {"model": "groq/x", "reasoning_effort": "high"},
                    },
                ]
            )
            == []
        )

    def test_predicate_ignores_entries_without_a_reasoning_knob(self):
        assert (
            _unverified_provider_reasoning_knob(
                [
                    {
                        "model_name": "plain",
                        "litellm_params": {
                            "model": "together_ai/Qwen/Qwen2.5-Coder-32B-Instruct",
                            "drop_params": True,
                        },
                    }
                ]
            )
            == []
        )

    def test_no_reasoning_knob_on_unverified_providers(self):
        offenders = _unverified_provider_reasoning_knob(_load_model_list())
        assert not offenders, (
            "model_list entries setting `litellm_params.reasoning_effort` (or "
            "`thinking`) on a provider not recorded in this file as "
            f"advertising that knob: {offenders} — most OpenAI-compatible "
            "provider configs (together_ai among them) never advertise it "
            "under any condition, so `drop_params: true` pops it silently and "
            "`model_info.supports_reasoning` does not help, because nothing on "
            "that path reads it. Read that provider's "
            "`get_supported_openai_params` against the pinned litellm version, "
            "then: if it advertises the knob behind "
            "`litellm.supports_reasoning`, add the prefix/knob to "
            "`_SUPPORTS_REASONING_GATED_KNOBS` and set "
            "`model_info: {supports_reasoning: true}` on the entry; if it "
            "advertises the knob unconditionally, add it to "
            "`_UNCONDITIONAL_REASONING_KNOBS`; note the version you checked "
            "either way. Only if it advertises the knob nowhere is dropping it "
            "the answer — there is then no config that makes it take effect."
        )

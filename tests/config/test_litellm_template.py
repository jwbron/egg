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
(``reasoning_effort`` / ``thinking``) in a shape LiteLLM silently
drops. It catches a
forgetting operator who adds a new backend without its paired alias or
lets the two rows' params drift. Commented-out example entries in the
template are YAML comments, not parsed entries, so they're naturally
excluded.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LITELLM_TEMPLATE = REPO_ROOT / "config" / "litellm-models.template.yaml"

_ALIAS_SUFFIX = "[1m]"

_OPENROUTER_PREFIX = "openrouter/"

# Both knobs are appended under the same ``litellm.supports_reasoning``
# condition in ``OpenrouterConfig.get_supported_openai_params``, so both are
# dropped in the same silence for an unflagged model — guarding only
# ``reasoning_effort`` would leave the identical trap open one key over.
_REASONING_KNOBS = ("reasoning_effort", "thinking")

# Non-OpenRouter provider prefixes whose LiteLLM chat config has been verified
# to advertise ``reasoning_effort`` at all.
#
# EMPTY BY DESIGN. ``model_info.supports_reasoning`` is an OpenRouter-specific
# mechanism: only ``OpenrouterConfig`` consults ``litellm.supports_reasoning``.
# Other providers answer from a static list — e.g. ``TogetherAIConfig`` only
# subtracts from ``OpenAIGPTConfig``'s base list, which contains no
# ``reasoning_effort`` under any condition — so setting ``model_info`` there
# changes nothing and the parameter is still popped silently. Add a prefix
# here only after reading that provider's ``get_supported_openai_params``
# against the pinned litellm version, and say which version you checked.
_REASONING_EFFORT_NATIVE_PROVIDERS: tuple[str, ...] = ()


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
    return str((entry.get("litellm_params") or {}).get("model") or "")


def _reasoning_knobs_set(entry: dict) -> list[str]:
    """Reasoning knobs this entry sets in ``litellm_params`` (possibly none)."""
    params = entry.get("litellm_params") or {}
    return [knob for knob in _REASONING_KNOBS if params.get(knob) is not None]


def _reasoning_effort_offenders(entries: list[dict]) -> list[str]:
    """Names of ``openrouter/*`` entries whose ``litellm_params`` reasoning knob
    is a silent no-op: set, but without ``model_info.supports_reasoning`` to
    make LiteLLM advertise the parameter as supported.

    Scoped to OpenRouter deliberately: ``supports_reasoning`` is the lever only
    ``OpenrouterConfig`` pulls. Other providers are covered by
    ``_unverified_provider_reasoning_effort`` below, because there the same
    ``model_info`` block would be a placebo. See the class docstring below."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if _entry_model(entry).startswith(_OPENROUTER_PREFIX)
        and _reasoning_knobs_set(entry)
        and not (entry.get("model_info") or {}).get("supports_reasoning")
    )


def _unverified_provider_reasoning_effort(entries: list[dict]) -> list[str]:
    """Names of non-OpenRouter entries setting a reasoning knob on a provider
    that has not been verified to advertise it.

    Deliberately ignores ``model_info``: outside OpenRouter it is not consulted,
    so accepting it here would let the guard certify the exact defect it exists
    to catch."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if not _entry_model(entry).startswith(_OPENROUTER_PREFIX)
        and _reasoning_knobs_set(entry)
        and not _entry_model(entry).startswith(_REASONING_EFFORT_NATIVE_PROVIDERS)
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
    """On ``openrouter/*``, ``litellm_params.reasoning_effort`` reaches the wire
    only when LiteLLM believes the model is reasoning-capable.

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

    ``supports_reasoning`` is an OpenRouter-only lever, so the guard is scoped
    to ``openrouter/*`` and other providers get their own check below: for them
    a ``model_info`` block is a placebo, and one guard covering both would go
    green on a config that still drops the parameter."""

    def test_predicate_flags_the_silent_noop(self):
        # A guard that cannot fire is not a guard. Exercise it against the
        # exact shape #3599 reports in the wild: reasoning_effort set in
        # litellm_params, model not flagged.
        assert _reasoning_effort_offenders(
            [
                {
                    "model_name": "laguna-s-2.1",
                    "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
                }
            ]
        ) == ["laguna-s-2.1"]

    def test_predicate_accepts_the_flagged_form(self):
        assert (
            _reasoning_effort_offenders(
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
            _reasoning_effort_offenders(
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
        assert _reasoning_effort_offenders(
            [
                {
                    "model_name": "thinky",
                    "litellm_params": {"model": "openrouter/x/y", "thinking": {"budget_tokens": 1}},
                }
            ]
        ) == ["thinky"]

    def test_predicate_ignores_non_openrouter_providers(self):
        # `supports_reasoning` is not consulted off OpenRouter, so this entry
        # is not an offender *by this predicate's rule* — it is caught by
        # TestReasoningEffortOffOpenrouter below, whose advice actually works.
        assert (
            _reasoning_effort_offenders(
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

    def test_reasoning_effort_requires_supports_reasoning(self):
        offenders = _reasoning_effort_offenders(_load_model_list())
        assert not offenders, (
            "`openrouter/*` model_list entries setting "
            "`litellm_params.reasoning_effort` (or `thinking`) without "
            f"`model_info.supports_reasoning: true`: {offenders} — LiteLLM "
            "drops the parameter silently for any model missing from its "
            "built-in model-cost map, so as written this is a no-op and the "
            "agent runs at the provider's default depth. Either add "
            "`model_info: {supports_reasoning: true}` to the entry, or use "
            "OpenRouter's native `extra_body.reasoning.effort` instead. Pin "
            "the flag even for a model currently in the map: map membership "
            "changes under you on a litellm bump, the config does not."
        )


class TestReasoningEffortOffOpenrouter:
    """Off OpenRouter, ``model_info.supports_reasoning`` is not the fix — it is
    not read at all.

    Only ``OpenrouterConfig.get_supported_openai_params`` consults
    ``litellm.supports_reasoning``. Every other provider answers from a static
    list: ``TogetherAIConfig.get_supported_openai_params`` only *subtracts*
    ``response_format`` / tool params from ``OpenAIGPTConfig``'s base list, and
    that base list carries no ``reasoning_effort`` under any condition. So on a
    ``together_ai/*`` route the parameter is popped silently whatever
    ``model_info`` says — which is why these entries need a separate guard
    rather than the OpenRouter one waved over them.

    There is no config-shaped fix to assert here, so the guard demands a
    human-verified provider prefix in ``_REASONING_EFFORT_NATIVE_PROVIDERS``
    instead: someone has to read that provider's ``get_supported_openai_params``
    against the pinned litellm before the build goes green."""

    def test_predicate_flags_an_unverified_provider(self):
        assert _unverified_provider_reasoning_effort(
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

    def test_predicate_ignores_openrouter_entries(self):
        assert (
            _unverified_provider_reasoning_effort(
                [
                    {
                        "model_name": "or",
                        "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
                    }
                ]
            )
            == []
        )

    def test_predicate_ignores_entries_without_a_reasoning_knob(self):
        assert (
            _unverified_provider_reasoning_effort(
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

    def test_no_reasoning_effort_on_unverified_providers(self):
        offenders = _unverified_provider_reasoning_effort(_load_model_list())
        assert not offenders, (
            "non-`openrouter/*` model_list entries setting "
            "`litellm_params.reasoning_effort` (or `thinking`) on a provider "
            f"not listed in `_REASONING_EFFORT_NATIVE_PROVIDERS`: {offenders} "
            "— `model_info.supports_reasoning` does NOT help here; it is an "
            "OpenRouter-only mechanism, and most OpenAI-compatible provider "
            "configs (together_ai among them) never advertise "
            "`reasoning_effort`, so `drop_params: true` pops it silently. "
            "Read that provider's `get_supported_openai_params` against the "
            "pinned litellm version; if it does advertise the parameter, add "
            "the prefix to `_REASONING_EFFORT_NATIVE_PROVIDERS` in this file "
            "noting the version you checked. Otherwise drop the knob — there "
            "is no config that makes it take effect."
        )

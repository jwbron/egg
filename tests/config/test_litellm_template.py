"""Regression guard: every litellm ``model_name`` must ship a paired ``[1m]`` alias.

The template at ``config/litellm-models.template.yaml`` is per-operator
config copied to ``~/.config/egg/litellm-models.yaml``. Each routed
``model_name: <x>`` row MUST ship with a sibling
``model_name: <x>[1m]`` row pointing at the same ``litellm_params``
(#2832). The bare and suffixed rows absorb Claude Code startup-probe
suffix leaks — without the alias registered, LiteLLM 400s those probes
with ``Invalid model name``.

This test file enforces six guarantees: every bare row has its
``[1m]`` alias, every ``[1m]`` alias has its bare sibling, paired rows
share equal ``litellm_params`` and equal ``model_info`` (both compared
as parsed YAML) — so probes and real requests can never be quietly
routed through different configs — no entry sets a reasoning knob
(``reasoning_effort`` / ``thinking``) in a shape LiteLLM silently drops,
and the litellm version the provider tables below were read against is
still the version the image pins. It catches a forgetting operator who
adds a new backend without its paired alias or lets the two rows' params
drift. Commented-out example entries in the template are YAML comments,
not parsed entries, so they're naturally excluded.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LITELLM_TEMPLATE = REPO_ROOT / "config" / "litellm-models.template.yaml"
LITELLM_DOCKERFILE = REPO_ROOT / "config" / "litellm" / "Dockerfile"

_ALIAS_SUFFIX = "[1m]"

# The litellm version every provider-config claim in this file was read
# against.
# ``TestProviderTablesTrackThePinnedLitellm::test_verified_version_matches_the_image_pin``
# fails the build if the image is bumped past it, because which providers gate a
# reasoning knob — and on what — is version-specific: providers get added, and a
# gate can flip from fail-closed to fail-open in a patch release. A bump means
# re-reading the ``get_supported_openai_params`` of each prefix below, then
# moving this string.
_VERIFIED_LITELLM_VERSION = "1.86.2"

# The reasoning knobs LiteLLM can silently drop. Both are appended under the
# same ``litellm.supports_reasoning`` condition in
# ``OpenrouterConfig.get_supported_openai_params``, so both are dropped in the
# same silence for an unflagged model — guarding only ``reasoning_effort``
# would leave the identical trap open one key over.
_REASONING_KNOBS = ("reasoning_effort", "thinking")

# Provider prefix -> the reasoning knobs that provider's LiteLLM chat config
# decides to advertise by consulting ``litellm.supports_reasoning(model, …)``,
# AND whose gate call is reachable by the entry's own ``model_info``.
#
# For these prefix/knob combinations ``model_info: {supports_reasoning: true}``
# on the entry is the thing that makes the knob reach the wire:
# ``Router._create_deployment`` registers the entry's ``model_info`` into
# ``litellm.model_cost`` (``litellm/router.py:7186-7215``, mirrored for
# dynamically added deployments at ``:7897-7923``), which is exactly what
# ``supports_reasoning`` → ``_get_model_info_helper`` reads back
# (``litellm/utils.py:2775-2781``). Nothing in that path is OpenRouter-specific.
#
# MEMBERSHIP CRITERION — BOTH halves, not just the first:
#
#   1. the provider's ``get_supported_openai_params`` consults
#      ``litellm.supports_reasoning`` for that knob, and
#   2. the gate call passes ``custom_llm_provider``, spelled the same as the
#      prefix an operator writes in ``litellm_params.model``.
#
# (2) is what was silently assumed for several rounds, and it is not free.
# The router files ``model_info`` under the *written* model string
# (``custom_llm_provider + "/" + model`` when that field is set, raw ``model``
# otherwise). Request-time lookup builds its candidate keys from
# ``custom_llm_provider + "/" + <stripped model>``
# (``_get_potential_model_names``, ``litellm/utils.py:5551-5586``) — but ONLY
# when the gate passed one. A gate that calls ``supports_reasoning(model)`` bare
# gets ``custom_llm_provider=None``, and that branch never prefixes any
# candidate, so the registered ``openrouter/…``-shaped key is not a candidate at
# all. Those providers go in ``_GATE_UNREACHABLE_BY_MODEL_INFO`` below — the
# gate is real, but ``model_info`` cannot satisfy it.
#
# CLASSIFY BY SHAPE, NOT BY COUNT. A hard count ("nine providers do this") is a
# claim that rots on every litellm bump and tells you nothing about the entry in
# front of you. Read the provider's ``get_supported_openai_params`` and ask what
# it does when the model is ABSENT from LiteLLM's map. Two shapes appear at
# the pinned version, both of which want ``model_info`` set:
#
#   (a) pure gate — the ``supports_reasoning`` call is the only condition, so
#       an unmapped model fails closed and the knob is dropped in silence.
#       ``model_info`` is REQUIRED. Most rows below are this shape.
#   (b) name heuristics OR gate — ``anthropic`` lets a recognised name
#       (``claude-3-7-sonnet``, the Claude 4.6/4.7 families) through without
#       consulting the map at all, and falls back to the gate for every other
#       name. ``model_info`` is what carries those other names; on a recognised
#       name it is merely redundant.
#
# A third shape — gated, but on a key ``model_info`` cannot reach — is NOT here;
# see ``_GATE_UNREACHABLE_BY_MODEL_INFO``.
#
# The contrast case, and the reason the shape matters more than the call:
# azure's o-series config calls ``supports_reasoning`` too
# (``llms/azure/chat/o_series_transformation.py:49-71``) but FAILS OPEN — a
# deployment name absent from LiteLLM's map is *assumed* reasoning-capable and
# gets ``reasoning_effort`` advertised unconditionally. Same call, opposite
# failure mode. So "calls ``supports_reasoning``" is not the test; "what happens
# when the model is absent" is.
#
# When a provider's shape is unclear, put it here rather than in
# ``_UNCONDITIONAL_REASONING_KNOBS``: demanding ``model_info`` never suppresses
# a knob that would otherwise be sent, whereas the other table stops checking
# the entry altogether.
#
# Verified by reading each ``get_supported_openai_params`` at the pinned litellm
# (``_VERIFIED_LITELLM_VERSION``); line numbers are from that tag.
#
# Note the knob column: ``zai`` and ``minimax`` gate ``thinking`` only and never
# advertise ``reasoning_effort`` at all, so a per-provider tuple would be wrong
# for them. That is why this maps to knobs rather than to a bare prefix list.
_SUPPORTS_REASONING_GATED_KNOBS: dict[str, tuple[str, ...]] = {
    # (a) pure gate, both knobs under one ``if``; passes ``custom_llm_provider``
    "openrouter/": ("reasoning_effort", "thinking"),  # llms/openrouter/chat/transformation.py:41-48
    "gemini/": ("reasoning_effort", "thinking"),  # llms/gemini/chat/transformation.py:96-98
    # (a) pure gate, one knob; each passes ``custom_llm_provider`` spelled
    # exactly as the prefix below (``self.custom_llm_provider`` or a literal)
    "groq/": ("reasoning_effort",),  # llms/groq/chat/transformation.py:107-110
    "xai/": ("reasoning_effort",),  # llms/xai/chat/transformation.py:77-80
    "deepinfra/": ("reasoning_effort",),  # llms/deepinfra/chat/transformation.py:80-84
    "cerebras/": ("reasoning_effort",),  # llms/cerebras/chat.py:74-75
    "fireworks_ai/": ("reasoning_effort",),  # llms/fireworks_ai/chat/transformation.py:121-122
    "perplexity/": ("reasoning_effort",),  # llms/perplexity/chat/transformation.py:58-61
    "bedrock_mantle/": ("reasoning_effort",),  # llms/bedrock_mantle/chat/transformation.py:55-59
    "zai/": ("thinking",),  # llms/zai/chat/transformation.py:51-54
    "minimax/": ("thinking",),  # llms/minimax/chat/transformation.py:97
    # (b) name heuristics OR gate; gate passes ``custom_llm_provider``
    # ("anthropic", matching the prefix)
    "anthropic/": ("reasoning_effort", "thinking"),  # llms/anthropic/chat/transformation.py:454-464
}

# Provider prefix -> reasoning knobs that provider gates on
# ``litellm.supports_reasoning`` **on a key the entry's own ``model_info`` can
# never land on**. Half the criterion above holds and the other half does not,
# which is why these cannot sit in the map above: demanding ``model_info`` here
# would certify a config that still drops the knob in silence.
#
# ``vertex_ai`` and ``github_copilot`` call the gate with no
# ``custom_llm_provider``, so ``_get_potential_model_names`` takes its
# ``custom_llm_provider is None`` branch (``litellm/utils.py:5554-5565``) and
# every candidate key is derived from the bare model string — no candidate can
# ever equal the ``vertex_ai/…`` / ``github_copilot/…`` key the router
# registered. ``bedrock`` fails the same way one step further along: the
# converse config's ``custom_llm_provider`` property returns
# ``"bedrock_converse"`` (``llms/bedrock/chat/converse_transformation.py:124-126``)
# while the operator must write the ``bedrock/`` prefix — ``bedrock_converse``
# is not in ``LlmProviders``, so there is no spelling that makes the write key
# and the read key meet.
#
# ``model_info`` cannot RESCUE these entries. That is not the same claim as "the
# knob can never reach the wire here", and conflating the two is how this guard
# would fail a build on a config that already works. Two ways the knob still
# gets advertised on these prefixes, both of which the predicate must let
# through:
#
#   1. The provider config's own model-NAME heuristics fire before the gate is
#      consulted at all — ``bedrock`` converse's ``gpt-oss`` / Nova-2 / Claude-4
#      / ``deepseek.r1`` branches. Those are pure substring tests on a name the
#      operator writes, so the guard can evaluate them exactly:
#      ``_HEURISTIC_ADVERTISED_KNOBS`` below. (``github_copilot``'s ``"claude"
#      in model`` is NOT one of these — it is ANDed with the unreachable gate,
#      not ORed with it, so a Claude name alone never suffices there.)
#   2. The bare, provider-stripped model name happens to be a top-level key in
#      LiteLLM's built-in map — e.g. ``gemini-2.5-pro`` is, with
#      ``supports_reasoning: true``, so ``model: vertex_ai/gemini-2.5-pro`` puts
#      the knob on the wire with no ``model_info`` and no config at all. That is
#      a fact about LiteLLM's map at one version, not about the entry, so it
#      cannot be derived here — an operator who has SEEN the knob on the wire
#      records it with ``model_info.egg_wire_verified_knobs`` (below).
#
# Absent either, the guard fails closed: no YAML key makes the gate pass, so it
# asks for the provider's native request shape or for empirical verification
# against the ``request_params`` log line rather than pretending ``model_info``
# will do it.
#
# Verified at ``_VERIFIED_LITELLM_VERSION``; line numbers are from that tag.
_GATE_UNREACHABLE_BY_MODEL_INFO: dict[str, tuple[str, ...]] = {
    # llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py:328-330
    # — ``supports_reasoning(model)``, no provider
    "vertex_ai/": ("reasoning_effort", "thinking"),
    # llms/github_copilot/chat/transformation.py:122-129 — ``"claude" in model``
    # AND ``supports_reasoning(model=model.lower())``, no provider. Both
    # conjuncts must hold, so the Claude name is not an escape from the gate:
    # it needs the bare stripped name (``claude-sonnet-4``, not
    # ``github_copilot/claude-sonnet-4``) to be in LiteLLM's map.
    "github_copilot/": ("reasoning_effort", "thinking"),
    # llms/bedrock/chat/converse_transformation.py:555-575 — provider passed but
    # spelled ``bedrock_converse``. Note also that the ``gpt-oss`` and Nova-2
    # branches short-circuit the ``elif`` chain and append ``reasoning_effort``
    # ONLY, so on those name families ``thinking`` is never advertised at all.
    "bedrock/": ("reasoning_effort", "thinking"),
}

# ``model_info`` key an operator uses to record that they have SEEN a knob on
# the wire for this route, mapping knob -> the litellm version it was observed
# at. It is the green path out of ``_GATE_UNREACHABLE_BY_MODEL_INFO`` for the
# case no static rule can express: the bare model name happening to be in
# LiteLLM's built-in map (``vertex_ai/gemini-2.5-pro`` today). Empirical proof
# beats source reading, but it expires — the map moves on a bump, which is why
# the value is a version and why the guard only honours it at the pinned one.
#
# Not accepted by the other two guards on purpose. On a gated-but-reachable
# provider ``model_info: {supports_reasoning: true}`` is the actual fix and an
# attestation would just let an entry skip it; on a provider in no table at all
# the required work is reading that config, and an attestation there would be a
# way to never do it.
#
# ``ModelInfo`` is ``ConfigDict(extra="allow")`` (``litellm/types/router.py``),
# so LiteLLM carries the key through router init and ignores it.
_WIRE_VERIFIED_KNOBS_KEY = "egg_wire_verified_knobs"

# Provider prefix -> a predicate mapping a written model string to the knobs
# that provider advertises on the NAME ALONE, with no ``supports_reasoning``
# call involved.
#
# Only ``bedrock`` (converse) has these. ``converse_transformation.py:555-575``
# is an ``if``/``elif`` chain whose first two branches short-circuit before any
# gate, and whose third ORs three Claude-name tests and ``deepseek.r1`` in front
# of the gate. Note the knob asymmetry the chain creates: on a ``gpt-oss`` or
# Nova-2 name ``reasoning_effort`` is advertised and ``thinking`` is NOT — and
# never can be, whatever the config says, because the matching branch returns
# before the branch that appends it.
#
# Verified at ``_VERIFIED_LITELLM_VERSION``.
_BEDROCK_BOTH_KNOB_NAMES = ("claude-3-7", "claude-sonnet-4", "claude-opus-4", "deepseek.r1")


def _is_nova_2_model(model: str) -> bool:
    """Mirror of ``AmazonConverseConfig._is_nova_2_model``.

    Strips the routing prefix then the regional prefix, exactly as
    ``llms/bedrock/chat/converse_transformation.py:280-331`` does, before
    matching the Nova 2 families."""
    for routing_prefix in ("bedrock/converse/", "bedrock/", "converse/"):
        if model.startswith(routing_prefix):
            model = model.removeprefix(routing_prefix)
            break
    for region_prefix in ("us.", "eu.", "apac."):
        if model.startswith(region_prefix):
            model = model.removeprefix(region_prefix)
            break
    return model.startswith("amazon.nova-2-") or model.startswith("nova-2/")


def _bedrock_heuristic_knobs(model: str) -> tuple[str, ...]:
    """Knobs ``AmazonConverseConfig`` advertises for *model* without any gate."""
    if "gpt-oss" in model:
        return ("reasoning_effort",)
    if _is_nova_2_model(model):
        return ("reasoning_effort",)
    if any(name in model for name in _BEDROCK_BOTH_KNOB_NAMES):
        return ("reasoning_effort", "thinking")
    return ()


_HEURISTIC_ADVERTISED_KNOBS = {"bedrock/": _bedrock_heuristic_knobs}

# Provider prefix -> reasoning knobs that provider advertises UNCONDITIONALLY,
# i.e. with no ``model_info`` needed and no model-map lookup involved.
#
# EMPTY BY DESIGN: no such provider has been verified yet. Azure's o-series
# config is the closest candidate (it advertises ``reasoning_effort`` for any
# deployment name absent from LiteLLM's map) but is *conditionally* fail-open —
# a mapped, non-reasoning deployment name is still gated — so it is deliberately
# left out rather than half-recorded here. This is not the same category as the
# two maps above — here ``model_info`` is unnecessary; in the first it is
# required, in the second it is unreachable.
#
# A provider in none of the three tables is a provider nobody has read yet, NOT
# a provider that is known broken — the tables are a record of what has been
# checked, not a closed classification of every provider that exists.
# ``together_ai`` is the one such config that HAS been read and does drop the
# knob unconditionally: ``TogetherAIConfig.get_supported_openai_params``
# (``llms/together_ai/chat.py:18-46``) only *subtracts* from
# ``OpenAIGPTConfig``'s base list, and that base list
# (``llms/openai/chat/gpt_transformation.py:138-187``) carries no reasoning knob
# under any condition. That is a fact about those two configs, not about the
# complement. Add a prefix here only after reading that provider's
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
    """The provider-qualified model key request-time lookup resolves to.

    ``custom_llm_provider`` is an equally valid way to name the provider:
    ``model: qwen/qwen3-max`` + ``custom_llm_provider: openrouter`` routes
    exactly like ``model: openrouter/qwen/qwen3-max``. Normalise both spellings
    to the prefixed form so provider lookup sees the same string either way —
    otherwise the second shape falls through to the unverified-provider bucket
    and gets advice written for a different provider.

    This mirrors the *read* side: ``get_llm_provider`` strips a provider prefix
    the model already carries, and ``_get_potential_model_names`` then re-adds
    the provider exactly once (``litellm/utils.py:5551-5586``). The *write* side
    differs — see ``_registered_model_key``."""
    params = entry.get("litellm_params") or {}
    model = str(params.get("model") or "")
    provider = str(params.get("custom_llm_provider") or "")
    if provider and not model.startswith(f"{provider}/"):
        return f"{provider}/{model}"
    return model


def _registered_model_key(entry: dict) -> str:
    """The model-cost-map key LiteLLM files this entry's ``model_info`` under.

    ``Router._create_deployment`` prepends ``custom_llm_provider`` to
    ``litellm_params.model`` **unconditionally** when that field is set
    (``litellm/router.py:7196-7200``, mirrored at ``:7904-7908``); it does not
    check whether the model string already carries the prefix. So this is *not*
    always the same key ``_entry_model`` resolves — see
    ``_model_info_reaches_the_gate``."""
    params = entry.get("litellm_params") or {}
    model = str(params.get("model") or "")
    provider = str(params.get("custom_llm_provider") or "")
    return f"{provider}/{model}" if provider else model


def _model_info_reaches_the_gate(entry: dict) -> bool:
    """Whether this entry's ``model_info`` lands on the key the gate reads back.

    Spelling the provider twice — ``model: openrouter/qwen/qwen3-max`` *plus*
    ``custom_llm_provider: openrouter`` — registers ``model_info`` under
    ``openrouter/openrouter/qwen/qwen3-max`` (unconditional prepend) while the
    gate looks up ``openrouter/qwen/qwen3-max`` (prefix stripped, then re-added
    once). The flag is written and read at different keys, so
    ``supports_reasoning`` still answers False and the knob is still dropped in
    silence — with the config now *looking* correct. Spell the provider once.

    This models the *entry*-shaped way the two keys diverge. The other way is
    provider-shaped — a gate that passes no ``custom_llm_provider`` at all, or
    passes a different one than the operator can write — and no entry can fix
    that, which is why it lives in ``_GATE_UNREACHABLE_BY_MODEL_INFO`` rather
    than in this predicate."""
    return _registered_model_key(entry) == _entry_model(entry)


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
    because the gate is not OpenRouter-specific — a range of other provider
    configs consult the same call, and for every prefix in that table
    ``model_info`` is what satisfies it. Knobs the provider does not gate are
    not this predicate's business (``_unverified_provider_reasoning_knob`` has
    them), and neither are providers whose gate ``model_info`` cannot reach
    (``_gate_unreachable_reasoning_knob``) — demanding the flag there would
    certify a config that still drops the knob.

    ``model_info`` only counts when it lands on the key the gate reads
    (``_model_info_reaches_the_gate``): a double-spelled provider registers it
    somewhere nothing looks, which is a silent no-op wearing the fix's clothes."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if set(_reasoning_knobs_set(entry))
        & set(_knobs_for_provider(_entry_model(entry), _SUPPORTS_REASONING_GATED_KNOBS))
        and not (
            (entry.get("model_info") or {}).get("supports_reasoning")
            and _model_info_reaches_the_gate(entry)
        )
    )


def _heuristic_knobs(model: str) -> tuple[str, ...]:
    """Knobs the provider prefixing *model* advertises on the name alone."""
    for prefix, predicate in _HEURISTIC_ADVERTISED_KNOBS.items():
        if model.startswith(prefix):
            return predicate(model)
    return ()


def _wire_verified_knobs(entry: dict) -> tuple[str, ...]:
    """Knobs this entry attests were seen on the wire at the pinned litellm.

    An attestation naming a different version is ignored — a knob observed at
    1.85 says nothing about 1.87, since what carries it there is LiteLLM's
    built-in map rather than anything in this file. That makes the version
    guard's re-read apply to operator attestations too."""
    attested = (entry.get("model_info") or {}).get(_WIRE_VERIFIED_KNOBS_KEY) or {}
    if not isinstance(attested, dict):
        return ()
    return tuple(
        knob
        for knob, version in attested.items()
        if knob in _REASONING_KNOBS and str(version) == _VERIFIED_LITELLM_VERSION
    )


def _gate_unreachable_reasoning_knob(entries: list[dict]) -> list[str]:
    """Names of entries whose reasoning knob is gated on
    ``litellm.supports_reasoning`` at a key the entry's ``model_info`` can never
    reach — see ``_GATE_UNREACHABLE_BY_MODEL_INFO``.

    Separate from ``_reasoning_knob_offenders`` because the remedy differs and
    getting that wrong is how this guard has previously certified the defect it
    exists to catch: there ``model_info`` is the fix, here it is a placebo that
    *looks* like the fix. ``model_info.supports_reasoning`` is deliberately
    ignored for that reason.

    But "``model_info`` cannot rescue this" is not "this cannot work", and a
    guard that fails a build with no way to go green is its own defect. Two
    exits stay open, both narrow and both checkable:

    * the provider advertises the knob on the model NAME, before consulting the
      gate (``_HEURISTIC_ADVERTISED_KNOBS`` — bedrock's ``gpt-oss`` / Nova-2 /
      Claude-4 / ``deepseek.r1`` branches), evaluated here rather than assumed;
    * the operator has seen the knob on the wire and recorded it at the pinned
      litellm version (``_WIRE_VERIFIED_KNOBS_KEY``), which is the only way to
      express "the bare name is in LiteLLM's map" without importing litellm."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if (
            set(_reasoning_knobs_set(entry))
            & set(_knobs_for_provider(_entry_model(entry), _GATE_UNREACHABLE_BY_MODEL_INFO))
        )
        - set(_heuristic_knobs(_entry_model(entry)))
        - set(_wire_verified_knobs(entry))
    )


def _unverified_provider_reasoning_knob(entries: list[dict]) -> list[str]:
    """Names of entries setting a reasoning knob their provider is not known to
    advertise at all — in none of the three tables above.

    Deliberately ignores ``model_info``: these providers never consult
    ``litellm.supports_reasoning`` for this knob, so accepting the flag here
    would let the guard certify the exact defect it exists to catch."""
    return sorted(
        entry["model_name"]
        for entry in entries
        if set(_reasoning_knobs_set(entry))
        - set(_knobs_for_provider(_entry_model(entry), _SUPPORTS_REASONING_GATED_KNOBS))
        - set(_knobs_for_provider(_entry_model(entry), _GATE_UNREACHABLE_BY_MODEL_INFO))
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


class TestReasoningKnobIsNotASilentNoop:
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

    The gate is **not** OpenRouter-specific. A range of other provider configs
    consult the same ``litellm.supports_reasoning`` call when deciding whether
    to advertise a reasoning knob — ``gemini`` with exactly OpenRouter's
    fail-closed shape, ``groq`` / ``xai`` / ``deepinfra`` / ``cerebras`` /
    ``fireworks_ai`` / ``perplexity`` / ``bedrock_mantle`` on
    ``reasoning_effort``, ``zai`` / ``minimax`` on ``thinking``, ``anthropic``
    behind a name heuristic — and on those ``model_info`` satisfies it, because
    ``Router._create_deployment`` registers ``model_info`` into
    ``litellm.model_cost`` under a key built from ``litellm_params``, nothing
    provider-specific. ``_SUPPORTS_REASONING_GATED_KNOBS`` records which knob
    each of them gates and which shape it is.

    Consulting the gate is necessary but not sufficient for membership: the gate
    call must also pass a ``custom_llm_provider`` the operator can spell, or the
    flag is registered under a key the lookup never forms.
    ``vertex_ai`` / ``github_copilot`` / ``bedrock`` fail that second half and
    get ``TestReasoningKnobUnreachableByModelInfo`` below; providers in no table
    at all get ``TestReasoningKnobOnUnverifiedProvider``. In both of those
    ``model_info`` is a placebo, for different reasons."""

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

    @pytest.mark.parametrize(
        ("prefix", "knob"),
        [
            (prefix, knob)
            for prefix, knobs in sorted(_SUPPORTS_REASONING_GATED_KNOBS.items())
            for knob in knobs
        ],
    )
    def test_every_gated_row_demands_model_info_and_accepts_it(self, prefix, knob):
        # One case per (prefix, knob) pair in the table, so a row added without
        # thinking cannot ride in untested — and the per-knob splits (`zai` and
        # `minimax` gate `thinking` only) are exercised rather than assumed.
        # `model_info` is the fix on every row in THIS table: the registration
        # path (`Router._create_deployment` -> `litellm.model_cost`) is not
        # provider-specific, and each of these gates passes a
        # `custom_llm_provider` matching the prefix, so the key it registers
        # under is the key the gate reads back. Rows failing that second half
        # live in `_GATE_UNREACHABLE_BY_MODEL_INFO` and must not appear here.
        bare = {
            "model_name": "m",
            "litellm_params": {"model": f"{prefix}vendor/model", knob: "high"},
        }
        assert _reasoning_knob_offenders([bare]) == ["m"]
        assert _unverified_provider_reasoning_knob([bare]) == []
        assert _gate_unreachable_reasoning_knob([bare]) == []
        flagged = {**bare, "model_info": {"supports_reasoning": True}}
        assert _reasoning_knob_offenders([flagged]) == []
        assert _unverified_provider_reasoning_knob([flagged]) == []
        assert _gate_unreachable_reasoning_knob([flagged]) == []

    @pytest.mark.parametrize(
        ("prefix", "knob"),
        [
            (prefix, knob)
            for prefix, knobs in sorted(_SUPPORTS_REASONING_GATED_KNOBS.items())
            for knob in _REASONING_KNOBS
            if knob not in knobs
        ],
    )
    def test_knobs_a_gated_provider_does_not_advertise_get_the_other_guard(self, prefix, knob):
        # The complement of the case above: a knob this provider never
        # advertises is not fixable with `model_info`, so it must land in the
        # unverified bucket instead of being told to set a placebo.
        entry = {
            "model_name": "m",
            "litellm_params": {"model": f"{prefix}vendor/model", knob: "high"},
            "model_info": {"supports_reasoning": True},
        }
        assert _reasoning_knob_offenders([entry]) == []
        assert _unverified_provider_reasoning_knob([entry]) == ["m"]

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

    def test_predicate_flags_a_double_spelled_provider(self):
        # `model:` already prefixed AND `custom_llm_provider:` set is the one
        # shape where `model_info` is present and still does nothing: litellm
        # registers it under `openrouter/openrouter/...` (unconditional prepend)
        # while the gate reads `openrouter/...`. Green here would certify a
        # config that looks fixed and is not.
        entry = {
            "model_name": "qwen3-max",
            "litellm_params": {
                "model": "openrouter/qwen/qwen3-max",
                "custom_llm_provider": "openrouter",
                "reasoning_effort": "high",
            },
            "model_info": {"supports_reasoning": True},
        }
        assert _reasoning_knob_offenders([entry]) == ["qwen3-max"]
        # Dropping the redundant provider spelling is the fix.
        deduped = {
            **entry,
            "litellm_params": {
                k: v for k, v in entry["litellm_params"].items() if k != "custom_llm_provider"
            },
        }
        assert _reasoning_knob_offenders([deduped]) == []

    def test_reasoning_knob_requires_supports_reasoning(self):
        offenders = _reasoning_knob_offenders(_load_model_list())
        assert not offenders, (
            "model_list entries setting `litellm_params.reasoning_effort` (or "
            "`thinking`) on a provider that gates it on "
            f"`litellm.supports_reasoning`, without effective `model_info`: "
            f"{offenders} — LiteLLM drops the parameter silently for any model "
            "missing from its built-in model-cost map, so as written this is a "
            "no-op and the agent runs at the provider's default depth. Add "
            "`model_info: {supports_reasoning: true}` to the entry (it is "
            "registered into the model-cost map under a key built from "
            "`litellm_params`, and every provider in "
            "`_SUPPORTS_REASONING_GATED_KNOBS` passes a matching "
            "`custom_llm_provider` to the gate, so the flag lands on the key "
            "the gate reads back), or on `openrouter/*` use the "
            "native `extra_body.reasoning.effort` instead. Pin the flag even "
            "for a model currently in the map: map membership changes under you "
            "on a litellm bump, the config does not. If the entry already "
            "carries the flag, check you have not spelled the provider twice — "
            "a prefixed `model:` plus `custom_llm_provider:` registers "
            "`model_info` under a doubled prefix that nothing reads back."
        )


class TestReasoningKnobUnreachableByModelInfo:
    """Where a provider gates the knob on ``litellm.supports_reasoning`` but at
    a key the entry's own ``model_info`` cannot land on, the flag is a placebo
    wearing the fix's clothes — so this needs its own guard and its own advice.

    The write key is the model string as written
    (``Router._create_deployment``, ``litellm/router.py:7196-7200``). The read
    keys are built by ``_get_potential_model_names``
    (``litellm/utils.py:5551-5586``) from the *stripped* model plus whatever
    ``custom_llm_provider`` the gate passed — and when the gate passes none,
    that function's first branch prefixes nothing, so no candidate can ever
    equal a ``vertex_ai/…``-shaped key. ``bedrock`` (converse) passes one, but
    spelled ``bedrock_converse`` while the operator must write ``bedrock/``.

    Telling those operators to add ``model_info.supports_reasoning`` would be
    this guard certifying the exact defect it exists to catch, one taxonomy
    branch over — so the predicate ignores that flag entirely.

    It does NOT ignore the two ways such an entry can genuinely work, because a
    guard that fails a working config with no path to green is the mirror-image
    defect: it costs the operator the knob. Bedrock's name heuristics are
    evaluated (``_HEURISTIC_ADVERTISED_KNOBS``), and an operator who has watched
    the knob appear in the post-mapping ``request_params`` line can record that
    at the pinned litellm version (``_WIRE_VERIFIED_KNOBS_KEY``). Neither exit
    is a free-form opt-out: the first is derived from source, the second names a
    version and expires when the image moves."""

    @pytest.mark.parametrize(
        ("prefix", "knob"),
        [
            (prefix, knob)
            for prefix, knobs in sorted(_GATE_UNREACHABLE_BY_MODEL_INFO.items())
            for knob in knobs
        ],
    )
    def test_model_info_does_not_clear_the_flag(self, prefix, knob):
        # One case per (prefix, knob) pair, so a row cannot be added to the
        # table without the "model_info doesn't rescue it" property being
        # exercised — that property is the whole reason the table is separate.
        entry = {
            "model_name": "m",
            "litellm_params": {"model": f"{prefix}vendor/model", knob: "high"},
        }
        assert _gate_unreachable_reasoning_knob([entry]) == ["m"]
        flagged = {**entry, "model_info": {"supports_reasoning": True}}
        assert _gate_unreachable_reasoning_knob([flagged]) == ["m"]
        # And it must not be double-reported by the other two guards, whose
        # advice would be wrong for it.
        assert _reasoning_knob_offenders([flagged]) == []
        assert _unverified_provider_reasoning_knob([flagged]) == []

    def test_predicate_ignores_entries_without_a_reasoning_knob(self):
        assert (
            _gate_unreachable_reasoning_knob(
                [{"model_name": "plain", "litellm_params": {"model": "vertex_ai/gemini-3-pro"}}]
            )
            == []
        )

    def test_predicate_ignores_reachable_gated_providers(self):
        # `gemini/` passes `custom_llm_provider="gemini"` to the gate and
        # `vertex_ai/` passes nothing — same Google model family, opposite
        # answers. Pin the pair so a future edit cannot merge the two rows.
        gemini = {
            "model_name": "g",
            "litellm_params": {"model": "gemini/gemini-3-pro", "reasoning_effort": "high"},
            "model_info": {"supports_reasoning": True},
        }
        vertex = {
            "model_name": "v",
            "litellm_params": {"model": "vertex_ai/gemini-3-pro", "reasoning_effort": "high"},
            "model_info": {"supports_reasoning": True},
        }
        assert _gate_unreachable_reasoning_knob([gemini]) == []
        assert _reasoning_knob_offenders([gemini]) == []
        assert _gate_unreachable_reasoning_knob([vertex]) == ["v"]

    @pytest.mark.parametrize(
        ("model", "advertised"),
        [
            # `if "gpt-oss" in model` — first branch, no gate, one knob.
            ("bedrock/openai.gpt-oss-120b", ("reasoning_effort",)),
            # `elif self._is_nova_2_model(model)` — same, through the regional
            # and routing prefix stripping the real helper does.
            ("bedrock/amazon.nova-2-lite-v1:0", ("reasoning_effort",)),
            ("bedrock/us.amazon.nova-2-pro-preview-20251202-v1:0", ("reasoning_effort",)),
            # `elif "claude-sonnet-4" in model or ... or supports_reasoning(...)`
            # — the gate is the last disjunct, so the name alone carries both.
            ("bedrock/anthropic.claude-sonnet-4-20250514-v1:0", ("reasoning_effort", "thinking")),
            ("bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0", ("reasoning_effort", "thinking")),
            ("bedrock/deepseek.r1-v1:0", ("reasoning_effort", "thinking")),
            # Nova 1 is not Nova 2, and a plain Claude 3.5 name matches nothing.
            ("bedrock/amazon.nova-pro-v1:0", ()),
            ("bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0", ()),
        ],
    )
    def test_name_heuristics_are_evaluated_not_assumed(self, model, advertised):
        # Failing the build on a config that already works costs the operator
        # the knob just as surely as certifying a broken one — and bedrock's
        # heuristics are pure substring tests on a name written right here, so
        # there is no excuse for guessing at them.
        for knob in _REASONING_KNOBS:
            entry = {"model_name": "m", "litellm_params": {"model": model, knob: "high"}}
            expected = [] if knob in advertised else ["m"]
            assert _gate_unreachable_reasoning_knob([entry]) == expected
            # Whichever way it lands, the other two guards stay out of it.
            assert _reasoning_knob_offenders([entry]) == []
            assert _unverified_provider_reasoning_knob([entry]) == []

    def test_wire_verified_knob_is_a_green_path(self):
        # `gemini-2.5-pro` is a top-level key in LiteLLM's built-in map at the
        # pinned version, so vertex's bare `supports_reasoning(model)` resolves
        # it and the knob reaches the wire with no `model_info` at all. No
        # static rule here can know that — the map is not importable from this
        # process and moves on a bump — so an operator who has seen it on the
        # `request_params` line records the observation instead.
        entry = {
            "model_name": "gemini-2-5-pro",
            "litellm_params": {"model": "vertex_ai/gemini-2.5-pro", "reasoning_effort": "high"},
        }
        assert _gate_unreachable_reasoning_knob([entry]) == ["gemini-2-5-pro"]
        verified = {
            **entry,
            "model_info": {
                _WIRE_VERIFIED_KNOBS_KEY: {"reasoning_effort": _VERIFIED_LITELLM_VERSION}
            },
        }
        assert _gate_unreachable_reasoning_knob([verified]) == []

    def test_wire_verification_is_per_knob_and_expires_with_the_pin(self):
        base = {
            "model_name": "v",
            "litellm_params": {
                "model": "vertex_ai/gemini-2.5-pro",
                "reasoning_effort": "high",
                "thinking": {"type": "enabled"},
            },
        }
        # Attesting one knob says nothing about the other.
        one_knob = {
            **base,
            "model_info": {
                _WIRE_VERIFIED_KNOBS_KEY: {"reasoning_effort": _VERIFIED_LITELLM_VERSION}
            },
        }
        assert _gate_unreachable_reasoning_knob([one_knob]) == ["v"]
        # An observation made against a different litellm is not evidence about
        # this one: what carries the knob is LiteLLM's map, which the bump moves.
        stale = {**base, "model_info": {_WIRE_VERIFIED_KNOBS_KEY: {"reasoning_effort": "1.80.0"}}}
        assert _gate_unreachable_reasoning_knob([stale]) == ["v"]
        # A malformed attestation is ignored rather than trusted.
        malformed = {**base, "model_info": {_WIRE_VERIFIED_KNOBS_KEY: ["reasoning_effort"]}}
        assert _gate_unreachable_reasoning_knob([malformed]) == ["v"]

    def test_wire_verification_is_not_accepted_by_the_other_guards(self):
        # On a reachable gated provider the real fix exists, so an attestation
        # must not be a way around setting it; on an unread provider the
        # required work is reading the config. Honouring it in either place is
        # how a guard stops guarding.
        gated = {
            "model_name": "or",
            "litellm_params": {"model": "openrouter/x/y", "reasoning_effort": "high"},
            "model_info": {
                _WIRE_VERIFIED_KNOBS_KEY: {"reasoning_effort": _VERIFIED_LITELLM_VERSION}
            },
        }
        assert _reasoning_knob_offenders([gated]) == ["or"]
        unverified = {
            "model_name": "tog",
            "litellm_params": {"model": "together_ai/Qwen/Qwen2.5-7B", "reasoning_effort": "high"},
            "model_info": {
                _WIRE_VERIFIED_KNOBS_KEY: {"reasoning_effort": _VERIFIED_LITELLM_VERSION}
            },
        }
        assert _unverified_provider_reasoning_knob([unverified]) == ["tog"]

    def test_no_reasoning_knob_where_model_info_cannot_reach_the_gate(self):
        offenders = _gate_unreachable_reasoning_knob(_load_model_list())
        assert not offenders, (
            "model_list entries setting `litellm_params.reasoning_effort` (or "
            "`thinking`) on a provider that gates the knob on "
            f"`litellm.supports_reasoning` at a key `model_info` cannot reach: "
            f"{offenders} — do NOT add `model_info: {{supports_reasoning: "
            "true}}` here. LiteLLM registers it under the model string as "
            "written, while these gates look the model up with no "
            "`custom_llm_provider` (`vertex_ai`, `github_copilot`) or with a "
            "different one than you can write (`bedrock`, whose converse config "
            "reports `bedrock_converse`), so the flag is filed where nothing "
            "reads it. Three ways forward, in order of preference: (1) use the "
            "provider's native request shape, which does not go through the "
            "param mapper at all; (2) pick a model whose name the provider "
            "config recognises without the gate — on `bedrock` those are "
            "`gpt-oss` and Nova 2 (`reasoning_effort` only; `thinking` is never "
            "advertised on those names), and `claude-3-7` / `claude-sonnet-4` / "
            "`claude-opus-4` / `deepseek.r1` (both knobs), all of which this "
            "guard already accepts; (3) if you have SEEN the knob on the wire "
            "for this route — `cost_callback` logs post-mapping "
            "`request_params` per call, so grep the LiteLLM pod stream on real "
            f"traffic — record it as `model_info: {{{_WIRE_VERIFIED_KNOBS_KEY}: "
            f'{{<knob>: "{_VERIFIED_LITELLM_VERSION}"}}}}` on both paired '
            "rows. That is an observation, not a guess, and it expires when the "
            "image pin moves, since what carries the knob there is LiteLLM's "
            "built-in map rather than anything in this file. See "
            "`_GATE_UNREACHABLE_BY_MODEL_INFO` for the per-provider reason."
        )


class TestReasoningKnobOnUnverifiedProvider:
    """Where a provider does not advertise the knob at all,
    ``model_info.supports_reasoning`` is not the fix — it is not read.

    This is the complement of the two tables above, not "anything
    that isn't OpenRouter". ``TogetherAIConfig.get_supported_openai_params``
    (``llms/together_ai/chat.py:18-46``) only *subtracts* ``response_format`` /
    tool params from ``OpenAIGPTConfig``'s base list, and that base list carries
    no reasoning knob under any condition — nothing there ever calls
    ``litellm.supports_reasoning``. So on a ``together_ai/*`` route the
    parameter is popped silently whatever ``model_info`` says, which is why
    these entries need a separate guard rather than the gated-provider one
    waved over them.

    There is no config-shaped fix to assert here, so the guard demands a
    human-verified entry in one of the three tables instead: someone has to read
    that provider's ``get_supported_openai_params`` against the pinned litellm
    before the build goes green. The escape hatches are not interchangeable — a
    provider that consults ``litellm.supports_reasoning`` at all belongs in
    ``_SUPPORTS_REASONING_GATED_KNOBS`` (where ``model_info`` is then
    *required*) or, if its gate call cannot see a key ``model_info`` produces,
    in ``_GATE_UNREACHABLE_BY_MODEL_INFO``; listing it in
    ``_UNCONDITIONAL_REASONING_KNOBS`` instead would stop checking it entirely
    and let the genuine silent no-op through. When in doubt, the gated table is
    the fail-safe classification.

    Being in no table means "nobody has read this provider's config yet",
    not "this provider is broken" — the guard fails closed on ignorance."""

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
            "`thinking`) on a provider/knob pair no table in this file records: "
            f"{offenders} — nobody has read that provider's chat config yet, so "
            "the guard fails closed rather than guessing. Many "
            "OpenAI-compatible configs (together_ai among them) never advertise "
            "the knob under any condition, and there `drop_params: true` pops "
            "it silently and `model_info.supports_reasoning` cannot help "
            "because nothing on that path reads it — but that is a fact about "
            "those configs, not about every provider outside the tables. Read "
            "this one's `get_supported_openai_params` against the pinned "
            "litellm version, then: if it consults "
            "`litellm.supports_reasoning` for that knob (however it combines "
            "the call with name heuristics) AND passes a `custom_llm_provider` "
            "spelled like the prefix you write, add the prefix/knob to "
            "`_SUPPORTS_REASONING_GATED_KNOBS` and set "
            "`model_info: {supports_reasoning: true}` on the entry; if it "
            "consults the gate but with no `custom_llm_provider` or a "
            "different one, `model_info` cannot reach it — add the prefix/knob "
            "to `_GATE_UNREACHABLE_BY_MODEL_INFO` instead; if it "
            "advertises the knob unconditionally, add it to "
            "`_UNCONDITIONAL_REASONING_KNOBS`; note the version you checked "
            "either way. Only if it advertises the knob nowhere is dropping it "
            "the answer — there is then no config that makes it take effect."
        )


class TestProviderTablesTrackThePinnedLitellm:
    """The provider tables above are a reading of litellm source at one version.

    Which providers gate a reasoning knob, on which knob, whether the gate fails
    closed or open, and whether the call passes a ``custom_llm_provider`` are all
    version-specific facts, and providers get added between releases. So pin the
    reading to the image tag and fail the build when they diverge, rather than
    trusting a comment nobody re-reads on a bump.

    What this guard does NOT catch is a table that was incomplete when written —
    the ``gemini`` / ``vertex_ai`` omissions earlier in this PR's review happened
    *at* the pinned version, with no bump involved, and only a re-read of the
    source found them. This forces that re-read at the one moment it is
    guaranteed to be needed; it does not substitute for it."""

    def test_verified_version_matches_the_image_pin(self):
        # Tolerate the shapes a pin legitimately takes — a trailing
        # `AS <stage>`, and a `@sha256:` digest suffix — so a Dockerfile edit
        # that keeps the same version gets silence rather than a confusing
        # "version mismatch: 1.86.2@sha256:…" or a spurious "no FROM line".
        pinned = re.search(
            r"^FROM\s+ghcr\.io/berriai/litellm:v(?P<version>[^\s@]+)"
            r"(?:@sha256:[0-9a-f]+)?(?:\s+[Aa][Ss]\s+\S+)?\s*$",
            LITELLM_DOCKERFILE.read_text(),
            re.MULTILINE,
        )
        assert pinned, (
            f"no `FROM ghcr.io/berriai/litellm:v<version>` line in "
            f"{LITELLM_DOCKERFILE} — this test cannot tell whether the provider "
            "tables in tests/config/test_litellm_template.py are still current"
        )
        assert pinned.group("version") == _VERIFIED_LITELLM_VERSION, (
            f"config/litellm/Dockerfile now pins litellm "
            f"v{pinned.group('version')}, but the reasoning-knob provider "
            f"tables in this file were read against v{_VERIFIED_LITELLM_VERSION}"
            ". Re-read `get_supported_openai_params` for each prefix in "
            "`_SUPPORTS_REASONING_GATED_KNOBS` at the new tag (checking both the "
            "knobs advertised and whether the gate still fails closed), update "
            "the line references, then move `_VERIFIED_LITELLM_VERSION`."
        )

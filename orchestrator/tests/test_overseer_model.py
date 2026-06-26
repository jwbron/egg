"""Slice-2 (#2270 §1, folds #2813): overseer model tiering + deprecation shim.

Slice-2 routes the overseer's decision/adjudication model through the
per-agent resolver (``agent_model_resolution.resolve_overseer_model``)
with explicit tiering and deprecates the bespoke
``overseer_decision_maker_model`` field. The three tiers, per the
ratified contract (task-2-1):

    * ``classify``     -> Haiku   (cheap, high-volume classification)
    * ``routine``      -> Sonnet  (routine corrective decisions)
    * ``adversarial``  -> Opus    (high-stakes / adversarial adjudication)

The headline fix (#2270 §1): the overseer decision tier must no longer
default to **Sonnet** — Sonnet mis-classifies the overseer's own
legitimate bootstrap as a prompt-injection attack, refuses, exits, and
broadcasts ``[high]`` security-flavored alerts each respawn cycle. The
adversarial tier now runs on Opus (the fleet standard) via the normal
resolver, NOT the bespoke ``overseer_decision_maker_model`` field.

This file asserts the three acceptance facets of tester task-2-2:

    1. tiering          — the resolver returns the tiered model per tier;
    2. deprecation-shim — the deprecated field warns and still maps;
    3. #2813-bypass     — the spawn path no longer routes the overseer
                          model through ``classify_model(decision_model)``.

Convention: these tests are scaffolded by the tester *before* the coder
lands task-2-1 (the producers work the same slice in parallel). Each
gates on a slice-2 "landed" sentinel (``resolve_overseer_model``) and
``pytest.skip``s until it exists, so the slice stays green while the
implementation is in flight — mirroring the skip-guard pattern already
established in ``test_agent_model_resolution.py``.
"""

from __future__ import annotations

import logging
import re
import sys
import warnings
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_agent_model_resolution.py / test_overseer_spawn.py)
# ---------------------------------------------------------------------------
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))


# ---------------------------------------------------------------------------
# Slice-2 "landed" sentinel + interface accessors
# ---------------------------------------------------------------------------
# The whole of slice-2 (resolver tiering, the deprecation shim in
# models.py, and the kubernetes_spawner bypass removal) lands together as
# task-2-1. ``resolve_overseer_model`` is the sentinel: when it imports,
# the coder's slice-2 production code is present and every assertion below
# runs strictly; until then the suite skips so the slice stays green.


def _require_slice2_landed():
    """Return ``resolve_overseer_model`` or skip — the slice-2 sentinel."""
    try:
        from agent_model_resolution import (  # type: ignore[import-not-found]
            resolve_overseer_model,
        )

        return resolve_overseer_model
    except ImportError:
        pytest.skip(
            "agent_model_resolution.resolve_overseer_model not yet implemented "
            "(waiting on coder for slice-2 task-2-1)"
        )


def _tier_models() -> dict:
    """Return the ``OVERSEER_TIER_MODELS`` tier->default-model table or skip."""
    try:
        from agent_model_resolution import (  # type: ignore[import-not-found]
            OVERSEER_TIER_MODELS,
        )

        return OVERSEER_TIER_MODELS
    except ImportError:
        pytest.skip(
            "agent_model_resolution.OVERSEER_TIER_MODELS not yet implemented "
            "(waiting on coder for slice-2 task-2-1)"
        )


def _pipeline_config(**overrides):
    from models import PipelineConfig

    return PipelineConfig(**overrides)


def _spawner_source() -> str:
    """Source text of ``kubernetes_spawner.py`` (for the #2813 regression)."""
    return (_orchestrator_path / "kubernetes_spawner.py").read_text()


# A model name that is not any tier default and routes through the LiteLLM
# classifier, so a deprecated-field value that "maps" through is unambiguous
# (it can only be the operator's value, never a tier default).
_SENTINEL_MODEL = "qwen3-coder-30b"


# =============================================================================
# 1. Tiering — the resolver returns the tiered model per tier
# =============================================================================


class TestOverseerModelTiering:
    """``classify`` -> haiku, ``routine`` -> sonnet, ``adversarial`` -> opus.

    The resolver returns a full :class:`AgentModelDecision` so the spawn
    path gets the Claude-Code-facing alias + upstream the same way every
    other agent does — no bespoke field.
    """

    def test_tier_table_pins_the_three_documented_tiers(self):
        _require_slice2_landed()
        table = _tier_models()
        assert {"classify", "routine", "adversarial"} <= set(table), (
            "the overseer tier table must define the three documented tiers "
            f"(classify/routine/adversarial); got {sorted(table)}"
        )
        assert table["classify"] == "haiku"
        assert table["routine"] == "sonnet"
        # The §1 headline fix: the decision/adversarial tier is Opus, NOT Sonnet.
        assert table["adversarial"] == "opus", (
            "#2270 §1: the overseer adversarial/decision tier must run on Opus, "
            f"not {table['adversarial']!r}"
        )

    def test_resolver_returns_tier_default_for_each_tier(self):
        resolve_overseer_model = _require_slice2_landed()
        table = _tier_models()
        for tier, expected_model in table.items():
            decision = resolve_overseer_model(tier)
            # All three tier defaults are Claude aliases -> Anthropic upstream,
            # alias passed through verbatim, no LiteLLM upstream_model.
            assert decision.claude_code_alias == expected_model, (
                f"tier {tier!r} should resolve to {expected_model!r}, got "
                f"{decision.claude_code_alias!r}"
            )
            assert decision.upstream == "anthropic"
            assert decision.upstream_model is None

    def test_adversarial_tier_is_opus(self):
        """The single most important assertion of the slice: decision-tier Opus."""
        resolve_overseer_model = _require_slice2_landed()
        decision = resolve_overseer_model("adversarial")
        assert decision.claude_code_alias == "opus"
        assert decision.upstream == "anthropic"

    def test_classify_tier_stays_cheap_on_haiku(self):
        resolve_overseer_model = _require_slice2_landed()
        decision = resolve_overseer_model("classify")
        assert decision.claude_code_alias == "haiku"

    def test_routine_tier_on_sonnet(self):
        resolve_overseer_model = _require_slice2_landed()
        decision = resolve_overseer_model("routine")
        assert decision.claude_code_alias == "sonnet"

    def test_unknown_tier_is_rejected(self):
        """A typo'd tier must fail loudly, not silently fall back to a default."""
        resolve_overseer_model = _require_slice2_landed()
        with pytest.raises((ValueError, KeyError)):
            resolve_overseer_model("definitely-not-a-tier")


# =============================================================================
# 2. Deprecation shim — overseer_decision_maker_model warns + maps
# =============================================================================


class TestOverseerDecisionModelDeprecation:
    """``overseer_decision_maker_model`` is deprecated but still honored.

    Slice-2 keeps the field for back-compat (it goes fully inert later in
    slice-9), so setting it must (a) surface a deprecation signal and
    (b) still influence the resolved overseer model rather than being a
    silent no-op.
    """

    def test_setting_deprecated_field_surfaces_a_deprecation_signal(self, caplog):
        _require_slice2_landed()
        from models import PipelineConfig

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with caplog.at_level(logging.WARNING):
                cfg = PipelineConfig(overseer_decision_maker_model="opus")
                # Pydantic's ``Field(deprecated=...)`` warns on *access*; a
                # validator-based shim warns on *construction*. Touch both so
                # either implementation is observed.
                _ = cfg.overseer_decision_maker_model

        warned = any(issubclass(w.category, DeprecationWarning) for w in recorded)
        logged = any("deprecat" in r.getMessage().lower() for r in caplog.records)
        assert warned or logged, (
            "setting overseer_decision_maker_model must surface a deprecation "
            "signal (a DeprecationWarning or a logged deprecation notice)"
        )

    def test_deprecated_field_still_maps_to_the_resolved_model(self):
        """The deprecated value is not a silent no-op — it still maps through."""
        resolve_overseer_model = _require_slice2_landed()
        table = _tier_models()
        cfg = _pipeline_config(overseer_decision_maker_model=_SENTINEL_MODEL)

        # The mapping may land either on a resolved tier (via the resolver
        # reading the deprecated field / an injected agent_models override) or
        # be visible as an injected ``agent_models['overseer']`` entry. Accept
        # either, so the test pins behaviour ("the value still has effect")
        # without over-coupling to the shim's internal mechanism.
        resolved_names = []
        for tier in table:
            decision = resolve_overseer_model(tier, cfg)
            resolved_names.append(decision.upstream_model or decision.claude_code_alias)

        mapped_via_tier = any(
            name == _SENTINEL_MODEL or (name and name.startswith(_SENTINEL_MODEL))
            for name in resolved_names
        )
        agent_models = getattr(cfg, "agent_models", {}) or {}
        mapped_via_agent_models = agent_models.get("overseer") in (
            _SENTINEL_MODEL,
            f"{_SENTINEL_MODEL}[1m]",
        )
        assert mapped_via_tier or mapped_via_agent_models, (
            "a set (deprecated) overseer_decision_maker_model must still map "
            "into the resolved overseer model; it became a silent no-op "
            f"(resolved tiers: {resolved_names}, agent_models: {agent_models})"
        )

    def test_default_config_does_not_warn(self, caplog):
        """An unset deprecated field must stay silent — no noisy default warning."""
        _require_slice2_landed()
        from models import PipelineConfig

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with caplog.at_level(logging.WARNING):
                cfg = PipelineConfig()
                _ = cfg  # construction only; do not access the deprecated field

        assert not any(issubclass(w.category, DeprecationWarning) for w in recorded), (
            "constructing a default PipelineConfig must not emit a deprecation warning"
        )


# =============================================================================
# 3. #2813 — the spawn path no longer reads overseer_decision_maker_model
# =============================================================================


class TestSpawnBypassRemoved:
    """The overseer model is resolved via the per-agent resolver, not the
    ``classify_model(decision_model)`` bypass at ``kubernetes_spawner.py``.

    #2813: ``spawn_overseer_job`` handed ``decision_model`` straight to
    ``classify_model`` (and thence the command builder), never consulting
    the per-agent resolver. Slice-2 removes that bypass. (Slice-3 folds the
    whole spawn path into ``spawn_agent_job``; this is the slice-2 step.)
    """

    def test_classify_model_decision_model_bypass_is_gone(self):
        _require_slice2_landed()
        normalized = re.sub(r"\s+", "", _spawner_source())
        assert "classify_model(decision_model)" not in normalized, (
            "#2813: kubernetes_spawner must not route the overseer model "
            "through classify_model(decision_model); resolve it via the "
            "per-agent resolver instead."
        )

    def test_spawn_path_resolves_via_the_per_agent_resolver(self):
        _require_slice2_landed()
        src = _spawner_source()
        assert ("resolve_overseer_model" in src) or ("resolve_agent_model" in src), (
            "the overseer spawn path should resolve its model through the "
            "per-agent resolver (resolve_overseer_model / resolve_agent_model), "
            "matching how every other agent is spawned."
        )

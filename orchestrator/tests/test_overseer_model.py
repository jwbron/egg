"""Slice-2 (#2270 §1, folds #2813): overseer model tiering + deprecation shim.

Slice-2 routes the overseer's base/adjudication model through the
per-agent resolver (``agent_model_resolution.resolve_agent_model``) and
exposes the overseer's three decision tiers via
``resolve_overseer_models``, deprecating the bespoke
``overseer_decision_maker_model`` field. The three tiers, per the landed
implementation (coder task-2-1, ``e20a735be``):

    * ``classify``     -> Haiku   (fixed cheap default,
                                   :data:`OVERSEER_CLASSIFY_MODEL`)
    * ``routine``      -> Sonnet  (fixed default for routine corrective
                                   decisions, :data:`OVERSEER_ROUTINE_MODEL`)
    * ``adversarial``  -> Opus    (high-stakes / adversarial adjudication —
                                   the overseer agent's OWN resolved model
                                   via ``resolve_agent_model(OVERSEER)``,
                                   ``opus`` by default; the only
                                   operator-tunable tier)

The headline fix (#2270 §1): the overseer decision/adjudication tier must
no longer default to **Sonnet** — Sonnet mis-classifies the overseer's own
legitimate bootstrap as a prompt-injection attack, refuses, exits, and
broadcasts ``[high]`` security-flavored alerts each respawn cycle. The
adversarial tier now runs on Opus (the fleet standard) via the normal
resolver, NOT the bespoke ``overseer_decision_maker_model`` field.

This file asserts the three acceptance facets of tester task-2-2:

    1. tiering          — ``resolve_overseer_models`` returns the tiered
                          ``OverseerModelTiers`` (string aliases);
    2. deprecation-shim — the deprecated field is documented-deprecated,
                          stays silent by default, and the spawn path warns
                          (where the warning actually fires) when it is set
                          to a non-default value, while no longer driving
                          the model;
    3. #2813-bypass     — the spawn path no longer routes the overseer
                          model through ``classify_model(decision_model)``,
                          resolving via the per-agent resolver instead.

Convention: these tests are scaffolded by the tester alongside the coder
landing task-2-1 (the producers work the same slice in parallel). The
slice-2 "landed" sentinel is ``resolve_overseer_models`` — the symbol the
coder actually ships — so once the branches converge the assertions run
**strictly** against the merged code rather than skipping forever
(mirroring the skip-guard pattern in ``test_agent_model_resolution.py``).
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
# The whole of slice-2 (resolver tiering, the models.py deprecation, and the
# kubernetes_spawner bypass removal) lands together as task-2-1.
# ``resolve_overseer_models`` is the sentinel: it is the symbol the coder
# actually ships, so when it imports, slice-2 production code is present and
# every assertion below runs strictly; until then the suite skips so the
# slice stays green while the implementation is in flight.


def _require_slice2_landed():
    """Return ``resolve_overseer_models`` or skip — the slice-2 sentinel."""
    try:
        from agent_model_resolution import (  # type: ignore[import-not-found]
            resolve_overseer_models,
        )

        return resolve_overseer_models
    except ImportError:
        pytest.skip(
            "agent_model_resolution.resolve_overseer_models not yet implemented "
            "(waiting on coder for slice-2 task-2-1)"
        )


def _tier_constants() -> tuple[str, str]:
    """Return ``(OVERSEER_CLASSIFY_MODEL, OVERSEER_ROUTINE_MODEL)`` or skip."""
    try:
        from agent_model_resolution import (  # type: ignore[import-not-found]
            OVERSEER_CLASSIFY_MODEL,
            OVERSEER_ROUTINE_MODEL,
        )

        return OVERSEER_CLASSIFY_MODEL, OVERSEER_ROUTINE_MODEL
    except ImportError:
        pytest.skip(
            "agent_model_resolution overseer tier constants not yet implemented "
            "(waiting on coder for slice-2 task-2-1)"
        )


def _spawner_source() -> str:
    """Source text of ``kubernetes_spawner.py`` (for the #2813 regression)."""
    return (_orchestrator_path / "kubernetes_spawner.py").read_text()


# =============================================================================
# 1. Tiering — resolve_overseer_models returns the tiered OverseerModelTiers
# =============================================================================


class TestOverseerModelTiering:
    """``resolve_overseer_models`` -> ``OverseerModelTiers`` of string aliases.

    The classify/routine tiers are the fixed cheap defaults
    (:data:`OVERSEER_CLASSIFY_MODEL` / :data:`OVERSEER_ROUTINE_MODEL`); the
    adversarial tier is the overseer agent's OWN resolved model via
    ``resolve_agent_model(OVERSEER)`` — ``opus`` by default (the §1 fix).
    """

    def test_tier_constants_pin_cheap_defaults(self):
        _require_slice2_landed()
        classify, routine = _tier_constants()
        assert classify == "haiku", (
            f"the classify tier must stay cheap on Haiku; got {classify!r}"
        )
        assert routine == "sonnet", (
            f"the routine corrective tier must be Sonnet; got {routine!r}"
        )

    def test_resolver_returns_the_three_tiers(self):
        resolve_overseer_models = _require_slice2_landed()
        classify, routine = _tier_constants()
        tiers = resolve_overseer_models()
        # Tiers are plain string aliases on an OverseerModelTiers dataclass —
        # not per-tier AgentModelDecisions.
        assert tiers.classify == classify
        assert tiers.routine == routine
        # The §1 headline fix: the adversarial/decision tier is Opus, NOT Sonnet.
        assert tiers.adversarial == "opus", (
            "#2270 §1: the overseer adversarial/decision tier must default to "
            f"Opus, not {tiers.adversarial!r}"
        )

    def test_adversarial_tier_is_opus(self):
        """The single most important assertion of the slice: decision-tier Opus."""
        resolve_overseer_models = _require_slice2_landed()
        assert resolve_overseer_models().adversarial == "opus"

    def test_classify_tier_stays_cheap_on_haiku(self):
        resolve_overseer_models = _require_slice2_landed()
        assert resolve_overseer_models().classify == "haiku"

    def test_routine_tier_on_sonnet(self):
        resolve_overseer_models = _require_slice2_landed()
        assert resolve_overseer_models().routine == "sonnet"

    def test_adversarial_tier_honors_resolver_precedence(self):
        """Only the adversarial tier is operator-tunable — via the resolver.

        The classify/routine tiers are FIXED constants; the adversarial tier
        flows through ``resolve_agent_model(OVERSEER)``, so a repo-level
        default (the same precedence every other agent honors) overrides it
        while the cheap tiers stay pinned. This is the slice's "adversarial
        runs on the resolved model" guarantee, exercised through the
        reachable repo-default path (the spawn path passes
        ``pipeline_config=None``).
        """
        from unittest.mock import patch

        resolve_overseer_models = _require_slice2_landed()

        # Baseline: no repo context -> built-in opus for the adversarial tier.
        with patch(
            "config.repo_config.get_default_agent_model", return_value=None
        ):
            base = resolve_overseer_models(None, "any/repo")
        assert base.adversarial == "opus"

        # A repo-level default flows through the adversarial tier only.
        with patch(
            "config.repo_config.get_default_agent_model", return_value="sonnet"
        ):
            overridden = resolve_overseer_models(None, "any/repo")
        assert overridden.adversarial == "sonnet", (
            "the adversarial tier must honor resolve_agent_model precedence "
            "(repo-level default), not stay pinned to opus"
        )
        # The cheap tiers are fixed — a repo default must NOT move them.
        assert overridden.classify == "haiku"
        assert overridden.routine == "sonnet"


# =============================================================================
# 2. Deprecation shim — overseer_decision_maker_model is deprecated + inert
# =============================================================================


class TestOverseerDecisionModelDeprecation:
    """``overseer_decision_maker_model`` is deprecated and no longer drives spawn.

    Slice-2 keeps the field for back-compat (slice-9 makes it fully inert),
    so it must (a) be documented-deprecated, (b) stay silent for a default
    config, and (c) surface a deprecation warning *where it actually fires* —
    the spawn path (``kubernetes_spawner.spawn_overseer_job``), when an
    operator still sets a non-default value — while the model resolves via
    ``resolve_agent_model(OVERSEER)`` regardless.
    """

    def test_field_default_is_sonnet_and_documented_deprecated(self):
        _require_slice2_landed()
        from models import PipelineConfig

        field = PipelineConfig.model_fields["overseer_decision_maker_model"]
        assert field.default == "sonnet", (
            "the deprecated field's default must remain 'sonnet' so the spawn "
            "path's non-default guard stays silent for unchanged configs"
        )
        description = (field.description or "").lower()
        assert "deprecat" in description, (
            "overseer_decision_maker_model must be documented as deprecated "
            f"(#2270 §1); description was {field.description!r}"
        )

    def test_default_config_does_not_warn(self, caplog):
        """An unset deprecated field must stay silent — no noisy default warning."""
        _require_slice2_landed()
        from models import PipelineConfig

        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always")
            with caplog.at_level(logging.WARNING):
                cfg = PipelineConfig()
                _ = cfg.overseer_decision_maker_model

        assert not any(
            issubclass(w.category, DeprecationWarning) for w in recorded
        ), "constructing/reading a default PipelineConfig must not warn"

    def test_spawn_path_warns_on_non_default_and_ignores_it(self):
        """The deprecation warning fires in the spawn path on a non-default value.

        Aligned to where the warning actually fires (#2270 §1): the bespoke
        field no longer drives the spawn, so the spawn path warns that a
        non-default ``decision_model`` is inert and resolves the base model
        through ``resolve_agent_model(OVERSEER)`` instead. Asserted against
        the spawn source so the test is robust to the structlog logger used
        there (the warning is a structured log, not a ``DeprecationWarning``).
        """
        _require_slice2_landed()
        src = _spawner_source()
        normalized = re.sub(r"\s+", "", src)

        # The non-default guard must exist (default 'sonnet' stays silent).
        assert 'decision_model!="sonnet"' in normalized, (
            "the spawn path must guard the deprecation warning on a "
            "non-default decision_model (the default 'sonnet' stays silent)"
        )
        # ... and it must be a deprecation warning naming the field.
        assert "deprecated" in src.lower(), (
            "the spawn path must emit a deprecation notice when the bespoke "
            "overseer_decision_maker_model is set to a non-default value"
        )


# =============================================================================
# 3. #2813 — the spawn path no longer reads overseer_decision_maker_model
# =============================================================================


class TestSpawnBypassRemoved:
    """The overseer model is resolved via the per-agent resolver, not the
    ``classify_model(decision_model)`` bypass at ``kubernetes_spawner.py``.

    #2813: ``spawn_overseer_job`` handed ``decision_model`` straight to
    ``classify_model`` (and thence the command builder), never consulting
    the per-agent resolver. Slice-2 removes that bypass and drops the dead
    ``EGG_OVERSEER_DECISION_MODEL`` env. (Slice-3 folds the whole spawn path
    into ``spawn_agent_job``; this is the slice-2 step.)
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
        assert "resolve_agent_model" in _spawner_source(), (
            "the overseer spawn path should resolve its model through the "
            "per-agent resolver (resolve_agent_model), matching how every "
            "other agent is spawned."
        )

    def test_dead_decision_model_env_is_not_injected(self):
        """The bespoke ``EGG_OVERSEER_DECISION_MODEL`` env is no longer injected.

        The string may survive in an explanatory comment, so this asserts the
        env is not set as a dict key (``"EGG_OVERSEER_DECISION_MODEL":``) in
        the spawn-env mapping, rather than its mere absence from the source.
        """
        _require_slice2_landed()
        normalized = re.sub(r"\s+", "", _spawner_source())
        assert '"EGG_OVERSEER_DECISION_MODEL":' not in normalized, (
            "#2270 §1 / #2813: the dead EGG_OVERSEER_DECISION_MODEL env "
            "(derived from the deprecated field) must not be injected into "
            "the overseer spawn env."
        )

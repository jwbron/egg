"""Tests for ``orchestrator/agent_model_resolution.py`` — slice-2 of #2769.

The resolver decides, for a given agent role, which Claude-Code-facing
model alias to pass to ``build_consensus_wrapped_command``, which
upstream to register on the gateway session, and which upstream-side
model name (if any) to put in ``session.upstream_model``.

Precedence (highest wins):

    1. ``pipeline_config.agent_models.get(role.value)``  — per-pipeline
    2. ``get_default_agent_model(repo)``                 — per-repo
    3. ``"opus"`` built-in default

Classifier (model string → upstream):

    * ``"opus"``, ``"opus[1m]"``, ``"sonnet"``, ``"sonnet[1m]"``,
      ``"haiku"``, ``"claude-*"``  → ``upstream="anthropic"``,
      ``claude_code_alias=<the string>``, ``upstream_model=None``.
    * anything else → ``upstream="litellm"``,
      ``claude_code_alias="opus"`` (cq-5 mitigation: Claude Code keeps
      seeing a recognized alias so its compaction math stays sane),
      ``upstream_model=<the string>``.

Plan reference: ``.egg-state/drafts/2769-plan.md`` TASK-2-3 / TASK-2-7.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add orchestrator to sys.path the same way test_concurrent_executor.py does.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


def _resolver():
    """Return the slice-2 ``resolve_agent_model`` symbol or skip the test.

    Slice-2 has not landed yet from the coder's side when the tester
    scaffolds — keep the suite green until the symbol exists.
    """
    try:
        from agent_model_resolution import resolve_agent_model  # type: ignore[import-not-found]

        return resolve_agent_model
    except ImportError:
        pytest.skip(
            "agent_model_resolution.resolve_agent_model not yet implemented "
            "(waiting on coder for slice-2)"
        )


def _decision_cls():
    """Return the slice-2 ``AgentModelDecision`` symbol or skip."""
    try:
        from agent_model_resolution import AgentModelDecision  # type: ignore[import-not-found]

        return AgentModelDecision
    except ImportError:
        pytest.skip(
            "agent_model_resolution.AgentModelDecision not yet implemented "
            "(waiting on coder for slice-2)"
        )


def _agent_role():
    """Return the ``AgentRole`` enum (canonical source: ``egg_contracts.agent_roles``)."""
    from egg_contracts.agent_roles import AgentRole

    return AgentRole


def _pipeline_config(**overrides):
    """Build a ``PipelineConfig`` with the given overrides.

    Mirrors the helper pattern at ``test_concurrent_executor.py:20`` but
    keeps the slice-2 ``agent_models`` field exposed.
    """
    from models import PipelineConfig

    return PipelineConfig(**overrides)


# =============================================================================
# AgentModelDecision dataclass shape
# =============================================================================


class TestAgentModelDecisionShape:
    """The decision triple is the resolver's contract.

    Downstream callers (``concurrent_executor`` / ``routes.pipelines``)
    unpack it into ``--model`` (Claude-Code-facing) and the
    ``register_session(upstream=..., upstream_model=...)`` payload.
    """

    def test_decision_has_three_named_fields(self):
        decision_cls = _decision_cls()
        d = decision_cls(
            claude_code_alias="opus",
            upstream="anthropic",
            upstream_model=None,
        )
        assert d.claude_code_alias == "opus"
        assert d.upstream == "anthropic"
        assert d.upstream_model is None

    def test_decision_accepts_upstream_model_string(self):
        decision_cls = _decision_cls()
        d = decision_cls(
            claude_code_alias="opus",
            upstream="litellm",
            upstream_model="qwen3-coder-30b",
        )
        assert d.upstream_model == "qwen3-coder-30b"


# =============================================================================
# Precedence rules: pipeline > repo > built-in
# =============================================================================


class TestResolutionPrecedence:
    """Per-pipeline ``agent_models`` wins; then repo default; then ``opus``."""

    def test_builtin_default_is_opus_when_no_override(self):
        """No pipeline override, no repo default → ``opus`` (Anthropic)."""
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.claude_code_alias == "opus"
        assert d.upstream == "anthropic"
        assert d.upstream_model is None

    def test_repo_default_used_when_no_pipeline_override(self):
        """Pipeline empty, repo sets ``sonnet`` → ``sonnet`` (Anthropic)."""
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch(
            "config.repo_config.get_default_agent_model",
            return_value="sonnet",
        ):
            d = resolve_agent_model(AgentRole.CODER, config, "owner/repo")

        assert d.claude_code_alias == "sonnet"
        assert d.upstream == "anthropic"
        assert d.upstream_model is None

    def test_pipeline_override_beats_repo_default(self):
        """Pipeline ``refiner=qwen3-coder-30b`` wins even if repo says ``sonnet``."""
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"refiner": "qwen3-coder-30b"})

        with patch(
            "config.repo_config.get_default_agent_model",
            return_value="sonnet",
        ):
            d = resolve_agent_model(AgentRole.REFINER, config, "owner/repo")

        # Pipeline override wins:
        assert d.upstream == "litellm"
        assert d.upstream_model == "qwen3-coder-30b"
        assert d.claude_code_alias == "opus"  # cq-5 mitigation

    def test_pipeline_override_does_not_leak_to_other_roles(self):
        """``agent_models={"refiner": "qwen3-coder-30b"}`` MUST NOT change
        the coder's resolution — slice-2 is per-role, not per-pipeline-wide.
        """
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"refiner": "qwen3-coder-30b"})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            coder_decision = resolve_agent_model(AgentRole.CODER, config, None)

        # Coder is not overridden → built-in default.
        assert coder_decision.upstream == "anthropic"
        assert coder_decision.claude_code_alias == "opus"
        assert coder_decision.upstream_model is None

    def test_none_repo_skips_repo_lookup(self):
        """``repo is None`` MUST NOT raise — overseer / unsliced callers
        sometimes pass ``None`` here (no per-repo default to consult).
        """
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        # If the resolver consults the repo lookup it should be guarded;
        # the simplest correct implementation skips the call entirely when
        # ``repo is None``.  We patch defensively in either case.
        with patch(
            "config.repo_config.get_default_agent_model",
            return_value=None,
        ):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.claude_code_alias == "opus"
        assert d.upstream == "anthropic"

    def test_non_string_repo_default_raises_value_error(self):
        """A non-string ``default_agent_model`` in ``repositories.yaml``
        MUST raise a clear ``ValueError`` rather than degrade silently.

        Without the ``isinstance`` guard in ``get_default_agent_model`` a
        non-string (e.g. ``default_agent_model: 4``) reaches
        ``classify_model``, where the ``claude-*`` regex raises an opaque
        ``TypeError`` from its internals — which the spawn-path
        ``except Exception`` then swallows into the opus fallback with no
        actionable log line. The guard converts that into an explicit,
        operator-readable error naming the bad value.
        """
        from config.repo_config import get_default_agent_model

        with patch("config.repo_config.get_repo_setting", return_value=4):
            with pytest.raises(ValueError, match="must be a string"):
                get_default_agent_model("owner/repo")


# =============================================================================
# Classifier: Anthropic vs LiteLLM dispatch by model name
# =============================================================================


class TestAnthropicClassification:
    """Known Claude aliases → ``upstream="anthropic"``,
    ``claude_code_alias=<the string>``, ``upstream_model=None``.

    The Anthropic path preserves the original alias on ``--model`` so
    today's Claude routing is byte-identical to a default-config
    pipeline.
    """

    @pytest.mark.parametrize(
        "model",
        ["opus", "opus[1m]", "sonnet", "sonnet[1m]", "haiku"],
    )
    def test_short_claude_alias_is_anthropic(self, model):
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": model})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.upstream == "anthropic", f"alias {model!r} should be anthropic"
        assert d.claude_code_alias == model, (
            f"alias should pass through, got {d.claude_code_alias!r}"
        )
        assert d.upstream_model is None

    @pytest.mark.parametrize(
        "model",
        [
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-sonnet-4-5",
            "claude-haiku-4-5-20251001",
        ],
    )
    def test_claude_prefixed_full_name_is_anthropic(self, model):
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": model})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.upstream == "anthropic"
        assert d.claude_code_alias == model
        assert d.upstream_model is None


class TestLiteLLMClassification:
    """Anything that does not look like a Claude alias → LiteLLM.

    On the LiteLLM path the cq-5 mitigation pins
    ``claude_code_alias="opus"`` regardless of the upstream model
    name — Claude Code's compaction math is name-derived, so it
    must keep seeing a recognized alias.
    """

    @pytest.mark.parametrize(
        "model",
        [
            "qwen3-coder-30b",
            "qwen2.5-72b-instruct",
            "qwen-max",
            "llama-3-70b-instruct",
            "mistral-large-2",
            "deepseek-v3",
            "gpt-4o",
        ],
    )
    def test_non_claude_model_routes_to_litellm(self, model):
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": model})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.upstream == "litellm", f"{model!r} should route to litellm"
        # cq-5 mitigation: Claude Code MUST keep seeing a recognized alias.
        assert d.claude_code_alias == "opus", (
            f"LiteLLM-routed agents MUST present 'opus' to Claude Code so "
            f"compaction math stays sane (cq-5); got {d.claude_code_alias!r}"
        )
        assert d.upstream_model == model

    def test_litellm_alias_pin_is_opus_not_upstream_model(self):
        """Adversarial guard for the cq-5 mitigation: the
        Claude-Code-facing alias for a LiteLLM-routed agent is
        ALWAYS ``"opus"``, never the upstream model name — even
        when the upstream model is something like ``"opus-7b"``
        whose substring matches the Claude alias.
        """
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()

        for tricky_model in ("opus-7b", "claude-clone-12b", "claudia-9000"):
            config = _pipeline_config(agent_models={"coder": tricky_model})

            with patch("config.repo_config.get_default_agent_model", return_value=None):
                d = resolve_agent_model(AgentRole.CODER, config, None)

            # Either the classifier correctly routes to anthropic
            # (only if the name matches the documented exact aliases),
            # or it routes to litellm with ``claude_code_alias="opus"``.
            # What MUST NOT happen: routing to litellm with a non-opus
            # alias, because Claude Code would then receive an unknown
            # model name in its --model flag.
            if d.upstream == "litellm":
                assert d.claude_code_alias == "opus", (
                    f"litellm route MUST pin claude_code_alias='opus' for "
                    f"cq-5, got {d.claude_code_alias!r} for "
                    f"upstream_model={tricky_model!r}"
                )


# =============================================================================
# PipelineConfig.agent_models validation (TASK-2-1)
# =============================================================================


def _agent_models_field_exists() -> bool:
    """Return True if ``PipelineConfig`` exposes the slice-2
    ``agent_models`` field — slice-2 may not have landed yet.
    """
    from models import PipelineConfig

    return "agent_models" in PipelineConfig.model_fields


class TestAgentModelsValidation:
    """``PipelineConfig.agent_models`` keys MUST be valid ``AgentRole``
    values; values are free-form strings validated downstream.
    """

    def test_default_is_empty_dict(self):
        """No behavioral change for existing pipelines — slice-2
        regression guard.
        """
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")
        config = _pipeline_config()
        assert config.agent_models == {}, (
            f"Default agent_models MUST be an empty dict (regression "
            f"guard); got {config.agent_models!r}"
        )

    def test_known_role_constructs_successfully(self):
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")
        config = _pipeline_config(agent_models={"refiner": "qwen3-coder-30b"})
        assert config.agent_models == {"refiner": "qwen3-coder-30b"}

    def test_unknown_role_raises_validation_error(self):
        """Pydantic validator MUST reject unknown roles at construction
        time — names that drift from the canonical ``AgentRole`` enum
        would silently fail at spawn time otherwise.
        """
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as excinfo:
            _pipeline_config(agent_models={"bogus_role": "qwen3-coder-30b"})

        # Error message MUST name the offending role so operators can
        # debug from logs alone.
        assert "bogus_role" in str(excinfo.value), (
            f"ValidationError MUST cite the unknown role; got: {excinfo.value}"
        )

    def test_unhonored_real_role_raises_validation_error(self):
        """Roles that exist in ``AgentRole`` but are never threaded
        through ``resolve_agent_model`` (overseer, autofixer,
        conflict_resolver, inspector) MUST be rejected — accepting them
        would let a deliberate override silently no-op at spawn, which is
        exactly the silent-ignore trap the validator exists to prevent.
        """
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")
        from pydantic import ValidationError

        for unhonored in ("overseer", "autofixer", "conflict_resolver", "inspector"):
            with pytest.raises(ValidationError) as excinfo:
                _pipeline_config(agent_models={unhonored: "qwen3-coder-30b"})
            assert unhonored in str(excinfo.value), (
                f"ValidationError MUST cite the unhonored role {unhonored!r}; got: {excinfo.value}"
            )

    def test_multiple_known_roles_accepted(self):
        """Many roles can be overridden simultaneously.

        ``applier`` (the producer of the ``apply`` phase, threaded
        through ``resolve_agent_model`` via ``_PHASE_ROLES["apply"]``) is
        included deliberately: it is an honored role and must be accepted
        as a key, even though it is easy to mistake for an unhonored
        utility role.
        """
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")
        config = _pipeline_config(
            agent_models={
                "refiner": "qwen3-coder-30b",
                "coder": "claude-3-5-sonnet-20241022",
                "tester": "sonnet",
                "applier": "haiku",
            }
        )
        assert config.agent_models["refiner"] == "qwen3-coder-30b"
        assert config.agent_models["coder"] == "claude-3-5-sonnet-20241022"
        assert config.agent_models["tester"] == "sonnet"
        assert config.agent_models["applier"] == "haiku"

    def test_one_bad_role_in_a_mix_still_fails(self):
        """Mixed dict — one bad role kills the whole construction."""
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _pipeline_config(
                agent_models={
                    "refiner": "qwen3-coder-30b",
                    "not_a_real_role_xyz": "x",
                }
            )


# =============================================================================
# Regression: default config produces byte-identical Anthropic decision
# =============================================================================


class TestDualImportRepoConfig:
    """Regression for the prod-container import topology (slice-2 v2).

    `orchestrator/Dockerfile:66` flattens ``config/repo_config.py`` to
    ``/app/repo_config.py`` (no ``/app/config/`` package).  The resolver
    must therefore fall back to a top-level ``repo_config`` import when
    the ``config.`` package is unavailable.  This regression test asserts
    the fallback works — without it, every Anthropic-default spawn in
    production would have raised ``ModuleNotFoundError`` on the first
    pipeline submit.

    Mirrors the dual-import pattern already used at
    ``shared/egg_restrictions/patterns.py:913-916`` and
    ``orchestrator/routes/signals.py:961-964``.
    """

    def test_top_level_repo_config_fallback_resolves(self, monkeypatch):
        """Simulate the prod-container layout: ``config`` package absent,
        ``repo_config`` available at the top level.  The resolver MUST
        still produce the built-in opus / anthropic decision instead of
        propagating ``ModuleNotFoundError``.
        """
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")

        import types

        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        # Build a top-level ``repo_config`` shim whose
        # ``get_default_agent_model`` returns None (no override).
        repo_config_shim = types.ModuleType("repo_config")
        repo_config_shim.get_default_agent_model = lambda repo: None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "repo_config", repo_config_shim)

        # Remove the ``config.repo_config`` module so the primary import
        # raises ImportError and the fallback fires.  Need to remove
        # ``config`` itself too so ``from config.repo_config import ...``
        # actually raises rather than re-importing the existing module.
        for name in ("config.repo_config", "config"):
            sys.modules.pop(name, None)

        # Block import of the ``config`` package by inserting a meta-path
        # finder that raises ImportError specifically for it — leaves
        # other imports alone.
        import importlib.abc
        import importlib.machinery

        class _BlockConfigFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == "config" or fullname.startswith("config."):
                    raise ImportError(f"Simulating prod-container layout: {fullname} unavailable")
                return None

        finder = _BlockConfigFinder()
        sys.meta_path.insert(0, finder)
        try:
            d = resolve_agent_model(AgentRole.CODER, config, "owner/repo")
        finally:
            sys.meta_path.remove(finder)

        # Built-in opus / anthropic falls through.  The shim returned None
        # so neither pipeline override (empty) nor repo default fired.
        assert d.claude_code_alias == "opus"
        assert d.upstream == "anthropic"
        assert d.upstream_model is None


class TestDefaultPathRegression:
    """With ``agent_models={}`` and no repo default, EVERY role resolves
    to the Anthropic default — this is the slice-2 no-op invariant.
    """

    def test_every_assigned_role_defaults_to_anthropic_opus(self):
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            for role in (
                AgentRole.CODER,
                AgentRole.TESTER,
                AgentRole.DOCUMENTER,
                AgentRole.REFINER,
                AgentRole.ARCHITECT,
                AgentRole.TASK_PLANNER,
                AgentRole.RISK_ANALYST,
                AgentRole.REVIEWER_CODE,
                AgentRole.REVIEWER_CONTRACT,
                AgentRole.REVIEWER_CODE_HOLISTIC,
                AgentRole.REVIEWER_SECURITY,
                AgentRole.REVIEWER_CONCURRENCY,
            ):
                d = resolve_agent_model(role, config, "any/repo")
                assert d.claude_code_alias == "opus"
                assert d.upstream == "anthropic"
                assert d.upstream_model is None, (
                    f"Default path for {role.value} produced "
                    f"upstream_model={d.upstream_model!r} — slice-2 "
                    f"regression: default config must be Anthropic-only"
                )

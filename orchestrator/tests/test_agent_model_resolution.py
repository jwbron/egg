"""Tests for ``orchestrator/agent_model_resolution.py`` — slice-2 of #2769.

The resolver decides, for a given agent role, which Claude-Code-facing
model alias to pass to ``build_consensus_wrapped_command``, which
upstream to register on the gateway session, and which upstream-side
model name (if any) to put in ``session.upstream_model``.

Precedence (highest wins):

    1. ``pipeline_config.agent_models.get(role.value)``  — per-pipeline
    2. ``get_default_agent_model(repo)``                 — per-repo
    3. Built-in default — ``"fable"`` for refine/plan phase roles,
       ``"opus"`` for everything else

Classifier (model string → upstream):

    * ``"opus"``, ``"opus[1m]"``, ``"sonnet"``, ``"sonnet[1m]"``,
      ``"haiku"``, ``"fable"``, ``"fable[1m]"``, ``"claude-*"``
      → ``upstream="anthropic"``,
      ``claude_code_alias=<the string>``, ``upstream_model=None``.
    * anything else → ``upstream="litellm"``,
      ``claude_code_alias="<model>[1m]"`` (#2832: the [1m] suffix
      opts Claude Code into 1M-context compaction math via the
      ``ANTHROPIC_CUSTOM_MODEL_OPTION`` env var; Claude Code strips
      the suffix before send, so LiteLLM keys on the bare name),
      ``upstream_model=<the bare model name>``.

Plan reference: ``.egg-state/drafts/2769-plan.md`` TASK-2-3 / TASK-2-7;
#2832 superseded the cq-5 recognized-alias mitigation with env-var
registration in the agent sandbox.
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


# Built-in defaults of the context guardrails (#3175), mirrored from
# ``_CONTEXT_GUARDRAILS`` / ``_LITELLM_EXTRA_GUARDRAILS``. The Bash/MCP
# caps ride on EVERY decision's ``env_vars()`` (the dump arithmetic is
# route-independent); the tighter Read cap is LiteLLM-only (the Claude
# route keeps tool_output_cap's built-in 256 KiB default).
_GUARDRAIL_DEFAULTS = {
    "BASH_MAX_OUTPUT_LENGTH": "20000",
    "MAX_MCP_OUTPUT_TOKENS": "15000",
}
_LITELLM_GUARDRAIL_DEFAULTS = {
    **_GUARDRAIL_DEFAULTS,
    "EGG_READ_CAP_BYTES": "65536",
}

# Orchestrator-side override knobs for the guardrails — cleared in tests
# that assert exact env shapes so a value in the developer's own
# environment can't skew the expectation.
_GUARDRAIL_OVERRIDE_VARS = (
    "EGG_AGENT_BASH_MAX_OUTPUT_LENGTH",
    "EGG_AGENT_MAX_MCP_OUTPUT_TOKENS",
    "EGG_LITELLM_READ_CAP_BYTES",
)


def _clear_guardrail_overrides(monkeypatch):
    for var in _GUARDRAIL_OVERRIDE_VARS:
        monkeypatch.delenv(var, raising=False)


# =============================================================================
# AgentModelDecision dataclass shape
# =============================================================================


class TestAgentModelDecisionShape:
    """The decision tuple is the resolver's contract.

    Four named fields: ``claude_code_alias``, ``upstream``,
    ``upstream_model``, ``effort``. Downstream callers
    (``concurrent_executor`` / ``routes.pipelines``) unpack it into
    ``--model`` (Claude-Code-facing), the
    ``register_session(upstream=..., upstream_model=...)`` payload, and
    the ``--effort`` flag on the spawned agent.
    """

    def test_decision_has_four_named_fields(self):
        decision_cls = _decision_cls()
        d = decision_cls(
            claude_code_alias="opus",
            upstream="anthropic",
            upstream_model=None,
        )
        assert d.claude_code_alias == "opus"
        assert d.upstream == "anthropic"
        assert d.upstream_model is None
        assert d.effort is None

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
        # #2832: claude_code_alias is the suffixed name fed to Claude
        # Code's custom-model registration (was "opus" under cq-5).
        assert d.claude_code_alias == "qwen3-coder-30b[1m]"

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
        ["opus", "opus[1m]", "sonnet", "sonnet[1m]", "haiku", "fable", "fable[1m]"],
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

    On the LiteLLM path (#2832) ``claude_code_alias`` is the bare
    upstream name with the ``[1m]`` context-window-opt-in suffix
    appended — passed to ``--model`` and to
    ``ANTHROPIC_CUSTOM_MODEL_OPTION`` so Claude Code registers the
    custom model with 1M-window compaction math.
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
        # #2832: alias is "<model>[1m]" so Claude Code registers the
        # custom model with 1M-window compaction math.
        assert d.claude_code_alias == f"{model}[1m]", (
            f"LiteLLM-routed agents MUST present '<model>[1m]' to Claude "
            f"Code so it registers the custom model option (#2832); got "
            f"{d.claude_code_alias!r}"
        )
        assert d.upstream_model == model

    def test_litellm_alias_carries_1m_suffix_not_opus(self):
        """Adversarial guard for the #2832 env-var registration: the
        Claude-Code-facing alias for a LiteLLM-routed agent is NEVER
        the legacy ``"opus"`` pin — it MUST carry the ``[1m]`` suffix
        so Claude Code's custom-model registration fires with 1M
        compaction math.
        """
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()

        for tricky_model in ("opus-7b", "claude-clone-12b", "claudia-9000"):
            config = _pipeline_config(agent_models={"coder": tricky_model})

            with patch("config.repo_config.get_default_agent_model", return_value=None):
                d = resolve_agent_model(AgentRole.CODER, config, None)

            # The classifier may route a tricky name to anthropic (only
            # if it matches the documented exact aliases) — otherwise it
            # MUST route to litellm with claude_code_alias = "<name>[1m]".
            if d.upstream == "litellm":
                assert d.claude_code_alias == f"{tricky_model}[1m]", (
                    f"litellm route MUST pin claude_code_alias='<name>[1m]' "
                    f"for #2832 custom-model registration; got "
                    f"{d.claude_code_alias!r} for "
                    f"upstream_model={tricky_model!r}"
                )

    def test_pre_suffixed_model_is_idempotent(self):
        """If the operator passes ``qwen3-coder-30b[1m]`` directly,
        the resolver must not double the suffix — ``upstream_model``
        is the bare name and ``claude_code_alias`` carries a single
        ``[1m]``.
        """
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": "qwen3-coder-30b[1m]"})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.upstream == "litellm"
        assert d.upstream_model == "qwen3-coder-30b"
        assert d.claude_code_alias == "qwen3-coder-30b[1m]"

    def test_litellm_env_vars_register_custom_model(self, monkeypatch):
        """``AgentModelDecision.env_vars()`` returns the
        ``ANTHROPIC_CUSTOM_MODEL_OPTION`` pair that Claude Code reads at
        startup to opt the custom model into 1M compaction math (#2832),
        plus ``ANTHROPIC_AUTH_METHOD=api_key`` to mark the LiteLLM path
        as api-key auth for config validation / startup logging (#2832),
        plus ``CLAUDE_CODE_SUBAGENT_MODEL`` so Task-tool subagents route
        to the same upstream, plus ``ANTHROPIC_DEFAULT_HAIKU_MODEL`` /
        ``ANTHROPIC_SMALL_FAST_MODEL`` so the haiku alias and background
        helper calls route there too — all to avoid defaulting to a
        Claude model the LiteLLM proxy can't resolve
        (ProxyModelNotFoundError 400). The subagent var carries the
        ``[1m]`` alias; the haiku vars carry the bare upstream name
        (the suffix is read per-variable and small/fast calls don't
        need the 1M window).

        Since #3175 the LiteLLM env also carries the context guardrails
        (``BASH_MAX_OUTPUT_LENGTH`` / ``MAX_MCP_OUTPUT_TOKENS`` plus the
        LiteLLM-only ``EGG_READ_CAP_BYTES`` tightening) — per-turn
        re-billing of the full conversation makes a single oversized
        tool result disproportionately expensive.
        """
        _clear_guardrail_overrides(monkeypatch)
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": "qwen3-coder-30b"})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.env_vars() == {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "qwen3-coder-30b[1m]",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "qwen3-coder-30b",
            "ANTHROPIC_AUTH_METHOD": "api_key",
            "CLAUDE_CODE_SUBAGENT_MODEL": "qwen3-coder-30b[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3-coder-30b",
            "ANTHROPIC_SMALL_FAST_MODEL": "qwen3-coder-30b",
            **_LITELLM_GUARDRAIL_DEFAULTS,
        }

    def test_anthropic_env_vars_carry_only_guardrails(self, monkeypatch):
        """The default Anthropic path carries exactly the route-independent
        Bash/MCP context guardrails (#3175) and none of the custom-model
        registration vars — so the Claude *wire* shape stays identical to
        the pre-#2832 spawn, and the Read cap keeps tool_output_cap's
        built-in 256 KiB default (no ``EGG_READ_CAP_BYTES``).
        """
        _clear_guardrail_overrides(monkeypatch)
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.env_vars() == _GUARDRAIL_DEFAULTS


class TestSubOneMContextModels:
    """Models whose real context window is below 1M (``_SUB_1M_CONTEXT_MODELS``)
    must NOT get the ``[1m]`` suffix: Claude Code would treat them as 1M and
    defer compaction past their true limit, overflowing the upstream mid-turn.
    They take the bare alias → Claude Code's 200K default, which compacts
    safely below their window. See #2987.
    """

    @pytest.mark.parametrize("model", ["kimi-k2.7-code", "glm-5.1"])
    def test_sub_1m_model_drops_1m_suffix(self, model):
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": model})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.upstream == "litellm"
        assert d.upstream_model == model
        assert d.claude_code_alias == model, (
            f"sub-1M model {model!r} MUST present the bare name to Claude Code "
            f"(no [1m]) so it uses the 200K default and compacts before the "
            f"model's real limit; got {d.claude_code_alias!r}"
        )

    @pytest.mark.parametrize("model", ["kimi-k2.7-code", "glm-5.1"])
    def test_pre_suffixed_sub_1m_model_normalized_to_bare(self, model):
        """A stray operator ``[1m]`` on a sub-1M model is overridden — the
        registry is authoritative, so the alias is still bare.
        """
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": f"{model}[1m]"})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.upstream_model == model
        assert d.claude_code_alias == model

    @pytest.mark.parametrize("model", ["kimi-k2.7-code", "glm-5.1"])
    def test_sub_1m_env_vars_carry_bare_name(self, model, monkeypatch):
        """Every custom-model env var for a sub-1M model carries the bare
        name — none may leak the ``[1m]`` suffix (which would re-trigger the
        1M profile for the main agent or its Task-tool subagents). Parametrized
        over both registry entries so a future drift that only broke ``glm-5.1``
        (e.g. a ``.lower()`` or escape that special-cased the hyphen-period in
        ``k2.7``) is caught here, not in the field.
        """
        _clear_guardrail_overrides(monkeypatch)
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": model})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.env_vars() == {
            "ANTHROPIC_CUSTOM_MODEL_OPTION": model,
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": model,
            "ANTHROPIC_AUTH_METHOD": "api_key",
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            **_LITELLM_GUARDRAIL_DEFAULTS,
        }

    @pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro", "qwen3.7-max"])
    def test_one_m_models_still_carry_1m_suffix(self, model):
        """Regression guard: genuine >=1M cost-center models are unaffected —
        they still get ``[1m]`` so they use their full 1M window.
        """
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config(agent_models={"coder": model})

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            d = resolve_agent_model(AgentRole.CODER, config, None)

        assert d.upstream == "litellm"
        assert d.claude_code_alias == f"{model}[1m]"
        assert d.upstream_model == model

    def test_registry_entries_all_below_1m(self):
        """Every entry in ``_SUB_1M_CONTEXT_MODELS`` must declare a real
        window <1M. If a future upstream (e.g. a "Kimi K4 1.5M" successor)
        is added with a >=1M window, it belongs on the standard ``[1m]``
        path and adding it here would force it onto the 200K profile —
        wasting >800K of headroom. This test asserts the dict values
        aren't dead data; bumping a value to >=1M is the bug signal.
        """
        from agent_model_resolution import _SUB_1M_CONTEXT_MODELS

        for name, window in _SUB_1M_CONTEXT_MODELS.items():
            assert window < 1_000_000, (
                f"_SUB_1M_CONTEXT_MODELS[{name!r}] = {window:_}, which is "
                f">=1M. Models with a >=1M real window belong on the standard "
                f"[1m] path; remove this entry."
            )

    def test_pre_suffixed_sub_1m_emits_override_warning(self, caplog):
        """When an operator passes ``kimi-k2.7-code[1m]`` (sub-1M model with a
        stray [1m] suffix), the resolver overrides the suffix and emits a
        warning — silent overrides leave operators chasing why their
        requested 1M profile isn't being applied.
        """
        import logging

        from agent_model_resolution import classify_model

        with caplog.at_level(logging.WARNING, logger="agent_model_resolution"):
            d = classify_model("kimi-k2.7-code[1m]")

        assert d.claude_code_alias == "kimi-k2.7-code"
        # The warning's %r formatting puts repr quotes around the input alias and
        # the normalised bare name; assert both quoted forms appear so a future
        # drift that swapped the format args (e.g. logged ``bare`` twice instead
        # of ``model, bare``) would still fail this test rather than passing on
        # the substring ``"[1m]"`` in the static format string alone.
        assert any(
            "'kimi-k2.7-code[1m]'" in record.message and "'kimi-k2.7-code'" in record.message
            for record in caplog.records
        ), (
            f"expected override warning naming both 'kimi-k2.7-code[1m]' and 'kimi-k2.7-code'; "
            f"got {[r.message for r in caplog.records]!r}"
        )

    def test_bare_sub_1m_emits_no_warning(self, caplog):
        """The common case (operator passes the bare ``kimi-k2.7-code`` name)
        must NOT emit the override warning — the warning is reserved for
        the actual override.
        """
        import logging

        from agent_model_resolution import classify_model

        with caplog.at_level(logging.WARNING, logger="agent_model_resolution"):
            classify_model("kimi-k2.7-code")

        assert not caplog.records, (
            f"bare sub-1M model must not warn; got {[r.message for r in caplog.records]!r}"
        )


# =============================================================================
# Context guardrails (#3175)
# =============================================================================


class TestContextGuardrails:
    """Context guardrails on agent spawns (#3175 PR 2).

    Every turn re-bills the whole conversation at the cached rate — on
    every route; Anthropic cache reads are ~10% of input price — so a
    single oversized tool result (verbose ``pytest -v``, whole-megafile
    Read, unbounded MCP result) keeps costing for the life of the
    session. ``env_vars()`` injects ``BASH_MAX_OUTPUT_LENGTH`` /
    ``MAX_MCP_OUTPUT_TOKENS`` on every decision; the LiteLLM path adds
    a tighter ``EGG_READ_CAP_BYTES`` (the Claude route keeps
    tool_output_cap's built-in 256 KiB default).
    """

    @pytest.fixture(autouse=True)
    def _clean_overrides(self, monkeypatch):
        _clear_guardrail_overrides(monkeypatch)
        self.monkeypatch = monkeypatch

    def _litellm_decision(self):
        from agent_model_resolution import classify_model

        return classify_model("deepseek-v4-pro")

    def _anthropic_decision(self):
        from agent_model_resolution import classify_model

        return classify_model("opus")

    def test_litellm_decision_carries_all_guardrail_defaults(self):
        env = self._litellm_decision().env_vars()
        for var, default in _LITELLM_GUARDRAIL_DEFAULTS.items():
            assert env.get(var) == default

    def test_anthropic_decision_carries_bash_and_mcp_caps(self):
        env = self._anthropic_decision().env_vars()
        for var, default in _GUARDRAIL_DEFAULTS.items():
            assert env.get(var) == default

    def test_anthropic_decision_skips_read_cap_tightening(self):
        """The Read tightening is deliberately LiteLLM-only: the Claude
        route already runs tool_output_cap's predictive Read cap at its
        built-in 256 KiB default, and injecting the 64 KiB value would
        tighten production-route behavior 4x — even an operator override
        of the LiteLLM knob must not leak across routes.
        """
        self.monkeypatch.setenv("EGG_LITELLM_READ_CAP_BYTES", "1024")
        assert "EGG_READ_CAP_BYTES" not in self._anthropic_decision().env_vars()

    def test_operator_override_respected(self):
        self.monkeypatch.setenv("EGG_LITELLM_READ_CAP_BYTES", "131072")
        env = self._litellm_decision().env_vars()
        assert env["EGG_READ_CAP_BYTES"] == "131072"
        # The other two guardrails keep their defaults.
        assert env["BASH_MAX_OUTPUT_LENGTH"] == _GUARDRAIL_DEFAULTS["BASH_MAX_OUTPUT_LENGTH"]
        assert env["MAX_MCP_OUTPUT_TOKENS"] == _GUARDRAIL_DEFAULTS["MAX_MCP_OUTPUT_TOKENS"]

    def test_bash_mcp_overrides_apply_to_both_routes(self):
        self.monkeypatch.setenv("EGG_AGENT_BASH_MAX_OUTPUT_LENGTH", "30000")
        for decision in (self._litellm_decision(), self._anthropic_decision()):
            assert decision.env_vars()["BASH_MAX_OUTPUT_LENGTH"] == "30000"

    def test_empty_override_opts_guardrail_out(self):
        """An empty-string override omits that var entirely — on both
        routes — so the sandbox falls back to Claude Code's (or
        tool_output_cap's) own default; the per-guardrail kill switch.
        """
        self.monkeypatch.setenv("EGG_AGENT_BASH_MAX_OUTPUT_LENGTH", "")
        for decision in (self._litellm_decision(), self._anthropic_decision()):
            env = decision.env_vars()
            assert "BASH_MAX_OUTPUT_LENGTH" not in env
            assert env["MAX_MCP_OUTPUT_TOKENS"] == _GUARDRAIL_DEFAULTS["MAX_MCP_OUTPUT_TOKENS"]

    @pytest.mark.parametrize("bad", ["not-a-number", "64kb", "0", "-5"])
    def test_invalid_override_warns_and_falls_back(self, bad, caplog):
        """Garbage overrides must not be forwarded into the sandbox —
        ``tool_output_cap`` would warn-and-default per call and Claude
        Code's handling is undefined. Warn once at resolution time and
        inject the built-in default instead.
        """
        import logging

        self.monkeypatch.setenv("EGG_AGENT_MAX_MCP_OUTPUT_TOKENS", bad)
        with caplog.at_level(logging.WARNING, logger="agent_model_resolution"):
            env = self._litellm_decision().env_vars()

        assert env["MAX_MCP_OUTPUT_TOKENS"] == _GUARDRAIL_DEFAULTS["MAX_MCP_OUTPUT_TOKENS"]
        assert any(
            "EGG_AGENT_MAX_MCP_OUTPUT_TOKENS" in record.message and repr(bad) in record.message
            for record in caplog.records
        ), (
            f"expected fallback warning naming the override; got {[r.message for r in caplog.records]!r}"
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
        conflict_resolver) MUST be rejected — accepting them
        would let a deliberate override silently no-op at spawn, which is
        exactly the silent-ignore trap the validator exists to prevent.
        """
        if not _agent_models_field_exists():
            pytest.skip("PipelineConfig.agent_models not yet implemented")
        from pydantic import ValidationError

        for unhonored in ("overseer", "autofixer", "conflict_resolver"):
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
    to an Anthropic-route built-in: ``fable`` for the refine/plan phase
    roles, ``opus`` for everything else. ``upstream_model`` stays
    ``None`` on every default path — the slice-2 no-op invariant.
    """

    def test_implement_phase_roles_default_to_anthropic_opus(self):
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            for role in (
                AgentRole.CODER,
                AgentRole.TESTER,
                AgentRole.DOCUMENTER,
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

    def test_refine_and_plan_roles_default_to_anthropic_fable(self):
        """Refine/plan producers and reviewers pick up the built-in
        ``fable`` default while staying on the Anthropic upstream."""
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            for role in (
                AgentRole.REFINER,
                AgentRole.REVIEWER_REFINE,
                AgentRole.REVIEWER_AGENT_DESIGN,
                AgentRole.ARCHITECT,
                AgentRole.TASK_PLANNER,
                AgentRole.RISK_ANALYST,
                AgentRole.REVIEWER_PLAN,
            ):
                d = resolve_agent_model(role, config, "any/repo")
                assert d.claude_code_alias == "fable", (
                    f"{role.value} should default to fable, got {d.claude_code_alias!r}"
                )
                assert d.upstream == "anthropic"
                assert d.upstream_model is None

    def test_repo_default_overrides_fable_builtin(self):
        """A repo-level ``default_agent_model`` still beats the built-in
        fable default — precedence is unchanged, only tier 3 split."""
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch(
            "config.repo_config.get_default_agent_model",
            return_value="sonnet",
        ):
            d = resolve_agent_model(AgentRole.REFINER, config, "owner/repo")

        assert d.claude_code_alias == "sonnet"
        assert d.upstream == "anthropic"
        assert d.upstream_model is None


# =============================================================================
# Effort pinning: fable decisions carry effort="high", everything else None
# =============================================================================


class TestEffortPinning:
    """Fable-routed decisions pin ``effort="high"`` (FABLE_EFFORT); every
    other decision carries ``effort=None`` so the agent inherits Claude
    Code's per-model default (notably opus stays on its existing
    default-effort baseline).
    """

    @pytest.mark.parametrize("model", ["fable", "fable[1m]"])
    def test_fable_aliases_pin_high_effort(self, model):
        from agent_model_resolution import classify_model

        d = classify_model(model)
        assert d.effort == "high"
        assert d.upstream == "anthropic"

    @pytest.mark.parametrize(
        "model",
        [
            "opus",
            "opus[1m]",
            "sonnet",
            "sonnet[1m]",
            "haiku",
            "claude-sonnet-4-5",
            "claude-fable-5",
        ],
    )
    def test_other_claude_aliases_inherit_default_effort(self, model):
        from agent_model_resolution import classify_model

        assert classify_model(model).effort is None

    def test_litellm_models_inherit_default_effort(self):
        from agent_model_resolution import classify_model

        assert classify_model("qwen3-max").effort is None

    def test_refine_plan_default_decision_carries_high_effort(self):
        """The built-in fable default for refine/plan roles flows through
        ``resolve_agent_model`` with the pinned effort attached."""
        resolve_agent_model = _resolver()
        AgentRole = _agent_role()
        config = _pipeline_config()

        with patch("config.repo_config.get_default_agent_model", return_value=None):
            refiner = resolve_agent_model(AgentRole.REFINER, config, "any/repo")
            coder = resolve_agent_model(AgentRole.CODER, config, "any/repo")

        assert refiner.effort == "high"
        assert coder.effort is None

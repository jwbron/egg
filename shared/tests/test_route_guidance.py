"""Tests for the LiteLLM-route system-prompt addendum (#3175).

The addendum is appended by ``egg_agent.client.run_agent_async`` only on
non-Claude routes; these tests pin the env gate and the properties the
caching design depends on (pure constant, bounded size, names only the
``general-purpose`` subagent).
"""

import pytest
from egg_agent.route_guidance import (
    LITELLM_ROUTE_GUIDANCE,
    is_route_guidance_disabled,
    route_guidance_addendum,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CUSTOM_MODEL_OPTION", raising=False)
    monkeypatch.delenv("EGG_ROUTE_PROMPT_GUIDANCE", raising=False)


class TestRouteGate:
    def test_claude_route_gets_no_addendum(self):
        # ANTHROPIC_CUSTOM_MODEL_OPTION unset = first-party Anthropic route.
        assert route_guidance_addendum() is None

    def test_blank_route_var_gets_no_addendum(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_CUSTOM_MODEL_OPTION", "   ")
        assert route_guidance_addendum() is None

    def test_litellm_route_gets_addendum(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_CUSTOM_MODEL_OPTION", "deepseek-v4-pro[1m]")
        assert route_guidance_addendum() == LITELLM_ROUTE_GUIDANCE

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " Off "])
    def test_kill_switch_disables(self, monkeypatch, value):
        monkeypatch.setenv("ANTHROPIC_CUSTOM_MODEL_OPTION", "deepseek-v4-pro[1m]")
        monkeypatch.setenv("EGG_ROUTE_PROMPT_GUIDANCE", value)
        assert is_route_guidance_disabled()
        assert route_guidance_addendum() is None

    @pytest.mark.parametrize("value", ["", "true", "1", "on", "garbage"])
    def test_non_disabling_values_keep_addendum(self, monkeypatch, value):
        monkeypatch.setenv("ANTHROPIC_CUSTOM_MODEL_OPTION", "qwen3-max[1m]")
        monkeypatch.setenv("EGG_ROUTE_PROMPT_GUIDANCE", value)
        assert not is_route_guidance_disabled()
        assert route_guidance_addendum() == LITELLM_ROUTE_GUIDANCE


class TestAddendumContent:
    def test_deterministic(self, monkeypatch):
        # Per-session stability is the cache-prefix contract: two renders
        # in the same env must be byte-identical.
        monkeypatch.setenv("ANTHROPIC_CUSTOM_MODEL_OPTION", "deepseek-v4-pro[1m]")
        assert route_guidance_addendum() == route_guidance_addendum()

    def test_bounded_size(self):
        # The addendum rides in the system prompt of every LiteLLM-routed
        # session; keep it well under the per-event envelope scale so it
        # never becomes its own context-cost problem.
        assert len(LITELLM_ROUTE_GUIDANCE.encode("utf-8")) < 2048

    def test_names_only_registered_subagent(self):
        # The sandbox runtime registers no AgentDefinition beyond the SDK's
        # built-in general-purpose; naming an unregistered type (e.g.
        # Explore) burns a turn on the unknown-subagent retry — the exact
        # waste this addendum exists to remove. Mirrors the constraint on
        # _EXPLORATION_SUBAGENT_GUIDANCE in orchestrator/routes/pipelines.py.
        assert "general-purpose" in LITELLM_ROUTE_GUIDANCE
        assert "Explore" not in LITELLM_ROUTE_GUIDANCE

    def test_core_levers_present(self):
        # Levers 2 + 4 from #3175 PR 3: batching, output filtering,
        # subagent-isolated bulk reads.
        assert "Batch independent tool calls" in LITELLM_ROUTE_GUIDANCE
        assert "Filter command output" in LITELLM_ROUTE_GUIDANCE
        assert "subagent" in LITELLM_ROUTE_GUIDANCE

    def test_advisory_not_budget(self):
        # The guidance must steer working style without licensing skipped
        # verification — pin the explicit disclaimer.
        assert "not budgets" in LITELLM_ROUTE_GUIDANCE
        assert "never skip" in LITELLM_ROUTE_GUIDANCE

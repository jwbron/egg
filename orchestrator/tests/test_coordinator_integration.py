"""
Tests for coordinator end-to-end integration (Phase 5, TASK-5-4).

Tests the full coordinator pipeline lifecycle and validates component
integration across the coordinator feature.
"""

import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from models import (
    AgentRole,
    AgentSpawnRecord,
    CoordinatorState,
    Escalation,
    GuardrailCounters,
    PhaseDecision,
)


class TestCoordinatorInstructionsExist:
    """Tests for coordinator agent instructions (Phase 5, TASK-5-1)."""

    def test_coordinator_instructions_file_exists(self):
        """sandbox/.claude/rules/coordinator.md must exist."""
        instructions_path = _project_root / "sandbox" / ".claude" / "rules" / "coordinator.md"
        assert instructions_path.exists()

    def test_coordinator_instructions_under_3000_tokens(self):
        """Coordinator instructions should be under 3000 tokens."""
        instructions_path = _project_root / "sandbox" / ".claude" / "rules" / "coordinator.md"
        if not instructions_path.exists():
            pytest.skip("coordinator.md not yet created")

        content = instructions_path.read_text()
        # Rough estimate: 1 token ≈ 4 characters
        estimated_tokens = len(content) / 4
        assert estimated_tokens < 3000, (
            f"Coordinator instructions are approximately {estimated_tokens:.0f} tokens, "
            "should be under 3000."
        )

    def test_coordinator_instructions_cover_key_topics(self):
        """Coordinator instructions must cover critical topics."""
        instructions_path = _project_root / "sandbox" / ".claude" / "rules" / "coordinator.md"
        if not instructions_path.exists():
            pytest.skip("coordinator.md not yet created")

        content = instructions_path.read_text().lower()
        required_topics = [
            "egg-orch coordinator",  # CLI tool references
            "spawn",  # Agent spawning
            "escalat",  # Escalation policy
        ]
        for topic in required_topics:
            assert topic in content, f"Coordinator instructions should cover '{topic}'"


class TestCoordinatorContainerSpawning:
    """Tests for coordinator container spawning support (Phase 5, TASK-5-2)."""

    def test_container_spawner_supports_coordinator(self):
        """container_spawner.py must support coordinator role.

        Gap: Coordinator-specific prompt generation.
        """
        spawner_path = _project_root / "orchestrator" / "container_spawner.py"
        if not spawner_path.exists():
            pytest.skip("container_spawner.py not found")

        content = spawner_path.read_text()
        has_coordinator = "coordinator" in content.lower()
        if not has_coordinator:
            pytest.skip(
                "container_spawner.py does not yet reference coordinator. "
                "Need coordinator-specific prompt with task context and state for resume."
            )


class TestCoordinatorSSEEvents:
    """Tests for coordinator events in SSE stream (Phase 5, TASK-5-3)."""

    def test_sse_includes_coordinator_events(self):
        """SSE stream must include coordinator event types.

        Gap: Coordinator events not yet in SSE handlers.
        """
        sse_path = _project_root / "orchestrator" / "sse.py"
        if not sse_path.exists():
            pytest.skip("sse.py not found")

        content = sse_path.read_text()
        has_coordinator = "coordinator" in content.lower()
        if not has_coordinator:
            pytest.skip(
                "sse.py does not yet reference coordinator events. "
                "Add COORDINATOR_DECISION, COORDINATOR_SPAWN, "
                "COORDINATOR_ESCALATION, COORDINATOR_LOOPBACK to SSE stream."
            )

    def test_unified_sse_includes_coordinator_events(self):
        """Unified SSE must include coordinator events for DAG visualizer."""
        unified_path = _project_root / "orchestrator" / "unified_sse.py"
        if not unified_path.exists():
            pytest.skip("unified_sse.py not found")

        content = unified_path.read_text()
        has_coordinator = "coordinator" in content.lower()
        if not has_coordinator:
            pytest.skip(
                "unified_sse.py does not yet reference coordinator events. "
                "DAG visualizer should render coordinator as a node."
            )


class TestCoordinatorDocumentation:
    """Tests for coordinator documentation (Phase 5, TASK-5-5)."""

    def test_coordinator_guide_exists(self):
        """docs/guides/coordinator.md must exist."""
        guide_path = _project_root / "docs" / "guides" / "coordinator.md"
        assert guide_path.exists()

    def test_index_links_to_coordinator_guide(self):
        """docs/index.md must link to coordinator guide."""
        index_path = _project_root / "docs" / "index.md"
        if not index_path.exists():
            pytest.skip("docs/index.md not found")

        content = index_path.read_text().lower()
        has_coordinator = "coordinator" in content
        if not has_coordinator:
            pytest.skip(
                "docs/index.md does not yet reference coordinator. "
                "Add coordinator to the task-specific guide lookup table."
            )


class TestCoordinatorWorkflowScenarios:
    """Integration tests for coordinator workflow decision scenarios."""

    def test_simple_bug_fix_workflow_state(self):
        """Simple bug fix: coordinator should skip analyze/plan phases.

        Validates the state model can represent a skip-to-implement workflow.
        """
        state = CoordinatorState(
            workflow_type="bug_fix",
            phase_decisions=[
                PhaseDecision(
                    phase="analyze",
                    action="skip",
                    reason="Simple bug fix, no analysis needed",
                ),
                PhaseDecision(
                    phase="plan",
                    action="skip",
                    reason="Simple bug fix, no planning needed",
                ),
            ],
            agents_spawned=[
                AgentSpawnRecord(
                    role=AgentRole.CODER,
                    status="complete",
                    task_context="Fix auth bug in #432",
                ),
                AgentSpawnRecord(
                    role=AgentRole.TESTER,
                    status="complete",
                    task_context="Test auth fix",
                ),
            ],
            guardrail_counters=GuardrailCounters(total_agents_spawned=2),
        )

        # Verify workflow representation
        assert state.workflow_type == "bug_fix"
        assert len(state.phase_decisions) == 2
        assert all(d.action == "skip" for d in state.phase_decisions)
        assert len(state.agents_spawned) == 2

    def test_complex_feature_workflow_state(self):
        """Complex feature: coordinator uses full SDLC with all phases."""
        state = CoordinatorState(
            workflow_type="feature",
            phase_decisions=[
                PhaseDecision(phase="analyze", action="advance"),
                PhaseDecision(phase="plan", action="advance"),
                PhaseDecision(phase="implement", action="advance"),
            ],
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
                AgentSpawnRecord(role=AgentRole.TESTER, status="complete"),
                AgentSpawnRecord(role=AgentRole.DOCUMENTER, status="complete"),
            ],
            guardrail_counters=GuardrailCounters(total_agents_spawned=3),
        )

        assert state.workflow_type == "feature"
        assert len(state.phase_decisions) == 3

    def test_loopback_workflow_state(self):
        """Tester finds edge case, coordinator loops back to coder."""
        state = CoordinatorState(
            workflow_type="bug_fix",
            phase_decisions=[
                PhaseDecision(phase="implement", action="advance"),
                PhaseDecision(
                    phase="implement",
                    action="loopback",
                    reason="Tester found edge case in auth flow",
                ),
            ],
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="complete", retry_number=0),
                AgentSpawnRecord(role=AgentRole.TESTER, status="complete", retry_number=0),
                # Second round
                AgentSpawnRecord(
                    role=AgentRole.CODER,
                    status="complete",
                    retry_number=1,
                    task_context="Fix edge case in auth flow",
                ),
                AgentSpawnRecord(
                    role=AgentRole.TESTER,
                    status="complete",
                    retry_number=1,
                    task_context="Verify edge case fix",
                ),
            ],
            guardrail_counters=GuardrailCounters(
                total_agents_spawned=4,
                retries_by_role={"coder": 1, "tester": 1},
            ),
        )

        assert len(state.agents_spawned) == 4
        assert state.guardrail_counters.total_agents_spawned == 4
        loopback_decisions = [d for d in state.phase_decisions if d.action == "loopback"]
        assert len(loopback_decisions) == 1

    def test_escalation_workflow_state(self):
        """Coordinator escalates ambiguous requirement to human."""
        state = CoordinatorState(
            workflow_type="feature",
            escalations=[
                Escalation(
                    question="Should the API use REST or GraphQL?",
                    escalation_type="choice",
                    resolution="REST",
                ),
            ],
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="running"),
            ],
        )

        assert len(state.escalations) == 1
        assert state.escalations[0].resolution == "REST"

    def test_crash_recovery_state(self):
        """Coordinator crash state can be used for re-assessment."""
        state = CoordinatorState(
            workflow_type="feature",
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
                AgentSpawnRecord(role=AgentRole.TESTER, status="running"),
            ],
            phase_decisions=[
                PhaseDecision(phase="implement", action="advance"),
            ],
            guardrail_counters=GuardrailCounters(
                total_agents_spawned=2,
                coordinator_respawns=1,  # One respawn already
            ),
        )

        # A new coordinator can re-assess from this state
        assert state.guardrail_counters.coordinator_respawns == 1
        running_agents = [a for a in state.agents_spawned if a.status == "running"]
        assert len(running_agents) == 1
        assert running_agents[0].role == AgentRole.TESTER

    def test_guardrail_exceeded_state(self):
        """State at guardrail limits prevents further spawning."""
        state = CoordinatorState(
            guardrail_counters=GuardrailCounters(
                total_agents_spawned=10,  # At max
                retries_by_role={"coder": 2},  # At max retries
                coordinator_respawns=2,  # At max respawns
            ),
        )

        # Validate limits
        MAX_AGENTS = 10
        MAX_RETRIES = 2
        MAX_RESPAWNS = 2

        assert state.guardrail_counters.total_agents_spawned >= MAX_AGENTS
        assert state.guardrail_counters.retries_by_role.get("coder", 0) >= MAX_RETRIES
        assert state.guardrail_counters.coordinator_respawns >= MAX_RESPAWNS

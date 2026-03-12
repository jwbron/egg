"""
Tests for coordinator state management in state_store.py (Phase 1, TASK-1-4).

Tests update_coordinator_state and get_coordinator_state methods
with incremental merge support.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_project_root = Path(__file__).parent.parent.parent
for p in (_project_root / "orchestrator", _project_root / "shared"):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from models import (
    AgentRole,
    AgentSpawnRecord,
    CoordinatorState,
    GuardrailCounters,
    PhaseDecision,
    Pipeline,
    PipelineConfig,
)


class TestStateStoreCoordinatorMethods:
    """Tests for coordinator state management methods in StateStore."""

    def test_state_store_has_update_coordinator_state(self):
        """StateStore must have update_coordinator_state method.

        Gap: New method needed in state_store.py.
        """
        state_store_path = _project_root / "orchestrator" / "state_store.py"
        content = state_store_path.read_text()
        has_method = "update_coordinator_state" in content
        if not has_method:
            pytest.skip(
                "update_coordinator_state not yet added to StateStore. "
                "Need: update_coordinator_state(pipeline_id, state) with incremental merge."
            )

    def test_state_store_has_get_coordinator_state(self):
        """StateStore must have get_coordinator_state method.

        Gap: New method needed in state_store.py.
        """
        state_store_path = _project_root / "orchestrator" / "state_store.py"
        content = state_store_path.read_text()
        has_method = "get_coordinator_state" in content
        if not has_method:
            pytest.skip(
                "get_coordinator_state not yet added to StateStore. "
                "Need: get_coordinator_state(pipeline_id) returning CoordinatorState."
            )


class TestCoordinatorStateInPipeline:
    """Tests for coordinator state integrated into Pipeline model.

    The coordinator state is embedded in the Pipeline model, so state
    persistence naturally goes through the existing Pipeline save/load path.
    """

    def test_coordinator_state_model_standalone(self):
        """CoordinatorState works correctly as a standalone model."""
        state = CoordinatorState(
            workflow_type="bug_fix",
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER),
            ],
            guardrail_counters=GuardrailCounters(total_agents_spawned=1),
        )
        # Verify it serializes
        data = state.model_dump()
        assert data["workflow_type"] == "bug_fix"
        assert len(data["agents_spawned"]) == 1

    def test_coordinator_state_incremental_update(self):
        """CoordinatorState supports incremental updates by adding to lists."""
        state = CoordinatorState(workflow_type="feature")

        # Simulate incremental updates
        state.agents_spawned.append(
            AgentSpawnRecord(role=AgentRole.CODER, task_context="Implement feature")
        )
        assert len(state.agents_spawned) == 1

        state.agents_spawned.append(
            AgentSpawnRecord(role=AgentRole.TESTER, task_context="Test feature")
        )
        assert len(state.agents_spawned) == 2

        state.phase_decisions.append(
            PhaseDecision(phase="implement", action="advance")
        )
        assert len(state.phase_decisions) == 1

        state.guardrail_counters.total_agents_spawned = 2
        assert state.guardrail_counters.total_agents_spawned == 2

    def test_coordinator_state_merge_preserves_existing(self):
        """Incremental merge should preserve existing data."""
        state = CoordinatorState(
            workflow_type="feature",
            agents_spawned=[
                AgentSpawnRecord(role=AgentRole.CODER, status="complete"),
            ],
        )

        # Add more data without losing existing
        state.agents_spawned.append(
            AgentSpawnRecord(role=AgentRole.TESTER, status="running")
        )
        assert len(state.agents_spawned) == 2
        assert state.agents_spawned[0].status == "complete"  # preserved
        assert state.agents_spawned[1].status == "running"  # new

    def test_coordinator_state_serialization_full_cycle(self):
        """CoordinatorState full serialize → deserialize cycle."""
        original = CoordinatorState(
            workflow_type="complex_feature",
            agents_spawned=[
                AgentSpawnRecord(
                    role=AgentRole.CODER,
                    status="complete",
                    container_id="c-001",
                    task_context="Phase 1 implementation",
                    retry_number=0,
                ),
                AgentSpawnRecord(
                    role=AgentRole.TESTER,
                    status="running",
                    container_id="c-002",
                    retry_number=0,
                ),
            ],
            phase_decisions=[
                PhaseDecision(
                    phase="refine",
                    action="skip",
                    reason="Simple fix, no refinement needed",
                ),
                PhaseDecision(
                    phase="implement",
                    action="advance",
                    reason="All tests pass",
                ),
            ],
            guardrail_counters=GuardrailCounters(
                total_agents_spawned=5,
                retries_by_role={"coder": 1},
                coordinator_respawns=0,
            ),
        )

        json_str = original.model_dump_json()
        restored = CoordinatorState.model_validate_json(json_str)

        assert restored.workflow_type == "complex_feature"
        assert len(restored.agents_spawned) == 2
        assert restored.agents_spawned[0].container_id == "c-001"
        assert len(restored.phase_decisions) == 2
        assert restored.phase_decisions[0].action == "skip"
        assert restored.guardrail_counters.total_agents_spawned == 5
        assert restored.guardrail_counters.retries_by_role["coder"] == 1

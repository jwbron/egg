"""Tests for PhaseDependencyGraph.

Covers:
- Wave computation from phase dependencies
- Cycle detection
- Single-node and empty graphs
- Topological sort ordering
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from egg_contracts.dependency_graph import PhaseDependencyGraph


@dataclass
class FakePhase:
    """Minimal Phase-like object for testing."""

    id: str
    name: str = ""
    dependencies: list[str] = field(default_factory=list)


class TestPhaseDependencyGraphWaves:
    """Tests for compute_waves()."""

    def test_linear_chain(self):
        """Phases with linear dependencies produce sequential waves."""
        phases = [
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
            FakePhase(id="phase-3", dependencies=["phase-2"]),
        ]
        graph = PhaseDependencyGraph(phases)
        waves = graph.compute_waves()

        assert len(waves) == 3
        assert waves[0].phase_ids == ["phase-1"]
        assert waves[1].phase_ids == ["phase-2"]
        assert waves[2].phase_ids == ["phase-3"]

    def test_independent_phases_same_wave(self):
        """Independent phases are grouped into the same wave."""
        phases = [
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=[]),
            FakePhase(id="phase-3", dependencies=[]),
        ]
        graph = PhaseDependencyGraph(phases)
        waves = graph.compute_waves()

        assert len(waves) == 1
        assert sorted(waves[0].phase_ids) == ["phase-1", "phase-2", "phase-3"]
        assert waves[0].is_parallel()

    def test_diamond_dependency(self):
        """Diamond dependency pattern produces correct waves."""
        phases = [
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
            FakePhase(id="phase-3", dependencies=["phase-1"]),
            FakePhase(id="phase-4", dependencies=["phase-2", "phase-3"]),
        ]
        graph = PhaseDependencyGraph(phases)
        waves = graph.compute_waves()

        assert len(waves) == 3
        assert waves[0].phase_ids == ["phase-1"]
        assert sorted(waves[1].phase_ids) == ["phase-2", "phase-3"]
        assert waves[1].is_parallel()
        assert waves[2].phase_ids == ["phase-4"]

    def test_mixed_independent_and_dependent(self):
        """Mix of independent and dependent phases."""
        phases = [
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
            FakePhase(id="phase-3", dependencies=["phase-1"]),
            FakePhase(id="phase-4", dependencies=["phase-2", "phase-3"]),
            FakePhase(id="phase-5", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        waves = graph.compute_waves()

        assert waves[0].phase_ids == ["phase-1"]
        # phase-2, phase-3, phase-5 all depend only on phase-1
        assert sorted(waves[1].phase_ids) == ["phase-2", "phase-3", "phase-5"]
        assert waves[2].phase_ids == ["phase-4"]

    def test_wave_numbers_are_one_indexed(self):
        """Wave numbers start at 1."""
        phases = [
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        waves = graph.compute_waves()

        assert waves[0].wave_number == 1
        assert waves[1].wave_number == 2


class TestPhaseDependencyGraphCycleDetection:
    """Tests for cycle detection."""

    def test_no_cycle(self):
        """Graph without cycles returns False."""
        phases = [
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        assert not graph.has_cycle()

    def test_direct_cycle(self):
        """Direct circular dependency is detected."""
        phases = [
            FakePhase(id="phase-1", dependencies=["phase-2"]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        assert graph.has_cycle()

    def test_indirect_cycle(self):
        """Indirect circular dependency is detected."""
        phases = [
            FakePhase(id="phase-1", dependencies=["phase-3"]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
            FakePhase(id="phase-3", dependencies=["phase-2"]),
        ]
        graph = PhaseDependencyGraph(phases)
        assert graph.has_cycle()

    def test_self_cycle(self):
        """Self-referencing dependency is detected."""
        phases = [
            FakePhase(id="phase-1", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        assert graph.has_cycle()

    def test_cycle_raises_on_compute_waves(self):
        """compute_waves() raises ValueError on cycle."""
        phases = [
            FakePhase(id="phase-1", dependencies=["phase-2"]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        with pytest.raises(ValueError, match="cycles"):
            graph.compute_waves()

    def test_cycle_raises_on_topological_sort(self):
        """topological_sort() raises ValueError on cycle."""
        phases = [
            FakePhase(id="phase-1", dependencies=["phase-2"]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        with pytest.raises(ValueError, match="cycles"):
            graph.topological_sort()


class TestPhaseDependencyGraphEdgeCases:
    """Tests for edge cases."""

    def test_single_node(self):
        """Single phase with no dependencies."""
        phases = [FakePhase(id="phase-1", dependencies=[])]
        graph = PhaseDependencyGraph(phases)
        waves = graph.compute_waves()

        assert len(waves) == 1
        assert waves[0].phase_ids == ["phase-1"]
        assert not waves[0].is_parallel()

    def test_empty_graph(self):
        """Empty graph produces no waves."""
        graph = PhaseDependencyGraph([])
        waves = graph.compute_waves()
        assert waves == []

    def test_none_phases(self):
        """None phases produces no waves."""
        graph = PhaseDependencyGraph(None)
        waves = graph.compute_waves()
        assert waves == []

    def test_unknown_dependency_ignored(self):
        """Dependencies on unknown phases are ignored."""
        phases = [
            FakePhase(id="phase-1", dependencies=["phase-99"]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        # Should not raise
        waves = graph.compute_waves()
        # phase-1 has unknown dep, but it's ignored
        assert len(waves) == 2

    def test_add_phase_manually(self):
        """Phases can be added manually."""
        graph = PhaseDependencyGraph()
        graph.add_phase("phase-1")
        graph.add_phase("phase-2", dependencies=["phase-1"])
        graph.add_phase("phase-3", dependencies=["phase-1"])

        waves = graph.compute_waves()
        assert len(waves) == 2
        assert waves[0].phase_ids == ["phase-1"]
        assert sorted(waves[1].phase_ids) == ["phase-2", "phase-3"]


class TestPhaseDependencyGraphSequentialOrder:
    """Tests for get_sequential_order()."""

    def test_sequential_order_respects_dependencies(self):
        """Sequential order puts dependencies before dependents."""
        phases = [
            FakePhase(id="phase-3", dependencies=["phase-1"]),
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=["phase-1"]),
        ]
        graph = PhaseDependencyGraph(phases)
        order = graph.get_sequential_order()

        assert order.index("phase-1") < order.index("phase-2")
        assert order.index("phase-1") < order.index("phase-3")

    def test_sequential_order_deterministic(self):
        """Sequential order is deterministic (sorted within waves)."""
        phases = [
            FakePhase(id="phase-3", dependencies=[]),
            FakePhase(id="phase-1", dependencies=[]),
            FakePhase(id="phase-2", dependencies=[]),
        ]
        graph = PhaseDependencyGraph(phases)
        order1 = graph.get_sequential_order()
        order2 = graph.get_sequential_order()

        assert order1 == order2

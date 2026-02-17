"""Tests for plan parser dependencies field propagation.

Covers:
- ParsedPhase.dependencies -> Phase.dependencies via to_contract_phase()
- Various dependency formats (phase-N, numeric, comma-separated)
- Empty/missing dependencies
"""

from __future__ import annotations

from egg_contracts.plan_parser import ParsedPhase, ParsedTask


class TestToContractPhaseDependencies:
    """Tests for to_contract_phase() dependency propagation."""

    def test_empty_dependencies(self):
        """Phase with empty dependencies produces empty list."""
        phase = ParsedPhase(
            number=1,
            name="Phase 1",
            goal="Do something",
            tasks=[],
            dependencies="",
        )
        contract_phase = phase.to_contract_phase()
        assert contract_phase.dependencies == []

    def test_single_phase_id_dependency(self):
        """Dependencies in phase-N format are preserved."""
        phase = ParsedPhase(
            number=2,
            name="Phase 2",
            goal="Do something",
            tasks=[],
            dependencies="phase-1",
        )
        contract_phase = phase.to_contract_phase()
        assert contract_phase.dependencies == ["phase-1"]

    def test_multiple_comma_separated_dependencies(self):
        """Comma-separated dependencies are all parsed."""
        phase = ParsedPhase(
            number=4,
            name="Phase 4",
            goal="Do something",
            tasks=[],
            dependencies="phase-1, phase-2, phase-3",
        )
        contract_phase = phase.to_contract_phase()
        assert contract_phase.dependencies == ["phase-1", "phase-2", "phase-3"]

    def test_numeric_dependencies_normalized(self):
        """Numeric dependencies are normalized to phase-N format."""
        phase = ParsedPhase(
            number=3,
            name="Phase 3",
            goal="Do something",
            tasks=[],
            dependencies="1, 2",
        )
        contract_phase = phase.to_contract_phase()
        assert contract_phase.dependencies == ["phase-1", "phase-2"]

    def test_contract_phase_id_format(self):
        """Contract phase ID follows phase-N format."""
        phase = ParsedPhase(
            number=5,
            name="Phase 5",
            goal="Do something",
            tasks=[
                ParsedTask(
                    id="TASK-5-1",
                    phase_number=5,
                    task_number=1,
                    description="test",
                    acceptance_criteria="works",
                ),
            ],
            dependencies="phase-1",
        )
        contract_phase = phase.to_contract_phase()
        assert contract_phase.id == "phase-5"

    def test_tasks_preserved_with_dependencies(self):
        """Tasks are correctly converted alongside dependencies."""
        phase = ParsedPhase(
            number=1,
            name="Phase 1",
            goal="Do something",
            tasks=[
                ParsedTask(
                    id="TASK-1-1",
                    phase_number=1,
                    task_number=1,
                    description="First task",
                    acceptance_criteria="passes",
                ),
                ParsedTask(
                    id="TASK-1-2",
                    phase_number=1,
                    task_number=2,
                    description="Second task",
                    acceptance_criteria="passes",
                ),
            ],
            dependencies="phase-2",
        )
        contract_phase = phase.to_contract_phase()
        assert len(contract_phase.tasks) == 2
        assert contract_phase.dependencies == ["phase-2"]

    def test_list_format_dependencies(self):
        """Dependencies provided as a list are handled."""
        phase = ParsedPhase(
            number=2,
            name="Phase 2",
            goal="Do something",
            tasks=[],
        )
        # Manually set dependencies as a list (as it might come from YAML)
        phase.dependencies = ["phase-1", "phase-3"]  # type: ignore[assignment]
        contract_phase = phase.to_contract_phase()
        assert contract_phase.dependencies == ["phase-1", "phase-3"]

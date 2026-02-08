"""Tests for action/populate-contract-tasks.py."""

import sys
from pathlib import Path

# Add action directory to path so we can import the module
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "action"))
sys.path.insert(0, str(PROJECT_ROOT / "shared"))

from importlib import import_module

from egg_contracts.plan_parser import ParsedPhase, ParsedTask

# Import the module dynamically since it has a hyphenated filename
spec = import_module("populate-contract-tasks")
extract_acceptance_criteria = spec.extract_acceptance_criteria


def make_task(phase: int, task: int, description: str, acceptance: str) -> ParsedTask:
    """Helper to create a ParsedTask."""
    return ParsedTask(
        id=f"TASK-{phase}-{task}",
        phase_number=phase,
        task_number=task,
        description=description,
        acceptance_criteria=acceptance,
    )


def make_phase(number: int, name: str, tasks: list[ParsedTask]) -> ParsedPhase:
    """Helper to create a ParsedPhase."""
    return ParsedPhase(
        number=number,
        name=name,
        goal="",
        tasks=tasks,
    )


class TestExtractAcceptanceCriteria:
    """Tests for extracting acceptance criteria from parsed phases."""

    def test_extracts_task_acceptance_criteria(self):
        """Test extracting acceptance criteria from tasks."""
        phases = [
            make_phase(
                1,
                "Setup",
                [
                    make_task(1, 1, "Create schema", "Schema validates test contracts"),
                    make_task(1, 2, "Add validation", "Unauthorized mutations rejected"),
                ],
            )
        ]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 2
        assert criteria[0]["id"] == "ac-1"
        assert "[TASK-1-1]" in criteria[0]["description"]
        assert "Schema validates test contracts" in criteria[0]["description"]
        assert criteria[0]["verified"] is False
        assert criteria[1]["id"] == "ac-2"
        assert "[TASK-1-2]" in criteria[1]["description"]
        assert "Unauthorized mutations rejected" in criteria[1]["description"]

    def test_extracts_from_multiple_phases(self):
        """Test extracting acceptance criteria from multiple phases."""
        phases = [
            make_phase(
                1,
                "Setup",
                [make_task(1, 1, "Task 1", "Criterion 1")],
            ),
            make_phase(
                2,
                "Implementation",
                [make_task(2, 1, "Task 2", "Criterion 2")],
            ),
        ]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 2
        assert criteria[0]["id"] == "ac-1"
        assert criteria[1]["id"] == "ac-2"

    def test_skips_empty_acceptance_criteria(self):
        """Test that empty acceptance criteria are skipped."""
        phases = [
            make_phase(
                1,
                "Setup",
                [
                    make_task(1, 1, "Has criteria", "Valid criterion"),
                    make_task(1, 2, "No criteria", ""),
                    make_task(1, 3, "Whitespace only", "   "),
                ],
            )
        ]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 1
        assert "Valid criterion" in criteria[0]["description"]

    def test_skips_placeholder_criteria(self):
        """Test that placeholder criteria from unparseable tasks are skipped."""
        phases = [
            make_phase(
                1,
                "Setup",
                [
                    make_task(1, 1, "Real task", "Real criterion"),
                    make_task(1, 2, "Placeholder task", "Human verification"),
                ],
            )
        ]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 1
        assert "Real criterion" in criteria[0]["description"]

    def test_empty_phases(self):
        """Test with empty phases list."""
        criteria = extract_acceptance_criteria([])
        assert len(criteria) == 0

    def test_phase_with_no_tasks(self):
        """Test phase with no tasks."""
        phases = [make_phase(1, "Empty Phase", [])]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 0

    def test_criterion_includes_task_id(self):
        """Test that criterion description includes the task ID for traceability."""
        phases = [
            make_phase(
                3,
                "Testing",
                [make_task(3, 5, "Run tests", "All tests pass")],
            )
        ]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 1
        assert "[TASK-3-5]" in criteria[0]["description"]
        assert "All tests pass" in criteria[0]["description"]

    def test_preserves_criterion_text(self):
        """Test that criterion text is preserved exactly."""
        long_criterion = (
            "All unit tests pass with >90% coverage, "
            "integration tests complete successfully, "
            "and linting shows no errors"
        )
        phases = [
            make_phase(
                1,
                "Quality",
                [make_task(1, 1, "Quality checks", long_criterion)],
            )
        ]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 1
        assert long_criterion in criteria[0]["description"]

    def test_sequential_ids(self):
        """Test that criteria get sequential IDs across all phases."""
        phases = [
            make_phase(
                1,
                "Phase 1",
                [
                    make_task(1, 1, "T1", "C1"),
                    make_task(1, 2, "T2", "C2"),
                ],
            ),
            make_phase(
                2,
                "Phase 2",
                [
                    make_task(2, 1, "T3", "C3"),
                ],
            ),
        ]
        criteria = extract_acceptance_criteria(phases)
        assert len(criteria) == 3
        assert criteria[0]["id"] == "ac-1"
        assert criteria[1]["id"] == "ac-2"
        assert criteria[2]["id"] == "ac-3"

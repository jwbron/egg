"""Tests for ``plan_parser.validate_producer_only_slices`` (#2565).

The plan-phase ``task_planner`` can decompose work into slices that
include a terminal documentation- or test-only slice (e.g. a final
``Documentation`` slice whose only task is a ``documenter``). Such
slices burn the full implement-phase BRC roster (~8 agents) on work
the in-slice ``documenter`` / ``tester`` agents already produce in
every other slice. The validator runs at plan-time so the
``handle_consensus_propose_signal`` hook can NACK a planner before any
producer cycle is wasted.

These tests cover:

* Slices with at least one ``coder`` task pass.
* Slices where every task omits ``role`` pass — the execution-time
  default treats ``role=None`` as ``coder``.
* Slices with mixed ``coder`` + ``tester`` / ``documenter`` tasks pass.
* ``documenter``-only slices are rejected with the slice ID and name
  in the structured error.
* ``tester``-only slices are rejected.
* Mixed ``tester`` + ``documenter`` (no coder) slices are rejected.
* Empty-task slices are skipped (the parser injects a placeholder
  coder task for any unparseable phase, so an empty list at this
  point is benign).
* Multi-slice inputs report one error per offending slice and leave
  clean slices alone.
"""

from __future__ import annotations

from egg_contracts.models import Slice, Task
from egg_contracts.plan_parser import validate_producer_only_slices


def _slice(slice_id: str, name: str, tasks: list[Task]) -> Slice:
    return Slice(id=slice_id, name=name, tasks=tasks)


def _task(task_id: str, role: str | None) -> Task:
    return Task(
        id=task_id,
        description="task",
        acceptance_criteria="acc",
        files_affected=["src/foo.py"],
        role=role,
    )


class TestSlicesThatPass:
    """Slices that contain (effective) coder work — the validator
    must leave them alone."""

    def test_empty_input(self) -> None:
        assert validate_producer_only_slices([]) == []

    def test_explicit_coder_only(self) -> None:
        slices = [_slice("slice-1", "Setup", [_task("task-1-1", "coder")])]
        assert validate_producer_only_slices(slices) == []

    def test_role_none_counts_as_coder(self) -> None:
        # Tasks without an explicit role default to coder at execution
        # time. The rule fires only when the planner *explicitly*
        # assigned every task in the slice to a non-coder producer
        # role — a slice where every task omits ``role`` has implicit
        # coder work and passes.
        slices = [_slice("slice-1", "Setup", [_task("task-1-1", None)])]
        assert validate_producer_only_slices(slices) == []

    def test_mixed_coder_and_tester(self) -> None:
        slices = [
            _slice(
                "slice-1",
                "Setup with tests",
                [_task("task-1-1", "coder"), _task("task-1-2", "tester")],
            )
        ]
        assert validate_producer_only_slices(slices) == []

    def test_mixed_coder_and_documenter(self) -> None:
        slices = [
            _slice(
                "slice-1",
                "Setup with docs",
                [
                    _task("task-1-1", "coder"),
                    _task("task-1-2", "documenter"),
                ],
            )
        ]
        assert validate_producer_only_slices(slices) == []

    def test_coder_alongside_tester_and_documenter(self) -> None:
        slices = [
            _slice(
                "slice-1",
                "Full slice",
                [
                    _task("task-1-1", "coder"),
                    _task("task-1-2", "tester"),
                    _task("task-1-3", "documenter"),
                ],
            )
        ]
        assert validate_producer_only_slices(slices) == []

    def test_one_coder_one_role_none_passes(self) -> None:
        slices = [
            _slice(
                "slice-1",
                "Mixed",
                [_task("task-1-1", "coder"), _task("task-1-2", None)],
            )
        ]
        assert validate_producer_only_slices(slices) == []


class TestEmptyTaskSlice:
    """Slices with no tasks are skipped — the parser handles those
    by injecting a placeholder coder task elsewhere."""

    def test_empty_tasks_skipped(self) -> None:
        slices = [_slice("slice-1", "Empty", [])]
        assert validate_producer_only_slices(slices) == []


class TestProducerOnlySlicesRejected:
    """The structural anti-pattern #2565 targets — slices whose
    tasks are exclusively ``tester`` / ``documenter``."""

    def test_documenter_only_slice_rejected(self) -> None:
        slices = [
            _slice(
                "slice-5",
                "Documentation",
                [_task("task-5-1", "documenter")],
            )
        ]
        errors = validate_producer_only_slices(slices)
        assert len(errors) == 1
        assert "slice-5" in errors[0]
        assert "Documentation" in errors[0]
        assert "documenter" in errors[0]
        assert "no coder task" in errors[0]

    def test_tester_only_slice_rejected(self) -> None:
        slices = [
            _slice(
                "slice-3",
                "Write the tests",
                [_task("task-3-1", "tester")],
            )
        ]
        errors = validate_producer_only_slices(slices)
        assert len(errors) == 1
        assert "slice-3" in errors[0]
        assert "tester" in errors[0]

    def test_mixed_tester_documenter_no_coder_rejected(self) -> None:
        slices = [
            _slice(
                "slice-4",
                "Tests and docs",
                [
                    _task("task-4-1", "tester"),
                    _task("task-4-2", "documenter"),
                ],
            )
        ]
        errors = validate_producer_only_slices(slices)
        assert len(errors) == 1
        assert "slice-4" in errors[0]
        # Both roles surface in the role-distribution hint so the
        # planner sees what they actually emitted.
        assert "tester" in errors[0]
        assert "documenter" in errors[0]

    def test_multiple_documenter_tasks_in_one_slice(self) -> None:
        # Several docs tasks but still no coder — same rejection.
        slices = [
            _slice(
                "slice-5",
                "Documentation",
                [
                    _task("task-5-1", "documenter"),
                    _task("task-5-2", "documenter"),
                ],
            )
        ]
        errors = validate_producer_only_slices(slices)
        assert len(errors) == 1
        assert "slice-5" in errors[0]


class TestMultipleSlices:
    """Multi-slice inputs: one error per offending slice; clean slices
    are unaffected."""

    def test_one_offender_among_clean_slices(self) -> None:
        slices = [
            _slice("slice-1", "Setup", [_task("task-1-1", "coder")]),
            _slice("slice-2", "Wire up", [_task("task-2-1", "coder")]),
            _slice("slice-3", "Documentation", [_task("task-3-1", "documenter")]),
        ]
        errors = validate_producer_only_slices(slices)
        assert len(errors) == 1
        assert "slice-3" in errors[0]
        # Clean slices must NOT appear.
        assert "slice-1" not in errors[0]
        assert "slice-2" not in errors[0]

    def test_multiple_offenders_each_reported(self) -> None:
        slices = [
            _slice("slice-1", "Setup", [_task("task-1-1", "coder")]),
            _slice("slice-2", "Tests", [_task("task-2-1", "tester")]),
            _slice("slice-3", "Docs", [_task("task-3-1", "documenter")]),
        ]
        errors = validate_producer_only_slices(slices)
        assert len(errors) == 2
        joined = "\n".join(errors)
        assert "slice-2" in joined
        assert "slice-3" in joined
        assert "slice-1" not in joined

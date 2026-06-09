"""Tests for ``plan_parser.validate_slice_file_overlap`` (#3046).

The slice DAG must encode file-level dependencies: two slices that touch
overlapping ``files_affected`` must be **ordered** along the dependency
DAG (one a transitive ``dependencies`` ancestor of the other), so the
later slice's integration branch is forked from a base that already
contains the earlier slice's commits. Unordered overlapping slices fork
independently off the shared base and collide at integration — the #3023
modify/delete conflict.

These tests cover:

* Disjoint slices (any topology) pass cleanly — parallel slicing is the
  point of the feature.
* Overlapping slices left as parallel roots / siblings are rejected, one
  error per offending pair, naming the shared file(s).
* Overlapping slices ordered along a chain pass — including the
  transitive case where an intermediate slice is disjoint.
* The validator is cycle- and duplicate-id-safe (those are reported by
  ``validate_forest``, not here).
* The #3023 shape (three roots all touching ``consensus_wrapper.py``)
  reproduces three pairwise errors.
"""

from __future__ import annotations

from egg_contracts.models import Slice, Task
from egg_contracts.plan_parser import validate_slice_file_overlap

_TASK_SEQ = [0]


def _slice(id_: str, deps: list[str] | None = None, files: list[str] | None = None) -> Slice:
    """Build a slice whose single task declares ``files``.

    ``files=None`` yields a slice with no tasks (no overlap signal);
    ``files=[]`` yields a task with an empty file list (also no signal).
    """
    tasks: list[Task] = []
    if files is not None:
        _TASK_SEQ[0] += 1
        tasks = [Task(id=f"task-{_TASK_SEQ[0]}", description="d", files_affected=files)]
    return Slice(id=id_, name=f"slice {id_}", dependencies=deps or [], tasks=tasks)


class TestNoViolation:
    """Topologies that should pass overlap validation cleanly."""

    def test_empty_input(self) -> None:
        assert validate_slice_file_overlap([]) == []

    def test_single_slice(self) -> None:
        assert validate_slice_file_overlap([_slice("slice-1", [], ["a.py"])]) == []

    def test_disjoint_roots_run_parallel(self) -> None:
        # Two roots touching different files are safe to branch in parallel.
        slices = [
            _slice("slice-1", [], ["a.py"]),
            _slice("slice-2", [], ["b.py"]),
        ]
        assert validate_slice_file_overlap(slices) == []

    def test_overlap_with_direct_dependency_edge(self) -> None:
        # slice-2 depends on slice-1; sharing a file is fine because
        # slice-2's branch is cut from slice-1's tip.
        slices = [
            _slice("slice-1", [], ["x.py"]),
            _slice("slice-2", ["slice-1"], ["x.py"]),
        ]
        assert validate_slice_file_overlap(slices) == []

    def test_overlap_with_transitive_dependency_edge(self) -> None:
        # slice-1 → slice-2(disjoint) → slice-3; slice-1 and slice-3 share
        # a file but slice-3 transitively depends on slice-1, so the fork
        # point is still correct.
        slices = [
            _slice("slice-1", [], ["x.py"]),
            _slice("slice-2", ["slice-1"], ["y.py"]),
            _slice("slice-3", ["slice-2"], ["x.py"]),
        ]
        assert validate_slice_file_overlap(slices) == []

    def test_slices_without_files_have_no_signal(self) -> None:
        # No declared files → nothing to compare. Both the no-task and
        # empty-file-list shapes are silent.
        slices = [
            _slice("slice-1", [], None),
            _slice("slice-2", [], []),
        ]
        assert validate_slice_file_overlap(slices) == []


class TestOverlapRejection:
    """Overlapping slices with no ordering between them are rejected."""

    def test_two_unordered_roots_sharing_a_file(self) -> None:
        slices = [
            _slice("slice-1", [], ["shared.py", "a.py"]),
            _slice("slice-2", [], ["shared.py", "b.py"]),
        ]
        errors = validate_slice_file_overlap(slices)
        assert len(errors) == 1
        msg = errors[0]
        assert "slice-1" in msg and "slice-2" in msg
        assert "shared.py" in msg
        # The non-shared files must NOT appear — only the intersection.
        assert "a.py" not in msg and "b.py" not in msg
        assert "#3046" in msg

    def test_siblings_in_different_subtrees_collide(self) -> None:
        # slice-2 and slice-3 both depend on slice-1 (a tree, valid
        # forest) but touch the same file — neither depends on the other,
        # so they fork independently off slice-1 and collide.
        slices = [
            _slice("slice-1", [], ["root.py"]),
            _slice("slice-2", ["slice-1"], ["shared.py"]),
            _slice("slice-3", ["slice-1"], ["shared.py"]),
        ]
        errors = validate_slice_file_overlap(slices)
        assert len(errors) == 1
        assert "slice-2" in errors[0] and "slice-3" in errors[0]

    def test_modify_delete_shape_from_3023(self) -> None:
        # The #3023 incident: three roots, all touching
        # consensus_wrapper.py (slice-3 deletes it). Every pair conflicts.
        wrapper = "orchestrator/consensus_wrapper.py"
        slices = [
            _slice("slice-1", [], [wrapper, "orchestrator/phase_idle_budget.py"]),
            _slice("slice-2", [], [wrapper, "orchestrator/on_demand_spawner.py"]),
            _slice("slice-3", [], [wrapper, "orchestrator/startup_reconciliation.py"]),
        ]
        errors = validate_slice_file_overlap(slices)
        # 3 choose 2 = 3 offending pairs.
        assert len(errors) == 3
        assert all(wrapper in e for e in errors)

    def test_one_error_per_pair_not_per_file(self) -> None:
        # Two slices sharing two files yield ONE error listing both.
        slices = [
            _slice("slice-1", [], ["x.py", "y.py"]),
            _slice("slice-2", [], ["x.py", "y.py"]),
        ]
        errors = validate_slice_file_overlap(slices)
        assert len(errors) == 1
        assert "x.py" in errors[0] and "y.py" in errors[0]


class TestRobustness:
    """The validator must not crash on shapes validate_forest owns."""

    def test_cycle_is_not_flagged_here(self) -> None:
        # A 2-cycle makes the slices mutually reachable, so they count as
        # ordered (no overlap error). The cycle itself is validate_forest's
        # job — this validator must merely not loop forever.
        slices = [
            _slice("slice-1", ["slice-2"], ["x.py"]),
            _slice("slice-2", ["slice-1"], ["x.py"]),
        ]
        assert validate_slice_file_overlap(slices) == []

    def test_duplicate_ids_do_not_double_count(self) -> None:
        # Duplicate ids are reported by validate_forest; here we just make
        # sure the dedup keeps the pair walk well-defined.
        slices = [
            _slice("slice-1", [], ["x.py"]),
            _slice("slice-1", [], ["x.py"]),
            _slice("slice-2", [], ["x.py"]),
        ]
        errors = validate_slice_file_overlap(slices)
        # slice-1 (deduped) vs slice-2 → exactly one pair.
        assert len(errors) == 1

    def test_unknown_dependency_is_treated_as_absent(self) -> None:
        # slice-2 depends on a non-existent slice-9; that edge can't make
        # it an ancestor of slice-1, so an overlap with slice-1 still fires.
        slices = [
            _slice("slice-1", [], ["x.py"]),
            _slice("slice-2", ["slice-9"], ["x.py"]),
        ]
        errors = validate_slice_file_overlap(slices)
        assert len(errors) == 1

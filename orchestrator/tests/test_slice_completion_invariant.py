"""Slice-completion validity invariant (#3214).

Regression coverage for the false-complete class that wedged pipeline
``issue-3200``: an interior forest node (``slice-3``) was persisted as
``SliceStatus.COMPLETE`` while its only task was still ``pending``, it had
no integration branch, no recorded fork base, and it carried its parent's
commit SHA. ``_persist_slice_status_complete`` wrote that contradictory
state with no validation, so the slice-DAG driver skipped a slice that
never ran and the chain wedged with no successor — silently, for ~9h,
because nothing failed loud at the moment of the bad write.

The fix is a single completion-validity predicate
(``_validate_slice_completion_basis``) enforced at both the write
chokepoint (raise :class:`SliceCompletionInvariantError`) and the Layer-A
bootstrap read-trust point (alert + decline to trust). Its canonical
"work finished" half lives on the model as ``Slice.tasks_all_complete``
so it can't drift across the orchestrator. These tests lock the predicate
and the model property; the exact ``slice-3`` state is reproduced below as
the concrete anchor.
"""

from __future__ import annotations

from egg_contracts.models import Slice, SliceStatus, Task, TaskStatus
from routes.pipelines import (
    SliceCompletionInvariantError,
    _validate_slice_completion_basis,
)


def _slice(
    slice_id: str = "slice-3",
    *,
    task_statuses: list[TaskStatus] | None = None,
    status: SliceStatus = SliceStatus.PENDING,
    pr_number: int | None = None,
    integration_base_sha: str | None = None,
    parent_branch_at_creation: str | None = None,
    commit: str | None = None,
) -> Slice:
    """Build a Slice with one task per entry in ``task_statuses``."""
    statuses = task_statuses if task_statuses is not None else [TaskStatus.PENDING]
    return Slice(
        id=slice_id,
        name=f"Slice {slice_id}",
        status=status,
        pr_number=pr_number,
        integration_base_sha=integration_base_sha,
        parent_branch_at_creation=parent_branch_at_creation,
        commit=commit,
        tasks=[
            Task(id=f"task-3-{i + 1}", description="t", status=st, files_affected=[])
            for i, st in enumerate(statuses)
        ],
    )


# --------------------------------------------------------------------------
# Slice.tasks_all_complete — the canonical model-side predicate
# --------------------------------------------------------------------------


class TestTasksAllComplete:
    def test_all_complete(self) -> None:
        assert _slice(task_statuses=[TaskStatus.COMPLETE]).tasks_all_complete is True

    def test_multiple_all_complete(self) -> None:
        assert (
            _slice(task_statuses=[TaskStatus.COMPLETE, TaskStatus.COMPLETE]).tasks_all_complete
            is True
        )

    def test_one_pending(self) -> None:
        assert _slice(task_statuses=[TaskStatus.PENDING]).tasks_all_complete is False

    def test_mixed(self) -> None:
        assert (
            _slice(task_statuses=[TaskStatus.COMPLETE, TaskStatus.PENDING]).tasks_all_complete
            is False
        )

    def test_blocked_is_not_complete(self) -> None:
        assert _slice(task_statuses=[TaskStatus.BLOCKED]).tasks_all_complete is False

    def test_empty_task_list_is_not_complete(self) -> None:
        # A slice with nothing to do is a degenerate state a caller must
        # justify with a merged PR / verified consensus, not silently
        # treat as done.
        assert _slice(task_statuses=[]).tasks_all_complete is False


# --------------------------------------------------------------------------
# _validate_slice_completion_basis — accepts every legitimate basis
# --------------------------------------------------------------------------


class TestValidBases:
    def test_all_tasks_complete(self) -> None:
        assert _validate_slice_completion_basis(_slice(task_statuses=[TaskStatus.COMPLETE])) is None

    def test_pr_number_argument(self) -> None:
        # The run-loop PR-open caller passes pr_number; tasks may not all
        # be flipped yet on the in-memory contract.
        assert (
            _validate_slice_completion_basis(
                _slice(task_statuses=[TaskStatus.PENDING]), pr_number=3213
            )
            is None
        )

    def test_pr_number_on_model(self) -> None:
        assert (
            _validate_slice_completion_basis(
                _slice(task_statuses=[TaskStatus.PENDING], pr_number=3213)
            )
            is None
        )

    def test_basis_merged(self) -> None:
        # Layer-B bootstrap / run-loop merged-skip: ancestry-verified.
        assert (
            _validate_slice_completion_basis(
                _slice(task_statuses=[TaskStatus.PENDING]), basis="merged"
            )
            is None
        )

    def test_basis_consensus_complete(self) -> None:
        # Layer-C case 3 / run-loop success: consensus reached (PR not yet
        # opened, or its URL unparseable so pr_number is None).
        assert (
            _validate_slice_completion_basis(
                _slice(task_statuses=[TaskStatus.PENDING]), basis="consensus_complete"
            )
            is None
        )

    def test_forked_integration_branch(self) -> None:
        # A slice that recorded a fork base actually ran its integration
        # branch (#2871) — valid even with tasks still pending on the
        # in-memory contract and an unparseable (None) PR number.
        assert (
            _validate_slice_completion_basis(
                _slice(
                    task_statuses=[TaskStatus.PENDING],
                    integration_base_sha="a" * 40,
                )
            )
            is None
        )


# --------------------------------------------------------------------------
# _validate_slice_completion_basis — rejects the false-complete class
# --------------------------------------------------------------------------


class TestInvalidBases:
    def test_pending_task_no_pr_no_basis_no_fork_is_rejected(self) -> None:
        reason = _validate_slice_completion_basis(_slice(task_statuses=[TaskStatus.PENDING]))
        assert reason is not None
        assert "slice-3" in reason

    def test_unknown_basis_does_not_pass(self) -> None:
        # A caller-declared basis only counts if it is a recognised,
        # verified basis — a typo / novel string must not slip through.
        reason = _validate_slice_completion_basis(
            _slice(task_statuses=[TaskStatus.PENDING]), basis="bogus"
        )
        assert reason is not None

    def test_empty_tasks_no_pr_no_basis_no_fork_is_rejected(self) -> None:
        reason = _validate_slice_completion_basis(_slice(task_statuses=[]))
        assert reason is not None

    def test_mixed_tasks_no_pr_no_basis_no_fork_is_rejected(self) -> None:
        reason = _validate_slice_completion_basis(
            _slice(task_statuses=[TaskStatus.COMPLETE, TaskStatus.PENDING])
        )
        assert reason is not None

    def test_exact_slice3_production_state_is_flagged(self) -> None:
        """The literal state read off pipeline ``issue-3200`` contract:
        an interior node persisted COMPLETE with a pending task, no PR,
        no fork base, no parent branch, carrying its parent's commit SHA.
        The predicate must flag it — this is the wedge's root state.
        """
        slice3 = _slice(
            slice_id="slice-3",
            task_statuses=[TaskStatus.PENDING],
            status=SliceStatus.COMPLETE,  # the corrupt on-disk status
            pr_number=None,
            integration_base_sha=None,
            parent_branch_at_creation=None,
            commit="9e3eea6197755372f57efa50a66178bd4a5a6c16",  # parent's commit
        )
        reason = _validate_slice_completion_basis(slice3, pr_number=slice3.pr_number)
        assert reason is not None, "the slice-3 false-complete signature must be rejected"


# --------------------------------------------------------------------------
# SliceCompletionInvariantError contract
# --------------------------------------------------------------------------


class TestInvariantError:
    def test_is_runtime_error(self) -> None:
        # Propagates as a hard failure, not swallowed into a best-effort
        # save handler.
        assert issubclass(SliceCompletionInvariantError, RuntimeError)

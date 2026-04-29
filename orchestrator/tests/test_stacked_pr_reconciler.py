"""Stacked-PR reconciler tests (#2137 TASK-5-4).

Pure-function tests for ``find_orphaned_child_prs`` and the
``reconcile_once`` driver. The reconciler is intentionally
side-effect-free at import time and decoupled from the gateway via
three callable seams; that makes it easy to drive deterministic
fakes here without spinning up a fake GitHub.

Coverage:

* Empty contracts return no orphans.
* Roots (slices with no ``parent_branch_at_creation``) are skipped —
  their base is the pipeline branch which the reconciler is not
  responsible for.
* A child slice whose base IS still in ``extant_branches`` is skipped
  (GitHub auto-retarget already did its job).
* A child slice whose base is missing produces one
  ``OrphanedChildPR`` with ``intended_new_base`` pulled from
  ``Slice.parent_branch_at_creation`` — explicitly NOT the PR's own
  metadata, so a stale rebase under the PR can't poison the result.
* Slices whose PR has not yet been opened (no matching head) are
  silently skipped — the next reconciliation pass picks them up
  once the PR exists.
* ``reconcile_once`` invokes the rebase callable once per orphan
  and counts successes / failures; rebase exceptions are caught
  and counted, never propagated.
* Reconciler is idempotent — a call with no orphans returns a
  zero-count result without touching the rebase callable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# sys.path setup matches test_concurrent_executor_staging_branch.py.
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"
for _p in (_orchestrator_path, _shared_path):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from egg_contracts.models import Contract, IssueInfo, Slice  # noqa: E402
from stacked_pr_reconciler import (  # noqa: E402
    OrphanedChildPR,
    ReconciliationResult,
    find_orphaned_child_prs,
    reconcile_once,
)


def _slice(
    id_: str,
    *,
    deps: list[str] | None = None,
    parent_branch: str | None = None,
) -> Slice:
    return Slice(
        id=id_,
        name=f"slice {id_}",
        dependencies=deps or [],
        parent_branch_at_creation=parent_branch,
    )


def _contract(*slices: Slice) -> Contract:
    return Contract(
        issue=IssueInfo(number=2137, title="t", url="u"),
        slices=list(slices),
    )


def _pr(
    *,
    number: int,
    head: str,
    base: str,
) -> dict[str, Any]:
    """Build a PR record matching ``GatewayClient.list_open_prs``'s shape.

    The producer normalises GitHub's ``headRefName``/``baseRefName``
    fields to ``head_ref``/``base_ref``. Earlier drafts of these
    tests fed in ``head``/``base`` keys that matched a since-fixed
    consumer bug; we now drive the documented contract so a future
    regression of the same shape mismatch surfaces here instead of
    in production.
    """
    return {"number": number, "head_ref": head, "base_ref": base}


# ---------- find_orphaned_child_prs ----------


class TestFindOrphans:
    def test_empty_contract_no_orphans(self) -> None:
        contract = _contract()
        assert find_orphaned_child_prs(contract, [], set()) == []

    def test_root_slice_with_no_parent_branch_skipped(self) -> None:
        # parent_branch_at_creation is None → the slice is a root and
        # the reconciler ignores it. (Roots target the pipeline branch
        # which is the project's own integration target.)
        contract = _contract(_slice("slice-1", parent_branch=None))
        prs = [_pr(number=10, head="egg/issue-2137/slice-1", base="main")]
        assert find_orphaned_child_prs(contract, prs, set()) == []

    def test_child_with_extant_base_skipped(self) -> None:
        # The base branch still exists on origin → GitHub auto-retarget
        # is going to handle this without our help.
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        extant = {"egg/issue-2137/slice-1"}
        assert find_orphaned_child_prs(contract, prs, extant) == []

    def test_child_with_deleted_base_surfaces_orphan(self) -> None:
        # The parent slice's branch was just deleted (the merge
        # cascade) — that's why we're orphaned. The reconciler must
        # walk up the DAG to find an extant ancestor and fall back
        # to the pipeline branch when the chain has been entirely
        # deleted (or, as here, the parent slice isn't in the
        # contract).
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        extant: set[str] = set()  # parent base no longer on origin
        orphans = find_orphaned_child_prs(contract, prs, extant)
        assert len(orphans) == 1
        orphan = orphans[0]
        assert isinstance(orphan, OrphanedChildPR)
        assert orphan.slice_id == "slice-2"
        assert orphan.pr_number == 11
        assert orphan.branch == "egg/issue-2137/slice-2"
        assert orphan.deleted_base == "egg/issue-2137/slice-1"
        # Walk-up fallback: the parent's branch is gone (and slice-1
        # isn't in the contract here), so the pipeline branch is the
        # last-resort target.
        assert orphan.intended_new_base == "egg/issue-2137"

    def test_intended_new_base_walks_up_to_extant_ancestor(self) -> None:
        # A 3-level chain (slice-1 → slice-2 → slice-3): when
        # slice-2's branch is deleted (the immediate parent), the
        # reconciler must walk up to slice-1 — its branch is still
        # alive, so it's the right rebase target.
        contract = _contract(
            _slice("slice-1"),
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            ),
            _slice(
                "slice-3",
                deps=["slice-2"],
                parent_branch="egg/issue-2137/slice-2",
            ),
        )
        prs = [
            _pr(
                number=12,
                head="egg/issue-2137/slice-3",
                base="egg/issue-2137/slice-2",
            )
        ]
        # slice-2's branch deleted; slice-1's still alive.
        extant: set[str] = {"egg/issue-2137/slice-1"}
        orphans = find_orphaned_child_prs(contract, prs, extant)
        assert len(orphans) == 1
        # Walk: slice-3's parent is slice-2 (deleted) → walk to
        # slice-1 (extant) → use it.
        assert orphans[0].intended_new_base == "egg/issue-2137/slice-1"

    def test_intended_new_base_falls_back_to_pipeline_branch(self) -> None:
        # All ancestors deleted: the pipeline branch is the safe
        # fallback because it's never deleted by the stacked-PR
        # flow.
        contract = _contract(
            _slice("slice-1"),
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            ),
            _slice(
                "slice-3",
                deps=["slice-2"],
                parent_branch="egg/issue-2137/slice-2",
            ),
        )
        prs = [
            _pr(
                number=12,
                head="egg/issue-2137/slice-3",
                base="egg/issue-2137/slice-2",
            )
        ]
        # Both ancestors gone.
        extant: set[str] = set()
        orphans = find_orphaned_child_prs(contract, prs, extant)
        assert len(orphans) == 1
        assert orphans[0].intended_new_base == "egg/issue-2137"

    def test_intended_new_base_ignores_pr_metadata(self) -> None:
        # The PR's own ``base`` may have been modified by an out-of-
        # band action; the reconciler must compute its target from
        # the contract DAG and ``extant_branches``, not from the
        # PR's metadata.
        contract = _contract(
            _slice("slice-1"),
            _slice(
                "slice-2",
                deps=["slice-1"],
                # The TRUE parent branch the slice was created off of:
                parent_branch="egg/issue-2137/slice-1",
            ),
        )
        # PR has been retargeted to a misleading base by some
        # operator action — but slice-2's actual parent slice-1 is
        # still alive on origin, so that's where we should rebase.
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/some-other-thing",
            )
        ]
        extant: set[str] = {"egg/issue-2137/slice-1"}
        orphans = find_orphaned_child_prs(contract, prs, extant)
        assert len(orphans) == 1
        # ``intended_new_base`` is computed from the DAG + extant
        # set, NOT from the PR's own base metadata.
        assert orphans[0].intended_new_base == "egg/issue-2137/slice-1"

    def test_no_pr_for_slice_silently_skipped(self) -> None:
        # The slice is set up but its PR has not yet been opened —
        # the reconciler is patient and just waits for the next pass.
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        # No PR matches the head ``egg/issue-2137/slice-2``.
        prs: list[dict[str, Any]] = []
        assert find_orphaned_child_prs(contract, prs, set()) == []

    def test_slice_without_provisioned_branch_is_skipped(self) -> None:
        # Slice has a parent dep but ``parent_branch_at_creation`` is
        # still None — the integration branch hasn't been provisioned
        # yet. Wait for TASK-4-2 to populate the field before we
        # reconcile.
        contract = _contract(_slice("slice-2", deps=["slice-1"], parent_branch=None))
        prs = [_pr(number=11, head="egg/issue-2137/slice-2", base="any")]
        assert find_orphaned_child_prs(contract, prs, set()) == []

    def test_pr_with_non_string_base_skipped(self) -> None:
        # Defensive: a malformed PR record (base is not a string)
        # must not crash the reconciler. The pass should silently
        # skip and move on.
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        prs = [{"number": 11, "head_ref": "egg/issue-2137/slice-2", "base_ref": None}]
        # Should not raise.
        orphans = find_orphaned_child_prs(contract, prs, set())
        # And should NOT count this PR as orphaned (we have no way to
        # know its base disappeared if there's no string to check).
        assert orphans == []

    def test_pr_with_legacy_head_base_keys_still_accepted(self) -> None:
        # Backwards-compat: callers built before the producer/consumer
        # shape was aligned passed ``head``/``base`` keys. The
        # reconciler should still match those records so any
        # out-of-tree wiring keeps working while the canonical
        # contract is the ``_ref`` form.
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        prs: list[dict[str, Any]] = [
            {
                "number": 11,
                "head": "egg/issue-2137/slice-2",
                "base": "egg/issue-2137/slice-1",
            }
        ]
        orphans = find_orphaned_child_prs(contract, prs, set())
        assert len(orphans) == 1
        assert orphans[0].pr_number == 11
        assert orphans[0].deleted_base == "egg/issue-2137/slice-1"

    def test_pr_with_missing_number_dropped(self) -> None:
        # A PR record without a real ``number`` cannot be retargeted
        # via ``gh pr edit``, so the reconciler must drop it rather
        # than emit a phantom orphan with ``pr_number=0``.
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        prs: list[dict[str, Any]] = [
            {
                "head_ref": "egg/issue-2137/slice-2",
                "base_ref": "egg/issue-2137/slice-1",
            }
        ]
        orphans = find_orphaned_child_prs(contract, prs, set())
        assert orphans == []

    def test_pr_with_zero_number_dropped(self) -> None:
        # Same defence: an explicit ``"number": 0`` is rejected.
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        prs = [_pr(number=0, head="egg/issue-2137/slice-2", base="egg/issue-2137/slice-1")]
        assert find_orphaned_child_prs(contract, prs, set()) == []


# ---------- reconcile_once ----------


class _RecordingRebaser:
    def __init__(self, *, return_value: bool = True, raise_exc: Exception | None = None) -> None:
        self.calls: list[OrphanedChildPR] = []
        self._return_value = return_value
        self._raise = raise_exc

    def __call__(self, orphan: OrphanedChildPR) -> bool:
        self.calls.append(orphan)
        if self._raise is not None:
            raise self._raise
        return self._return_value


class TestProducerConsumerContract:
    """The reconciler must consume ``GatewayClient.list_open_prs``'s
    actual output without a translation layer.

    Earlier drafts of the reconciler matched dicts with ``head``
    and ``base`` keys while the gateway producer normalised to
    ``head_ref`` and ``base_ref``. The mismatch made
    ``find_orphaned_child_prs`` a silent no-op in production. Lock
    the contract here so any future drift fails this test instead
    of slipping through to the reconciler thread.
    """

    def test_producer_normalised_shape_round_trips(self) -> None:
        # Mirror the exact shape that
        # ``GatewayClient.list_open_prs`` produces (see
        # ``orchestrator/gateway_client.py`` — keys are
        # ``number`` (int), ``head_ref`` (str), ``base_ref`` (str)).
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )
        producer_shape: list[dict[str, Any]] = [
            {
                "number": 11,
                "head_ref": "egg/issue-2137/slice-2",
                "base_ref": "egg/issue-2137/slice-1",
            }
        ]
        orphans = find_orphaned_child_prs(contract, producer_shape, set())
        assert len(orphans) == 1
        # ``pr_number`` MUST be the real PR number from the producer
        # — not silently defaulted to 0 — so the production rebase
        # bridge can retarget the PR.
        assert orphans[0].pr_number == 11
        assert orphans[0].deleted_base == "egg/issue-2137/slice-1"


class TestReconcileOnce:
    def _orphaned_setup(self) -> Contract:
        return _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            )
        )

    def test_no_orphans_returns_zero_counts(self) -> None:
        rebaser = _RecordingRebaser()
        result = reconcile_once(
            _contract(),
            list_open_prs=lambda: [],
            list_extant_branches=lambda: set(),
            rebase_onto=rebaser,
        )
        assert isinstance(result, ReconciliationResult)
        assert result.orphans_detected == 0
        assert result.rebases_attempted == 0
        assert result.rebases_succeeded == 0
        assert result.rebases_failed == 0
        # The rebase callable must NOT be invoked when there's nothing
        # to do — the reconciler is supposed to be cheap on the
        # quiet path.
        assert rebaser.calls == []

    def test_one_orphan_one_rebase_called(self) -> None:
        contract = self._orphaned_setup()
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        rebaser = _RecordingRebaser(return_value=True)
        result = reconcile_once(
            contract,
            list_open_prs=lambda: prs,
            list_extant_branches=lambda: set(),
            rebase_onto=rebaser,
        )
        assert result.orphans_detected == 1
        assert result.rebases_attempted == 1
        assert result.rebases_succeeded == 1
        assert result.rebases_failed == 0
        assert len(rebaser.calls) == 1
        called = rebaser.calls[0]
        assert called.branch == "egg/issue-2137/slice-2"
        # Walk-up fallback: slice-1 isn't in the contract here, so
        # the pipeline branch is the last-resort rebase target.
        assert called.intended_new_base == "egg/issue-2137"
        assert called.deleted_base == "egg/issue-2137/slice-1"
        # The orphan now carries the PR number so the production
        # bridge can retarget the PR after the rebase.
        assert called.pr_number == 11

    def test_rebase_returning_false_counts_failure(self) -> None:
        contract = self._orphaned_setup()
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        rebaser = _RecordingRebaser(return_value=False)
        result = reconcile_once(
            contract,
            list_open_prs=lambda: prs,
            list_extant_branches=lambda: set(),
            rebase_onto=rebaser,
        )
        assert result.rebases_succeeded == 0
        assert result.rebases_failed == 1

    def test_rebase_exception_counted_not_propagated(self) -> None:
        # Decision-15 invariant: the reconciler must not crash on a
        # single rebase failure. Counted, logged, moved on.
        contract = self._orphaned_setup()
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        rebaser = _RecordingRebaser(raise_exc=RuntimeError("gateway down"))
        # Should NOT raise.
        result = reconcile_once(
            contract,
            list_open_prs=lambda: prs,
            list_extant_branches=lambda: set(),
            rebase_onto=rebaser,
        )
        assert result.rebases_failed == 1
        assert result.rebases_succeeded == 0

    def test_callables_invoked_each_pass(self) -> None:
        # The contract is a snapshot; the open-PR / extant-branch
        # callables are invoked fresh each pass so the reconciler
        # always sees the latest GitHub state.
        contract = self._orphaned_setup()
        list_prs_calls = [0]
        list_branches_calls = [0]

        def _list_prs() -> list[dict[str, Any]]:
            list_prs_calls[0] += 1
            return []

        def _list_branches() -> set[str]:
            list_branches_calls[0] += 1
            return set()

        rebaser = _RecordingRebaser()
        reconcile_once(
            contract,
            list_open_prs=_list_prs,
            list_extant_branches=_list_branches,
            rebase_onto=rebaser,
        )
        assert list_prs_calls[0] == 1
        assert list_branches_calls[0] == 1

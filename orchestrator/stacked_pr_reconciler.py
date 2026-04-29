"""Stacked-PR rebase reconciler for the slice DAG (#2137).

Per-slice PRs stack along the slice DAG: root slices target the
pipeline branch (``egg/issue-N``); child slices target the parent
slice's integration branch (``egg/issue-N/slice-{parent_M}``). When
a parent PR is merged GitHub auto-retargets child PRs in the common
case (decision-16 hybrid), but edge cases (force-pushes, manual
branch deletion) leave the child PR pointing at a deleted base.

The reconciler runs on a fixed cadence (default 30 s, env var
``EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS``) and:

1. Lists open child slice PRs whose ``base`` branch no longer exists
   on origin.
2. Computes the intended new base from
   ``Slice.parent_branch_at_creation`` (recorded by TASK-4-2 when
   the integration branch was provisioned).
3. Calls ``GatewayClient.rebase_onto`` (orchestrator-side bridge
   in :mod:`orchestrator.gateway_client`) which forwards the
   request through the gateway's existing per-agent allowlist
   plumbing — internally constructed via
   :func:`gateway.git_client.build_rebase_onto_args` and submitted
   through the same ``/api/v1/git`` endpoint that authorised
   agents use today. No new privileged orchestrator-role
   endpoint is introduced (refine-phase decision-15).

This module is pure-Python and side-effect-free at import time —
the orchestrator's pipeline run loop wires up an async timer that
calls :func:`reconcile_once` on the chosen cadence, so the unit
tests can exercise the matching logic deterministically without
actually opening a TCP connection to GitHub.

Decision-15 invariant: the reconciler must NOT introduce a new
privileged orchestrator-role endpoint. The ``rebase_onto`` helper
is a narrow extension of the existing per-agent rebase allowlist;
the reconciler authenticates as the existing low-privilege agent
identity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from egg_contracts.models import Contract

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrphanedChildPR:
    """A child slice PR whose base branch has been deleted.

    The reconciler builds one of these per detected orphan and
    issues a single ``rebase_onto`` call per object. The
    ``intended_new_base`` is sourced from
    ``Slice.parent_branch_at_creation``, NOT inferred from the
    PR's own metadata — this keeps the reconciler robust against
    cases where the parent slice's branch has been renamed or
    rebased out from under the PR.
    """

    slice_id: str
    pr_number: int
    branch: str
    deleted_base: str
    intended_new_base: str


@dataclass(frozen=True)
class ReconciliationResult:
    """Snapshot of a single reconciliation pass."""

    orphans_detected: int
    rebases_attempted: int
    rebases_succeeded: int
    rebases_failed: int


def find_orphaned_child_prs(
    contract: Contract,
    open_prs: list[dict[str, Any]],
    extant_branches: set[str],
) -> list[OrphanedChildPR]:
    """Return the orphans whose base branch has disappeared.

    Pure function: given the contract, a list of open PRs (each a
    dict with at least ``number``, ``head_ref``, and ``base_ref``
    keys — the normalised shape produced by
    :meth:`GatewayClient.list_open_prs`), and the set of branch
    names known to exist on origin, return one
    :class:`OrphanedChildPR` per detected orphan.

    A child slice PR is orphaned when:

    1. The slice's ``parent_branch_at_creation`` is set (i.e. it's
       not a root slice that targets the pipeline branch directly).
    2. There IS an open PR with a head branch matching the slice's
       integration branch.
    3. The PR's base branch is NOT in ``extant_branches``.

    Roots, completed slices, and slices whose base still exists
    are silently skipped so the reconciler is idempotent on each
    pass.

    PR records missing ``head_ref`` (or its legacy ``head`` alias),
    a real integer ``number``, or ``base_ref`` are dropped — a
    malformed record is treated as "no PR" rather than coerced
    into a phantom orphan with ``pr_number=0``.
    """
    if not contract.slices:
        return []

    # Index PRs by head branch for O(1) lookup. Accept both the
    # canonical ``head_ref``/``base_ref`` shape (from
    # ``GatewayClient.list_open_prs``) and the legacy
    # ``head``/``base`` shape that earlier drafts of the reconciler
    # consumed; the latter keeps any out-of-tree caller working
    # while the documented contract is the ``_ref`` form.
    pr_by_head: dict[str, dict[str, Any]] = {}
    for pr in open_prs:
        head = pr.get("head_ref") or pr.get("head")
        if isinstance(head, str) and head:
            pr_by_head[head] = pr

    orphans: list[OrphanedChildPR] = []
    issue_number = contract.issue.number if contract.issue is not None else None
    pipeline_id = contract.contract_key
    issue_branch = f"egg/issue-{issue_number}" if issue_number else f"egg/{pipeline_id}"

    for slice_ in contract.slices:
        parent = slice_.parent_branch_at_creation
        if parent is None:
            continue  # not yet provisioned
        slice_branch = f"{issue_branch}/{slice_.id}"
        pr = pr_by_head.get(slice_branch)
        if pr is None:
            continue  # slice's PR hasn't been opened yet (or is closed)
        deleted_base = pr.get("base_ref") or pr.get("base")
        if not isinstance(deleted_base, str) or deleted_base in extant_branches:
            continue  # base still alive — GitHub auto-retarget did its job
        raw_number = pr.get("number")
        if isinstance(raw_number, bool) or not isinstance(raw_number, int) or raw_number <= 0:
            # A malformed record without a real PR number cannot be
            # retargeted by ``rebase_onto``. Drop it; the next
            # reconciler tick will pick the slice up if the PR
            # surfaces with a valid number.
            logger.debug(
                "stacked_pr_reconciler: dropping PR record with invalid 'number'",
                extra={"slice_id": slice_.id, "head": slice_branch, "raw_number": raw_number},
            )
            continue
        orphans.append(
            OrphanedChildPR(
                slice_id=slice_.id,
                pr_number=int(raw_number),
                branch=slice_branch,
                deleted_base=deleted_base,
                intended_new_base=parent,
            )
        )
    return orphans


def reconcile_once(
    contract: Contract,
    *,
    list_open_prs: Callable[[], list[dict[str, Any]]],
    list_extant_branches: Callable[[], set[str]],
    rebase_onto: Callable[[OrphanedChildPR], bool],
) -> ReconciliationResult:
    """Run a single reconciliation pass.

    The three callables decouple this function from the actual
    gateway client / GitHub API, so the unit tests can substitute
    deterministic fakes. In production they wrap
    :meth:`GatewayClient.list_open_prs`,
    :meth:`GatewayClient.list_remote_branches`, and the
    ``rebase_onto`` helper landing in TASK-5-2.

    The ``rebase_onto`` callable receives the full
    :class:`OrphanedChildPR` so the production wiring can use
    ``pr_number`` to retarget the PR's base after the local
    rebase. Returning ``True`` indicates the orphan is fully
    healed (rebased + pushed + retargeted on GitHub); ``False``
    counts as ``rebases_failed`` and is retried on the next tick.

    Returns a :class:`ReconciliationResult` snapshot for telemetry.
    """
    open_prs = list_open_prs()
    extant = list_extant_branches()
    orphans = find_orphaned_child_prs(contract, open_prs, extant)

    succeeded = 0
    failed = 0
    for orphan in orphans:
        try:
            ok = rebase_onto(orphan)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "stacked_pr_reconciler_rebase_failed",
                extra={
                    "slice_id": orphan.slice_id,
                    "pr_number": orphan.pr_number,
                    "deleted_base": orphan.deleted_base,
                    "new_base": orphan.intended_new_base,
                    "error": str(exc),
                },
            )
            failed += 1
            continue
        if ok:
            succeeded += 1
        else:
            failed += 1

    return ReconciliationResult(
        orphans_detected=len(orphans),
        rebases_attempted=len(orphans),
        rebases_succeeded=succeeded,
        rebases_failed=failed,
    )


__all__ = (
    "OrphanedChildPR",
    "ReconciliationResult",
    "find_orphaned_child_prs",
    "reconcile_once",
)

"""Stacked-PR reconciler context-branch fallback tests (#2548 task-2-3).

The reconciler's last-resort fallback was previously hard-coded to the
pipeline branch (``egg/<id>/work``).  Under #2548 slice-1 stacks on the
dedicated context branch (``egg/<id>/context``) which carries the
refine + plan analysis docs and BRC consensus transcripts, so an
orphaned slice-1 should retarget there to keep those artifacts
reachable through the slice PR diff.

The new resolution order in ``_resolve_extant_new_base`` is:

1. Walk the slice DAG via ``dependencies[0]`` until an extant ancestor
   branch is found.
2. If the chain is exhausted, prefer ``contract.pr.context_branch``
   when set AND still present in ``extant_branches``.
3. Final fallback: ``pipeline_branch`` (legacy / pre-#2548 behavior).

These tests pin every interesting transition in that decision tree:

* Step 2 fires when the chain is exhausted and the context branch IS
  extant.
* Step 2 falls through to step 3 when the context branch is set but
  the branch has been deleted from origin.
* Step 2 is bypassed entirely when the contract has no PR metadata or
  the field is unset / empty (legacy fallback, no regression).
* Step 1 still wins over step 2 — when the DAG walk finds an extant
  ancestor we use that, *not* the context branch.
* The empty-string falsy case is treated as "unset" rather than as a
  literal branch name (so an accidentally-blank field doesn't push
  PRs onto a non-existent ``""`` branch).

These tests live alongside ``test_stacked_pr_reconciler.py`` so a
future refactor of the resolver has both surfaces covered without
having to rediscover the cross-test invariants.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# sys.path setup matches test_stacked_pr_reconciler.py.
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"
for _p in (_orchestrator_path, _shared_path):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from egg_contracts.models import Contract, IssueInfo, PRMetadata, Slice  # noqa: E402
from stacked_pr_reconciler import (  # noqa: E402
    _resolve_extant_new_base,
    find_orphaned_child_prs,
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


def _contract(
    *slices: Slice,
    pipeline_id: str | None = None,
    context_branch: str | None = None,
) -> Contract:
    """Build a Contract with optional ``pr.context_branch`` set."""
    pr = None
    if context_branch is not None:
        # Provide a non-empty title so PRMetadata's ``min_length=1``
        # constraint on ``title`` is satisfied — title is irrelevant
        # to these tests but Pydantic enforces it.
        pr = PRMetadata(title="t", description="", context_branch=context_branch)
    return Contract(
        issue=IssueInfo(number=2137, title="t", url="u"),
        pipeline_id=pipeline_id,
        slices=list(slices),
        pr=pr,
    )


def _pr(*, number: int, head: str, base: str) -> dict[str, Any]:
    return {"number": number, "head_ref": head, "base_ref": base}


# ---------- _resolve_extant_new_base (unit) ----------


class TestResolveExtantNewBaseContextBranch:
    """Unit-test the resolver directly so the decision tree is locked
    independently of ``find_orphaned_child_prs``'s wiring."""

    def test_dag_walk_wins_over_context_branch(self) -> None:
        """An extant DAG ancestor is preferred over the context branch
        — the context branch is only the cascade fallback when every
        ancestor is gone.  Without this property, a healthy stack
        would be force-rebased onto the context branch and lose the
        useful intermediate context."""
        s1 = _slice("slice-1")
        s2 = _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1")
        s3 = _slice("slice-3", deps=["slice-2"], parent_branch="egg/issue-2137/slice-2")
        slices_by_id = {s.id: s for s in [s1, s2, s3]}
        # slice-2 deleted (merge cascade), slice-1 still alive.
        extant: set[str] = {"egg/issue-2137/slice-1", "egg/issue-2137/context"}
        result = _resolve_extant_new_base(
            s3,
            slices_by_id,
            extant,
            "egg/issue-2137",
            "egg/issue-2137/work",
            context_branch="egg/issue-2137/context",
        )
        # DAG walk found slice-1 → use it; context branch is irrelevant.
        assert result == "egg/issue-2137/slice-1"

    def test_chain_exhausted_with_extant_context_branch_uses_context(self) -> None:
        """When every ancestor's branch has been deleted but the
        context branch is still alive on origin, retarget there.
        This is the headline #2548 task-2-3 behavior — the orphaned
        slice-1 case (parent gone, context branch stable)."""
        s1 = _slice("slice-1")
        s2 = _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1")
        slices_by_id = {s.id: s for s in [s1, s2]}
        extant: set[str] = {"egg/issue-2137/context"}  # ancestors all gone
        result = _resolve_extant_new_base(
            s2,
            slices_by_id,
            extant,
            "egg/issue-2137",
            "egg/issue-2137/work",
            context_branch="egg/issue-2137/context",
        )
        assert result == "egg/issue-2137/context"

    def test_context_branch_set_but_deleted_falls_through_to_pipeline(self) -> None:
        """If the context branch is set on the contract but has been
        deleted from origin (e.g. a manual cleanup wiped it), the
        resolver MUST fall through to the pipeline branch — pushing
        PRs onto a non-existent ``egg/<id>/context`` would be a
        production-breaking corruption."""
        s1 = _slice("slice-1")
        s2 = _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1")
        slices_by_id = {s.id: s for s in [s1, s2]}
        # Context branch set on contract but NOT in extant_branches.
        extant: set[str] = set()
        result = _resolve_extant_new_base(
            s2,
            slices_by_id,
            extant,
            "egg/issue-2137",
            "egg/issue-2137/work",
            context_branch="egg/issue-2137/context",
        )
        assert result == "egg/issue-2137/work", (
            "Resolver MUST fall through to pipeline branch when context "
            "branch is missing from extant_branches"
        )

    def test_no_context_branch_legacy_fallback_unchanged(self) -> None:
        """``context_branch=None`` (no PR metadata, or pre-#2548
        contract): the resolver must produce the pre-#2548 result —
        pipeline branch — exactly.  Pin this so a future refactor
        cannot accidentally regress legacy pipelines."""
        s1 = _slice("slice-1")
        s2 = _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1")
        slices_by_id = {s.id: s for s in [s1, s2]}
        result = _resolve_extant_new_base(
            s2,
            slices_by_id,
            set(),
            "egg/issue-2137",
            "egg/issue-2137/work",
            context_branch=None,
        )
        assert result == "egg/issue-2137/work"

    def test_empty_string_context_branch_treated_as_unset(self) -> None:
        """An empty-string ``context_branch`` is *falsy* per the
        spec's ``if context_branch and context_branch in extant_branches``
        guard, so the resolver must fall back to the pipeline branch
        rather than try to retarget to ``""``.  An empty branch name
        would either fail the gateway request or, worse, produce a
        mis-targeted PR — both unacceptable."""
        s1 = _slice("slice-1")
        s2 = _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1")
        slices_by_id = {s.id: s for s in [s1, s2]}
        result = _resolve_extant_new_base(
            s2,
            slices_by_id,
            set(),
            "egg/issue-2137",
            "egg/issue-2137/work",
            context_branch="",
        )
        assert result == "egg/issue-2137/work"

    def test_default_context_branch_kwarg_is_none(self) -> None:
        """The ``context_branch`` kwarg defaults to ``None`` so legacy
        callers (and the unit-test surface in
        ``test_stacked_pr_reconciler.py``) continue to behave as
        pre-#2548.  Pin the default value here so a future refactor
        cannot quietly flip it."""
        s1 = _slice("slice-1")
        s2 = _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137/slice-1")
        slices_by_id = {s.id: s for s in [s1, s2]}
        # No context_branch kwarg passed at all.
        result = _resolve_extant_new_base(
            s2,
            slices_by_id,
            set(),
            "egg/issue-2137",
            "egg/issue-2137/work",
        )
        assert result == "egg/issue-2137/work"

    def test_context_branch_qualified_pipeline_id_preserved(self) -> None:
        """A qualified pipeline (``issue-2137-v3``) routes its
        context branch under the qualified namespace
        (``egg/issue-2137-v3/context``).  The resolver does not
        rewrite the value — it must pass through whatever the
        contract carries."""
        s1 = _slice("slice-1")
        s2 = _slice("slice-2", deps=["slice-1"], parent_branch="egg/issue-2137-v3/slice-1")
        slices_by_id = {s.id: s for s in [s1, s2]}
        extant: set[str] = {"egg/issue-2137-v3/context"}
        result = _resolve_extant_new_base(
            s2,
            slices_by_id,
            extant,
            "egg/issue-2137-v3",
            "egg/issue-2137-v3/work",
            context_branch="egg/issue-2137-v3/context",
        )
        assert result == "egg/issue-2137-v3/context"


# ---------- find_orphaned_child_prs (integration with the contract) ----------


class TestFindOrphansContextBranchEndToEnd:
    """The reconciler reads ``contract.pr.context_branch`` and threads
    it through to the resolver.  Lock that wiring here so a future
    refactor of ``find_orphaned_child_prs`` can't silently strip the
    plumbing."""

    def test_context_branch_chosen_when_chain_exhausted_and_extant(self) -> None:
        """Headline #2548 task-2-3 behavior, end-to-end: an orphaned
        slice-2 with no surviving ancestors retargets to the context
        branch when it's set on the contract and present on origin."""
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            ),
            context_branch="egg/issue-2137/context",
        )
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        # Parent gone, context branch alive.
        extant: set[str] = {"egg/issue-2137/context"}
        orphans = find_orphaned_child_prs(contract, prs, extant)
        assert len(orphans) == 1
        assert orphans[0].intended_new_base == "egg/issue-2137/context"

    def test_context_branch_missing_from_extant_falls_through(self) -> None:
        """If the contract carries the context branch but the branch
        is missing from origin, the reconciler MUST fall through to
        the pipeline branch.  Pushing onto the deleted context branch
        would re-enter the orphan loop."""
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            ),
            context_branch="egg/issue-2137/context",
        )
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        # No branches alive — context branch deleted too.
        orphans = find_orphaned_child_prs(contract, prs, set())
        assert len(orphans) == 1
        assert orphans[0].intended_new_base == "egg/issue-2137/work", (
            "Reconciler must fall through to pipeline branch when context "
            "branch is set on the contract but missing from origin"
        )

    def test_no_pr_metadata_legacy_fallback_unchanged(self) -> None:
        """A contract with ``contract.pr is None`` (e.g. a refine-only
        run, or a pipeline that pre-dates the context-PR mechanism)
        must inherit the pre-#2548 fallback exactly: pipeline branch,
        no exceptions, no spurious ``None``-ref retarget."""
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137/slice-1",
            ),
            # context_branch defaults to None → contract.pr stays None.
        )
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137/slice-2",
                base="egg/issue-2137/slice-1",
            )
        ]
        orphans = find_orphaned_child_prs(contract, prs, set())
        assert len(orphans) == 1
        assert orphans[0].intended_new_base == "egg/issue-2137/work"

    def test_dag_walk_still_wins_over_context_branch(self) -> None:
        """End-to-end: when an extant DAG ancestor exists, the
        reconciler uses it even if a context branch is also extant.
        This protects healthy stacks from being force-rebased onto
        the context branch root."""
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
            context_branch="egg/issue-2137/context",
        )
        prs = [
            _pr(
                number=12,
                head="egg/issue-2137/slice-3",
                base="egg/issue-2137/slice-2",
            )
        ]
        # slice-2 deleted; slice-1 + context both alive.
        extant: set[str] = {"egg/issue-2137/slice-1", "egg/issue-2137/context"}
        orphans = find_orphaned_child_prs(contract, prs, extant)
        assert len(orphans) == 1
        # DAG walk wins: slice-1 is the rebase target, NOT context.
        assert orphans[0].intended_new_base == "egg/issue-2137/slice-1"

    def test_qualified_pipeline_id_with_context_branch(self) -> None:
        """Qualifier suffixes (``-v3``, ``-backend``) propagate through
        contract.pr.context_branch unchanged.  Reconciler must
        retarget to the qualified context branch, not the unqualified
        one."""
        contract = _contract(
            _slice(
                "slice-2",
                deps=["slice-1"],
                parent_branch="egg/issue-2137-v3/slice-1",
            ),
            pipeline_id="issue-2137-v3",
            context_branch="egg/issue-2137-v3/context",
        )
        prs = [
            _pr(
                number=11,
                head="egg/issue-2137-v3/slice-2",
                base="egg/issue-2137-v3/slice-1",
            )
        ]
        extant: set[str] = {"egg/issue-2137-v3/context"}
        orphans = find_orphaned_child_prs(contract, prs, extant)
        assert len(orphans) == 1
        assert orphans[0].intended_new_base == "egg/issue-2137-v3/context", (
            "Reconciler must use the qualified context branch, not the "
            "unqualified ``egg/issue-2137/context``"
        )

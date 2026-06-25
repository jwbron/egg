"""Structural guards for slice-PR commit-history hygiene (#3255).

Slice PRs (`egg/<id>/slice-N -> egg/<id>/work`) used to accumulate large
numbers of unrelated commits — every later slice's
``Persist contract after slice ... completion`` commit plus re-stamped copies
of the whole refine/plan prefix (observed 9x on PR #3236). The file *diffs*
stayed correct, but the commit list was heavily polluted, confusing human and
bot reviewers (companion to #3254).

Root cause, confirmed against the live issue-3200 branches: the generic
``on-merge-conflict.yml`` bot resolves a conflicting PR by merging its *base*
into its head. For a slice PR the base is the advancing ``egg/<id>/work``
state-branch, so each merge dragged work's entire lineage into the early
slice's history. ``egg/<id>/work`` itself is clean and append-only — the
pollution lived entirely on slice branches.

The fix has two halves, both asserted here so a future "cleanup" cannot
silently regress them:

1. ``on-merge-conflict.yml`` skips slice PRs — slice-stack reconciliation is
   owned by the orchestrator, not the generic base-into-head merge bot. The
   ``egg/<id>/work -> main`` context PR is deliberately *not* skipped so the
   base PR keeps tracking ``main``.
2. ``reusable-review.yml`` overlays the canonical contract from
   ``egg/<id>/work`` into a slice PR's review checkout (working-tree only),
   so contract verification still reasons about the live contract even though
   the conflict bot no longer merges work in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFLICT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "on-merge-conflict.yml"
REUSABLE_CONFLICT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "reusable-conflict-resolve.yml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "reusable-review.yml"

SLICE_HEAD_REGEX = "^egg/.+/slice-[0-9]+$"


def _step_run(
    workflow: Path, job: str, *, step_id: str | None = None, step_name: str | None = None
) -> str:
    if not workflow.exists():
        pytest.skip(f"{workflow} not found — repo is in an unexpected state")
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    job_def = data.get("jobs", {}).get(job)
    assert job_def is not None, f"missing `{job}` job in {workflow.name}"
    for step in job_def.get("steps", []):
        if not isinstance(step, dict):
            continue
        if step_id is not None and step.get("id") == step_id:
            return step.get("run", "")
        if step_name is not None and step.get("name") == step_name:
            return step.get("run", "")
    raise AssertionError(
        f"step (id={step_id!r}, name={step_name!r}) not found in `{job}` of {workflow.name}"
    )


@pytest.fixture
def find_script() -> str:
    """The bash body of the conflict bot's `find` step (id: find)."""
    return _step_run(CONFLICT_WORKFLOW, "find-conflicts", step_id="find")


def test_conflict_bot_reads_head_ref(find_script: str) -> None:
    """The PR projection must surface each PR's head ref so the skip can key on it.

    Without ``head_ref`` in the projection there is nothing to match the slice
    pattern against and the skip silently never fires.
    """
    assert "head_ref" in find_script, (
        "the conflict bot's PR projection no longer exposes `head_ref` — the "
        "slice-PR skip (#3255) has nothing to match on"
    )


def test_conflict_bot_skips_slice_prs(find_script: str) -> None:
    """A slice PR head must be matched and skipped (not added to `conflicting`)."""
    lines = find_script.splitlines()
    slice_if_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if line.lstrip().startswith("if [[") and SLICE_HEAD_REGEX in line
        ),
        None,
    )
    assert slice_if_idx is not None, (
        "the conflict bot has no `if [[ ... ^egg/.+/slice-[0-9]+$ ... ]]` guard — "
        "slice PRs will again be auto-resolved by merging work in, re-polluting "
        "their commit history (#3255)"
    )
    # The guard must short-circuit (continue) rather than fall through into the
    # conflicting+=("$pr_num") accumulation.
    tail = "\n".join(lines[slice_if_idx : slice_if_idx + 4])
    assert "continue" in tail, (
        "the slice-PR guard does not `continue` — a slice PR could still be "
        "queued for conflict resolution (#3255)"
    )


def test_conflict_bot_does_not_skip_the_context_pr(find_script: str) -> None:
    """The skip must be keyed on slice topology, not all egg branches.

    The ``egg/<id>/work -> main`` context PR must keep being conflict-resolved
    so the base PR tracks ``main`` (an explicit requirement of #3255). A blanket
    ``^egg/`` skip would silently stop updating it.
    """
    assert "/slice-" in SLICE_HEAD_REGEX  # guard against a typo in this test
    assert SLICE_HEAD_REGEX in find_script, "slice-topology skip regex missing"
    # No blanket egg-namespace skip that would also catch the work branch.
    assert "^egg/.+/work" not in find_script and '"^egg/"' not in find_script, (
        "the conflict bot appears to skip the work/context PR too — it must keep "
        "resolving egg/<id>/work -> main so the base PR stays up to date (#3255)"
    )


def test_reusable_conflict_resolve_skips_slice_prs() -> None:
    """Defense-in-depth: the reusable workflow must also skip slice PRs.

    The ``find`` step only guards the push/schedule entry point. The
    ``workflow_dispatch`` (manual) entry point calls
    ``reusable-conflict-resolve.yml`` directly with an operator-supplied
    ``pr_number`` and would otherwise re-introduce exactly the history pollution
    #3255 removes. The slice skip therefore also lives in the reusable workflow,
    where both entry points converge.
    """
    slice_check = _step_run(REUSABLE_CONFLICT_WORKFLOW, "resolve", step_id="slice-check")
    assert SLICE_HEAD_REGEX in slice_check, (
        "the reusable conflict-resolve workflow no longer matches the slice-PR "
        "head pattern — manual dispatch could re-pollute slice history (#3255)"
    )
    assert "skip=true" in slice_check, (
        "the reusable conflict-resolve slice-check sets no skip output (#3255)"
    )

    # Every step that performs the actual conflict resolution must be gated on
    # the slice-check skip, or the guard is inert.
    data = yaml.safe_load(REUSABLE_CONFLICT_WORKFLOW.read_text(encoding="utf-8"))
    gated = {
        "Checkout PR branch",
        "Run egg conflict resolution",
    }
    seen = set()
    for step in data["jobs"]["resolve"].get("steps", []):
        if not isinstance(step, dict) or step.get("name") not in gated:
            continue
        seen.add(step["name"])
        assert "steps.slice-check.outputs.skip != 'true'" in step.get("if", ""), (
            f"step {step['name']!r} is not gated on the slice-check skip — a "
            f"manually dispatched slice PR would still be resolved (#3255)"
        )
    assert seen == gated, f"expected gated steps {gated} not all present, saw {seen}"


def test_review_overlays_canonical_contract_for_slice_prs() -> None:
    """The slice-PR review checkout must overlay the contract from work.

    Once the conflict bot stops merging work into slice branches, a slice
    branch's contract is frozen at its fork point. Contract verification must
    instead read the live contract from ``egg/<id>/work`` (the orchestrator's
    sole-writer branch — gateway phase_filter #2979).
    """
    overlay = _step_run(
        REVIEW_WORKFLOW,
        "review",
        step_name="Overlay canonical contract from work branch (slice PRs)",
    )
    assert "slice-[0-9]+$" in overlay, (
        "the overlay step no longer derives the work branch from a slice head match (#3255)"
    )
    assert "ref=${work_branch}" in overlay, (
        "the overlay step no longer reads .egg-state/contracts from the canonical "
        "work branch via the contents API (#3255)"
    )


def test_overlay_runs_after_pr_checkout_and_before_review() -> None:
    """Ordering guard: overlay must land between the PR checkout and the review.

    If the overlay ran before the PR checkout, the checkout would clobber it;
    if it ran after the egg review step, the reviewer would never see it.
    """
    data = yaml.safe_load(REVIEW_WORKFLOW.read_text(encoding="utf-8"))
    names = [s.get("name") for s in data["jobs"]["review"].get("steps", []) if isinstance(s, dict)]
    checkout = "Checkout PR code for review"
    overlay = "Overlay canonical contract from work branch (slice PRs)"
    review = "Run egg to review and post feedback"
    for name in (checkout, overlay, review):
        assert name in names, f"review job missing expected step {name!r} (#3255)"
    assert names.index(checkout) < names.index(overlay) < names.index(review), (
        "the canonical-contract overlay is mis-ordered — it must run after the PR "
        "checkout and before the egg review step (#3255)"
    )

"""Regression test for #2769 / #2593 / #2744 — context PR opens up-front
and is idempotent across re-entry of the plan→implement hook.

This test lives under ``integration_tests/regression/`` because that's
the kubectl-gated regression tier where the parent ``conftest.py``
exposes the ``orchestrator_url`` and ``egg_stack`` fixtures (with
``gateway_url`` attribute) — the legacy ``integration_tests/local_pipeline/``
directory was deleted on 2026-05-11 in commit ``f7803637d1`` and MUST
NOT be referenced (TASK-3-9 acceptance criteria, plan §"trust-boundary
scope").

Test shape (TASK-3-9 acceptance criteria steps a–g):

a. Spawn a 2-slice DAG pipeline against the local stack.
b. Advance to the plan→implement boundary.
c. Assert a single PR exists with ``head=egg/<id>/work`` and
   ``base=main``.
d. Extract the PR number.
e. Deliberately clear ``contract.pr.context_pr_number`` on disk to
   simulate the #2769 / #2593 / #2744 mid-run loss.
f. Re-trigger the implement-start hook via ``advance_phase``.
g. Assert no duplicate PR is opened AND the same PR number is
   re-persisted (the idempotency repair guarantee documented in
   ``_open_context_pr_at_implement_start``'s idempotency contract).

The test inherits ``orchestrator_url`` + ``egg_stack`` from the parent
kubectl-gated fixture; the ``kubectl``-skip is automatic via the
session-scoped fixture in ``integration_tests/conftest.py``. When the
cluster is unavailable, pytest skips with a clear message instead of
failing.

Per #2548 / #2777, the context PR head is the pipeline's work branch
(``egg/<id>/work``) — NOT a separate ``egg/<id>/context`` branch. The
pre-#2548 scaffold (worktree materialisation, two-tier idempotency,
fallback resolver, soft-fail wrapper, dedup observability,
``_CONTEXT_BRANCH_RE`` gateway exemption) was removed in slice-2.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(
    reason=(
        "TASK-3-9: full end-to-end pipeline drive against the local "
        "stack requires a Claude-provider-backed plan phase to populate "
        "contract.pr metadata — same caveat as the existing "
        "test_slice_restart_branch_invariants.py module docstring (the "
        "ScriptedProvider pod-injection avenue from #2474 was ruled "
        "out). Skipped pending a deterministic plan-phase stub. The "
        "unit-tier coverage in orchestrator/tests/test_context_pr_opener.py "
        "covers the same idempotency contract against the in-process "
        "_open_context_pr_at_implement_start function (the happy / "
        "idempotent / hard-required-raises paths from TASK-3-8's "
        "acceptance criteria). Re-enable this test once the local-stack "
        "harness can drive plan→implement without a live agent."
    )
)
def test_context_pr_opens_up_front_and_is_idempotent(
    orchestrator_url: str,  # noqa: ARG001  — fixture wires the kubectl skip
    egg_stack: object,  # noqa: ARG001  — typed loosely (EggStack lives behind kubectl)
) -> None:
    """End-to-end regression for #2769 / #2593 / #2744.

    Steps a–g from the module docstring above. The body is intentionally
    skipped while the local-stack harness lacks a deterministic plan
    phase; the docstring + ``@pytest.mark.skip`` reason document the
    intent so the test re-enables cleanly once the harness lands. The
    parametrized unit coverage in
    ``orchestrator/tests/test_context_pr_opener.py`` exercises the same
    idempotency contract at the in-process boundary, so the regression
    is not entirely unguarded today.
    """
    # NOTE: the skip above prevents this body from running.  Keep the
    # step ordering here so the next contributor sees the wire-up.
    #
    # 1. spawn 2-slice DAG pipeline against orchestrator_url
    # 2. advance to plan→implement boundary
    # 3. resolve open PRs for the pipeline and assert exactly one
    #    matches head=egg/<id>/work base=main
    # 4. capture pr_number
    # 5. clear contract.pr.context_pr_number on the work-tree contract
    #    file (the on-disk repair scenario)
    # 6. POST /api/v1/pipelines/<id>/phases/implement/advance to re-run
    #    the implement-start hook (or whichever route is the canonical
    #    re-entry point when the harness lands)
    # 7. assert open PRs still has exactly one match AND its number ==
    #    pr_number (no duplicate created); also assert the contract on
    #    disk has context_pr_number == pr_number (idempotent repair)
    raise NotImplementedError("covered by the @pytest.mark.skip above; see docstring")

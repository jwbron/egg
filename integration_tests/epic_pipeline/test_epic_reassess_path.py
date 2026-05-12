"""
Slice-2 epic-reassess end-to-end integration test (issue #1557 task-2-9).

This test exercises the reassess path against the stub-jira fake the
slice-1 tester (task-1-7) will land. It is **deferred behind a skip**
because the slice-1 prerequisites are not yet on this branch:

- ``integration_tests/fixtures/stub_jira.py`` — the in-process Atlassian
  fake (task-1-7). The test imports the fixture via
  ``integration_tests.fixtures.stub_jira`` and seeds an epic with four
  children (one per classification class: Done / In-flight / Updatable /
  Net-new).
- ``integration_tests/epic_pipeline/conftest.py`` — the slice-1
  conftest that shares ``egg_stack`` + ``egg_stack.gateway_url`` from
  the top-level integration conftest (task-1-8).

Once both arrive, this test should be un-skipped and the contract task
``task-2-9`` re-verified. The skip marker carries the slice-1 task
references so the slice-1 tester can grep for callers of their
fixtures when wiring them up.

Acceptance criteria (task-2-9 — reassess section):

- ``make test-integration`` passes the new reassess end-to-end flow.
- In-flight refusal exercised by an integration test scenario where
  the planner emits an ``'edit'`` action on an ``in_flight`` child
  without the override marker; assert ``jira_action_status='failed'``
  and the apply phase re-spawns successfully when the operator adds
  ``in-flight-confirmed`` to ``Task.notes``.

The test plan below is documented inline so a reviewer can confirm the
acceptance is covered once the stub arrives.
"""

from __future__ import annotations

import pytest

# Skip marker — gate on slice-1 prerequisites. Two reasons:
# (1) stub-jira fake (task-1-7) lives in ``integration_tests/fixtures/``
#     and is not yet on this branch.
# (2) ``epic_pipeline/conftest.py`` (task-1-8) does not yet exist;
#     this directory has no conftest.py wiring the ``egg_stack``
#     fixture from the parent.
#
# Under ``make test-integration`` (kubectl-gated) this test will
# pytest.skip cleanly until the prerequisites land.
pytestmark = pytest.mark.skip(
    reason=(
        "Awaiting slice-1 prerequisites: stub-jira fake (task-1-7) "
        "+ epic_pipeline/conftest.py (task-1-8). Test plan documented "
        "inline; see test bodies for acceptance coverage."
    )
)


def test_reassess_end_to_end_classifies_all_four_children() -> None:
    """End-to-end reassess: seed an epic with one child per
    classification class (Done / In-flight / Updatable / Net-new),
    drive the pipeline through plan → apply, and assert each task's
    ``jira_action`` is the canonical mapping:

      Done       → no Task (excluded from planner per decision-5)
      In-flight  → no Task (planner refuses to mutate without marker)
      Updatable  → Task with ``jira_action='edit'``
      Net-new    → Task with ``jira_action='create'``

    After the apply phase confirms, each surviving Task's
    ``jira_action_status`` must be ``'applied'`` (acceptance: "assert
    REVIEWER_CONTRACT ACKs the apply-phase consensus on contract-state
    convergence").
    """
    pytest.fail("Test plan documented; awaiting slice-1 prerequisites.")


def test_in_flight_refusal_without_marker_lands_as_failed() -> None:
    """Scenario: the planner emits ``jira_action='edit'`` against an
    ``in_flight`` child without the per-ticket ``in-flight-confirmed``
    marker in ``Task.notes``. The applier refuses at gateway-call time
    and writes ``jira_action_status='failed'`` with reason
    ``'in-flight not confirmed'``.

    Re-spawn the apply phase after the operator adds
    ``in-flight-confirmed`` to ``Task.notes`` and assert the task
    transitions to ``'applied'`` (acceptance: "the apply phase
    re-spawns successfully when the operator adds 'in-flight-
    confirmed' to Task.notes").
    """
    pytest.fail("Test plan documented; awaiting slice-1 prerequisites.")


def test_wontdo_drain_runs_post_apply_consensus() -> None:
    """A consolidate-into cluster (1 survivor + 2 obsoletes) produces:

      - 1 Task with ``jira_action='edit'`` for the survivor
      - 2 Tasks with ``jira_action='wontdo'`` for the obsoletes

    The applier emits a single Won't-Do handoff JSON; the post-apply
    drain (TASK-2-7) iterates and calls ``/transition`` for each.
    Each obsolete Task's ``jira_action_status`` flips to ``'applied'``
    after the transition succeeds.

    Acceptance: "Won't-Do handoff JSON (produced by the applier) is
    drained by the orchestrator via /transition after applier
    consensus; per-Task jira_action_status flips to 'applied' after
    a successful transition."
    """
    pytest.fail("Test plan documented; awaiting slice-1 prerequisites.")


def test_idempotent_rerun_no_duplicate_writes() -> None:
    """Acceptance (slice-1 task-1-8 mirror, exercised here for reassess):
    "Idempotent re-run produces zero new gateway writes on the second
    pass (every Task already has status 'applied')."

    Run the pipeline twice end-to-end and assert the second pass makes
    zero create / edit / link / transition calls (stub-jira's
    ``request_log`` is empty for the second pass).
    """
    pytest.fail("Test plan documented; awaiting slice-1 prerequisites.")


def test_remotelinks_signal_promotes_to_in_flight() -> None:
    """Seed a child whose Atlassian status is 'To Do' (statusCategory
    'new') but whose remote-link list includes a GitHub PR URL.
    Assert the reassess sweep classifies the child as ``in_flight``
    and emits ``in_flight_evidence`` naming the remote-link signal.

    Acceptance (task-2-4): "Sweep result includes an ``in_flight:
    bool`` per child and an ``in_flight_evidence: list[str]``
    enumerating which signals fired."
    """
    pytest.fail("Test plan documented; awaiting slice-1 prerequisites.")

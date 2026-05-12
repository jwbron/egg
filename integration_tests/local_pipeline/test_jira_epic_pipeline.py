"""Integration tests for the Jira-epic pipeline (#1557 TASK-1-18).

Scaffolds the two end-to-end scenarios required by the issue:

(a) **Fresh-epic end-to-end** — ``submit_task --mode auto`` on a Jira
    epic that has no children. The pipeline drives refine HITL → plan
    HITL → apply, the apply step creates the children, and the Stop-
    after-plan path terminates with status ``PLAN_STOPPED``.

(b) **Reassess end-to-end** — ``submit_task --mode auto`` on a Jira
    epic that already has children. The refine input gatherer
    classifies them, the in-flight gate fires for any mutated
    in-flight child, the operator Confirms the divergence, the
    Won't-Do batch lands, and the Continue-to-implement fork fans out
    one child pipeline per remaining child.

Both scenarios require a live local pipeline stack (gateway +
orchestrator in k3s).  When ``kubectl`` is missing they skip with the
same friendly message used elsewhere in ``integration_tests/
local_pipeline/``. When the stack is up, the scenarios still skip
with a pointer to the deferred follow-up PR — the coder slice that
implements TASK-1-13 / 14 / 16 / 17 has been deferred (#1557 v4
propose) and the test scaffolds document the gap so reviewers and
CI can see the coverage hole without breaking the suite.
"""

from __future__ import annotations

import subprocess

import pytest

from .conftest import LocalPipelineStack

pytestmark = pytest.mark.integration


def _kubectl_available() -> bool:
    """Lightweight kubectl probe — mirrors the conftest's helper.

    Re-implemented here (rather than imported) so the skipif marker on
    the class can fire at collection time, before the conftest fixture
    has been instantiated. This means the test file imports cleanly
    even when kubectl is missing.
    """
    try:
        result = subprocess.run(
            ["kubectl", "cluster-info"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except FileNotFoundError, subprocess.TimeoutExpired:
        return False


_KUBECTL_AVAILABLE = _kubectl_available()
_SKIP_REASON_KUBECTL = (
    "kubectl is not available or not connected to a cluster — Jira-epic "
    "pipeline integration tests require k3s (see docs/guides/testing.md)"
)

# Coder slices TASK-1-13 / 14 / 16 / 17 were deferred from #1557 to a
# follow-up PR (v4 propose). Both scenarios below skip with this message
# so the suite advertises the coverage gap loudly rather than silently
# passing on placeholder asserts.
_DEFERRED_SKIP_REASON = "TASK-1-13/14/16/17 deferred to follow-up PR (see #1557 v4 propose)"


# ---------------------------------------------------------------------------
# Scenario (a) — fresh-epic end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _KUBECTL_AVAILABLE, reason=_SKIP_REASON_KUBECTL)
class TestFreshEpicEndToEnd:
    """``submit_task --mode auto`` on an epic with no children."""

    def test_submit_task_creates_pipeline_for_fresh_epic(
        self,
        local_pipeline_stack: LocalPipelineStack,
        orchestrator_url: str,
    ) -> None:
        """``submit_task --mode auto`` accepts an epic key + creates a pipeline.

        Expected handoff sequence (full end-to-end):

        1. ``submit_task`` is invoked with ``jira_epic_key=<key>`` and
           ``mode=auto``.  The orchestrator detects no existing
           children and selects ``jira_effective_mode="fresh"``.
        2. The refine phase runs the epic-mode prompt; the agent writes
           the analysis draft and the phase reaches HITL.
        3. The operator approves the refine HITL gate; the apply step
           rewrites the epic Description (wholesale per decision-9).
        4. The plan phase runs the epic-mode prompt; the agent emits
           per-node ticket-shaped descriptions; the phase reaches HITL.
        5. The operator approves the plan HITL gate; the apply step
           creates the child tickets under the epic.
        6. The Stop-after-plan path terminates the pipeline with status
           ``PLAN_STOPPED``.

        Acceptance:

        - The orchestrator's pipeline DB shows the children's Jira keys
          on ``epic_apply.plan_node_to_jira_key``.
        - Final pipeline status is ``PLAN_STOPPED``.
        - ``epic_apply.applied_edits[]`` records every gateway-mediated
          mutation with ``status="applied"``.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

    def test_fresh_epic_pipeline_terminates_plan_stopped(
        self,
        local_pipeline_stack: LocalPipelineStack,
        orchestrator_url: str,
    ) -> None:
        """The pipeline's terminal status is ``PLAN_STOPPED``.

        Without an explicit ``stop_after`` override the auto-mode epic
        pipeline halts at the plan-gate after the apply step lands the
        children — confirming the epic-keyed default differs from a
        normal issue-keyed pipeline (which continues into implement).
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

    def test_fresh_epic_writes_decision_9_description_rewrite(
        self,
        local_pipeline_stack: LocalPipelineStack,
        gateway_url: str,
    ) -> None:
        """The refine apply step rewrites the epic's Description in full.

        Asserts a single ``ticket/edit`` was issued against the epic
        key after refine HITL approval, and that the resulting
        ``epic_apply.refine_description_sha256`` matches the live
        Description hash.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

    def test_fresh_epic_creates_children_under_epic_hierarchy(
        self,
        local_pipeline_stack: LocalPipelineStack,
        gateway_url: str,
    ) -> None:
        """Plan apply creates children with the configured hierarchy field.

        For projects where ``jira-hierarchy.yaml`` maps to ``parent``,
        each child's ``parent`` field points at the epic key. For
        ``epic_link`` projects, the ``epic_link`` custom field is
        populated instead. The plan-node → Jira-key mapping lands on
        ``epic_apply.plan_node_to_jira_key``.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)


# ---------------------------------------------------------------------------
# Scenario (b) — reassess end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _KUBECTL_AVAILABLE, reason=_SKIP_REASON_KUBECTL)
class TestReassessEpicEndToEnd:
    """``submit_task --mode auto`` on an epic that already has children."""

    def test_reassess_epic_classifies_existing_children(
        self,
        local_pipeline_stack: LocalPipelineStack,
        orchestrator_url: str,
    ) -> None:
        """The refine input gatherer classifies every existing child.

        Expected per-child labels: ``done`` / ``to_do`` / ``in_flight``
        / ``updated`` / ``net-new`` — written to the
        ``existing-children.json`` agent-outputs file the plan agent
        reads.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

    def test_reassess_in_flight_gate_fires_on_mutation(
        self,
        local_pipeline_stack: LocalPipelineStack,
        orchestrator_url: str,
    ) -> None:
        """The plan apply step opens a HITL gate for in-flight mutations.

        When the plan proposes a mutation against any child currently
        classified ``in_flight`` (decision-8 OR semantics — any of
        ``jira_status`` / ``orchestrator_pr_url`` / ``remote_link``
        firing), the apply step pauses for HITL confirmation rather
        than overwriting in-flight work.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

    def test_reassess_operator_confirm_resolves_gate_and_proceeds(
        self,
        local_pipeline_stack: LocalPipelineStack,
        orchestrator_url: str,
    ) -> None:
        """Operator's "Confirm — apply anyway" lets the apply step proceed.

        Resolving the gate with ``Confirm — apply anyway`` records the
        decision on ``epic_apply.in_flight_gates[]`` with the chosen
        option and the apply step continues with the remainder of the
        batch.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

    def test_reassess_wont_do_batch_lands_after_confirm(
        self,
        local_pipeline_stack: LocalPipelineStack,
        orchestrator_url: str,
    ) -> None:
        """The Won't-Do batch is transitioned after the plan applies.

        ``orchestrator/jira_transitions.py`` runs the batched Won't-Do
        transitions in a single atomic pass (decision-3 batch). Each
        success bumps the per-entry ``status`` from ``pending`` to
        ``applied``; transition failures land ``status="failed"`` with
        an ``error`` field for the operator's follow-up.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

    def test_reassess_continue_to_implement_fans_out_child_pipelines(
        self,
        local_pipeline_stack: LocalPipelineStack,
        orchestrator_url: str,
    ) -> None:
        """``Continue to implement`` fork creates one child pipeline per child.

        The parent (epic-keyed) pipeline forks into N child pipelines —
        each keyed off a remaining child ticket, with
        ``jira_parent_epic_key`` set on the child so the PR-link
        writeback in the implement-phase finalizer (TASK-1-15) can fire.
        """
        pytest.skip(_DEFERRED_SKIP_REASON)

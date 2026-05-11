"""Regression test for #2430 — HITL alive-signal bypass.

#2430 was a deadlock: when a refiner registered an HITL decision via
``register_open_question`` the pipeline transitioned to ``awaiting_human``,
but the alive-signal scheduler kept the agent's container marked as
"live" for the duration of the human turnaround.  When the human
resolved the decision via ``provide_input``, the orchestrator emitted
the wake signal — but the scheduler had already double-signalled the
alive-state, leaving the resume callback wedged behind a stale alive
event.  The pipeline would sit at 99% awaiting_human forever.

The fix bypassed the alive-signal queue for the HITL resume path,
delivering the wake signal directly to the agent's main loop.

This test drives one full HITL round-trip end-to-end:

  refiner → ``register_open_question`` → ``awaiting_human`` →
  ``provide_input`` → pipeline resumes → ``phase_complete`` for refine →
  plan phase starts

and asserts each transition fires within a bounded deadline.  The two
assertions that ride on #2430's fix are (a) the resume-within-deadline
check after ``provide_input``, and (b) the downstream-plan-phase-starts
check (which only fires if the refiner's continuation callback actually
runs after the wake).

Contract reference: issue #2474 task-1-8.  Acceptance criterion: passes
on ``main``; reverting #2430's alive-signal bypass causes the resume-
within-deadline assertion to time out.
"""

from __future__ import annotations

import time

import pytest
import requests

from integration_tests.regression.conftest import start_pipeline

pytestmark = pytest.mark.integration

_HITL_DECISION_DEADLINE_S = 60.0
_RESUME_DEADLINE_S = 60.0  # #2430 bypass should resume well within 60s
_PLAN_START_DEADLINE_S = 120.0


def _get_pipeline_status(orchestrator_url: str, pipeline_id: str) -> dict:
    resp = requests.get(f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status", timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", {})


def _wait_for_status(
    orchestrator_url: str,
    pipeline_id: str,
    target: str,
    *,
    timeout: float,
    poll_interval: float = 2.0,
) -> dict:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        data = _get_pipeline_status(orchestrator_url, pipeline_id)
        status = data.get("status", "")
        if status:
            seen.append(status)
        if status == target:
            return data
        time.sleep(poll_interval)
    raise AssertionError(
        f"Pipeline {pipeline_id!r} did not reach status {target!r} within "
        f"{timeout}s; observed statuses: {seen!r}"
    )


def _wait_for_current_phase(
    orchestrator_url: str,
    pipeline_id: str,
    target_phase: str,
    *,
    timeout: float,
    poll_interval: float = 2.0,
) -> dict:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        data = _get_pipeline_status(orchestrator_url, pipeline_id)
        current = data.get("current_phase", "")
        if current:
            seen.append(current)
        if current == target_phase:
            return data
        time.sleep(poll_interval)
    raise AssertionError(
        f"Pipeline {pipeline_id!r} did not reach current_phase "
        f"{target_phase!r} within {timeout}s; observed: {seen!r}"
    )


def test_hitl_resume_does_not_deadlock_on_alive_signal(
    egg_stack,
    request: pytest.FixtureRequest,
) -> None:
    """A complete HITL round-trip must resume within the bypass deadline.

    The scripted refiner registers an open question, the pipeline
    transitions to ``awaiting_human``, we resolve it, and the
    pipeline must resume within ~60s.  The downstream plan phase
    starting is the strongest signal that the resume actually unblocked
    the refiner's continuation (and was not just a status flip).
    """
    payload = start_pipeline(
        request,
        orchestrator_url=egg_stack.gateway_url,
        prompt=(
            "Single-agent regression for #2430 HITL alive-signal bypass. "
            "Scripted refiner registers one open question via "
            "register_open_question, then waits for the resolution before "
            "completing the refine phase."
        ),
        repo="test-owner/test-repo",
    )
    pipeline_id = payload["pipeline_id"]

    # Step 1 — wait for the pipeline to transition into awaiting_human.
    awaiting = _wait_for_status(
        egg_stack.gateway_url,
        pipeline_id,
        "awaiting_human",
        timeout=_HITL_DECISION_DEADLINE_S,
    )
    pending = awaiting.get("pending_decisions") or []
    assert pending, (
        f"Pipeline is awaiting_human but has no pending decisions; status payload was {awaiting!r}"
    )
    # Take the first decision id — the scripted refiner only registers one.
    decision_id = pending[0].get("id") or pending[0].get("decision_id")
    assert decision_id, f"Pending decision is missing an id field: {pending[0]!r}"

    # Step 2 — resolve the decision via provide_input.
    resolve_resp = requests.post(
        f"{egg_stack.gateway_url}/api/v1/pipelines/{pipeline_id}/decisions/{decision_id}",
        json={"action": "select", "selected": "opt-1"},
        timeout=15,
    )
    assert resolve_resp.status_code < 400, (
        f"provide_input failed: HTTP {resolve_resp.status_code} {resolve_resp.text}"
    )

    # Step 3 — pipeline must resume out of awaiting_human within the bypass
    # deadline.  This is the load-bearing #2430 assertion.
    resume_start = time.monotonic()
    resumed = _wait_for_status(
        egg_stack.gateway_url,
        pipeline_id,
        "running",
        timeout=_RESUME_DEADLINE_S,
    )
    resume_elapsed = time.monotonic() - resume_start
    assert resumed.get("status") == "running", (
        f"#2430 regression: pipeline did not resume from awaiting_human "
        f"within {_RESUME_DEADLINE_S}s (took {resume_elapsed:.1f}s).  "
        f"Reverting the alive-signal bypass re-introduces the deadlock."
    )

    # Step 4 — the downstream plan phase must actually start.  If the
    # refiner's continuation callback is wedged behind a stale alive
    # event (#2430's exact failure mode), refine never completes and
    # plan never starts.
    _wait_for_current_phase(
        egg_stack.gateway_url,
        pipeline_id,
        "plan",
        timeout=_PLAN_START_DEADLINE_S,
    )

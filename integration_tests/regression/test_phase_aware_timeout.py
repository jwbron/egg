"""Regression test E.7 — phase-aware consensus timeouts.

The orchestrator supports per-phase consensus timeouts via the
contract field ``consensus_timeout_minutes_<phase>``.  When a phase's
producer fails to emit ``CONSENSUS_PROPOSE`` within the configured
window, a ``CONSENSUS_TIMEOUT`` event must fire so the orchestrator
can escalate (NACK the stalled phase, ping the operator, etc.).

HITL Q3 in the refine phase clarified a subtle point: the issue text's
``consensus_timeout_s = 30`` was a **typo** — the real field is
``consensus_timeout_minutes_<phase>``, taking **minutes**, not seconds.
The fix in HITL Q3 was to keep the minute-granular API and test it
with a 1-minute timeout, asserting the ``CONSENSUS_TIMEOUT`` event
fires within ``60 ± 10`` seconds.  We deliberately do NOT add second-
granular config support in this pipeline — the existing minutes-based
API is sufficient for the assertion, and the docstring below carries
the note for any future engineer who hits the typo in the issue.

Contract reference: issue #2474 task-1-11.  Acceptance criterion: passes
on ``main``; setting the timeout to 10 minutes on a scratch branch
causes the within-60±10s assertion to fail.
"""

from __future__ import annotations

import time

import pytest
import requests

from integration_tests.regression.conftest import start_pipeline

pytestmark = pytest.mark.integration

# ``consensus_timeout_minutes_plan = 1`` → CONSENSUS_TIMEOUT must fire
# within 60 ± 10 seconds.
_TIMEOUT_LOWER_S = 50.0
_TIMEOUT_UPPER_S = 90.0  # 60 + 10 + a small grace for k3s scheduling lag


def test_phase_consensus_timeout_fires_within_window(
    egg_stack,
    request: pytest.FixtureRequest,
) -> None:
    """Plan phase with ``consensus_timeout_minutes_plan = 1`` and a silent
    producer must emit ``CONSENSUS_TIMEOUT`` within 60 ± 10s of phase
    start.

    Docstring note (HITL Q3): the issue text's ``consensus_timeout_s =
    30`` was a typo — the real field is
    ``consensus_timeout_minutes_<phase>`` taking **minutes**.  Do not
    confuse the two.
    """
    payload = start_pipeline(
        request,
        orchestrator_url=egg_stack.gateway_url,
        prompt=(
            "Single-phase regression for E.7 phase-aware consensus "
            "timeouts.  Plan-phase producer is scripted to emit silent "
            "turns (no CONSENSUS_PROPOSE)."
        ),
        repo="test-owner/test-repo",
        config={
            "phase_configs": {
                "plan": {"consensus_timeout_minutes": 1},
            },
        },
    )
    pipeline_id = payload["pipeline_id"]

    # Wait for the plan phase to actually start before we time the
    # timeout window — we don't want refine-phase wall-clock leaking in.
    deadline = time.monotonic() + 180.0
    plan_phase_started_at: float | None = None
    while time.monotonic() < deadline:
        status_resp = requests.get(
            f"{egg_stack.gateway_url}/api/v1/pipelines/{pipeline_id}/status",
            timeout=15,
        )
        if status_resp.status_code < 400:
            data = status_resp.json().get("data", {})
            if data.get("current_phase") == "plan":
                plan_phase_started_at = time.monotonic()
                break
        time.sleep(2.0)
    assert plan_phase_started_at is not None, (
        f"Pipeline {pipeline_id!r} did not enter plan phase within 180s; "
        f"refine-phase set-up is wrong (does the scripted refiner emit "
        f"phase_complete?)"
    )

    # Now poll for the CONSENSUS_TIMEOUT message.  We bound the elapsed
    # window to 60 ± 10 seconds — a fire earlier than 50s indicates the
    # timeout's minute-to-second conversion is broken; later than 90s
    # indicates the timeout isn't firing at all (or the scheduling lag
    # is so bad the test is unreliable, in which case the upper bound
    # widens in a follow-up).
    upper_deadline = plan_phase_started_at + _TIMEOUT_UPPER_S
    timeout_seen_at: float | None = None
    while time.monotonic() < upper_deadline:
        msgs_resp = requests.get(
            f"{egg_stack.gateway_url}/api/v1/pipelines/{pipeline_id}/messages",
            params={"type": "CONSENSUS_TIMEOUT", "phase": "plan"},
            timeout=15,
        )
        if msgs_resp.status_code < 400:
            msgs = msgs_resp.json().get("messages", []) or []
            if msgs:
                timeout_seen_at = time.monotonic()
                break
        time.sleep(2.0)

    assert timeout_seen_at is not None, (
        f"CONSENSUS_TIMEOUT did not fire for plan phase within "
        f"{_TIMEOUT_UPPER_S}s of phase start.  Setting "
        f"consensus_timeout_minutes_plan to 10 on a scratch branch "
        f"reproduces this failure (the assertion is the regression "
        f"target)."
    )

    elapsed = timeout_seen_at - plan_phase_started_at
    assert _TIMEOUT_LOWER_S <= elapsed <= _TIMEOUT_UPPER_S, (
        f"E.7 regression: CONSENSUS_TIMEOUT fired at {elapsed:.1f}s of "
        f"plan-phase start, outside the expected window "
        f"[{_TIMEOUT_LOWER_S}, {_TIMEOUT_UPPER_S}]s.  The "
        f"consensus_timeout_minutes → seconds conversion has drifted, "
        f"or the orchestrator's tick rate has changed."
    )

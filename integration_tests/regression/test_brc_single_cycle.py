"""Regression test for the BRC happy-path single-cycle invariant.

This is a positive regression test: not pinned to a specific past bug,
but to the BRC consensus protocol's load-bearing invariant.  A single
producer / N-reviewer phase with no NACKs must produce exactly:

* 1 × ``CONSENSUS_PROPOSE`` from the producer
* N × ``CONSENSUS_ACK`` (one per reviewer in the roster)
* 0 × ``CONSENSUS_NACK``
* 1 × ``CONSENSUS_CONFIRMED``

If a future refactor accidentally emits two PROPOSE messages (e.g. an
errant re-propose loop), or duplicates the CONFIRMED signal, the exact-
count assertions below catch it.  Without this regression test, those
duplications can be silent — the orchestrator de-dupes some classes of
duplicate but the leaked extras still pollute ``brc-history`` and waste
agent token budget.

Contract reference: issue #2474 task-1-9.  Acceptance criterion: passes
on ``main``; a future regression that emits two PROPOSE messages from a
single producer trips the exact-count assertion.
"""

from __future__ import annotations

import time
from collections import Counter

import pytest
import requests

from integration_tests.regression.conftest import start_pipeline

pytestmark = pytest.mark.integration


def _list_brc_messages(
    orchestrator_url: str,
    pipeline_id: str,
    *,
    timeout: float = 30.0,
) -> list[dict]:
    """Fetch every BRC message recorded for the pipeline."""
    resp = requests.get(
        f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/messages",
        params={"type": "CONSENSUS_PROPOSE,CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_CONFIRMED"},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise AssertionError(f"Failed to fetch BRC messages: HTTP {resp.status_code} {resp.text}")
    return resp.json().get("messages", []) or []


def _wait_for_terminal(
    orchestrator_url: str,
    pipeline_id: str,
    *,
    timeout: float = 300.0,
    poll_interval: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = requests.get(f"{orchestrator_url}/api/v1/pipelines/{pipeline_id}/status", timeout=15)
        if resp.status_code < 400:
            data = resp.json().get("data", {})
            if data.get("status") in {"complete", "failed", "cancelled"}:
                return data
        time.sleep(poll_interval)
    raise AssertionError(f"Pipeline {pipeline_id!r} did not reach terminal state within {timeout}s")


def test_single_producer_phase_emits_exact_brc_message_counts(
    egg_stack,
    request: pytest.FixtureRequest,
) -> None:
    """One producer + N reviewers, no NACKs ⇒ exactly 1 PROPOSE, N ACKs,
    0 NACKs, 1 CONFIRMED per phase.
    """
    payload = start_pipeline(
        request,
        orchestrator_url=egg_stack.gateway_url,
        prompt=(
            "Single-phase regression for BRC happy-path single-cycle "
            "invariant.  Scripted provider drives one producer and all "
            "reviewers to ACK-on-first-pass."
        ),
        repo="test-owner/test-repo",
    )
    pipeline_id = payload["pipeline_id"]

    _wait_for_terminal(egg_stack.gateway_url, pipeline_id)
    messages = _list_brc_messages(egg_stack.gateway_url, pipeline_id)

    # Group by (phase, producer_role) so multi-phase pipelines don't
    # pollute each other's counts.
    by_phase: dict[tuple[str, str], Counter[str]] = {}
    for msg in messages:
        phase = msg.get("phase") or "_unknown"
        producer = msg.get("producer_role") or msg.get("from_role") or "_unknown"
        msg_type = msg.get("type") or msg.get("message_type") or "_unknown"
        by_phase.setdefault((phase, producer), Counter())[msg_type] += 1

    assert by_phase, (
        f"No BRC messages recorded for pipeline {pipeline_id!r}; "
        f"orchestrator returned an empty list"
    )

    # Roster size for the test pipeline.  The scripted scenario configures
    # this many reviewers; we pull the expected count from the pipeline
    # status payload to stay forward-compatible.
    status_resp = requests.get(
        f"{egg_stack.gateway_url}/api/v1/pipelines/{pipeline_id}", timeout=15
    )
    pipeline_doc = status_resp.json().get("data", {}) if status_resp.ok else {}
    expected_reviewers = pipeline_doc.get("brc_reviewers_per_phase", {})

    for (phase, producer), counter in by_phase.items():
        propose_count = counter.get("CONSENSUS_PROPOSE", 0)
        ack_count = counter.get("CONSENSUS_ACK", 0)
        nack_count = counter.get("CONSENSUS_NACK", 0)
        confirmed_count = counter.get("CONSENSUS_CONFIRMED", 0)

        assert propose_count == 1, (
            f"BRC invariant: producer {producer!r} in phase {phase!r} emitted "
            f"{propose_count} CONSENSUS_PROPOSE messages (expected exactly 1).  "
            f"A future regression that loops re-propose would surface here."
        )
        assert nack_count == 0, (
            f"BRC invariant: producer {producer!r} in phase {phase!r} received "
            f"{nack_count} NACKs (expected 0 in a scripted happy-path).  "
            f"The scripted reviewers should have ACKed on first pass."
        )
        assert confirmed_count == 1, (
            f"BRC invariant: producer {producer!r} in phase {phase!r} emitted "
            f"{confirmed_count} CONSENSUS_CONFIRMED signals (expected exactly 1)"
        )

        expected_ack_count = expected_reviewers.get(phase)
        if expected_ack_count is not None:
            assert ack_count == expected_ack_count, (
                f"BRC invariant: producer {producer!r} in phase {phase!r} got "
                f"{ack_count} ACKs but the roster has {expected_ack_count} "
                f"reviewers — one reviewer is missing or extras are leaking."
            )
        else:
            # No roster size advertised → fall back to "at least one ACK".
            assert ack_count >= 1, (
                f"BRC invariant: producer {producer!r} in phase {phase!r} got "
                f"zero ACKs — at minimum one reviewer must have ACKed."
            )

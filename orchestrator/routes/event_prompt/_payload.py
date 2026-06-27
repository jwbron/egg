"""Event-payload extractors — pull structured fields off the next-action payload.

Defensive readers that walk the orchestrator ``next-action`` route's
event_payload shapes (``pending_reviews`` / ``producer`` /
``changed_artifacts`` / ``nacks`` / ``iteration_feedback`` …). Each is
schema-drift tolerant: a malformed shape coerces to an empty/None result
rather than raising, so the wrapper degrades to its stub prompt instead
of crashing. AST-identical to the pre-split definitions — pure refactor
(#3312 slice-6).
"""

from __future__ import annotations

import re
from typing import Any


def _extract_changed_artifacts(event_payload: Any) -> list[str]:
    """Pull a top-level ``changed_artifacts`` list out of the event payload.

    Defensive against schema drift — non-list shapes coerce to empty
    rather than raising. Entries are stringified so a future schema
    surfaces structured artifact refs (e.g. dicts with role / path)
    without crashing the renderer.

    NB: the production payload from
    ``orchestrator/routes/consensus.py::_derive_next_action`` does NOT
    emit a top-level ``changed_artifacts`` key — reviewer-side
    fallback should prefer ``_extract_artifacts_for_producer`` which
    walks the ``pending_reviews[i].artifact_refs`` enrichment surfaced
    by the next-action route. This helper remains as the legacy /
    test-payload entry point.
    """
    if not isinstance(event_payload, dict):
        return []
    raw = event_payload.get("changed_artifacts")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item is not None and str(item).strip()]


def _extract_current_producers(event_payload: Any) -> list[str]:
    """Return the producer roles named by the *current* event.

    Walks the next-action route's payload shapes in priority order:

    * ``pending_reviews`` — the reviewer-side payload key emitted when
      one or more producers have proposals awaiting this reviewer's
      verdict. Each entry is ``{producer, current_version,
      prior_version, prior_verdict, artifact_refs?}``.
    * ``producer`` / ``producer_role`` — the producer-side payload key
      naming the agent's own producer slot (e.g. on a re-propose with
      ``unresolved_nacks``).

    Returns an empty list when no producer can be identified — the
    caller treats that as "legacy / synthetic payload, fall back to
    enumerating all stored memory SHAs" (backward-compat for callers
    that bypass the next-action route).

    De-dupes while preserving first-seen order so the rendered prompt
    sections are stable across invocations.
    """
    if not isinstance(event_payload, dict):
        return []

    seen: list[str] = []
    pending = event_payload.get("pending_reviews")
    if isinstance(pending, list):
        for entry in pending:
            if not isinstance(entry, dict):
                continue
            producer = entry.get("producer") or entry.get("producer_role")
            if not isinstance(producer, str):
                continue
            producer = producer.strip()
            if producer and producer not in seen:
                seen.append(producer)

    if not seen:
        raw = event_payload.get("producer") or event_payload.get("producer_role")
        if isinstance(raw, str) and raw.strip():
            seen.append(raw.strip())

    return seen


def _extract_proposal_sha_for_producer(event_payload: Any, producer: str) -> str:
    """Pull the producer's proposed commit SHA from the event payload.

    Reads ``pending_reviews[i].proposal_commit_sha`` for the entry whose
    ``producer`` matches (#3076) — the enrichment added by the
    next-action route from
    ``PeerConsensusTracker.get_current_proposal_snapshot``. Returns
    ``""`` when the payload carries no SHA for the named producer
    (legacy payloads, synthetic test paths), in which case callers fall
    back to the pre-#3076 behaviour (``HEAD`` delta endpoint / the
    degraded artifact-list fallback).

    The value is sanitised to a hex-ish token before being embedded in
    rendered shell commands: anything that is not a 7-64 char hex
    string is discarded rather than interpolated.

    Asymmetric regex with
    ``orchestrator/attestation_schemas.py::ProposalPayload.validate_commit_sha_format``
    is intentional: that writer-side validator uses a loose
    ``[A-Za-z0-9_]{7,64}`` so reconstruction sentinels (e.g.
    ``RECONSTRUCTED_NO_SHA``) round-trip through
    ``_proposal_commit_shas`` to non-shell consumers; this reader-side
    check is the strict hex-only shell-interpolation boundary that
    rejects those sentinels before any rendered ``git`` command sees
    them. Do not unify — tightening the writer breaks the sentinel
    round-trip; loosening this reader re-opens the shell-injection gap.
    """
    if not isinstance(event_payload, dict) or not isinstance(producer, str):
        return ""
    producer = producer.strip()
    if not producer:
        return ""
    pending = event_payload.get("pending_reviews")
    if not isinstance(pending, list):
        return ""
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        entry_producer = entry.get("producer") or entry.get("producer_role")
        if not isinstance(entry_producer, str) or entry_producer.strip() != producer:
            continue
        raw = entry.get("proposal_commit_sha")
        if isinstance(raw, str):
            candidate = raw.strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,64}", candidate):
                return candidate
        return ""
    return ""


def _extract_artifacts_for_producer(event_payload: Any, producer: str) -> list[str]:
    """Pull the artifact list for a specific producer from the payload.

    Priority order (reviewer_code_holistic v2 finding #1 — wire the
    per-producer artifact_refs the next-action route now emits, not
    the never-emitted top-level ``changed_artifacts`` key):

    1. ``pending_reviews[i].artifact_refs`` where ``entry.producer ==
       producer`` — the production reviewer-side payload, enriched by
       ``_has_pending_peer_proposals`` from
       ``PeerConsensusTracker.get_current_proposal_snapshot``.
    2. Top-level ``event_payload.changed_artifacts`` if the producer
       in the payload's top-level ``producer`` / ``producer_role`` key
       matches — preserved for legacy / synthetic test paths.

    Returns ``[]`` when no artifacts can be associated with the named
    producer.
    """
    if not isinstance(event_payload, dict) or not isinstance(producer, str):
        return []
    producer = producer.strip()
    if not producer:
        return []

    pending = event_payload.get("pending_reviews")
    if isinstance(pending, list):
        for entry in pending:
            if not isinstance(entry, dict):
                continue
            entry_producer = entry.get("producer") or entry.get("producer_role")
            if not isinstance(entry_producer, str) or entry_producer.strip() != producer:
                continue
            raw = entry.get("artifact_refs")
            if isinstance(raw, list):
                refs = [str(item) for item in raw if item is not None and str(item).strip()]
                if refs:
                    return refs

    # Legacy / synthetic-test fallback: only honour the top-level
    # ``changed_artifacts`` when the payload's top-level producer
    # matches the requested producer.
    top_producer = event_payload.get("producer") or event_payload.get("producer_role")
    if isinstance(top_producer, str) and top_producer.strip() == producer:
        return _extract_changed_artifacts(event_payload)
    return []


def _extract_producer_role(event_payload: Any) -> str:
    """Pull the producer role from the event payload, defaulting to
    ``(unknown producer)`` so the fallback entry still renders a
    label rather than a blank string.
    """
    if not isinstance(event_payload, dict):
        return "(unknown producer)"
    raw = event_payload.get("producer") or event_payload.get("producer_role")
    if not isinstance(raw, str) or not raw.strip():
        return "(unknown producer)"
    return raw.strip()


def _extract_nacks(event_payload: Any) -> list[dict[str, Any]]:
    """Pull a structured open-NACK list out of the event payload.

    The orchestrator's ``next-action`` route surfaces the open-NACK
    barrier (``orchestrator/peer_consensus.py:_open_nacks_barrier_response``)
    inside the event_payload when the action verb is ``propose`` on a
    re-propose. Three keys are accepted so the surface naming can evolve
    without breaking the wrapper:

    * ``nacks`` — the canonical key from ``_open_nacks_barrier_response``
      used for the 2+-reviewer barrier shape.
    * ``unresolved_nacks`` — the key emitted by ``next-action``'s
      single-reviewer NACK path (``orchestrator/routes/consensus.py``
      ``_derive_next_action`` lines 329-348). This is the common case
      for producer re-propose events: a single reviewer NACK does not
      trigger the open-NACK barrier (which requires 2+ distinct
      reviewers) but still carries reviewer ``reason`` /
      ``artifact_refs`` the producer needs to address. Omitting this
      key silently dropped single-reviewer NACK feedback from the
      per-event prompt (reviewer_code_holistic v2 finding).
    * ``aggregated_nacks`` — accepted for forward-compat in case the
      next-action route synthesises its own barrier-equivalent payload.

    Non-list values are coerced to an empty list rather than raised so
    a schema drift surfaces as "no NACKs rendered" rather than a hard
    crash in the wrapper.
    """
    if not isinstance(event_payload, dict):
        return []
    raw = event_payload.get("nacks")
    if not isinstance(raw, list):
        raw = event_payload.get("unresolved_nacks")
    if not isinstance(raw, list):
        raw = event_payload.get("aggregated_nacks")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _extract_iteration_feedback(event_payload: Any) -> dict[str, Any] | None:
    """Pull the per-iteration operator kickback off the event payload (#3231).

    The orchestrator's ``next-action`` route attaches the current phase
    execution's ``operator_directives`` (chronological) + the latest
    ``iteration_history`` summary onto the ``propose`` event_payload as a
    serializable ``iteration_feedback`` dict. This reader is the hop the
    ``_cli`` composer subprocess uses to pull it back out — defensive
    against schema drift so a missing/malformed block yields ``None``
    (section omitted) rather than crashing the wrapper's fallback to the
    slice-2 stub prompt.
    """
    if not isinstance(event_payload, dict):
        return None
    raw = event_payload.get("iteration_feedback")
    if not isinstance(raw, dict):
        return None
    return raw

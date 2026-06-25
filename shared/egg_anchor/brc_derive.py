"""Mechanical derivation of the #3189 deterministic BRC anchors.

From the BRC **message record** — the same list-of-dicts that
``read_peer_artifact`` and the orchestrator's ``_write_brc_history`` already
serialize — :func:`derive_brc_anchors` computes the four anchor fields the
protected root needs:

  (i)   last-reviewed SHA per producer (latest reviewed ``proposal_commit_sha``);
  (ii)  latest verdict per reviewer->producer edge (ACK / NACK / conditional-ACK);
  (iii) open NACK reasons (current-version NACKs not yet superseded);
  (iv)  conditional-ACK obligations (``pre_merge_condition``, resolved/unresolved).

The derivation is **purely mechanical**: it reads only structured message
fields (``message_type``, ``from_role``, ``to_role``, ``metadata``), never
agent-authored prose, so the anchors cannot drift from the record. It mirrors
the message-replay semantics of ``orchestrator.peer_consensus`` — proposal
versions advance on re-propose, and verdicts / obligations against a
superseded version become historical — without taking a dependency on the
orchestrator package.

This is the authoritative layer of the protected root: a threshold reseed
that re-derives these anchors from the record provably does not re-review a
settled SHA or drop a NACK obligation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .models import (
    BRCDerivedAnchors,
    ConditionalAckObligation,
    OpenNack,
    ReviewEdgeVerdict,
    ReviewVerdict,
)

CONSENSUS_PROPOSE = "CONSENSUS_PROPOSE"
CONSENSUS_ACK = "CONSENSUS_ACK"
CONSENSUS_NACK = "CONSENSUS_NACK"
CONSENSUS_OBLIGATION_RESOLVED = "CONSENSUS_OBLIGATION_RESOLVED"

__all__ = ["derive_brc_anchors"]


def _metadata(msg: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = msg.get("metadata")
    return meta if isinstance(meta, Mapping) else {}


def _payload(msg: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = _metadata(msg).get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _version(msg: Mapping[str, Any]) -> int | None:
    """Best-effort proposal version stamped on PROPOSE/ACK/NACK messages.

    Prefers the orchestrator-stamped ``metadata.version``; falls back to the
    ``ack_version`` / ``nack_version`` the BRC MCP tools carry in the payload.
    Returns None when no version is present (a pre-versioning historical
    message), letting the caller fall back to the producer's current version.
    """
    meta = _metadata(msg)
    payload = _payload(msg)
    for candidate in (
        meta.get("version"),
        payload.get("version"),
        payload.get("ack_version"),
        payload.get("nack_version"),
    ):
        if isinstance(candidate, bool):  # bool is an int subclass — reject
            continue
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None


def _commit_sha(msg: Mapping[str, Any]) -> str:
    meta = _metadata(msg)
    return str(meta.get("commit_sha") or _payload(msg).get("commit_sha") or "")


def _pre_merge_condition(msg: Mapping[str, Any]) -> str:
    meta = _metadata(msg)
    payload = _payload(msg)
    return str(meta.get("pre_merge_condition") or payload.get("pre_merge_condition") or "").strip()


def _resolved_in_diff(msg: Mapping[str, Any]) -> bool:
    meta = _metadata(msg)
    payload = _payload(msg)
    in_diff = str(
        meta.get("pre_merge_condition_resolved_in_diff")
        or payload.get("pre_merge_condition_resolved_in_diff")
        or ""
    ).strip()
    return bool(in_diff) or bool(payload.get("obligation_resolved"))


def _nack_reason(msg: Mapping[str, Any]) -> str:
    meta = _metadata(msg)
    payload = _payload(msg)
    # Structured reason only — never the free-form ``body`` prose, which would
    # make the anchor large and non-deterministic.
    return str(payload.get("reason") or meta.get("reason") or "").strip()


def _ordered(messages: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Chronological order for deterministic replay.

    The serialized record is already chronological, but when every message
    carries a timestamp we sort by ``(timestamp, id)`` to match the canonical
    replay tiebreak in ``orchestrator.peer_consensus`` and to be robust to a
    merged live+history stream. If any timestamp is missing we preserve input
    order (a stable, deterministic fallback).
    """
    msgs = list(messages)
    if msgs and all(m.get("timestamp") for m in msgs):
        return sorted(msgs, key=lambda m: (str(m.get("timestamp")), str(m.get("id") or "")))
    return msgs


def derive_brc_anchors(messages: Iterable[Mapping[str, Any]]) -> BRCDerivedAnchors:
    """Derive the four #3189 anchor fields from a BRC message record.

    ``messages`` is the serialized BRC message record (a list of message
    dicts as produced by ``read_peer_artifact`` / ``_write_brc_history``).
    Non-consensus message types are ignored. The result is deterministic for a
    given record: output lists are sorted by ``(producer, reviewer)`` and the
    SHA map by producer.
    """
    ordered = _ordered(messages)

    # producer -> current (latest) proposal version
    producer_version: dict[str, int] = {}
    # (producer, version) -> proposal_commit_sha
    version_sha: dict[tuple[str, int], str] = {}
    # (reviewer, producer) -> latest verdict state for that edge
    edges: dict[tuple[str, str], dict[str, Any]] = {}

    for msg in ordered:
        mtype = msg.get("message_type")

        if mtype == CONSENSUS_PROPOSE:
            producer = msg.get("from_role")
            if not producer:
                continue
            version = _version(msg)
            if version is None:
                version = producer_version.get(producer, 0) + 1
            producer_version[producer] = version
            sha = _commit_sha(msg)
            if sha:
                version_sha[(producer, version)] = sha

        elif mtype in (CONSENSUS_ACK, CONSENSUS_NACK):
            reviewer = msg.get("from_role")
            producer = msg.get("to_role")
            if not reviewer or not producer or producer == "all":
                continue
            version = _version(msg)
            if version is None:
                # No stamped version — attribute to the producer's current
                # proposal (the version under review when this verdict landed).
                version = producer_version.get(producer, 0)
            key = (reviewer, producer)
            if mtype == CONSENSUS_ACK:
                condition = _pre_merge_condition(msg)
                edges[key] = {
                    "verdict": (ReviewVerdict.CONDITIONAL_ACK if condition else ReviewVerdict.ACK),
                    "version": version,
                    "reason": "",
                    "condition": condition,
                    "resolved": _resolved_in_diff(msg),
                }
            else:  # CONSENSUS_NACK
                edges[key] = {
                    "verdict": ReviewVerdict.NACK,
                    "version": version,
                    "reason": _nack_reason(msg),
                    "condition": "",
                    "resolved": False,
                }

        elif mtype == CONSENSUS_OBLIGATION_RESOLVED:
            meta = _metadata(msg)
            reviewer = meta.get("reviewer_role")
            producer = meta.get("producer_role") or msg.get("to_role")
            if not reviewer or not producer:
                continue
            state = edges.get((reviewer, producer))
            if state and state["verdict"] == ReviewVerdict.CONDITIONAL_ACK:
                state["resolved"] = True

    return _assemble(producer_version, version_sha, edges)


def _assemble(
    producer_version: dict[str, int],
    version_sha: dict[tuple[str, int], str],
    edges: dict[tuple[str, str], dict[str, Any]],
) -> BRCDerivedAnchors:
    # Stable edge order: (producer, reviewer).
    edge_keys = sorted(edges, key=lambda k: (k[1], k[0]))

    # (i) last-reviewed SHA per producer: SHA of the highest version any
    #     reviewer has verdicted on for that producer.
    max_reviewed_version: dict[str, int] = {}
    for (_reviewer, producer), state in edges.items():
        version = state["version"]
        if version > max_reviewed_version.get(producer, -1):
            max_reviewed_version[producer] = version
    last_reviewed_sha: dict[str, str] = {}
    for producer in sorted(max_reviewed_version):
        sha = version_sha.get((producer, max_reviewed_version[producer]), "")
        if sha:
            last_reviewed_sha[producer] = sha

    # (ii) latest verdict per edge.
    latest_verdicts: list[ReviewEdgeVerdict] = [
        ReviewEdgeVerdict(
            reviewer=reviewer,
            producer=producer,
            verdict=edges[(reviewer, producer)]["verdict"],
            version=edges[(reviewer, producer)]["version"],
            reviewed_sha=version_sha.get((producer, edges[(reviewer, producer)]["version"]), ""),
        )
        for (reviewer, producer) in edge_keys
    ]

    # (iii) open NACKs: latest verdict is NACK AND it targets the producer's
    #       current proposal version (older NACKs are superseded).
    open_nacks: list[OpenNack] = []
    # (iv) conditional-ACK obligations: latest verdict is a conditional ACK
    #      against the current proposal version (re-propose clears obligations).
    obligations: list[ConditionalAckObligation] = []
    for reviewer, producer in edge_keys:
        state = edges[(reviewer, producer)]
        current = producer_version.get(producer, state["version"])
        if state["version"] != current:
            continue
        if state["verdict"] == ReviewVerdict.NACK:
            open_nacks.append(
                OpenNack(
                    reviewer=reviewer,
                    producer=producer,
                    version=state["version"],
                    reason=state["reason"],
                )
            )
        elif state["verdict"] == ReviewVerdict.CONDITIONAL_ACK:
            obligations.append(
                ConditionalAckObligation(
                    reviewer=reviewer,
                    producer=producer,
                    version=state["version"],
                    condition=state["condition"],
                    resolved=state["resolved"],
                )
            )

    return BRCDerivedAnchors(
        last_reviewed_sha=last_reviewed_sha,
        latest_verdicts=latest_verdicts,
        open_nacks=open_nacks,
        conditional_ack_obligations=obligations,
    )

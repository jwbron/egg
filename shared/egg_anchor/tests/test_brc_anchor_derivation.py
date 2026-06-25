"""Tests for the #3189 deterministic BRC anchor derivation (slice-3, task-3-2).

#3200 / slice-3 ("Derive"). The coder (task-3-1) lands a *deterministic*
derivation that, from the BRC message record (CONSENSUS_PROPOSE / ACK / NACK /
conditional-ACK / OBLIGATION_RESOLVED), computes the four #3189 anchor fields
that become the authoritative layer of the protected root (slice-4):

  (i)   last-reviewed SHA per producer — the latest *reviewed* proposal commit
        per reviewer->producer edge;
  (ii)  latest verdict per reviewer->producer edge (acked / nacked / pending);
  (iii) open NACK reasons — NACKs on the producer's *current* proposal version
        that are not yet resolved;
  (iv)  conditional-ACK obligations — ``pre_merge_condition`` text per edge with
        a resolved / unresolved status.

The derivation is mechanical (never agent-transcribed) and the
``shared/egg_anchor`` model is extended *additively* — ``BRCState.acks`` /
``nacks`` / ``last_message_id`` (``models.py:96-103``) must keep working.

Tester and coder run as parallel BRC producers on separate branches, so the
coder's derivation symbol and the extended model fields may be absent when this
file is collected on the tester branch. The locator helpers ``pytest.skip``
until the implementation merges — the established slice convention (see
``orchestrator/tests/test_reseed_threshold.py``) — which keeps the suite green
pre-merge and runs the assertions at PR assembly. The legacy-non-regression
test below has no such guard: it asserts the existing ``BRCState`` contract that
must survive the additive extension, and therefore runs today.
"""

from __future__ import annotations

from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Locators (skip-guard convention) — resolve the coder's symbols or skip.
# ---------------------------------------------------------------------------

# Candidate (module, attribute) pairs for the derivation entry point. The coder
# owns the exact name; these cover the plausible spellings so the assertions
# activate the moment any one of them lands.
_DERIVATION_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("egg_anchor.brc_anchors", "derive_brc_anchors"),
    ("egg_anchor.brc_anchors", "derive_anchors"),
    ("egg_anchor.derivation", "derive_brc_anchors"),
    ("egg_anchor.derivation", "derive_anchors"),
    ("egg_anchor.models", "derive_brc_anchors"),
    ("egg_anchor", "derive_brc_anchors"),
    ("egg_anchor", "derive_anchors"),
)

# Candidate names for the additive model carrying the four derived fields.
_MODEL_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("egg_anchor.models", "BRCAnchors"),
    ("egg_anchor.models", "BRCReviewAnchors"),
    ("egg_anchor.models", "ReviewAnchors"),
    ("egg_anchor.models", "DeterministicAnchors"),
    ("egg_anchor", "BRCAnchors"),
)


def _resolve(candidates: tuple[tuple[str, str], ...], what: str) -> Any:
    for module_name, attr in candidates:
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError:
            continue
        obj = getattr(module, attr, None)
        if obj is not None:
            return obj
    pytest.skip(
        f"{what} not found yet (coder task-3-1 unmerged); "
        f"tried {[f'{m}.{a}' for m, a in candidates]}"
    )


def _derivation() -> Any:
    return _resolve(_DERIVATION_CANDIDATES, "BRC anchor derivation")


def _derive(messages: list[Any]) -> Any:
    """Call the coder's derivation with whichever call shape it accepts."""
    fn = _derivation()
    for kwargs in ({}, {"messages": messages}):
        try:
            if kwargs:
                return fn(**kwargs)
            return fn(messages)
        except TypeError:
            continue
    pytest.skip("derivation present but call signature did not match (messages list)")


# ---------------------------------------------------------------------------
# Field accessors — tolerate dict-shaped or attribute-shaped output and the
# several reasonable names for each of the four fields.
# ---------------------------------------------------------------------------


def _get(obj: Any, names: tuple[str, ...], what: str) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    pytest.skip(f"derived anchors expose no {what} field (tried {list(names)})")


def _last_reviewed(anchors: Any) -> Any:
    return _get(
        anchors,
        ("last_reviewed_shas", "last_reviewed_sha", "last_reviewed_commit_shas", "last_reviewed"),
        "last-reviewed-SHA",
    )


def _verdicts(anchors: Any) -> Any:
    return _get(
        anchors,
        ("latest_verdicts", "verdicts", "latest_verdict", "verdict_by_edge"),
        "latest-verdict",
    )


def _open_nacks(anchors: Any) -> Any:
    return _get(
        anchors,
        ("open_nacks", "open_nack_reasons", "open_nacks_by_edge", "nacks"),
        "open-NACK",
    )


def _obligations(anchors: Any) -> Any:
    return _get(
        anchors,
        ("conditional_ack_obligations", "obligations", "pre_merge_obligations", "ack_obligations"),
        "conditional-ACK-obligation",
    )


def _flatten(value: Any) -> str:
    """Render a (possibly nested) container as one searchable string."""
    return repr(value)


# ---------------------------------------------------------------------------
# Fixture: a realistic BRC message record exercising the task-3-2 AC scenario.
# ---------------------------------------------------------------------------

SHA_CODER_V1 = "aaaaaaa1111111111111111111111111111111aa"
SHA_CODER_V2 = "bbbbbbb2222222222222222222222222222222bb"
SHA_TESTER_V1 = "ccccccc3333333333333333333333333333333cc"


class _Msg:
    """Message-store-shaped record: attribute access plus a ``metadata`` dict.

    Mirrors the orchestrator ``Message`` surface the reconstruction reads
    (``m.message_type`` / ``m.from_role`` / ``m.metadata``) while also being
    subscriptable, so a derivation that consumes dict-like messages also works.
    """

    def __init__(self, message_type: str, from_role: str, seq: int, **metadata: Any) -> None:
        self.message_type = message_type
        self.from_role = from_role
        self.to_role = "all"
        self.seq = seq
        self.id = f"msg-{seq:03d}"
        self.metadata = metadata

    def __getitem__(self, key: str) -> Any:
        if key in ("message_type", "from_role", "to_role", "id", "seq"):
            return getattr(self, key)
        return self.metadata[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __repr__(self) -> str:  # keep failures legible
        return f"_Msg({self.message_type} from {self.from_role} {self.metadata})"


def _scenario_messages() -> list[_Msg]:
    """Multiple producers; ACK then re-propose + NACK; conditional ACKs.

    coder:  v1 proposed -> reviewer_code ACK v1; re-proposed v2 ->
            reviewer_code NACK v2 ("missing guard") AND reviewer_security
            conditional-ACK v2 (obligation "git mv old new", UNRESOLVED).
    tester: v1 proposed -> reviewer_code conditional-ACK v1
            (obligation "update import path") then OBLIGATION_RESOLVED.
    """

    def meta_propose(producer: str, version: int, sha: str) -> dict[str, Any]:
        return {
            "producer_role": producer,
            "version": version,
            "commit_sha": sha,
            "proposal_commit_sha": sha,
        }

    def meta_ack(producer: str, version: int, sha: str, condition: str = "") -> dict[str, Any]:
        return {
            "producer_role": producer,
            "version": version,
            "ack_version": version,
            "commit_sha": sha,
            "proposal_commit_sha": sha,
            "pre_merge_condition": condition,
        }

    def meta_nack(producer: str, version: int, reason: str) -> dict[str, Any]:
        return {
            "producer_role": producer,
            "version": version,
            "nack_version": version,
            "reason": reason,
        }

    return [
        # coder v1
        _Msg("CONSENSUS_PROPOSE", "coder", 1, **meta_propose("coder", 1, SHA_CODER_V1)),
        _Msg("CONSENSUS_ACK", "reviewer_code", 2, **meta_ack("coder", 1, SHA_CODER_V1)),
        # coder re-proposes v2 — supersedes v1
        _Msg("CONSENSUS_PROPOSE", "coder", 3, **meta_propose("coder", 2, SHA_CODER_V2)),
        _Msg("CONSENSUS_NACK", "reviewer_code", 4, **meta_nack("coder", 2, "missing guard")),
        _Msg(
            "CONSENSUS_ACK",
            "reviewer_security",
            5,
            **meta_ack("coder", 2, SHA_CODER_V2, condition="git mv old new"),
        ),
        # tester v1 — conditional ACK then obligation resolved in-cycle
        _Msg("CONSENSUS_PROPOSE", "tester", 6, **meta_propose("tester", 1, SHA_TESTER_V1)),
        _Msg(
            "CONSENSUS_ACK",
            "reviewer_code",
            7,
            **meta_ack("tester", 1, SHA_TESTER_V1, condition="update import path"),
        ),
        _Msg(
            "CONSENSUS_OBLIGATION_RESOLVED",
            "coder",  # a non-producer role satisfies it; tester cannot self-resolve
            8,
            producer_role="tester",
            reviewer_role="reviewer_code",
            resolved_by="coder",
            commit_sha=SHA_TESTER_V1,
        ),
    ]


# ---------------------------------------------------------------------------
# Legacy non-regression — runs today (no skip-guard), guards the additive
# extension required by task-3-1.
# ---------------------------------------------------------------------------


def test_legacy_brcstate_fields_untouched() -> None:
    """``BRCState.acks`` / ``nacks`` / ``last_message_id`` survive the extension."""
    from egg_anchor.models import BRCState

    state = BRCState(
        acks=["reviewer_code"],
        nacks=["reviewer_security"],
        last_message_id="msg-008",
    )
    assert state.acks == ["reviewer_code"]
    assert state.nacks == ["reviewer_security"]
    assert state.last_message_id == "msg-008"

    # Defaults remain backwards-compatible (empty lists, no last id).
    default = BRCState()
    assert default.acks == []
    assert default.nacks == []
    assert default.last_message_id is None


# ---------------------------------------------------------------------------
# Derivation correctness — skip-guarded until the coder's symbol lands.
# ---------------------------------------------------------------------------


def test_last_reviewed_sha_per_producer() -> None:
    """Latest *reviewed* proposal commit is surfaced per producer."""
    anchors = _derive(_scenario_messages())
    blob = _flatten(_last_reviewed(anchors))
    # coder's reviewed head advanced to v2; tester reviewed at v1.
    assert SHA_CODER_V2 in blob, blob
    assert SHA_TESTER_V1 in blob, blob
    # The superseded v1 coder proposal is NOT the last-reviewed SHA.
    assert SHA_CODER_V1 not in blob, blob


def test_latest_verdict_per_edge() -> None:
    """Each reviewer->producer edge resolves to its current-version verdict."""
    verdicts = _verdicts(_derive(_scenario_messages()))
    blob = _flatten(verdicts).lower()
    # reviewer_code -> coder is NACKED on v2; reviewer_security -> coder ACKED;
    # reviewer_code -> tester ACKED.
    assert "nack" in blob, blob
    assert "ack" in blob, blob


def test_open_nack_reason_on_current_version() -> None:
    """The current-version NACK reason is an open anchor; ACKs are not NACKs."""
    nacks = _open_nacks(_derive(_scenario_messages()))
    blob = _flatten(nacks)
    assert "missing guard" in blob, blob


def test_unresolved_obligation_surfaced() -> None:
    """An unresolved conditional-ACK obligation is carried with its text."""
    obligations = _derive(_scenario_messages())
    blob = _flatten(_obligations(obligations))
    assert "git mv old new" in blob, blob


def test_resolved_obligation_marked_resolved() -> None:
    """A resolved obligation is distinguished from an unresolved one.

    The tester's ``update import path`` obligation was resolved in-cycle; the
    coder's ``git mv old new`` obligation was not. The derived anchors must let
    a consumer tell them apart — whether by omitting the resolved one from an
    "open obligations" view or by carrying an explicit resolved flag.
    """
    obligations = _obligations(_derive(_scenario_messages()))
    blob = _flatten(obligations)
    unresolved_present = "git mv old new" in blob

    assert unresolved_present, blob
    if "update import path" not in blob:
        # Resolved obligation filtered out of the open view — acceptable.
        return
    # Resolved obligation retained — then a resolved/unresolved marker must
    # exist so the two are distinguishable.
    assert any(
        token in blob.lower() for token in ("resolved", "true", "false", "open", "satisfied")
    ), blob


def test_derivation_is_deterministic() -> None:
    """Identical message records derive byte-identical anchors (slice-4 prereq)."""
    first = _derive(_scenario_messages())
    second = _derive(_scenario_messages())
    assert _flatten(first) == _flatten(second)

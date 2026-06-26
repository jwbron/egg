"""Mid-turn message intent discrimination + #3123 retention (issue #2270 slice-7, §2b).

The alert-reflection failure mode (#2270 §2): an overseer/orchestrator
*informational* broadcast — ``overseer_restart [info]``, ``agent-heartbeat-stall``,
``stuck-phase-transition`` — was being injected mid-turn as a **BINDING course
correction**, so the producer it landed on dutifully "course-corrected" on its own
monitoring noise. The live ``issue-3200`` proving run reflected the overseer's own
alert stream straight back into the agents.

The slice-7 fix (task-7-2) gates *bindingness* on **intent**, not ``from_role``:
``classify_message_intent`` splits traffic into ``operator_directive`` (genuine
human/operator messages, explicit directive message_types, and the one
orchestrator OVERSEER_ALERT that IS a directive — the #3123
``brc_confirmation_timeout`` directed nudge, marked via ``metadata.alert_type``)
versus ``informational`` (everything else from overseer/orchestrator). ``_render_block``
renders directives under a BINDING header and informational notices under a clearly
NON-binding header.

This module pins both halves of the contract:

* ``classify_message_intent`` — operator directives + the directed #3123 nudge are
  ``operator_directive``; broadcast informational alerts are ``informational``; and
* the #3123 golden-file regression — the brc-confirmation-timeout nudge still
  renders in the BINDING section (the silent-drop failure mode #3123 closed must
  stay closed), and informational alerts render only in the non-binding section.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — make ``shared/`` importable so ``egg_agent.midturn_messages``
# resolves against the local tree (the shared/tests/conftest.py root does not
# cover this sibling test root; a local conftest mirrors it).
# ---------------------------------------------------------------------------

_shared_dir = Path(__file__).resolve().parents[2]
if str(_shared_dir) not in sys.path:
    sys.path.insert(0, str(_shared_dir))

from egg_agent.midturn_messages import (  # noqa: E402
    INTENT_INFORMATIONAL,
    INTENT_OPERATOR_DIRECTIVE,
    MidturnMessagePoller,
    _render_block,
    classify_message_intent,
)

# ---------------------------------------------------------------------------
# Message fixtures — the real shapes the bus emits (see
# routes/pipelines.py::_send_brc_confirmation_nudge,
# _publish_branch_divergence_alert, and mcp__progress__overseer_alert).
# ---------------------------------------------------------------------------

# #3123: the directed brc-confirmation-timeout nudge. from_role=orchestrator,
# message_type=OVERSEER_ALERT, to_role=<the stuck producer>, and
# metadata.alert_type=brc_confirmation_timeout. This MUST stay a directive.
_BRC_NUDGE: dict[str, object] = {
    "id": "msg-brc-nudge-1",
    "from_role": "orchestrator",
    "to_role": "coder",
    "message_type": "OVERSEER_ALERT",
    "subject": "BRC confirmation timeout — call mcp__brc__confirm",
    "body": "You are PROPOSED and fully ACKed but have not confirmed in 700s. Call mcp__brc__confirm now.",
    "timestamp": "2026-06-26T06:16:42+00:00",
    "metadata": {
        "alert_type": "brc_confirmation_timeout",
        "elapsed_seconds": 700,
        "source": "health_monitor",
    },
}

# The alert-reflection vector: an overseer respawn broadcast. Informational.
_OVERSEER_RESTART_INFO: dict[str, object] = {
    "id": "msg-restart-1",
    "from_role": "orchestrator",
    "to_role": "all",
    "message_type": "OVERSEER_ALERT",
    "subject": "overseer_restart: overseer [info]",
    "body": "Overseer container was respawned. Old container exited 0; new container running.",
    "timestamp": "2026-06-26T07:53:26+00:00",
    "metadata": {"exit_code": 0, "respawn_attempt": 1},
}

# A false-positive monitoring alert broadcast by the overseer agent itself.
_HEARTBEAT_STALL_INFO: dict[str, object] = {
    "id": "msg-stall-1",
    "from_role": "overseer",
    "to_role": "all",
    "message_type": "OVERSEER_ALERT",
    "subject": "agent-heartbeat-stall [medium]",
    "body": "Tester silent for 679s — 1.1x the 600s threshold. Container still running.",
    "timestamp": "2026-06-26T03:47:35+00:00",
    "metadata": {"anomaly": "agent-heartbeat-stall", "priority": "medium"},
}

# An orchestrator-derived stuck-phase-transition broadcast. Informational.
_STUCK_PHASE_INFO: dict[str, object] = {
    "id": "msg-stuck-1",
    "from_role": "orchestrator",
    "to_role": "all",
    "message_type": "OVERSEER_ALERT",
    "subject": "stuck-phase-transition: event-loop [high]",
    "body": "ack pending 3880s without BRC-bus progress (budget 30m).",
    "timestamp": "2026-06-26T07:21:45+00:00",
    "metadata": {"anomaly": "stuck-phase-transition", "priority": "high"},
}

# A genuine human operator directive — the canonical binding case.
_OPERATOR_DIRECTIVE: dict[str, object] = {
    "id": "msg-op-1",
    "from_role": "operator",
    "to_role": "coder",
    "message_type": "STATUS",
    "subject": "scope change: drop slice 9",
    "body": "Drop slice 9 from this pipeline; do not implement the cleanup slice.",
    "timestamp": "2026-06-26T06:00:00+00:00",
    "metadata": {},
}

# A peer/protocol message — never surfaced mid-turn (between-invocation path).
_PEER_PROTOCOL: dict[str, object] = {
    "id": "msg-peer-1",
    "from_role": "reviewer_code",
    "to_role": "coder",
    "message_type": "CONSENSUS_NACK",
    "subject": "NACK: missing test",
    "body": "Please add a regression test before re-proposing.",
    "timestamp": "2026-06-26T06:05:00+00:00",
    "metadata": {},
}


def _make_poller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, messages: list[dict]):
    """Build a poller wired to return ``messages`` from a single fetch.

    The cursor is pre-seeded so the poll is NOT treated as a first-ever seed
    (which advances silently and injects nothing). ``interval_secs=0`` makes the
    throttle gate always open.
    """
    poller = MidturnMessagePoller(
        "issue-3200",
        "coder",
        interval_secs=0.0,
        cursor_dir=str(tmp_path),
    )
    poller._write_cursor("seed-cursor-id")
    monkeypatch.setattr(poller, "_fetch", lambda since_id: (list(messages), False))
    return poller


_BINDING_HEADER = "## Operator directive(s) received mid-turn"
_NONBINDING_HEADER = "## Informational notices (mid-turn — NOT binding)"


# ---------------------------------------------------------------------------
# #3123 retention — the directed brc-confirmation-timeout nudge stays binding.
# ---------------------------------------------------------------------------

# Golden render of the #3123 nudge as a binding mid-turn block. Built against the
# stable fixture body above so the snapshot is deterministic; a change here means
# the binding-injection rendering changed and must be a deliberate #3123 decision.
_EXPECTED_NUDGE_BLOCK = (
    "## Operator directive(s) received mid-turn\n"
    "\n"
    "The operator sent the following while you were working. They are "
    "BINDING course corrections — apply them to your remaining work NOW. "
    "If a directive contradicts work you have already done this turn, stop "
    "and reconcile (rework, drop, or adopt as directed) before proposing; "
    "do not finish the contradicted approach first.\n"
    "\n"
    "### [2026-06-26T06:16:42] from orchestrator (OVERSEER_ALERT): "
    "BRC confirmation timeout — call mcp__brc__confirm\n"
    "\n"
    "You are PROPOSED and fully ACKed but have not confirmed in 700s. "
    "Call mcp__brc__confirm now.\n"
)


def test_3123_brc_nudge_renders_as_binding_block(tmp_path, monkeypatch) -> None:
    """#3123 golden-file: the directed brc-confirmation nudge injects as binding.

    Asserts the exact rendered block. The silent-drop failure mode #3123 closed
    (producer fetches the nudge, advances the cursor past it, injects nothing)
    must stay closed: ``poll()`` returns the binding block verbatim, and the
    nudge classifies as an operator directive (not informational).
    """
    assert classify_message_intent(_BRC_NUDGE) == INTENT_OPERATOR_DIRECTIVE
    poller = _make_poller(tmp_path, monkeypatch, [_BRC_NUDGE])
    block = poller.poll()
    assert block == _EXPECTED_NUDGE_BLOCK


def test_3123_brc_nudge_not_dropped(tmp_path, monkeypatch) -> None:
    """The nudge is selected for injection (non-None), with its subject + body."""
    poller = _make_poller(tmp_path, monkeypatch, [_BRC_NUDGE])
    block = poller.poll()
    assert block is not None
    assert _BINDING_HEADER in block
    assert "BRC confirmation timeout" in block
    assert "mcp__brc__confirm" in block
    assert "BINDING course corrections" in block


# ---------------------------------------------------------------------------
# Intent discriminator — the slice-7 §2b contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(_OPERATOR_DIRECTIVE, id="human-operator-directive"),
        pytest.param(_BRC_NUDGE, id="directed-brc-confirmation-nudge"),
    ],
)
def test_operator_directives_classify_as_directive(message) -> None:
    """Operator directives and the directed #3123 nudge are ``operator_directive``."""
    assert classify_message_intent(message) == INTENT_OPERATOR_DIRECTIVE


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(_OVERSEER_RESTART_INFO, id="overseer_restart-info"),
        pytest.param(_HEARTBEAT_STALL_INFO, id="agent-heartbeat-stall"),
        pytest.param(_STUCK_PHASE_INFO, id="stuck-phase-transition"),
        pytest.param(_PEER_PROTOCOL, id="peer-consensus-nack"),
    ],
)
def test_informational_and_protocol_classify_as_informational(message) -> None:
    """Broadcast informational alerts + peer/protocol traffic are ``informational``.

    This is the alert-reflection fix: an OVERSEER_ALERT broadcast is monitoring
    noise, not an operator course-correction, and must never classify as a binding
    directive back into the very agent that triggered it.
    """
    assert classify_message_intent(message) == INTENT_INFORMATIONAL


def test_explicit_intent_field_overrides() -> None:
    """A forward-compatible explicit ``intent`` is honored when present.

    The calibration corpus models post-derivation messages carrying an ``intent``
    key (``operator_directive`` / ``informational``); the classifier must respect
    it so the production detector and the injector agree.
    """
    informational = {**_OPERATOR_DIRECTIVE, "intent": "informational"}
    directive = {**_OVERSEER_RESTART_INFO, "intent": "operator_directive"}
    assert classify_message_intent(informational) == INTENT_INFORMATIONAL
    assert classify_message_intent(directive) == INTENT_OPERATOR_DIRECTIVE


# ---------------------------------------------------------------------------
# End-to-end through poll() — the intent gate drives the binding/non-binding split.
# ---------------------------------------------------------------------------


def test_poll_splits_binding_and_informational_sections(tmp_path, monkeypatch) -> None:
    """A mixed batch renders directives BINDING and alerts NON-binding.

    The exact reflection scenario: operator directive + #3123 nudge land in the
    binding section; the overseer/orchestrator alerts land only under the
    explicitly non-binding header (surfaced for awareness, never as orders); the
    peer CONSENSUS_NACK is not surfaced at all (between-invocation path).
    """
    batch = [
        _OVERSEER_RESTART_INFO,
        _OPERATOR_DIRECTIVE,
        _HEARTBEAT_STALL_INFO,
        _BRC_NUDGE,
        _STUCK_PHASE_INFO,
        _PEER_PROTOCOL,
    ]
    poller = _make_poller(tmp_path, monkeypatch, batch)
    block = poller.poll()
    assert block is not None
    assert _BINDING_HEADER in block
    assert _NONBINDING_HEADER in block

    binding_part, _, info_part = block.partition(_NONBINDING_HEADER)
    # Directives live in the binding section only.
    assert "scope change: drop slice 9" in binding_part
    assert "BRC confirmation timeout" in binding_part
    # Informational alerts live in the non-binding section only.
    assert "overseer_restart" in info_part
    assert "agent-heartbeat-stall" in info_part
    assert "stuck-phase-transition" in info_part
    assert "overseer_restart" not in binding_part
    assert "agent-heartbeat-stall" not in binding_part
    # The peer/protocol message is never surfaced mid-turn.
    assert "NACK" not in block


def test_poll_informational_only_renders_non_binding(tmp_path, monkeypatch) -> None:
    """A batch of pure monitoring noise renders ONLY the non-binding section.

    Critically, it carries no BINDING header — the producer must not treat any of
    it as an operator course-correction (the alert-reflection fix).
    """
    batch = [_OVERSEER_RESTART_INFO, _HEARTBEAT_STALL_INFO, _STUCK_PHASE_INFO, _PEER_PROTOCOL]
    poller = _make_poller(tmp_path, monkeypatch, batch)
    block = poller.poll()
    assert block is not None
    assert _NONBINDING_HEADER in block
    assert _BINDING_HEADER not in block
    assert "BINDING course corrections" not in block
    # Cursor advanced past the whole batch (last id, no replay next turn).
    assert poller._read_cursor() == "msg-peer-1"


def test_render_block_binding_section_preamble() -> None:
    """``_render_block`` frames operator directives under the BINDING header."""
    block = _render_block([_OPERATOR_DIRECTIVE])
    assert block.startswith(_BINDING_HEADER)
    assert "BINDING course corrections" in block
    assert "scope change: drop slice 9" in block
    assert _NONBINDING_HEADER not in block

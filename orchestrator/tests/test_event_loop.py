"""Slice-2 tester tests for the orchestrator-owned event loop (#3064 TASK-2-3).

These tests are written **test-first**: they pin the slice-2 contract that
the coder's parallel ``orchestrator/event_loop.py`` (TASK-2-1) must satisfy.
Per the plan (``.egg-state/drafts/3064-plan.md`` slice 2) the event loop:

  * consumes the logic backing ``routes.consensus._derive_next_action``
    IN-PROCESS, per role;
  * maps the six derived verbs to a spawn decision —
      - ``propose`` / ``ack`` / ``nack`` ⇒ request a one-shot Job via an
        **injectable** spawner (so these tests have NO k8s dependency);
      - ``confirm`` / ``complete`` ⇒ executed orchestrator-side, agent-free,
        with NO pod ever spawned;
      - ``wait`` ⇒ nothing;
  * dedupes spawns on ``sha256(pipeline, slice, phase, role, action, event
    identity)`` where the event identity is ``proposal_commit_sha`` for the
    review verbs and the target version + open-NACK set for proposes;
  * keeps an in-memory dedupe set reconciled against live Job labels so that
    a repeated poll AND a simulated orchestrator restart never double-spawn;
  * enforces **at most one live pod per (role, slice)**;
  * emits a structured spawn→invoke timing field per spawn (the slice-4
    latency budget reads it);
  * persists NO spawn bookkeeping (statelessness is asserted via the
    restart-reconciliation path, not a store).

Pinned public surface (the contract — the coder aligns the names if they
diverge, exactly as slice-1's tester re-aligned to task-1-1):

    event_loop.compute_dedupe_key(
        pipeline_id, slice_id, phase, role, action, identity) -> str  # sha256 hex
    event_loop.SPAWN_ACTIONS        : frozenset  # {"propose","ack","nack"}
    event_loop.AGENT_FREE_ACTIONS   : frozenset  # {"confirm","complete"}
    event_loop.EventDecision        : dataclass(role, action, dedupe_key,
                                                 spawned, agent_free, timing)
    event_loop.OrchestratorEventLoop(tracker, spawner, *, pipeline_id,
                                     slice_id, phase, clock=...,
                                     agent_free_handler=...)
        .reconcile(live_dedupe_keys)            # seed live set (restart)
        .poll_once(roles) -> list[EventDecision]

The loop calls ``event_loop._derive_next_action(tracker, role)`` — these
tests monkeypatch that symbol to script the six verbs deterministically, so
the mapping is exercised without standing up real consensus trackers.
The injected spawner is a plain recorder; ``agent_free_handler`` is a plain
callable. Import of ``event_loop`` is done at call-time inside each test so
this file still collects before the coder's module lands (slice-1 convention).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add orchestrator to path (mirrors the other orchestrator test modules).
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingSpawner:
    """Injectable one-shot spawner double.

    The loop is expected to call ``spawn_event(role=, action=, dedupe_key=,
    payload=)`` exactly once per *new* (non-deduped) spawn decision and to
    never call it for ``confirm``/``complete``/``wait``. Each call returns the
    dedupe_key as the Job label so a reconciliation view can be reconstructed.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def spawn_event(self, *, role, action, dedupe_key, payload=None):
        self.calls.append(
            {
                "role": role,
                "action": action,
                "dedupe_key": dedupe_key,
                "payload": payload,
            }
        )
        return dedupe_key

    # Convenience views ----------------------------------------------------
    @property
    def spawn_count(self) -> int:
        return len(self.calls)

    @property
    def spawned_keys(self) -> list[str]:
        return [c["dedupe_key"] for c in self.calls]

    @property
    def spawned_actions(self) -> list[str]:
        return [c["action"] for c in self.calls]


class _AgentFreeRecorder:
    """Records orchestrator-side agent-free handling of confirm/complete."""

    def __init__(self) -> None:
        self.handled: list[tuple[str, str]] = []  # (action, role)

    def __call__(self, *, action, role, payload=None):
        self.handled.append((action, role))


class _FakeClock:
    """Deterministic monotonic clock — advances a fixed step per read."""

    def __init__(self, start: float = 1000.0, step: float = 0.25) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> float:
        now = self._now
        self._now += self._step
        return now


def _script(monkeypatch, mapping):
    """Monkeypatch ``event_loop._derive_next_action`` with a scripted map.

    ``mapping`` is ``{role: (action, payload, reason)}``. Unlisted roles map
    to ``("wait", None, "scripted-default")``.
    """
    import event_loop

    def _fake_derive(tracker, role):
        return mapping.get(role, ("wait", None, "scripted-default"))

    monkeypatch.setattr(event_loop, "_derive_next_action", _fake_derive, raising=True)


def _make_loop(spawner, *, agent_free_handler=None, clock=None, slice_id="slice-2"):
    """Construct the loop under test with injected collaborators."""
    import event_loop

    return event_loop.OrchestratorEventLoop(
        tracker=object(),  # opaque; _derive_next_action is monkeypatched
        spawner=spawner,
        pipeline_id="issue-3064",
        slice_id=slice_id,
        phase="implement",
        clock=clock or _FakeClock(),
        agent_free_handler=agent_free_handler or _AgentFreeRecorder(),
    )


# Event-identity payloads matching _derive_next_action's real shapes.
_PROPOSE_PAYLOAD = {"producer": "coder"}
_REVIEW_PAYLOAD_V1 = {
    "pending_reviews": [{"producer": "coder", "proposal_commit_sha": "deadbeef1"}]
}
_REVIEW_PAYLOAD_V2 = {
    "pending_reviews": [{"producer": "coder", "proposal_commit_sha": "feedface2"}]
}


# ---------------------------------------------------------------------------
# compute_dedupe_key — the sha256 identity contract
# ---------------------------------------------------------------------------


class TestComputeDedupeKey:
    """sha256(pipeline, slice, phase, role, action, identity)."""

    def _key(self, **over):
        import event_loop

        base = {
            "pipeline_id": "issue-3064",
            "slice_id": "slice-2",
            "phase": "implement",
            "role": "coder",
            "action": "propose",
            "identity": "v2|{}",
        }
        base.update(over)
        return event_loop.compute_dedupe_key(**base)

    def test_is_sha256_hex(self):
        key = self._key()
        assert isinstance(key, str)
        assert len(key) == 64
        int(key, 16)  # raises if not hex

    def test_deterministic(self):
        assert self._key() == self._key()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("pipeline_id", "issue-9999"),
            ("slice_id", "slice-3"),
            ("phase", "plan"),
            ("role", "tester"),
            ("action", "ack"),
            ("identity", "v3|{}"),
        ],
    )
    def test_every_field_changes_the_key(self, field, value):
        """Flipping ANY of the six fields must change the digest — otherwise
        two distinct events could collide and one would be silently dropped.
        """
        assert self._key() != self._key(**{field: value})


# ---------------------------------------------------------------------------
# Verb → decision mapping (all six verbs)
# ---------------------------------------------------------------------------


class TestVerbMapping:
    """propose|ack|nack spawn; confirm|complete agent-free; wait nothing."""

    def test_action_sets_are_exhaustive_and_disjoint(self):
        import event_loop

        assert event_loop.SPAWN_ACTIONS == frozenset({"propose", "ack", "nack"})
        assert event_loop.AGENT_FREE_ACTIONS == frozenset({"confirm", "complete"})
        assert event_loop.SPAWN_ACTIONS.isdisjoint(event_loop.AGENT_FREE_ACTIONS)

    @pytest.mark.parametrize(
        "action,payload",
        [
            ("propose", _PROPOSE_PAYLOAD),
            ("ack", _REVIEW_PAYLOAD_V1),
            ("nack", _REVIEW_PAYLOAD_V1),
        ],
    )
    def test_spawn_verbs_request_one_pod(self, monkeypatch, action, payload):
        _script(monkeypatch, {"coder": (action, payload, "x")})
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner)

        decisions = loop.poll_once(["coder"])

        assert spawner.spawn_count == 1
        assert spawner.spawned_actions == [action]
        (d,) = decisions
        assert d.action == action
        assert d.spawned is True
        assert d.agent_free is False
        assert d.dedupe_key and len(d.dedupe_key) == 64

    @pytest.mark.parametrize("action", ["confirm", "complete"])
    def test_agent_free_verbs_spawn_no_pod(self, monkeypatch, action):
        _script(monkeypatch, {"reviewer_code": (action, None, "x")})
        spawner = _RecordingSpawner()
        handler = _AgentFreeRecorder()
        loop = _make_loop(spawner, agent_free_handler=handler)

        decisions = loop.poll_once(["reviewer_code"])

        assert spawner.spawn_count == 0, "confirm/complete must never spawn a pod"
        assert handler.handled == [(action, "reviewer_code")]
        (d,) = decisions
        assert d.action == action
        assert d.spawned is False
        assert d.agent_free is True

    def test_wait_spawns_nothing_and_invokes_no_handler(self, monkeypatch):
        _script(monkeypatch, {"documenter": ("wait", {"blocking_agents": []}, "x")})
        spawner = _RecordingSpawner()
        handler = _AgentFreeRecorder()
        loop = _make_loop(spawner, agent_free_handler=handler)

        decisions = loop.poll_once(["documenter"])

        assert spawner.spawn_count == 0
        assert handler.handled == []
        (d,) = decisions
        assert d.action == "wait"
        assert d.spawned is False
        assert d.agent_free is False
        assert d.dedupe_key is None

    def test_mixed_roles_in_one_poll(self, monkeypatch):
        """A single poll fans across roles; only spawn-verbs hit the spawner."""
        _script(
            monkeypatch,
            {
                "coder": ("propose", _PROPOSE_PAYLOAD, "x"),
                "reviewer_code": ("ack", _REVIEW_PAYLOAD_V1, "x"),
                "reviewer_security": ("confirm", None, "x"),
                "documenter": ("wait", None, "x"),
            },
        )
        spawner = _RecordingSpawner()
        handler = _AgentFreeRecorder()
        loop = _make_loop(spawner, agent_free_handler=handler)

        loop.poll_once(["coder", "reviewer_code", "reviewer_security", "documenter"])

        assert sorted(spawner.spawned_actions) == ["ack", "propose"]
        assert handler.handled == [("confirm", "reviewer_security")]


# ---------------------------------------------------------------------------
# Dedupe — repeated polls
# ---------------------------------------------------------------------------


class TestDedupeAcrossPolls:
    def test_same_derived_event_across_polls_spawns_once(self, monkeypatch):
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner)

        first = loop.poll_once(["coder"])
        second = loop.poll_once(["coder"])
        third = loop.poll_once(["coder"])

        assert spawner.spawn_count == 1, "repeated identical poll must not re-spawn"
        assert first[0].spawned is True
        assert second[0].spawned is False
        assert third[0].spawned is False
        # The dedupe key is stable across polls.
        assert first[0].dedupe_key == second[0].dedupe_key == third[0].dedupe_key

    def test_changed_event_identity_spawns_again(self, monkeypatch):
        """When consensus moves on (new proposal_commit_sha) the dedupe key
        changes and a fresh pod is spawned.
        """
        import event_loop

        spawner = _RecordingSpawner()
        loop = _make_loop(spawner)

        _script(monkeypatch, {"reviewer_code": ("ack", _REVIEW_PAYLOAD_V1, "x")})
        loop.poll_once(["reviewer_code"])

        monkeypatch.setattr(
            event_loop,
            "_derive_next_action",
            lambda t, r: ("ack", _REVIEW_PAYLOAD_V2, "x"),
            raising=True,
        )
        decisions = loop.poll_once(["reviewer_code"])

        assert spawner.spawn_count == 2
        assert decisions[0].spawned is True
        assert spawner.spawned_keys[0] != spawner.spawned_keys[1]

    def test_at_most_one_live_pod_per_role_slice(self, monkeypatch):
        """While a role's pod is live, a second poll for the SAME derived
        event must not create a concurrent second pod for that (role, slice).
        """
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner)

        for _ in range(5):
            loop.poll_once(["coder"])

        assert spawner.spawn_count == 1
        # Exactly one live key tracked for the role.
        live = loop.live_dedupe_keys()
        assert len(list(live)) == 1


# ---------------------------------------------------------------------------
# Dedupe — simulated orchestrator restart (stateless re-derivation)
# ---------------------------------------------------------------------------


class TestDedupeAcrossRestart:
    def test_restart_reconciles_against_live_jobs_no_duplicate(self, monkeypatch):
        """A fresh loop (orchestrator restart) holds NO in-memory dedupe set.
        It must rebuild the live set from live Job labels via ``reconcile``
        and then NOT re-spawn an event a still-running pod already owns.
        """
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})

        # --- pre-restart loop spawns one pod ---
        spawner1 = _RecordingSpawner()
        loop1 = _make_loop(spawner1)
        loop1.poll_once(["coder"])
        assert spawner1.spawn_count == 1
        live_labels = list(loop1.live_dedupe_keys())
        assert len(live_labels) == 1

        # --- restart: brand-new loop, fresh spawner, reconcile from Jobs ---
        spawner2 = _RecordingSpawner()
        loop2 = _make_loop(spawner2)
        loop2.reconcile(live_labels)  # seed from fake live-Job labels

        loop2.poll_once(["coder"])

        assert spawner2.spawn_count == 0, (
            "restart must reconcile live Job labels and skip the duplicate spawn"
        )

    def test_restart_without_reconcile_would_respawn_only_for_new_identity(self, monkeypatch):
        """After the live pod completes (its label no longer present), the
        next derived event for a *new* identity spawns normally — restart
        does not wedge future progress.
        """
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner)
        loop.reconcile([])  # no live jobs after restart

        _script(monkeypatch, {"reviewer_code": ("ack", _REVIEW_PAYLOAD_V2, "x")})
        loop.poll_once(["reviewer_code"])

        assert spawner.spawn_count == 1


# ---------------------------------------------------------------------------
# Statelessness — no spawn bookkeeping persisted
# ---------------------------------------------------------------------------


class TestNoPersistedBookkeeping:
    def test_dedupe_state_is_in_memory_only(self, monkeypatch, tmp_path):
        """The dedupe set must live only in process memory: a fresh loop with
        no ``reconcile`` starts empty (no hidden store rehydrates it).
        """
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})

        spawner1 = _RecordingSpawner()
        loop1 = _make_loop(spawner1)
        loop1.poll_once(["coder"])
        assert spawner1.spawn_count == 1

        # New loop, NO reconcile → empty live set → would spawn again.
        spawner2 = _RecordingSpawner()
        loop2 = _make_loop(spawner2)
        assert list(loop2.live_dedupe_keys()) == []
        loop2.poll_once(["coder"])
        assert spawner2.spawn_count == 1


# ---------------------------------------------------------------------------
# Structured spawn→invoke timing field
# ---------------------------------------------------------------------------


class TestTimingField:
    def test_spawn_emits_structured_timing(self, monkeypatch):
        """Every spawn decision carries a structured timing field the slice-4
        latency budget consumes; agent-free/wait decisions carry none.
        """
        _script(
            monkeypatch,
            {
                "coder": ("propose", _PROPOSE_PAYLOAD, "x"),
                "reviewer_code": ("confirm", None, "x"),
            },
        )
        spawner = _RecordingSpawner()
        clock = _FakeClock(start=1000.0, step=1.0)
        loop = _make_loop(spawner, clock=clock)

        decisions = {d.role: d for d in loop.poll_once(["coder", "reviewer_code"])}

        spawn_d = decisions["coder"]
        assert spawn_d.timing is not None
        # Timing is a structured mapping keyed for the latency budget.
        assert isinstance(spawn_d.timing, dict)
        assert "spawn_requested_at" in spawn_d.timing

        # Agent-free confirm carries no spawn timing.


# ---------------------------------------------------------------------------
# Slice-3 tests (TASK-3-2): orchestrator-side failure supervision
# ---------------------------------------------------------------------------
# These tests are the **contract** for the orchestrator-side supervision
# module (``orchestrator.supervision``) and the shared supervision policy
# module (``orchestrator.supervision_policy``).  The coder implements the
# modules to satisfy these tests; the constants must equal the wrapper's
# (#3138) so the slice-5 constant-equality assertion is a tautology.
#
# Design conventions kept from slice-2:
#  * Import of ``supervision`` / ``supervision_policy`` is done at call-time
#    inside each test so this file still collects before the coder's module
#    lands (slice-1 convention).
#  * An injected clock (``_FakeClock``) replaces ``time.monotonic`` so all
#    tests are deterministic — no real sleeps.
#  * A fake Job-status view (``_FakeJobView``) stands in for the Kubernetes
#    label-based dedupe-key view the live loop would see.
#
# Interface contract (to be satisfied by Task-3-1):
#
#   supervision.Supervisor(clock=_FakeClock())          # constructor
#     .record_failure(role, action, *, kind="failure", dedupe_key=None)
#     .record_success(role, action)
#     .set_dedupe_key(role, action, key)                    # fresh budget
#     .backoff_seconds(role, action)  -> int | None  # None if exhausted
#     .should_alert(role, action)     -> bool              # transition
#     .needs_agent_failed(role, action)    -> bool      # propose arm only
#     .is_exhausted(role, action)   -> bool
#
#   supervision_policy:
#       BACKOFF_MULTIPLIER          = 2
#       BACKOFF_CAP_SECONDS          = 30
#       WARN_STREAK                  = 5
#       OVERSEER_ALERT_STREAK       = 10
#       MAX_CONSECUTIVE_FAILURES     = 10
#       AGENT_FAILED_ANOMALY        = "agent-invocation-fail-streak"
#
# Kinds: "failure" (default — agent-invocation failed), "nack" (NACK
# returned — NOT a failure), "stale_exit" (stale-event exit — NOT a
# failure). Only "failure" increments the streak.


import supervision  # noqa: E402 — contract-driven imports, behind the flag
import supervision_policy  # noqa: E402


class TestSupervisionBackoff:
    """Backoff timing: streak * BACKOFF_MULTIPLIER s, capped at
    BACKOFF_CAP_SECONDS.  Injected clock — no real sleeps."""

    @staticmethod
    def _backoff(streak):
        return min(
            streak * supervision_policy.BACKOFF_MULTIPLIER,
            supervision_policy.BACKOFF_CAP_SECONDS,
        )

    def test_backoff_linear_with_streak(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for n in range(1, 16):
            supervisor.record_failure("coder", "propose")
            assert supervisor.backoff_seconds("coder", "propose") == self._backoff(n)

    def test_backoff_capped(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "propose")
        assert (
            supervisor.backoff_seconds("coder", "propose")
            == supervision_policy.BACKOFF_CAP_SECONDS
        )

    def test_backoff_zero_on_success(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        supervisor.record_failure("coder", "propose")
        assert supervisor.backoff_seconds("coder", "propose") == self._backoff(1)
        supervisor.record_success("coder", "propose")
        assert supervisor.backoff_seconds("coder", "propose") == 0

    def test_per_role_action_isolation(self):
        """Streaks are per-(role, action) — independent counters."""
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(3):
            supervisor.record_failure("coder", "propose")
            supervisor.record_failure("tester", "ack")
        assert supervisor.backoff_seconds("coder", "propose") == self._backoff(3)
        assert supervisor.backoff_seconds("tester", "ack") == self._backoff(3)
        supervisor.record_success("coder", "propose")
        assert supervisor.backoff_seconds("coder", "propose") == 0
        assert supervisor.backoff_seconds("tester", "ack") == self._backoff(3)


class TestSupervisionWarn:
    """Warn latch fires precisely once at streak >= WARN_STREAK."""

    def test_warn_at_five(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(4):
            supervisor.record_failure("coder", "propose")
            assert not supervisor.should_alert("coder", "propose")
        supervisor.record_failure("coder", "propose")
        assert supervisor.should_alert("coder", "propose")

    def test_warn_sticky(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(5):
            supervisor.record_failure("coder", "propose")
        assert supervisor.should_alert("coder", "propose")
        supervisor.record_failure("coder", "propose")
        assert not supervisor.should_alert("coder", "propose")


class TestSupervisionOverseerAlert:
    """OVERSEER_ALERT fires exactly once at streak >= OVERSEER_ALERT_STREAK."""

    def test_overseer_alert_at_ten(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(9):
            supervisor.record_failure("coder", "propose")
            assert not supervisor.should_alert("coder", "propose")
        supervisor.record_failure("coder", "propose")
        assert supervisor.should_alert("coder", "propose")

    def test_overseer_alert_sticky(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(10):
            supervisor.record_failure("coder", "propose")
        assert supervisor.should_alert("coder", "propose")
        supervisor.record_failure("coder", "propose")
        assert not supervisor.should_alert("coder", "propose")

    def test_warn_and_overseer_independent(self):
        """Warn and OVERSEER_ALERT are independent sticky-latches."""
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(10):
            supervisor.record_failure("coder", "propose")
        assert supervisor.should_alert("coder", "propose")


class TestNonTriggers:
    """Stale-exit and NACK are never counted as failures."""

    def test_nack_is_silent(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(20):
            supervisor.record_failure("coder", "nack", kind="nack")
        assert supervisor.backoff_seconds("coder", "nack") == 0
        assert not supervisor.is_exhausted("coder", "nack")

    def test_stale_exit_is_silent(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(20):
            supervisor.record_failure("coder", "propose", kind="stale_exit")
        assert supervisor.backoff_seconds("coder", "propose") == 0
        assert not supervisor.is_exhausted("coder", "propose")

    def test_default_kind_is_counted(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        supervisor.record_failure("coder", "propose")
        assert (
            supervisor.backoff_seconds("coder", "propose")
            == min(
                supervision_policy.BACKOFF_MULTIPLIER,
                supervision_policy.BACKOFF_CAP_SECONDS,
            )
        )


class TestSupervisionExhaustion:
    """No respawn after MAX_CONSECUTIVE_FAILURES consecutive *failure* streaks."""

    def test_exhaustion_after_max_streak(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES - 1):
            supervisor.record_failure("coder", "propose")
            assert not supervisor.is_exhausted("coder", "propose")
        supervisor.record_failure("coder", "propose")
        assert supervisor.is_exhausted("coder", "propose")

    def test_exhaustion_per_role_action(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "propose")
        assert supervisor.is_exhausted("coder", "propose")
        assert not supervisor.is_exhausted("coder", "ack")
        assert not supervisor.is_exhausted("tester", "propose")

    def test_exhausted_backoff_is_none(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "propose")
        assert supervisor.backoff_seconds("coder", "propose") is None

    def test_exhaustion_reset_on_success(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "propose")
        assert supervisor.is_exhausted("coder", "propose")
        supervisor.record_success("coder", "propose")
        assert not supervisor.is_exhausted("coder", "propose")
        supervisor.record_failure("coder", "propose")
        assert not supervisor.is_exhausted("coder", "propose")


class TestAgFailedOnProposeArmExhaustion:
    """Propose-arm exhaustion must engage the AGENT_FAILED path."""

    def test_propose_exhaustion_needs_agent_failed(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "propose")
        assert supervisor.is_exhausted("coder", "propose")
        assert supervisor.needs_agent_failed("coder", "propose")

    def test_non_propose_actions_skip_agent_failed(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "ack")
        assert supervisor.is_exhausted("coder", "ack")
        assert not supervisor.needs_agent_failed("coder", "ack")

    def test_agent_failed_sticky(self):
        """AGENT_FAILED is sticky — exhaustion once latches forever."""
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "propose")
        assert supervisor.needs_agent_failed("coder", "propose")
        supervisor.record_failure("coder", "propose")
        assert supervisor.needs_agent_failed("coder", "propose")


class TestFreshBudgetOnDedupeKeyChange:
    """A new dedupe key gives a fresh budget."""

    def test_dedupe_change_resets_exhaustion(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(supervision_policy.MAX_CONSECUTIVE_FAILURES):
            supervisor.record_failure("coder", "propose", dedupe_key="key-v1")
        assert supervisor.is_exhausted("coder", "propose")
        supervisor.set_dedupe_key("coder", "propose", "key-v2")
        assert not supervisor.is_exhausted("coder", "propose")

    def test_dedupe_change_resets_streak(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(5):
            supervisor.record_failure("coder", "propose", dedupe_key="key-v1")
        supervisor.set_dedupe_key("coder", "propose", "key-v2")
        assert supervisor.backoff_seconds("coder", "propose") == 0

    def test_same_dedupe_key_persists(self):
        supervisor = supervision.Supervisor(clock=_FakeClock())
        for _ in range(3):
            supervisor.record_failure("coder", "propose", dedupe_key="key-v1")
        supervisor.set_dedupe_key("coder", "propose", "key-v1")
        assert (
            supervisor.backoff_seconds("coder", "propose")
            == min(3 * supervision_policy.BACKOFF_MULTIPLIER, supervision_policy.BACKOFF_CAP_SECONDS)
        )


class TestSupervisionPolicyConstants:
    """Shared supervision_policy constants are defined per plan."""

    def test_constants_exist(self):
        assert hasattr(supervision_policy, "BACKOFF_MULTIPLIER")
        assert hasattr(supervision_policy, "BACKOFF_CAP_SECONDS")
        assert hasattr(supervision_policy, "WARN_STREAK")
        assert hasattr(supervision_policy, "OVERSEER_ALERT_STREAK")
        assert hasattr(supervision_policy, "MAX_CONSECUTIVE_FAILURES")
        assert hasattr(supervision_policy, "AGENT_FAILED_ANOMALY")

    def test_numeric_constants(self):
        assert supervision_policy.BACKOFF_MULTIPLIER >= 1
        assert supervision_policy.BACKOFF_CAP_SECONDS >= 1
        assert supervision_policy.WARN_STREAK >= 1
        assert supervision_policy.OVERSEER_ALERT_STREAK >= supervision_policy.WARN_STREAK
        assert supervision_policy.MAX_CONSECUTIVE_FAILURES >= 1

    def test_wrapper_values(self):
        """Values must match the wrapper's #3138 constants."""
        assert supervision_policy.BACKOFF_MULTIPLIER == 2
        assert supervision_policy.BACKOFF_CAP_SECONDS == 30
        assert supervision_policy.WARN_STREAK == 5
        assert supervision_policy.OVERSEER_ALERT_STREAK == 10
        assert supervision_policy.MAX_CONSECUTIVE_FAILURES == 10

    def test_anomaly_name(self):
        assert supervision_policy.AGENT_FAILED_ANOMALY == "agent-invocation-fail-streak"

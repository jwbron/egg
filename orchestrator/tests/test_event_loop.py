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
        assert decisions["reviewer_code"].timing is None


# ===========================================================================
# Slice-3 (#3064 TASK-3-2) — failure-supervision re-homing (HITL cq-2)
# ===========================================================================
#
# These tests are written **test-first** against the slice-3 contract the
# coder's parallel work (TASK-3-1) must satisfy. Per the plan
# (``.egg-state/drafts/3064-plan.md`` slice 3) the orchestrator re-homes the
# #3138 failure-supervision behavior that used to live in the in-pod wrapper:
#
#   * Job-status watching for one-shot pods, with a per-(role, arm) failure
#     streak mirroring #3138.
#   * The #3138 streak constants are extracted into a shared module
#     ``orchestrator/supervision_policy.py`` (NEW) so the event loop and the
#     wrapper template read ONE set of values — no fork, no drift:
#         linear backoff ``streak * 2s`` capped at 30s; warn at streak 5;
#         **sticky** OVERSEER_ALERT (anomaly ``agent-invocation-fail-streak``)
#         at streak 10; reset on success.
#   * Only an abnormal Job termination increments the streak. A NACK and a
#     stale-event exit-0 are legitimate BRC outcomes — explicit NON-triggers.
#   * A new event identity (dedupe-key change) gets a fresh budget.
#   * Respawn is bounded: once the streak hits the alert threshold the arm is
#     exhausted and is NOT respawned again.
#   * Producer **propose**-arm exhaustion engages the existing AGENT_FAILED
#     path (#2806); reviewer arms do not.
#
# Pinned public surface (the contract — the coder aligns names if they
# diverge, exactly as slice-1/slice-2's testers re-aligned to the coder's
# task):
#
#     supervision_policy.BACKOFF_FACTOR_SECONDS : int  == 2
#     supervision_policy.BACKOFF_CAP_SECONDS    : int  == 30
#     supervision_policy.WARN_STREAK            : int  == 5
#     supervision_policy.ALERT_STREAK           : int  == 10
#     supervision_policy.FAIL_STREAK_ANOMALY    : str  == "agent-invocation-fail-streak"
#     supervision_policy.backoff_seconds(streak) -> int
#         # min(streak * factor, cap); 0 for streak <= 0
#
#     event_loop.JOB_SUCCEEDED / JOB_FAILED / JOB_NACK / JOB_STALE_EXIT : str
#         # Job-termination outcome tokens fed to the supervisor. Only
#         # JOB_FAILED (abnormal termination) counts toward the streak.
#     event_loop.SupervisionDecision : dataclass(role, action, streak,
#         backoff_seconds, should_respawn, warn, alert, agent_failed,
#         exhausted, respawn_at)
#     event_loop.FailureSupervisor(*, clock=..., alert_handler=None,
#                                  agent_failed_handler=None)
#         .record_outcome(*, role, action, dedupe_key, outcome,
#                         last_rc=0, duration=0.0) -> SupervisionDecision
#
# Imports of ``event_loop`` / ``supervision_policy`` are done at call-time
# inside each test so this file still COLLECTS before the coder's slice-3
# module lands (slice-1/slice-2 convention).


class _ManualClock:
    """Deterministic clock the test advances explicitly (no real sleeps).

    Unlike ``_FakeClock`` (which auto-advances on every read) this returns a
    fixed value until the test calls :meth:`advance` / :meth:`set`, so the
    supervisor's ``respawn_at`` is a pure function of the streak under test.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = float(start)

    def __call__(self) -> float:
        return self._now

    def advance(self, dt: float) -> None:
        self._now += dt

    def set(self, now: float) -> None:
        self._now = float(now)


class _AlertRecorder:
    """Records each OVERSEER_ALERT the supervisor raises (sticky at streak 10)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

    @property
    def count(self) -> int:
        return len(self.calls)


class _AgentFailedRecorder:
    """Records AGENT_FAILED engagements (producer propose-arm exhaustion)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

    @property
    def count(self) -> int:
        return len(self.calls)


def _make_supervisor(*, clock=None, alert_handler=None, agent_failed_handler=None):
    """Construct the FailureSupervisor under test with injected collaborators."""
    import event_loop

    return event_loop.FailureSupervisor(
        clock=clock or _ManualClock(),
        alert_handler=alert_handler,
        agent_failed_handler=agent_failed_handler,
    )


def _fail(sup, *, role="coder", action="propose", dedupe_key="k1", **over):
    """Feed one abnormal-termination outcome and return the decision."""
    import event_loop

    return sup.record_outcome(
        role=role, action=action, dedupe_key=dedupe_key, outcome=event_loop.JOB_FAILED, **over
    )


# ---------------------------------------------------------------------------
# supervision_policy — pure backoff/threshold contract
# ---------------------------------------------------------------------------


class TestSupervisionPolicyConstants:
    def test_constant_values(self):
        import supervision_policy as sp

        assert sp.BACKOFF_FACTOR_SECONDS == 2
        assert sp.BACKOFF_CAP_SECONDS == 30
        assert sp.WARN_STREAK == 5
        assert sp.ALERT_STREAK == 10
        assert sp.FAIL_STREAK_ANOMALY == "agent-invocation-fail-streak"
        # Warn strictly precedes the alert/exhaustion threshold.
        assert sp.WARN_STREAK < sp.ALERT_STREAK

    @pytest.mark.parametrize(
        "streak,expected",
        [
            (0, 0),
            (1, 2),
            (2, 4),
            (3, 6),
            (4, 8),
            (5, 10),
            (9, 18),
            (10, 20),
            (14, 28),
            (15, 30),  # cap reached
            (16, 30),  # capped
            (50, 30),  # capped
        ],
    )
    def test_backoff_sequence_and_cap(self, streak, expected):
        """``streak * 2s`` capped at 30s; non-positive streak ⇒ 0 (no sleep)."""
        import supervision_policy as sp

        assert sp.backoff_seconds(streak) == expected

    def test_backoff_never_exceeds_cap(self):
        import supervision_policy as sp

        for streak in range(0, 60):
            assert sp.backoff_seconds(streak) <= sp.BACKOFF_CAP_SECONDS


# ---------------------------------------------------------------------------
# Backoff timing — injected clock, no real sleeps
# ---------------------------------------------------------------------------


class TestSupervisionBackoffTiming:
    def test_respawn_at_follows_backoff_sequence(self):
        import supervision_policy as sp

        clock = _ManualClock(start=1000.0)
        sup = _make_supervisor(clock=clock)

        # Streaks 1..9 keep respawning; respawn_at = now + streak*2s (capped).
        for streak in range(1, sp.ALERT_STREAK):
            clock.set(1000.0)  # pin "now" so the assertion is exact
            d = _fail(sup)
            assert d.streak == streak
            assert d.backoff_seconds == sp.backoff_seconds(streak)
            assert d.should_respawn is True
            assert d.respawn_at == 1000.0 + sp.backoff_seconds(streak)

    def test_no_real_sleep_is_taken(self):
        """The supervisor must compute backoff, never block: a record_outcome
        call returns immediately regardless of the streak's nominal backoff.
        """
        clock = _ManualClock(start=0.0)
        sup = _make_supervisor(clock=clock)
        for _ in range(12):
            _fail(sup)
        # The injected clock only moves when the *test* advances it.
        assert clock() == 0.0


# ---------------------------------------------------------------------------
# Warn-at-5 / silent below threshold
# ---------------------------------------------------------------------------


class TestSupervisionWarn:
    def test_silent_below_warn_threshold(self):
        import supervision_policy as sp

        sup = _make_supervisor()
        for streak in range(1, sp.WARN_STREAK):  # 1..4
            d = _fail(sup)
            assert d.warn is False, f"streak {streak} must be silent"
            assert d.alert is False

    def test_warn_fires_once_at_threshold_then_sticky(self):
        import supervision_policy as sp

        sup = _make_supervisor()
        decisions = [_fail(sup) for _ in range(sp.ALERT_STREAK - 1)]  # streaks 1..9
        warned = [i + 1 for i, d in enumerate(decisions) if d.warn]
        # Warn fires exactly once, on the streak that first reaches WARN_STREAK.
        assert warned == [sp.WARN_STREAK]
        # No alert before the alert threshold.
        assert all(d.alert is False for d in decisions)


# ---------------------------------------------------------------------------
# Sticky OVERSEER_ALERT at streak 10 (exactly once)
# ---------------------------------------------------------------------------


class TestSupervisionAlert:
    def test_alert_fires_exactly_once_at_alert_threshold(self):
        import supervision_policy as sp

        alerts = _AlertRecorder()
        sup = _make_supervisor(alert_handler=alerts)

        decisions = [_fail(sup) for _ in range(sp.ALERT_STREAK + 3)]  # well past 10
        alerted = [i + 1 for i, d in enumerate(decisions) if d.alert]
        assert alerted == [sp.ALERT_STREAK], "alert must fire once, at streak 10"

        # The handler was invoked exactly once with the #3138 anomaly name.
        assert alerts.count == 1
        payload = alerts.calls[0]
        assert payload.get("anomaly") == sp.FAIL_STREAK_ANOMALY
        assert payload.get("streak") == sp.ALERT_STREAK
        assert payload.get("role") == "coder"

    def test_alert_is_sticky_no_refire_past_threshold(self):
        import supervision_policy as sp

        alerts = _AlertRecorder()
        sup = _make_supervisor(alert_handler=alerts)
        for _ in range(sp.ALERT_STREAK + 5):
            _fail(sup)
        assert alerts.count == 1, "sticky latch must not re-fire above the threshold"


# ---------------------------------------------------------------------------
# Bounded respawn — exhaustion stops further respawns
# ---------------------------------------------------------------------------


class TestSupervisionBoundedRespawn:
    def test_respawn_stops_at_exhaustion(self):
        import supervision_policy as sp

        sup = _make_supervisor()
        decisions = [_fail(sup) for _ in range(sp.ALERT_STREAK + 4)]
        for i, d in enumerate(decisions):
            streak = i + 1
            if streak < sp.ALERT_STREAK:
                assert d.should_respawn is True
                assert d.exhausted is False
                assert d.respawn_at is not None
            else:
                assert d.should_respawn is False, f"streak {streak} is exhausted"
                assert d.exhausted is True
                assert d.respawn_at is None


# ---------------------------------------------------------------------------
# Streak reset on success
# ---------------------------------------------------------------------------


class TestSupervisionResetOnSuccess:
    def test_success_resets_streak_and_latches(self):
        import event_loop
        import supervision_policy as sp

        alerts = _AlertRecorder()
        sup = _make_supervisor(alert_handler=alerts)

        # Build a streak past the warn threshold, then succeed.
        for _ in range(sp.WARN_STREAK + 1):  # streaks 1..6
            _fail(sup)
        ok = sup.record_outcome(
            role="coder", action="propose", dedupe_key="k1", outcome=event_loop.JOB_SUCCEEDED
        )
        assert ok.streak == 0
        assert ok.should_respawn is False
        assert ok.warn is False and ok.alert is False

        # A subsequent failure starts from a fresh budget AND the warn latch
        # was cleared (warn can fire again once the threshold is re-reached).
        d1 = _fail(sup)
        assert d1.streak == 1
        assert d1.warn is False
        rebuild = [d1] + [_fail(sup) for _ in range(sp.WARN_STREAK - 1)]  # up to streak 5
        assert rebuild[-1].streak == sp.WARN_STREAK
        assert rebuild[-1].warn is True


# ---------------------------------------------------------------------------
# Non-triggers — NACK and stale-event exit-0 never count
# ---------------------------------------------------------------------------


class TestSupervisionNonTriggers:
    @pytest.mark.parametrize("outcome_attr", ["JOB_NACK", "JOB_STALE_EXIT"])
    def test_non_trigger_does_not_increment_or_respawn(self, outcome_attr):
        import event_loop

        sup = _make_supervisor()
        outcome = getattr(event_loop, outcome_attr)
        d = sup.record_outcome(
            role="reviewer_code", action="nack", dedupe_key="k1", outcome=outcome
        )
        assert d.streak == 0
        assert d.should_respawn is False
        assert d.warn is False and d.alert is False
        assert d.agent_failed is False

    @pytest.mark.parametrize("outcome_attr", ["JOB_NACK", "JOB_STALE_EXIT"])
    def test_non_trigger_leaves_existing_streak_unchanged(self, outcome_attr):
        """Only abnormal termination counts: a NACK / stale-exit interleaved
        with failures must neither bump nor reset the accumulated streak.
        """
        import event_loop

        sup = _make_supervisor()
        for _ in range(3):
            _fail(sup, role="coder", action="propose", dedupe_key="k1")
        outcome = getattr(event_loop, outcome_attr)
        nt = sup.record_outcome(role="coder", action="propose", dedupe_key="k1", outcome=outcome)
        assert nt.streak == 3, "non-trigger must not reset the failure streak"
        # The next real failure continues from 4, proving the streak persisted.
        assert _fail(sup, role="coder", action="propose", dedupe_key="k1").streak == 4

    def test_nack_never_raises_alert(self):
        import event_loop
        import supervision_policy as sp

        alerts = _AlertRecorder()
        sup = _make_supervisor(alert_handler=alerts)
        for _ in range(sp.ALERT_STREAK + 2):
            sup.record_outcome(
                role="coder", action="propose", dedupe_key="k1", outcome=event_loop.JOB_NACK
            )
        assert alerts.count == 0, "a stream of NACKs must never trip the alert"


# ---------------------------------------------------------------------------
# Fresh budget on dedupe-key change
# ---------------------------------------------------------------------------


class TestSupervisionKeyChange:
    def test_new_dedupe_key_resets_streak_and_latches(self):
        import supervision_policy as sp

        alerts = _AlertRecorder()
        sup = _make_supervisor(alert_handler=alerts)

        # Build past the warn threshold on key k1.
        for _ in range(sp.WARN_STREAK + 1):  # 1..6
            _fail(sup, dedupe_key="k1")

        # Consensus moves on → new event identity (k2): fresh budget.
        d = _fail(sup, dedupe_key="k2")
        assert d.streak == 1, "a new dedupe key must start a fresh streak"
        assert d.warn is False, "the warn latch must reset with the new key"

        # The warn latch genuinely reset: warn fires again at the threshold.
        rebuild = [d] + [_fail(sup, dedupe_key="k2") for _ in range(sp.WARN_STREAK - 1)]
        assert rebuild[-1].streak == sp.WARN_STREAK
        assert rebuild[-1].warn is True
        # No alert ever fired (neither key reached the alert threshold).
        assert alerts.count == 0


# ---------------------------------------------------------------------------
# AGENT_FAILED engagement on producer propose-arm exhaustion
# ---------------------------------------------------------------------------


class TestSupervisionAgentFailed:
    def test_producer_propose_exhaustion_engages_agent_failed(self):
        import supervision_policy as sp

        failed = _AgentFailedRecorder()
        sup = _make_supervisor(agent_failed_handler=failed)

        decisions = [_fail(sup, role="coder", action="propose") for _ in range(sp.ALERT_STREAK + 2)]
        engaged = [i + 1 for i, d in enumerate(decisions) if d.agent_failed]
        assert engaged == [sp.ALERT_STREAK], "AGENT_FAILED engages once, at exhaustion"
        assert failed.count == 1
        assert failed.calls[0].get("role") == "coder"
        assert failed.calls[0].get("action") == "propose"

    def test_reviewer_arm_exhaustion_does_not_engage_agent_failed(self):
        """Reviewer (ack/nack) arms still alert at exhaustion but must NOT
        engage the producer-only AGENT_FAILED path.
        """
        import supervision_policy as sp

        alerts = _AlertRecorder()
        failed = _AgentFailedRecorder()
        sup = _make_supervisor(alert_handler=alerts, agent_failed_handler=failed)

        decisions = [
            _fail(sup, role="reviewer_code", action="ack") for _ in range(sp.ALERT_STREAK + 2)
        ]
        assert all(d.agent_failed is False for d in decisions)
        assert failed.count == 0
        # The sticky alert still fires once for the reviewer arm.
        assert alerts.count == 1


# ---------------------------------------------------------------------------
# Constants single-source — loop and wrapper read identical values
# ---------------------------------------------------------------------------


class TestSupervisionConstantsSingleSource:
    """The wrapper template and the loop must read ONE set of #3138
    constants via ``supervision_policy`` — no fork, no drift.
    """

    def test_wrapper_renders_supervision_policy_constants(self):
        import supervision_policy as sp
        from consensus_wrapper import build_event_pump_wrapped_command

        cmd = build_event_pump_wrapped_command("")
        assert cmd[:2] == ["bash", "-c"]
        script = cmd[2]

        # The agent-invocation-fail-streak supervision arm in the wrapper
        # must reference the SAME thresholds/anomaly the loop's policy uses.
        assert sp.FAIL_STREAK_ANOMALY in script
        assert f"-ge {sp.WARN_STREAK}" in script
        assert f"-ge {sp.ALERT_STREAK}" in script
        assert f"-gt {sp.BACKOFF_CAP_SECONDS}" in script
        assert f"* {sp.BACKOFF_FACTOR_SECONDS}" in script

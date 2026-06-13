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

import statistics
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


# ---------------------------------------------------------------------------
# p50 < 60s spawn→invoke latency budget (#3064 slice-4)
# ---------------------------------------------------------------------------


class _DeltaClock:
    """Deterministic monotonic clock yielding a chosen delta per spawn.

    ``poll_once`` reads the clock exactly twice per spawn — ``requested_at``
    immediately before ``spawner.spawn_event(...)`` and ``dispatched_at``
    immediately after — so the structured ``spawn_dispatch_seconds`` for spawn
    *i* equals ``deltas_s[i]`` exactly, with no real sleeps. (Fresh dedupe keys
    take the no-abort ``ready_to_respawn`` early-return, so the supervisor adds
    no extra clock reads on this path.)
    """

    def __init__(self, deltas_s: list[float]) -> None:
        self._ticks: list[float] = []
        t = 0.0
        for d in deltas_s:
            self._ticks.append(t)
            self._ticks.append(t + d)
            t += d + 1.0  # +1s gap so successive spawns never share a tick
        self._i = 0

    def __call__(self) -> float:
        v = self._ticks[self._i]
        self._i += 1
        return v


class TestLatencyBudgetFromTimingField:
    """The p50<60s spawn→invoke budget is computed from the slice-2 timing field.

    The authoritative budget reads ``EventDecision.timing['spawn_dispatch_seconds']``
    — measured in ``poll_once`` across the WHOLE ``spawner.spawn_event(...)`` call,
    which in orchestrator mode fans out through the slice-4 worktree re-attach
    validation, ``_clean_reused_worktree`` (fetch + hard-sync), and
    ``_get_or_create_session`` before the k8s Job is created. A regression that
    slows any of that new code therefore moves this measured interval, so the
    budget actually guards the slice-4 latency (unlike a sub-segment timer that
    starts only once ``spawn_agent_job`` begins). Driven under an injected clock
    — no real sleeps.
    """

    _ROLES = ["coder", "reviewer_code", "reviewer_security", "documenter", "tester"]

    def _p50_dispatch_seconds(self, monkeypatch, deltas_s):
        """Drive one ``poll_once`` over N roles; return the p50 of the
        structured ``spawn_dispatch_seconds`` field each spawn decision carries.
        """
        _script(
            monkeypatch,
            {r: ("propose", {"producer": r}, "x") for r in self._ROLES},
        )
        spawner = _RecordingSpawner()
        loop = _make_loop(spawner, clock=_DeltaClock(deltas_s), slice_id="slice-4")

        decisions = loop.poll_once(self._ROLES)

        samples = [
            d.timing["spawn_dispatch_seconds"]
            for d in decisions
            if d.spawned and d.timing is not None
        ]
        # The measured field matches the simulated clock exactly (full span).
        assert samples == [round(x, 6) for x in deltas_s]
        return statistics.median(samples)

    def test_p50_spawn_to_invoke_below_60s(self, monkeypatch):
        """Realistic per-spawn dispatch latencies keep p50 under the 60s budget."""
        deltas = [5.0, 9.0, 8.0, 12.0, 7.0]  # seconds
        p50 = self._p50_dispatch_seconds(monkeypatch, deltas)
        assert p50 < 60.0, f"p50 {p50}s must be under the 60s budget"

    def test_budget_trips_when_p50_at_or_above_60s(self, monkeypatch):
        """Negative control: a simulated p50 ≥ 60s makes the budget go red.

        Proves the assertion is load-bearing — the budget reads the real
        ``spawn_dispatch_seconds`` and fails when the full spawn→dispatch span
        crosses 60s, rather than being skipped on a missing field.
        """
        deltas = [61.0, 65.0, 62.0]  # seconds, all over budget
        p50 = self._p50_dispatch_seconds(monkeypatch, deltas)
        assert p50 >= 60.0
        assert not (p50 < 60.0), "budget predicate must go red at p50 ≥ 60s"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

# Slice-3 tests (TASK-3-2): orchestrator-side failure supervision (#3064)
# ---------------------------------------------------------------------------
# These are the **implementation-aligned** tests for the coder's
# ``event_loop.JobSupervisor`` (Task-3-1 / TASK 4e51af4).  The coder
# implemented ``JobSupervisor`` as a per-dedupe-key streak tracker that
# exhausts once the consecutive failure streak reaches
# ``supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT`` (10).  Per
# slice-2 convention, ``import event_loop`` and ``import
# supervision_policy`` is done inside each test so this file still
# collects before the coder's module lands.
# All tests use the ``_FakeClock`` that is defined earlier in this file.


class TestJobSupervisor:
    """All tests exercise the actual ``event_loop.JobSupervisor``."""

    def test_constructs_with_clock(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        assert isinstance(supervisor, event_loop.JobSupervisor)

    def test_initial_state_no_backoff(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        assert supervisor.backoff_seconds("key-1") == 0
        assert not supervisor.is_exhausted("key-1")

    # ---------- Backoff timing ----------

    def test_backoff_linear_with_streak(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for n in range(1, 6):
            supervisor.record_abort("key-a", "propose", "coder")
            expected = n * supervision_policy.SUPERVISION_BACKOFF_FACTOR
            assert supervisor.backoff_seconds("key-a") == expected

    def test_backoff_capped(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        # 15 * 2 = 30 = cap
        for _ in range(15):
            supervisor.record_abort("key-1", "propose", "coder")
        assert (
            supervisor.backoff_seconds("key-1")
            == supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS
        )

    def test_backoff_reset_on_success(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        supervisor.record_abort("key-1", "propose", "coder")
        supervisor.record_success("key-1")
        assert supervisor.backoff_seconds("key-1") == 0

    def test_backoff_per_key_independent(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        supervisor.record_abort("key-a", "propose", "coder")
        supervisor.record_abort("key-b", "ack", "tester")
        assert supervisor.backoff_seconds("key-a") == 2
        assert supervisor.backoff_seconds("key-b") == 2

    # ---------- Warning threshold (5) is NOT exhaustion ----------

    def test_not_exhausted_at_warn_threshold(self):
        """Streak reaches the WARN threshold but exhaustion happens at ALERT (10), not WARN."""
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(4):
            supervisor.record_abort("key-w", "propose", "coder")
        assert not supervisor.is_exhausted("key-w")
        supervisor.record_abort("key-w", "propose", "coder")
        # At streak 5 we are NOT exhausted (the WARN threshold is advisory)
        assert not supervisor.is_exhausted("key-w")

    def test_warn_not_exhausted_at_five_helper(self):
        """Five consecutive failures hit the warn but exhaustion is not reached until 10."""
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(5):
            supervisor.record_abort("key-w", "propose", "coder")
        assert not supervisor.is_exhausted("key-w")

    # ---------- Exhaustion at 10 ----------

    def test_exhaustion_at_ten(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(9):
            supervisor.record_abort("key-o", "propose", "coder")
            assert not supervisor.is_exhausted("key-o")
        supervisor.record_abort("key-o", "propose", "coder")
        assert supervisor.is_exhausted("key-o")

    def test_exhaustion_sticky(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(10):
            supervisor.record_abort("key-o", "propose", "coder")
        assert supervisor.is_exhausted("key-o")
        supervisor.record_abort("key-o", "propose", "coder")
        assert supervisor.is_exhausted("key-o")

    # ---------- on_exhausted teardown hook (#3064 slice-4) ----------

    def test_on_exhausted_fires_once_at_the_exhaustion_transition(self):
        """The streak-exhaustion teardown hook fires exactly once, with the
        arm's (role, action, dedupe_key), as the key crosses into exhausted.

        This is the production trigger the orchestrator wires to gateway-session
        teardown — driven through the real ``record_abort`` exhaustion path, not
        a direct call to the teardown method.
        """
        import event_loop
        import supervision_policy

        fired: list[dict] = []
        supervisor = event_loop.JobSupervisor(
            clock=_FakeClock(),
            on_exhausted=lambda **kw: fired.append(kw),
        )
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT - 1):
            supervisor.record_abort("key-x", "ack", "reviewer_code")
            assert fired == [], "must not fire before the exhaustion threshold"

        supervisor.record_abort("key-x", "ack", "reviewer_code")
        assert fired == [{"role": "reviewer_code", "action": "ack", "dedupe_key": "key-x"}]

        # Sticky: further aborts on the already-exhausted key do not re-fire.
        supervisor.record_abort("key-x", "ack", "reviewer_code")
        assert len(fired) == 1

    def test_on_exhausted_failure_never_wedges_supervision(self):
        """A raising teardown hook is swallowed — exhaustion state still advances."""
        import event_loop
        import supervision_policy

        def _boom(**_kw):
            raise RuntimeError("gateway down")

        supervisor = event_loop.JobSupervisor(clock=_FakeClock(), on_exhausted=_boom)
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            supervisor.record_abort("key-x", "propose", "coder")

        # The hook raised, but the key is still marked exhausted (best-effort).
        assert supervisor.is_exhausted("key-x")

    # ---------- Non-triggers: NACK / legitimate don't increment streak ----------

    def test_nack_is_silent(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(20):
            supervisor.record_legitimate_outcome("key-n", "nack")
        assert supervisor.backoff_seconds("key-n") == 0
        assert not supervisor.is_exhausted("key-n")

    def test_legitimate_outcome_no_effect(self):
        import event_loop

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        supervisor.record_legitimate_outcome("key-n", "confirm")
        assert supervisor.backoff_seconds("key-n") == 0
        assert not supervisor.is_exhausted("key-n")

    # ---------- Exhaustion reset on success ----------

    def test_exhaustion_reset_on_success(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            supervisor.record_abort("key-r", "propose", "coder")
        assert supervisor.is_exhausted("key-r")
        supervisor.record_success("key-r")
        assert not supervisor.is_exhausted("key-r")
        assert supervisor.backoff_seconds("key-r") == 0

    def test_reconciliation_clears_state(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            supervisor.record_abort("key-1", "propose", "coder")
        assert supervisor.is_exhausted("key-1")
        supervisor.reconcile(["key-1"])
        assert not supervisor.is_exhausted("key-1")
        assert supervisor.backoff_seconds("key-1") == 0

    # ---------- Fresh budget on dedupe key change ----------

    def test_dedupe_change_resets_exhaustion(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            supervisor.record_abort("key-v1", "propose", "coder")
        assert supervisor.is_exhausted("key-v1")
        assert not supervisor.is_exhausted("key-v2")

    def test_same_dedupe_key_persists(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(3):
            supervisor.record_abort("key-x", "propose", "coder")
        expected = min(
            3 * supervision_policy.SUPERVISION_BACKOFF_FACTOR,
            supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS,
        )
        assert supervisor.backoff_seconds("key-x") == expected

    # ---------- Per-key separation ----------

    def test_key_isolation_exhaustion(self):
        import event_loop
        import supervision_policy

        supervisor = event_loop.JobSupervisor(clock=_FakeClock())
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            supervisor.record_abort("key-a", "propose", "coder")
            supervisor.record_abort("key-b", "ack", "tester")
        assert supervisor.is_exhausted("key-a")
        assert supervisor.is_exhausted("key-b")
        supervisor.record_success("key-b")
        assert not supervisor.is_exhausted("key-b")


class TestSupervisionPolicyConstants:
    """Shared supervision_policy constants are defined per plan (#3138)."""

    def test_constants_exist(self):
        import supervision_policy

        assert hasattr(supervision_policy, "SUPERVISION_BACKOFF_FACTOR")
        assert hasattr(supervision_policy, "SUPERVISION_BACKOFF_CAP_SECONDS")
        assert hasattr(supervision_policy, "SUPERVISION_FAILURE_STREAK_WARN")
        assert hasattr(supervision_policy, "SUPERVISION_FAILURE_STREAK_ALERT")

    def test_wrapper_values(self):
        """Values must match the wrapper's #3138 constants."""
        import supervision_policy

        assert supervision_policy.SUPERVISION_BACKOFF_FACTOR == 2
        assert supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS == 30
        assert supervision_policy.SUPERVISION_FAILURE_STREAK_WARN == 5
        assert supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT == 10

    def test_loop_reexports_equal_supervision_policy(self):
        """The event loop re-exports the SAME values as supervision_policy.

        Slice-3 AC4 ("loop and wrapper template constants asserted equal via
        supervision_policy"). A fork between the loop's re-exports and the
        single source breaks this.
        """
        import event_loop
        import supervision_policy

        assert (
            event_loop.SUPERVISION_BACKOFF_FACTOR == supervision_policy.SUPERVISION_BACKOFF_FACTOR
        )
        assert (
            event_loop.SUPERVISION_BACKOFF_CAP_SECONDS
            == supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS
        )
        assert (
            event_loop.SUPERVISION_FAILURE_STREAK_WARN
            == supervision_policy.SUPERVISION_FAILURE_STREAK_WARN
        )
        assert (
            event_loop.SUPERVISION_FAILURE_STREAK_ALERT
            == supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT
        )

    def test_wrapper_template_renders_supervision_policy_values(self):
        """The rendered pod wrapper embeds the supervision_policy constants.

        Slice-3 AC4: the wrapper template and the loop must read identical
        constants. ``consensus_wrapper`` re-exports them from
        ``supervision_policy`` and interpolates them into the bash template,
        so the rendered guards/backoff must reflect the single-source values.
        A fork (hardcoded literal that diverges from supervision_policy) makes
        one of these substrings disappear from the render.
        """
        import supervision_policy
        from consensus_wrapper import (
            SUPERVISION_BACKOFF_CAP_SECONDS,
            SUPERVISION_BACKOFF_FACTOR,
            SUPERVISION_FAILURE_STREAK_ALERT,
            SUPERVISION_FAILURE_STREAK_WARN,
            build_consensus_wrapped_command,
        )

        # The wrapper's re-exports are the same single source as the loop's.
        assert SUPERVISION_BACKOFF_FACTOR == supervision_policy.SUPERVISION_BACKOFF_FACTOR
        assert SUPERVISION_BACKOFF_CAP_SECONDS == supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS
        assert SUPERVISION_FAILURE_STREAK_WARN == supervision_policy.SUPERVISION_FAILURE_STREAK_WARN
        assert (
            SUPERVISION_FAILURE_STREAK_ALERT == supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT
        )

        script = build_consensus_wrapped_command("x")[2]
        warn = supervision_policy.SUPERVISION_FAILURE_STREAK_WARN
        alert = supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT
        factor = supervision_policy.SUPERVISION_BACKOFF_FACTOR
        cap = supervision_policy.SUPERVISION_BACKOFF_CAP_SECONDS
        # Guards interpolate the constants verbatim into the bash template.
        assert f'-ge {warn} ] && [ "$AGENT_FAIL_ALERTED_5"' in script
        assert f'-ge {alert} ] && [ "$AGENT_FAIL_ALERTED_10"' in script
        assert f"AGENT_FAIL_STREAK * {factor}" in script
        assert f'"$agent_backoff_secs" -gt {cap}' in script


# ---------------------------------------------------------------------------
# Slice-3 supervision DRIVEN THROUGH THE LOOP (TASK-3-2).
#
# The tests above exercise ``JobSupervisor`` in isolation; these drive the
# whole production path — ``OrchestratorEventLoop.poll_once`` observing a
# (fake) Job-status view and feeding the supervisor — so the wiring itself
# (not just the primitive) is under test. A regression that stops the loop
# from observing Job outcomes breaks these.
# ---------------------------------------------------------------------------


class _ManualClock:
    """Settable monotonic clock — does NOT auto-advance (unlike _FakeClock).

    Backoff timing through the loop needs the test to control elapsed time
    explicitly, so the clock returns the same value until ``advance`` is
    called.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeJobStatusView:
    """Scriptable Job-status observer for loop-driven supervision tests.

    ``outcome_for(key)`` returns the outcome set for that key (default
    ``running`` — still in flight). The loop calls this once per live key
    per poll.
    """

    def __init__(self) -> None:
        import event_loop

        self._default = event_loop.JOB_OUTCOME_RUNNING
        self._outcomes: dict[str, str] = {}
        self.queries: list[str] = []
        self.reaped: list[str] = []

    def set(self, key: str, outcome: str) -> None:
        self._outcomes[key] = outcome

    def outcome_for(self, key: str) -> str:
        self.queries.append(key)
        return self._outcomes.get(key, self._default)

    def reap_terminated(self, key: str) -> int:
        # Mirror the spawner view: the loop's abnormal branch calls this to
        # delete the terminated Job so it is observed exactly once.
        self.reaped.append(key)
        return 1


def _make_supervised_loop(spawner, *, clock, supervisor, status_view, slice_id="slice-3"):
    """Construct a loop wired with a supervisor + Job-status view."""
    import event_loop

    return event_loop.OrchestratorEventLoop(
        tracker=object(),
        spawner=spawner,
        pipeline_id="issue-3064",
        slice_id=slice_id,
        phase="implement",
        clock=clock,
        agent_free_handler=_AgentFreeRecorder(),
        roles=["coder"],
        job_supervisor=supervisor,
        job_status_view=status_view,
    )


def _propose_key(loop):
    """The dedupe key the scripted ``coder`` propose resolves to."""
    import event_loop

    identity = event_loop.event_identity("propose", _PROPOSE_PAYLOAD)
    return event_loop.compute_dedupe_key(
        loop.pipeline_id, loop.slice_id, loop.phase, "coder", "propose", identity
    )


class TestSupervisionDrivenThroughLoop:
    """``poll_once`` observes Job status and drives the supervisor end-to-end."""

    def _abort_cycle(self, loop, view, clock, key):
        """Drive exactly one abnormal-termination abort through the loop.

        Advances well past the backoff cap so a non-exhausted key respawns
        (poll #1 → spawn live), then a second poll observes the live Job as
        abnormal (poll #2 → record_abort). An exhausted key never respawns,
        so no abort is recorded — which is exactly the "no respawn after
        exhaustion" behavior.
        """
        view.set(key, __import__("event_loop").JOB_OUTCOME_ABNORMAL)
        clock.advance(loop.supervisor.backoff_cap + 5)
        loop.poll_once(["coder"])  # respawn (unless exhausted)
        loop.poll_once(["coder"])  # observe abnormal → record_abort

    def test_backoff_applied_between_respawns(self, monkeypatch):
        """After an abort the loop refuses to respawn until backoff elapses."""
        import event_loop

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key = _propose_key(loop)

        # Initial spawn (fresh key, no abort yet).
        loop.poll_once(["coder"])
        assert len(spawner.calls) == 1

        # Job died abnormally — observed on the next poll → streak 1, backoff 2.
        view.set(key, event_loop.JOB_OUTCOME_ABNORMAL)
        loop.poll_once(["coder"])
        assert supervisor.backoff_seconds(key) == 2
        # Same poll re-derived but backed off — no respawn yet.
        assert len(spawner.calls) == 1

        # Still inside the backoff window → no respawn.
        view.set(key, event_loop.JOB_OUTCOME_RUNNING)
        clock.advance(1)  # < 2s backoff
        loop.poll_once(["coder"])
        assert len(spawner.calls) == 1

        # Backoff window elapsed → respawn.
        clock.advance(2)  # now >= 2s since the abort
        loop.poll_once(["coder"])
        assert len(spawner.calls) == 2

    def test_success_resets_streak_through_loop(self, monkeypatch):
        """A clean rc=0 completion resets the streak via the loop."""
        import event_loop

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key = _propose_key(loop)

        self._abort_cycle(loop, view, clock, key)
        self._abort_cycle(loop, view, clock, key)
        assert supervisor.backoff_seconds(key) == 4  # streak 2

        # Next observation is a success → streak resets.
        clock.advance(supervisor.backoff_cap + 5)
        loop.poll_once(["coder"])  # respawn
        view.set(key, event_loop.JOB_OUTCOME_SUCCESS)
        loop.poll_once(["coder"])  # observe success
        assert supervisor.backoff_seconds(key) == 0
        assert not supervisor.is_exhausted(key)

    def test_stale_exit_is_a_non_trigger_through_loop(self, monkeypatch):
        """A legitimate outcome (stale-event exit 0 / NACK) never increments."""
        import event_loop

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key = _propose_key(loop)

        # Drive several legitimate outcomes through the loop.
        for _ in range(5):
            clock.advance(supervisor.backoff_cap + 5)
            loop.poll_once(["coder"])  # spawn live
            view.set(key, event_loop.JOB_OUTCOME_LEGITIMATE)
            loop.poll_once(["coder"])  # observe legitimate → no increment
            view.set(key, event_loop.JOB_OUTCOME_RUNNING)

        assert supervisor.backoff_seconds(key) == 0
        assert not supervisor.is_exhausted(key)
        # Legitimate outcomes free the key, so it keeps getting respawned.
        assert len(spawner.calls) >= 5

    def test_abnormal_outcome_reaps_terminated_job(self, monkeypatch):
        """The abnormal branch reaps the terminated Job so it is observed once
        (#3181 re-review). Without the reap the FAILED Job lingers and the
        streak re-increments against one dead pod."""
        import event_loop

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key = _propose_key(loop)

        loop.poll_once(["coder"])  # initial spawn
        view.set(key, event_loop.JOB_OUTCOME_ABNORMAL)
        loop.poll_once(["coder"])  # observe abnormal → record_abort + reap

        assert view.reaped == [key]
        assert supervisor.backoff_seconds(key) == 2  # streak 1, counted once
        # Non-abnormal outcomes never reap.
        view.set(key, event_loop.JOB_OUTCOME_SUCCESS)
        clock.advance(supervisor.backoff_cap + 5)
        loop.poll_once(["coder"])  # respawn
        loop.poll_once(["coder"])  # observe success → no further reap
        assert view.reaped == [key]

    def test_warn_latch_fires_at_five_silent_below(self, monkeypatch):
        """No warn/alert below the WARN threshold; sticky warn exactly at it."""
        import event_loop
        import supervision_policy

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        alerts: list[dict] = []
        supervisor = event_loop.JobSupervisor(
            clock=clock, overseer_alert=lambda **kw: alerts.append(kw)
        )
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key = _propose_key(loop)

        # Four aborts — silent (no warn latch, no alert).
        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_WARN - 1):
            self._abort_cycle(loop, view, clock, key)
        assert not supervisor._alerted_warn.get(key, False)
        assert alerts == []

        # Fifth abort — sticky warn latch set, still no alert, not exhausted.
        self._abort_cycle(loop, view, clock, key)
        assert supervisor._alerted_warn.get(key) is True
        assert alerts == []
        assert not supervisor.is_exhausted(key)

    def test_alert_fires_exactly_once_and_sticky_through_loop(self, monkeypatch):
        """Sticky OVERSEER_ALERT exactly once at the ALERT threshold."""
        import event_loop
        import supervision_policy

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        alerts: list[dict] = []
        supervisor = event_loop.JobSupervisor(
            clock=clock, overseer_alert=lambda **kw: alerts.append(kw)
        )
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key = _propose_key(loop)

        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            self._abort_cycle(loop, view, clock, key)

        assert supervisor.is_exhausted(key)
        assert len(alerts) == 1
        assert alerts[0]["anomaly"] == "agent-invocation-fail-streak"

        # Further abort cycles never re-fire the alert (sticky) and never
        # respawn the exhausted key.
        spawn_count_at_exhaustion = len(spawner.calls)
        self._abort_cycle(loop, view, clock, key)
        assert len(alerts) == 1
        assert len(spawner.calls) == spawn_count_at_exhaustion

    def test_propose_arm_exhaustion_engages_agent_failed(self, monkeypatch):
        """Producer propose-arm exhaustion calls the AGENT_FAILED handler once."""
        import event_loop
        import supervision_policy

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        failures: list[dict] = []
        supervisor = event_loop.JobSupervisor(
            clock=clock, agent_failed=lambda **kw: failures.append(kw)
        )
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key = _propose_key(loop)

        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            self._abort_cycle(loop, view, clock, key)

        assert len(failures) == 1
        assert failures[0]["role"] == "coder"
        assert failures[0]["action"] == "propose"

    def test_review_arm_exhaustion_does_not_engage_agent_failed(self, monkeypatch):
        """A reviewer (ack) arm exhaustion alerts but is NOT a producer failure."""
        import event_loop
        import supervision_policy

        _script(monkeypatch, {"reviewer_code": ("ack", _REVIEW_PAYLOAD_V1, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        failures: list[dict] = []
        alerts: list[dict] = []
        supervisor = event_loop.JobSupervisor(
            clock=clock,
            agent_failed=lambda **kw: failures.append(kw),
            overseer_alert=lambda **kw: alerts.append(kw),
        )
        view = _FakeJobStatusView()
        loop = event_loop.OrchestratorEventLoop(
            tracker=object(),
            spawner=spawner,
            pipeline_id="issue-3064",
            slice_id="slice-3",
            phase="implement",
            clock=clock,
            roles=["reviewer_code"],
            job_supervisor=supervisor,
            job_status_view=view,
        )
        identity = event_loop.event_identity("ack", _REVIEW_PAYLOAD_V1)
        key = event_loop.compute_dedupe_key(
            "issue-3064", "slice-3", "implement", "reviewer_code", "ack", identity
        )
        view.set(key, event_loop.JOB_OUTCOME_ABNORMAL)

        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            clock.advance(supervisor.backoff_cap + 5)
            loop.poll_once(["reviewer_code"])
            loop.poll_once(["reviewer_code"])

        # Alert still fires (any arm), but AGENT_FAILED is producer-only.
        assert len(alerts) == 1
        assert failures == []

    def test_fresh_budget_on_dedupe_key_change_through_loop(self, monkeypatch):
        """A new dedupe key spawns despite an exhausted predecessor."""
        import event_loop
        import supervision_policy

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        view = _FakeJobStatusView()
        loop = _make_supervised_loop(spawner, clock=clock, supervisor=supervisor, status_view=view)
        key1 = _propose_key(loop)

        for _ in range(supervision_policy.SUPERVISION_FAILURE_STREAK_ALERT):
            self._abort_cycle(loop, view, clock, key1)
        assert supervisor.is_exhausted(key1)

        # Consensus state moves on: the propose now targets a new version, so
        # the derived event yields a different dedupe key with a fresh budget.
        new_payload = {"current_version": "2", "producer": "coder"}
        _script(monkeypatch, {"coder": ("propose", new_payload, "x")})
        identity2 = event_loop.event_identity("propose", new_payload)
        key2 = event_loop.compute_dedupe_key(
            loop.pipeline_id, loop.slice_id, loop.phase, "coder", "propose", identity2
        )
        assert key2 != key1

        spawns_before = len(spawner.calls)
        clock.advance(supervisor.backoff_cap + 5)
        loop.poll_once(["coder"])
        assert not supervisor.is_exhausted(key2)
        assert len(spawner.calls) == spawns_before + 1

    def test_no_observation_without_status_view(self, monkeypatch):
        """With no status view wired the loop never drives the supervisor."""
        import event_loop

        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        spawner = _RecordingSpawner()
        clock = _ManualClock()
        supervisor = event_loop.JobSupervisor(clock=clock)
        loop = event_loop.OrchestratorEventLoop(
            tracker=object(),
            spawner=spawner,
            pipeline_id="issue-3064",
            slice_id="slice-3",
            phase="implement",
            clock=clock,
            roles=["coder"],
            job_supervisor=supervisor,
            # job_status_view omitted → dormant (slice-2 behavior).
        )
        key = _propose_key(loop)
        loop.poll_once(["coder"])
        loop.poll_once(["coder"])
        # No observation ⇒ the (deduped) key stays live, streak untouched.
        assert supervisor.backoff_seconds(key) == 0
        assert len(spawner.calls) == 1


# ---------------------------------------------------------------------------
# Convergence-stall detection (#3064 slice-5, TASK-5-1 / TASK-5-2)
#
# Drives ``_check_convergence_stall`` from tracker-timestamp fixtures and a
# spy notifier so the re-homed idle-budget judgment is asserted behaviorally
# (not as a literal-vs-itself equality). Covers: no alert before budget, a
# single ``stuck-phase-transition`` emission after budget, the sticky latch,
# bus-movement reset, in-flight-Job suppression, and notifier-None dormancy.
# ---------------------------------------------------------------------------


class _NotifierSpy:
    """Captures convergence-stall notifier invocations."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *, anomaly, priority, summary, detail) -> None:
        self.calls.append(
            {"anomaly": anomaly, "priority": priority, "summary": summary, "detail": detail}
        )


class _StallTracker:
    """Fake tracker exposing only what ``_check_convergence_stall`` reads.

    ``get_latest_progress_timestamp`` returns the latest BRC-bus activity as
    a ``datetime`` (or ``None`` for "no bus activity ever").
    """

    def __init__(self, latest=None) -> None:
        self.latest = latest

    def get_latest_progress_timestamp(self):
        return self.latest


def _bus_dt(epoch: float):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, tz=UTC)


def _make_stall_loop(monkeypatch, *, notifier, tracker, roles=("coder",), budget_min=10):
    """Build a loop wired for convergence-stall tests.

    Sets ``EGG_BRC_IDLE_BUDGET_MIN`` (default 10 ⇒ 600s budget) and scripts
    ``_derive_next_action`` so each role derives a pending ``propose`` event.
    """
    import event_loop

    monkeypatch.setenv("EGG_BRC_IDLE_BUDGET_MIN", str(budget_min))
    _script(monkeypatch, dict.fromkeys(roles, ("propose", _PROPOSE_PAYLOAD, "x")))
    return event_loop.OrchestratorEventLoop(
        tracker=tracker,
        spawner=_RecordingSpawner(),
        pipeline_id="issue-3064",
        slice_id="slice-5",
        phase="implement",
        roles=list(roles),
        convergence_stall_notifier=notifier,
    )


def _patch_now(monkeypatch, holder):
    """Patch ``event_loop.time.time`` to return ``holder['now']``."""
    import event_loop

    monkeypatch.setattr(event_loop.time, "time", lambda: holder["now"])


class TestConvergenceStall:
    """Behavioral coverage of ``OrchestratorEventLoop._check_convergence_stall``."""

    BASE = 1_000_000.0
    BUDGET_SEC = 600  # budget_min=10

    def test_no_alert_before_budget(self, monkeypatch):
        """No alert while the bus has been quiet for less than the budget."""
        now = {"now": self.BASE}
        _patch_now(monkeypatch, now)
        spy = _NotifierSpy()
        # Bus last moved 50s ago — well within the budget.
        tracker = _StallTracker(latest=_bus_dt(self.BASE - 50))
        loop = _make_stall_loop(monkeypatch, notifier=spy, tracker=tracker)

        loop._check_convergence_stall()  # idle 50s < budget
        now["now"] = self.BASE + self.BUDGET_SEC - 100  # idle still < budget
        loop._check_convergence_stall()

        assert spy.calls == []

    def test_alert_fires_once_after_budget(self, monkeypatch):
        """Once the bus is quiet past the budget the loop emits once.

        Seeding is bus-timestamp-relative (event-based), so the alert fires
        on the first poll where ``now - last_bus_activity`` exceeds the
        budget — it does not wait a full budget window after the loop first
        observes the pending event (#3064 review NB3).
        """
        from consensus_wrapper import EVENT_PUMP_IDLE_BUDGET_ANOMALY

        now = {"now": self.BASE}
        _patch_now(monkeypatch, now)
        spy = _NotifierSpy()
        # Bus quiet for longer than the budget already at the first poll.
        tracker = _StallTracker(latest=_bus_dt(self.BASE - (self.BUDGET_SEC + 10)))
        loop = _make_stall_loop(monkeypatch, notifier=spy, tracker=tracker)

        loop._check_convergence_stall()  # idle > budget on first observation

        assert len(spy.calls) == 1
        call = spy.calls[0]
        # Anomaly name must match the in-pod wrapper's constant exactly.
        assert call["anomaly"] == EVENT_PUMP_IDLE_BUDGET_ANOMALY
        assert call["anomaly"] == "stuck-phase-transition"
        assert call["priority"] == "high"
        assert "coder" in call["summary"]

    def test_sticky_latch_no_duplicate_emission(self, monkeypatch):
        """Once alerted, further polls in the same stall episode are silent."""
        now = {"now": self.BASE}
        _patch_now(monkeypatch, now)
        spy = _NotifierSpy()
        tracker = _StallTracker(latest=_bus_dt(self.BASE - (self.BUDGET_SEC + 10)))
        loop = _make_stall_loop(monkeypatch, notifier=spy, tracker=tracker)

        loop._check_convergence_stall()  # emits
        now["now"] = self.BASE + 100
        loop._check_convergence_stall()  # latched — must not re-emit
        now["now"] = self.BASE + 1000
        loop._check_convergence_stall()  # still latched

        assert len(spy.calls) == 1

    def test_reset_when_bus_moves(self, monkeypatch):
        """Recent bus activity clears the latch; a fresh stall re-alerts."""
        now = {"now": self.BASE}
        _patch_now(monkeypatch, now)
        spy = _NotifierSpy()
        tracker = _StallTracker(latest=_bus_dt(self.BASE - (self.BUDGET_SEC + 10)))
        loop = _make_stall_loop(monkeypatch, notifier=spy, tracker=tracker)

        loop._check_convergence_stall()  # first emission
        assert len(spy.calls) == 1

        # Bus moves (activity within the budget window) → all-roles reset
        # clears the latch; the still-pending role is re-observed in the same
        # pass, re-anchoring first-seen to the NEW bus timestamp (a fresh
        # stall episode) with the latch cleared.
        bus_ts2 = self.BASE + 645
        now["now"] = bus_ts2 + 5  # idle 5s < budget
        tracker.latest = _bus_dt(bus_ts2)
        loop._check_convergence_stall()
        assert loop._stall_alerted == {}
        assert loop._stall_first_seen.get("coder") == bus_ts2

        # Bus stays quiet at bus_ts2; advancing past the budget re-alerts.
        now["now"] = bus_ts2 + self.BUDGET_SEC + 10
        loop._check_convergence_stall()
        assert len(spy.calls) == 2

    def test_in_flight_job_suppresses_alert(self, monkeypatch):
        """A role whose event has a live Job is being handled — never stalled."""
        import event_loop

        now = {"now": self.BASE}
        _patch_now(monkeypatch, now)
        spy = _NotifierSpy()
        tracker = _StallTracker(latest=_bus_dt(self.BASE - 10_000))
        loop = _make_stall_loop(monkeypatch, notifier=spy, tracker=tracker)

        # Mark the coder's propose event as in-flight.
        identity = event_loop.event_identity("propose", _PROPOSE_PAYLOAD)
        key = event_loop.compute_dedupe_key(
            loop.pipeline_id, loop.slice_id, loop.phase, "coder", "propose", identity
        )
        loop._live_keys.add(key)

        loop._check_convergence_stall()
        now["now"] = self.BASE + self.BUDGET_SEC + 10
        loop._check_convergence_stall()

        assert spy.calls == []
        assert "coder" not in loop._stall_first_seen

    def test_dormant_when_notifier_none(self, monkeypatch):
        """With no notifier wired the check is inert — no tracking, no raise."""
        now = {"now": self.BASE}
        _patch_now(monkeypatch, now)
        tracker = _StallTracker(latest=_bus_dt(self.BASE - 10_000))
        loop = _make_stall_loop(monkeypatch, notifier=None, tracker=tracker)

        loop._check_convergence_stall()
        now["now"] = self.BASE + self.BUDGET_SEC + 10
        loop._check_convergence_stall()

        # Returned early both times — no per-role state accumulated.
        assert loop._stall_first_seen == {}
        assert loop._stall_alerted == {}

    def test_agent_free_role_never_stalls(self, monkeypatch):
        """confirm/complete/wait roles make progress agent-free — not stalled."""
        now = {"now": self.BASE}
        _patch_now(monkeypatch, now)
        spy = _NotifierSpy()
        tracker = _StallTracker(latest=_bus_dt(self.BASE - 10_000))
        loop = _make_stall_loop(monkeypatch, notifier=spy, tracker=tracker, roles=("reviewer",))
        # Override the script: reviewer derives a non-spawn (confirm) action.
        _script(monkeypatch, {"reviewer": ("confirm", None, "x")})

        loop._check_convergence_stall()
        now["now"] = self.BASE + self.BUDGET_SEC + 10
        loop._check_convergence_stall()

        assert spy.calls == []
        assert "reviewer" not in loop._stall_first_seen


# ---------------------------------------------------------------------------
# Active-roles publishing (#3064 slice-5, TASK-5-1)
#
# poll_once must publish the set of roles with a live one-shot Job to the
# health monitor's ``set_active_roles`` (via the ``active_roles_notifier``
# callback). Without this the monitor's ``_active_jobs`` stays empty in
# orchestrator mode and every tripwire is suppressed.
# ---------------------------------------------------------------------------


class _ActiveRolesRecorder:
    """Captures each set published to ``active_roles_notifier``."""

    def __init__(self) -> None:
        self.published: list[set] = []

    def __call__(self, roles) -> None:
        self.published.append(set(roles))


def _make_publishing_loop(monkeypatch, notifier, *, roles=("coder",)):
    import event_loop

    return event_loop.OrchestratorEventLoop(
        tracker=object(),
        spawner=_RecordingSpawner(),
        pipeline_id="issue-3064",
        slice_id="slice-5",
        phase="implement",
        roles=list(roles),
        clock=_FakeClock(),
        active_roles_notifier=notifier,
    )


class TestActiveRolesPublishing:
    """poll_once publishes the live-Job role set to the monitor."""

    def test_publishes_live_role_after_spawn(self, monkeypatch):
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        rec = _ActiveRolesRecorder()
        loop = _make_publishing_loop(monkeypatch, rec)

        loop.poll_once(["coder"])

        # The newly spawned coder Job is live → published this tick.
        assert rec.published[-1] == {"coder"}

    def test_publishes_empty_when_no_live_jobs(self, monkeypatch):
        _script(monkeypatch, {"coder": ("wait", {"blocking_agents": []}, "x")})
        rec = _ActiveRolesRecorder()
        loop = _make_publishing_loop(monkeypatch, rec)

        loop.poll_once(["coder"])

        # wait derives no spawn → no live Job → empty set published.
        assert rec.published[-1] == set()

    def test_no_notifier_is_safe(self, monkeypatch):
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})
        loop = _make_publishing_loop(monkeypatch, None)

        # Must not raise when no notifier is wired (pod mode / unit tests).
        loop.poll_once(["coder"])

    def test_publish_failure_does_not_wedge_poll(self, monkeypatch):
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})

        def _boom(_roles):
            raise RuntimeError("monitor unreachable")

        loop = _make_publishing_loop(monkeypatch, _boom)

        # A notifier that raises is swallowed — the poll still returns.
        decisions = loop.poll_once(["coder"])
        assert decisions[0].action == "propose"

    def test_reconciled_role_published_via_dedupe_path(self, monkeypatch):
        """A key seeded by ``reconcile()`` (``_live_keys`` populated, no
        ``_key_meta`` entry) hits the dedupe early-return on the next poll
        rather than a fresh spawn. That path must still label the key so the
        adopted/reconciled role is published — otherwise its tripwires stay
        silently suppressed for the pod's lifetime.
        """
        _script(monkeypatch, {"coder": ("propose", _PROPOSE_PAYLOAD, "x")})

        # Derive the live label exactly as a pre-restart loop would have.
        # Same slice_id as the publishing loop so the dedupe keys match.
        spawner1 = _RecordingSpawner()
        loop1 = _make_loop(spawner1, slice_id="slice-5")
        loop1.poll_once(["coder"])
        live_labels = list(loop1.live_dedupe_keys())
        assert len(live_labels) == 1

        # Restart: fresh publishing loop, reconcile from the live Job label.
        rec = _ActiveRolesRecorder()
        loop2 = _make_publishing_loop(monkeypatch, rec)
        loop2.reconcile(live_labels)

        decisions = loop2.poll_once(["coder"])

        # Dedupe path taken (no re-spawn) but the role is now labeled + published.
        assert decisions[0].spawned is False
        assert rec.published[-1] == {"coder"}

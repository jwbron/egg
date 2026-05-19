"""Plan-phase in-process BRC end-to-end test (#2717 slice-2 task-2-4).

Acceptance criteria covered (per contract task-2-4):

* Boots ``run_pipeline_in_process`` against a deterministic pipeline
  id with harness-faked subagents.
* Advances past the refine HITL gate by sending
  ``"approve_continue"`` to the refine-gate yield.
* Asserts the plan stage spawns 3 producers (architect, task_planner,
  risk_analyst) + 1 reviewer (reviewer_plan).
* Asserts the BRC mechanics reach ``CONSENSUS_CONFIRMED`` on every
  producer edge (architect → reviewer_plan, task_planner →
  reviewer_plan, risk_analyst → reviewer_plan) by inspecting the
  in-process orchestrator's ``_plan_tracker.evaluate()`` snapshot.
* Asserts the stage yields a plan-HITL decision with the expected
  fields (id, question, options, decision_type, phase).
* Test runs in <120s under the harness fakes (no real Anthropic API
  call, no real Claude Code spawn).

The test uses ``MagicMock`` substrate-bundle fakes mirroring the
existing ``fake_bundle`` fixture in
``shared/tests/test_run_pipeline_in_process_sentinel_and_hitl.py`` so
a behaviour drift between unit and integration coverage is caught.

Why this is the slice-2 BRC stress test
---------------------------------------
Slice-1 wired one role (``refiner``) end-to-end on the substrate;
slice-2 is the **first multi-role BRC stress test** — 3 producers
concurrent, 1 reviewer, four CONFIRMED transitions to converge. The
plan describes this as "first multi-role BRC stress test on the
substrate". The test exercises the same ``ThreadPoolExecutor``
concurrency the implementation uses (``_run_plan_phase`` per
``orchestrator/substrate/in_process.py:830``) so a regression in
the BRC mechanics under multi-producer concurrency surfaces here
rather than in the slice-3 implement-phase test.

Why CONSENSUS_CONFIRMED is verified via the tracker, not the bus
----------------------------------------------------------------
The in-process substrate's spawner is synchronous: when
``bundle.spawner.spawn(role, ...)`` returns, the subagent has
finished. The coder's TASK-2-1 implementation therefore drives the
BRC transitions deterministically by calling
``PeerConsensusTracker.handle_propose(...)``,
``handle_ack(...)``, and ``handle_confirmed(...)`` on the
orchestrator's behalf — the harness-faked subagents do not emit
their own BRC messages. ``handle_confirmed`` does NOT publish a
``CONSENSUS_CONFIRMED`` message to the bus; it updates internal
state and the source of truth is ``tracker.evaluate()`` which
returns ``is_complete``, ``blocking_agents``, and a per-agent
``confirmed`` flag. The test asserts every plan-team role is in
the ``confirmed`` set, which is the in-process analogue of "fired
CONSENSUS_CONFIRMED" on the bus.

Graceful skip on missing implementation
---------------------------------------
The test is committed against a contract that names the
``_run_plan_phase`` method. If the coder's TASK-2-1 implementation
has not yet been merged, the relevant attribute on
``_InProcessOrchestrator`` is missing and the test skips with a
clear pointer. Once TASK-2-1 lands the skip disappears and the
assertions run. This keeps the tester unblocked when scaffolding
ahead of the coder (per the role's scaffold-first guidance).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
in_process_mod = pytest.importorskip(
    "orchestrator.substrate.in_process",
    reason="orchestrator/substrate/in_process.py not present yet",
)
agent_roles_mod = pytest.importorskip(
    "egg_contracts.agent_roles",
    reason="shared/egg_contracts/agent_roles.py not importable",
)

AgentRole = agent_roles_mod.AgentRole


# ---------------------------------------------------------------------------
# Plan-phase role expectations — pinned from
# ``shared/egg_contracts/agent_roles.py`` (``_PHASE_ROLES["plan"]``
# and ``_PHASE_REVIEWERS["plan"]``). If those maps drift the test
# fails loudly with a clear pointer to the source of truth.
#
# Untyped containers because ``AgentRole`` resolves through
# ``pytest.importorskip`` — mypy sees it as a runtime value, not a
# class, and a ``frozenset[AgentRole]`` annotation would be rejected
# as "Variable AgentRole is not valid as a type" (mirroring how
# ``shared/tests/test_rubric_loader.py`` consumes the enum without
# annotation).
# ---------------------------------------------------------------------------

_EXPECTED_PRODUCERS = frozenset(
    {AgentRole.ARCHITECT, AgentRole.TASK_PLANNER, AgentRole.RISK_ANALYST}
)
_EXPECTED_REVIEWERS = frozenset({AgentRole.REVIEWER_PLAN})


# ---------------------------------------------------------------------------
# Fixtures — short intervals + fake substrate bundle
# ---------------------------------------------------------------------------


@pytest.fixture
def short_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink background-thread intervals so tests run in seconds."""
    monkeypatch.setattr(in_process_mod, "_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BRC_REVIEW_INTERVAL", 0.05)
    monkeypatch.setattr(in_process_mod, "_BUS_TICK_INTERVAL", 0.05)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` at a clean tmp dir so sentinel reads/writes are isolated.

    The generator's ``_write_active_role_sentinel`` writes under
    ``$HOME/.claude/egg-active-role.json``; without this fixture the
    test would pollute the developer's actual home directory.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def isolated_pipeline_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level ``PeerConsensusTracker`` registry between tests.

    ``get_peer_consensus_tracker(pipeline_id)`` returns a module-level
    cached tracker; back-to-back tests using the same pipeline id
    would otherwise inherit confirmed state from each other. Clear
    the registry so every test starts from a fresh tracker.
    """
    try:
        import orchestrator.peer_consensus as peer_consensus
    except ImportError:  # pragma: no cover — defensive
        return
    # The registry symbol name varies across decomposition slices —
    # try a few candidates rather than pin a specific private name.
    for candidate in ("_TRACKERS", "_PEER_CONSENSUS_TRACKERS", "_tracker_registry"):
        registry = getattr(peer_consensus, candidate, None)
        if isinstance(registry, dict):
            registry.clear()


def _make_fake_bundle(tmp_path: Path, *, write_producer_outputs: bool = True) -> MagicMock:
    """Build a substrate bundle that records every spawn invocation.

    The fake spawner returns a synthetic ``AgentResult`` (``exit_code=0``,
    40-zero commit, ``stdout="ok"``) for every role. The fake's
    ``spawner.spawn`` is a ``MagicMock`` so the test can inspect
    ``.call_args_list`` to verify which roles were dispatched.

    When ``write_producer_outputs=True`` (default), the fake spawner
    also creates each producer's ``EGG_PRODUCER_OUTPUT_PATH`` JSON
    file before returning so the N9 architect-handoff guard in
    ``_run_plan_phase`` (reviewer_code v3 non-blocking NB1) sees a
    well-formed handoff and the happy-path BRC mechanics converge.
    Tests that want to exercise the N9 fail-fast path pass
    ``write_producer_outputs=False`` to leave the architect output
    missing.
    """
    bundle = MagicMock()

    def _spawn(role: Any, _prompt: str, env: dict[str, str], _worktree: Any) -> MagicMock:
        if write_producer_outputs:
            output_path = env.get("EGG_PRODUCER_OUTPUT_PATH")
            if output_path:
                target = Path(output_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    '{"role": "' + getattr(role, "value", str(role)) + '", "summary": "ok"}',
                    encoding="utf-8",
                )
        return MagicMock(
            exit_code=0,
            commit_sha="0" * 40,
            stdout="ok",
            worktree=tmp_path / "wt",
            artifacts=[],
        )

    bundle.spawner.spawn = MagicMock(side_effect=_spawn)
    bundle.worktrees.create = MagicMock(return_value=tmp_path / "wt")
    bundle.worktrees.tear_down = MagicMock()
    bundle.name = "claude-code"

    # InProcessMessageBus exposes ``add_message`` / ``get_messages``.
    # Back the fake with a real InProcessMessageBus instance so the
    # in-process orchestrator's background bus-tick + heartbeat loops
    # see a working surface (a MagicMock would return truthy garbage
    # and the loops swallow the resulting type errors via their bare
    # except clauses).
    try:
        from orchestrator.substrate.claude_code.message_bus import InProcessMessageBus

        bundle.bus = InProcessMessageBus()
    except ImportError:  # pragma: no cover — defensive
        bundle.bus = MagicMock()

    return bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_plan_stage() -> bool:
    """Return True iff ``_InProcessOrchestrator`` has a plan-stage method.

    The coder's TASK-2-1 implementation uses ``_run_plan_phase``;
    a few peer names are also accepted so the test does not pin a
    specific private method name. What the test enforces is the
    observable behaviour: a plan-HITL yield with the expected fields
    and three concurrent producer spawns.
    """
    runner_cls = getattr(in_process_mod, "_InProcessOrchestrator", None)
    if runner_cls is None:
        return False
    return any(
        hasattr(runner_cls, attr)
        for attr in (
            "_run_plan_phase",
            "_run_plan",
            "run_plan",
            "_dispatch_plan",
            "_plan_stage",
        )
    )


def _spawned_roles(bundle: MagicMock) -> set[Any]:
    """Return the set of roles passed to ``bundle.spawner.spawn``.

    ``spawn`` signature per ``AgentSpawner.spawn(role, prompt, env,
    worktree)`` — the role is the first positional arg.
    """
    roles: set[Any] = set()
    for call in bundle.spawner.spawn.call_args_list:
        if call.args:
            roles.add(call.args[0])
        elif "role" in call.kwargs:
            roles.add(call.kwargs["role"])
    return roles


def _drive_past_refine_gate(gen: Any) -> Any:
    """Drive the generator through preflight + refine HITL gate.

    Returns the next yield (the plan-HITL decision when TASK-2-1
    has landed).

    The driving sequence:
      1. ``next(gen)`` — preflight HITL.
      2. ``send("approve")`` — past preflight, into refiner spawn.
      3. ``send("approve_continue")`` — past refine HITL gate, into
         the plan-phase BRC stage.
    """
    next(gen)
    gen.send("approve")
    return gen.send("approve_continue")


def _runner_from_gen(gen: Any) -> Any | None:
    """Pull the ``_InProcessOrchestrator`` instance out of a live generator.

    The generator's frame's ``self`` local is the runner; the test
    needs the runner to read ``self._plan_tracker.evaluate()`` after
    the plan stage runs.
    """
    frame = gen.gi_frame
    if frame is None:
        return None
    return frame.f_locals.get("self")


# ---------------------------------------------------------------------------
# Tests — plan-phase BRC end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on "
        "``_InProcessOrchestrator`` yet. This test runs once the "
        "plan-stage method (e.g. ``_run_plan_phase``) is present."
    ),
)
def test_plan_stage_spawns_three_producers_and_one_reviewer(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Plan stage spawns the four plan-phase roles via the substrate bundle.

    Drives the generator past the refine HITL gate and asserts the
    fake spawner observed calls for ``architect``, ``task_planner``,
    ``risk_analyst`` (producers) and ``reviewer_plan`` (reviewer).
    The producer ordering is not pinned — they run concurrently
    via ``ThreadPoolExecutor`` per ``_run_plan_phase``'s call to
    ``concurrent.futures.ThreadPoolExecutor``.

    Acceptance bullet: plan stage spawns 3 producers + 1 reviewer.
    """
    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-spawns"
    run = in_process_mod.run_pipeline_in_process

    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            try:
                _drive_past_refine_gate(gen)
            except NotImplementedError as exc:
                pytest.fail(
                    "_maybe_fence still raises NotImplementedError after "
                    "TASK-2-1 was expected to remove the plan-phase fence "
                    "branch. TASK-2-1 must have removed the plan branch "
                    "of the fence; only ``approve_continue`` past the "
                    f"plan-HITL gate should still trip it. Error: {exc!r}"
                )
        finally:
            gen.close()
            # Background threads need a moment to wind down.
            time.sleep(0.2)

    spawned = _spawned_roles(bundle)
    # The refiner spawn happens before the plan stage — strip it
    # before checking the plan-phase role set.
    plan_spawned = spawned - {AgentRole.REFINER}

    missing_producers = _EXPECTED_PRODUCERS - plan_spawned
    assert not missing_producers, (
        f"plan stage must spawn all three producers; "
        f"missing={sorted(r.value for r in missing_producers)} "
        f"spawned={sorted(getattr(r, 'value', str(r)) for r in plan_spawned)}"
    )
    missing_reviewers = _EXPECTED_REVIEWERS - plan_spawned
    assert not missing_reviewers, (
        f"plan stage must spawn reviewer_plan; missing="
        f"{sorted(r.value for r in missing_reviewers)} "
        f"spawned={sorted(getattr(r, 'value', str(r)) for r in plan_spawned)}"
    )


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_stage_yields_hitl_decision_with_expected_fields(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Plan stage yields a HITL decision with question / options / phase set.

    Per task-2-4 acceptance: "asserts the stage yields a plan-HITL
    decision with the expected fields". The fields enforced here:

    * ``id`` — non-empty string (used by the contract decisions list).
    * ``question`` — non-empty string the operator reads.
    * ``options`` — non-empty sequence of allowed answers.
    * ``decision_type`` — one of ``phase_gate`` or ``choice`` (the
      same shapes the refine-gate uses; the plan gate is a phase
      gate by analogy).
    * ``phase`` — ``"plan"`` (the gate is the plan→implement
      boundary).

    The exact strings are owned by the coder; the shape is pinned
    here so a regression that yields a None / falsy decision or one
    missing key fields fails clearly.
    """
    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-hitl"
    run = in_process_mod.run_pipeline_in_process

    plan_hitl: Any = None
    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            plan_hitl = _drive_past_refine_gate(gen)

            assert plan_hitl is not None, (
                "plan stage must yield a HITLDecision after the "
                "refine-gate answer ``approve_continue``; got None."
            )
            # ``HITLDecision`` is a dataclass; both attribute access
            # and ``.id`` / ``.question`` work. Tolerate dict-shaped
            # answers too in case a future plan-HITL change moves
            # to a dict envelope.
            decision_id = getattr(plan_hitl, "id", None) or (
                plan_hitl.get("id") if isinstance(plan_hitl, dict) else None
            )
            question = getattr(plan_hitl, "question", None) or (
                plan_hitl.get("question") if isinstance(plan_hitl, dict) else None
            )
            options = getattr(plan_hitl, "options", None) or (
                plan_hitl.get("options") if isinstance(plan_hitl, dict) else None
            )
            decision_type = getattr(plan_hitl, "decision_type", None) or (
                plan_hitl.get("decision_type") if isinstance(plan_hitl, dict) else None
            )
            phase = getattr(plan_hitl, "phase", None) or (
                plan_hitl.get("phase") if isinstance(plan_hitl, dict) else None
            )

            assert isinstance(decision_id, str) and decision_id, (
                f"plan-HITL ``id`` must be a non-empty string; got {decision_id!r}"
            )
            assert isinstance(question, str) and question, (
                f"plan-HITL ``question`` must be a non-empty string; got {question!r}"
            )
            assert options, f"plan-HITL ``options`` must be a non-empty sequence; got {options!r}"
            # Tolerate the plan-gate landing as either a phase_gate
            # (same as the refine-gate's terminal yield) or a choice
            # (the lightweight variant). Anything else (a free-form
            # ``feedback`` decision, ``confirm``, etc.) would be a
            # design regression — the plan gate is a phase boundary
            # the operator approves / changes / stops.
            assert decision_type in {"phase_gate", "choice"}, (
                f"plan-HITL ``decision_type`` must be phase_gate or choice; got {decision_type!r}"
            )
            # phase may arrive as the enum value (``"plan"``) or the
            # enum member; tolerate both.
            phase_str = getattr(phase, "value", phase)
            assert phase_str == "plan", (
                f"plan-HITL ``phase`` must equal ``'plan'``; got {phase_str!r}"
            )
        finally:
            gen.close()
            time.sleep(0.2)


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_stage_reaches_consensus_confirmed_for_each_producer(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Plan stage drives BRC to CONSENSUS_CONFIRMED on every producer edge.

    Per task-2-4 acceptance: "reaches CONSENSUS_CONFIRMED for all
    three plan-phase BRC edges (architect → reviewer_plan,
    task_planner → reviewer_plan, risk_analyst → reviewer_plan)".

    Inspects the in-process orchestrator's ``_plan_tracker`` (a
    ``PeerConsensusTracker`` registered against the plan-phase
    review graph) for the per-agent ``confirmed`` flag from
    ``evaluate()``. CONSENSUS_CONFIRMED is the tracker's in-memory
    state transition, not a bus message — see this file's
    module-level docstring.
    """
    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-consensus"
    run = in_process_mod.run_pipeline_in_process

    runner = None
    eval_snapshot: dict[str, Any] | None = None

    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            _drive_past_refine_gate(gen)
            # Pull the runner before closing the generator so we
            # have access to ``_plan_tracker`` and the stashed
            # ``_plan_eval`` snapshot.
            runner = _runner_from_gen(gen)
            tracker = getattr(runner, "_plan_tracker", None)
            if tracker is not None:
                eval_snapshot = tracker.evaluate()
            elif runner is not None:
                # Fallback: the implementation may stash the eval
                # snapshot on the runner directly.
                eval_snapshot = getattr(runner, "_plan_eval", None)
        finally:
            gen.close()
            # Background threads need a moment to flush.
            time.sleep(0.3)

    assert eval_snapshot is not None, (
        "plan stage must register a ``_plan_tracker`` (or stash a "
        "``_plan_eval`` snapshot) on the orchestrator so the operator's "
        "HITL gate sees the BRC evaluation; neither attribute was "
        "populated. Without these, the plan-HITL gate cannot surface "
        "partial-consensus state."
    )

    # ``evaluate()`` returns a per-role ``agents`` map with ``confirmed``
    # booleans. The plan-team confirmed set must include every producer
    # AND the reviewer.
    agents = eval_snapshot.get("agents") or {}
    plan_team = ("architect", "task_planner", "risk_analyst", "reviewer_plan")
    not_confirmed = [
        role for role in plan_team if not (agents.get(role, {}) or {}).get("confirmed", False)
    ]
    assert not not_confirmed, (
        f"plan-phase BRC must reach CONSENSUS_CONFIRMED on every "
        f"plan-team role; not_confirmed={not_confirmed!r}; "
        f"agents snapshot={agents!r}. The fake spawner returns "
        f"exit_code=0 for every spawn so a missing confirmation "
        f"points at the BRC mechanics, not the spawn shim."
    )

    # And the high-level ``is_complete`` flag should be True — every
    # producer confirmed AND no unresolved NACKs.
    assert eval_snapshot.get("is_complete") is True, (
        f"plan-phase BRC ``is_complete`` must be True after every "
        f"producer reaches CONFIRMED; got "
        f"{eval_snapshot.get('is_complete')!r}. blocking_agents="
        f"{eval_snapshot.get('blocking_agents')!r}, "
        f"unresolved_nacks={eval_snapshot.get('unresolved_nacks')!r}"
    )


# ---------------------------------------------------------------------------
# Adversarial probing — plan stage edge cases the coder must hold
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_stage_does_not_run_when_operator_rejects_refine(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Plan stage MUST NOT run when the operator picks a non-continue answer.

    The plan stage is gated on the operator answering the refine
    HITL with ``approve_continue``. Other answers (``request_changes``,
    ``change_approach``, ``stop``) terminate the generator without
    advancing into plan; ``stop`` returns the artifact path,
    ``request_changes`` re-loops the refine, and ``change_approach``
    aborts. A regression that fans into plan on a non-continue
    answer would burn three subagent spawns the operator did not
    approve.

    Adversarial probe: ``stop`` after the refine-gate must NOT
    invoke ``bundle.spawner.spawn`` for any of the plan-phase roles.
    """
    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-refine-stop"
    run = in_process_mod.run_pipeline_in_process

    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            next(gen)
            gen.send("approve")
            try:
                gen.send("stop")
                # ``stop`` is terminal — the generator must
                # StopIteration with the artifact path string.
            except StopIteration as stop:
                assert isinstance(stop.value, str), (
                    f"``stop`` must terminate cleanly with the "
                    f"artifact path; got value={stop.value!r}"
                )
        finally:
            gen.close()
            time.sleep(0.2)

    spawned = _spawned_roles(bundle)
    plan_spawned = spawned & _EXPECTED_PRODUCERS
    assert not plan_spawned, (
        f"plan stage MUST NOT spawn producers when the operator "
        f"answers ``stop`` at the refine gate; saw spawns for "
        f"{sorted(r.value for r in plan_spawned)}. This is a HITL "
        f"safety bug — three subagent spawns the operator did not "
        f"approve."
    )


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_stage_does_not_spawn_implement_phase_roles(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Plan stage spawns only plan-phase roles, never implement-phase ones.

    Adversarial probe against a misrouted ``_PHASE_ROLES`` lookup:
    if the coder accidentally indexed by ``"implement"`` instead of
    ``"plan"`` (off-by-one in a phase dispatch table), the plan
    stage would spawn ``coder`` / ``tester`` / ``documenter``
    instead. Pin the negative invariant.
    """
    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-phase-isolation"
    run = in_process_mod.run_pipeline_in_process

    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            _drive_past_refine_gate(gen)
        finally:
            gen.close()
            time.sleep(0.2)

    forbidden = {
        AgentRole.CODER,
        AgentRole.TESTER,
        AgentRole.DOCUMENTER,
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CODE_HOLISTIC,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_SECURITY,
        AgentRole.REVIEWER_CONCURRENCY,
    }
    spawned = _spawned_roles(bundle)
    leaked = spawned & forbidden
    assert not leaked, (
        f"plan stage MUST NOT spawn implement-phase roles; saw "
        f"spawns for {sorted(getattr(r, 'value', str(r)) for r in leaked)}. "
        f"This points at a phase-dispatch lookup that indexed "
        f"``_PHASE_ROLES`` with the wrong key."
    )


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_stage_does_not_invoke_refiner_a_second_time(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Plan stage MUST NOT re-spawn the refiner.

    Adversarial probe: the refiner already ran in the refine stage
    before the operator's approve_continue. A regression that
    re-included REFINER in the plan-phase producer set (off-by-one
    in role iteration) would burn an extra spawn and write a stale
    refine artifact. Pin the single-refiner-spawn invariant.
    """
    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-refiner-once"
    run = in_process_mod.run_pipeline_in_process

    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            _drive_past_refine_gate(gen)
        finally:
            gen.close()
            time.sleep(0.2)

    refiner_spawn_count = sum(
        1
        for call in bundle.spawner.spawn.call_args_list
        if call.args and call.args[0] == AgentRole.REFINER
    )
    assert refiner_spawn_count == 1, (
        f"refiner must be spawned exactly once (in the refine stage "
        f"before the plan stage); saw {refiner_spawn_count} spawn(s)."
    )


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_stage_carries_phase_env_var_to_producers(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Every plan-phase spawn must carry ``EGG_PHASE=plan`` in its env.

    Adversarial probe: a regression that forgot to set ``EGG_PHASE``
    on plan-producer spawn envs would cause the spawned subagents
    to see the wrong phase and possibly drop into refine code paths
    or skip plan-specific contract validation. Pin the env-propagation
    contract.

    Refiner spawns are excluded — the refiner runs in the refine
    phase and the refine spawn shape does not include ``EGG_PHASE``.
    """
    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-env"
    run = in_process_mod.run_pipeline_in_process

    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            _drive_past_refine_gate(gen)
        finally:
            gen.close()
            time.sleep(0.2)

    plan_spawn_envs: list[dict[str, str]] = []
    for call in bundle.spawner.spawn.call_args_list:
        role = call.args[0] if call.args else call.kwargs.get("role")
        if role == AgentRole.REFINER:
            continue
        # ``spawn(role, prompt, env, worktree)`` — env is args[2] or
        # the ``env`` kwarg.
        env_arg = None
        if len(call.args) >= 3:
            env_arg = call.args[2]
        else:
            env_arg = call.kwargs.get("env")
        if isinstance(env_arg, dict):
            plan_spawn_envs.append(env_arg)

    assert plan_spawn_envs, (
        "no plan-phase spawn invocations had an env dict captured; "
        "the spawn() call shape may have changed — update this test."
    )
    missing_phase = [env for env in plan_spawn_envs if env.get("EGG_PHASE") != "plan"]
    assert not missing_phase, (
        f"every plan-phase spawn env must set EGG_PHASE=plan; "
        f"{len(missing_phase)}/{len(plan_spawn_envs)} envs were "
        f"missing or wrong. Examples (capped at 3): "
        f"{[{k: v for k, v in env.items() if k.startswith('EGG_')} for env in missing_phase[:3]]}"
    )


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_gate_decision_persists_with_phase_plan(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """Plan-gate decision must persist with ``phase: "plan"`` in the contract.

    Reviewer_code v1 blocker B1 (#2717 slice-2): the in-process
    orchestrator previously hardcoded ``phase: "refine"`` inside
    ``_write_pending_decision``, so every plan-gate decision landed
    on disk with the wrong phase even though the yielded
    ``HITLDecision`` itself carried ``phase="plan"``. Pin the
    invariant that the persisted decision's ``phase`` field matches
    the yielded decision's ``phase`` so the regression cannot recur.
    """
    import json

    bundle = _make_fake_bundle(tmp_path)

    pipeline_id = "pipeline-plan-brc-phase-persisted"
    state_dir = tmp_path / ".egg-state"
    run = in_process_mod.run_pipeline_in_process

    plan_hitl: Any = None
    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=state_dir,
        )
        try:
            plan_hitl = _drive_past_refine_gate(gen)
        finally:
            gen.close()
            time.sleep(0.2)

    assert plan_hitl is not None, (
        "plan stage must yield a HITLDecision after the refine-gate "
        "answer ``approve_continue``; got None."
    )

    yielded_phase = getattr(plan_hitl, "phase", None) or (
        plan_hitl.get("phase") if isinstance(plan_hitl, dict) else None
    )
    yielded_phase_str = getattr(yielded_phase, "value", yielded_phase)
    yielded_id = getattr(plan_hitl, "id", None) or (
        plan_hitl.get("id") if isinstance(plan_hitl, dict) else None
    )

    contract_path = state_dir / "contracts" / f"{pipeline_id}.json"
    assert contract_path.is_file(), (
        f"plan-gate decision must persist to {contract_path}; not found."
    )
    contract = json.loads(contract_path.read_text())

    decisions = contract.get("decisions") or []
    plan_decision = next(
        (d for d in decisions if d.get("id") == yielded_id),
        None,
    )
    assert plan_decision is not None, (
        f"plan-gate decision with id={yielded_id!r} not found in "
        f"persisted contract decisions={decisions!r}."
    )
    assert plan_decision.get("phase") == yielded_phase_str, (
        f"persisted decision's phase must equal yielded decision's "
        f"phase; persisted={plan_decision.get('phase')!r} vs "
        f"yielded={yielded_phase_str!r}. Reviewer_code v1 blocker B1 "
        f"(#2717 slice-2)."
    )
    assert contract.get("current_phase") == yielded_phase_str, (
        f"contract's current_phase must equal the yielded plan-gate "
        f"phase; current_phase={contract.get('current_phase')!r} vs "
        f"yielded={yielded_phase_str!r}. Reviewer_code v1 blocker B1."
    )


@pytest.mark.skipif(
    not _has_plan_stage(),
    reason=(
        "task-2-1 (coder) has not landed a plan-stage method on ``_InProcessOrchestrator`` yet."
    ),
)
def test_plan_stage_fails_fast_when_architect_handoff_missing(
    tmp_path: Path,
    fake_home: Path,
    short_intervals: None,
    isolated_pipeline_state: None,
) -> None:
    """N9 fail-fast: missing architect output skips fan-out + reviewer; gate is_complete=False.

    Reviewer_code v3 non-blocking NB1 + NB3 (#2717 slice-2): when the
    architect spawn returns exit_code=0 but never writes its
    ``EGG_PRODUCER_OUTPUT_PATH`` JSON, the orchestrator must
    (a) record a NACK on the ``reviewer_plan → architect`` edge that
    survives to the plan-HITL gate (no optimistic-ACK clobber), and
    (b) skip the downstream fan-out + reviewer spawn entirely (no
    wasted subagents on a dangling handoff). The plan-gate decision
    must surface ``is_complete=False`` with the architect in the
    blocking set and offer the ``retry`` / ``abort`` options.

    Without this invariant, the v2 N9 NACK silently sank under the
    reviewer's optimistic-ACK path and the gate appeared converged.
    """
    # write_producer_outputs=False leaves the architect output missing
    # — exactly the broken-handoff case N9 is designed to catch.
    bundle = _make_fake_bundle(tmp_path, write_producer_outputs=False)

    pipeline_id = "pipeline-plan-brc-n9-fail-fast"
    run = in_process_mod.run_pipeline_in_process

    plan_hitl: Any = None
    runner = None
    with patch(
        "orchestrator.substrate.select_substrate",
        return_value=bundle,
    ):
        gen = run(
            pipeline_id,
            env={"EGG_SUBSTRATE": "claude-code"},
            state_dir=tmp_path / ".egg-state",
        )
        try:
            plan_hitl = _drive_past_refine_gate(gen)
            runner = _runner_from_gen(gen)
        finally:
            gen.close()
            time.sleep(0.2)

    # NB3: downstream producers and reviewer must NOT spawn when N9 fires.
    spawned = _spawned_roles(bundle)
    forbidden_after_n9 = {
        AgentRole.TASK_PLANNER,
        AgentRole.RISK_ANALYST,
        AgentRole.REVIEWER_PLAN,
    }
    leaked = spawned & forbidden_after_n9
    assert not leaked, (
        f"N9 fail-fast must skip downstream fan-out + reviewer spawn "
        f"when the architect output file is missing; saw spawns for "
        f"{sorted(r.value for r in leaked)}. NB3 (#2717 slice-2)."
    )

    # NB1: the plan-gate decision must surface the failure to the operator,
    # not silently complete via the reviewer's optimistic-ACK fallback.
    assert plan_hitl is not None, (
        "plan stage must yield a HITLDecision even on the N9 fail-fast path; got None."
    )
    options = list(getattr(plan_hitl, "options", None) or [])
    assert "retry" in options and "abort" in options, (
        f"plan-gate must offer retry / abort on the N9 fail-fast path; "
        f"got options={options!r}. NB1 (#2717 slice-2): the v2 NACK "
        f"was previously clobbered by optimistic-ACK and the gate "
        f"surfaced the success-path approve_continue options instead."
    )

    # And the tracker's evaluate() must report is_complete=False with
    # the architect in the blocking set.
    tracker = getattr(runner, "_plan_tracker", None)
    assert tracker is not None, "runner must register a plan tracker"
    eval_snapshot = tracker.evaluate()
    assert eval_snapshot.get("is_complete") is False, (
        f"plan-phase BRC must NOT converge when the architect "
        f"handoff is broken; eval={eval_snapshot!r}."
    )
    blocking = set(eval_snapshot.get("blocking_agents") or [])
    assert "architect" in blocking, (
        f"architect must appear in blocking_agents on the N9 path; "
        f"blocking_agents={sorted(blocking)!r}."
    )

"""Slice scheduler for the implement-phase DAG (#2137).

The implement phase used to run as a single monolithic agent team on
one branch through one BRC consensus. Tickets large enough to fill
the context window (empirically ~33K LOC / 41 files) caused
compaction and quality drops. This module replaces that with a DAG
of independent **slices**: each slice is its own integration branch,
fresh BRC tracker, identical agent roster, and stacked PR.

The scheduler is the orchestrator-side glue:

1. Builds a ``DependencyGraph[str]`` from ``Contract.slices`` (the
   slice IDs are the node keys; ``slice.dependencies`` are the edges).
2. Computes execution waves (kahn-style) — every slice in a wave can
   run concurrently because its dependencies are satisfied.
3. Caps wave concurrency at ``max_parallel_slices`` (config; default
   2; env var ``EGG_ORCH_MAX_PARALLEL_SLICES``).
4. Owns the two-tier ``max_cycles`` accounting (per-slice local cap
   default 3; pipeline-global cap default 10) — either trip
   escalates HITL.
5. Detects failure-cascades — a 60 s grace window after a slice
   fails, then walks the downstream subtree and marks each
   transitive descendant ``BLOCKED_ON_FAILED_DEPENDENCY``. One
   ``OVERSEER_ALERT`` is emitted with the full blocked subtree.

The public ``teardown_slice`` / ``respawn_slice`` /
``get_slice_status`` helpers expose slice-addressable hooks so the
follow-up MCP control-verb work (#2199) can wrap them as
``restart_slice`` / ``get_slice_status`` / ``list_slices`` without
refactoring the scheduler internals.

This module is intentionally pure-Python (no I/O, no gateway calls)
so its behaviour is deterministic in unit tests. The orchestrator's
implement-phase run loop wires it up to the real container-spawn
machinery in :mod:`orchestrator.concurrent_executor` and the BRC
tracker layer in :mod:`orchestrator.peer_consensus`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from egg_contracts.dependency_graph import DependencyGraph
from egg_contracts.models import Contract, Slice

if TYPE_CHECKING:
    pass


def _resolve_default(name: str, fallback: int | float) -> int | float:
    """Resolve a config default by importing the env-var helper lazily.

    The slice scheduler is unit-tested without the full orchestrator
    import surface, so we import ``orchestrator.env_config`` lazily
    and fall back to the literal default if the import fails (e.g.
    in test fixtures that don't pull the orchestrator package).
    """
    try:
        from orchestrator import env_config

        helper = getattr(env_config, name, None)
        if callable(helper):
            return helper()
    except Exception:  # noqa: BLE001
        pass
    return fallback


class SchedulerSliceState(StrEnum):
    """Slice lifecycle states tracked by the scheduler.

    Distinct from :class:`SliceStatus` (the contract-level field):
    that field tracks declarative state (pending/in_progress/etc.);
    this enum tracks the scheduler's runtime view, including the
    failure-cascade transitions that don't appear on the contract.
    """

    READY = "ready"  # Dependencies satisfied; eligible to spawn
    RUNNING = "running"  # Agent team spawned; BRC underway
    COMPLETE = "complete"  # CONSENSUS_CONFIRMED received
    FAILED = "failed"  # max_cycles tripped or HITL-escalated
    BLOCKED_ON_FAILED_DEPENDENCY = "blocked_on_failed_dependency"
    PENDING = "pending"  # Dependencies not yet satisfied
    TEARDOWN = "teardown"  # ``teardown_slice`` invoked; pending respawn


@dataclass
class SliceRuntime:
    """Per-slice scheduler state.

    Mutated by the scheduler as slices progress through their
    lifecycle. The field set is deliberately narrow — anything that
    needs to survive across orchestrator restarts is on the
    contract; this struct holds the in-memory bookkeeping.
    """

    slice_id: str
    parent_slice_id: str | None
    state: SchedulerSliceState = SchedulerSliceState.PENDING
    local_cycles: int = 0
    failed_at: float | None = None
    cascade_due_at: float | None = None
    spawned_at: float | None = None
    completed_at: float | None = None


@dataclass
class CascadeEvent:
    """Snapshot of a failure-cascade firing.

    Returned by :meth:`SliceScheduler.poll_cascades` and consumed by
    the orchestrator's run loop to (a) mark the downstream subtree
    on the contract and (b) emit a single ``OVERSEER_ALERT`` with
    anomaly type ``slice-cascade-block``.
    """

    failed_slice_id: str
    blocked_subtree: list[str] = field(default_factory=list)
    fired_at: float = 0.0


class SliceScheduler:
    """Wave-based scheduler for the implement-phase slice DAG.

    The class is instantiated once per pipeline. The orchestrator's
    implement-phase run loop calls :meth:`iter_ready` on each tick
    to harvest slices whose dependencies are satisfied (capped at
    ``max_parallel_slices`` concurrently in flight), spawns their
    container teams, and feeds completion / failure events back via
    :meth:`record_complete` / :meth:`record_failure`. Failure-cascade
    detection is polled via :meth:`poll_cascades`.

    Thread-safety: every public method acquires the internal lock,
    so callers may invoke them from arbitrary threads (the BRC
    tracker, the cascade poller, and the run loop all live in
    different threads).
    """

    def __init__(
        self,
        contract: Contract,
        *,
        max_parallel_slices: int | None = None,
        local_max_cycles: int | None = None,
        global_max_cycles: int | None = None,
        failure_grace_seconds: float | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        hitl_escalator: Callable[[str, str], None] | None = None,
    ) -> None:
        # Resolve env-var defaults lazily so callers that pass
        # explicit values keep their behaviour unchanged, but a bare
        # ``SliceScheduler(contract)`` picks up the operator's
        # ``EGG_ORCH_*`` overrides from ``orchestrator/env_config.py``.
        if max_parallel_slices is None:
            max_parallel_slices = int(_resolve_default("get_max_parallel_slices", 2))
        if local_max_cycles is None:
            local_max_cycles = int(_resolve_default("get_slice_local_max_cycles", 3))
        if global_max_cycles is None:
            global_max_cycles = int(_resolve_default("get_slice_global_max_cycles", 10))
        if failure_grace_seconds is None:
            failure_grace_seconds = float(_resolve_default("get_slice_failure_grace_seconds", 60.0))

        self._contract = contract
        self._max_parallel_slices = max(1, int(max_parallel_slices))
        self._local_max_cycles = max(1, int(local_max_cycles))
        self._global_max_cycles = max(1, int(global_max_cycles))
        self._failure_grace_seconds = max(0.0, float(failure_grace_seconds))
        self._time_fn = time_fn
        self._hitl_escalator = hitl_escalator
        self._lock = threading.RLock()

        self._graph: DependencyGraph[str] = DependencyGraph()
        self._runtimes: dict[str, SliceRuntime] = {}
        self._wave_index: dict[str, int] = {}
        self._global_cycles: int = 0
        # Cascades waiting to fire — keyed on the failed slice id; the
        # value is the wall-clock time at which they expire. Populated
        # by ``record_failure`` and drained by ``poll_cascades``.
        self._pending_cascades: dict[str, float] = {}
        self._fired_cascades: set[str] = set()

        # Defense-in-depth: revalidate the forest constraint at
        # scheduler-construction time so contracts that bypassed
        # ``_populate_contract_from_plan`` (legacy state-branch
        # restores, manual ``egg-contract`` edits) still surface a
        # clear error before the run loop spins on a multi-parent or
        # cyclic DAG. ``validate_forest`` returns structured strings;
        # we raise ``ValueError`` so the run loop's caller sees the
        # message and can route it to HITL / OVERSEER_ALERT.
        try:
            from egg_contracts.plan_parser import validate_forest as _validate_forest
        except ImportError:  # pragma: no cover — module-import shim
            _validate_forest = None  # type: ignore[assignment]

        if _validate_forest is not None:
            forest_errors = _validate_forest(list(self._contract.slices))
            if forest_errors:
                raise ValueError(
                    "SliceScheduler refused to start: contract slice DAG is not a forest "
                    "(see plan_parser.validate_forest). Errors: " + "; ".join(forest_errors)
                )

        self._build_graph()
        self._compute_initial_states()

    # ---------- Construction helpers ----------

    def _build_graph(self) -> None:
        """Populate the dependency graph from the contract's slice list."""
        slices_by_id: dict[str, Slice] = {}
        for slice_ in self._contract.slices:
            slices_by_id[slice_.id] = slice_
            self._graph.add_node(slice_.id)
        for slice_ in self._contract.slices:
            for dep in slice_.dependencies or []:
                if dep in slices_by_id:
                    self._graph.add_edge(slice_.id, dep)
                # Unknown deps are silently dropped; the forest
                # validator catches them at ingestion time.

        # Build a map slice_id → wave for ``parent_slice_id`` lookups.
        if self._graph.nodes:
            try:
                waves = self._graph.compute_waves()
            except ValueError:
                waves = []
            for wave_idx, wave_nodes in enumerate(waves):
                for node_id in wave_nodes:
                    self._wave_index[node_id] = wave_idx

    def _compute_initial_states(self) -> None:
        """Initialise per-slice runtime state.

        Slices with no dependencies start in READY; everything else
        is PENDING. The ``parent_slice_id`` is the unique parent in
        the forest (or ``None`` for roots).
        """
        for slice_ in self._contract.slices:
            deps = slice_.dependencies or []
            parent = deps[0] if deps else None
            initial_state = SchedulerSliceState.READY if not deps else SchedulerSliceState.PENDING
            self._runtimes[slice_.id] = SliceRuntime(
                slice_id=slice_.id,
                parent_slice_id=parent,
                state=initial_state,
            )

    # ---------- Public scheduling API ----------

    def iter_ready(self) -> Iterator[tuple[str, str | None]]:
        """Yield ``(slice_id, parent_slice_id)`` for each ready slice.

        Respects the parallel-slice cap: stops yielding once the
        already-RUNNING count plus the count yielded in *this*
        iteration reaches ``max_parallel_slices``. Caller is expected
        to invoke :meth:`mark_spawned` for each yielded slice before
        the next ``iter_ready`` call — that flips the runtime state
        so the next tick sees the same slot under the cap. Yielding
        without spawning is safe (the next tick will re-yield) but
        will hold the slot budget across both ticks.
        """
        # Snapshot eligible slices under the lock; yield outside the
        # lock so the generator doesn't hold it while the caller does
        # blocking I/O. The slot budget counts both already-running
        # slices and slices we've just yielded in this iteration.
        with self._lock:
            in_flight = sum(
                1 for rt in self._runtimes.values() if rt.state == SchedulerSliceState.RUNNING
            )
            available = self._max_parallel_slices - in_flight
            if available <= 0:
                return
            ready_snapshot = [
                (rt.slice_id, rt.parent_slice_id)
                for rt in self._runtimes.values()
                if rt.state == SchedulerSliceState.READY
            ]
        # Yield up to ``available`` entries from the snapshot. The
        # snapshot is stable for the duration of the iteration —
        # callers that ``mark_spawned`` mid-iteration won't see their
        # state change reflected in this generator, which is the
        # desired semantic (one full sweep per tick).
        yield from ready_snapshot[:available]

    def mark_spawned(self, slice_id: str) -> None:
        """Record that the slice's agent team has been spawned."""
        with self._lock:
            runtime = self._runtimes.get(slice_id)
            if runtime is None:
                return
            runtime.state = SchedulerSliceState.RUNNING
            runtime.spawned_at = self._time_fn()

    def record_cycle(self, slice_id: str) -> bool:
        """Record a BRC re-proposal cycle on the slice.

        Returns ``True`` when EITHER the local-per-slice cap or the
        global pipeline cap has been tripped. Caller is expected to
        treat that as a HITL escalation trigger.

        The HITL escalator (when configured) is invoked AFTER the
        scheduler lock is released — the escalator may issue HTTP /
        contract-write I/O whose latency would otherwise serialise
        every other scheduler operation, and a >180 s round-trip
        would even trip the orchestrator's stuck-phase-transition
        timeout. (Concurrency reviewer's blocker #1 on v1; #2012
        precedent.)
        """
        escalation_args: tuple[str, str] | None = None
        tripped = False
        with self._lock:
            runtime = self._runtimes.get(slice_id)
            if runtime is None:
                return False
            runtime.local_cycles += 1
            self._global_cycles += 1
            tripped = (
                runtime.local_cycles >= self._local_max_cycles
                or self._global_cycles >= self._global_max_cycles
            )
            if tripped:
                reason = (
                    f"slice {slice_id} hit local cap "
                    f"({runtime.local_cycles}/{self._local_max_cycles})"
                    if runtime.local_cycles >= self._local_max_cycles
                    else (
                        f"pipeline hit global cap ({self._global_cycles}/{self._global_max_cycles})"
                    )
                )
                escalation_args = (slice_id, reason)

        if escalation_args is not None and self._hitl_escalator is not None:
            try:
                self._hitl_escalator(*escalation_args)
            except Exception:  # noqa: BLE001
                # Escalation failures are non-fatal — better to
                # surface them as an alert than crash the loop.
                pass
        return tripped

    def record_complete(self, slice_id: str) -> None:
        """Record that the slice has reached CONSENSUS_CONFIRMED."""
        with self._lock:
            runtime = self._runtimes.get(slice_id)
            if runtime is None:
                return
            runtime.state = SchedulerSliceState.COMPLETE
            runtime.completed_at = self._time_fn()
            self._unblock_children(slice_id)

    def record_failure(self, slice_id: str) -> None:
        """Record that the slice has failed.

        Marks the slice FAILED and arms the failure-cascade timer.
        The 60 s grace window (configurable) gives HITL a chance to
        resolve before the downstream subtree is locked out.
        Siblings continue to run — only the descendants of the
        failed slice are affected.
        """
        with self._lock:
            runtime = self._runtimes.get(slice_id)
            if runtime is None:
                return
            runtime.state = SchedulerSliceState.FAILED
            now = self._time_fn()
            runtime.failed_at = now
            runtime.cascade_due_at = now + self._failure_grace_seconds
            self._pending_cascades[slice_id] = runtime.cascade_due_at

    def cancel_cascade(self, slice_id: str) -> None:
        """Cancel an armed cascade — usually after HITL resolves the failure."""
        with self._lock:
            self._pending_cascades.pop(slice_id, None)

    def poll_cascades(self) -> list[CascadeEvent]:
        """Fire any cascades whose grace window has expired.

        Returns one :class:`CascadeEvent` per cascade. Caller is
        responsible for emitting the resulting OVERSEER_ALERT and
        updating the contract's slice statuses.
        """
        events: list[CascadeEvent] = []
        with self._lock:
            now = self._time_fn()
            ripe = [
                slice_id
                for slice_id, due_at in list(self._pending_cascades.items())
                if due_at <= now and slice_id not in self._fired_cascades
            ]
            for slice_id in ripe:
                blocked = self._compute_downstream(slice_id)
                for descendant in blocked:
                    desc_runtime = self._runtimes.get(descendant)
                    if desc_runtime is not None and desc_runtime.state in {
                        SchedulerSliceState.PENDING,
                        SchedulerSliceState.READY,
                    }:
                        desc_runtime.state = SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY
                events.append(
                    CascadeEvent(
                        failed_slice_id=slice_id,
                        blocked_subtree=blocked,
                        fired_at=now,
                    )
                )
                self._fired_cascades.add(slice_id)
                self._pending_cascades.pop(slice_id, None)
        return events

    # ---------- Slice-addressable hooks (#2199 follow-up surface) ----------

    def teardown_slice(self, slice_id: str) -> bool:
        """Mark the slice as torn down.

        Public hook so the per-slice MCP control work (#2199) can
        request a clean teardown of a slice's agent team without
        reaching into scheduler internals. The caller is responsible
        for actually killing the containers and tracker — this just
        flips the runtime state so the next ``iter_ready`` skip the
        slice and ``respawn_slice`` knows where to pick up.
        """
        with self._lock:
            runtime = self._runtimes.get(slice_id)
            if runtime is None:
                return False
            runtime.state = SchedulerSliceState.TEARDOWN
            return True

    def respawn_slice(self, slice_id: str) -> bool:
        """Reset the slice to READY (with cycles preserved).

        Mirror of ``teardown_slice`` — the per-slice MCP follow-up
        will use this to ``restart_slice`` after teardown. Cycles are
        kept so the local cap still bounds repeated restarts.
        """
        with self._lock:
            runtime = self._runtimes.get(slice_id)
            if runtime is None:
                return False
            # Only respawn from a TEARDOWN/FAILED state — refusing to
            # respawn an already-RUNNING slice prevents accidental
            # double-spawn races.
            if runtime.state not in {
                SchedulerSliceState.TEARDOWN,
                SchedulerSliceState.FAILED,
                SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY,
            }:
                return False
            runtime.state = SchedulerSliceState.READY
            runtime.failed_at = None
            runtime.cascade_due_at = None
            self._pending_cascades.pop(slice_id, None)
            self._fired_cascades.discard(slice_id)
            return True

    def get_slice_status(self, slice_id: str) -> SliceRuntime | None:
        """Return the per-slice runtime view, or ``None`` if unknown."""
        with self._lock:
            runtime = self._runtimes.get(slice_id)
            if runtime is None:
                return None
            # Return a copy so external callers can't mutate state.
            return SliceRuntime(
                slice_id=runtime.slice_id,
                parent_slice_id=runtime.parent_slice_id,
                state=runtime.state,
                local_cycles=runtime.local_cycles,
                failed_at=runtime.failed_at,
                cascade_due_at=runtime.cascade_due_at,
                spawned_at=runtime.spawned_at,
                completed_at=runtime.completed_at,
            )

    def list_slices(self) -> list[SliceRuntime]:
        """Return runtime snapshots for every slice in declared order."""
        with self._lock:
            return [
                SliceRuntime(
                    slice_id=rt.slice_id,
                    parent_slice_id=rt.parent_slice_id,
                    state=rt.state,
                    local_cycles=rt.local_cycles,
                    failed_at=rt.failed_at,
                    cascade_due_at=rt.cascade_due_at,
                    spawned_at=rt.spawned_at,
                    completed_at=rt.completed_at,
                )
                for rt in self._runtimes.values()
            ]

    # ---------- Read-only introspection ----------

    @property
    def global_cycles(self) -> int:
        """Total summed BRC cycles across every slice in this pipeline."""
        with self._lock:
            return self._global_cycles

    @property
    def max_parallel_slices(self) -> int:
        """Resolved parallel-slice cap (config + env var)."""
        return self._max_parallel_slices

    @property
    def local_max_cycles(self) -> int:
        return self._local_max_cycles

    @property
    def global_max_cycles(self) -> int:
        return self._global_max_cycles

    @property
    def failure_grace_seconds(self) -> float:
        return self._failure_grace_seconds

    def all_done(self) -> bool:
        """True when every slice has reached a terminal state."""
        terminal = {
            SchedulerSliceState.COMPLETE,
            SchedulerSliceState.FAILED,
            SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY,
        }
        with self._lock:
            return all(rt.state in terminal for rt in self._runtimes.values())

    # ---------- Internal helpers ----------

    def _unblock_children(self, parent_slice_id: str) -> None:
        """Promote PENDING / BLOCKED children of a completed slice to READY.

        Caller must hold ``self._lock``.

        Includes ``BLOCKED_ON_FAILED_DEPENDENCY`` children so the
        cascade-then-respawn-then-complete recovery path lights up:
        once a previously-failed parent is respawned and ultimately
        completes, its descendants (which the prior cascade marked
        BLOCKED) are promoted back to READY. Without this branch the
        downstream subtree stays permanently blocked even though its
        parent has finished — see concurrency reviewer's blocker #2
        on v1.
        """
        unblockable_states = {
            SchedulerSliceState.PENDING,
            SchedulerSliceState.BLOCKED_ON_FAILED_DEPENDENCY,
        }
        for runtime in self._runtimes.values():
            if runtime.state not in unblockable_states:
                continue
            if runtime.parent_slice_id != parent_slice_id:
                continue
            runtime.state = SchedulerSliceState.READY

    def _compute_downstream(self, slice_id: str) -> list[str]:
        """Return the transitive downstream subtree of ``slice_id``.

        Excludes the failed slice itself; ordered by traversal so
        callers see ancestors before descendants. Caller must hold
        ``self._lock``.
        """
        if slice_id not in self._graph.nodes:
            return []
        visited: set[str] = set()
        order: list[str] = []
        # BFS over ``dependents`` (children).
        queue: list[str] = [slice_id]
        while queue:
            current = queue.pop(0)
            node = self._graph.nodes.get(current)
            if node is None:
                continue
            for dependent in node.dependents:
                if dependent in visited:
                    continue
                visited.add(dependent)
                order.append(dependent)
                queue.append(dependent)
        return order


__all__ = (
    "CascadeEvent",
    "SchedulerSliceState",
    "SliceRuntime",
    "SliceScheduler",
)

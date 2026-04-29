"""Tests for ``orchestrator.global_slice_admit`` (#2241 gap 1).

Covers the process-singleton admission counter that bounds slice
spawns across all running pipelines, distinct from the per-pipeline
``EGG_ORCH_MAX_PARALLEL_SLICES`` cap enforced inside
``SliceScheduler``.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
for _p in (_orchestrator_path,):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import global_slice_admit  # noqa: E402


def setup_function() -> None:
    global_slice_admit.reset_for_testing(cap=4)


def teardown_function() -> None:
    global_slice_admit.reset_for_testing()


def test_admits_up_to_cap() -> None:
    global_slice_admit.reset_for_testing(cap=2)
    assert global_slice_admit.try_admit("p1", "slice-1") is True
    assert global_slice_admit.try_admit("p1", "slice-2") is True
    # Saturated
    assert global_slice_admit.try_admit("p1", "slice-3") is False
    assert global_slice_admit.try_admit("p2", "slice-1") is False


def test_release_frees_slot_for_other_pipeline() -> None:
    global_slice_admit.reset_for_testing(cap=1)
    assert global_slice_admit.try_admit("p1", "slice-1") is True
    assert global_slice_admit.try_admit("p2", "slice-1") is False
    global_slice_admit.release("p1", "slice-1")
    assert global_slice_admit.try_admit("p2", "slice-1") is True


def test_admit_is_idempotent_on_same_key() -> None:
    """Re-admitting an already-admitted key returns True without consuming a second slot.

    Mirrors the scheduler's "yielding without spawning is safe"
    contract — a slice that gets admitted, defers spawn for some
    reason, and re-admits next tick must not double-count.
    """
    global_slice_admit.reset_for_testing(cap=1)
    assert global_slice_admit.try_admit("p1", "slice-1") is True
    assert global_slice_admit.try_admit("p1", "slice-1") is True
    snap = global_slice_admit.snapshot()
    assert snap["admitted"] == 1


def test_release_is_idempotent() -> None:
    global_slice_admit.reset_for_testing(cap=2)
    global_slice_admit.try_admit("p1", "slice-1")
    global_slice_admit.release("p1", "slice-1")
    # Second release is a no-op (must not over-release into negatives).
    global_slice_admit.release("p1", "slice-1")
    snap = global_slice_admit.snapshot()
    assert snap["admitted"] == 0
    # Cap still has room for new admissions.
    assert global_slice_admit.try_admit("p2", "slice-1") is True


def test_release_without_admit_is_safe() -> None:
    global_slice_admit.reset_for_testing(cap=2)
    # Releasing a key that was never admitted must not raise or
    # under-count the admitted set.
    global_slice_admit.release("p1", "never-admitted")
    snap = global_slice_admit.snapshot()
    assert snap["admitted"] == 0


def test_snapshot_lists_admitted_keys_sorted() -> None:
    global_slice_admit.reset_for_testing(cap=4)
    global_slice_admit.try_admit("p2", "slice-1")
    global_slice_admit.try_admit("p1", "slice-2")
    global_slice_admit.try_admit("p1", "slice-1")
    snap = global_slice_admit.snapshot()
    assert snap["admitted"] == 3
    assert snap["cap"] == 4
    assert snap["admitted_keys"] == ["p1/slice-1", "p1/slice-2", "p2/slice-1"]


def test_env_var_resolution(monkeypatch) -> None:
    """The cap is read from EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES via env_config."""
    global_slice_admit.reset_for_testing()  # clear cap_override so env_config wins
    monkeypatch.setenv("EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES", "1")
    snap = global_slice_admit.snapshot()
    assert snap["cap"] == 1
    assert global_slice_admit.try_admit("p1", "slice-1") is True
    assert global_slice_admit.try_admit("p1", "slice-2") is False


def test_default_cap_is_four() -> None:
    """Default cap matches the operationally observed safe ceiling (#2241)."""
    global_slice_admit.reset_for_testing()  # clear cap_override
    snap = global_slice_admit.snapshot()
    assert snap["cap"] == 4


def test_concurrent_admission_respects_cap() -> None:
    """Concurrent try_admit calls from many threads never exceed the cap."""
    cap = 3
    global_slice_admit.reset_for_testing(cap=cap)
    successes: list[bool] = []
    successes_lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker(idx: int) -> None:
        barrier.wait()
        ok = global_slice_admit.try_admit("p1", f"slice-{idx}")
        with successes_lock:
            successes.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for s in successes if s) == cap
    assert global_slice_admit.snapshot()["admitted"] == cap

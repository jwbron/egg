"""Per-pipeline state locks for atomic load-modify-save cycles (#3312).

Extracted verbatim from the pre-split ``state_store.py``. The barrel
re-exports ``get_pipeline_state_lock`` / ``release_pipeline_state_lock`` and
the ``_pipeline_state_locks`` registry (the test seam at
``state_store._pipeline_state_locks``).
"""

import threading

# Per-pipeline state locks for atomic load-modify-save cycles.
# Prevents race conditions where concurrent writers (e.g. update_pipeline
# and DecisionQueue.resolve_decision) can clobber each other's changes.
_pipeline_state_locks: dict[str, threading.RLock] = {}
_state_locks_lock = threading.Lock()


def get_pipeline_state_lock(pipeline_id: str) -> threading.RLock:
    """Get a per-pipeline lock for coordinating state access.

    All code that does a load-modify-save cycle on pipeline state
    should acquire this lock to prevent concurrent writes from
    overwriting each other.  The lock is reentrant (RLock) so
    nested acquisitions within the same thread are safe.

    Args:
        pipeline_id: Pipeline ID

    Returns:
        RLock for the given pipeline
    """
    with _state_locks_lock:
        if pipeline_id not in _pipeline_state_locks:
            _pipeline_state_locks[pipeline_id] = threading.RLock()
        return _pipeline_state_locks[pipeline_id]


def release_pipeline_state_lock(pipeline_id: str) -> None:
    """Remove the per-pipeline lock when a pipeline is deleted.

    Call this after deleting a pipeline to prevent unbounded growth
    of ``_pipeline_state_locks``.  Safe to call even if no lock exists
    for the given pipeline ID.

    Precondition: the lock must not be currently held by any thread.
    If it is, the lock is left in place to avoid breaking mutual
    exclusion for threads still referencing the old lock object.

    Args:
        pipeline_id: Pipeline ID whose lock should be discarded
    """
    with _state_locks_lock:
        lock = _pipeline_state_locks.get(pipeline_id)
        if lock is None:
            return
        # Only remove if the lock is not currently held.  A held lock
        # means another thread is mid-operation; removing it would cause
        # new callers to get a fresh lock, breaking mutual exclusion.
        # RLock has no .locked() method, so we try a non-blocking acquire.
        if lock.acquire(blocking=False):
            lock.release()
            _pipeline_state_locks.pop(pipeline_id, None)

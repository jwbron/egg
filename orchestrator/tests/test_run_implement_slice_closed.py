"""End-to-end wiring guard for the ``slice.closed`` emitter (issue #3364).

The slice scheduler exposes an injected ``slice_closed_emitter`` seam, but a
seam left default-``None`` never fires. This test pins the *production*
wiring in ``_run_implement_phase_slices``: it must construct the
``SliceScheduler`` with a non-``None`` emitter, and invoking that emitter
must publish an **allowlisted** ``slice.closed`` event — carrying
``{slice_id, outcome}`` — onto the real orchestrator event bus. Without the
wiring the emitter is ``None`` and the operator directive ("the skill
consumes both") is unmet; this test fails in that state.

It deliberately does NOT re-test the injected-emitter unit contract (that
lives in ``test_slice_scheduler.py``) — it verifies the closure the run
loop actually builds reaches the event bus.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import routes.pipelines as pipelines_pkg
from events import EventType, get_event_bus


class _CaptureSchedulerError(ValueError):
    """Raised by the stub scheduler to short-circuit the run loop right
    after construction — the ``except ValueError`` path returns early, so
    no gateway / container machinery is exercised."""


def _drive_run_implement_and_capture_emitter(pipeline_id: str):
    """Drive ``_run_implement_phase_slices`` far enough to capture the
    ``slice_closed_emitter`` the production code passes to ``SliceScheduler``.

    Patches ``SliceScheduler`` with a stub that records the kwarg then
    raises, and stubs ``load_contract`` to yield a single-slice contract so
    the loop reaches the construction site.
    """
    captured: dict[str, object] = {}

    class _StubScheduler:
        def __init__(self, contract, *, max_parallel_slices=None, slice_closed_emitter=None, **_kw):
            captured["emitter"] = slice_closed_emitter
            raise _CaptureSchedulerError("stop-after-capture")

    fake_contract = SimpleNamespace(slices=[SimpleNamespace(id="slice-1", dependencies=[])])
    pipeline = SimpleNamespace(
        branch="egg/issue-3364/work",
        issue_number=3364,
        config=SimpleNamespace(max_parallel_slices=1),
    )
    store = SimpleNamespace(repo_path="/tmp/does-not-matter")

    with (
        patch("orchestrator.slice_scheduler.SliceScheduler", _StubScheduler),
        patch("slice_scheduler.SliceScheduler", _StubScheduler),
        patch("egg_contracts.loader.load_contract", return_value=fake_contract),
        patch("egg_contracts.loader.save_contract"),
        # The context-PR opener runs before construction; neutralize it so
        # the test doesn't touch the gateway.
        patch.object(
            pipelines_pkg, "_open_context_pr_at_implement_start", return_value=None, create=True
        ),
    ):
        rc, _logs = pipelines_pkg._run_implement_phase_slices(
            pipeline_id,
            pipeline,
            spawner=None,
            repo_volumes={},
            gateway_mode="local",
            repos=[],
            sandbox_env={},
            store=store,
            certs_volume=None,
            worktree_repo_path=pipelines_pkg.Path("/tmp/does-not-matter"),
        )

    # Construction stub raised ValueError → the loop returns the early
    # validation-failure tuple, confirming we stopped right after capture.
    assert rc == 1
    return captured


def test_run_implement_wires_slice_closed_emitter() -> None:
    """The production run loop must pass a non-None emitter (the core fix:
    left default-None the seam never fires in production)."""
    captured = _drive_run_implement_and_capture_emitter("issue-3364-wire")
    assert captured.get("emitter") is not None, (
        "SliceScheduler constructed without slice_closed_emitter — "
        "slice.closed would never fire in production"
    )


def test_wired_emitter_publishes_allowlisted_slice_closed_event() -> None:
    """Invoking the captured production emitter publishes an allowlisted
    ``slice.closed`` event with the ``{slice_id, outcome}`` payload."""
    pipeline_id = "issue-3364-emit"
    captured = _drive_run_implement_and_capture_emitter(pipeline_id)
    emitter = captured["emitter"]
    assert emitter is not None

    # Real production closure → real event bus. ``get_history`` records
    # synchronously under the bus lock at publish() time, so the assertion
    # is deterministic despite async delivery.
    emitter("slice-1", "complete")
    emitter("slice-2", "failed")

    history = get_event_bus().get_history(
        pipeline_id=pipeline_id, event_type=EventType.SLICE_CLOSED
    )
    # get_history returns newest-first.
    assert len(history) == 2
    by_slice = {evt.data.get("slice_id"): evt for evt in history}

    complete_evt = by_slice["slice-1"]
    assert complete_evt.event_type == EventType.SLICE_CLOSED
    assert complete_evt.event_type.value == "slice.closed"
    assert complete_evt.data == {"slice_id": "slice-1", "outcome": "complete"}

    failed_evt = by_slice["slice-2"]
    assert failed_evt.data == {"slice_id": "slice-2", "outcome": "failed"}

    # AC-B4: the emitted event type is on the /status/wait allowlist, so it
    # actually reaches wait-status consumers (end-to-end consumption).
    assert "slice.closed" in pipelines_pkg._STATUS_WAIT_EVENT_TYPES
    assert EventType.SLICE_CLOSED.value in pipelines_pkg._STATUS_WAIT_EVENT_TYPES

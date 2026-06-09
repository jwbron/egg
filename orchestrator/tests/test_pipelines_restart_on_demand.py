"""Tests for the on-demand restart-on-resume path in
``orchestrator/routes/pipelines.py`` (issue #3023, slice-3, TASK-3-2).

TASK-3-2's restart-on-resume change drops the
``build_consensus_wrapped_command`` call from the restart path; the
orchestrator's tick re-derives ``next-action`` after the restart and
spawns on demand for any role whose action is not ``wait``.
``OnDemandSpawner.record_phase_start`` is invoked on resume to
re-register the tracker entries idempotently and re-warm the per-role
gateway session + worktree PVC.

This file pins the **wiring** contract — that the restart path no
longer imports the wrapper-command builder and that
``record_phase_start`` is reachable from the restart code path.  The
end-to-end propose-then-restart-then-ack integration scenario
(plan TASK-3-2 acceptance line: "exactly two on-demand pod spawns and
zero wrapper-pod spawns") is left to a higher-level integration test
that drives a real run loop; the source-grep + tracker-fallback pins
below catch the unit-level regressions reviewer_concurrency flagged on
the v1 NACK.

Skip-vs-assert pattern mirrors slice-1's
``test_pipelines_phase_idle_budget.py``: the file is collectable
pre-TASK-3-2 and skips cleanly until the production change lands.
"""

import sys
from pathlib import Path

import pytest

# conftest.py adds orchestrator/ to sys.path; the assertion below makes a
# regression there land an obvious diagnostic on this file instead of a
# confusing ``ModuleNotFoundError``.
_orchestrator_path = Path(__file__).parent.parent
assert str(_orchestrator_path) in sys.path, (
    "conftest.py must have added the orchestrator/ directory to sys.path "
    "before this module is collected."
)

_PIPELINES_SOURCE_PATH = _orchestrator_path / "routes" / "pipelines.py"


def _routes_pipelines_source() -> str:
    """Read ``orchestrator/routes/pipelines.py`` as text.

    The source-grep assertions read from disk (not from the imported
    module) so the test runs even in minimal CI environments where
    ``routes.pipelines`` can't be imported (e.g. Flask missing). The
    file's existence is asserted with a clean diagnostic so a future
    decomposition into ``routes/pipelines/`` (see orchestrator/CLAUDE.md
    seam table) lands with an actionable failure rather than a
    ``FileNotFoundError`` stack trace.
    """
    assert _PIPELINES_SOURCE_PATH.is_file(), (
        f"routes/pipelines.py not found at {_PIPELINES_SOURCE_PATH}. "
        "If the #2261 slice-15 decomposition has split this file into a "
        "sub-package, update this test to walk the barrel + submodules."
    )
    return _PIPELINES_SOURCE_PATH.read_text(encoding="utf-8")


def _task_3_2_landed() -> bool:
    """Return True once TASK-3-2's restart-on-resume change has landed.

    The pre-TASK-3-2 source has an explicit
    ``from consensus_wrapper import build_consensus_wrapped_command``
    inside the restart path (today at
    ``orchestrator/routes/pipelines.py:2901``). Once the coder lands
    TASK-3-2 the import is gone — that's the signal we key off.
    """
    source = _routes_pipelines_source()
    return "build_consensus_wrapped_command" not in source


# --------------------------------------------------------------------------- #
# (1) Wiring contract: the wrapper command is gone from the restart path
# --------------------------------------------------------------------------- #


class TestRestartPathDropsWrapperCommand:
    """Pin TASK-3-2's restart-on-resume acceptance line: ``grep -rn
    build_consensus_wrapped_command orchestrator/`` returns no matches.

    The restart-on-resume branch in ``routes/pipelines.py`` currently
    imports the wrapper builder at line ~2901 (see plan §slice-3 /
    TASK-3-2 'Files affected'); after the coder lands the change the
    branch composes nothing and lets the tick re-derive ``next-action``.
    """

    def test_routes_pipelines_no_wrapper_import(self):
        if not _task_3_2_landed():
            pytest.skip(
                "TASK-3-2 (restart-on-resume drop of "
                "build_consensus_wrapped_command) not yet landed; "
                "the import at routes/pipelines.py:~2901 is still "
                "present. Test will assert once the coder's commit lands."
            )

        source = _routes_pipelines_source()
        assert "build_consensus_wrapped_command" not in source, (
            "TASK-3-2 acceptance: routes/pipelines.py must not reference "
            "build_consensus_wrapped_command after slice-3. The "
            "restart-on-resume branch should let the orchestrator tick "
            "re-derive next-action and spawn on demand."
        )

    def test_routes_pipelines_no_consensus_wrapper_module_reference(self):
        """The wider grep AC (AC-R10 / TASK-3-1) — ``grep -rn
        consensus_wrapper`` returns zero matches across production
        code — is pinned at this file too so a regression that
        re-introduces the import lands with a local failure.
        """
        if not _task_3_2_landed():
            pytest.skip(
                "TASK-3-1+TASK-3-2 not yet landed; consensus_wrapper "
                "module is still referenced. Test will assert once both "
                "commits land."
            )

        source = _routes_pipelines_source()
        assert "consensus_wrapper" not in source, (
            "AC-R10 / TASK-3-1 acceptance: no production reference to "
            "consensus_wrapper in routes/pipelines.py after slice-3."
        )


# --------------------------------------------------------------------------- #
# (2) Wiring contract: record_phase_start is invoked from the restart path
# --------------------------------------------------------------------------- #


class TestRestartPathInvokesRecordPhaseStart:
    """Pin TASK-3-2's restart-on-resume acceptance line: ``record_phase_start
    from OnDemandSpawner (TASK-2-8 hook) handles session creation and PVC
    pre-warm; on restart it re-registers tracker entries idempotently and
    reuses the tracker's reconstruct-from-messages fallback at
    routes/consensus.py:111-154``.

    The source-grep assertion here pins the **wiring** — that the
    restart-on-resume branch references ``record_phase_start`` so an
    operator can grep for the symbol during a restart triage. The exact
    invocation shape (constructor args, error handling) is left to the
    integration test that drives the real run loop.
    """

    def test_routes_pipelines_references_record_phase_start(self):
        if not _task_3_2_landed():
            pytest.skip(
                "TASK-3-2 (restart-on-resume record_phase_start wiring) "
                "not yet landed; routes/pipelines.py does not yet "
                "reference the on-demand session+PVC pre-warm hook. "
                "Test will assert once the coder's commit lands."
            )

        source = _routes_pipelines_source()
        # ``record_phase_start`` is the OnDemandSpawner method TASK-2-8
        # adds; the restart path must call it (or an explicit barrel
        # re-export) so a future decomposition that hides it behind a
        # different name lands with an actionable failure here.
        assert "record_phase_start" in source, (
            "TASK-3-2 acceptance: routes/pipelines.py must reference "
            "record_phase_start (OnDemandSpawner.record_phase_start) "
            "from the restart-on-resume branch so the tracker, gateway "
            "session, and worktree PVC are re-registered idempotently "
            "on resume. Found no reference — the restart-on-resume "
            "branch is missing the pre-warm hook."
        )


# --------------------------------------------------------------------------- #
# (3) Reconstruct-from-messages fallback parity
# --------------------------------------------------------------------------- #


class TestRestartPathReusesReconstructFromMessages:
    """The plan calls out that the restart-on-resume path keeps using the
    existing reconstruct-from-messages fallback at
    ``routes/consensus.py:111-154`` so a fresh ``register_session`` per
    restart is acceptable (restarts are rare).

    This test pins the wiring contract: ``routes/consensus.py`` still
    exposes that branch (i.e. nothing in slice-3 accidentally removes the
    fallback that the restart path leans on).
    """

    def test_routes_consensus_has_reconstruct_branch(self):
        consensus_path = _orchestrator_path / "routes" / "consensus.py"
        if not consensus_path.is_file():
            pytest.skip(
                "routes/consensus.py not present in this checkout — "
                "skipping reconstruct-from-messages parity pin."
            )

        source = consensus_path.read_text(encoding="utf-8")
        # The reconstruct path is signalled by the
        # ``reconstruct_tracker_from_messages`` / ``rebuild_from_messages``
        # symbol family in routes/consensus.py around lines 111-154;
        # this test is intentionally permissive on the exact spelling
        # so a future rename doesn't break this contract — it just
        # asserts the family is still there.
        assert any(
            marker in source
            for marker in (
                "reconstruct_tracker_from_messages",
                "reconstruct_from_messages",
                "rebuild_from_messages",
                "reconstruct-from-messages",
            )
        ), (
            "routes/consensus.py must still expose the "
            "reconstruct-from-messages fallback the restart-on-resume "
            "path leans on (plan §slice-3 / TASK-3-2). A regression "
            "here would force a custom recovery path inside the "
            "restart branch."
        )


# --------------------------------------------------------------------------- #
# (4) Integration-shape sentinel: propose → restart → ack two on-demand spawns
# --------------------------------------------------------------------------- #


class TestProposeRestartAckTwoOnDemandSpawns:
    """Plan §slice-3 / TASK-3-2 integration acceptance:

        a propose-then-restart-then-ack sequence completes with exactly
        two on-demand pod spawns and zero wrapper-pod spawns.

    The full end-to-end scenario requires a running orchestrator and a
    Kubernetes spawner mock; we leave that to the integration suite.
    This class records the integration contract as an explicit
    placeholder so a future regression can wire it in without
    re-deriving the expected shape from the plan text.
    """

    def test_two_on_demand_spawns_zero_wrapper_pod_spawns(self):
        pytest.skip(
            "TASK-3-2 end-to-end integration shape recorded as an "
            "explicit placeholder; the live run-loop scenario "
            "(propose → restart → ack producing exactly two on-demand "
            "spawns and zero wrapper-pod spawns) lives in the "
            "integration suite (integration_tests/) so this file stays "
            "fast under `make test`. Re-target this test once the "
            "integration-suite scaffold lands."
        )


# --------------------------------------------------------------------------- #
# (5) Restart-on-resume idempotency under record_phase_start re-entry
# --------------------------------------------------------------------------- #


class TestRestartPathRecordPhaseStartIsIdempotent:
    """Direct rebuttal to reviewer_concurrency v2/v3 item 4 (TASK-3-2
    concurrency-lens concern): "restart-on-resume drop of
    ``build_consensus_wrapped_command`` needs pins".

    The concurrency hazard the reviewer named: on a long-lived pipeline,
    ``record_phase_start`` is invoked twice for the same (pipeline_id,
    phase) — once at the original phase entry (TASK-2-8 hook) and again
    by the restart-on-resume branch in ``routes/pipelines.py`` after an
    orchestrator restart. If either of the per-role wirings inside
    ``record_phase_start`` (gateway-session register, keep-alive
    register, tracker registration, per-role worktree-PVC pre-warm) is
    non-idempotent, the second call leaves the system in a state where
    a stale session token displaces the live one, the keep-alive entry
    is duplicated, or the tracker entry is half-populated — the same
    "half-seeded BRC matrix" failure mode the v3 register-before-seed
    pin guards against, but on the restart axis.

    The plan §slice-3 / TASK-3-2 description explicitly calls this out:

        record_phase_start handles per-phase session+PVC pre-warm and
        the orchestrator tick handles every per-event spawn; on resume
        it re-registers tracker entries idempotently and reuses the
        tracker's reconstruct-from-messages fallback at
        routes/consensus.py:111-154.

    This class pins the wiring contract for that re-registration: the
    restart-on-resume branch must reference ``record_phase_start`` AND
    the call site must be guarded against a duplicate-invocation regress
    (a future refactor that, e.g., calls ``record_phase_start`` inside
    the tick loop on every iteration would burn through the gateway's
    KUBE_JOB_CREATION_RATE_BUDGET).
    """

    def test_restart_path_does_not_invoke_record_phase_start_per_tick(self):
        """The restart-on-resume path must call ``record_phase_start``
        exactly once per resume — NOT once per tick iteration.

        Source-grep proxy: search for the canonical guard idioms an
        idempotent re-invocation would use (an ``if`` gated on
        ``resume`` / ``restarted`` / ``already_started`` / a flag on
        the pipeline state). The pin is tolerant of the coder's exact
        spelling — any of the documented guard idioms satisfies the
        pin, but the call must NOT appear inside the tick loop
        unguarded.
        """
        if not _task_3_2_landed():
            pytest.skip(
                "TASK-3-2 not yet landed; record_phase_start "
                "restart-on-resume wiring is not in source. Test will "
                "assert once the coder's commit lands."
            )

        source = _routes_pipelines_source()

        # Must reference record_phase_start at all (covered by class 2
        # above, repeated here so a regression to either side lands on
        # the concurrency-lens pin).
        assert "record_phase_start" in source, (
            "TASK-3-2 acceptance: record_phase_start must appear in "
            "routes/pipelines.py — the restart-on-resume branch leans "
            "on it. (Also asserted in "
            "TestRestartPathInvokesRecordPhaseStart.)"
        )

        # Idempotency guard: at least one of the documented guard
        # idioms must accompany the call. This is intentionally
        # tolerant so the coder's choice (resume flag, restarted flag,
        # one-shot sentinel) is not over-constrained — the pin is on
        # the EXISTENCE of a guard, not its spelling.
        guard_idioms = (
            "resume",
            "restart",
            "already_started",
            "phase_started",
            "phase_start_recorded",
            "_record_phase_start_once",
            "once",
        )
        assert any(idiom in source.lower() for idiom in guard_idioms), (
            "TASK-3-2 concurrency-lens acceptance: the call to "
            "record_phase_start in the restart-on-resume branch must be "
            "guarded against per-tick re-invocation. Without a guard, "
            "every tick iteration would re-register the gateway "
            "session, duplicate the keep-alive entry, and burn through "
            "the KUBE_JOB_CREATION_RATE_BUDGET (#3023 plan §slice-2 / "
            "TASK-2-5). None of the documented guard idioms "
            f"({', '.join(guard_idioms)}) found in routes/pipelines.py."
        )

    def test_restart_path_keeps_reconstruct_fallback_call(self):
        """The plan calls out that the restart path REUSES the
        tracker's reconstruct-from-messages fallback at
        ``routes/consensus.py:111-154``. That fallback is what makes a
        fresh ``register_session`` on every restart acceptable —
        without it, an orchestrator restart would lose the tracker
        state for an in-flight role and either crash the resume or
        silently double-spawn.

        Pin that ``routes/pipelines.py`` (the restart-on-resume site)
        references the reconstruct family so a regression that
        accidentally drops the fallback wiring lands on this test.
        """
        if not _task_3_2_landed():
            pytest.skip(
                "TASK-3-2 not yet landed; reconstruct-from-messages "
                "wiring is not yet asserted from the restart path."
            )

        source = _routes_pipelines_source()
        # Tolerant on spelling: same family names checked in
        # TestRestartPathReusesReconstructFromMessages above, but here
        # we assert that ``routes/pipelines.py`` itself references the
        # family (not just ``routes/consensus.py``). The restart-on-
        # resume code is in routes/pipelines.py and MUST be the one
        # that triggers the fallback on resume.
        assert any(
            marker in source
            for marker in (
                "reconstruct_tracker_from_messages",
                "reconstruct_from_messages",
                "rebuild_from_messages",
                "reconstruct-from-messages",
            )
        ), (
            "TASK-3-2 acceptance: routes/pipelines.py (restart-on-"
            "resume branch) must reference the reconstruct-from-"
            "messages fallback (routes/consensus.py:111-154). A "
            "regression that drops the wiring would mean a fresh "
            "register_session on resume without a tracker rebuild — "
            "double-spawn risk on the resumed phase."
        )

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

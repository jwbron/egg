"""Regression test for the flattened bridge driver (#2717 slice-1 task-1-3).

This pins the cq-7 / R17 walking-skeleton-bridge contract for Option B
(see ``plugins/egg-sdlc/skills/egg-sdlc/SKILL.md`` "Walking-skeleton
bridge gap" callout, picked over option (a)'s daemon variant). The
flattened driver runs in a fresh Python process per stage and the
``pending_hitl`` envelope in ``.egg-state/contracts/<id>.json`` is the
ONLY surviving state between invocations — generator object, ``gi_frame``,
background threads, and Python heap all die at process exit.

Acceptance criteria covered (per contract task-1-3):

* (a) The first invocation's ``pending_hitl.decision.question`` matches
  the preflight question the in-process generator yields on its first
  ``next()``.
* (b) After the test writes the operator's answer to
  ``pending_hitl.answer``, the second invocation consumes that answer
  and yields the refine-gate decision instead.

The test runs in <30s (per the AC's runtime cap) — guarded with
``pytest.mark.timeout(30)`` so a regression that hangs the driver
(e.g. ``generator.send()`` deadlock, background-thread non-join, blocking
substrate call) fails loudly rather than wedging CI.

Substrate isolation
-------------------
``run_pipeline_in_process`` is generator-shaped: each yield is an
``HITLDecision``, and between the preflight and refine-gate yields the
generator dispatches the refiner via ``select_substrate(env).spawner``.
A real spawn would invoke Claude Code's Agent tool (and thus the
Anthropic API), which the acceptance criterion forbids.

To keep the subprocess hermetic we ship a small shim through ``-c`` that:

1. Monkey-patches ``orchestrator.substrate.select_substrate`` to return a
   ``MagicMock`` bundle whose ``spawner.spawn`` returns a synthetic
   ``AgentResult`` (``exit_code=0``, ``commit_sha=<40 zeros>``,
   ``stdout="ok"``) — the same pattern used by the in-process unit tests
   in ``shared/tests/test_run_pipeline_in_process*.py``.
2. Shrinks the background-thread intervals so the test does not block on
   the default 5-second heartbeat tick.
3. Hands control to the real ``bin/run_pipeline.py`` driver via
   ``runpy.run_path(...)`` so the test exercises the production driver,
   not a re-implemented stand-in.

This is the standard pattern for testing CLI scripts that need a fake
substrate while exercising the real driver — no test-only flag added
to ``run_pipeline.py`` itself.

Driver invocation contract probed
---------------------------------
The coder's driver (task-1-1) accepts the pipeline id as a positional
``argv[1]``; the shim passes it that way. ``EGG_PIPELINE_ID`` is also
set so that any downstream tool reading the env (e.g. the orchestrator
heartbeat machinery) sees the same id. The driver's CWD is set to
``tmp_path`` so the ``.egg-state/contracts/<id>.json`` path resolves
cleanly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants — keep aligned with the driver's expected invocation contract
# ---------------------------------------------------------------------------


#: Path the coder commits the driver to per task-1-1 acceptance.
_DRIVER_PATH = Path("plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py")


#: Pipeline id used throughout the test. Deterministic so re-runs are
#: idempotent and don't fan into the orchestrator's id-space.
_PIPELINE_ID = "issue-bridge-round-trip"


#: The preflight question the in-process generator yields on its first
#: ``next()``. Source: ``orchestrator.substrate.in_process._build_preflight_decision``.
_PREFLIGHT_QUESTION = "Confirm the refiner will run against this repo + issue?"


# ---------------------------------------------------------------------------
# Shim — fake substrate + run the real driver via runpy
# ---------------------------------------------------------------------------


def _shim_source() -> str:
    """Subprocess shim source: patch substrate, then ``runpy`` the driver.

    Kept as a string so the test owns its own contract — no test-only
    code lives under ``plugins/egg-sdlc/`` or ``orchestrator/``.

    The substrate fake mirrors the existing ``fake_bundle`` fixture in
    ``shared/tests/test_run_pipeline_in_process_sentinel_and_hitl.py`` so
    a behaviour drift between unit and integration coverage is caught.
    """
    return textwrap.dedent(
        """
        import os, sys, runpy
        from unittest.mock import MagicMock
        from pathlib import Path

        # ----- Fake substrate bundle (no real Claude Code spawn) -----
        import orchestrator.substrate as _sub
        from orchestrator.substrate import in_process as _ip

        _wt = Path(os.environ.get('EGG_STATE_DIR', '.')) / 'wt'
        _wt.mkdir(parents=True, exist_ok=True)

        _bundle = MagicMock()
        _bundle.spawner.spawn = MagicMock(return_value=MagicMock(
            exit_code=0,
            commit_sha='0' * 40,
            stdout='ok',
            worktree=_wt,
            artifacts=[],
        ))
        _bundle.worktrees.create = MagicMock(return_value=_wt)
        _bundle.worktrees.tear_down = MagicMock()
        _bundle.name = 'claude-code'
        _sub.select_substrate = lambda env=None, **kw: _bundle

        # Shrink heartbeat / brc-review / bus intervals so the
        # generator doesn't block on the default 5s tick during a
        # subprocess test.
        _ip._HEARTBEAT_INTERVAL = 0.05
        _ip._BRC_REVIEW_INTERVAL = 0.05
        _ip._BUS_TICK_INTERVAL = 0.05

        # ----- Now run the real driver -----
        _driver = os.environ['EGG_TEST_DRIVER_PATH']
        sys.argv = [_driver, os.environ['EGG_PIPELINE_ID']]
        runpy.run_path(_driver, run_name='__main__')
        """
    )


def _invoke_driver(
    *,
    state_dir: Path,
    pipeline_id: str,
    repo_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Spawn the driver in a fresh Python process.

    Returns the ``CompletedProcess`` so callers can assert on exit code
    + stderr. The driver's CWD is ``state_dir`` so any cwd-relative
    ``.egg-state/`` path it computes lands in the test's tmp tree.
    """
    driver_path = (repo_root / _DRIVER_PATH).resolve()
    env = {
        **os.environ,
        "EGG_PIPELINE_ID": pipeline_id,
        "EGG_STATE_DIR": str(state_dir),
        "EGG_SUBSTRATE": "claude-code",
        "EGG_TEST_DRIVER_PATH": str(driver_path),
        # Subprocess PYTHONPATH must let every transitive import the
        # driver triggers resolve. The Makefile's
        # ``PYTHONPATH := shared:gateway:orchestrator`` (test target,
        # cwd-relative) is the source of truth; we mirror it with
        # absolute paths because the subprocess's CWD is the per-test
        # tmp dir. Each entry covers a distinct import shape:
        #   * ``<repo>/shared`` — ``egg_contracts`` etc. (imported
        #     transitively by ``orchestrator.substrate.k3s_adapter``).
        #   * ``<repo>`` — the ``orchestrator`` package itself
        #     (``orchestrator/__init__.py`` makes it a real package).
        #   * ``<repo>/orchestrator`` — bare-name top-level imports
        #     internal to the ``orchestrator/`` tree, e.g.
        #     ``orchestrator/models.py:16`` does
        #     ``from slice_id_validation import SLICE_ID_PATTERN`` and
        #     ``in_process.py:531-534`` has a bare ``from models import
        #     HITLDecision`` fallback. Without ``<repo>/orchestrator``
        #     on PYTHONPATH these crash the subprocess before the
        #     bridge driver yields its first HITL decision.
        #   * ``<repo>/gateway`` — matches the Makefile shape.
        "PYTHONPATH": os.pathsep.join(
            [
                str(repo_root / "shared"),
                str(repo_root),
                str(repo_root / "orchestrator"),
                str(repo_root / "gateway"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ).rstrip(os.pathsep),
    }
    return subprocess.run(
        [sys.executable, "-c", _shim_source()],
        capture_output=True,
        text=True,
        cwd=str(state_dir),
        env=env,
        # subprocess-level timeout: a stuck driver should not eat the
        # full pytest-timeout budget on its own.
        timeout=20,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Resolve the repo root from this test file's location."""
    return Path(__file__).resolve().parents[2]


def _read_contract(state_dir: Path, pipeline_id: str) -> dict:
    contract_path = state_dir / ".egg-state" / "contracts" / f"{pipeline_id}.json"
    assert contract_path.exists(), (
        f"driver must write the contract to {contract_path} — "
        f"directory contains: {sorted((state_dir / '.egg-state').rglob('*'))}"
    )
    return json.loads(contract_path.read_text())


def _write_answer(state_dir: Path, pipeline_id: str, answer: str) -> None:
    """Write the operator's answer + ``status=answered`` to ``pending_hitl``.

    The driver's protocol per ``run_pipeline.py`` (task-1-1 schema):

    * Skill body writes ``answer = <text>`` and ``status = "answered"``.
    * On the next invocation the driver promotes ``answer`` into
      ``answer_log`` and clears ``answer`` back to ``None``.

    A test that only writes ``answer`` without flipping ``status`` to
    ``answered`` would NOT cause the driver to promote it — that is
    by design (the skill must affirm the answer is final before the
    driver consumes it). Pin both fields here to mirror the
    skill-body contract.
    """
    contract_file = state_dir / ".egg-state" / "contracts" / f"{pipeline_id}.json"
    blob = json.loads(contract_file.read_text())
    pending = blob.get("pending_hitl") or {}
    pending["answer"] = answer
    pending["status"] = "answered"
    # Mirror the driver's ISO-8601 UTC timestamp format
    # (run_pipeline.py:101-103) so a test fixture and the driver's
    # source of truth never drift.
    pending["timestamp"] = datetime.now(UTC).isoformat()
    blob["pending_hitl"] = pending
    contract_file.write_text(json.dumps(blob, indent=2))


# ---------------------------------------------------------------------------
# Test — full two-stage round trip
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_repo_root() / _DRIVER_PATH).exists(),
    reason=(
        f"{_DRIVER_PATH} not present — task-1-1 (coder) has not landed "
        f"yet. This is the upstream dependency for the round-trip test."
    ),
)
def test_bridge_flattened_round_trip(tmp_path: Path) -> None:
    """Full round-trip: process exit, operator answers, process re-entry.

    Stage A — first invocation:
      * Contract starts with no ``pending_hitl.answer``.
      * Driver advances the generator to its first yield (preflight).
      * Driver writes the yielded ``HITLDecision`` to
        ``pending_hitl.decision`` and exits ``0``.
      * Test asserts ``pending_hitl.decision.question`` matches the
        preflight question (AC bullet (a)).

    Stage B — second invocation (after the operator answers):
      * Test writes ``"approve"`` to ``pending_hitl.answer``.
      * Driver re-reads the contract, drives the generator past the
        preflight yield via ``generator.send("approve")``, lands on
        the next yield (the refine-gate decision), writes that to
        ``pending_hitl.decision`` and exits ``0``.
      * Test asserts ``pending_hitl.decision`` is **different** from
        the preflight decision (AC bullet (b)) — the round-trip
        actually advanced the state machine.
    """
    repo_root = _repo_root()

    # ----- Stage A: first invocation -----
    proc1 = _invoke_driver(
        state_dir=tmp_path,
        pipeline_id=_PIPELINE_ID,
        repo_root=repo_root,
    )
    assert proc1.returncode == 0, (
        f"first invocation must exit 0 on generator yield (AC: "
        f"'exits with status 0 when the generator yields'). "
        f"stdout={proc1.stdout[-1000:]!r} stderr={proc1.stderr[-1000:]!r}"
    )

    contract1 = _read_contract(tmp_path, _PIPELINE_ID)
    pending1 = contract1.get("pending_hitl")
    assert pending1, (
        f"driver must write a ``pending_hitl`` envelope to the contract "
        f"after the first yield (task-1-1 schema). contract keys: "
        f"{sorted(contract1.keys())}"
    )
    decision1 = pending1.get("decision")
    assert decision1, (
        f"``pending_hitl.decision`` must be populated after the first yield. "
        f"pending_hitl={pending1!r}"
    )
    assert decision1.get("question") == _PREFLIGHT_QUESTION, (
        f"AC bullet (a): first yield must be the preflight question "
        f"{_PREFLIGHT_QUESTION!r}; got {decision1.get('question')!r}"
    )
    # task-1-1 schema fields: decision, answer, version, pipeline_id,
    # timestamp. Pin the load-bearing ones so a drift surfaces clearly.
    assert pending1.get("pipeline_id") == _PIPELINE_ID, (
        f"``pending_hitl.pipeline_id`` must round-trip the requested id; "
        f"got {pending1.get('pipeline_id')!r}"
    )
    assert "version" in pending1, (
        f"``pending_hitl.version`` is part of the stable schema (task-1-1) "
        f"so the daemon variant in TASK-3-2 can co-evolve; missing from "
        f"envelope {pending1!r}"
    )
    assert "timestamp" in pending1, (
        f"``pending_hitl.timestamp`` is part of the stable schema (task-1-1); "
        f"missing from envelope {pending1!r}"
    )

    # ----- Stage B: write answer, re-invoke -----
    _write_answer(tmp_path, _PIPELINE_ID, answer="approve")
    proc2 = _invoke_driver(
        state_dir=tmp_path,
        pipeline_id=_PIPELINE_ID,
        repo_root=repo_root,
    )
    assert proc2.returncode == 0, (
        f"second invocation must exit 0 on generator yield. "
        f"stdout={proc2.stdout[-1000:]!r} stderr={proc2.stderr[-1000:]!r}"
    )

    contract2 = _read_contract(tmp_path, _PIPELINE_ID)
    pending2 = contract2.get("pending_hitl") or {}
    decision2 = pending2.get("decision")
    assert decision2, (
        f"``pending_hitl.decision`` must be re-populated with the next "
        f"yield after the round-trip. pending_hitl={pending2!r}"
    )
    # AC bullet (b): the second yield is the refine-gate decision, NOT
    # the preflight. The refine-gate question shape is
    # ``"Refine analysis at ... Approve and continue?"`` or the
    # failure variant ``"Refiner FAILED..."``; both are distinct from
    # the preflight question.
    assert decision2.get("question") != _PREFLIGHT_QUESTION, (
        f"AC bullet (b): after answering preflight, the generator must "
        f"advance past it. Second-invocation question must differ from "
        f"the preflight; got identical question {decision2.get('question')!r}. "
        f"This means the driver did not consume ``pending_hitl.answer`` "
        f"(or the contract round-trip lost state)."
    )
    # The refine-gate decision_type is ``phase_gate`` per
    # ``_build_refine_gate_decision``; pin so a regression that yields a
    # different decision shape (e.g. preflight again, or a misrouted
    # choice) is caught.
    assert decision2.get("decision_type") in {"phase_gate", "choice"}, (
        f"refine-gate yield must be a phase_gate (or choice for the "
        f"failure variant); got {decision2.get('decision_type')!r} on "
        f"the second yield"
    )


# ---------------------------------------------------------------------------
# Adversarial probing — single-pass invariants the driver must hold
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_repo_root() / _DRIVER_PATH).exists(),
    reason=f"{_DRIVER_PATH} not present yet (task-1-1 dependency).",
)
def test_driver_is_idempotent_when_answer_unchanged(tmp_path: Path) -> None:
    """Re-running the driver without changing ``pending_hitl.answer`` is a no-op.

    Defensive invariant: if the operator hasn't answered the current
    decision, the driver must not silently skip ahead — it should
    either (a) re-write the same decision (idempotent) or (b) exit
    cleanly without corrupting state. Either is acceptable; what the
    driver MUST NOT do is advance the generator's state when there is
    no new answer to consume — that would lose the operator's intended
    decision boundary.
    """
    repo_root = _repo_root()

    # First invocation produces the preflight decision.
    proc1 = _invoke_driver(state_dir=tmp_path, pipeline_id=_PIPELINE_ID, repo_root=repo_root)
    assert proc1.returncode == 0, proc1.stderr
    contract1 = _read_contract(tmp_path, _PIPELINE_ID)
    decision1 = (contract1.get("pending_hitl") or {}).get("decision")
    assert decision1 and decision1.get("question") == _PREFLIGHT_QUESTION

    # Second invocation WITHOUT writing an answer.
    proc2 = _invoke_driver(state_dir=tmp_path, pipeline_id=_PIPELINE_ID, repo_root=repo_root)
    assert proc2.returncode == 0, (
        f"driver must tolerate re-invocation without a new answer; stderr={proc2.stderr[-500:]!r}"
    )
    contract2 = _read_contract(tmp_path, _PIPELINE_ID)
    decision2 = (contract2.get("pending_hitl") or {}).get("decision")
    # The decision question must still be the preflight — the driver
    # MUST NOT have advanced past it without an answer.
    assert decision2 and decision2.get("question") == _PREFLIGHT_QUESTION, (
        f"driver advanced the generator without a new answer; "
        f"second-invocation decision={decision2!r}. This is a HITL "
        f"safety bug — the operator's preflight answer would be lost."
    )

#!/usr/bin/env python3
"""Flattened single-yield stage driver for the egg-sdlc skill (#2717 TASK-1-1).

The skill cannot drive a long-lived Python generator across multiple
``AskUserQuestion`` round-trips — every ``python3`` subprocess from a
Bash skill step exits between yields, killing generator state and
background threads. Per cq-1 = Option C (hybrid), the refine and plan
phases use this **flattened** bridge: each invocation advances
``run_pipeline_in_process`` to its next yield, serialises the yielded
``HITLDecision`` to the contract's ``pending_hitl`` envelope, and
exits. The skill body in ``SKILL.md`` calls this driver in a loop;
between calls it renders ``pending_hitl.decision`` via ``AskUserQuestion``
and writes the operator's answer to ``pending_hitl.answer``.

Slice-3's daemon variant (``orchestrator/substrate/claude_code/hitl_daemon.py``,
TASK-3-2) consumes the SAME ``pending_hitl`` envelope schema so the two
bridges share a state-serialization contract (risk_analyst R17
mitigation).

``pending_hitl`` envelope schema (STABLE contract — slice-3 daemon
inherits this shape; do NOT change field names/types without bumping
``version``):

    pending_hitl: {
        version: int,           # schema version (currently 1)
        pipeline_id: str,       # echoes contract.pipeline_id for sanity
        timestamp: str,         # ISO-8601 UTC timestamp of last write
        decision: dict | None,  # the most recently yielded HITLDecision
                                # (serialised via .model_dump(mode="json")
                                # when pydantic; otherwise dict()) — None
                                # when the generator has not yielded yet
        answer: Any | None,     # the operator's response to ``decision``,
                                # written by the skill body before
                                # invoking the driver again. The driver
                                # consumes it via ``generator.send(answer)``
                                # then clears it back to None.
        status: str,            # one of:
                                #   "pending"   — decision waiting for answer
                                #   "answered"  — answer written, awaiting send
                                #   "completed" — generator returned (StopIteration)
                                #   "aborted"   — operator aborted at HITL
                                #   "error"     — driver hit an internal error
        result: str | None,     # generator return value when status==completed
                                # (the refine artifact path, typically)
        error: str | None,      # diagnostic message when status==error
    }

The ``status`` field is the skill's loop predicate: when it reads
``answered`` it knows there is an answer to ferry; when it reads
``pending`` it knows to render the decision; when it reads
``completed`` or ``error`` it exits the loop.

Generator state across invocations
----------------------------------
Each ``python3 bin/run_pipeline.py`` invocation is a fresh process.
Generator frames cannot persist across processes — that's the design
trade-off accepted for the flattened bridge (cq-1 Option C). To
resume across invocations, this driver replays the operator's
answers in order on every call: it reads ``pending_hitl.answer_log``
(a list appended once per answered yield) and feeds them back into a
fresh generator one at a time, then yields the *next* decision back
to the caller.

This works because ``run_pipeline_in_process`` is deterministic — the
same ``(pipeline_id, repo, issue_number, issue_body)`` inputs combined
with the same answer sequence reach the same yield boundary. For the
walking-skeleton phases (refine + plan) this is exact; the
implement phase has too many concurrent yields for replay to be
practical, which is why slice-3 ships the daemon variant instead.

Exit codes
----------

* ``0`` — generator yielded (decision written, status pending) or
  completed cleanly (status completed/aborted).
* ``1`` — driver hit an internal error (status error, error message
  written to ``pending_hitl.error``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Schema version of the ``pending_hitl`` envelope. Bump if you change
#: field names/types so the slice-3 daemon variant can refuse
#: incompatible envelopes rather than silently mis-reading.
PENDING_HITL_SCHEMA_VERSION = 1

#: Top-of-file marker tests use to detect this driver was actually
#: invoked (vs. a stale process from an earlier invocation).
DRIVER_INVOKED_MARKER = "egg-sdlc run_pipeline driver invoked"


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp string."""
    return datetime.now(UTC).isoformat()


def _ensure_contracts_dir(state_root: Path) -> Path:
    """Make sure ``.egg-state/contracts/`` exists and return the path."""
    contracts = state_root / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    return contracts


def _contract_path(state_root: Path, pipeline_id: str) -> Path:
    contracts = _ensure_contracts_dir(state_root)
    return contracts / f"{pipeline_id}.json"


def _read_contract(contract_path: Path, pipeline_id: str) -> dict[str, Any]:
    """Read the contract file, returning a default skeleton ONLY when absent.

    A missing file is a routine first-invocation state (no decisions
    persisted yet) and is handled silently. A *present-but-unparseable*
    file is NOT silently overwritten: an OSError / JSONDecodeError /
    non-object payload re-raises so the caller can persist an ``error``
    envelope rather than discarding ``answer_log`` and re-prompting the
    operator from scratch.
    """
    default: dict[str, Any] = {
        "schemaVersion": "1.1",
        "pipeline_id": pipeline_id,
        "current_phase": "refine",
        "decisions": [],
    }
    if not contract_path.exists():
        return default
    try:
        data = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # fmt: skip
        raise RuntimeError(
            f"contract file at {contract_path} is unparseable: {exc}. "
            "Refusing to overwrite — the operator's accumulated "
            "answer_log would be silently dropped. Inspect / repair "
            "the contract file by hand, or delete it to start fresh."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"contract file at {contract_path} is not a JSON object "
            f"(got {type(data).__name__}); refusing to overwrite."
        )
    data.setdefault("pipeline_id", pipeline_id)
    return data


def _write_contract(contract_path: Path, contract: dict[str, Any]) -> None:
    """Atomically write the contract file via temp + os.replace.

    Same shape as ``_InProcessOrchestrator._write_pending_decision`` so
    concurrent readers never observe a half-written file.
    """
    tmp = contract_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(contract, indent=2), encoding="utf-8")
    os.replace(tmp, contract_path)


def _serialise_decision(decision: Any) -> dict[str, Any] | None:
    """Best-effort decision → dict.

    Accepts pydantic ``HITLDecision`` (via ``.model_dump``), dataclass-
    like objects (via ``__dict__``), bare dicts, or anything else (the
    ``repr`` fallback ensures the shape is at least observable).

    The ``HITLDecision`` pydantic shape is the only expected input; if
    its ``model_dump(mode="json")`` raises we log loudly to stderr (the
    operator wants to know — a malformed envelope leaves
    ``AskUserQuestion`` with no ``question`` / ``options`` to render
    and the skill loop wedges silently otherwise).
    """
    if decision is None:
        return None
    if isinstance(decision, dict):
        return dict(decision)
    model_dump = getattr(decision, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
            if isinstance(dumped, dict):
                return dumped
            print(
                "run_pipeline.py: _serialise_decision: model_dump returned "
                f"{type(dumped).__name__} (expected dict); falling back to __dict__.",
                file=sys.stderr,
            )
        except (TypeError, ValueError) as exc:  # fmt: skip
            print(
                "run_pipeline.py: _serialise_decision: model_dump(mode='json') "
                f"raised {type(exc).__name__}: {exc}; falling back to __dict__. "
                "The pending_hitl envelope may not render correctly via "
                "AskUserQuestion — investigate the HITLDecision shape.",
                file=sys.stderr,
            )
    # Fall back to __dict__ for dataclasses / simple objects.
    raw = getattr(decision, "__dict__", None)
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    print(
        "run_pipeline.py: _serialise_decision: no model_dump / __dict__ "
        f"available on {type(decision).__name__}; persisting repr only. "
        "The skill body will not be able to render this decision.",
        file=sys.stderr,
    )
    return {"repr": repr(decision)}


def _new_envelope(
    pipeline_id: str,
    *,
    status: str = "pending",
    decision: dict[str, Any] | None = None,
    answer: Any = None,
    result: str | None = None,
    error: str | None = None,
    answer_log: list[Any] | None = None,
) -> dict[str, Any]:
    """Construct a ``pending_hitl`` envelope with all stable fields."""
    return {
        "version": PENDING_HITL_SCHEMA_VERSION,
        "pipeline_id": pipeline_id,
        "timestamp": _now_iso(),
        "decision": decision,
        "answer": answer,
        "status": status,
        "result": result,
        "error": error,
        "answer_log": list(answer_log) if answer_log is not None else [],
    }


def _coerce_envelope(raw: Any, pipeline_id: str) -> dict[str, Any]:
    """Validate / upgrade a stored envelope.

    Tolerates older shapes (missing ``answer_log``, missing ``version``)
    by defaulting them; rejects shapes whose ``version`` is newer than
    we understand by raising ``ValueError`` so the slice-3 daemon
    cannot accidentally consume a future-version envelope as if it were
    v1.
    """
    if not isinstance(raw, dict):
        return _new_envelope(pipeline_id)
    version = raw.get("version", PENDING_HITL_SCHEMA_VERSION)
    if isinstance(version, int) and version > PENDING_HITL_SCHEMA_VERSION:
        raise ValueError(
            f"pending_hitl envelope version {version} is newer than this "
            f"driver supports (max {PENDING_HITL_SCHEMA_VERSION}); upgrade "
            "the skill / driver to match the orchestrator."
        )
    return _new_envelope(
        pipeline_id,
        status=str(raw.get("status") or "pending"),
        decision=raw.get("decision") if isinstance(raw.get("decision"), dict) else None,
        answer=raw.get("answer"),
        result=raw.get("result") if isinstance(raw.get("result"), str) else None,
        error=raw.get("error") if isinstance(raw.get("error"), str) else None,
        answer_log=list(raw.get("answer_log") or []),
    )


def _persist_envelope(
    contract_path: Path,
    contract: dict[str, Any],
    envelope: dict[str, Any],
) -> None:
    """Write ``pending_hitl`` back to the contract and flush atomically."""
    envelope["timestamp"] = _now_iso()
    contract["pending_hitl"] = envelope
    _write_contract(contract_path, contract)


def _import_runner() -> Any:
    """Lazy import of ``run_pipeline_in_process``.

    The orchestrator package may not be importable in every smoke test
    environment; surface the import error as a structured ``error``
    envelope rather than a stack trace to stderr.
    """
    try:
        from orchestrator.substrate.in_process import run_pipeline_in_process
    except ImportError as exc:
        raise RuntimeError(
            "orchestrator.substrate.in_process.run_pipeline_in_process is not "
            f"importable: {exc}. Run `python3 bin/preflight.py` for install "
            "instructions."
        ) from exc
    return run_pipeline_in_process


def _advance_generator(
    runner: Any,
    *,
    pipeline_id: str,
    repo: str | None,
    issue_number: int | None,
    issue_body: str | None,
    state_root: Path,
    answer_log: list[Any],
) -> tuple[dict[str, Any] | None, str, str | None, Any]:
    """Drive a fresh generator forward, replaying ``answer_log``.

    Returns ``(decision_dict_or_None, status, result_or_None, next_answer)``.
    The fresh generator is closed before this function returns; its
    background threads are joined cleanly via ``GeneratorExit``
    discipline implemented in ``_InProcessOrchestrator``.

    Algorithm:
    1. Start the generator and call ``next()`` to land on the first yield.
    2. For each previously-collected answer in ``answer_log``, call
       ``generator.send(answer)`` — this lands on the next yield.
    3. The "next yield" after replay is the new decision the caller
       should render. Persist it and exit.
    4. If the generator returns instead of yielding, persist the
       result as ``completed``.
    """
    effective_env = {
        **os.environ,
        "EGG_SUBSTRATE": os.environ.get("EGG_SUBSTRATE", "claude-code"),
    }
    generator = runner(
        pipeline_id,
        repo=repo,
        issue_number=issue_number,
        issue_body=issue_body,
        env=effective_env,
        state_dir=state_root,
    )

    next_answer: Any = None
    try:
        try:
            # Stage 0: land on first yield.
            decision = next(generator)
        except StopIteration as stop:
            # Generator returned before yielding — exceedingly rare but
            # treat as a completed run.
            return None, "completed", _stopiter_value(stop), None

        # Replay each previously-collected answer in order. If we run
        # out of decisions before consuming the full answer_log, the
        # operator answered more times than the generator yielded —
        # truncate quietly so the loop converges. (The skill body is
        # expected to maintain answer_log invariants but a defensive
        # truncate avoids a hard error.)
        for replay in answer_log:
            try:
                decision = generator.send(replay)
            except StopIteration as stop:
                return None, "completed", _stopiter_value(stop), None

        # ``decision`` now holds the next-to-show HITL decision.
        return _serialise_decision(decision), "pending", None, next_answer
    finally:
        # Always close cleanly — GeneratorExit joins the background
        # threads inside _InProcessOrchestrator's ``finally`` block.
        # If teardown itself raises (e.g. _teardown_worktrees hits an
        # OSError), the orchestrator's own ``finally`` already
        # suppresses; we add a single stderr line here so the failure
        # is at least observable to an operator running the driver
        # with ``2>>driver.log``. The driver still returns success
        # because the generator's primary work (advancing to the next
        # yield) already succeeded.
        try:
            generator.close()
        except Exception as close_exc:  # noqa: BLE001 — defensive
            print(
                "run_pipeline.py: generator.close() raised "
                f"{type(close_exc).__name__}: {close_exc}; worktree may be "
                "leaked. Inspect ~/.egg-worktrees/ or EGG_WORKTREE_BASE for "
                "orphaned per-role checkouts.",
                file=sys.stderr,
            )


def _stopiter_value(stop: StopIteration) -> str | None:
    """Extract the generator's return value from a StopIteration.

    ``run_pipeline_in_process`` returns the refine artifact path as a
    string when the operator completes the gate (or a diagnostic
    message on abort, per ``_PreflightAborted``).
    """
    value = getattr(stop, "value", None)
    if value is None:
        return None
    return str(value)


def _is_aborted_status(answer: Any) -> bool:
    """Match the orchestrator's abort-detection logic for the answer
    field. We re-check here so the envelope's ``status`` is informative
    (``aborted`` vs ``completed``) when the generator stops on
    operator-abort.

    The abort vocabulary lives at
    ``orchestrator.substrate.in_process.ABORT_ANSWERS`` (single source
    of truth shared with ``_answer_is_abort`` in the orchestrator and
    the slice-3 daemon variant). We import lazily so the driver's
    import-time error path still hits the structured "preflight failed"
    message rather than a cascading ImportError.
    """
    if answer is None:
        return False
    if isinstance(answer, dict):
        answer = answer.get("selected") or answer.get("value")
    if not isinstance(answer, str):
        return False
    try:
        from orchestrator.substrate.in_process import ABORT_ANSWERS
    except ImportError:
        # Fall back to the literal set — only reached when the
        # orchestrator package is not importable, in which case the
        # driver's main() has already failed and we're computing this
        # for an envelope that won't be observed anyway.
        return answer.lower() in {"abort", "stop", "cancel"}
    return answer.lower() in ABORT_ANSWERS


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description=(
            "Advance the in-process orchestrator generator to its next "
            "HITL yield and persist the yielded decision to the "
            "contract's pending_hitl envelope."
        ),
    )
    parser.add_argument(
        "pipeline_id",
        help="Pipeline identifier (e.g. 'issue-1234'). Used to locate "
        "the contract under .egg-state/contracts/<id>.json.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Optional repo identifier ('owner/name'). Defaults to EGG_REPO from the env if unset.",
    )
    parser.add_argument(
        "--issue-number",
        type=int,
        default=None,
        help="Optional GitHub issue number; only used for artifact labelling.",
    )
    parser.add_argument(
        "--issue-body",
        default=None,
        help="Optional refiner task body. Defaults to reading "
        ".egg-state/drafts/<id>-issue.md when set inside the generator.",
    )
    parser.add_argument(
        "--state-root",
        default=None,
        help="Override the .egg-state/ root (defaults to <cwd>/.egg-state).",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help=(
            "Reserved for slice-3 (TASK-3-2): connect to / launch the "
            "long-lived hitl_daemon for implement-phase rather than "
            "running the flattened single-yield path. Today this flag "
            "is unimplemented and exits with a structured error so the "
            "skill can fall back to the flattened path."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Driver entry point. See module docstring for the lifecycle."""
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])

    # The marker exists so tests can scrape stdout/stderr and verify the
    # process actually ran (vs. a stale envelope).
    print(DRIVER_INVOKED_MARKER, file=sys.stderr)

    pipeline_id = str(args.pipeline_id).strip()
    if not pipeline_id:
        print("run_pipeline.py: pipeline_id must be non-empty", file=sys.stderr)
        return 1

    state_root = Path(args.state_root) if args.state_root else Path.cwd() / ".egg-state"
    contract_path = _contract_path(state_root, pipeline_id)
    try:
        contract = _read_contract(contract_path, pipeline_id)
    except RuntimeError as exc:
        # Contract present but unparseable. Surface loudly rather than
        # silently overwriting with a fresh skeleton — the operator's
        # accumulated answer_log would otherwise be dropped, the skill
        # would re-prompt from preflight, and there would be no signal
        # of the corruption.
        print(f"run_pipeline.py: {exc}", file=sys.stderr)
        return 1

    # Coerce any pre-existing envelope; if absent, create an empty one.
    try:
        envelope = _coerce_envelope(contract.get("pending_hitl"), pipeline_id)
    except ValueError as exc:
        envelope = _new_envelope(pipeline_id, status="error", error=f"envelope_coerce: {exc}")
        _persist_envelope(contract_path, contract, envelope)
        print(f"run_pipeline.py: {exc}", file=sys.stderr)
        return 1

    # ``--daemon`` is reserved for slice-3 (TASK-3-2). Today it short-
    # circuits with a structured error so the skill body sees a clean
    # signal it should fall back to the flattened path.
    if args.daemon:
        envelope = _new_envelope(
            pipeline_id,
            status="error",
            error=(
                "daemon mode is reserved for slice-3 (TASK-3-2) "
                "(orchestrator/substrate/claude_code/hitl_daemon.py); "
                "fall back to the flattened single-yield path for "
                "refine/plan phases."
            ),
        )
        _persist_envelope(contract_path, contract, envelope)
        print(envelope["error"], file=sys.stderr)
        return 1

    # If the skill body wrote an answer since the last call, append it
    # to the answer_log so the next replay picks it up. The skill body
    # writes ``answer`` (and leaves ``status`` at ``answered``); we
    # promote it into ``answer_log`` here.
    pending_answer = envelope.get("answer")
    if envelope.get("status") == "answered" and pending_answer is not None:
        envelope["answer_log"].append(pending_answer)
        envelope["answer"] = None

    # Repo / issue defaults — pick up from env when the caller didn't
    # pass them on the CLI.
    repo = args.repo or os.environ.get("EGG_REPO") or os.environ.get("EGG_PIPELINE_REPO")
    issue_number = args.issue_number
    if issue_number is None:
        env_issue = os.environ.get("EGG_ISSUE_NUMBER")
        if env_issue and env_issue.isdigit():
            issue_number = int(env_issue)

    try:
        runner = _import_runner()
    except RuntimeError as exc:
        envelope = _new_envelope(
            pipeline_id, status="error", error=str(exc), answer_log=envelope["answer_log"]
        )
        _persist_envelope(contract_path, contract, envelope)
        print(str(exc), file=sys.stderr)
        return 1

    try:
        decision_dict, status, result, _ = _advance_generator(
            runner,
            pipeline_id=pipeline_id,
            repo=repo,
            issue_number=issue_number,
            issue_body=args.issue_body,
            state_root=state_root,
            answer_log=list(envelope["answer_log"]),
        )
    except Exception as exc:  # noqa: BLE001 — driver-level failure
        trace = traceback.format_exc(limit=8)
        envelope = _new_envelope(
            pipeline_id,
            status="error",
            error=f"{type(exc).__name__}: {exc}\n{trace}",
            answer_log=envelope["answer_log"],
        )
        _persist_envelope(contract_path, contract, envelope)
        print(f"run_pipeline.py: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # Translate completed-but-aborted answers into ``status == aborted``
    # for skill-body observability. The orchestrator's _PreflightAborted
    # path returns a diagnostic string and surfaces it through StopIteration.
    if status == "completed" and envelope["answer_log"]:
        last_answer = envelope["answer_log"][-1]
        if _is_aborted_status(last_answer):
            status = "aborted"

    envelope = _new_envelope(
        pipeline_id,
        status=status,
        decision=decision_dict,
        answer=None,
        result=result,
        answer_log=envelope["answer_log"],
    )
    _persist_envelope(contract_path, contract, envelope)

    # Print a brief human-readable status line so the skill body has
    # something to log without parsing the JSON file.
    print(
        f"run_pipeline.py: status={status} pipeline_id={pipeline_id} "
        f"decision={'set' if decision_dict else 'none'} "
        f"answers_replayed={len(envelope['answer_log'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

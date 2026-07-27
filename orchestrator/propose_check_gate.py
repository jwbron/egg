"""Propose-time check gate — execute the repo's configured checks before
a proposal becomes reviewable (#3669).

BRC consensus validates *coherence*, not *correspondence* (#3595 root
cause 6). Run 5 of the ``laguna-s-2.1`` assessment reached full 8-of-8
consensus on both slices with zero unresolved NACKs while carrying three
purely mechanical defects: a file 84 lines over the hard cap, a missing
``FINDING_CLASS_REMEDIATIONS`` key whose test already existed and named
the exact failure in its own assertion message, and a SIGTERM/143
reclassification that left a contradicting test in the tree. None of the
three needs judgment; all three are found by running the commands the
repo already configures.

The remedy is **not** more reviewers, and **not** a mandate that each
reviewer run the checks. The same run's five NACK rounds caught four
genuine semantic defects — an unregistered ``detect_heartbeat_stall``, a
data source ``cq-1`` had ruled out, a container-ID-vs-role-name key
error, a wrong ``tool_calls_by_role`` lookup — that no linter finds.
Reviewer attention is the one resource that demonstrably catches those,
and spending it on something a script does perfectly costs 5x the
compute for one bit of information. The seam between "reason over a
diff" and "execute against the environment" is unmanned, and it should
be manned by the **system**: once per proposal, before any reviewer is
dispatched.

Relationship to the per-slice green gate (#3398)
------------------------------------------------
This gate does **not** replace it, and must not be read as weakening it.
Run 5 proves propose-time alone is insufficient: ``slice-2`` introduced
none of the three defects and inherited all of them from ``slice-1``'s
integration. Only a check at the *integration tip* sees that. The two
gates are complementary and differ deliberately:

============  ===================================  =========================
              propose gate (this module, #3669)    green gate (#3398)
============  ===================================  =========================
when          before a proposal becomes reviewable  before a slice PR opens
tree          the proposed ``commit_sha``           the integration-branch tip
test command  ``full_command`` (``make test-all``)  ``command`` (``make test``)
on red        proposal rejected; producer fixes     PR withheld; HITL decision
============  ===================================  =========================

Narrowed runs are not evidence
------------------------------
``make test`` is changeset-aware by design and narrows to the tests
statically reachable from the diff; ``make test-all`` is the CI ground
truth (root ``CLAUDE.md``). An agent following the repo's own documented
guidance therefore gets a green *narrow* result and reports "tests pass"
with the confidence of a full run. This is not hypothetical: the run-5
handoff reported "3751 passed, 1 failed" where a full run at the same
tip reports 8833 passed and surfaces the third defect — which lives in
an unrelated file about rate limiting, exactly the kind the import graph
will not reach.

So this gate never runs the narrowed form. Each configured check is
resolved to its ``full_command`` when ``repositories.yaml`` declares one
(egg: ``test`` → ``make test-all``) and to ``command`` otherwise, and
the **exact command string** plus the **SHA it ran against** are
recorded in the verdict and stamped onto the accepted proposal's
``attestation.checks_verified``. A narrowed run is then visibly narrow
rather than silently incomplete.

Fail open on infra, closed on real failures
-------------------------------------------
A check that could not run is not a check that passed. The vocabulary is
#3621's, shared literally: the infra signatures are imported from
``slice_green_gate`` so the two gates cannot drift on what counts as an
infrastructure fault, and the same *uniform* classification is applied
to every red this gate produces (#3621's complaint about the green gate
was precisely an asymmetry — ``classify_infra`` applied to one run and
not another). Fail-open covers: no configured checks, unresolvable
pipeline state, worktree/session/spawn failure, a pod that never reaches
a terminal state, an unparseable verdict, a checkout that cannot
materialise the proposed SHA, and a red verdict whose every failed check
carries an infra tag. It never covers a check that ran and failed.

Where the execution lives
-------------------------
This module is the *decision*: operator switches, which checks to run,
the verdict ledger, and whether a given ``CONSENSUS_PROPOSE`` is
rejected. The *execution* — the sandboxed runner Job, its manifest, and
the verdict protocol — lives in ``propose_check_runner``, including the
#3622 budget handling (the check deadline is a **pod-level**
``activeDeadlineSeconds``, so scheduling latency is not charged to it).

Asynchrony, and why a proposal can be "pending"
-----------------------------------------------
The sandbox posts ``CONSENSUS_PROPOSE`` over HTTP with a **15-second**
timeout (``egg_agent_tools.handlers._gateway.orchestrator_request``), so
the checks cannot run inside the request. They run in a background
thread against a sandboxed runner Job, and the verdict is recorded in a
process-local ledger keyed by ``(pipeline_id, slice_id, commit_sha)`` —
so a tree is checked **once**, no matter how many producers propose it
or how many times a propose is retried.

A propose that arrives before its tree has a verdict is therefore
answered ``409 checks_running``: the proposal is *not* recorded, no
reviewer is dispatched, and the producer re-proposes once the run
finishes. That is the one place the shape bends: a green proposal is
rejected once with a "wait" before it is accepted. Everything after the
verdict lands is untouched — a green verdict makes the propose path
byte-for-byte what it was before this gate existed.

Rollout
-------
``EGG_PROPOSE_CHECK_GATE``: ``off`` (**the default**) → ``log`` (run the
checks, log the verdict loudly, never reject) → ``on`` (reject on a
definitive red). It ships ``off`` deliberately: #3670 records that a
clean ``origin/main`` is red on two non-hermetic tests off-container,
and a gate enabled against a red baseline rejects every proposal on day
one. Flip the default only once #3670 is green — the invariant that
makes a check result load-bearing anywhere is "the suite is red" being
unambiguous evidence.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import propose_check_runner as _runner
from egg_logging import get_logger

logger = get_logger("orchestrator.propose_check_gate")

# Re-exported so the runner's output-tail budget has one owner and the
# rejection envelope's smaller slice reads next to it.
_REJECTION_OUTPUT_TAIL_CHARS = 1500

# Operator switch. Three-state, default "off" — see the module docstring
# ("Rollout"): #3670 must land before this can default on, or the gate
# rejects every proposal against a red baseline.
GATE_ENV_VAR = "EGG_PROPOSE_CHECK_GATE"

_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_LOG_ONLY_VALUES = frozenset({"log", "log-only", "log_only"})
_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

# Unset or unrecognised resolves here. Unlike the green gate — whose
# default is "on", so a typo can only over-verify — this gate's default
# is the *weakest* mode, so an unrecognised value cannot silently
# strengthen a deployment either. Both directions log.
_DEFAULT_MODE: Literal["off", "log", "on"] = "off"

# Comma-separated check *names* the gate skips. Same default as the
# green gate: the full security scan belongs on the context PR /
# terminal verification, not on every proposal.
SKIP_CHECKS_ENV_VAR = "EGG_PROPOSE_CHECK_GATE_SKIP_CHECKS"
_DEFAULT_SKIP_CHECKS = "security"

# Comma-separated pipeline phases the gate applies to. Refine and plan
# producers author *drafts*; running a repo's build/test suite against a
# prose analysis is pure cost and would red on unrelated baseline noise.
# The mechanical-defect class this gate exists for is an implement-phase
# class.
PHASES_ENV_VAR = "EGG_PROPOSE_CHECK_GATE_PHASES"
_DEFAULT_PHASES = "implement"

# Infra-red fail-open switch (#3417 / #3621 vocabulary). Default on: a
# red verdict whose every failed check matches an infra signature fails
# open instead of rejecting. "off" restores strict every-red-rejects.
# Anything unrecognised degrades to the default *and logs* — the typo
# direction here is strict → lenient, so it must not be silent.
INFRA_FAIL_OPEN_ENV_VAR = "EGG_PROPOSE_CHECK_GATE_INFRA_FAIL_OPEN"
_INFRA_FAIL_OPEN_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_INFRA_FAIL_OPEN_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})

# Ledger cap. One entry per (pipeline, slice, tree) is small, but the
# orchestrator process is long-lived and shared by every pipeline it
# drives, so an unbounded dict is a slow leak: a busy fleet proposing
# many trees across many pipelines cannot grow it without limit.
_LEDGER_MAX_ENTRIES = 256

# How long a ``failed`` record keeps rejecting the same tree before the
# gate re-runs the checks for it. Without this a red is cached for the
# life of the orchestrator process, so a red caused by a transient the
# infra classifier did not recognise (a flaky check, an evicted cache)
# is unrecoverable for that tree — the producer's only move is to
# rewrite history for a new SHA. The window is long enough that the
# normal fix-and-repropose loop never re-runs a genuine red (the
# producer's next propose carries a new SHA and a new ledger key), and
# short enough that a stuck producer is not stuck for the process's
# life. ``passed`` records are never expired: a green tree stays green.
_FAILED_RECORD_TTL_SECONDS = 3600

# A git object name and nothing else. ``ProposalPayload.commit_sha`` is
# an unconstrained agent-supplied string; anything that is not a hex
# object name cannot be a tree the runner could check out, so the gate
# declines rather than manufacturing a red out of a malformed field.
_COMMIT_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")


# --------------------------------------------------------------------------
# Operator switches
# --------------------------------------------------------------------------


def propose_check_gate_mode() -> Literal["off", "log", "on"]:
    """Resolve ``EGG_PROPOSE_CHECK_GATE`` to ``off`` / ``log`` / ``on``.

    Unset resolves to ``_DEFAULT_MODE`` (``off``, see the module
    docstring's Rollout note). An unrecognised value also resolves to
    the default and logs a warning: strengthening a gate by typo is as
    much a surprise as weakening one.
    """
    raw = os.environ.get(GATE_ENV_VAR, "").strip().lower()
    if not raw:
        return _DEFAULT_MODE
    if raw in _ENABLED_VALUES:
        return "on"
    if raw in _LOG_ONLY_VALUES:
        return "log"
    if raw in _DISABLED_VALUES:
        return "off"
    logger.warning(
        "Unrecognised propose-check-gate switch value; falling back to the default mode",
        env_var=GATE_ENV_VAR,
        value=raw,
        mode=_DEFAULT_MODE,
    )
    return _DEFAULT_MODE


def _infra_fail_open_enabled() -> bool:
    """Resolve the infra-red fail-open switch (default on).

    Mirrors ``slice_green_gate._infra_fail_open_enabled`` exactly,
    including the loud degrade: a mistyped value resolves to the
    *lenient* posture, which must never be silent.
    """
    raw = os.environ.get(INFRA_FAIL_OPEN_ENV_VAR, "on").strip().lower()
    if not raw or raw in _INFRA_FAIL_OPEN_ENABLED_VALUES:
        return True
    if raw in _INFRA_FAIL_OPEN_DISABLED_VALUES:
        return False
    logger.warning(
        "Unrecognised propose-check-gate infra-fail-open switch value; "
        "falling back to the default (fail open on all-infra reds)",
        env_var=INFRA_FAIL_OPEN_ENV_VAR,
        value=raw,
        fail_open=True,
    )
    return True


def _gate_phases() -> set[str]:
    raw = os.environ.get(PHASES_ENV_VAR, _DEFAULT_PHASES)
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def gate_checks(repo: str) -> list[dict[str, str]]:
    """Return the checks this gate runs for ``repo``, in ground-truth form.

    Config-driven: exactly ``get_repo_checks(repo)`` minus the skip set,
    with each entry's command resolved to its ``full_command`` when the
    repo declares one (#3669) and to ``command`` otherwise. The returned
    entries carry the *resolved* ``command`` plus a ``narrowed`` flag
    recording whether the repo had a ground-truth form to offer, so the
    attestation can say which one ran.

    Returns ``[]`` — gate skips — when the repo configures no checks or
    config loading fails. A config problem must never reject a
    proposal.
    """
    try:
        from config.repo_config import get_repo_checks
    except ImportError:
        try:
            from repo_config import get_repo_checks  # type: ignore[no-redef]
        except ImportError:
            return []

    try:
        configured = get_repo_checks(repo)
    except Exception as exc:  # noqa: BLE001 — config load is best-effort
        logger.warning(
            "Propose check gate skipped: failed to load repo checks config (#3669)",
            repo=repo,
            error=str(exc),
        )
        return []

    raw_skip = os.environ.get(SKIP_CHECKS_ENV_VAR, _DEFAULT_SKIP_CHECKS)
    skip = {name.strip().lower() for name in raw_skip.split(",") if name.strip()}

    resolved: list[dict[str, str]] = []
    for check in configured:
        if check["name"].strip().lower() in skip:
            continue
        full = check.get("full_command")
        resolved.append(
            {
                "name": check["name"],
                "command": full or check["command"],
                # "narrowed" is about *evidence quality*, not about the
                # command's content: the gate cannot know whether a repo's
                # ``command`` narrows, only whether the repo declared a
                # ground-truth form it could have run instead. Recording
                # the honest answer is the point — see the module
                # docstring's "Narrowed runs are not evidence".
                "narrowed": "false" if full else "unknown",
            }
        )
    return resolved


# --------------------------------------------------------------------------
# Verdict ledger
# --------------------------------------------------------------------------


@dataclass
class CheckRun:
    """One execution of the configured checks against one tree.

    Keyed by ``(pipeline_id, slice_id, commit_sha)``, **not** by role:
    the SHA determines the tree, so two producers proposing the same
    tree share one run and a retried propose never re-spawns a runner.
    That is the "runs ONCE per proposal" property, slightly stronger.
    """

    pipeline_id: str
    slice_id: str | None
    commit_sha: str
    base_branch: str
    repo: str
    checks: list[dict[str, str]]
    state: Literal["running", "passed", "failed", "infra"] = "running"
    verdict: dict[str, Any] | None = None
    failed: list[dict[str, Any]] = field(default_factory=list)
    infra_reason: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


# The ledger is **process-local**. That is correct only because the
# orchestrator Deployment runs ``replicas: 1`` and serves on a single
# waitress process: every propose for a given pipeline reaches the same
# interpreter, so "one run per tree" holds. Under more than one replica
# or a forking WSGI worker the ledger would fragment — two replicas
# would each start their own runner for the same tree, and a deferral
# recorded on one would be invisible to the other's event loop. Moving
# the gate to a shared store (Redis, like ``redis_message_store``) is
# the prerequisite for scaling the orchestrator horizontally.
_LEDGER: dict[tuple[str, str, str], CheckRun] = {}
_LEDGER_LOCK = threading.Lock()

# Producers whose propose was deferred on a still-running check run,
# keyed by ``(pipeline_id, slice_id, producer_role)`` → the ledger key
# they are waiting on. Read by the orchestrator event loop (see
# :func:`propose_spawn_block_reason`) so it does not respawn the
# producer arm into a 409 it can do nothing about. Guarded by
# ``_LEDGER_LOCK``: it is only ever consistent relative to the ledger.
_DEFERRED_PROPOSES: dict[tuple[str, str, str], tuple[str, str, str]] = {}

#: ``blocked`` reason the event loop records for a deferred producer.
PROPOSE_BLOCK_REASON = "checks_running"


def _ledger_key(pipeline_id: str, slice_id: str | None, commit_sha: str) -> tuple[str, str, str]:
    return (pipeline_id, slice_id or "", commit_sha)


def _deferral_key(pipeline_id: str, slice_id: str | None, role: str) -> tuple[str, str, str]:
    return (pipeline_id, slice_id or "", role or "")


def reset_ledger() -> None:
    """Drop every recorded run. Test seam; not called in production."""
    with _LEDGER_LOCK:
        _LEDGER.clear()
        _DEFERRED_PROPOSES.clear()


def propose_spawn_block_reason(pipeline_id: str, slice_id: str | None, role: str) -> str | None:
    """Why the event loop should not spawn ``role``'s propose right now.

    Returns :data:`PROPOSE_BLOCK_REASON` while the checks this producer
    was last deferred on are still running, ``None`` otherwise.

    Without this the deferral is invisible to the orchestrator's event
    loop: the 409 unwraps into a clean agent exit, ``_derive_next_action``
    re-derives the *identical* ``propose`` event, and the loop respawns a
    pod that can only be deferred again. Three such rounds trip #3425's
    no-op park, which then reports the slice as wedged on "an
    operator-bound wedge" and holds it for the 1800s retry heartbeat —
    a misdiagnosis of a gate that would have cleared on its own in
    minutes. Blocking the spawn here means one deferral, then one
    re-dispatch when the verdict lands.

    Self-cleaning: a deferral whose run has finished (or been evicted)
    is dropped on the next query, so a stale entry cannot wedge the arm
    in the other direction.
    """
    key = _deferral_key(pipeline_id, slice_id, role)
    with _LEDGER_LOCK:
        ledger_key = _DEFERRED_PROPOSES.get(key)
        if ledger_key is None:
            return None
        record = _LEDGER.get(ledger_key)
        if record is None or record.state != "running":
            _DEFERRED_PROPOSES.pop(key, None)
            return None
    return PROPOSE_BLOCK_REASON


def _record_deferral(
    pipeline_id: str, slice_id: str | None, role: str, ledger_key: tuple[str, str, str]
) -> None:
    with _LEDGER_LOCK:
        _DEFERRED_PROPOSES[_deferral_key(pipeline_id, slice_id, role)] = ledger_key


def _evict_locked() -> None:
    """Trim finished entries oldest-first once over the cap.

    Only finished entries are evictable: dropping a ``running`` record
    would let a second propose spawn a duplicate runner for a tree
    already in flight.
    """
    if len(_LEDGER) <= _LEDGER_MAX_ENTRIES:
        return
    finished = sorted(
        (k for k, v in _LEDGER.items() if v.state != "running"),
        key=lambda k: _LEDGER[k].finished_at or _LEDGER[k].started_at,
    )
    if not finished:
        # Every record is in flight, so the cap cannot be honoured. This
        # is a real condition, not a benign one: it means more than
        # _LEDGER_MAX_ENTRIES runner Jobs are live at once, which is a
        # runaway (a wedged wait loop, a fleet spawning unbounded
        # proposes), so say so rather than growing silently.
        logger.warning(
            "Propose check ledger over cap with no finished entries to evict (#3669)",
            entries=len(_LEDGER),
            cap=_LEDGER_MAX_ENTRIES,
        )
        return
    for key in finished[: len(_LEDGER) - _LEDGER_MAX_ENTRIES]:
        _LEDGER.pop(key, None)


def _failed_record_expired(record: CheckRun) -> bool:
    """True for a ``failed`` record past :data:`_FAILED_RECORD_TTL_SECONDS`."""
    if record.state != "failed":
        return False
    return (time.time() - (record.finished_at or record.started_at)) > _FAILED_RECORD_TTL_SECONDS


def _record_verdict(
    record: CheckRun, verdict: dict[str, Any] | None, infra_reason: str | None
) -> None:
    """Fold a runner result into ``record``, applying infra classification.

    Uniformly — every red this gate produces is classified, there is no
    second run carrying untagged entries (#3621's asymmetry complaint).

    ``state`` is assigned **last** in every branch. The gate reads a
    record without taking the ledger lock (a propose must not queue
    behind a check run finishing), and ``state`` is what it branches on,
    so every field the chosen branch reads must already be in place when
    the state that selects it becomes visible.
    """
    with _LEDGER_LOCK:
        record.finished_at = time.time()
        if verdict is None:
            record.infra_reason = infra_reason or "runner produced no verdict"
            record.state = "infra"
            return
        record.verdict = verdict
        failed = [c for c in verdict.get("checks", []) if not c.get("ok")]
        if not failed:
            record.state = "passed"
            return
        genuine = failed
        if _infra_fail_open_enabled():
            infra_failed = [c for c in failed if c.get("infra")]
            genuine = [c for c in failed if not c.get("infra")]
            if infra_failed and not genuine:
                record.infra_reason = (
                    "every red check matched an infrastructure signature: "
                    + "; ".join(f"{c.get('name')}: {c.get('infra')}" for c in infra_failed)
                )
                record.state = "infra"
                return
        record.failed = genuine
        record.state = "failed"


def _run_and_record(record: CheckRun) -> None:
    """Background body: execute the checks and fold the result in.

    Never raises — a thread that dies with an exception would leave the
    record ``running`` forever and wedge every propose for that tree.
    """
    try:
        from kubernetes_spawner import get_kubernetes_spawner

        spawner = get_kubernetes_spawner()
        # Called through the module object so the execution half stays a
        # patchable seam for callers that test the gate's decisions
        # without spawning a pod.
        verdict, infra_reason = _runner.run_propose_checks(
            pipeline_id=record.pipeline_id,
            commit_sha=record.commit_sha,
            base_branch=record.base_branch,
            repo=record.repo,
            checks=record.checks,
            spawner=spawner,
        )
    except Exception as exc:  # noqa: BLE001 — any failure here is infrastructure
        verdict, infra_reason = None, f"propose check runner raised: {exc}"

    _record_verdict(record, verdict, infra_reason)
    logger.info(
        "Propose check verdict recorded (#3669)",
        pipeline_id=record.pipeline_id,
        slice_id=record.slice_id,
        commit_sha=record.commit_sha,
        state=record.state,
        failed_checks=[c.get("name") for c in record.failed] or None,
        infra_reason=record.infra_reason,
        duration_seconds=round((record.finished_at or time.time()) - record.started_at, 1),
    )


def _get_or_start_run(
    *,
    pipeline_id: str,
    slice_id: str | None,
    commit_sha: str,
    base_branch: str,
    repo: str,
    checks: list[dict[str, str]],
) -> CheckRun:
    """Return the ledger record for this tree, starting a run if absent.

    A ``failed`` record older than :data:`_FAILED_RECORD_TTL_SECONDS` is
    discarded rather than replayed, so a red is re-checked instead of
    rejecting the same tree for the life of the process.
    """
    key = _ledger_key(pipeline_id, slice_id, commit_sha)
    with _LEDGER_LOCK:
        existing = _LEDGER.get(key)
        if existing is not None and not _failed_record_expired(existing):
            return existing
        if existing is not None:
            logger.info(
                "Propose check gate: red verdict expired, re-running the checks (#3669)",
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                commit_sha=commit_sha,
                age_seconds=int(time.time() - (existing.finished_at or existing.started_at)),
            )
        record = CheckRun(
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            commit_sha=commit_sha,
            base_branch=base_branch,
            repo=repo,
            checks=checks,
        )
        _LEDGER[key] = record
        _evict_locked()

    logger.info(
        "Propose check gate: starting configured checks for proposed tree (#3669)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        commit_sha=commit_sha,
        base_branch=base_branch,
        commands=[c["command"] for c in checks],
    )
    threading.Thread(
        target=_run_and_record,
        args=(record,),
        name=f"propose-check-{commit_sha[:12]}",
        daemon=True,
    ).start()
    return record


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def checks_verified_attestation(record: CheckRun) -> dict[str, Any]:
    """Build the ``attestation.checks_verified`` block for a proposal.

    Records, per check, the **exact command** that ran and the **SHA it
    ran against** — the sub-requirement of #3669 that makes a narrowed
    run visibly narrow rather than silently incomplete. ``verified_by``
    is ``"system"`` so this can never be confused with an agent's
    self-report, which is what ``checks_passed`` has always been.

    Each entry also carries ``narrowed``, which is the honest answer to
    a question the gate genuinely cannot decide. ``"false"`` means the
    repo declared a ``full_command`` and the gate ran it. ``"unknown"``
    means it did not, so the gate ran the only form configured — which
    may be changeset-narrowed, and if so this evidence is *not* the
    full-suite claim it otherwise looks like. Recording "unknown"
    instead of asserting "full" is what keeps a repo that forgot to
    declare ``full_command`` from silently inheriting the stronger
    claim.
    """
    verdict = record.verdict or {}
    narrowed_by_name = {c.get("name"): c.get("narrowed", "unknown") for c in record.checks}
    return {
        "status": record.state,
        "verified_by": "system",
        "gate": "propose-check-gate (#3669)",
        # The SHA the runner actually had checked out, when it reported
        # one; the proposed SHA otherwise. They agree unless the runner
        # could not resolve HEAD, in which case the proposed SHA is the
        # honest claim.
        "commit_sha": verdict.get("commit_sha") or record.commit_sha,
        "gate_id": verdict.get("gate_id"),
        "checks": [
            {
                "name": c.get("name"),
                "command": c.get("command"),
                "ok": bool(c.get("ok")),
                "exit_code": c.get("exit_code"),
                "narrowed": narrowed_by_name.get(c.get("name"), "unknown"),
            }
            for c in verdict.get("checks", [])
        ],
        "infra_reason": record.infra_reason,
    }


def _pending_envelope(record: CheckRun) -> tuple[str, int, dict[str, Any]]:
    commands = ", ".join(c["command"] for c in record.checks)
    elapsed = int(time.time() - record.started_at)
    return (
        f"Propose deferred: the configured checks are running against your "
        f"proposed tree {record.commit_sha[:12]} ({commands}). Your proposal "
        f"has NOT been recorded and no reviewer has been dispatched — the "
        f"system runs these once per tree so reviewers never spend a round on "
        f"code that does not build (#3669). Exit cleanly now: the orchestrator "
        f"owns the wait and will re-dispatch this propose when the verdict "
        f"lands, and the answer will be an acceptance or a named failing "
        f"check. Do NOT poll, sleep, or retry the propose yourself. "
        f"Started {elapsed}s ago.",
        409,
        {
            "status": "checks_running",
            "commit_sha": record.commit_sha,
            "commands": [c["command"] for c in record.checks],
            "elapsed_seconds": elapsed,
        },
    )


def _red_envelope(record: CheckRun) -> tuple[str, int, dict[str, Any]]:
    names = ", ".join(str(c.get("name")) for c in record.failed)
    blocks = []
    for check in record.failed:
        tail = str(check.get("output_tail") or "")[-_REJECTION_OUTPUT_TAIL_CHARS:]
        blocks.append(
            f"[{check.get('name')}] `{check.get('command')}` exited "
            f"{check.get('exit_code')}:\n{tail}".strip()
        )
    verdict = record.verdict or {}
    return (
        f"Propose rejected: the configured checks are red at your proposed "
        f"commit {record.commit_sha[:12]}: {names}. Consensus cannot certify "
        f"code that does not build, so this proposal is not reviewable (#3669). "
        f"Fix the named checks, commit, push, and propose again.\n\n"
        + "\n\n".join(blocks)
        # No bypass switch is named here: GATE_ENV_VAR is an
        # orchestrator-process env var an agent cannot read or set, so
        # offering it as a remedy in agent-facing text only invites a
        # round spent trying. Operators find it in the gate docs.
        + (f"\n\nFull output: runner pod {verdict.get('pod')} (gate {verdict.get('gate_id')})."),
        409,
        {
            "status": "checks_red",
            "commit_sha": record.commit_sha,
            "gate_id": verdict.get("gate_id"),
            "pod": verdict.get("pod"),
            "failed_checks": [
                {
                    "name": c.get("name"),
                    "command": c.get("command"),
                    "exit_code": c.get("exit_code"),
                    "output_tail": str(c.get("output_tail") or "")[-_REJECTION_OUTPUT_TAIL_CHARS:],
                }
                for c in record.failed
            ],
        },
    )


def propose_check_rejection(
    *,
    pipeline_id: str,
    repo: str,
    slice_id: str | None,
    producer_role: str,
    commit_sha: str,
    branch: str,
    current_phase: str | None,
    payload: dict[str, Any] | None = None,
) -> tuple[str, int, dict[str, Any]] | None:
    """Gate a ``CONSENSUS_PROPOSE`` on the configured checks (#3669).

    Returns ``None`` when the proposal may proceed, or a
    ``(message, status_code, details)`` rejection the caller renders.
    Called before the tracker records anything, so a rejected proposal
    mutates no consensus state and dispatches no reviewer.

    On a passing verdict the ``payload``'s attestation is stamped with
    ``checks_verified`` (see :func:`checks_verified_attestation`) so the
    recorded proposal carries the system's evidence — command and SHA —
    rather than only the producer's self-report.

    Fail-open (returns ``None``) on: gate off, a phase outside the
    configured set, a no-SHA propose, a non-hex SHA, no configured
    checks, an unresolvable branch, and an ``infra`` verdict.
    Fail-closed only on a verdict with at least one genuine red.
    """
    mode = propose_check_gate_mode()
    if mode == "off":
        return None
    if not commit_sha:
        # A no-op propose (#3027) carries no tree, so there is nothing
        # to check. The contract-completeness gate already prevents a
        # no-op from being an escape hatch.
        return None
    if not _COMMIT_SHA_RE.fullmatch(commit_sha):
        # ``commit_sha`` is an agent-supplied payload field and
        # ``ProposalPayload`` does not constrain its shape. The runner
        # only ever passes it to git in list form, so this is defence in
        # depth rather than the sole guard — but a value that cannot be
        # a commit is also a value the runner cannot check out, and
        # "could not run" is a fail-open, not a red.
        logger.warning(
            "Propose check gate skipped: commit_sha is not a hex object name (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            role=producer_role,
        )
        return None
    if (current_phase or "").strip().lower() not in _gate_phases():
        return None
    if not repo or not branch:
        logger.warning(
            "Propose check gate skipped: no repo/branch to resolve the proposed tree (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            repo=repo or None,
            branch=branch or None,
        )
        return None

    checks = gate_checks(repo)
    if not checks:
        logger.info(
            "Propose check gate skipped: no configured checks for repo (#3669)",
            pipeline_id=pipeline_id,
            repo=repo,
        )
        return None

    # Slice proposals are checked against the slice's integration
    # branch; phase-level ones against the pipeline branch. Either way
    # the runner detaches to ``commit_sha``, so the branch only has to
    # make the object reachable.
    base_branch = f"{branch}/{slice_id}" if slice_id else branch

    record = _get_or_start_run(
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        commit_sha=commit_sha,
        base_branch=base_branch,
        repo=repo,
        checks=checks,
    )

    if record.state == "running":
        if mode == "log":
            # Log mode must not change the flow at all: the run is now
            # in flight for its soak signal and the propose proceeds.
            return None
        # Make the deferral visible to the event loop before returning
        # it, so the producer arm is not respawned into the same 409
        # every poll (see propose_spawn_block_reason).
        _record_deferral(
            pipeline_id,
            slice_id,
            producer_role,
            _ledger_key(pipeline_id, slice_id, commit_sha),
        )
        return _pending_envelope(record)

    if record.state == "infra":
        # A check that could not run is not a check that passed, and it
        # is also not a check that failed. Proceed, loudly, and record
        # the honest status on the proposal.
        logger.warning(
            "Propose check gate failing open: checks could not run (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            role=producer_role,
            commit_sha=commit_sha,
            infra_reason=record.infra_reason,
            mode=mode,
        )
        _stamp_attestation(payload, record, producer_role)
        return None

    if record.state == "passed":
        logger.info(
            "Propose check gate passed (#3669)",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            role=producer_role,
            commit_sha=commit_sha,
            commands=[c["command"] for c in checks],
        )
        _stamp_attestation(payload, record, producer_role)
        return None

    # state == "failed"
    logger.error(
        "Propose check gate red: configured checks failed at the proposed commit (#3669)",
        pipeline_id=pipeline_id,
        slice_id=slice_id,
        role=producer_role,
        commit_sha=commit_sha,
        failed_checks=[c.get("name") for c in record.failed],
        mode=mode,
    )
    if mode == "log":
        _stamp_attestation(payload, record, producer_role)
        return None
    return _red_envelope(record)


def _stamp_attestation(
    payload: dict[str, Any] | None, record: CheckRun, producer_role: str = ""
) -> None:
    """Record the system's check evidence on the proposal payload.

    Two refusals, both about never turning a propose that would have
    succeeded into a 400:

    * A non-dict ``attestation`` is left exactly as it is. It is already
      malformed and ``handle_propose`` will fail on it; the failure
      should stay where it belongs rather than move here.
    * An **absent or empty** attestation is only created for a role that
      has a registered producer schema. ``handle_propose`` calls
      ``validate_attestation`` iff ``proposal.attestation`` is truthy,
      and that function *raises* for a role with no schema (the
      ``simplifier``, for one). Creating a dict to hold evidence would
      then reject an otherwise-valid proposal — evidence recording must
      never be load-bearing on acceptance. Such a role keeps its verdict
      in the structured log instead.
    """
    if not isinstance(payload, dict):
        return
    attestation = payload.get("attestation")
    if attestation is not None and not isinstance(attestation, dict):
        return
    if not attestation:
        try:
            from attestation_schemas import PRODUCER_ATTESTATION_MODELS
        except ImportError:  # pragma: no cover — packaging guard
            return
        if producer_role not in PRODUCER_ATTESTATION_MODELS:
            logger.info(
                "Propose check gate: verdict not stamped (role has no producer "
                "attestation schema); see the structured verdict log (#3669)",
                role=producer_role or None,
                commit_sha=record.commit_sha,
                state=record.state,
            )
            return
        attestation = {}
        payload["attestation"] = attestation
    attestation["checks_verified"] = checks_verified_attestation(record)

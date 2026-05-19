"""Plan-phase BRC pipeline body for the in-process Claude Code substrate.

Extracted from ``orchestrator/substrate/in_process.py`` (#2717 slice-2)
so the in-process generator file stays under the repo's 1500-line
hard cap (``scripts/file-size-allowlist.yaml``). The plan-phase
helpers live here as module-level functions that take the
``_InProcessOrchestrator`` instance as their first argument — this
keeps the public method surface on the class identical (the class's
``_run_plan_phase`` is a thin wrapper that delegates here) while
moving ~700 lines of body out of the generator module.

Why module-level functions instead of a sub-package: the plan-phase
body is a single linear flow (spawn-architect → fan-out → spawn-
reviewer → parse-verdicts → confirm); a sub-package per the
``docs/guides/decomposition-pattern.md`` pattern is overkill at
this size and would obscure the architect-first ordering. The
function-with-runner-instance pattern keeps state explicit and
mirrors how ``orchestrator/concurrent_executor.py`` exposes its
per-phase helpers.

See the orchestrator's ``_run_plan_phase`` docstring for the
end-to-end design narrative; this module owns the implementation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .in_process import _InProcessOrchestrator


def run_plan_phase(
    runner: _InProcessOrchestrator,
    refine_artifact_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """Run the plan-phase BRC cycle. See ``_InProcessOrchestrator._run_plan_phase``.

    Wraps the heartbeat-phase flip around the body so HEARTBEAT
    messages carry ``phase="plan"`` for the duration of the stage
    and through the subsequent plan HITL gate.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from egg_contracts.agent_roles import AgentRole

    from . import select_substrate

    try:
        from orchestrator.peer_consensus import (
            create_peer_consensus_tracker,
            get_peer_consensus_tracker,
        )
        from orchestrator.review_graph import get_review_graph_for_phase
    except ImportError:  # pragma: no cover
        from peer_consensus import (  # type: ignore[no-redef, import-untyped]
            create_peer_consensus_tracker,
            get_peer_consensus_tracker,
        )
        from review_graph import (  # type: ignore[no-redef, import-untyped]
            get_review_graph_for_phase,
        )

    runner._current_phase = "plan"
    return _run_plan_phase_inner(
        runner,
        refine_artifact_path,
        bundle_factory=select_substrate,
        executor_factory=ThreadPoolExecutor,
        as_completed_fn=as_completed,
        agent_role_module=AgentRole,
        create_tracker=create_peer_consensus_tracker,
        get_tracker=get_peer_consensus_tracker,
        graph_factory=get_review_graph_for_phase,
    )


def _run_plan_phase_inner(
    runner: _InProcessOrchestrator,
    refine_artifact_path: Path,
    *,
    bundle_factory: Any,
    executor_factory: Any,
    as_completed_fn: Any,
    agent_role_module: Any,
    create_tracker: Any,
    get_tracker: Any,
    graph_factory: Any,
) -> tuple[Path, dict[str, Any]]:
    """Plan-phase body. Parameters accept the lazily-imported primitives so
    the outer wrapper owns the imports and this body is import-error-free."""
    bundle = getattr(runner, "_bundle", None)
    if bundle is None:
        bundle = bundle_factory(runner.env)
        runner._bundle = bundle

    drafts_dir, _, _ = runner._ensure_state_dirs()
    artifact_id = runner.issue_number or runner.pipeline_id
    plan_artifact_path = drafts_dir / f"{artifact_id}-plan.md"

    architect_role = agent_role_module.ARCHITECT
    downstream_producers: list[Any] = [
        agent_role_module.TASK_PLANNER,
        agent_role_module.RISK_ANALYST,
    ]
    plan_producers: list[Any] = [architect_role, *downstream_producers]
    plan_reviewer = agent_role_module.REVIEWER_PLAN

    graph = graph_factory("plan", repo=runner.repo)
    tracker = get_tracker(runner.pipeline_id)
    if tracker is None:
        tracker = create_tracker(runner.pipeline_id, graph, cooldown_seconds=0)
    for role in (*plan_producers, plan_reviewer):
        tracker.register_agent(role.value)
    runner._plan_tracker = tracker

    producer_results: dict[Any, Any] = {}
    producer_artifacts: dict[Any, Path] = {}

    # Stage 4a: architect spawns FIRST, synchronously.
    architect_artifact, architect_result = spawn_plan_producer(
        runner,
        architect_role,
        bundle,
        refine_artifact_path,
        plan_artifact_path,
        architect_output_path=None,
    )
    producer_results[architect_role] = architect_result
    producer_artifacts[architect_role] = architect_artifact
    architect_output_path = plan_producer_output_path(runner, architect_role)
    _record_producer_propose(runner, tracker, architect_role, architect_artifact, architect_result)

    # Stage 4b: task_planner + risk_analyst fan out concurrently.
    with executor_factory(max_workers=len(downstream_producers)) as pool:
        future_map = {
            pool.submit(
                spawn_plan_producer,
                runner,
                role,
                bundle,
                refine_artifact_path,
                plan_artifact_path,
                architect_output_path,
            ): role
            for role in downstream_producers
        }
        for fut in as_completed_fn(future_map):
            role = future_map[fut]
            try:
                artifact_path, spawn_result = fut.result()
            except Exception as exc:  # noqa: BLE001 — defensive
                producer_results[role] = exc
                producer_artifacts[role] = plan_artifact_path
                continue
            producer_results[role] = spawn_result
            producer_artifacts[role] = artifact_path
            _record_producer_propose(runner, tracker, role, artifact_path, spawn_result)

    # Stage 4c: reviewer_plan + verdict-JSON parsing.
    reviewer_artifact, reviewer_result = spawn_plan_reviewer(
        runner, bundle, producer_artifacts, plan_artifact_path
    )
    producer_results[plan_reviewer] = reviewer_result
    producer_artifacts[plan_reviewer] = reviewer_artifact

    verdict_path, verdicts = read_plan_reviewer_verdicts(runner, plan_producers=plan_producers)
    runner._verdict_diagnostics = {
        "verdict_path": str(verdict_path) if verdict_path else None,
        "verdicts": verdicts,
        "reviewer_exit_code": int(getattr(reviewer_result, "exit_code", 0) or 0),
    }
    _apply_reviewer_verdicts(
        runner,
        tracker,
        plan_reviewer,
        plan_producers,
        producer_artifacts,
        producer_results,
        reviewer_result,
        verdicts,
    )

    # Stage 4d: drive CONSENSUS_CONFIRMED on each agent.
    for role in (*plan_producers, plan_reviewer):
        try:
            tracker.handle_confirmed(role.value)
        except Exception as exc:  # noqa: BLE001 — defensive
            log_tracker_warning("handle_confirmed", role.value, exc, runner.pipeline_id)

    plan_eval = tracker.evaluate()

    if not plan_artifact_path.exists():
        plan_artifact_path.write_text(
            format_plan_placeholder(
                pipeline_id=runner.pipeline_id,
                issue_number=runner.issue_number,
                repo=runner.repo,
                plan_producers=[role.value for role in plan_producers],
                plan_reviewer=plan_reviewer.value,
                producer_results=producer_results,
                plan_eval=plan_eval,
                verdict_diagnostics=runner._verdict_diagnostics,
            )
        )

    return plan_artifact_path, plan_eval


def plan_producer_output_path(runner: _InProcessOrchestrator, role: Any) -> Path:
    """Return ``.egg-state/agent-outputs/<issue>-<role>-output.json``."""
    runner._ensure_state_dirs()
    outputs_dir = runner.state_root / "agent-outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    artifact_id = runner.issue_number or runner.pipeline_id
    return outputs_dir / f"{artifact_id}-{role.value}-output.json"


def _record_producer_propose(
    runner: _InProcessOrchestrator,
    tracker: Any,
    role: Any,
    artifact_path: Path,
    spawn_result: Any,
) -> None:
    """Record CONSENSUS_PROPOSE for a producer when its spawn succeeded."""
    exit_code = int(getattr(spawn_result, "exit_code", 0) or 0)
    if exit_code != 0:
        return
    commit_sha = getattr(spawn_result, "commit_sha", None) or synthetic_commit_for(role.value)
    try:
        tracker.handle_propose(
            role.value,
            {
                "summary": (
                    f"{role.value} produced plan-phase artifact at "
                    f"{artifact_path} via the in-process Claude "
                    "Code substrate (#2717 slice-2)."
                ),
                "artifacts": [str(artifact_path)],
                "commit_sha": commit_sha,
            },
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log_tracker_warning("handle_propose", role.value, exc, runner.pipeline_id)


def read_plan_reviewer_verdicts(
    runner: _InProcessOrchestrator,
    *,
    plan_producers: list[Any] | None = None,
) -> tuple[Path | None, dict[str, dict[str, Any]]]:
    """Parse the reviewer_plan verdict JSON if present.

    Two schemas are accepted to align with the rubric the documenter
    shipped (``plugins/egg-sdlc/skills/egg-sdlc/agents/reviewer_plan.md``)
    AND a more granular extension shape:

    1. **Rubric-default (single-verdict, broadcast).** The rubric
       documents the JSON object as a single top-level verdict
       (``verdict`` ∈ {"ACK", "NACK"}, ``analysis`` carrying the
       eight criteria, ``feedback`` blob, ``artifact_references``).
       When this is the shape on disk, the verdict is broadcast to
       every plan producer edge — ACK acks all three, NACK nacks
       all three with ``feedback`` as the per-edge reason. This is
       the "Option (c)" resolution from reviewer_code_holistic v3
       NACK blocker H3.
    2. **Per-producer extension (per-edge).** When the verdict JSON
       carries a ``per_producer`` mapping of
       ``{role_name: {"verdict": "ACK"|"NACK", "reason": str, ...}}``
       entries, per-edge semantics override the broadcast: each
       edge's verdict is taken from the matching entry. A reviewer
       that wants edge granularity (e.g. ACK architect + NACK
       task_planner) writes the wrapper; the rubric's default
       single-verdict shape stays broadcast-compatible.

    Returns ``(verdict_path, verdicts)``. ``verdicts`` is empty
    when the file is missing or the JSON is unparseable; the
    orchestrator's fail-closed heuristic in
    ``_apply_reviewer_verdicts`` treats that as NACK only when
    the reviewer's spawn itself failed.
    """
    outputs_dir = runner.state_root / "agent-outputs"
    artifact_id = runner.issue_number or runner.pipeline_id
    verdict_path = outputs_dir / f"{artifact_id}-reviewer_plan-output.json"
    if not verdict_path.is_file():
        return None, {}
    try:
        blob = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):  # fmt: skip
        return verdict_path, {}
    if not isinstance(blob, dict):
        return verdict_path, {}

    # Schema 2: per-producer extension wrapper takes precedence if
    # it's a well-formed dict. Reviewers that want per-edge
    # granularity opt into it explicitly.
    per_producer = blob.get("per_producer")
    if isinstance(per_producer, dict) and per_producer:
        normalised: dict[str, dict[str, Any]] = {}
        for role_name, entry in per_producer.items():
            if not isinstance(entry, dict):
                continue
            verdict = str(entry.get("verdict", "")).strip().upper()
            if verdict not in {"ACK", "NACK"}:
                continue
            normalised[str(role_name)] = {
                "verdict": verdict,
                "reason": str(entry.get("reason", "")),
                "artifact_references": list(entry.get("artifact_references") or []),
                "pre_merge_condition": str(entry.get("pre_merge_condition", "")),
            }
        if normalised:
            return verdict_path, normalised

    # Schema 1: rubric-default single-verdict broadcast. The rubric
    # specifies ``verdict``, ``analysis``, ``feedback``,
    # ``artifact_references`` at the top level. NACK propagates the
    # ``feedback`` blob into every producer's per-edge reason so
    # the operator sees the same revision instructions on each
    # tracker edge.
    top_verdict = str(blob.get("verdict", "")).strip().upper()
    if top_verdict in {"ACK", "NACK"}:
        # When the caller hasn't told us which producers to
        # broadcast across (legacy callers), the broadcast is
        # impossible — return empty and let the orchestrator's
        # fail-closed / optimistic-ACK heuristic apply.
        if not plan_producers:
            return verdict_path, {}
        feedback = str(blob.get("feedback", "")).strip()
        # NACK with an empty feedback blob would hit
        # ReviewPayload.validate_nack_has_reason. Synthesise a
        # placeholder so the tracker records the NACK rather than
        # silently losing it (reviewer_code v3 non-blocking #1).
        broadcast_reason = (
            feedback
            or f"reviewer_plan broadcast {top_verdict}: top-level verdict "
            "without a per-edge feedback blob — see verdict JSON for the "
            "criteria-keyed analysis."
        )
        broadcast_refs = list(blob.get("artifact_references") or [])
        broadcast = {
            "verdict": top_verdict,
            "reason": broadcast_reason,
            "artifact_references": broadcast_refs,
            "pre_merge_condition": str(blob.get("pre_merge_condition", "")),
        }
        return verdict_path, {role.value: broadcast for role in plan_producers}

    return verdict_path, {}


def _apply_reviewer_verdicts(
    runner: _InProcessOrchestrator,
    tracker: Any,
    reviewer_role: Any,
    plan_producers: list[Any],
    producer_artifacts: Mapping[Any, Path],
    producer_results: Mapping[Any, Any],
    reviewer_result: Any,
    verdicts: Mapping[str, Mapping[str, Any]],
) -> None:
    """Apply ACK / NACK to the tracker per the reviewer's verdict JSON.

    Reviewer_code_holistic v1 blocker H2: under v1 the orchestrator
    forged ACKs based purely on exit_code. v2: parse the verdict
    file; fail-closed (NACK every edge) when missing AND the
    reviewer's spawn failed; preserve the harness-faked test path
    (verdict file missing AND reviewer exit_code 0) as an optimistic
    ACK with a diagnostic surface in the placeholder body.
    """
    reviewer_exit_code = int(getattr(reviewer_result, "exit_code", 0) or 0)
    verdict_file_present = bool(verdicts)
    fail_closed = (not verdict_file_present) and reviewer_exit_code != 0

    for producer in plan_producers:
        producer_run = producer_results.get(producer)
        if isinstance(producer_run, Exception):
            continue
        producer_exit = int(getattr(producer_run, "exit_code", 0) or 0)
        if producer_exit != 0:
            continue

        entry = verdicts.get(producer.value)
        if entry is None:
            if fail_closed:
                _record_reviewer_nack(
                    runner,
                    tracker,
                    reviewer_role,
                    producer,
                    producer_artifacts,
                    reason=(
                        "reviewer_plan verdict file missing / unparseable "
                        f"AND reviewer exit_code={reviewer_exit_code} — "
                        "in-process orchestrator fail-closes per "
                        "#2717 slice-2 v2 blocker H2."
                    ),
                )
                continue
            _record_reviewer_ack(
                runner,
                tracker,
                reviewer_role,
                producer,
                producer_artifacts,
                reason=(
                    "reviewer_plan ACK (synthetic): verdict file absent "
                    "AND reviewer exit_code=0 — in-process synchronous-"
                    "spawn-as-signal default per #2717 slice-2."
                ),
            )
            continue

        if entry["verdict"] == "ACK":
            _record_reviewer_ack(
                runner,
                tracker,
                reviewer_role,
                producer,
                producer_artifacts,
                reason=entry.get("reason", ""),
                artifact_references=entry.get("artifact_references"),
                pre_merge_condition=entry.get("pre_merge_condition") or "",
            )
        else:  # entry["verdict"] == "NACK"
            _record_reviewer_nack(
                runner,
                tracker,
                reviewer_role,
                producer,
                producer_artifacts,
                reason=entry.get("reason", ""),
                artifact_references=entry.get("artifact_references"),
            )


def _record_reviewer_ack(
    runner: _InProcessOrchestrator,
    tracker: Any,
    reviewer_role: Any,
    producer: Any,
    producer_artifacts: Mapping[Any, Path],
    *,
    reason: str = "",
    artifact_references: Any | None = None,
    pre_merge_condition: str = "",
) -> None:
    refs = list(artifact_references or [str(producer_artifacts.get(producer, ""))])
    if not refs or not refs[0]:
        refs = [str(producer_artifacts.get(producer, ""))]
    payload: dict[str, Any] = {
        "artifact_references": refs,
        "reason": reason
        or (
            "reviewer_plan ACK in #2717 slice-2: producer artifact "
            "structurally valid; reviewer verdict JSON not parsed "
            "(see _verdict_diagnostics)."
        ),
    }
    if pre_merge_condition:
        payload["pre_merge_condition"] = pre_merge_condition
    try:
        tracker.handle_ack(reviewer_role.value, producer.value, payload)
    except Exception as exc:  # noqa: BLE001 — defensive
        log_tracker_warning(
            "handle_ack",
            f"{reviewer_role.value}→{producer.value}",
            exc,
            runner.pipeline_id,
        )


def _record_reviewer_nack(
    runner: _InProcessOrchestrator,
    tracker: Any,
    reviewer_role: Any,
    producer: Any,
    producer_artifacts: Mapping[Any, Path],
    *,
    reason: str,
    artifact_references: Any | None = None,
) -> None:
    refs = list(artifact_references or [str(producer_artifacts.get(producer, ""))])
    if not refs or not refs[0]:
        refs = [str(producer_artifacts.get(producer, ""))]
    try:
        tracker.handle_nack(
            reviewer_role.value,
            producer.value,
            {"artifact_references": refs, "reason": reason},
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        log_tracker_warning(
            "handle_nack",
            f"{reviewer_role.value}→{producer.value}",
            exc,
            runner.pipeline_id,
        )


def spawn_plan_producer(
    runner: _InProcessOrchestrator,
    role: Any,
    bundle: Any,
    refine_artifact_path: Path,
    plan_artifact_path: Path,
    architect_output_path: Path | None = None,
) -> tuple[Path, Any]:
    """Dispatch a single plan-phase producer via the substrate.

    Reviewer_concurrency v1 blocker #1: does NOT write the
    active-role sentinel under concurrent dispatch. Each spawn
    carries ``EGG_AGENT_ROLE`` in its own env so the hook's primary
    role-resolution channel is per-spawn correct; the single-valued
    ``$HOME/.claude/egg-active-role.json`` sentinel cannot
    disambiguate three concurrent role-holders.
    """
    worktree = bundle.worktrees.create(runner.pipeline_id, role)
    producer_output_path = plan_producer_output_path(runner, role)

    spawn_env = {
        **runner.env,
        "EGG_PIPELINE_ID": runner.pipeline_id,
        "EGG_AGENT_ROLE": role.value,
        "EGG_REPO_ROOT": str(worktree),
        "EGG_WORKTREE_ROOT": str(worktree),
        "EGG_PHASE": "plan",
        "EGG_REFINE_ARTIFACT_PATH": str(refine_artifact_path),
        "EGG_PLAN_ARTIFACT_PATH": str(plan_artifact_path),
        "EGG_PRODUCER_OUTPUT_PATH": str(producer_output_path),
    }
    if architect_output_path is not None:
        spawn_env["EGG_ARCHITECT_OUTPUT_PATH"] = str(architect_output_path)
    if runner.repo:
        spawn_env["EGG_REPO"] = runner.repo
    if runner.issue_number is not None:
        spawn_env["EGG_ISSUE_NUMBER"] = str(runner.issue_number)

    prompt_lines = [
        f"Plan-phase {role.value} dispatch for pipeline "
        f"{runner.pipeline_id} (issue={runner.issue_number or '<none>'}).",
        f"Refine artifact: {refine_artifact_path}",
        f"Plan artifact target: {plan_artifact_path}",
        f"Your handoff JSON target: {producer_output_path}",
    ]
    if architect_output_path is not None:
        prompt_lines.append(f"Architect handoff input: {architect_output_path}")
    prompt_text = "\n".join(prompt_lines) + "\n"

    spawn_result = bundle.spawner.spawn(role, prompt_text, spawn_env, worktree)
    return plan_artifact_path, spawn_result


def spawn_plan_reviewer(
    runner: _InProcessOrchestrator,
    bundle: Any,
    producer_artifacts: Mapping[Any, Path],
    plan_artifact_path: Path,
) -> tuple[Path, Any]:
    """Dispatch reviewer_plan once and return its ``AgentResult``.

    Reviewer dispatches solo (no concurrent role-holder), so the
    single-valued sentinel correctly identifies the active role for
    any nested-dispatch fallback the reviewer's subagents might
    trigger.
    """
    from egg_contracts.agent_roles import AgentRole

    worktree = bundle.worktrees.create(runner.pipeline_id, AgentRole.REVIEWER_PLAN)

    per_role_inputs: dict[str, str] = {}
    for role in producer_artifacts:
        if role is AgentRole.REVIEWER_PLAN:
            continue
        per_role_inputs[f"EGG_{role.value.upper()}_OUTPUT_PATH"] = str(
            plan_producer_output_path(runner, role)
        )

    artifact_id = runner.issue_number or runner.pipeline_id
    reviewer_verdict_path = (
        runner.state_root / "agent-outputs" / f"{artifact_id}-reviewer_plan-output.json"
    )

    spawn_env = {
        **runner.env,
        "EGG_PIPELINE_ID": runner.pipeline_id,
        "EGG_AGENT_ROLE": AgentRole.REVIEWER_PLAN.value,
        "EGG_REPO_ROOT": str(worktree),
        "EGG_WORKTREE_ROOT": str(worktree),
        "EGG_PHASE": "plan",
        "EGG_PLAN_ARTIFACT_PATH": str(plan_artifact_path),
        "EGG_REVIEWER_VERDICT_PATH": str(reviewer_verdict_path),
        **per_role_inputs,
    }
    if runner.repo:
        spawn_env["EGG_REPO"] = runner.repo
    if runner.issue_number is not None:
        spawn_env["EGG_ISSUE_NUMBER"] = str(runner.issue_number)

    runner._write_active_role_sentinel(AgentRole.REVIEWER_PLAN.value)

    per_role_inputs_summary = ", ".join(
        f"{k.lower()}={v}" for k, v in sorted(per_role_inputs.items())
    )
    prompt_text = (
        f"Plan-phase reviewer_plan dispatch for pipeline "
        f"{runner.pipeline_id} (issue={runner.issue_number or '<none>'}).\n"
        f"Plan artifact target: {plan_artifact_path}\n"
        f"Producer handoff JSON inputs: {per_role_inputs_summary}\n"
        f"Your verdict JSON target: {reviewer_verdict_path}\n"
    )

    spawn_result = bundle.spawner.spawn(AgentRole.REVIEWER_PLAN, prompt_text, spawn_env, worktree)
    return plan_artifact_path, spawn_result


def format_plan_placeholder(
    *,
    pipeline_id: str,
    issue_number: int | None,
    repo: str | None,
    plan_producers: list[str],
    plan_reviewer: str,
    producer_results: Mapping[Any, Any],
    plan_eval: Mapping[str, Any],
    verdict_diagnostics: Mapping[str, Any] | None = None,
) -> str:
    """Render the plan-artifact placeholder body."""
    verdict_diagnostics = verdict_diagnostics or {}

    lines: list[str] = [
        "# Plan analysis (placeholder — plan producers did not land a full plan)",
        "",
        f"Pipeline: {pipeline_id}",
        f"Repo: {repo or '<unspecified>'}",
        f"Issue: {issue_number if issue_number is not None else '<none>'}",
        "",
        "## Per-producer diagnostics",
        "",
    ]
    for producer in plan_producers:
        lines.append(_render_role_diagnostics(producer, producer_results))

    lines.append("")
    lines.append("## reviewer_plan diagnostics")
    lines.append("")
    lines.append(_render_role_diagnostics(plan_reviewer, producer_results))

    lines.append("")
    lines.append("## reviewer_plan verdict parsing")
    lines.append("")
    verdict_path = verdict_diagnostics.get("verdict_path")
    verdicts = verdict_diagnostics.get("verdicts") or {}
    reviewer_exit_code = verdict_diagnostics.get("reviewer_exit_code", "<unknown>")
    lines.append(f"- verdict_path: {verdict_path or '<missing>'}")
    lines.append(f"- reviewer_exit_code: {reviewer_exit_code}")
    if verdicts:
        lines.append("- per_producer:")
        for role_name in sorted(verdicts.keys()):
            entry = verdicts[role_name]
            lines.append(
                f"  - {role_name}: verdict={entry.get('verdict')!r}; "
                f"reason={(entry.get('reason') or '')[:200]!r}"
            )
    else:
        lines.append("- per_producer: <empty> — reviewer did not write a parseable verdict JSON")

    lines.append("")
    lines.append("## BRC evaluation snapshot")
    lines.append("")
    lines.append(f"- is_complete: {bool(plan_eval.get('is_complete'))}")
    lines.append(f"- blocking_agents: {list(plan_eval.get('blocking_agents') or [])!r}")
    nack_details = plan_eval.get("unresolved_nack_details") or []
    lines.append(f"- unresolved_nack_details: {list(nack_details)!r}")
    lines.append("")
    lines.append(
        "This placeholder was emitted by `run_pipeline_in_process._run_plan_phase` "
        "because the substrate's plan producers did not land the canonical "
        "plan artifact themselves. Inspect the per-producer + reviewer "
        "diagnostics above and the BRC snapshot to decide retry/abort at the "
        "plan HITL gate."
    )
    return "\n".join(lines) + "\n"


def _render_role_diagnostics(role_name: str, producer_results: Mapping[Any, Any]) -> str:
    """Render the per-role diagnostics block (exit code + commit + stdout)."""
    result = next(
        (r for k, r in producer_results.items() if getattr(k, "value", str(k)) == role_name),
        None,
    )
    if result is None:
        return f"### {role_name}\n\n- <no spawn result recorded>\n"
    if isinstance(result, Exception):
        return f"### {role_name}\n\n- exception: {result!r}\n"
    exit_code = int(getattr(result, "exit_code", 0) or 0)
    commit_sha = getattr(result, "commit_sha", None)
    stdout = (getattr(result, "stdout", "") or "")[:500]
    return (
        f"### {role_name}\n\n"
        f"- exit_code: {exit_code}\n"
        f"- commit_sha: {commit_sha or '<none>'}\n"
        f"- stdout (truncated):\n\n```\n{stdout}\n```\n"
    )


def synthetic_commit_for(role_name: str) -> str:
    """Return a per-role synthetic commit SHA.

    Reviewer_concurrency v1 non-blocking #2: derive a 7-hex SHA
    from a SHA-1 of the role name so per-producer ProposalPayload
    entries remain distinguishable. The ``ace1`` prefix keeps the
    string obviously synthetic in log output. Must never escape
    the in-process driver — see ``_SYNTHETIC_PLAN_COMMIT`` docstring.
    """
    import hashlib

    digest = hashlib.sha1(role_name.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"ace1{digest[:3]}"


def log_tracker_warning(verb: str, role_label: str, exc: Exception, pipeline_id: str) -> None:
    """Log a tracker-guard rejection at WARNING.

    Reviewer_code_holistic v1 non-blocking: bare ``except Exception:
    pass`` around tracker.handle_* calls silently discards root
    cause when the eval snapshot's ``blocking_agents`` only surfaces
    the symptom. v2 logs the verb + role + exception so an operator
    debugging a stuck plan gate gets a structured breadcrumb.
    """
    try:
        import logging

        logger = logging.getLogger("orchestrator.substrate.in_process")
        logger.warning(
            "plan-phase tracker.%s rejected for %s (pipeline_id=%s): %s",
            verb,
            role_label,
            pipeline_id,
            exc,
        )
    except Exception:  # noqa: BLE001 — defensive
        pass

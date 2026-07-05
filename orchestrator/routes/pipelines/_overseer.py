"""overseer detection-plane helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any  # noqa: F401

import routes.pipelines as _pkg  # noqa: E402,F401
from models import Pipeline, PipelinePhase, PipelineStatus  # noqa: F401

if TYPE_CHECKING:
    from overseer.corrective import CorrectiveExecutor  # noqa: F401
    from overseer.decision_maker import AdjudicationVerdict  # noqa: F401

    try:
        from ..container_spawner import ContainerSpawner  # noqa: F401
    except ImportError:  # pragma: no cover
        from container_spawner import ContainerSpawner  # type: ignore  # noqa: F401

    try:
        from ..kubernetes_spawner import SpawnedContainer  # noqa: F401
    except ImportError:  # pragma: no cover
        from kubernetes_spawner import SpawnedContainer  # type: ignore  # noqa: F401


def _spawn_overseer_agent(
    *,
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    issue_number: int | None,
    gateway_mode: str,
    pipeline_repos: list | None,
    max_turns: int,
    decision_model: str = "sonnet",
    prompt_override: str | None = None,
) -> "SpawnedContainer":  # noqa: UP037
    """Spawn the overseer as a normal agent (#2270 §1.5).

    The overseer is just a particular agent — it goes through the generic
    :meth:`ContainerSpawner.spawn_agent_job` path with a command built by
    ``build_agent_command`` exactly like every other role. There is no bespoke
    spawn method, no ``EGG_OVERSEER_*`` env, and no baked-in
    ``overseer_monitor.py`` bootstrap (that trust-and-run script was the direct
    cause of the §1 self-injection loop). Monitoring arrives via the agent's
    normal MCP tools / the ``egg-orch`` CLI.

    The overseer's model tier resolves through ``resolve_overseer_model`` (Opus
    by default, #2270 §1 / folds #2813); the deprecated
    ``overseer_decision_maker_model`` (passed as ``decision_model``) is inert and
    only warns. A resolver regression degrades to the built-in opus/anthropic
    default rather than crashing spawn.
    """
    from agent_model_resolution import (
        DEFAULT_AGENT_MODEL,
        UPSTREAM_ANTHROPIC,
        classify_model,
        resolve_overseer_model,
    )
    from egg_agent import build_agent_command

    try:
        from ..models import AgentRole
    except ImportError:
        from models import AgentRole  # type: ignore[no-redef]

    # The overseer resolves its model from the pipeline's PRIMARY repo.
    # ``pipeline_repos`` is canonically primary-first (#3393 slices 1-2), so
    # take the first (primary) entry via ``next(iter(...))`` rather than a
    # positional ``[0]`` collapse (#3393 slice-3).
    overseer_repo = next(iter(pipeline_repos or []), None)
    try:
        overseer_decision = resolve_overseer_model(
            "adversarial",
            pipeline_config=None,
            repo=overseer_repo,
        )
    except Exception as resolve_err:  # noqa: BLE001 — degrade, don't crash
        _pkg.logger.warning(
            "Failed to resolve overseer model decision for spawn; "
            "falling back to built-in opus / anthropic default",
            error=str(resolve_err),
        )
        overseer_decision = classify_model(DEFAULT_AGENT_MODEL)

    # The bespoke ``overseer_decision_maker_model`` no longer drives the spawn.
    # Warn if an operator still sets it to a non-default value (#2270 §1 / #2813).
    if decision_model and decision_model != "sonnet":
        _pkg.logger.warning(
            "overseer_decision_maker_model=%r is deprecated and no longer "
            "drives the overseer spawn; the base model now resolves via "
            "resolve_agent_model(OVERSEER) -> %s. Set agent_models['overseer'] "
            "to override. See #2270 §1 / #2813.",
            decision_model,
            overseer_decision.claude_code_alias,
        )

    # #2270 §1.5: no bespoke ``EGG_OVERSEER_*`` env — only the generic
    # ``BASH_COMMAND_TIMEOUT`` (long-poll CLI calls) and the resolved-decision
    # model env (custom-model registration + context guardrails, #2832/#3175).
    extra_env = {
        "BASH_COMMAND_TIMEOUT": "0",
        **overseer_decision.env_vars(),
    }

    # Monitoring arrives via MCP tools / the ``egg-orch`` CLI, not a baked-in
    # script the agent is told to trust and run. The prompt describes the
    # observe→classify→alert loop and leaves the mechanics to the agent's tools.
    # When ``prompt_override`` is set (the #2270 slice-4 on-demand adjudicator),
    # use it verbatim — a single-shot adjudication of one finding, not the
    # continuous monitoring loop.
    default_overseer_prompt = (
        f"You are the overseer agent for pipeline {pipeline_id}. You are a "
        "normal egg agent with read-only monitoring permissions: there is no "
        "baked-in script to run and no pre-built monitoring loop to trust. "
        "Observe pipeline health using your MCP tools and the `egg-orch` CLI, "
        "and surface only genuine anomalies.\n\n"
        "Loop until the pipeline reaches a terminal state (complete, failed, or "
        "cancelled):\n"
        "1. Read the live pipeline state (`mcp__progress__query_status` or "
        "`egg-orch pipeline status`), the BRC consensus matrix "
        "(`mcp__brc__get_state`), and recent agent messages.\n"
        "2. Classify what you see. The overwhelming majority of observations "
        "are normal — only a wedged phase transition, a real consensus "
        "deadlock, repeated agent crashes, or similar genuine failures warrant "
        "action.\n"
        "3. When (and only when) you find a real problem, broadcast a single "
        "OVERSEER_ALERT with `mcp__progress__overseer_alert`, setting priority "
        "by severity and naming the anomaly, the evidence, and a recommended "
        "operator action.\n"
        "4. Otherwise wait briefly and repeat.\n\n"
        "Be conservative: a false alarm trains operators to ignore you, so "
        "prefer silence over a low-confidence alert. When the pipeline ends, "
        "emit a final health summary."
    )
    overseer_prompt = prompt_override or default_overseer_prompt
    command = build_agent_command(
        prompt=overseer_prompt,
        model=overseer_decision.claude_code_alias,
        max_turns=max_turns,
        effort=overseer_decision.effort,
    )

    spawn_kwargs: dict[str, _pkg.Any] = {
        "pipeline_id": pipeline_id,
        "agent_role": AgentRole.OVERSEER,
        "issue_number": issue_number,
        "repo_volumes": None,
        "mode": gateway_mode,
        "extra_env": extra_env,
        "repos": pipeline_repos if pipeline_repos else None,
        "command": command,
    }
    # Forward per-agent upstream routing only when it would change behavior, so
    # the default Anthropic overseer keeps the pre-#2769 call signature (mirrors
    # ``concurrent_executor._spawn_agent``).
    if (
        overseer_decision.upstream != UPSTREAM_ANTHROPIC
        or overseer_decision.upstream_model is not None
    ):
        spawn_kwargs["upstream"] = overseer_decision.upstream
        spawn_kwargs["upstream_model"] = overseer_decision.upstream_model

    return spawner.spawn_agent_job(**spawn_kwargs)


def _consume_adjudicator_verdict(spawned: _pkg.Any, finding: _pkg.Any) -> "AdjudicationVerdict":  # noqa: UP037
    """Consume the structured verdict an on-demand adjudicator produced.

    Best-effort and defensive (#2270 slice-4). The adjudicator is a NORMAL
    spawned agent; its structured verdict reaches the orchestrator either inline
    on the spawn result (when a synchronous runner surfaces it as
    ``adjudication_verdict`` / ``result_text``) or out-of-band. When no verdict
    is available yet, we degrade to a conservative *defer-to-operator* verdict so
    a genuine deadlock is never silently dropped — the slice-6 authority plane
    executes on whatever this returns.
    """
    from overseer.decision_maker import parse_adjudication_verdict

    raw: _pkg.Any = None
    for attr in ("adjudication_verdict", "result_text", "stdout"):
        value = getattr(spawned, attr, None)
        if value:
            raw = value
            break
    return parse_adjudication_verdict(raw, finding=finding)


def _overseer_should_be_present(
    *, running_agent_count: int, pipeline_status: PipelineStatus
) -> bool:
    """Gate overseer presence on agents actually running (#2270 slice-5, §3).

    Decisive rules (the tester contract pins these exactly):

    * ``running_agent_count <= 0`` ⇒ ``False`` regardless of status — the §3
      guarantee that a multi-hour *zero-agent* HITL park spawns no overseer.
    * a terminal pipeline status (``COMPLETE`` / ``FAILED`` / ``CANCELLED``) ⇒
      ``False`` regardless of the count — nothing left to monitor.
    * otherwise (agents in flight, non-terminal) ⇒ ``True``.

    The overseer is only useful while a phase is actively executing agents, so
    presence tracks "are there agents to watch", not the phase calendar.
    """
    if running_agent_count <= 0:
        return False
    if pipeline_status in (
        PipelineStatus.COMPLETE,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    ):
        return False
    return True


def _count_phase_agents(pipeline: Pipeline, phase: PipelinePhase) -> int:
    """Count the agents a phase is about to run (#2270 slice-5 roster source).

    Prefers the runtime roster cached on the phase execution (populated once
    the phase has spawned); falls back to the deterministic
    ``get_roles_for_phase`` source the concurrent executor itself consults, so
    a not-yet-spawned phase still reports its imminent cohort. A derivation
    failure returns 0 — conservatively *no* overseer rather than guessing,
    which keeps the §3 "no overseer with zero agents" invariant safe.
    """
    phase_exec = pipeline.phases.get(phase)
    if phase_exec is not None and getattr(phase_exec, "agents", None):
        return len(phase_exec.agents)
    try:
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase(
            phase.value,
            include_reviewers=True,
            repo=pipeline.repo,
            has_contract=getattr(pipeline, "has_contract", True),
        )
        return len(list(roles))
    except Exception as exc:  # noqa: BLE001 - roster derivation is best-effort
        _pkg.logger.debug(
            "Could not derive phase roster for overseer presence gate",
            pipeline_id=getattr(pipeline, "id", None),
            phase=getattr(phase, "value", str(phase)),
            error=str(exc),
        )
        return 0


def _escalate_finding_to_adjudicator(
    finding: _pkg.Any,
    *,
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    issue_number: int | None,
    gateway_mode: str,
    pipeline_repos: list | None,
    max_turns: int = 3,
    spawn_overseer: _pkg.Any = None,
    consume_verdict: _pkg.Any = None,
) -> "AdjudicationVerdict | None":  # noqa: UP037
    """Escalate a finding to an on-demand OVERSEER adjudicator (#2270 slice-4).

    The escalation→adjudicator path is the ONLY thing the orchestrator-side
    overseership spends an agent on. The gate is strict:

    * a finding **without** ``requires_adjudication`` returns ``None`` and NEVER
      spawns an adjudicator — the routine majority is handled deterministically;
    * a finding **with** ``requires_adjudication`` spawns a NORMAL on-demand
      OVERSEER agent (the slice-3 normalized spawn, Opus via the slice-2
      resolver) with a one-shot adjudication prompt, and the orchestrator
      consumes its structured verdict in-process.

    ``spawn_overseer`` / ``consume_verdict`` are injectable seams so the path is
    unit-testable without a live container; they default to
    :func:`_spawn_overseer_agent` and :func:`_consume_adjudicator_verdict`.
    """
    if not getattr(finding, "requires_adjudication", False):
        return None  # routine finding — deterministic handling, no agent spend

    from overseer.decision_maker import build_adjudication_prompt

    spawn = spawn_overseer or _pkg._spawn_overseer_agent
    consume = consume_verdict or _pkg._consume_adjudicator_verdict

    prompt = build_adjudication_prompt(finding)
    spawned = spawn(
        spawner=spawner,
        pipeline_id=pipeline_id,
        issue_number=issue_number,
        gateway_mode=gateway_mode,
        pipeline_repos=pipeline_repos if pipeline_repos else None,
        max_turns=max_turns,
        prompt_override=prompt,
    )
    verdict = consume(spawned, finding)
    _pkg.logger.info(
        "Overseer adjudicated finding",
        pipeline_id=pipeline_id,
        finding_class=getattr(finding, "finding_class", "?"),
        confirmed=getattr(verdict, "confirmed", None),
        recommended_action=getattr(verdict, "recommended_action", None),
    )
    return verdict


def _run_overseer_detection_plane(
    snapshot: _pkg.Any,
    *,
    spawner: "ContainerSpawner",  # noqa: UP037
    pipeline_id: str,
    issue_number: int | None,
    gateway_mode: str,
    pipeline_repos: list | None,
    plane: _pkg.Any = None,
    max_turns: int = 3,
) -> "list[tuple[Any, AdjudicationVerdict | None]]":  # noqa: UP037
    """Evaluate the detection plane and escalate only findings that need it.

    The orchestrator-side overseership spine (#2270 Option C, slice-4): run the
    deterministic detectors over ``snapshot`` (no LLM), then escalate ONLY the
    findings carrying ``requires_adjudication`` to the on-demand adjudicator.
    Returns ``(finding, verdict)`` pairs — ``verdict`` is ``None`` for routine
    findings that were handled deterministically without an agent.

    The default plane already carries the slice-8 §5 coverage-gap detectors
    (registered in :meth:`DetectionPlane.default`), so production runs the full
    detector set without any wiring here.
    """
    from health_checks.detection_plane import default_detection_plane, escalate_findings

    active_plane = plane or default_detection_plane()
    findings = active_plane.evaluate(snapshot)

    results: list[tuple[_pkg.Any, _pkg.Any]] = []

    def _spawn_adjudicator(finding: _pkg.Any) -> _pkg.Any:
        verdict = _pkg._escalate_finding_to_adjudicator(
            finding,
            spawner=spawner,
            pipeline_id=pipeline_id,
            issue_number=issue_number,
            gateway_mode=gateway_mode,
            pipeline_repos=pipeline_repos,
            max_turns=max_turns,
        )
        results.append((finding, verdict))
        return verdict

    # The canonical gate (health_checks.detection_plane.escalate_findings) calls
    # the spawn callback exactly once per requires_adjudication finding and never
    # for routine ones — a single source of truth shared with the tester contract.
    escalate_findings(findings, spawn_adjudicator=_spawn_adjudicator)
    return results


def _send_brc_confirmation_nudge(
    escalation: dict[str, _pkg.Any],
    pipeline_id: str,
    phase: str | None,
) -> bool:
    """Wake a producer stuck post-ACK with a directed OVERSEER_ALERT (#2079).

    Wired as an escalation callback for HealthMonitor's
    ``brc_confirmation_timeout`` alert.  The deterministic detector in
    ``check_brc_progress`` knows the exact remediation, so we deliver
    it directly to the stuck producer rather than relying on the
    overseer agent's discretion.

    Uses ``OVERSEER_ALERT`` (not ``STATUS`` or ``NUDGE``) because it
    appears in **both** the producer's pre-confirm wait_loop filter
    (``CONSENSUS_ACK,CONSENSUS_NACK,CONSENSUS_RE_REVIEW,STATUS,OVERSEER_ALERT``,
    post-#2531) and post-confirm wait_loop filter
    (``CONSENSUS_CONFIRMED,CONSENSUS_RE_REVIEW,OVERSEER_ALERT``) and has
    no protocol-specific semantics that would conflict with a producer
    nudge — ``CONSENSUS_RE_REVIEW`` is also in both filters but means
    "a peer re-proposed; re-review their artifact," not "you are
    wedged; confirm." ``STATUS`` is in the pre-confirm filter (it
    carries the orchestrator's *Ready to confirm* nudge) but not the
    post-confirm filter, so it wouldn't reach a producer wedged after
    a successful confirm. A wedged producer is in the
    ``fully_acked but not confirmed`` set, which means they are most
    likely blocked on the pre-confirm wait. The subject calls out
    that the alert originated from the orchestrator's deterministic
    detector rather than the overseer agent.

    Returns True when a message was posted, False otherwise (wrong
    alert type, missing fields, message store unavailable, send error).
    """
    if escalation.get("alert_type") != "brc_confirmation_timeout":
        return False

    producer = escalation.get("agent_id")
    if not producer:
        return False

    elapsed = escalation.get("elapsed_seconds")
    # check_brc_progress always populates elapsed_seconds; treat
    # missing or non-positive values as a malformed escalation rather
    # than rendering "have not confirmed in 0s" in the body.
    if elapsed is None or elapsed <= 0:
        return False

    store_fn = _pkg._get_message_store()
    if store_fn is None:
        return False

    # _get_message_store already verified the package is importable;
    # Message/MessageType live in the same module so a defensive
    # try/except here would only add per-call import overhead.
    from message_store import Message, MessageType

    body = (
        f"You are PROPOSED and fully ACKed but have not confirmed in "
        f"{elapsed}s. Call `mcp__brc__confirm` now. If it returns "
        "`status='pending_acks'`, read `message` for the guard reason and "
        "wait on the prerequisite events instead: `CONSENSUS_PROPOSE` if a "
        "producer hasn't proposed (`zero_proposal_producers`), "
        "`CONSENSUS_ACK` / `CONSENSUS_RE_REVIEW` if a reviewer's ACK is "
        "stale or unresolved. Then retry confirm."
    )

    try:
        msg_store = store_fn()
        # Bypass the POST /messages/send route on purpose: this is an
        # orchestrator-internal nudge, and we do not want HealthMonitor's
        # MESSAGE_SENT handler (rate-limit + HEARTBEAT tracking) to see it.
        # Future audit/observability subscribers should be aware this path
        # does not emit EventType.MESSAGE_SENT.
        msg_store.add_message(
            Message(
                pipeline_id=pipeline_id,
                from_role="orchestrator",
                to_role=producer,
                message_type=MessageType.OVERSEER_ALERT,
                subject="BRC confirmation timeout — call mcp__brc__confirm",
                body=body,
                phase=phase,
                metadata={
                    "alert_type": "brc_confirmation_timeout",
                    "elapsed_seconds": elapsed,
                    "source": "health_monitor",
                },
            )
        )
        _pkg.logger.info(
            "Sent BRC confirmation-timeout nudge",
            pipeline_id=pipeline_id,
            producer=producer,
            elapsed_seconds=elapsed,
        )
        return True
    except Exception as send_err:
        _pkg.logger.warning(
            "Failed to send BRC confirmation-timeout nudge (non-fatal)",
            pipeline_id=pipeline_id,
            producer=producer,
            error=str(send_err),
        )
        return False


def _corrective_open_operator_hitl(
    *,
    pipeline_id: str,
    issue_number: int | None = None,
    repo_path: _pkg.Any = None,
    question: str | None = None,
    options: _pkg.Any = None,
    finding: _pkg.Any = None,
    phase: str | None = None,
    **_: _pkg.Any,
) -> str:
    """``open_operator_hitl`` seam: open a HITL contract decision (orchestrator id).

    The decision is written via :func:`apply_mutation` under ``Role.IMPLEMENTER``
    — the same ``decisions.*`` owner the ``register_open_question`` MCP tool and
    the impasse router use — with an orchestrator-side actor so the audit trail
    stays distinct from agent-authored decisions. This is the REAL enforcement
    point: the contract write runs as the control plane (which has no gateway
    agent pattern), while agents — incl. the overseer — stay blocked from
    ``.egg-state/contracts/``. Returns the new decision id.
    """
    from egg_contracts.decisions import next_cq_id
    from egg_contracts.loader import load_contract, save_contract
    from egg_contracts.models import Decision, DecisionOption, DecisionType
    from egg_contracts.roles import Role
    from egg_contracts.validator import apply_mutation

    identifier = _pkg._pipeline_identifier(issue_number, pipeline_id)
    resolved_repo = repo_path or _pkg.get_repo_path()
    contract = load_contract(identifier, resolved_repo)
    existing = contract.decisions or []
    next_idx = len(existing)
    decision_id = next_cq_id(existing)

    finding_class = str(getattr(finding, "finding_class", "") or "")
    severity = str(getattr(finding, "severity", "") or "medium")

    if question:
        question_text = question
    else:
        lines = [
            f"The overseer detection plane flagged ``{finding_class or 'an anomaly'}`` "
            f"(severity ``{severity}``) in pipeline ``{pipeline_id}`` and the on-demand "
            "adjudicator escalated it for operator judgement.",
        ]
        evidence = getattr(finding, "evidence", None)
        if evidence:
            lines.append(f"**Evidence**: {evidence}")
        question_text = "\n".join(lines)

    if options:
        decision_options = [
            DecisionOption(id=f"opt-{i + 1}", label=str(label)) for i, label in enumerate(options)
        ]
    else:
        decision_options = [
            DecisionOption(id="opt-1", label="Intervene now (operator will act manually)"),
            DecisionOption(id="opt-2", label="Dismiss — detector over-fired (calibration data)"),
            DecisionOption(id="opt-3", label="Other (explain in reply)"),
        ]

    decision = Decision(
        id=decision_id,
        question=question_text,
        type=DecisionType.HITL,
        phase=contract.current_phase,
        options=decision_options,
    )
    result = apply_mutation(
        contract,
        role=Role.IMPLEMENTER,
        actor="orchestrator-overseer-corrective",
        field_path=f"decisions.{next_idx}",
        new_value=decision,
        reason=f"Overseer corrective: open operator HITL for {finding_class or 'finding'}",
    )
    if not result.success:
        raise RuntimeError(f"failed to open operator HITL decision: {result.message}")
    save_contract(contract, resolved_repo)
    # NOTE(#3427): like ``route_impasses``, this overseer-corrective writer
    # lands the ``cq-N`` decision with a bare ``save_contract`` and no
    # write-time ``persist_contract_statefiles`` — so a HITL opened between
    # checkpoints shares the same phase-restart volatility window (the
    # ``git reset --hard origin/<work>`` can revert it). The append-only
    # guard protects it from id reuse, but not from reversion. Not persisted
    # here because the corrective seam runs against ``get_repo_path()`` (the
    # base repo), not a pushable pipeline worktree — wiring a worktree-scoped
    # persist through the CorrectiveExecutor is the residual follow-up.
    return decision_id


def _corrective_nudge_agent(
    *,
    pipeline_id: str,
    target_role: str | None = None,
    phase: str | None = None,
    finding: _pkg.Any = None,
    escalation: dict[str, _pkg.Any] | None = None,
    **_: _pkg.Any,
) -> bool:
    """``nudge_agent`` seam: deliver the deterministic BRC-confirmation nudge.

    Wires to :func:`_send_brc_confirmation_nudge` (the #2079 directed wake), which
    posts an ``OVERSEER_ALERT`` the stuck producer's wait-loop filters admit. An
    explicit ``escalation`` dict is used when present, otherwise synthesized in
    the ``brc_confirmation_timeout`` shape that helper requires. Returns whether
    the nudge was delivered.
    """
    payload = dict(escalation or {})
    payload.setdefault("alert_type", "brc_confirmation_timeout")
    payload.setdefault("agent_id", target_role)
    elapsed = payload.get("elapsed_seconds")
    payload["elapsed_seconds"] = elapsed if (elapsed and elapsed > 0) else 1
    return _pkg._send_brc_confirmation_nudge(payload, pipeline_id, phase)


def _corrective_respawn_cohort(
    *,
    pipeline_id: str,
    target_role: str | None = None,
    reason: str | None = None,
    **_: _pkg.Any,
) -> bool:
    """``respawn_cohort`` seam: restart the target role(s) via the general path.

    Delegates to the orchestrator's public restart endpoint
    (``POST /agents/<role>/restart``) — the same general-restart machinery the
    overseer monitor's ``_execute_restart_agent`` uses — so restart-budget
    enforcement, consensus reset, and one-shot Job teardown all happen
    server-side, with no bespoke respawn plumbing. ``target_role`` may be a single
    role or a comma-separated cohort. Returns whether every role restarted.
    """
    import urllib.request
    from urllib.parse import quote

    roles = [r.strip() for r in str(target_role or "").split(",") if r.strip()]
    if not roles:
        raise RuntimeError("respawn_cohort: empty target cohort")

    orchestrator_url = _pkg.os.environ.get("EGG_ORCHESTRATOR_URL", "http://localhost:9849")
    restart_reason = (reason or "overseer corrective respawn")[:500]
    for role in roles:
        restart_url = (
            f"{orchestrator_url}/api/v1/pipelines/"
            f"{quote(pipeline_id, safe='')}/agents/{quote(role, safe='')}/restart"
        )
        req = urllib.request.Request(
            restart_url,
            data=_pkg.json.dumps({"reason": restart_reason}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=60) as resp:
            result = _pkg.json.loads(resp.read().decode())
        if not result.get("success"):
            raise RuntimeError(
                f"restart of {role!r} failed: {result.get('message', 'unknown error')}"
            )
    return True


def _build_overseer_corrective_executor(
    *,
    issue_number: int | None = None,
    repo_path: _pkg.Any = None,
    config: _pkg.Any = None,
    audit_sink: _pkg.Any = None,
    open_operator_hitl: _pkg.Any = None,
    nudge_agent: _pkg.Any = None,
    respawn_cohort: _pkg.Any = None,
) -> "CorrectiveExecutor":  # noqa: UP037
    """Construct the §4 :class:`CorrectiveExecutor` wired to the production seams.

    Seams are injectable so the path stays unit-testable without a live
    orchestrator. The default ``open_operator_hitl`` seam is bound to the
    pipeline's ``issue_number`` / ``repo_path`` so it can resolve the contract.
    The rate-limit window derives from the overseer config when present, falling
    back to the executor default.
    """
    from overseer.corrective import CorrectiveExecutor

    def _default_open_hitl(**kwargs: _pkg.Any) -> str:
        kwargs.setdefault("issue_number", issue_number)
        kwargs.setdefault("repo_path", repo_path)
        return _pkg._corrective_open_operator_hitl(**kwargs)

    kwargs: dict[str, _pkg.Any] = {}
    window = getattr(config, "overseer_infra_error_dedup_window_seconds", None)
    if isinstance(window, int) and window > 0:
        kwargs["window_seconds"] = float(window)

    return CorrectiveExecutor(
        open_operator_hitl=open_operator_hitl or _default_open_hitl,
        nudge_agent=nudge_agent or _pkg._corrective_nudge_agent,
        respawn_cohort=respawn_cohort or _pkg._corrective_respawn_cohort,
        audit_sink=audit_sink,
        **kwargs,
    )


def _execute_overseer_verdicts(
    results: list[tuple[_pkg.Any, _pkg.Any]],
    *,
    pipeline_id: str,
    issue_number: int | None,
    running_agent_count: int,
    phase: str | None = None,
    executor: _pkg.Any = None,
) -> list[_pkg.Any]:
    """Run the §4 authority plane over adjudicated ``(finding, verdict)`` pairs.

    For each pair carrying a verdict, dispatch the recommended action through the
    :class:`CorrectiveExecutor`. The executor enforces the closed vocabulary (a
    ``none`` recommendation is skipped here as the non-executable no-op), the
    zero-agent-park bar, rate-limiting, idempotency, and audit logging. Returns
    the per-verdict :class:`CorrectiveOutcome` list (empty when nothing was
    adjudicated or actioned).
    """
    active = executor or _pkg._build_overseer_corrective_executor(issue_number=issue_number)
    outcomes: list[_pkg.Any] = []
    for finding, verdict in results:
        if verdict is None:
            continue  # routine finding — handled deterministically, no action
        action = str(getattr(verdict, "recommended_action", "") or "").strip()
        if action in ("", "none"):
            continue  # adjudicator advised no action — nothing to execute
        evidence = getattr(finding, "evidence", None) or {}
        target_role = str(getattr(verdict, "target", "") or "") or str(
            evidence.get("agent_role") or evidence.get("agent_id") or ""
        )
        finding_class = str(getattr(finding, "finding_class", "") or "")
        outcomes.append(
            active.execute(
                action,
                pipeline_id=pipeline_id,
                running_agent_count=running_agent_count,
                phase=phase,
                target_role=target_role,
                finding=finding,
                idempotency_key=f"{finding_class}:{target_role}" if finding_class else None,
            )
        )
    return outcomes


def _teardown_phase_overseer(
    spawner: "ContainerSpawner",  # noqa: UP037
    container_id: str,
    pipeline_id: str,
    phase_label: str,
    reason: str,
) -> None:
    """Stop the phase-scoped overseer container.

    Caller is responsible for holding ``overseer_lock`` and setting
    ``phase_overseer_active = False`` before this call.
    """
    try:
        spawner.stop_agent_container(
            container_id,
            cleanup_session=True,
            timeout=10,
        )
        _pkg.logger.info(
            f"Overseer container stopped ({reason})",
            pipeline_id=pipeline_id,
            phase=phase_label,
            container_id=container_id[:12],
        )
    except Exception as overseer_err:
        _pkg.logger.debug(
            f"Failed to stop overseer container ({reason})",
            pipeline_id=pipeline_id,
            error=str(overseer_err),
        )

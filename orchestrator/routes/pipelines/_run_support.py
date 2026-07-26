"""run-loop support helpers helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import routes.pipelines as _pkg  # noqa: E402,F401

if TYPE_CHECKING:
    from egg_container import MountSpec  # noqa: F401
    from egg_contracts.agent_roles import AgentRole as ContractAgentRole  # noqa: F401


def _clear_stale_impasses_for_producers(
    repo_path: _pkg.Path,
    pipeline_id: str,
    producer_roles: "list[ContractAgentRole]",  # noqa: UP037
    *,
    cleanup_reason: str,
) -> None:
    """Drop the ``impasse`` field from each producer's per-pipeline
    agent-output file before the next BRC cycle.

    ``save_agent_output`` writes with ``mode="w"`` so a producer that
    respawns and reaches its handoff write will overwrite the stale
    impasse on its own. But if a producer crashes before writing in the
    next iteration (or if the implement roster ever becomes
    contract-task-driven, in which case a producer with no remaining
    tasks won't spawn at all), the iter-N impasse file would persist
    into iter-N+1's ``collect_impasses`` scan and re-trigger routing on
    a stale signal — which the ``delegation_attempts`` counter would
    then translate into a spurious "second impasse on same task" HITL
    escalation.

    Pre-clearing the field keeps ``collect_impasses`` honest about what
    came out of the *current* iteration only. Other top-level fields on
    the agent output (``handoff_data``, ``role``, anything else) are
    preserved.
    """
    for role_enum in producer_roles:
        try:
            existing = _pkg.load_agent_output(repo_path, role_enum, identifier=pipeline_id)
        except Exception as exc:  # noqa: BLE001
            # Best-effort agent-output file read. Catches OSError on
            # the file read, json.JSONDecodeError on parse, and
            # pydantic.ValidationError on the role-specific shape.
            # Continue (no impasse to clear if the file is unreadable).
            _pkg.logger.debug(
                "Could not pre-load agent output to clear stale impasse",
                pipeline_id=pipeline_id,
                role=role_enum.value,
                error=str(exc),
            )
            continue
        if not isinstance(existing, dict) or "impasse" not in existing:
            continue
        cleaned = {k: v for k, v in existing.items() if k != "impasse"}
        try:
            _pkg.save_agent_output(
                repo_path,
                role_enum,
                cleaned,
                identifier=pipeline_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Atomic file write of JSON-serialisable dict. Catches
            # OSError (write/rename), TypeError/ValueError (non-
            # serialisable value sneaking in). Continue — the stale
            # impasse will re-trigger routing but the delegation
            # counter still bounds the retry.
            _pkg.logger.warning(
                "Failed to clear stale impasse from agent output",
                pipeline_id=pipeline_id,
                role=role_enum.value,
                error=str(exc),
            )
            continue
        _pkg.logger.info(
            "Cleared stale impasse from agent output",
            pipeline_id=pipeline_id,
            role=role_enum.value,
            cleanup_reason=cleanup_reason,
        )


def _pipeline_superseded_by_restart(
    store, pipeline_id: str, run_epoch: _pkg.datetime | None
) -> bool:
    """True if a newer ``run_epoch`` means another thread now owns this pipeline.

    Reloads pipeline state and compares its ``run_epoch`` against the epoch the
    caller runs under (#3315 facet a). Best-effort: a missing epoch or a load
    failure returns ``False`` so a transient store hiccup never tears down a
    legitimately-running phase. Shared by the ``_run_concurrent_phase`` poll
    loop and the slice-path impasse-retry wrapper so the "no escalation when
    superseded" property holds on both routes.
    """
    if store is None or run_epoch is None:
        return False
    try:
        _epoch_pip = store.load_pipeline(pipeline_id)
    except Exception as _epoch_err:  # noqa: BLE001 — never wedge the caller
        _pkg.logger.debug(
            "Epoch supersession check failed; continuing",
            pipeline_id=pipeline_id,
            error=str(_epoch_err),
        )
        return False
    current_epoch = _epoch_pip.run_epoch or _epoch_pip.created_at
    return current_epoch != run_epoch


def _spawn_and_wait(
    spawner,
    pipeline_id: str,
    agent_role: _pkg.AgentRole,
    issue_number: int | None,
    repo_volumes: dict[str, str],
    gateway_mode: str,
    repos: list[str],
    phase: str,
    sandbox_env: dict[str, str],
    sandbox_command: list[str],
    timeout: int = 3600,
    store=None,
    certs_volume: str | None = None,
    branch: str | None = None,
    extra_mounts: list["MountSpec"] | None = None,  # noqa: UP037
    spawn_max_retries: int | None = None,
    spawn_retry_initial_backoff_seconds: float | None = None,
) -> tuple[int, str]:
    """Spawn a container, wait for it to exit, clean up, return (exit_code, logs).

    If ``store`` is provided, the container is recorded in the phase execution
    state so that the status endpoint can report it while it runs.

    The container is launched via the shared ``build_sandbox_config()`` path,
    which handles GATEWAY_URL, proxy vars, DNS lockdown, extra_hosts, and
    .git shadow mounts automatically.

    Args:
        repo_volumes: Mapping of repo_name -> host_path for volume mounts.
            Each entry is mounted at /home/egg/repos/<name> in the container,
            with .git shadowed by /dev/null bind mounts to force gateway git operations.
        certs_volume: Docker named volume for gateway CA certs (mounted at
            /shared/certs read-only). If None, certs are not mounted.
        spawn_max_retries: Override for spawn retry attempts (None uses spawner default).
        spawn_retry_initial_backoff_seconds: Override for initial backoff (None uses spawner default).

    Returns:
        (exit_code, container_logs) — logs are captured before cleanup on failure.
    """
    try:
        from agent_model_resolution import DEFAULT_AGENT_MODEL
    except ImportError:
        from ..agent_model_resolution import (  # type: ignore[import-not-found, no-redef]
            DEFAULT_AGENT_MODEL,
        )

    retry_kwargs: dict = {}
    if spawn_max_retries is not None:
        retry_kwargs["spawn_max_retries"] = spawn_max_retries
    if spawn_retry_initial_backoff_seconds is not None:
        retry_kwargs["spawn_retry_initial_backoff_seconds"] = spawn_retry_initial_backoff_seconds

    # NOTE: this helper only supports the default Anthropic auth path. It
    # does not forward ``upstream``/``upstream_model``, so ``spawn_agent_job``
    # falls back to the Anthropic branch and injects the session-token
    # placeholder into ``CLAUDE_CODE_OAUTH_TOKEN`` (#2817). It has no
    # production callers today (only test references). If this path is ever
    # revived for a LiteLLM agent, plumb ``upstream``/``upstream_model``
    # through here — otherwise Claude Code would send ``x-api-key`` (api_key
    # auth) while the placeholder lands in the OAuth header, leaving the
    # credential header empty and the session unresolvable.
    spawned = spawner.spawn_agent_job(
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        issue_number=issue_number,
        mode=gateway_mode,
        wait_for_gateway=False,
        repos=repos,
        phase=phase,
        extra_env=sandbox_env,
        command=sandbox_command,
        repo_volumes=repo_volumes,
        branch=branch,
        extra_mounts=extra_mounts,
        jira_ticket=(sandbox_env.get("EGG_JIRA_TICKET") or None),
        **retry_kwargs,
    )

    # Record container and agent in phase execution state
    if store is not None:
        try:
            from models import AgentExecution

            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(_pkg.PipelinePhase(phase))

                # Track container — preserve backend-specific fields
                # (pod_name, namespace, job_name on K8s) from the spawner.
                container_info = spawned.container_info.model_copy(
                    update={
                        "status": _pkg.ContainerStatus.RUNNING,
                        "started_at": _pkg.datetime.now(_pkg.UTC),
                        "agent_role": agent_role,
                    }
                )
                phase_execution.containers.append(container_info)

                # Track agent execution.
                #
                # ``slice_id`` is explicitly ``None`` because this helper has
                # no production callers today and is reachable only from
                # tests that mock-patch it. If a future change resurrects
                # this path for a sliced spawn, the caller MUST plumb a
                # ``slice_id`` through here — otherwise the new
                # ``(role, slice_id)`` walks added in #2422 will not see
                # the record. See PR #2435 review thread.
                # This helper hard-codes the default Anthropic auth path (see
                # the NOTE above ``spawn_agent_job``), so the resolved model is
                # always the built-in default alias. Stamp it for parity with
                # ``_run_concurrent_phase`` / ``restart_agent`` (#3174) — if this
                # test-only path is ever resurrected for production it will not
                # silently regress resolved-model visibility.
                agent_execution = AgentExecution(
                    role=agent_role,
                    status=_pkg.AgentExecutionStatus.RUNNING,
                    container_id=spawned.container_info.container_id,
                    slice_id=None,
                    started_at=_pkg.datetime.now(_pkg.UTC),
                    resolved_model=DEFAULT_AGENT_MODEL,
                )
                phase_execution.agents.append(agent_execution)

                store.save_pipeline(pipeline)
        except Exception as track_err:
            _pkg.logger.warning(
                "Failed to record container/agent in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    backend = spawner.backend
    try:
        final_info = backend.wait_for_container(
            spawned.container_info.container_id,
            timeout=timeout,
        )
    except (
        _pkg.ContainerNotFoundError,
        _pkg.ContainerOperationError,
        _pkg.PodNotFoundError,
        _pkg.JobOperationError,
    ) as e:
        _pkg.logger.warning(
            "Container lost during wait, marking failed",
            container_id=spawned.container_info.container_id,
            error=str(e),
        )
        final_info = _pkg.ContainerInfo(
            container_id=spawned.container_info.container_id,
            container_name=spawned.container_info.container_name,
            status=_pkg.ContainerStatus.FAILED,
            exit_code=-1,
            exited_at=_pkg.datetime.now(_pkg.UTC),
        )

    container_logs = ""
    if final_info.exit_code != 0:
        try:
            container_logs = backend.get_container_logs(
                spawned.container_info.container_id,
                tail=200,
            )
        except Exception:
            pass

    # Update container and agent status in phase execution
    if store is not None:
        try:
            with _pkg.get_pipeline_state_lock(pipeline_id):
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(_pkg.PipelinePhase(phase))

                # Update container status
                for ci in phase_execution.containers:
                    if ci.container_id == spawned.container_info.container_id:
                        ci.status = final_info.status
                        ci.exited_at = final_info.exited_at
                        ci.exit_code = final_info.exit_code
                        break

                # Update agent status
                for agent in phase_execution.agents:
                    if agent.container_id == spawned.container_info.container_id:
                        agent.completed_at = _pkg.datetime.now(_pkg.UTC)
                        if final_info.exit_code == 0:
                            agent.status = _pkg.AgentExecutionStatus.COMPLETE
                        else:
                            agent.status = _pkg.AgentExecutionStatus.FAILED
                            agent.error = f"Container exited with code {final_info.exit_code}"
                        break

                store.save_pipeline(pipeline)
        except Exception as track_err:
            _pkg.logger.warning(
                "Failed to update container/agent status in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    # Always clean up the container
    try:
        spawner.remove_agent_container(
            spawned.container_info.container_id,
            force=True,
            cleanup_session=True,
        )
    except Exception as cleanup_err:
        _pkg.logger.warning(
            "Failed to clean up container",
            container_id=spawned.container_info.container_id[:12],
            error=str(cleanup_err),
        )

    return final_info.exit_code, container_logs


def _classify_bare_gate_resolution(resolution: str | None) -> tuple[bool, str | None, str]:
    """Classify a legacy bare-string phase-gate resolution (#3636).

    A phase gate offers ``["approve", "request changes"]``, so answering
    with the literal option word *plus* a justification is the natural
    operator behaviour; and at a phase gate, justification is the whole
    point. Matching the **entire** resolution string against
    ``_APPROVE_KEYWORDS`` classified every such answer as free-text change
    requests: the gate silently took the revision branch, re-ran the
    phase, burned an ``max_hitl_review_cycles`` slot, and fed the
    operator's approval back to the producers as revision feedback.

    Match the **first line** instead and carry the remainder as context.
    That mirrors how the structured ``{"action": "approve", "context":
    ...}`` payload already behaves, and it makes the natural bare-string
    shape correct by construction. A first line that is anything other
    than a bare option word (e.g. ``"approve the rewrite but drop X"``)
    still falls through to the free-text branch, so only the
    option-word-plus-context shape changes meaning.

    Returns ``(is_approved, revision_feedback, approve_context)``:

    * ``(True, None, context)``: approved; ``context`` is the operator's
      note after the option word (``""`` when there was none).
    * ``(False, None, "")``: bare "request changes" with no specifics;
      the caller asks a follow-up.
    * ``(False, feedback, "")``: change request with actionable feedback.
    """
    text = (resolution or "").strip()

    # An entirely empty resolution is the historical reason ``""`` is a
    # member of ``_APPROVE_KEYWORDS``: there is nothing to approve or
    # reject, so the gate advances. Settle that case *before* deriving
    # ``head``, so a first line that merely collapses to ``""`` under the
    # punctuation strip below (".", "!!!") can never reach the approve
    # branch — that would be #3636 inverted, silently approving a
    # rejection whose first line happens to be stray punctuation.
    if not text:
        return True, None, ""

    # ``\r\n`` / lone ``\r`` are line separators too; splitting on ``\n``
    # alone left a CR-only body unsplit. A tab is deliberately *not* a
    # separator — it is horizontal whitespace, not a new line.
    parts = _pkg.re.split(r"\r\n|\r|\n", text, maxsplit=1)
    first_line = parts[0]
    remainder = parts[1].strip() if len(parts) > 1 else ""
    # Trailing sentence punctuation is noise on a one-word selection
    # ("Approved." / "LGTM!"), never part of the option label itself.
    head = first_line.strip().rstrip(".!").strip().lower()

    if head and head in _pkg._APPROVE_KEYWORDS:
        return True, None, remainder
    if head in _pkg._BARE_OPTION_LABELS:
        # The option word carries no information the producers need; the
        # remainder (if any) is the actionable part.
        return False, remainder or None, ""

    # A structured payload the JSON-first parsers rejected (unknown
    # ``action``, or no ``action`` field at all) still arrives here as
    # "bare" text. Returning the blob verbatim hands producers raw JSON
    # as revision feedback; extract the operator's own prose instead, and
    # when there is none fall back to "no specifics" so the caller asks a
    # follow-up rather than re-running the phase against a serialisation.
    try:
        payload = _pkg.json.loads(text)
    except ValueError, TypeError:  # JSONDecodeError is a ValueError
        payload = None
    if isinstance(payload, dict):
        prose = payload.get("feedback") or payload.get("context") or ""
        prose = prose.strip() if isinstance(prose, str) else ""
        return False, prose or None, ""

    return False, text, ""


def _parse_resolution(resolution: str | None) -> tuple[bool, str | None]:
    """Parse a HITL phase_gate resolution into (is_approved, feedback).

    Handles both JSON-structured resolutions and legacy bare-string formats.
    Used by the AWAITING_HUMAN recovery path in start_pipeline.

    Returns:
        (is_approved, feedback): is_approved is True for approve/select/submit_feedback
        actions, False for request_changes/change_approach. feedback contains the
        revision feedback text (if any) for non-approved resolutions.
    """
    if not resolution:
        return True, None

    resolution = resolution.strip()

    # JSON-first: try structured payload
    try:
        payload = _pkg.json.loads(resolution)
        if isinstance(payload, dict) and "action" in payload:
            action = payload["action"]
            feedback_text = payload.get("feedback", "") or None

            if action in ("approve", "select", "submit_feedback"):
                return True, None
            elif action in ("request_changes", "change_approach"):
                return False, feedback_text
            # Unknown action — fall through to legacy matching
    except _pkg.json.JSONDecodeError, TypeError, AttributeError:
        pass

    # Legacy bare-string resolution: first-line option-word matching (#3636)
    _approved, _feedback, _ = _pkg._classify_bare_gate_resolution(resolution)
    return _approved, _feedback

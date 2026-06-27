"""Non-consensus signal handlers (complete/progress/error/heartbeat/readiness) and their commit-verification helpers (#3312)."""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import routes.signals as _pkg
from egg_contracts.loader import ContractNotFoundError
from flask import Response
from handoffs import AgentOutput
from models import AgentExecutionStatus, AgentRole, Pipeline, PipelineStatus
from slice_id_validation import extract_slice_id as _extract_slice_id
from state_store import (
    InvalidPipelineIdError,
    PipelineNotFoundError,
)

from ._responses import make_error_response, make_success_response

_SIGTERM_PATTERN = re.compile(r"\b143\b")


def _is_sigterm_after_completion(pipeline: Pipeline, error_message: str) -> bool:
    """Return True if this error is a SIGTERM exit on an already-complete pipeline."""
    return pipeline.status == PipelineStatus.COMPLETE and bool(
        _SIGTERM_PATTERN.search(error_message)
    )


def _gateway_fetch_tracking_ref(
    pipeline_id: str,
    branch: str,
    worktree_path: Path,
    pipeline_state: Any | None,
) -> bool:
    """Fetch ``branch`` into ``refs/remotes/origin/<branch>`` via the gateway.

    The orchestrator pod holds no GitHub credentials (only the gateway does),
    so a raw ``git fetch origin`` from here fails on *every* call with
    "could not read Username" — which made ``_verify_commit_on_branch``
    return ``None`` on every propose and silently disabled the entire
    propose-time validation layer (#1473 commit-on-branch, #3016 / #3026
    draft presence + parseability) in the k8s deployment (#3081). Route the
    fetch through the gateway's authenticated ``/api/v1/git/fetch`` instead —
    the same plumbing ``_sync_worktree_with_remote`` already uses.

    Uses an explicit tracking refspec so the ``git branch -r --contains``
    read that follows sees a fresh ``origin/<branch>`` even on
    narrow-refspec mirrors (#3072 / #3075) — a bare-name fetch updates only
    ``FETCH_HEAD`` there, leaving the tracking ref stale and turning the
    contains-check into a wrongful 409 for freshly-pushed commits.

    Best-effort: returns ``False`` on any failure (caller degrades to the
    tri-state ``None`` path).

    On unset ``pipeline.network_mode``, ``_compute_gateway_mode`` issues a
    synchronous gateway RTT (``get_repo_visibility``) — intentional
    per-propose re-resolution. Most pipelines set ``network_mode`` at
    submission time so this is a no-op; callers that want to elide the
    roundtrip should set ``pipeline.network_mode`` upstream.
    """
    # Lazy imports: keep the ~2.4k-line gateway_client and ~24k-line
    # routes.pipelines modules out of ``signals`` import time (matches
    # the ``_get_draft_path`` pattern in the validators below). Done
    # outside the ``try`` so an import / mode-resolution failure does not
    # masquerade as a fetch failure in the warning below (and the
    # OVERSEER_ALERT it triggers in the caller).
    try:
        from gateway_client import get_gateway_client
    except ImportError:
        from ..gateway_client import get_gateway_client  # type: ignore[no-redef]

    mode = "public"
    if pipeline_state is not None:
        try:
            from routes.pipelines import _compute_gateway_mode
        except ImportError:
            from .pipelines import _compute_gateway_mode  # type: ignore[no-redef]
        mode, _vis = _compute_gateway_mode(pipeline_state)

    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    try:
        return get_gateway_client().fetch_branch(
            pipeline_id,
            str(worktree_path),
            args=[refspec],
            mode=mode,
        )
    except Exception as exc:
        _pkg.logger.warning(
            "Gateway tracking-ref fetch failed (non-blocking)",
            pipeline_id=pipeline_id,
            branch=branch,
            error=str(exc),
        )
        return False


def _commit_object_resolvable(worktree_path: Path, commit_sha: str) -> bool:
    """True when ``commit_sha`` resolves to a commit object locally.

    Lets the draft validators keep checking when branch verification was
    inconclusive: with the object present, a non-zero ``git show
    {sha}:{path}`` reliably means "path absent at commit", not "commit
    unknown". In the shared-object-store deployment (all agent worktrees
    share the base repo's ``.git``) a producer's commit is locally visible
    the moment it is created, no fetch required (#3081).
    """
    try:
        result = _pkg.subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "cat-file",
                "-e",
                f"{commit_sha}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _verify_commit_on_branch(
    commit: str,
    branch: str,
    worktree_path: Path,
    pipeline_id: str,
    pipeline_state: Any | None = None,
) -> bool | None:
    """Check if a commit exists on the expected branch.

    ``pipeline_state`` (when available at the call site) feeds
    ``_compute_gateway_mode`` so the authenticated fetch uses the
    pipeline's network mode; without it the fetch defaults to public and
    private-repo fetches degrade to ``None``.

    Returns:
        True if commit is on the branch.
        False if commit is NOT on the branch (hard-block).
        None if verification failed (best-effort, non-blocking).
    """
    try:
        # Authenticated fetch via the gateway, with an explicit tracking
        # refspec (#3081 — see _gateway_fetch_tracking_ref). The OVERSEER_ALERT
        # below is the visibility guard: when this fetch fails persistently,
        # every downstream propose-time validator degrades to non-blocking,
        # and that must not be silent again.
        #
        # NOTE: this is a logger-only signal — intentionally not broadcast to
        # agents via ``mcp__progress__overseer_alert``. Per-propose alerts on
        # every signal handler would be both noisy on the consensus stream and
        # the wrong granularity (a sustained-failure detector belongs in the
        # overseer monitor). Operator-side dashboards filter on the prefixed
        # log line; sustained-failure detection lives in the monitor.
        if not _pkg._gateway_fetch_tracking_ref(pipeline_id, branch, worktree_path, pipeline_state):
            _pkg.logger.error(
                "OVERSEER_ALERT commit_verification_fetch_failed",
                pipeline_id=pipeline_id,
                branch=branch,
                note=(
                    "gateway-authenticated fetch failed; commit-on-branch "
                    "verification and propose-time draft validation degrade "
                    "to non-blocking (#3081)"
                ),
            )
            return None  # Can't verify — don't block

        # Check if commit exists on the branch
        result = _pkg.subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "branch",
                "-r",
                "--contains",
                "--",
                commit,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            _pkg.logger.warning(
                "git branch --contains failed (non-blocking)",
                pipeline_id=pipeline_id,
                commit=commit,
                error=result.stderr.strip(),
            )
            return None  # Can't verify — don't block

        # Check if origin/{branch} appears in the output
        for line in result.stdout.splitlines():
            if f"origin/{branch}" in line.strip():
                return True

        # Commit not found on expected branch
        _pkg.logger.warning(
            "Commit not found on expected branch",
            pipeline_id=pipeline_id,
            commit=commit,
            expected_branch=branch,
            branches_containing=result.stdout.strip(),
        )
        return False

    except Exception as e:
        _pkg.logger.warning(
            "Branch verification failed (non-blocking)",
            pipeline_id=pipeline_id,
            commit=commit,
            branch=branch,
            error=str(e),
        )
        return None  # Don't block on verification errors


def _check_branch_progress(
    branch: str,
    phase_start_sha: str,
    worktree_path: Path,
    pipeline_id: str,
) -> None:
    """Log a warning if no new commits have been pushed since phase start."""
    try:
        result = _pkg.subprocess.run(
            [
                "git",
                "-C",
                str(worktree_path),
                "rev-parse",
                f"origin/{branch}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            current_tip = result.stdout.strip()
            if current_tip == phase_start_sha:
                _pkg.logger.warning(
                    "No new commits on branch since phase start",
                    pipeline_id=pipeline_id,
                    branch=branch,
                    phase_start_sha=phase_start_sha,
                )
    except Exception:
        pass  # Non-fatal — progress check is advisory


def handle_complete_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle agent completion signal.

    Request body data:
        {
            "agent_role": "coder",
            "commit": "abc1234",  // optional
            "files_changed": ["src/main.py"],  // optional
            "handoff_data": {...},  // optional
            "metrics": {...}  // optional
        }
    """
    agent_role_str = data.get("agent_role")
    if not agent_role_str:
        return make_error_response("Missing agent_role")

    try:
        agent_role = AgentRole(agent_role_str)
    except ValueError:
        return make_error_response(f"Invalid agent_role: {agent_role_str}")

    try:
        store = _pkg.get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        # Contracts live in per-pipeline worktrees, not the main repo.
        contract_path = _pkg.resolve_worktree_path(pipeline_id, repo_path)

        commit = data.get("commit")
        outputs = data.get("handoff_data", {})

        # SECURITY: Verify commit exists on the expected branch.
        # When the agent reports a commit SHA, confirm it was actually
        # pushed to the pipeline's assigned branch.  This catches the
        # failure mode where an agent pushes to an improvised branch
        # name and the orchestrator accepts the signal without checking.
        if commit and pipeline.branch:
            branch_verified = _pkg._verify_commit_on_branch(
                commit,
                pipeline.branch,
                contract_path,
                pipeline_id,
                pipeline_state=pipeline,
            )
            if branch_verified is False:
                # Hard-block: commit not found on expected branch.
                return make_error_response(
                    f"Completion rejected: commit {commit} not found on "
                    f"expected branch {pipeline.branch}. The agent may have "
                    f"pushed to a different branch.",
                    status_code=409,
                    details={
                        "commit": commit,
                        "expected_branch": pipeline.branch,
                        "pipeline_id": pipeline_id,
                    },
                )

            # Check for no new commits since phase start (advisory warning).
            # Only run when branch_verified is True — if verification failed
            # (returned None), the fetch didn't succeed and origin/{branch}
            # may be stale, making the progress check unreliable.
            if branch_verified is True:
                current_phase = pipeline.current_phase
                phase_exec = pipeline.phases.get(current_phase.value)
                if phase_exec and phase_exec.phase_start_sha:
                    _pkg._check_branch_progress(
                        pipeline.branch,
                        phase_exec.phase_start_sha,
                        contract_path,
                        pipeline_id,
                    )

        # Only interact with the contract for roles that have a contract
        # mapping.  Single-agent roles like REFINER and REVIEWER_REFINE
        # don't participate in contract orchestration.
        contract_role = _pkg._AGENT_ROLE_TO_CONTRACT_ROLE.get(agent_role)

        if contract_role is not None:
            # Contracts are keyed by pipeline_id (includes any qualifier) so
            # qualified pipelines resolve to the correct contract file. The
            # loader's compat shim handles legacy pre-unification paths.
            contract = _pkg.load_contract(pipeline_id, contract_path)
            orch = _pkg.create_orchestrator(contract)
            orch.complete_agent(contract_role, commit=commit, outputs=outputs)
            updated_contract = orch.apply_to_contract()
            _pkg.save_contract(updated_contract, contract_path)
            decision = orch.get_next_dispatch()
            is_complete = decision.all_complete
        else:
            is_complete = True

        # Save agent output (independent of contract — used for phase handoffs)
        if data.get("handoff_data") or data.get("files_changed"):
            output = AgentOutput(
                role=agent_role,
                commit=commit,
                files_changed=data.get("files_changed", []),
                handoff_data=outputs,
                metrics=data.get("metrics", {}),
            )
            # Derive pipeline identifier matching _pipeline_identifier() convention
            identifier: int | str = (
                pipeline.issue_number if pipeline.issue_number is not None else pipeline_id
            )
            _pkg.save_agent_output(contract_path, output, identifier=identifier)

        _pkg.logger.info(
            "Agent completed",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            commit=commit,
        )

        return make_success_response(
            "Completion recorded",
            data={
                "agent_role": agent_role.value,
                "commit": commit,
                "all_complete": is_complete,
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except ContractNotFoundError:
        # Contract may not exist yet (e.g. first phase still initializing)
        # or may have been cleaned up.  This is non-fatal — the multi-agent
        # executor tracks completion independently.
        _pkg.logger.warning(
            "Contract not found for completion signal (non-fatal)",
            pipeline_id=pipeline_id,
            role=agent_role_str,
            commit=data.get("commit"),
        )
        return make_success_response(
            "Completion acknowledged (contract not found)",
            data={
                "agent_role": agent_role_str,
                "contract_missing": True,
                "commit": data.get("commit"),
            },
        )
    except Exception as e:
        _pkg.logger.error(
            "Failed to record completion",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return make_error_response(f"Failed to record completion: {e}", status_code=500)


def handle_progress_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle progress update signal.

    Request body data:
        {
            "agent_role": "coder",
            "progress_percent": 50,
            "current_task": "Writing tests",
            "message": "..."
        }
    """
    agent_role_str = data.get("agent_role")

    _pkg.logger.info(
        "Progress update",
        pipeline_id=pipeline_id,
        role=agent_role_str,
        progress=data.get("progress_percent"),
        task=data.get("current_task"),
    )

    return make_success_response(
        "Progress recorded",
        data={
            "agent_role": agent_role_str,
            "progress_percent": data.get("progress_percent"),
        },
    )


def handle_error_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle error signal.

    Request body data:
        {
            "agent_role": "coder",
            "error": "Error message",
            "recoverable": false
        }
    """
    agent_role_str = data.get("agent_role")
    if not agent_role_str:
        return make_error_response("Missing agent_role")

    try:
        agent_role = AgentRole(agent_role_str)
    except ValueError:
        return make_error_response(f"Invalid agent_role: {agent_role_str}")

    error_message = data.get("error", "Unknown error")
    recoverable = data.get("recoverable", False)

    # Slice-scope the "already COMPLETE" suppression check below — without
    # this, a slice-2 coder finishing would silently swallow a slice-3
    # coder's error because both records share ``phase_execution.agents``
    # (#2422). The sandbox attaches ``slice_id`` on per-slice agents via
    # ``progress._maybe_attach_slice_id``; pipeline-level agents send no
    # ``slice_id`` and this resolves to ``None`` (matches non-sliced
    # records). ``_extract_slice_id`` rejects malformed values the same
    # way the BRC handlers do.
    try:
        signal_slice_id = _extract_slice_id(data)
    except ValueError as exc:
        return make_error_response(f"Invalid slice_id: {exc}")

    try:
        store = _pkg.get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        # SIGTERM (exit code 143) is expected when the orchestrator stops
        # agents after pipeline completion.  Suppress the error to avoid
        # noisy warnings on every successful run.
        if _pkg._is_sigterm_after_completion(pipeline, error_message):
            _pkg.logger.info(
                "Agent stopped after pipeline completion (SIGTERM — expected)",
                pipeline_id=pipeline_id,
                role=agent_role.value,
            )
            return make_success_response(
                "Clean shutdown acknowledged",
                data={
                    "agent_role": agent_role.value,
                    "clean_shutdown": True,
                },
            )

        # Suppress errors from agents already marked COMPLETE by the
        # consensus path.  _update_agents_complete() runs before containers
        # are stopped, so the agent is COMPLETE by the time this error
        # signal arrives.  See issue #1495.
        phase_key = pipeline.current_phase.value
        phase_execution = pipeline.phases.get(phase_key)
        if phase_execution is not None:
            for agent in phase_execution.agents:
                if (
                    agent.role == agent_role
                    and getattr(agent, "slice_id", None) == signal_slice_id
                    and agent.status == AgentExecutionStatus.COMPLETE
                ):
                    _pkg.logger.info(
                        "Agent already COMPLETE, suppressing error signal (consensus path)",
                        pipeline_id=pipeline_id,
                        role=agent_role.value,
                        slice_id=signal_slice_id,
                    )
                    return make_success_response(
                        "Error suppressed (agent already complete)",
                        data={
                            "agent_role": agent_role.value,
                            "already_complete": True,
                        },
                    )

        # Contracts live in per-pipeline worktrees, not the main repo.
        contract_path = _pkg.resolve_worktree_path(pipeline_id, repo_path)

        # Only interact with the contract for roles that have a contract
        # mapping.  Single-agent roles (REFINER, REVIEWER_REFINE, etc.)
        # don't participate in contract orchestration.
        contract_role = _pkg._AGENT_ROLE_TO_CONTRACT_ROLE.get(agent_role)

        if contract_role is not None:
            # Contracts are keyed by pipeline_id (includes any qualifier) so
            # qualified pipelines resolve to the correct contract file. The
            # loader's compat shim handles legacy pre-unification paths.
            contract = _pkg.load_contract(pipeline_id, contract_path)
            orch = _pkg.create_orchestrator(contract)
            orch.fail_agent(contract_role, error_message)
            updated_contract = orch.apply_to_contract()
            _pkg.save_contract(updated_contract, contract_path)

        _pkg.logger.error(
            "Agent failed",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            error=error_message,
            recoverable=recoverable,
        )

        return make_success_response(
            "Error recorded",
            data={
                "agent_role": agent_role.value,
                "error": error_message,
                "recoverable": recoverable,
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except ContractNotFoundError:
        _pkg.logger.warning(
            "Contract not found for error signal (non-fatal)",
            pipeline_id=pipeline_id,
            role=agent_role_str,
            error=error_message,
            recoverable=recoverable,
        )
        return make_success_response(
            "Error acknowledged (contract not found)",
            data={
                "agent_role": agent_role_str,
                "contract_missing": True,
                "error": error_message,
                "recoverable": recoverable,
            },
        )
    except Exception as e:
        _pkg.logger.error(
            "Failed to record error",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return make_error_response(f"Failed to record error: {e}", status_code=500)


def handle_heartbeat_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle heartbeat signal.

    Request body data:
        {
            "agent_role": "coder",
            "container_id": "abc123..."
        }
    """
    agent_role_str = data.get("agent_role")
    container_id = data.get("container_id")

    _pkg.logger.debug(
        "Heartbeat",
        pipeline_id=pipeline_id,
        role=agent_role_str,
        container_id=container_id[:12] if container_id else None,
    )

    return make_success_response(
        "Heartbeat acknowledged",
        data={
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )


def handle_readiness_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """Stub for the deprecated readiness signal.

    The readiness signal backed the legacy ``ConsensusEvaluator``
    READY-tallying protocol, which was removed in cq-5 of #2777. The
    surviving consensus path is BRC peer-consensus (see
    ``handle_consensus_*_signal``). This stub remains so existing routers
    can still surface a clean rejection if a legacy caller fires a
    ``readiness`` signal.
    """
    _pkg.logger.warning(
        "Readiness signal is no longer supported; use BRC consensus signals.",
        pipeline_id=pipeline_id,
        role=data.get("agent_role"),
    )
    return make_error_response(
        "Readiness signal removed under cq-5 of #2777. Use BRC consensus "
        "signals (consensus_propose / consensus_ack / consensus_nack / "
        "consensus_confirmed) instead.",
        status_code=410,
    )

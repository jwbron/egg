"""Concurrent phase executor for running multiple agents simultaneously.

Spawns all agents at phase start, all sharing the pipeline branch.
Monitors agent health, collects completion signals, and manages
consensus-based phase completion.
"""

import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from consensus_wrapper import build_consensus_wrapped_command
from events import EventType, emit_event
from message_store import Message, MessageType, get_message_store
from models import (
    AgentExecution,
    AgentExecutionStatus,
    AgentRole,
    Pipeline,
)
from peer_consensus import (
    create_peer_consensus_tracker,
    get_peer_consensus_tracker,
)
from review_graph import ReviewGraph, get_review_graph_for_phase

logger = get_logger("orchestrator.concurrent_executor")

# Type alias for spawn function.
# SpawnFn is called with (role, branch, extra_env, command) and returns
# a SpawnedContainer (from either ContainerSpawner or KubernetesSpawner).
# The result must have a container_info attribute with container_id.
SpawnFn = Callable[..., Any]

# Failure detection window: multiple failures within this window trigger abort
MULTI_FAILURE_WINDOW_SECONDS = 60


# Substrings (case-insensitive) indicating a spawn failure is worth retrying at
# the phase level.  Per-role retries in kubernetes_spawner already handle the
# short (<10s) case; these patterns cover longer gateway outages like a cold
# start (~30s) where every per-role attempt saw the gateway down.  See #1879.
_TRANSIENT_AGENT_ERROR_SUBSTRINGS: tuple[str, ...] = (
    "connection refused",
    "remote end closed",
    "closed connection",
    "connection reset",
    "timed out",
    "timeout",
    "service unavailable",
    "bad gateway",
    "failed to create any worktrees",
    "max retries exceeded",
)


def _uses_per_role_staging(pipeline: "Pipeline") -> bool:
    """Return True when a pipeline uses BABYSIT-style per-role staging branches.

    Mirrors the helper in ``orchestrator/routes/pipelines.py``: BABYSIT
    pipelines always use per-role staging; CUSTOM pipelines that supply
    a ``pr_number`` (#1762) inherit the same semantics so both modes
    share one runtime code path.

    Defined here as well (rather than importing from routes/pipelines.py)
    because ``routes.pipelines`` already imports from
    ``concurrent_executor`` — the reverse import would create a cycle.
    """
    try:
        from models import PipelineMode as _PipelineMode
    except Exception:
        return False
    mode = getattr(pipeline, "mode", None)
    if mode is None:
        return False
    if mode == _PipelineMode.BABYSIT:
        return True
    if mode == _PipelineMode.CUSTOM and getattr(pipeline, "pr_number", None) is not None:
        return True
    return False


def _is_transient_agent_error(error: str | None) -> bool:
    """Return True if an AgentExecution.error string looks retry-worthy.

    Conservative: only matches known transient patterns.  Unknown errors are
    treated as permanent so we fail fast instead of spinning on a real bug.
    """
    if not error:
        return False
    lowered = error.lower()
    return any(frag in lowered for frag in _TRANSIENT_AGENT_ERROR_SUBSTRINGS)


class ConcurrentPhaseExecutor:
    """Executes a pipeline phase with all agents running concurrently.

    All agents share the pipeline branch and communicate via the
    orchestrator message bus. Phase completion requires consensus
    from all agents.

    Container failure behavior:
    - Single failure: Log, notify other agents, create HITL decision
      with retry/abort/continue options.
    - Multiple failures (2+ within 60s): Abort phase immediately.
    - Failure during consensus: Remove READY signal, treat as single failure.

    Role roster resolution:
    - When ``roles`` is explicitly supplied, that list is used verbatim
      (the caller has already resolved the roster). ``_run_concurrent_phase``
      drives this from ``Pipeline.active_roles`` for CUSTOM-mode pipelines
      (#1762) and the subsumed BABYSIT path, so in-flight pipelines
      survive role-roster version bumps.
    - When ``roles`` is ``None``, the executor falls back to
      ``get_roles_for_phase(current_phase, has_contract, repo)``.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        spawn_fn: SpawnFn,
        max_concurrent: int = 6,
        review_graph: ReviewGraph | None = None,
        roles: list[AgentRole] | None = None,
    ) -> None:
        """Initialise the executor.

        Args:
            pipeline: The Pipeline record this executor is running against.
                When ``pipeline.active_roles`` is populated (CUSTOM-mode or
                BABYSIT subsumption per #1762), callers typically also
                pass the resolved list here as ``roles`` so the override
                is honoured even before the next pipeline reload.
            spawn_fn: Callable that creates containers for the given role.
            max_concurrent: Maximum number of containers to run at once.
            review_graph: Optional pre-filtered review graph; when None,
                the executor derives it from the pipeline's current phase.
            roles: Optional roster override. Driven by
                ``Pipeline.active_roles`` when CUSTOM-mode (#1762) or when
                BABYSIT's subsumption path populates the persisted roster.
                None falls through to the full phase-default roster.
        """
        self.pipeline = pipeline
        self.spawn_fn = spawn_fn
        self.max_concurrent = max_concurrent
        self._review_graph = review_graph
        self._roles_override = roles
        self._failure_times: list[datetime] = []
        self._lock = threading.Lock()

    def _get_review_graph(self) -> ReviewGraph:
        """Get the review graph, using the override if provided."""
        if self._review_graph is not None:
            return self._review_graph
        return get_review_graph_for_phase(
            self.pipeline.current_phase.value, repo=self.pipeline.repo
        )

    def get_agent_roles(self) -> list[AgentRole]:
        """Get the agent roles for concurrent execution.

        Returns the roles override if provided, otherwise returns roles
        appropriate for the pipeline's current phase, including both
        primary and reviewer roles.
        """
        if self._roles_override is not None:
            return list(self._roles_override)

        from egg_contracts.agent_roles import get_roles_for_phase

        phase = self.pipeline.current_phase.value
        contract_roles = get_roles_for_phase(
            phase,
            include_reviewers=True,
            repo=self.pipeline.repo,
            has_contract=getattr(self.pipeline, "has_contract", True),
        )
        return [AgentRole(r.value) for r in contract_roles]

    def get_worktree_branch(
        self,
        role: AgentRole,
        *,
        slice_id: str | None = None,
    ) -> str:
        """Get the worktree branch name for an agent role.

        Returns the pipeline's shared branch when set, falling back to
        an issue-based branch name.  All agents share the same branch
        so their commits land on a single history.

        Babysit-pr mode is the exception: to keep per-role proposals
        isolated from each other and from the PR's head branch, each
        producer is given a namespaced staging branch derived from the
        PR number, the PR head short-SHA, and the role
        (``egg/babysit-pr/{pr}/{short-sha}/{role}``).  This keeps commits
        rebase-able onto the PR head and lets reviewers ACK/NACK each
        role's staging branch independently before the final merge-and-push
        to the PR head moves forward.  If the PR head SHA is not known at
        call time, we fall back to the PR head branch so agents can still
        operate against the live PR.

        Slice-aware mode (#2137): when ``slice_id`` is supplied, the
        branch is namespaced under the slice's integration branch
        (``egg/issue-N/slice-M/{role}/work``). This keeps commits across
        slices completely isolated so a slice that fails or is restarted
        cannot corrupt sibling slices' history. The ``slice_id`` is
        normalised — both ``slice-2`` and the bare integer ``2`` are
        accepted (the latter for callers that haven't yet plumbed
        canonical IDs through). Babysit-pr mode is **not** slice-aware
        in this PR (refine-phase decision-8 deferred babysit slicing
        to a follow-up).
        """
        # Babysit-pr AND CUSTOM+PR (#1762): per-role staging branch
        # namespaced by PR head SHA. CUSTOM-mode pipelines that supply a
        # pr_number inherit BABYSIT's per-role staging semantics so both
        # modes land on one runtime code path. See
        # :func:`_uses_per_role_staging` at module scope.
        if _uses_per_role_staging(self.pipeline):
            pr_number = getattr(self.pipeline, "pr_number", None)
            sha = getattr(self.pipeline, "pr_head_sha", None)
            if pr_number and isinstance(sha, str) and len(sha) >= 7:
                short_sha = sha[:7]
                return f"egg/babysit-pr/{pr_number}/{short_sha}/{role.value}"
            # Fall back to the PR head branch so the agent still has a
            # starting point; the final-push head-move guard (Phase 5) will
            # keep things safe if the remote head has since moved.
            if self.pipeline.branch:
                return self.pipeline.branch

        if slice_id is not None:
            # Issue-mode slice scope: ``egg/issue-N/slice-M/{role}/work``.
            # This is what the slice scheduler uses for per-slice agent
            # teams (#2137 TASK-4-1). We honour the pipeline's existing
            # branch as the issue prefix when set, otherwise fall back to
            # the issue-number / pipeline id.
            issue = self.pipeline.issue_number or self.pipeline.id
            issue_branch = self.pipeline.branch or f"egg/issue-{issue}"
            normalised_slice = (
                slice_id
                if slice_id.startswith("slice-")
                else f"slice-{slice_id}"
            )
            return f"{issue_branch}/{normalised_slice}/{role.value}/work"

        if self.pipeline.branch:
            return self.pipeline.branch
        issue = self.pipeline.issue_number or self.pipeline.id
        return f"egg/issue-{issue}"

    def get_slice_integration_branch(self, slice_id: str) -> str:
        """Return the shared integration branch for a slice's BRC.

        Each slice has its own integration branch under the pipeline
        branch — ``egg/issue-N/slice-M`` — that the per-role work
        branches rebase onto. Roots base off the pipeline branch
        directly; child slices base off their parent slice's
        integration branch.
        """
        issue = self.pipeline.issue_number or self.pipeline.id
        issue_branch = self.pipeline.branch or f"egg/issue-{issue}"
        normalised_slice = (
            slice_id if slice_id.startswith("slice-") else f"slice-{slice_id}"
        )
        return f"{issue_branch}/{normalised_slice}"

    def get_agent_env(self, role: AgentRole) -> dict[str, str]:
        """Get additional environment variables for concurrent mode."""
        config = self.pipeline.config
        poll_interval = getattr(config, "message_poll_hint_seconds", 30)
        env = {
            "EGG_CONCURRENT_MODE": "true",
            "EGG_MESSAGE_POLL_INTERVAL": str(poll_interval),
        }
        # Add review graph info for BRC protocol
        graph = self._get_review_graph()
        if graph.is_producer(role.value):
            env["EGG_BRC_ROLE_TYPE"] = "producer"
            env["EGG_BRC_REVIEWERS"] = ",".join(graph.reviewers_for(role.value))
        if graph.is_reviewer(role.value):
            env["EGG_BRC_ROLE_TYPE"] = env.get("EGG_BRC_ROLE_TYPE", "") + (
                ",reviewer" if env.get("EGG_BRC_ROLE_TYPE") else "reviewer"
            )
            env["EGG_BRC_PRODUCERS"] = ",".join(graph.producers_for(role.value))

        # EGG_AGENT_FILE_PATTERNS was injected here historically so
        # sandbox/egg_lib/cli_push.py could implement its own
        # --scope-filter fallback.  The gateway enforces role
        # restrictions on push (#2039 — restricted-path-modified
        # rejection), so the env var has no consumer and is no longer
        # emitted.

        return env

    def spawn_all(
        self,
        agent_prompts: dict[AgentRole, str] | None = None,
    ) -> list[AgentExecution]:
        """Spawn all agent containers concurrently.

        Args:
            agent_prompts: Mapping of role to prompt text. When provided,
                each agent container is started with a Claude CLI command
                using the role-specific prompt.

        Returns:
            List of AgentExecution records for spawned agents.
        """
        roles = self.get_agent_roles()
        graph = self._get_review_graph()
        config = self.pipeline.config
        tracker = create_peer_consensus_tracker(
            self.pipeline.id,
            graph,
            auto_repropose_debounce_seconds=config.auto_repropose_debounce_seconds,
            max_auto_repropose=config.max_auto_repropose,
        )
        for role in roles:
            tracker.register_agent(role.value)

        return self._spawn_roles(roles, agent_prompts or {})

    def spawn_specific_roles(
        self,
        roles: list[AgentRole],
        agent_prompts: dict[AgentRole, str] | None = None,
    ) -> list[AgentExecution]:
        """Spawn a subset of agent roles.

        Used by the phase coordinator's transient-failure retry path
        (#1879): after ``spawn_all`` returns with some roles in FAILED
        state, the coordinator classifies failures and calls this to
        respawn just the failed roles without disturbing survivors.

        Does not touch the consensus tracker — roles were already
        registered by the original ``spawn_all`` call.

        Args:
            roles: Agent roles to spawn.
            agent_prompts: Mapping of role to prompt text (subset is OK).

        Returns:
            List of AgentExecution records for the respawned roles.
        """
        return self._spawn_roles(roles, agent_prompts or {})

    def _spawn_roles(
        self,
        roles: list[AgentRole],
        agent_prompts: dict[AgentRole, str],
    ) -> list[AgentExecution]:
        """Spawn the given roles concurrently on the thread pool."""
        executions: list[AgentExecution] = []

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            futures = {}
            for role in roles:
                prompt_text = agent_prompts.get(role, "")
                future = pool.submit(self._spawn_agent, role, prompt_text)
                futures[future] = role

            for future in as_completed(futures):
                role = futures[future]
                try:
                    execution = future.result()
                    executions.append(execution)
                    emit_event(
                        EventType.AGENT_STARTED,
                        self.pipeline.id,
                        data={"role": role.value},
                    )
                except Exception as e:
                    logger.error(
                        "Failed to spawn agent",
                        role=role.value,
                        error=str(e),
                        pipeline_id=self.pipeline.id,
                    )
                    executions.append(
                        AgentExecution(
                            role=role,
                            status=AgentExecutionStatus.FAILED,
                            error=str(e),
                        )
                    )

        return executions

    def _spawn_agent(self, role: AgentRole, prompt_text: str = "") -> AgentExecution:
        """Spawn a single agent container or Kubernetes Job.

        Args:
            role: The agent role to spawn.
            prompt_text: The prompt to pass to the Claude CLI. When non-empty,
                a sandbox command is built and passed to the spawn function.

        Works with both ContainerSpawner.create_concurrent_spawn_fn() and
        KubernetesSpawner.create_concurrent_spawn_fn().
        """
        branch = self.get_worktree_branch(role)
        env = self.get_agent_env(role)

        command: list[str] | None = None
        if prompt_text:
            command = build_consensus_wrapped_command(prompt_text)

        result = self.spawn_fn(
            role=role,
            branch=branch,
            extra_env=env,
            command=command,
        )

        # container_id works for both Docker containers and k8s Jobs/pods.
        # The KubernetesClient returns the Job UID as container_id.
        container_id = result.container_info.container_id

        return AgentExecution(
            role=role,
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            container_info=result.container_info,
            started_at=datetime.now(UTC),
        )

    def handle_agent_failure(self, role: str, error: str) -> dict[str, Any]:
        """Handle an agent failure during concurrent execution.

        Args:
            role: The failed agent's role.
            error: Error description.

        Returns:
            Dict describing the action taken: 'hitl_decision' or 'phase_abort'.
        """
        now = datetime.now(UTC)

        with self._lock:
            self._failure_times.append(now)

            # Check for multiple simultaneous failures
            recent = [
                t
                for t in self._failure_times
                if (now - t).total_seconds() < MULTI_FAILURE_WINDOW_SECONDS
            ]

            if len(recent) >= 2:
                return self._abort_phase(error, recent_failures=len(recent))

        # Single failure: notify other agents and create HITL decision
        return self._handle_single_failure(role, error)

    def _handle_single_failure(self, role: str, error: str) -> dict[str, Any]:
        """Handle a single agent failure."""
        # Notify other agents via message bus
        store = get_message_store()
        store.add_message(
            Message(
                pipeline_id=self.pipeline.id,
                from_role="orchestrator",
                to_role="all",
                message_type=MessageType.AGENT_FAILED,
                subject=f"Agent {role} failed",
                body=error,
                phase=self.pipeline.current_phase.value,
            )
        )

        # Remove from consensus
        tracker = get_peer_consensus_tracker(self.pipeline.id)
        crash_result = None
        if tracker:
            crash_result = tracker.handle_agent_crash(role)
            if crash_result.get("action") == "escalate":
                logger.warning(
                    "Agent crash requires escalation", role=role, reason=crash_result.get("reason")
                )

        emit_event(
            EventType.AGENT_FAILED,
            self.pipeline.id,
            data={"role": role, "error": error},
        )

        # Build context-aware HITL question
        question = f"Agent '{role}' failed: {error}."
        if crash_result and crash_result.get("blocking_producers"):
            blocking = crash_result["blocking_producers"]
            question += f" Reviewer had pending reviews for: {blocking}."
        question += " How to proceed?"

        # Create HITL decision
        decision = self.pipeline.add_decision(
            question=question,
            options=["Retry (respawn agent)", "Abort phase", "Continue without"],
            phase=self.pipeline.current_phase,
        )
        # Store failed role in context so the resolution handler can call
        # excuse_reviewer() when "Continue without" is selected.
        decision.context = f"failed_role:{role}"

        logger.warning(
            "Single agent failure, HITL decision created",
            role=role,
            error=error,
            decision_id=decision.id,
            pipeline_id=self.pipeline.id,
        )

        return {
            "action": "hitl_decision",
            "decision_id": decision.id,
            "failed_role": role,
            "crash_result": crash_result,
        }

    def _abort_phase(self, error: str, recent_failures: int) -> dict[str, Any]:
        """Abort the phase due to multiple simultaneous failures."""
        emit_event(
            EventType.PHASE_FAILED,
            self.pipeline.id,
            data={
                "reason": "multiple_agent_failures",
                "recent_failures": recent_failures,
                "error": error,
            },
        )

        decision = self.pipeline.add_decision(
            question=f"Multiple agent failures ({recent_failures} within {MULTI_FAILURE_WINDOW_SECONDS}s). Phase aborted. How to proceed?",
            options=["Retry phase", "Cancel pipeline"],
            phase=self.pipeline.current_phase,
        )

        logger.error(
            "Multiple agent failures, phase aborted",
            recent_failures=recent_failures,
            error=error,
            pipeline_id=self.pipeline.id,
        )

        return {
            "action": "phase_abort",
            "decision_id": decision.id,
            "recent_failures": recent_failures,
        }

    def check_consensus(self) -> dict[str, Any]:
        """Check if consensus has been reached for phase completion."""
        tracker = get_peer_consensus_tracker(self.pipeline.id)
        if not tracker:
            logger.warning(
                "Consensus tracker not found, attempting reconstruction",
                pipeline_id=self.pipeline.id,
            )
            # Attempt lazy reconstruction from message store
            try:
                from peer_consensus import reconstruct_tracker_from_messages

                graph = self._get_review_graph()
                tracker = reconstruct_tracker_from_messages(self.pipeline.id, graph)
            except ImportError:
                pass
            except Exception as e:
                logger.warning(
                    "Tracker reconstruction failed",
                    error=str(e),
                    pipeline_id=self.pipeline.id,
                )
        if tracker:
            result = tracker.evaluate()
            # Message-bus fallback: if reconstruction produced a tracker but
            # evaluate() says not complete, check the message store directly.
            # This handles the case where reconstruction replayed into an empty
            # tracker state (RC1/RC5) but all roles have CONFIRMED messages.
            if not result.get("is_complete"):
                all_roles = tracker.graph.all_roles()
                confirmed_in_tracker = len(all_roles) - len(result.get("blocking_agents", []))
                logger.info(
                    "Consensus incomplete — checking fallbacks",
                    pipeline_id=self.pipeline.id,
                    confirmed=confirmed_in_tracker,
                    total=len(all_roles),
                    blocking_agents=result.get("blocking_agents", []),
                    has_unresolved_nacks=result.get("has_unresolved_nacks", False),
                )

                # Safety net (#1671): if the tracker has all roles in
                # confirmed_roles but evaluate() returned False due to stale
                # NACK edges in the approval matrix (common after NACK →
                # re-propose cycles), trust the confirmed set.
                tracker_confirmed = tracker.confirmed_roles
                if all_roles and all_roles.issubset(tracker_confirmed):
                    logger.warning(
                        "All roles in tracker.confirmed_roles but evaluate() "
                        "returned incomplete — overriding (#1671)",
                        pipeline_id=self.pipeline.id,
                        confirmed_roles=sorted(tracker_confirmed),
                        has_unresolved_nacks=result.get("has_unresolved_nacks", False),
                        blocking_agents=result.get("blocking_agents", []),
                    )
                    result["is_complete"] = True
                    result["fallback"] = "tracker_confirmed"
                else:
                    # Message-bus fallback: scan message store for
                    # CONSENSUS_CONFIRMED messages (#1471/#1615).
                    try:
                        from message_store import get_message_store

                        store = get_message_store()
                        messages = store.get_messages(self.pipeline.id, limit=10000)
                        # Count a role as confirmed if it has a clean
                        # CONFIRMED message, or a pending_acks CONFIRMED
                        # message where the tracker later accepted the
                        # confirmation (#1671).
                        confirmed_roles: set[str] = set()
                        for m in messages:
                            if m.message_type != "CONSENSUS_CONFIRMED":
                                continue
                            if not (m.metadata or {}).get("pending_acks"):
                                confirmed_roles.add(m.from_role)
                            elif m.from_role in tracker_confirmed:
                                confirmed_roles.add(m.from_role)
                        if all_roles and all_roles.issubset(confirmed_roles):
                            logger.warning(
                                "Tracker state inconsistent with message bus — "
                                "tracker says incomplete but all roles confirmed "
                                "via messages (#1471/#1615)",
                                pipeline_id=self.pipeline.id,
                                confirmed_roles=sorted(confirmed_roles),
                                tracker_blocking=result.get("blocking_agents", []),
                            )
                            result["is_complete"] = True
                            result["fallback"] = "message_bus"
                        else:
                            missing = all_roles - confirmed_roles if all_roles else set()
                            logger.info(
                                "Message-bus fallback: not all roles confirmed",
                                pipeline_id=self.pipeline.id,
                                confirmed_roles=sorted(confirmed_roles),
                                missing_roles=sorted(missing),
                                total_messages=len(messages),
                            )
                    except Exception as e:
                        logger.warning(
                            "Message-bus fallback in check_consensus failed",
                            pipeline_id=self.pipeline.id,
                            error=str(e),
                        )
            return result
        return {"is_complete": False, "blocking_agents": [], "has_objections": False, "agents": {}}


def is_concurrent_execution(pipeline: Pipeline, phase: str | None = None) -> bool:
    """Check if a pipeline is configured for concurrent execution.

    When ``concurrent_execution`` is ``True``, BRC is active for every phase.
    Otherwise, BRC is active only when the given *phase* is listed in
    ``concurrent_phases`` (which defaults to
    ``["refine", "plan", "implement"]``).

    Args:
        pipeline: Pipeline to check.
        phase: Optional phase name to check against ``concurrent_phases``.

    Returns:
        True if concurrent execution should be used.
    """
    if getattr(pipeline.config, "concurrent_execution", False):
        return True
    if phase is None:
        return False
    concurrent_phases = getattr(pipeline.config, "concurrent_phases", [])
    return phase in concurrent_phases

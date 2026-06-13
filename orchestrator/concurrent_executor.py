"""Concurrent phase executor for running multiple agents simultaneously.

Spawns all agents at phase start, all sharing the pipeline branch.
Monitors agent health, collects completion signals, and manages
consensus-based phase completion.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from event_loop import OrchestratorEventLoop

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


from agent_model_resolution import (
    DEFAULT_AGENT_MODEL,
    UPSTREAM_ANTHROPIC,
    AgentModelDecision,
    classify_model,
    resolve_agent_model,
)
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
from slice_id_validation import SLICE_ID_PATTERN

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


def _is_transient_agent_error(error: str | None) -> bool:
    """Return True if an AgentExecution.error string looks retry-worthy.

    Conservative: only matches known transient patterns.  Unknown errors are
    treated as permanent so we fail fast instead of spinning on a real bug.
    """
    if not error:
        return False
    lowered = error.lower()
    return any(frag in lowered for frag in _TRANSIENT_AGENT_ERROR_SUBSTRINGS)


def _event_payload_refs(payload: dict[str, Any] | None) -> str | None:
    """Render a compact, env-safe payload-ref string for the one-shot Job.

    The wrapper arm re-derives the live payload itself (the injected refs are
    informational), so this is a best-effort breadcrumb: the producers under
    review for a reviewer event, or the producer for a propose. Returns
    ``None`` when there is nothing useful to carry.
    """
    if not payload:
        return None
    reviews = payload.get("pending_reviews")
    if reviews:
        refs = ",".join(str(r.get("producer", "")) for r in reviews if r.get("producer"))
        return refs or None
    producer = payload.get("producer")
    return str(producer) if producer else None


class _ExecutorEventSpawner:
    """Adapter exposing :class:`OrchestratorEventLoop`'s ``spawn_event`` surface
    over the executor's per-pipeline ``spawn_fn`` closure.

    The closure carries repos / mode / phase context and routes event spawns
    to the kubernetes spawner's ``spawn_event_job`` (which injects the event
    identity env + dedupe-key label and adopts an already-live key), so the
    adapter only has to resolve the per-role branch / env / command.
    """

    def __init__(
        self,
        *,
        executor: ConcurrentPhaseExecutor,
        roles: list[AgentRole],
        slice_id: str | None,
    ) -> None:
        self._ex = executor
        self._slice_id = slice_id
        self._role_by_value = {r.value: r for r in roles}

    def _agent_role(self, role: str) -> AgentRole:
        return self._role_by_value.get(role) or AgentRole(role)

    def spawn_event(
        self,
        *,
        role: str,
        action: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        agent_role = self._agent_role(role)
        branch = self._ex.get_worktree_branch(agent_role, slice_id=self._slice_id)
        env = {**self._ex.get_agent_env(agent_role), "EGG_EVENT_LOOP_OWNER": "orchestrator"}
        command, upstream, upstream_model = self._ex._build_event_spawn_params(agent_role)
        return self._ex.spawn_fn(
            role=agent_role,
            branch=branch,
            extra_env=env,
            command=command,
            upstream=upstream,
            upstream_model=upstream_model,
            event_action=action,
            event_dedupe_key=dedupe_key,
            event_payload_refs=_event_payload_refs(payload),
        )


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
      (the caller has already resolved the roster).
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
        slice_id: str | None = None,
        event_status_view: Any | None = None,
    ) -> None:
        """Initialise the executor.

        Args:
            pipeline: The Pipeline record this executor is running against.
            spawn_fn: Callable that creates containers for the given role.
            max_concurrent: Maximum number of containers to run at once.
            review_graph: Optional pre-filtered review graph; when None,
                the executor derives it from the pipeline's current phase.
            roles: Optional roster override. None falls through to the
                full phase-default roster.
            slice_id: Optional slice scope (#2137 TASK-4-3 / TASK-4-4).
                When supplied, the executor namespaces the BRC consensus
                tracker key under ``{pipeline_id}/{slice_id}`` so per-
                slice consensus is naturally isolated, and per-role
                worktree branches are emitted in the slice-scoped form
                ``egg/issue-N/{slice_id}/{role}/work`` so commits across
                slices stay isolated. ``None`` preserves the pre-slicing
                pipeline-scoped semantics.
            event_status_view: Optional Job-status observer (#3064 slice-3)
                exposing ``outcome_for(dedupe_key) -> str``. In
                orchestrator-ownership mode it lets the event loop watch
                one-shot Job termination and drive supervision (backoff,
                respawn, OVERSEER_ALERT). ``None`` (pod mode / tests without
                a cluster) leaves supervision observation dormant.
        """
        self.pipeline = pipeline
        self.spawn_fn = spawn_fn
        self.max_concurrent = max_concurrent
        self._review_graph = review_graph
        self._roles_override = roles
        self._slice_id = slice_id
        self._event_status_view = event_status_view
        self._failure_times: list[datetime] = []
        self._lock = threading.Lock()
        # #3064 slice-2: set when orchestrator-ownership mode starts the
        # event loop in ``spawn_all``; ``None`` in pod mode.
        self._event_loop: Any | None = None

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

        Slice-aware mode (#2137): when ``slice_id`` is supplied, **every
        agent in the slice shares the slice's integration branch
        ``egg/issue-N/slice-M``** — the same shared-branch model the
        non-slice flow has always used, just scoped per-slice. The
        slice is the unit of isolation; within a slice all agents
        collaborate on one history (otherwise the per-slice PR opened
        with ``head=integration_branch`` against ``base=parent_branch``
        would have an empty diff because the agents' commits would
        live on per-role sibling branches GitHub doesn't see). The
        ``slice_id`` is normalised — both ``slice-2`` and the bare
        integer ``2`` are accepted (the latter for callers that
        haven't yet plumbed canonical IDs through).
        """
        if slice_id is not None:
            # Issue-mode slice scope: ``egg/issue-N/slice-M`` — the
            # shared integration branch for every agent in the slice.
            # This is what the slice scheduler uses for per-slice agent
            # teams (#2137 TASK-4-1) and is what the per-slice PR's
            # ``head`` points at, so agents' commits MUST land here
            # rather than on per-role sibling branches that GitHub
            # cannot see in the slice PR's diff. We honour the
            # pipeline's existing branch as the issue prefix when set,
            # otherwise fall back to the issue-number / pipeline id.
            #
            # The pipeline tip is pushed to ``egg/<id>/work`` (#2399), so
            # the slice integration branch lives as a sibling of ``/work``
            # under ``egg/<id>/`` — strip the trailing ``/work`` from the
            # pipeline branch to get the namespace root.
            issue = self.pipeline.issue_number or self.pipeline.id
            issue_branch = self.pipeline.branch or f"egg/issue-{issue}"
            # Structural check (≥2 slashes, last segment ``work``) — see
            # ``_slice_namespace_root`` in ``routes/pipelines.py`` for
            # the matching helper. A degenerate single-segment input
            # like ``egg/work`` is treated as the root itself rather
            # than collapsing to ``egg``.
            if issue_branch.count("/") >= 2 and issue_branch.rsplit("/", 1)[1] == "work":
                issue_branch = issue_branch.rsplit("/", 1)[0]
            normalised_slice = slice_id if slice_id.startswith("slice-") else f"slice-{slice_id}"
            # Defense-in-depth: re-validate the normalised slice id
            # shape before embedding it in a git ref. The contract-
            # layer pydantic regex already enforces this on the
            # source, but the helper is part of the gateway-facing
            # surface — a future caller that forgets upstream
            # validation must not be able to smuggle path separators
            # or shell metacharacters in via this seam (per the
            # security reviewer's defense-in-depth suggestion on the
            # v1 BRC review). The pattern is the canonical one shared
            # with the signal handlers (#2403) and the operator restart
            # route (#2410) — see ``slice_id_validation``.
            if not SLICE_ID_PATTERN.fullmatch(normalised_slice):
                raise ValueError(
                    f"slice_id={slice_id!r} does not match the canonical shape ``slice-<N>``"
                )
            return f"{issue_branch}/{normalised_slice}"

        if self.pipeline.branch:
            return self.pipeline.branch
        issue = self.pipeline.issue_number or self.pipeline.id
        return f"egg/issue-{issue}"

    def get_slice_integration_branch(self, slice_id: str) -> str:
        """Return the shared integration branch for a slice's BRC.

        Each slice has its own integration branch as a sibling of the
        pipeline tip under ``egg/<id>/`` — ``egg/issue-N/slice-M`` —
        that the per-role work branches rebase onto. Roots base off the
        pipeline branch directly (``egg/issue-N/work``); child slices
        base off their parent slice's integration branch.

        The pipeline tip is pushed to ``egg/<id>/work`` (#2399), so the
        slice integration branch lives as a sibling of ``/work`` under
        ``egg/<id>/`` — strip the trailing ``/work`` from the pipeline
        branch to get the namespace root.

        The slice id is regex-validated for defense-in-depth (see
        ``get_worktree_branch``).
        """
        issue = self.pipeline.issue_number or self.pipeline.id
        issue_branch = self.pipeline.branch or f"egg/issue-{issue}"
        # Structural check (≥2 slashes, last segment ``work``) — see
        # ``_slice_namespace_root`` in ``routes/pipelines.py``.
        if issue_branch.count("/") >= 2 and issue_branch.rsplit("/", 1)[1] == "work":
            issue_branch = issue_branch.rsplit("/", 1)[0]
        normalised_slice = slice_id if slice_id.startswith("slice-") else f"slice-{slice_id}"
        if not SLICE_ID_PATTERN.fullmatch(normalised_slice):
            raise ValueError(
                f"slice_id={slice_id!r} does not match the canonical shape ``slice-<N>``"
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
        # When ``slice_id`` is set, the tracker is registered under the
        # nested key ``{pipeline_id}/{slice_id}`` so each slice's BRC
        # consensus is fully isolated from siblings (#2137 TASK-4-3,
        # refine-phase decision-14 hybrid).
        tracker = create_peer_consensus_tracker(
            self.pipeline.id,
            graph,
            slice_id=self._slice_id,
            auto_repropose_debounce_seconds=config.auto_repropose_debounce_seconds,
            max_auto_repropose=config.max_auto_repropose,
        )
        for role in roles:
            tracker.register_agent(role.value)

        # #3064 slice-2: in orchestrator-ownership mode the orchestrator owns
        # the BRC event loop and spawns a one-shot pod per actionable event
        # — NO agents are spawned up front. The tracker is still registered
        # above (the event loop derives against it). With the flag unset /
        # ``pod`` (the default) this branch is skipped and behavior is
        # byte-identical to before.
        if self._event_loop_owner() == "orchestrator":
            self._start_event_loop(roles, tracker)
            return []

        # A producer with no work in this slice (e.g. a documenter on a
        # code-only slice) is no longer pre-seeded here (#3027 retired the
        # #2581 pre-seed). Instead it stays spawned and submits a generic
        # no-op propose (``no_changes_needed=true``) at runtime, which the
        # consensus protocol accepts as a non-blocking, durable no-op —
        # robust to restart / reconstruction in a way the in-memory seed
        # never was.
        return self._spawn_roles(roles, agent_prompts or {})

    @staticmethod
    def _event_loop_owner() -> str:
        """Return the BRC event-loop ownership mode (``pod`` | ``orchestrator``).

        Lazy dual-path import (repo root vs ``orchestrator/`` on sys.path),
        mirroring ``consensus_wrapper._event_loop_owner``. An invalid value
        raises loudly (the #3023 no-silent-fallback contract).
        """
        try:
            from orchestrator.env_config import get_event_loop_owner
        except ImportError:
            from env_config import get_event_loop_owner  # type: ignore[no-redef]
        return get_event_loop_owner()

    def _start_event_loop(self, roles: list[AgentRole], tracker: Any) -> OrchestratorEventLoop:
        """Construct and start the orchestrator-owned event loop (#3064 slice-2).

        Wires the production lifecycle surface: one-shot spawns flow through
        the per-pipeline ``spawn_fn`` closure (which carries repos / mode /
        phase context and routes event spawns to the spawner's
        ``spawn_event_job`` — env identity + dedupe label + adoption);
        ``confirm``/``complete`` are recorded orchestrator-side with no pod.
        Restart reconciliation is backstopped by ``spawn_event_job``'s own
        dedupe-label adoption, so the loop seeds an empty live set. The loop
        runs on a daemon thread (poll-interval cadence) and stops when the
        slice converges.
        """
        from event_loop import JobSupervisor, OrchestratorEventLoop, make_role_list

        slice_id = self._slice_id
        pipeline_id = self.pipeline.id
        phase = self.pipeline.current_phase.value

        spawner_adapter = _ExecutorEventSpawner(
            executor=self,
            roles=roles,
            slice_id=slice_id,
        )

        def _agent_free(*, action: str, role: str, payload: Any = None) -> None:
            self._orchestrator_side_confirm(tracker, role)

        # Supervision (#3064 slice-3): when a producer's propose arm exhausts
        # its retry budget the supervisor fires a sticky OVERSEER_ALERT and
        # engages the existing AGENT_FAILED path. Both side effects are wired
        # to the real surfaces here so the loop's supervision is functional
        # end-to-end (not just an in-isolation primitive).
        supervisor = JobSupervisor(
            overseer_alert=self._emit_supervision_alert,
            agent_failed=self._handle_propose_arm_exhaustion,
            on_exhausted=self._teardown_exhausted_session,
        )

        # #3064 slice-5: convergence-stall notifier re-uses the same
        # OVERSEER_ALERT surface wired for the supervisor.
        loop = OrchestratorEventLoop(
            tracker,
            spawner_adapter,
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            phase=phase,
            agent_free_handler=_agent_free,
            roles=make_role_list(roles),
            job_supervisor=supervisor,
            job_status_view=self._event_status_view,
            convergence_stall_notifier=self._emit_supervision_alert,
            active_roles_notifier=self._publish_active_roles,
        )
        self._event_loop = loop
        loop.start()
        logger.info(
            "Started orchestrator-owned BRC event loop",
            pipeline_id=pipeline_id,
            slice_id=slice_id,
            phase=phase,
            roles=[r.value for r in roles],
        )

        # #3064 slice-5: set orchestrator mode on the health monitor and
        # heartbeat coordinator so their tripwire/refresh behavior reflects
        # the ownership mode (roles with no active Job are normal in
        # orchestrator mode; gateway-session refresh via heartbeat fan-out
        # is suppressed).
        self._enable_orchestrator_mode_surfaces()

        return loop

    def _publish_active_roles(self, roles: set[str]) -> None:
        """Publish the event loop's live-Job role set to the health monitor.

        Wired as the event loop's ``active_roles_notifier`` so the monitor's
        ``_active_jobs`` reflects which roles currently have an in-flight
        one-shot Job on every poll tick.  This is what makes orchestrator-mode
        active-Job scoping (and silent-mid-event-pod coverage) actually take
        effect in production — without it ``_active_jobs`` stays empty and
        ``_orchestrator_skip_tripwire`` suppresses every role.

        Best-effort: a missing health monitor (unit tests stand up only the
        component under test) is tolerated.
        """
        try:
            from health_monitor import get_health_monitor

            hm = get_health_monitor()
            if hm is not None:
                hm.set_active_roles(roles)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    def _enable_orchestrator_mode_surfaces(self) -> None:
        """Propagate orchestrator mode to downstream surfaces.

        In orchestrator mode:
        - The health monitor suppresses alerts for roles with no active Job.
        - The heartbeat coordinator suppresses gateway-session fan-out
          (refresh happens at spawn time from slice-4 worktree re-attach).
        - Absent-sender heartbeats between events trip nothing.

        Best-effort: a missing health monitor or coordinator is tolerated
        (unit tests often stand up only the component under test).
        """
        try:
            from health_monitor import get_health_monitor

            hm = get_health_monitor()
            if hm is not None:
                hm.set_orchestrator_mode(True)
        except Exception:  # noqa: BLE001 — best-effort
            pass

        try:
            from heartbeat import get_heartbeat_coordinator

            hc = get_heartbeat_coordinator()
            hc.set_orchestrator_mode(True)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    def _build_event_spawn_params(
        self, role: AgentRole
    ) -> tuple[list[str], str | None, str | None]:
        """Return ``(command, upstream, upstream_model)`` for a role's event pod.

        The event-pump template composes its own per-event prompt at runtime
        (``invoke_agent_for_event``), so the initial prompt is irrelevant —
        only model + effort matter, resolved identically to ``_spawn_agent``.
        ``upstream``/``upstream_model`` are returned only when they differ
        from the default Anthropic decision (mirroring ``_spawn_agent``'s
        conditional forwarding) so the default-Claude wire shape is unchanged.
        """
        decision = self._resolve_model_decision(role)
        command = build_consensus_wrapped_command(
            "", model=decision.claude_code_alias, effort=decision.effort
        )
        upstream: str | None = None
        upstream_model: str | None = None
        if decision.upstream != UPSTREAM_ANTHROPIC or decision.upstream_model is not None:
            upstream = decision.upstream
            upstream_model = decision.upstream_model
        return command, upstream, upstream_model

    def _orchestrator_side_confirm(self, tracker: Any, role: str) -> None:
        """Record a ``confirm``/``complete`` orchestrator-side — no pod (#3064).

        Mirrors the wrapper's agent-free ``egg-orch consensus confirmed`` arm:
        advance the tracker FSM and emit a ``CONSENSUS_CONFIRMED`` message
        (slice-tagged) so the bus + reconstruction reflect the confirmation.
        Best-effort and idempotent — a guard rejection (not yet eligible)
        simply leaves state unchanged for the next poll.
        """
        try:
            result = tracker.handle_confirmed(role)
        except Exception as exc:  # noqa: BLE001 — never wedge the loop
            logger.warning(
                "Orchestrator-side confirm failed",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                role=role,
                error=str(exc),
            )
            return
        status = (result or {}).get("status") if isinstance(result, dict) else None
        if status in ("pending_acks", "rejected"):
            # Not yet eligible — re-derived next poll; no message emitted.
            return
        try:
            store = get_message_store()
            slice_meta: dict[str, Any] = (
                {"slice_id": self._slice_id} if self._slice_id is not None else {}
            )
            store.add_message(
                Message(
                    pipeline_id=self.pipeline.id,
                    from_role=role,
                    to_role="all",
                    message_type=MessageType.CONSENSUS_CONFIRMED,
                    subject=f"Consensus confirmed by {role}",
                    body="orchestrator-side confirm (#3064 event loop)",
                    phase=self.pipeline.current_phase.value,
                    metadata=slice_meta,
                )
            )
        except Exception as exc:  # noqa: BLE001 — message emission is best-effort
            logger.warning(
                "Failed to emit CONSENSUS_CONFIRMED for orchestrator-side confirm",
                pipeline_id=self.pipeline.id,
                role=role,
                error=str(exc),
            )

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
                            slice_id=self._slice_id,
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
        branch = self.get_worktree_branch(role, slice_id=self._slice_id)
        env = self.get_agent_env(role)

        decision = self._resolve_model_decision(role)

        command: list[str] | None = None
        if prompt_text:
            command = build_consensus_wrapped_command(
                prompt_text, model=decision.claude_code_alias, effort=decision.effort
            )

        # On the LiteLLM path Claude Code needs the ANTHROPIC_CUSTOM_MODEL_OPTION
        # env vars to opt into 1M-context compaction math (#2832). The decision's
        # ``env_vars()`` is empty on the Anthropic path, so default-Claude spawns
        # carry no extra env — the pre-#2832 wire shape.
        env = {**env, **decision.env_vars()}

        # Forward the upstream/upstream_model kwargs to the spawner only
        # when they would change behavior — the default Anthropic decision
        # is omitted so test mocks and legacy spawn paths see the same
        # call signature they did before #2769 slice-2 (#2769 task-2-4).
        spawn_kwargs: dict[str, Any] = {
            "role": role,
            "branch": branch,
            "extra_env": env,
            "command": command,
        }
        if decision.upstream != UPSTREAM_ANTHROPIC or decision.upstream_model is not None:
            spawn_kwargs["upstream"] = decision.upstream
            spawn_kwargs["upstream_model"] = decision.upstream_model

        result = self.spawn_fn(**spawn_kwargs)

        # container_id works for both Docker containers and k8s Jobs/pods.
        # The KubernetesClient returns the Job UID as container_id.
        container_id = result.container_info.container_id

        return AgentExecution(
            role=role,
            status=AgentExecutionStatus.RUNNING,
            container_id=container_id,
            container_info=result.container_info,
            started_at=datetime.now(UTC),
            slice_id=self._slice_id,
        )

    def _resolve_model_decision(self, role: AgentRole) -> AgentModelDecision:
        """Resolve the per-agent model decision for a role (#2769 slice-2).

        Pure over (role, pipeline_config, repo); when no override is
        configured the resolver returns a built-in Anthropic decision —
        ``fable`` for the refine/plan roles, ``opus`` otherwise — so the wire
        shape stays Anthropic-only.

        Defensive wrap: a future resolver regression must not bring down
        spawn for every pipeline. Mirror the restart path's
        ``classify_model(DEFAULT_AGENT_MODEL)`` fallback
        (``routes/pipelines.py``) — degrade to the built-in opus / anthropic
        decision and log rather than crash. Shared by the long-lived
        ``_spawn_agent`` and the orchestrator-owned event-loop command
        builder so both resolve identically.
        """
        try:
            return resolve_agent_model(
                role=role,
                pipeline_config=self.pipeline.config,
                repo=self.pipeline.repo,
            )
        except Exception as resolve_err:  # noqa: BLE001 — degrade, don't crash
            logger.warning(
                "Failed to resolve per-agent model decision for spawn, "
                "falling back to built-in opus / anthropic default",
                role=role,
                error=str(resolve_err),
            )
            return classify_model(DEFAULT_AGENT_MODEL)

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

    # ------------------------------------------------------------------
    # Event-loop supervision side effects (#3064 slice-3)
    # ------------------------------------------------------------------
    def _emit_supervision_alert(
        self, *, anomaly: str, priority: str, summary: str, detail: str
    ) -> None:
        """Broadcast a sticky OVERSEER_ALERT for a failure-streak exhaustion.

        Mirrors the overseer monitor's broadcast convention: an
        ``OVERSEER_ALERT`` message to the ``all`` target with the anomaly name
        encoded in the subject (so ``/sdlc`` and any listener pick it up).
        Best-effort — a message-store hiccup must not wedge the loop.
        """
        try:
            get_message_store().add_message(
                Message(
                    pipeline_id=self.pipeline.id,
                    from_role="orchestrator",
                    to_role="all",
                    message_type=MessageType.OVERSEER_ALERT,
                    subject=f"{anomaly}: event-loop [{priority}]",
                    body=detail or summary,
                    phase=self.pipeline.current_phase.value,
                    metadata={
                        "anomaly": anomaly,
                        "priority": priority,
                        "summary": summary,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — alert emission is best-effort
            logger.warning(
                "Failed to broadcast supervision OVERSEER_ALERT",
                pipeline_id=self.pipeline.id,
                anomaly=anomaly,
                error=str(exc),
            )

    def _handle_propose_arm_exhaustion(
        self, *, role: str, action: str, dedupe_key: str, streak: int
    ) -> None:
        """Engage the existing AGENT_FAILED path for a producer propose arm.

        #2806 relocated for orchestrator mode: when a producer's propose arm
        exhausts its retry budget the producer is effectively stuck, so route
        it through :meth:`_handle_single_failure` (AGENT_FAILED broadcast +
        crash handling + HITL decision) — the same path a long-lived pod
        failure takes in pod mode.
        """
        error = (
            f"producer propose arm exhausted after {streak} consecutive "
            f"agent-invocation failures (dedupe_key={dedupe_key})"
        )
        try:
            self._handle_single_failure(role, error)
        except Exception as exc:  # noqa: BLE001 — never wedge the loop on this
            logger.warning(
                "Failed to engage AGENT_FAILED for propose-arm exhaustion",
                pipeline_id=self.pipeline.id,
                role=role,
                error=str(exc),
            )

    def _teardown_exhausted_session(self, *, role: str, action: str, dedupe_key: str) -> None:
        """Release a role's reused gateway session on streak exhaustion (#3064 slice-4).

        Wired to :class:`JobSupervisor`'s ``on_exhausted`` hook (the
        ``_exhausted`` transition). The exhausted event arm will spawn no
        further events, so the long-lived orchestrator-mode session keyed by
        the role's stable base ``container_id`` is torn down here rather than
        lingering to pipeline cleanup. Reaches :meth:`KubernetesSpawner.
        _teardown_session` via the teardown closure attached to ``spawn_fn``
        (the spawner is not held directly). Best-effort — a teardown failure
        must never wedge the supervision path.
        """
        teardown = getattr(self.spawn_fn, "teardown_event_session", None)
        if teardown is None:
            # Pod-mode / test spawn_fn without the event-mode teardown surface:
            # there is no long-lived per-role session to release.
            return
        try:
            agent_role = role if isinstance(role, AgentRole) else AgentRole(role)
        except ValueError:
            logger.warning(
                "Unknown role on streak-exhaustion teardown; skipping",
                pipeline_id=self.pipeline.id,
                role=role,
                dedupe_key=dedupe_key,
            )
            return
        try:
            teardown(agent_role)
            logger.info(
                "Tore down reused gateway session on streak exhaustion",
                pipeline_id=self.pipeline.id,
                role=agent_role.value,
                action=action,
                dedupe_key=dedupe_key,
            )
        except Exception as exc:  # noqa: BLE001 — never wedge the loop on teardown
            logger.warning(
                "Failed to tear down reused gateway session on streak exhaustion",
                pipeline_id=self.pipeline.id,
                role=agent_role.value,
                error=str(exc),
            )

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
        tracker = get_peer_consensus_tracker(self.pipeline.id, self._slice_id)
        if not tracker:
            logger.warning(
                "Consensus tracker not found, attempting reconstruction",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
            )
            # Attempt lazy reconstruction from message store — pipeline-
            # scoped only. ``reconstruct_tracker_from_messages`` can do a
            # filtered per-slice replay (#2761), but completing consensus
            # off a reconstructed slice tracker risks false consensus the
            # moment a fresh slice spawns roles whose names match an
            # already-confirmed prior slice. So this completion path
            # still reconstructs only the pipeline-level tracker.
            # Per-slice trackers are stateless event consumers and are
            # recreated by the slice scheduler on the next iteration; the
            # pipeline run loop's empty-tracker iteration will simply
            # observe is_complete=False, which is the correct answer for
            # a brand-new slice (#2535).
            if self._slice_id is None:
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
                # re-propose cycles), trust the confirmed set. This path is
                # safe for slice-scoped trackers because ``confirmed_roles``
                # is the per-slice tracker's own state.
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
                elif self._slice_id is not None:
                    # Slice-scoped: the message-bus fallback below scans
                    # ``store.get_messages(pipeline_id)`` pipeline-wide and
                    # cannot distinguish slice-1's CONFIRMs from slice-2's,
                    # so it would falsely declare consensus the moment a
                    # fresh slice spawns roles whose names match an already-
                    # confirmed prior slice (#2535). The in-memory per-slice
                    # tracker is the authoritative source for slice work;
                    # an empty fresh tracker correctly returns
                    # is_complete=False here so the pipeline run loop keeps
                    # polling.
                    logger.info(
                        "Skipping pipeline-wide message-bus fallback for slice-scoped tracker",
                        pipeline_id=self.pipeline.id,
                        slice_id=self._slice_id,
                        blocking_agents=result.get("blocking_agents", []),
                    )
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

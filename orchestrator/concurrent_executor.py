"""Concurrent phase executor for running multiple agents simultaneously.

Spawns all agents at phase start, all sharing the pipeline branch.
Monitors agent health, collects completion signals, and manages
consensus-based phase completion.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
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
    real_backend_window,
    reseed_threshold,
    resolve_agent_model,
)
from consensus_wrapper import build_consensus_wrapped_command

try:
    # Orchestrator-side check of the forwarded warm-resume flags
    # (``EGG_SESSION_RESUME`` / ``EGG_CONTEXT_DISCIPLINE`` on the orchestrator's
    # own env, which ``_forwarded_discipline_env`` propagates to the pod). Used to
    # gate the #3278 session-store env so default pods stay byte-identical.
    from egg_agent.session import session_resume_enabled
except ImportError:  # pragma: no cover - egg_agent always on PYTHONPATH in prod/tests

    def session_resume_enabled() -> bool:  # type: ignore[misc]
        return False


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

# #3496: arms-exhausted HITL escalation. Stable context discriminator +
# option labels, single-sourced here (the decision is created here) and
# lazily imported by the resolution dispatch hook in
# ``routes/decisions/_handlers.py`` — same pattern as the
# ``consensus_timeout_incomplete`` context in ``routes/pipelines.py``.
ARMS_EXHAUSTED_HITL_CONTEXT = "event_arms_exhausted"
ARMS_EXHAUSTED_RETRY_OPTION = "Retry arms (reset spawn budgets)"
ARMS_EXHAUSTED_RESTART_OPTION = "Restart phase"
# Label spells out that this option only *records* the operator's choice —
# it does not stop the phase (which keeps running, wedged, until cancel_task).
# "Abort phase" implied an action the resolution does not take (#3496 review).
ARMS_EXHAUSTED_ABORT_OPTION = "Abort (manual — recorded only)"

# #3548: all-arms-parked HITL escalation — the no-op-park sibling of the
# #3496 exhausted wedge. Same single-sourcing pattern; the restart/abort
# option labels are shared with the exhausted decision so operators see one
# vocabulary.
ARMS_PARKED_HITL_CONTEXT = "event_arms_parked"
ARMS_PARKED_RETRY_OPTION = "Retry arms (release no-op parks)"

# Warm-resume session-store substrate (#3278). The agent's Claude session store
# lives at ``$CLAUDE_CONFIG_DIR`` (default ``~/.claude`` = ``/home/egg/.claude``);
# we pin it explicitly so the wrapper's pull/push and the agent agree on the path
# regardless of HOME resolution. The pointer file is a pod-ephemeral local path
# the gate round-trips (the durable copy is orchestrator-owned in Redis). Both are
# injected only when warm resume is enabled, so default pods are byte-identical.
_POD_CLAUDE_CONFIG_DIR = "/home/egg/.claude"
_POD_SESSION_STATE_FILE = "/tmp/egg-session-state.json"  # noqa: S108 — pod-ephemeral, agent-local


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


# #3537: byte budget for the ``EGG_EVENT_RELEASE_CONTEXT`` env value. The
# composer's whole prompt envelope is 10 KiB (``PROMPT_ENVELOPE_MAX_BYTES``),
# so the release delta must stay a small fraction of it; resolution text is
# shrunk progressively and, as a last resort, dropped in favour of the bare
# decision ids (which alone still break the "retry the failing call" livelock).
_RELEASE_CONTEXT_ENV_MAX_BYTES = 3072


def _release_context_env_json(payload: dict[str, Any]) -> str:
    """Serialize the park-release delta for the pod env, size-capped (#3537).

    Mutates ``payload`` in place while shrinking (the caller builds it
    per-spawn). Shrinks the free-text fields (``resolution`` / ``question``)
    in steps; if the JSON still exceeds the budget, drops the enriched
    ``resolved_decisions`` list entirely - the id lists always survive.
    """
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) <= _RELEASE_CONTEXT_ENV_MAX_BYTES:
        return raw
    for cap in (800, 300, 100):
        for detail in payload.get("resolved_decisions") or []:
            for field in ("resolution", "question"):
                value = detail.get(field)
                if isinstance(value, str) and len(value) > cap:
                    detail[field] = value[:cap] + "…[truncated]"
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw.encode("utf-8")) <= _RELEASE_CONTEXT_ENV_MAX_BYTES:
            return raw
    payload.pop("resolved_decisions", None)
    return json.dumps(payload, ensure_ascii=False)


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
        release_context: dict[str, Any] | None = None,
    ) -> Any:
        agent_role = self._agent_role(role)
        branch = self._ex.get_worktree_branch(agent_role, slice_id=self._slice_id)
        # The one-shot wrapper keys on EGG_EVENT_ACTION (injected by
        # ``spawn_event_job`` via ``event_action`` below), not on an
        # ownership flag — the EGG_EVENT_LOOP_OWNER env was retired in #3164.
        env = self._ex.get_agent_env(agent_role)
        command, upstream, upstream_model, threshold, real_window = (
            self._ex._build_event_spawn_params(agent_role)
        )
        # Export the per-model reseed threshold so the in-pod resume-vs-reseed
        # gate (#3200 slice-8) and the #3249 measurement resolve a real-window
        # boundary instead of None (the gate's ``no_threshold`` safe-reseed
        # branch). Inert unless a discipline/resume/measurement flag is on in the
        # pod — the only consumers read it (#3279); a plain default pod ignores it.
        env["EGG_RESEED_THRESHOLD"] = str(threshold)
        # Export the REAL backend context window so the in-pod #3249 measurement
        # resolves ``real_backend_window`` / ``window_utilization`` instead of
        # degrading to None (``orchestrator`` is off the pod's ``PYTHONPATH``, so
        # ``measurement.py``'s fallback import can't compute it — #3316). Symmetric
        # with the ``EGG_RESEED_THRESHOLD`` injection above; the measurement reads
        # this ``EGG_REAL_BACKEND_WINDOW`` override first (measurement.py:229).
        env["EGG_REAL_BACKEND_WINDOW"] = str(real_window)
        # Warm-resume session-store substrate (#3278): pin the Claude session-store
        # location and the local pointer file the slice-8 gate round-trips, so the
        # wrapper's `egg-orch session-state pull|push` re-materialises the prior
        # transcript and `--resume` finds it. Gated on resume being enabled so a
        # default pod gets neither (byte-identical legacy path); the wrapper's
        # pull/push run under the same flag.
        if session_resume_enabled():
            env["CLAUDE_CONFIG_DIR"] = _POD_CLAUDE_CONFIG_DIR
            env["EGG_SESSION_STATE_FILE"] = _POD_SESSION_STATE_FILE
        # #3537: this spawn is the probe granted by a no-op-park release - the
        # world changed while the arm was parked (an operator resolved a
        # gating ``cq-N``, or the cohort's BRC state moved). Enrich the
        # resolved decision ids with their resolution text from the live
        # contract and export the delta so the pod-side prompt composer
        # (``routes/event_prompt/_cli.py``) can surface WHAT changed: without
        # it the released pod's prompt is byte-identical to the one that
        # parked, and a warm-resumed session replays its cached "still
        # blocked" plan forever. Best-effort: enrichment failure degrades to
        # the bare id lists, which still break the livelock.
        if release_context:
            env_payload: dict[str, Any] = dict(release_context)
            resolved_ids = release_context.get("resolved_decision_ids") or []
            if resolved_ids:
                details = self._ex._contract_decision_details(resolved_ids)
                if details:
                    env_payload["resolved_decisions"] = details
            env["EGG_EVENT_RELEASE_CONTEXT"] = _release_context_env_json(env_payload)
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
        # #3547: the run loop calls ``check_consensus`` every ~5s, so the
        # "consensus incomplete" observations repeat verbatim for minutes at
        # a time and define the INFO noise floor for the whole service. This
        # holds the last-logged incomplete state; the lines log at INFO only
        # when it changes and at DEBUG otherwise.
        self._last_incomplete_consensus_log: tuple[Any, ...] | None = None

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
        # Surface the agent timeout to the sandbox so the agent can warn
        # before the deadline and so the K8s Job active_deadline_seconds
        # matches the sandbox-side ClaudeConfig.timeout (#3665).
        agent_timeout = getattr(config, "agent_timeout_seconds", None)
        if agent_timeout is not None:
            env["EGG_AGENT_TIMEOUT_SECONDS"] = str(agent_timeout)
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

        # #3164: the orchestrator unconditionally owns the BRC event loop
        # and spawns a one-shot pod per actionable event — NO agents are
        # spawned up front. The tracker is registered above (the event loop
        # derives against it). ``agent_prompts`` is accepted for caller
        # compatibility but unused: the wrapper composes its own per-event
        # prompt via ``compose_event_prompt``.
        del agent_prompts
        self._start_event_loop(roles, tracker)
        return []

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
            hitl_probe=self._unresolved_contract_decision_ids,
            # #3465: a parked arm self-releases on consensus movement (a
            # cohort proposal/verdict never changes the parked arm's own
            # dedupe key, so this is the only non-heartbeat wake for an arm
            # parked while racing its upstream producer). ``getattr`` keeps
            # test doubles without the method on the heartbeat-only path.
            brc_probe=getattr(tracker, "consensus_state_fingerprint", None),
            # #3520: at the park transition the parked role's latest
            # WAITING_ON_ROLE heartbeat decides the alert's severity —
            # waiting on a live upstream producer is choreography, not a
            # wedge, so it must not fire at [high].
            waiting_probe=self._role_waiting_status,
            # #3364 PR C: the transient rate-limit path reports its two
            # transitions (cq-1 cumulative-wait threshold; deterministic-loop
            # guard) here; the executor owns the OVERSEER_ALERT + the loop-guard
            # halt (TASK-2-7). The paced retry itself runs through the normal
            # respawn gate (``ready_to_respawn`` honours the per-key rate-limit
            # backoff), so landed slices are never discarded.
            rate_limited_notifier=self._handle_rate_limited,
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
            arms_exhausted_notifier=self._handle_arms_exhausted,
            arms_exhausted_cleared_notifier=self._withdraw_arms_exhausted_hitl,
            arms_parked_notifier=self._handle_arms_parked,
            arms_parked_cleared_notifier=self._withdraw_arms_parked_hitl,
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

    def owns_event_loop(self) -> bool:
        """True when the orchestrator-owned BRC event loop drives this phase.

        The completion-poll site (``_run_concurrent_phase``) consults this to
        know that ``spawn_all`` returned ``[]`` *by design* — there are no
        up-front containers to wait on, so phase completion must be driven
        purely off ``check_consensus()`` + the consensus timeout, never off
        the empty container set. Concretely, the timeout fallthrough gates the
        incomplete-consensus HITL escalation (``return 1``) on this method so
        an orchestrator-owned slice that never converged is treated as the
        failure it is, rather than falling through to a bare ``return 0``. Set
        once ``spawn_all`` started the loop (always, post-#3164).
        """
        return self._event_loop is not None

    def stop_event_loop(self) -> None:
        """Stop the orchestrator-owned event loop if one is running (idempotent).

        Called by the completion-poll site on every exit path so the daemon
        thread does not outlive the phase (and stops requesting one-shot
        spawns) once consensus is reached, times out, or fails. A no-op if
        the loop was never started (e.g. a phase that short-circuited before
        ``spawn_all``).
        """
        loop = self._event_loop
        if loop is None:
            return
        try:
            loop.stop()
        except Exception:  # noqa: BLE001 — best-effort teardown
            logger.warning(
                "Failed to stop orchestrator-owned event loop cleanly",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                exc_info=True,
            )

    def _build_event_spawn_params(
        self, role: AgentRole
    ) -> tuple[list[str], str | None, str | None, int, int]:
        """Return ``(command, upstream, upstream_model, reseed_threshold, real_backend_window)`` for a role's event pod.

        The event-pump template composes its own per-event prompt at runtime
        (``invoke_agent_for_event``), so the initial prompt is irrelevant —
        only model + effort matter, resolved identically to ``_spawn_agent``.
        ``upstream``/``upstream_model`` are returned only when they differ
        from the default Anthropic decision (mirroring ``_spawn_agent``'s
        conditional forwarding) so the default-Claude wire shape is unchanged.

        ``reseed_threshold`` is the per-model token-occupancy boundary the
        in-pod resume-vs-reseed gate (#3200 slice-8) compares against. The
        orchestrator computes it here — it has the model decision, and the
        agent pod can't (``orchestrator`` is off the pod's ``PYTHONPATH``, so
        ``egg_agent.reseed.resolve_reseed_threshold`` resolves ``None`` without
        the ``EGG_RESEED_THRESHOLD`` override, taking the gate's ``no_threshold``
        safe-reseed branch every event). It is resolved against ``claude_code_alias``
        — the same string passed to ``--model``. Both in-pod consumers (the reseed
        gate, ``reseed.py:126-134``, and the #3249 measurement,
        ``measurement.py:252-262``) read this ``EGG_RESEED_THRESHOLD`` override
        first, so once it is always injected on the event path the injected
        threshold and the emitted measurement agree because they read the *same env
        var* — not because each independently re-resolves ``args.model``. And sub-1M
        LiteLLM models (whose bare alias carries their real-backend identity) resolve
        against their real window, not the ``[1m]``-implied 1M (#3279).

        ``real_backend_window`` is the true upstream context window (in tokens),
        resolved from the same ``claude_code_alias`` via
        :func:`agent_model_resolution.real_backend_window`. The in-pod #3249
        measurement reads it from the ``EGG_REAL_BACKEND_WINDOW`` override to
        compute ``window_utilization`` (occupancy / window); without it both
        window-relative metrics degrade to None (#3316). It is returned
        separately rather than recovered from ``reseed_threshold`` because the
        threshold is ``min(FLOOR, MARGIN * real_window)`` and so is not
        invertible once the floor binds.
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
        threshold = reseed_threshold(decision.claude_code_alias)
        real_window = real_backend_window(decision.claude_code_alias)
        return command, upstream, upstream_model, threshold, real_window

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
        # env vars to opt into 1M-context compaction math (#2832). Every route
        # also picks up the context-guardrail caps (#3175); the Anthropic path
        # carries only those — no custom-model registration — so the Claude
        # wire shape is unchanged.
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
            resolved_model=decision.claude_code_alias,
        )

    def _resolve_model_decision(self, role: AgentRole) -> AgentModelDecision:
        """Resolve the per-agent model decision for a role (#2769 slice-2).

        Pure over (role, pipeline_config, repo); when no override is
        configured the resolver returns a built-in Anthropic decision —
        ``opus`` for every role now that fable has been disabled (refine/plan
        roles unify on opus) — so the wire shape stays Anthropic-only.

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
        self,
        *,
        anomaly: str,
        priority: str,
        summary: str,
        detail: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Broadcast a sticky OVERSEER_ALERT for a failure-streak exhaustion.

        Mirrors the overseer monitor's broadcast convention: an
        ``OVERSEER_ALERT`` message to the ``all`` target with the anomaly name
        encoded in the subject (so ``/sdlc`` and any listener pick it up).
        Best-effort — a message-store hiccup must not wedge the loop.

        #3665 priority 4: enriches the OVERSEER_ALERT metadata with structured
        evidence so operators can act without hand-investigation. When the
        caller does not supply ``evidence``, the method builds it from the
        health monitor's activity ages and the BRC consensus tracker:

        * ``latest_heartbeat_age_s`` — seconds since the agent's last heartbeat
        * ``latest_tool_call_age_s`` — seconds since the agent's last tool call
        * ``last_progress_event`` — the agent's most recent progress event data
        * ``blocking_agents`` — the BRC consensus blocking set
        * ``consensus_state`` — the BRC consensus status dict
        * ``container_logs_tail`` — last N lines of container logs (best-effort)
        """
        try:
            if evidence is None:
                evidence = self._build_alert_evidence()
            metadata: dict[str, Any] = {
                "anomaly": anomaly,
                "priority": priority,
                "summary": summary,
            }
            if evidence:
                metadata["evidence"] = evidence
            get_message_store().add_message(
                Message(
                    pipeline_id=self.pipeline.id,
                    from_role="orchestrator",
                    to_role="all",
                    message_type=MessageType.OVERSEER_ALERT,
                    subject=f"{anomaly}: event-loop [{priority}]",
                    body=detail or summary,
                    phase=self.pipeline.current_phase.value,
                    metadata=metadata,
                )
            )
        except Exception as exc:  # noqa: BLE001 — alert emission is best-effort
            logger.warning(
                "Failed to broadcast supervision OVERSEER_ALERT",
                pipeline_id=self.pipeline.id,
                anomaly=anomaly,
                error=str(exc),
            )

    def _build_alert_evidence(self) -> dict[str, Any]:
        """Build structured evidence for a supervision alert (#3665 priority 4).

        Aggregates per-agent activity ages from the HealthMonitor and the BRC
        consensus blocking set from the peer-consensus tracker. All lookups are
        best-effort — a failure in any one leaves the corresponding field absent
        rather than raising.
        """
        evidence: dict[str, Any] = {}

        # Agent activity ages from the health monitor.
        try:
            from health_monitor import get_health_monitor

            hm = get_health_monitor()
            if hm is not None:
                activity = hm.get_agent_activity_ages()
                if activity:
                    # Pick the most-recently-active agent as the representative
                    # for the alert. If there's only one, that's the one.
                    best_role: str | None = None
                    best_age: float | None = None
                    for role, ages in activity.items():
                        hb_age = ages.get("last_heartbeat_age_s")
                        if hb_age is not None and (best_age is None or hb_age < best_age):
                            best_age = hb_age
                            best_role = role
                    if best_role is not None:
                        agent_info = activity[best_role]
                        evidence["agent_role"] = best_role
                        evidence["latest_heartbeat_age_s"] = agent_info.get("last_heartbeat_age_s")
                        evidence["latest_tool_call_age_s"] = agent_info.get("last_activity_age_s")
                        evidence["latest_progress_age_s"] = agent_info.get("last_progress_age_s")
                        # last_progress_event is stored on the agent state object.
                        agent = hm._agents.get(best_role)
                        if agent is not None:
                            evidence["last_progress_event"] = dict(agent.last_progress_data) if agent.last_progress_data else None
        except Exception:
            pass

        # BRC consensus state.
        try:
            from peer_consensus import get_peer_consensus_tracker

            tracker = get_peer_consensus_tracker(self.pipeline.id, self._slice_id)
            if tracker is not None:
                consensus = tracker.evaluate()
                evidence["blocking_agents"] = consensus.get("blocking_agents", [])
                evidence["consensus_state"] = {
                    "is_complete": consensus.get("is_complete", False),
                    "producer_phases": dict(tracker._producer_phases),
                    "reviewer_phases": dict(tracker._reviewer_phases),
                }
        except Exception:
            pass

        # Container logs tail (best-effort — the overseer fetches these at
        # _poll.py:78-85; we attempt the same for the alert payload).
        try:
            from container_backend import get_container_backend

            backend = get_container_backend()
            if backend is not None:
                # Try to get logs for the most recently active agent.
                if "agent_role" in evidence:
                    logs = backend.get_container_logs(evidence["agent_role"], tail=50)
                    if logs:
                        evidence["container_logs_tail"] = logs[-5000:]  # cap size
        except Exception:
            pass

        return evidence

    def _unresolved_contract_decision_ids(self) -> frozenset[str] | None:
        """Return ids of unresolved contract-resident decisions (#3425).

        Wired as the :class:`JobSupervisor`'s ``hitl_probe``: the fingerprint
        whose change releases a successful-no-op park. Resolving a contract
        HITL (``cq-N``) writes only the contract file — never the BRC tracker
        — so a parked arm cannot see the unblock through its dedupe key; this
        probe is how the resolution reaches the event loop. Read path mirrors
        the resolve fallback (``routes/decisions/_resolve.py``): the live
        contract in the shared pipeline worktree.

        Returns ``None`` (not the empty set) on any read failure — an
        unresolvable worktree, an exception, or a probe that itself signalled
        unknown — so the supervisor treats the fingerprint as unknown and
        falls back to the retry heartbeat, rather than mistaking an unreadable
        contract for "nothing pending" and releasing the park on the next
        successful read. A missing worktree during an *active* park is an
        anomaly (the shared pipeline worktree exists while agents are being
        spawned into it), so a transient ``resolve_pipeline_worktree`` → ``None``
        flap reads as "unknown", not "nothing pending" — otherwise the probed
        set would go ``{cq-3}`` → ``{}`` and release the park for a wasted probe
        spawn. ``ContractNotFoundError`` is the one exception: a resolvable
        worktree with genuinely no contract really means no decisions pending,
        so that maps to the empty set.

        This is *not* a one-time read: while an arm is parked, the loop calls
        it on every would-spawn poll tick (~5s) as the fast-release probe, so
        it re-reads the (small) contract file from disk each tick. It stays off
        the hot spawn path only in that it runs solely for parked keys, never
        for healthy ones.
        """
        try:
            import contract_store
            from egg_contracts import ContractNotFoundError, load_contract
            from routes.pipelines import _pipeline_identifier

            worktree = contract_store.resolve_pipeline_worktree(self.pipeline.id)
            if worktree is None:
                # "Unknown", not "nothing pending": a vanished worktree mid-park
                # is anomalous, so returning None (not the empty set) keeps the
                # park engaged on the retry heartbeat rather than releasing it.
                return None
            identifier = _pipeline_identifier(self.pipeline.issue_number, self.pipeline.id)
            try:
                contract = load_contract(identifier, worktree)
            except ContractNotFoundError:
                return frozenset()
            return frozenset(d.id for d in contract.decisions if not d.resolved)
        except Exception:  # noqa: BLE001 — probing is best-effort
            logger.warning(
                "Failed to probe contract decisions for no-op park fingerprint",
                pipeline_id=self.pipeline.id,
                exc_info=True,
            )
            return None

    def _contract_decision_details(self, decision_ids: list[str]) -> list[dict[str, Any]]:
        """Best-effort resolution details for park-released decisions (#3537).

        Called on the release-probe spawn path for the decision ids the
        supervisor recorded as having left the unresolved set while the arm
        was parked. Read path mirrors ``_unresolved_contract_decision_ids``
        (the live contract in the shared pipeline worktree - the same file
        the resolve route writes). Returns ``[]`` on any failure or when no
        listed id is found (e.g. the decision was removed rather than
        resolved): the caller then ships the bare id list, which alone still
        tells the respawned agent the blocker set changed.
        """
        try:
            import contract_store
            from egg_contracts import ContractNotFoundError, load_contract
            from routes.pipelines import _pipeline_identifier

            worktree = contract_store.resolve_pipeline_worktree(self.pipeline.id)
            if worktree is None:
                return []
            identifier = _pipeline_identifier(self.pipeline.issue_number, self.pipeline.id)
            try:
                contract = load_contract(identifier, worktree)
            except ContractNotFoundError:
                return []
            wanted = set(decision_ids)
            return [
                {
                    "id": d.id,
                    "question": d.question,
                    "resolved": bool(d.resolved),
                    "resolution": d.resolution,
                    "resolved_by": d.resolved_by,
                }
                for d in contract.decisions
                if d.id in wanted
            ]
        except Exception:  # noqa: BLE001 - enrichment is best-effort
            logger.warning(
                "Failed to enrich park-release decision details",
                pipeline_id=self.pipeline.id,
                decision_ids=decision_ids,
                exc_info=True,
            )
            return []

    def _role_waiting_status(self, role: str) -> tuple[str, bool] | None:
        """Return ``role``'s latest WAITING_ON_ROLE self-report status (#3520).

        Wired as the :class:`JobSupervisor`'s ``waiting_probe``, consulted
        once at the no-op park transition to pick the park alert's severity.
        Reads the message bus for ``role``'s most recent HEARTBEAT in the
        current phase; when that heartbeat self-reports
        ``state=WAITING_ON_ROLE`` this returns ``(waiting_on,
        waited_on_live)``, where ``waited_on_live`` is True iff every role
        named in ``metadata.waiting_on`` (comma-tolerant) emitted a bus
        message IN THE CURRENT PHASE within the health monitor's phase-aware
        staleness window (120s default in refine/plan/pr, 600s in implement —
        see below).

        Latest-heartbeat semantics are sound despite server-side dedup
        (``routes/messages.py``): only *consecutive identical* states are
        deduped, so the newest HEARTBEAT on the bus is always the role's
        current self-reported state. The phase filter keeps a previous
        phase's WAITING_ON_ROLE report (same pipeline stream) from being
        mistaken for current evidence. The broad latest-heartbeat read is
        HEAD-anchored so a dedup-aged (arbitrarily old but still current)
        WAITING_ON_ROLE self-report is always found; its residual tip-miss on
        a >30k-entry single-phase stream is closed by a supersession guard —
        if the tip-anchored liveness read shows the parked role's own latest
        in-window heartbeat is NOT WAITING_ON_ROLE, the self-report is treated
        as stale and this returns ``None`` (→ high alert).

        Returns ``None`` (unknown / no self-report) when the latest
        heartbeat is any other state, the role has no heartbeat this phase,
        the self-report has been superseded by a newer in-window heartbeat,
        or the read fails — the supervisor then falls back to the
        wedge-shaped high-priority alert, so a probe failure (or a stale
        self-report) can only make the alert MORE alarming, never quieter.
        """
        try:
            messages = get_message_store().get_messages(
                self.pipeline.id, limit=10000, slice_id=self._slice_id
            )
            phase = self.pipeline.current_phase.value
            latest = None
            for msg in messages:
                if (
                    msg.message_type == MessageType.HEARTBEAT
                    and msg.from_role == role
                    and msg.phase == phase
                ):
                    latest = msg
            if latest is None or latest.metadata.get("state") != "WAITING_ON_ROLE":
                return None
            waiting_on = str(latest.metadata.get("waiting_on") or "")
            waited_roles = [r.strip() for r in waiting_on.split(",") if r.strip()]
            if not waited_roles:
                return None
            # #3520: mirror the health monitor's phase-aware staleness
            # threshold (``health_monitor._get_heartbeat_threshold``): the
            # implement phase tolerates the longer implement heartbeat timeout
            # (default 600s), every other phase the shorter default (120s).
            # Reading the SAME ``PipelineConfig`` fields the monitor reads
            # keeps the two in lockstep under operator overrides — so a
            # producer this probe calls "live" is exactly one the monitor
            # would not yet have flagged stale, and the low-priority park
            # notice can never contradict a fresh ``heartbeat_timeout`` alert
            # about the same producer. A flat 600s would have called a
            # producer "live" for 5x the monitor's 120s window in refine/plan
            # — the very phases this PR targets.
            live_window_seconds = (
                self.pipeline.config.orchestrator_implement_heartbeat_timeout_seconds
                if phase == "implement"
                else self.pipeline.config.orchestrator_heartbeat_timeout_seconds
            )
            cutoff = datetime.now(UTC) - timedelta(seconds=live_window_seconds)
            # #3520 (review notes #2/#4): compute liveness from a dedicated
            # window-bounded, phase-filtered read rather than re-scanning the
            # broad latest-heartbeat fetch. ``since=cutoff`` anchors the read
            # near the stream tip, so the waited-on role's liveness stays
            # correct and cheap regardless of stream length — the unbounded
            # ``limit`` fetch above starts at the stream HEAD and can miss the
            # tip on a >30k-entry stream (note #2). The explicit ``>= cutoff``
            # filter is kept for precise sub-millisecond bounding on top of the
            # ``since`` stream-ID resolution. Phase-filtering (note #4) mirrors
            # the latest-heartbeat filter so a producer that emitted only in
            # the PRIOR phase, still inside the window, is not miscounted as
            # live right after a phase boundary. Both trims only ever SHRINK
            # the live set → fail-safe (toward the high-priority alert), never
            # a false low-priority notice. The latest-heartbeat read above
            # stays broad on purpose: server-side dedup can make the role's
            # current WAITING_ON_ROLE self-report arbitrarily old, so it must
            # be found outside any liveness window.
            recent = get_message_store().get_messages(
                self.pipeline.id, since=cutoff, limit=10000, slice_id=self._slice_id
            )

            def _at_or_after_cutoff(msg: Message) -> bool:
                ts = msg.timestamp if msg.timestamp.tzinfo else msg.timestamp.replace(tzinfo=UTC)
                return ts >= cutoff

            # #3520 (re-review note): guard the broad latest-heartbeat read's
            # residual tip-miss. That read is HEAD-anchored (``limit`` from
            # ``0-0``), so on a >30k-entry SINGLE-phase stream it can stop
            # before the tip and resolve ``latest`` to a stale WAITING_ON_ROLE
            # the role has since moved off of (e.g. dedup-exempt WORKING beats
            # at the tip while its arm is genuinely wedged). Left unguarded that
            # yields a low-priority "healthy wait" notice for a real wedge — the
            # one direction this probe must never take. The tip-anchored
            # ``recent`` read (``since=cutoff``) DOES see the tip, so if the
            # PARKED role's own latest in-window, current-phase heartbeat is not
            # WAITING_ON_ROLE, the self-report has been superseded → return
            # None → the high-priority wedge alert. When the role emitted
            # nothing in the window (its WAITING_ON_ROLE is deduped-old but
            # still current) the guard is inert and the downgrade still fires,
            # so the healthy-wait case (a self-report arbitrarily older than the
            # window) is preserved. This makes the "always degrades to high"
            # characterization hold in both directions.
            role_tip_state: str | None = None
            for msg in recent:
                if (
                    msg.message_type == MessageType.HEARTBEAT
                    and msg.from_role == role
                    and msg.phase == phase
                    and _at_or_after_cutoff(msg)
                ):
                    role_tip_state = msg.metadata.get("state")
            if role_tip_state is not None and role_tip_state != "WAITING_ON_ROLE":
                return None

            # #3520 (review notes #2/#4): compute liveness from the same
            # dedicated window-bounded, phase-filtered read rather than
            # re-scanning the broad latest-heartbeat fetch. ``since=cutoff``
            # anchors the read near the stream tip, so the waited-on role's
            # liveness stays correct and cheap regardless of stream length. The
            # explicit ``>= cutoff`` filter is kept for precise sub-millisecond
            # bounding on top of the ``since`` stream-ID resolution.
            # Phase-filtering (note #4) mirrors the latest-heartbeat filter so a
            # producer that emitted only in the PRIOR phase, still inside the
            # window, is not miscounted as live right after a phase boundary.
            # Both trims only ever SHRINK the live set → fail-safe (toward the
            # high-priority alert), never a false low-priority notice.
            recent_senders = {
                msg.from_role for msg in recent if msg.phase == phase and _at_or_after_cutoff(msg)
            }
            return (waiting_on, all(r in recent_senders for r in waited_roles))
        except Exception:  # noqa: BLE001 — probing is best-effort
            logger.warning(
                "Failed to probe WAITING_ON_ROLE heartbeat for no-op park alert",
                pipeline_id=self.pipeline.id,
                role=role,
                exc_info=True,
            )
            return None

    def _handle_arms_exhausted(
        self, *, report: list[dict[str, Any]], blocked_arms: list[tuple[str, str]]
    ) -> None:
        """Escalate the all-arms-exhausted livelock to the operator (#3496).

        Wired as the event loop's ``arms_exhausted_notifier``: every arm the
        slice needs in order to advance is blocked on an exhausted dedupe key
        (spawn budget spent), nothing is in flight, and exhaustion is
        terminal — the loop would otherwise sit silently logging "spawn
        blocked" until the consensus timeout hard-fails the slice hours
        later. Two surfaces, mirroring the consensus-timeout escalation:

        * an ``OVERSEER_ALERT`` broadcast (message-bus listeners), and
        * a **persisted** HITL decision (``pending_decisions`` — the surface
          the incident showed stays empty) whose options are executable on
          resolve (``routes/decisions/_handlers.py``): "Retry arms" clears
          the exhausted keys on the live loop for an in-band recovery;
          "Restart phase" tears the phase down; the abort option is
          recorded only (with a pointer at cancel_task — it does not stop
          the still-wedged phase on its own).

        Deduped on a pending decision with the same context: a second wedged
        slice (or a re-fired episode) never stacks a duplicate decision AND
        never re-broadcasts the alert — "Retry arms" resets every live loop
        of the pipeline, so one pending decision covers them all, and a
        re-armed per-loop latch would otherwise emit a redundant alert
        (#3496 review). The dedup read is best-effort and outside any state
        lock (two slices wedging on the same tick can still race past it,
        harmless given the sticky latch); a read failure falls through to
        escalating, since losing an escalation is worse than a possible
        duplicate. Best-effort throughout: an escalation failure must never
        wedge the event loop.
        """
        detail_lines = [
            (
                f"- {entry['role']}/{entry['action']}: streak={entry['streak']}, "
                f"recent terminations: {entry['exit_history_text']}"
            )
            for entry in report
        ]
        arms = ", ".join(f"{role}/{action}" for role, action in blocked_arms)
        slice_label = self._slice_id or "pipeline"
        detail = (
            f"Event loop for pipeline={self.pipeline.id} slice={slice_label} "
            f"phase={self.pipeline.current_phase.value} cannot advance: every "
            f"derivable spawn arm ({arms}) is blocked on an exhausted dedupe "
            f"key, no one-shot Job is in flight, and exhausted keys never "
            f"clear on their own. Exhausted keys:\n" + "\n".join(detail_lines)
        )
        # Read pending decisions up front so the dedup gate covers BOTH the
        # alert and the HITL. Best-effort: a read failure treats the pipeline
        # as not-yet-escalated so we still surface the wedge.
        store = None
        try:
            # Lazy import: routes.pipelines is too heavy to bind at module
            # import time (same precedent as _unresolved_contract_decision_ids).
            from routes import get_state_store_for_pipeline

            store, disk_pipeline = get_state_store_for_pipeline(self.pipeline.id)
            already_pending = any(
                d.context == ARMS_EXHAUSTED_HITL_CONTEXT
                for d in disk_pipeline.get_pending_decisions()
            )
        except Exception as exc:  # noqa: BLE001 — escalation must not wedge the loop
            logger.warning(
                "Failed to read pending decisions for arms-exhausted dedup; escalating anyway",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                error=str(exc),
            )
            already_pending = False

        if already_pending:
            logger.info(
                "Arms-exhausted HITL already pending; not re-alerting or stacking another",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
            )
            return

        self._emit_supervision_alert(
            anomaly="event-arms-exhausted",
            priority="high",
            summary=f"all spawn arms exhausted for slice {slice_label} — pipeline wedged",
            detail=detail,
        )

        if store is None:
            # The dedup read failed above, so we have no store to persist the
            # HITL through — the alert (message-bus surface) still fired.
            return
        try:
            from routes.pipelines import _persist_hitl_decision

            question = (
                f"{detail}\n\n"
                f"How to proceed?\n"
                f"- '{ARMS_EXHAUSTED_RETRY_OPTION}': clear the exhausted keys so the "
                f"blocked arms respawn with fresh budgets (in-band; nothing in "
                f"flight is torn down). If the underlying failure persists the "
                f"arms will re-exhaust and this decision will re-fire.\n"
                f"- '{ARMS_EXHAUSTED_RESTART_OPTION}': tear down and re-run the "
                f"current phase (work pushed to the shared branch is preserved).\n"
                f"- '{ARMS_EXHAUSTED_ABORT_OPTION}': recorded only — use cancel_task "
                f"to stop the pipeline."
            )
            decision = _persist_hitl_decision(
                self.pipeline.id,
                self.pipeline,
                store,
                question=question,
                options=[
                    ARMS_EXHAUSTED_RETRY_OPTION,
                    ARMS_EXHAUSTED_RESTART_OPTION,
                    ARMS_EXHAUSTED_ABORT_OPTION,
                ],
                phase=self.pipeline.current_phase,
                context=ARMS_EXHAUSTED_HITL_CONTEXT,
            )
            if decision is not None:
                logger.warning(
                    "Arms-exhausted HITL decision created",
                    pipeline_id=self.pipeline.id,
                    slice_id=self._slice_id,
                    decision_id=decision.id,
                    blocked_arms=arms,
                )
        except Exception as exc:  # noqa: BLE001 — escalation must not wedge the loop
            logger.warning(
                "Failed to persist arms-exhausted HITL decision",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                error=str(exc),
            )

    def _withdraw_arms_exhausted_hitl(self) -> None:
        """Auto-withdraw a stale arms-exhausted HITL once the wedge clears (#3496 review).

        Symmetric to :meth:`_handle_arms_exhausted`: wired as the event loop's
        ``arms_exhausted_cleared_notifier`` and fired once on the wedged→clear
        transition. When every derivable spawn arm recovers by a route other
        than the operator resolving this very decision — a fresh key derived, a
        spawn succeeded, an unrelated decision re-keyed the arms — the pending
        HITL is obsolete, so it is retracted rather than left for the operator
        to dispose of (mirrors ``_cancel_consensus_timeout_decisions`` on the
        convergence-success path).

        Pipeline-wide guard: the decision is deduped across slices (one
        decision covers them all), so it must NOT be withdrawn while another
        slice is still wedged. The calling loop clears its own latch before
        firing this, so the live-loop registry check sees only the *other*
        slices' latches — a still-wedged sibling holds the decision in place.

        Best-effort throughout: a withdrawal failure must never wedge the loop
        (the loop wraps this call, but the state-store work is guarded here
        too so a partial failure is logged with context).
        """
        try:
            # Lazy imports: same heavy-module reason as _handle_arms_exhausted.
            from event_loop import get_live_event_loops
            from routes import get_state_store_for_pipeline
            from routes.pipelines import _withdraw_arms_exhausted_decisions

            if any(
                loop.arms_exhausted_escalated for loop in get_live_event_loops(self.pipeline.id)
            ):
                # A sibling slice of this pipeline is still wedged on the shared
                # decision — leave it pending for that slice to resolve.
                return
            store, _ = get_state_store_for_pipeline(self.pipeline.id)
            withdrawn = _withdraw_arms_exhausted_decisions(self.pipeline.id, store)
            if withdrawn:
                logger.info(
                    "Auto-withdrew stale arms-exhausted HITL after the wedge cleared",
                    pipeline_id=self.pipeline.id,
                    slice_id=self._slice_id,
                    withdrawn=withdrawn,
                )
        except Exception as exc:  # noqa: BLE001 — withdrawal must not wedge the loop
            logger.warning(
                "Failed to auto-withdraw arms-exhausted HITL after wedge cleared",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                error=str(exc),
            )

    def _handle_arms_parked(
        self,
        *,
        report: list[dict[str, Any]],
        exhausted_report: list[dict[str, Any]],
        blocked_arms: list[tuple[str, str]],
    ) -> None:
        """Escalate the all-arms-parked stall to the operator (#3548).

        Wired as the event loop's ``arms_parked_notifier`` — the no-op-park
        sibling of :meth:`_handle_arms_exhausted`. Every arm the slice needs
        in order to advance is blocked on a no-op-parked (or exhausted)
        dedupe key with nothing in flight. A park does self-release, but
        only for one probe spawn per fingerprint change or 30-minute
        heartbeat; the #3548 incident sat silent for the full window with
        ``pending_decisions`` empty. Same two surfaces and the same
        dedup/best-effort posture as the exhausted escalation:

        * an ``OVERSEER_ALERT`` broadcast (message-bus listeners), and
        * a **persisted** HITL decision whose "Retry arms" resolution
          releases the parks on the live loop(s) in-band
          (``routes/decisions/_handlers.py``).
        """
        detail_lines = [
            (
                f"- {entry['role']}/{entry['action']}: "
                f"{entry['noop_streak']} consecutive no-op completions (parked)"
            )
            for entry in report
        ] + [
            (
                f"- {entry['role']}/{entry['action']}: streak={entry['streak']}, "
                f"recent terminations: {entry['exit_history_text']} (exhausted)"
            )
            for entry in exhausted_report
        ]
        arms = ", ".join(f"{role}/{action}" for role, action in blocked_arms)
        slice_label = self._slice_id or "pipeline"
        detail = (
            f"Event loop for pipeline={self.pipeline.id} slice={slice_label} "
            f"phase={self.pipeline.current_phase.value} cannot advance: every "
            f"derivable spawn arm ({arms}) is blocked on a no-op-parked (or "
            f"exhausted) dedupe key and no one-shot Job is in flight. Each "
            f"parked arm's agent keeps completing cleanly with zero BRC "
            f"progress, so respawning it unchanged cannot converge the round "
            f"— it is typically blocked on missing upstream state (e.g. a "
            f"producer that never proposed) or an unresolved operator "
            f"decision. Blocked arms:\n" + "\n".join(detail_lines)
        )
        store = None
        try:
            from routes import get_state_store_for_pipeline

            store, disk_pipeline = get_state_store_for_pipeline(self.pipeline.id)
            already_pending = any(
                d.context == ARMS_PARKED_HITL_CONTEXT for d in disk_pipeline.get_pending_decisions()
            )
        except Exception as exc:  # noqa: BLE001 — escalation must not wedge the loop
            logger.warning(
                "Failed to read pending decisions for arms-parked dedup; escalating anyway",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                error=str(exc),
            )
            already_pending = False

        if already_pending:
            logger.info(
                "Arms-parked HITL already pending; not re-alerting or stacking another",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
            )
            return

        self._emit_supervision_alert(
            anomaly="event-arms-parked",
            priority="high",
            summary=f"all spawn arms no-op-parked for slice {slice_label} — round stalled",
            detail=detail,
        )

        if store is None:
            return
        try:
            from routes.pipelines import _persist_hitl_decision

            question = (
                f"{detail}\n\n"
                f"How to proceed?\n"
                f"- '{ARMS_PARKED_RETRY_OPTION}': release the no-op parks so the "
                f"blocked arms respawn immediately (in-band; nothing in flight "
                f"is torn down). If the agents keep no-oping the arms will "
                f"re-park and this decision will re-fire.\n"
                f"- '{ARMS_EXHAUSTED_RESTART_OPTION}': tear down and re-run the "
                f"current phase (work pushed to the shared branch is preserved).\n"
                f"- '{ARMS_EXHAUSTED_ABORT_OPTION}': recorded only — use cancel_task "
                f"to stop the pipeline."
            )
            decision = _persist_hitl_decision(
                self.pipeline.id,
                self.pipeline,
                store,
                question=question,
                options=[
                    ARMS_PARKED_RETRY_OPTION,
                    ARMS_EXHAUSTED_RESTART_OPTION,
                    ARMS_EXHAUSTED_ABORT_OPTION,
                ],
                phase=self.pipeline.current_phase,
                context=ARMS_PARKED_HITL_CONTEXT,
            )
            if decision is not None:
                logger.warning(
                    "Arms-parked HITL decision created",
                    pipeline_id=self.pipeline.id,
                    slice_id=self._slice_id,
                    decision_id=decision.id,
                    blocked_arms=arms,
                )
        except Exception as exc:  # noqa: BLE001 — escalation must not wedge the loop
            logger.warning(
                "Failed to persist arms-parked HITL decision",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                error=str(exc),
            )

    def _withdraw_arms_parked_hitl(self) -> None:
        """Auto-withdraw a stale arms-parked HITL once the stall clears (#3548).

        Symmetric to :meth:`_handle_arms_parked` and modeled on
        :meth:`_withdraw_arms_exhausted_hitl`, including the pipeline-wide
        guard: the decision is deduped across slices, so it must not be
        withdrawn while another slice's loop is still inside an escalated
        parked episode.
        """
        try:
            from event_loop import get_live_event_loops
            from routes import get_state_store_for_pipeline
            from routes.pipelines import _withdraw_arms_parked_decisions

            if any(loop.arms_parked_escalated for loop in get_live_event_loops(self.pipeline.id)):
                return
            store, _ = get_state_store_for_pipeline(self.pipeline.id)
            withdrawn = _withdraw_arms_parked_decisions(self.pipeline.id, store)
            if withdrawn:
                logger.info(
                    "Auto-withdrew stale arms-parked HITL after the stall cleared",
                    pipeline_id=self.pipeline.id,
                    slice_id=self._slice_id,
                    withdrawn=withdrawn,
                )
        except Exception as exc:  # noqa: BLE001 — withdrawal must not wedge the loop
            logger.warning(
                "Failed to auto-withdraw arms-parked HITL after stall cleared",
                pipeline_id=self.pipeline.id,
                slice_id=self._slice_id,
                error=str(exc),
            )

    def _handle_propose_arm_exhaustion(
        self, *, role: str, action: str, dedupe_key: str, streak: int, fatal: bool = False
    ) -> None:
        """Engage the existing AGENT_FAILED path for a producer propose arm.

        #2806 relocated for orchestrator mode: when a producer's propose arm
        exhausts its retry budget the producer is effectively stuck, so route
        it through :meth:`_handle_single_failure` (AGENT_FAILED broadcast +
        crash handling + HITL decision) — the same path a long-lived pod
        failure takes in pod mode.

        ``fatal`` (#3373): the arm hit a non-retryable credential / quota
        failure (the agent exited ``EX_AUTH_FATAL``) and was exhausted on its
        *first* failure, not after the streak-to-10 budget. The ``error`` that
        becomes the AGENT_FAILED body and the HITL question must name the
        credential cause and its remediation — the generic "exhausted after N
        consecutive failures" wording would be both false (there was one
        failure, not ``streak``) and unactionable, exactly the degraded
        operator surface this work set out to replace.
        """
        if fatal:
            error = (
                f"producer propose arm hit a non-retryable credential/quota failure "
                f"(dedupe_key={dedupe_key}): the agent's Claude credential was rejected "
                f"(subscription weekly/usage limit, expired/invalid token, 401, or "
                f"exhausted credit balance). Not retried — a respawn would re-use the "
                f"same rejected credential. Remediation: rotate the Claude credential "
                f"(set the intended account as the active CLAUDE_CODE_OAUTH_TOKEN in "
                f"secrets.env and apply the gateway secret), then restart this phase to "
                f"mint a fresh dedupe key so pods respawn."
            )
        else:
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

    def _handle_rate_limited(
        self,
        *,
        role: str,
        action: str,
        dedupe_key: str,
        retry_count: int,
        cumulative_wait_seconds: float,
        backoff_seconds: float,
        threshold_crossed: bool,
        deterministic_loop: bool,
        fingerprint: Any,
    ) -> None:
        """React to a transient rate-limit outcome's reported transitions (#3364 PR C).

        Wired as the :class:`JobSupervisor`'s ``rate_limited_notifier``. The
        supervisor already PACED the respawn across the cap window (the paced
        retry needs no action here — it runs through the normal respawn gate,
        so completed slices are never discarded, AC-C3). This handler owns the
        two operator-facing reactions the supervisor deliberately does not take
        itself:

        * ``threshold_crossed`` (cq-1, AC-C5): the cumulative paced wait crossed
          the threshold. Emit an ``OVERSEER_ALERT`` so an attended operator is
          informed WHILE auto-recovery continues — there is NO hard wall-clock
          ceiling; the paced retry keeps going until the cap lifts.
        * ``deterministic_loop`` (AC-C4): the identical failure fingerprint
          reproduced at the same progression point past the loop-guard
          threshold — a deterministic failure masquerading as a throttle. Emit
          a distinct named alert and HALT the paced retry (mark the key
          exhausted) so the loop stops and the arms-exhausted HITL takes over,
          rather than looping forever. Orthogonal to the threshold alert.

        Best-effort throughout: an alerting/halt failure must never wedge the
        event loop (it fires from inside the supervisor's record path).
        """
        try:
            if threshold_crossed:
                self._emit_supervision_alert(
                    anomaly="agent-rate-limited",
                    priority="high",
                    summary=(
                        f"agent throttled — paced auto-retry continuing "
                        f"(action={action}, role={role})"
                    ),
                    detail=(
                        f"Event-pump for role={role} (action={action}) has been "
                        f"paused by a transient rate-limit / cap wall for a "
                        f"cumulative {int(cumulative_wait_seconds)}s across "
                        f"{retry_count} paced retries (dedupe_key={dedupe_key}). "
                        f"This is NOT a halt: the orchestrator keeps retrying on "
                        f"a paced cadence (currently ~{int(backoff_seconds)}s) "
                        f"until the cap lifts — a weekly/subscription cap can "
                        f"stay shut for hours-to-days. Completed slices are "
                        f"preserved. This alert fires once so an attended "
                        f"operator knows the pipeline is waiting on a cap wall, "
                        f"not stuck; no action is required for auto-recovery."
                    ),
                )
            if deterministic_loop:
                self._emit_supervision_alert(
                    anomaly="rate-limit-deterministic-loop",
                    priority="high",
                    summary=(
                        f"rate-limit paced retry reproducing an identical failure "
                        f"— escalating (action={action}, role={role})"
                    ),
                    detail=(
                        f"Event-pump for role={role} (action={action}) has "
                        f"reproduced the IDENTICAL failure fingerprint at the "
                        f"same progression point across {retry_count} paced "
                        f"rate-limit retries (dedupe_key={dedupe_key}) with no "
                        f"state advance. This is a deterministic failure "
                        f"masquerading as a transient throttle, so the paced "
                        f"retry is halted to stop an infinite loop; the "
                        f"arms-exhausted HITL surfaces retry / restart-phase / "
                        f"abort options (restart preserves work pushed to the "
                        f"shared branch). Fingerprint: {fingerprint}."
                    ),
                )
                # Halt the paced loop (the executor's decision, not the
                # recorder's): mark the key exhausted so the loop stops
                # respawning it and the arms-exhausted escalation takes over.
                loop = self._event_loop
                if loop is not None:
                    loop.supervisor.halt_rate_limited(dedupe_key)
        except Exception as exc:  # noqa: BLE001 — must never wedge the loop
            logger.warning(
                "Failed to handle rate-limit transition",
                pipeline_id=self.pipeline.id,
                role=role,
                action=action,
                dedupe_key=dedupe_key,
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
                # #3547: this branch runs on every ~5s poll tick, so at INFO
                # these lines drown the service log (2 lines x N slices every
                # tick). Log at INFO only when the incomplete state actually
                # changes; the unchanged repeats drop to DEBUG.
                incomplete_state = (
                    confirmed_in_tracker,
                    len(all_roles),
                    tuple(sorted(result.get("blocking_agents", []))),
                    bool(result.get("has_unresolved_nacks", False)),
                )
                state_changed = incomplete_state != self._last_incomplete_consensus_log
                self._last_incomplete_consensus_log = incomplete_state
                log_incomplete = logger.info if state_changed else logger.debug
                log_incomplete(
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
                    log_incomplete(
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
                            log_incomplete(
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
            if result.get("is_complete"):
                # Consensus reached (possibly via a fallback override): reset
                # so the next incomplete round logs its first tick at INFO.
                self._last_incomplete_consensus_log = None
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

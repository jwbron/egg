"""
Kubernetes spawner with integrated gateway session management.

Provides high-level Job spawning that replaces ContainerSpawner for
Kubernetes deployments:
- Creates Kubernetes Jobs via KubernetesClient
- Registers sessions with gateway (token-only auth, no IP binding)
- Injects proper environment configuration (GATEWAY_URL, proxy, DNS, etc.)
- Handles worktree setup via gateway_client.create_worktrees()
- Cleans up sessions on Job removal
"""

import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Add shared directory to path for logging and config
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


# ``agent_salvage`` and ``GatewayError`` stay imported on the barrel even
# though the barrel body no longer references them directly: they are live
# patch seams. Submodules reach them via ``_pkg.agent_salvage`` /
# ``_pkg.GatewayError`` and the suite patches ``kubernetes_spawner.agent_salvage``
# / ``kubernetes_spawner.GatewayError`` (#3312).
import agent_salvage  # noqa: F401
from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT
from gateway_client import (  # noqa: F401
    GatewayClient,
    GatewayError,
    get_gateway_client,
)
from kubernetes_client import (
    DEFAULT_NAMESPACE,
    KubernetesClient,
    get_kubernetes_client,
)
from models import AgentRole, ContainerInfo, ContainerStatus

# #2725: senders we always include in EGG_WAIT_PRODUCER_ALLOWLIST so
# system-emitted bus messages keep waking slice-scoped waiters. The
# overseer emits OVERSEER_ALERT and the orchestrator emits
# CONSENSUS_RE_REVIEW + the "ready to confirm" STATUS nudge; both
# carry these literal ``from_role`` values, so excluding them would
# turn the filter into a deadlock surface.
_WAIT_ALLOWLIST_SYSTEM_SENDERS: tuple[str, ...] = ("overseer", "orchestrator")


if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger("orchestrator.kubernetes_spawner")

# Must match the gateway's WORKTREE_BASE_DIR and docker-compose volume mounts.
WORKTREE_BASE_DIR = Path("/home/egg/.egg-worktrees")

# Default k8s service URLs for gateway and orchestrator
GATEWAY_K8S_URL = os.environ.get(
    "GATEWAY_K8S_URL", f"http://gateway.egg-system.svc.cluster.local:{GATEWAY_PORT}"
)
ORCHESTRATOR_K8S_URL = os.environ.get(
    "ORCHESTRATOR_K8S_URL", "http://orchestrator.egg-system.svc.cluster.local:9849"
)
PROXY_URL = os.environ.get(
    "EGG_PROXY_URL", f"http://gateway.egg-system.svc.cluster.local:{GATEWAY_PROXY_PORT}"
)

# Environment variables that extra_env must never override. Both upper and
# lowercase proxy variants are covered because many HTTP clients (curl,
# requests, libcurl) honor either case, so omitting the lowercase forms
# would leave a defense-in-depth hole.
_PROTECTED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "EGG_SESSION_TOKEN",
        "GATEWAY_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "EGG_ORCHESTRATOR_URL",
        # Lifecycle-control bearer token for the orchestrator's HITL/phase
        # endpoints. Agents must never hold it — the auto-approval incident
        # in #1769 was caused by in-cluster pods reaching unauth'd
        # /resolve. Blocking the key here is defense in depth; the base
        # spawner env below never sets it to begin with.
        "EGG_LIFECYCLE_SECRET",
        # Slice scope (#2410). The spawner is the
        # single source of truth: ``EGG_SLICE_ID`` is derived from the
        # ``slice_id`` parameter that already drives Job naming and
        # worktree id. Protecting the key prevents a future caller from
        # silently shipping a mismatched value via ``extra_env`` —
        # without this, the agent's signals could land on a different
        # slice than its Job/worktree, with no warning.
        "EGG_SLICE_ID",
        # Wait-loop producer allowlist (#2725). Spawner derives this
        # from the BRC review graph for the (phase, role) being spawned
        # — protecting it prevents an upstream ``extra_env`` from
        # silently substituting a stale or wrong allowlist, which would
        # cause the agent to sleep through legitimate ACK/NACK/PROPOSE
        # events without surfacing the misconfiguration.
        "EGG_WAIT_PRODUCER_ALLOWLIST",
        # Base branch for the BRC event-pump git-log delta (#2967). The
        # spawner derives this from the same resolved base branch used to
        # create the worktree and to build the agent prompt's diff commands,
        # so it must stay consistent across all three. An ``extra_env``
        # override could otherwise point the ``--not origin/<base>`` delta at
        # a different branch than the worktree was based on, silently
        # corrupting the re-review scope.
        "EGG_BASE_BRANCH",
        # Same single-source-of-truth shape (#2428). The agent's
        # ``egg-orch push`` retargets the refspec to ``HEAD:$EGG_BRANCH``
        # (sandbox/egg_lib/cli_push.py); the gateway's session-scoped
        # allowlist then compares that target against the
        # ``assigned_branch`` registered at session creation. The
        # spawner derives both from the same ``branch`` parameter, so
        # they agree. An ``extra_env`` value sneaking in from upstream
        # (e.g. the run loop's pipeline-level ``sandbox_env``) used to
        # win because the override loop ran after the spawner's
        # default, leaving slice agents pushing to ``<pid>/work``
        # instead of ``<pid>/<slice>`` — every coder push was rejected.
        "EGG_BRANCH",
        # Session-token placeholder credential (#2817). The spawner is the
        # single source of truth: both keys are derived from the same
        # ``session_token`` below (one is set per spawn, keyed on
        # ``upstream``). Protecting them matches the ``EGG_SESSION_TOKEN``
        # treatment — an ``extra_env`` override could otherwise desync the
        # credential header from the session the gateway resolves, leaving
        # the session unauthenticatable. No current caller passes either
        # via ``extra_env``; this is defense in depth.
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)


# Context-discipline flags (#3200) that the orchestrator forwards from its OWN
# env into every spawned agent pod when set. These switches are read *in-pod* —
# by the agent (``egg_agent.context_discipline`` / ``egg_agent.session``) and by
# the wrapper's in-pod prompt composer (``event_prompt._context_discipline_enabled``)
# — but the orchestrator deployment is the one process an operator can flip them
# on. Nothing else wires them into the Job env (no ``envFrom``, and ``sandbox_env``
# is built from pipeline fields), so without this forward ``kubectl set env`` on
# the orchestrator is a no-op for agents. Forwarding them here makes that the
# single operator knob. Default-OFF semantics are preserved: an unset/blank flag
# is simply not forwarded, so the pod stays on the legacy path byte-for-byte. The
# forward runs BEFORE the ``extra_env`` merge, so a per-spawn override still
# wins. ``EGG_SESSION_RESUME`` rides along as the narrower staged-rollout knob;
# the per-spawn substrate (``EGG_SESSION_STATE_FILE`` / ``EGG_RESEED_THRESHOLD``)
# needs real per-pod wiring and is deliberately NOT a blind forward.
#
# ``EGG_CONTEXT_MEASUREMENT`` forwards the #3249 emit-only measurement knob: its
# in-pod consumer landed in #3271 as ``egg_agent.measurement`` under the fixed
# name ``MEASUREMENT_ENV = "EGG_CONTEXT_MEASUREMENT"``, which ``record_measurement``
# gates on. Without forwarding it the surfaces no-op in every pod (#3277), so the
# instrumented proving run captures zero metrics even with discipline active.
_FORWARDED_DISCIPLINE_ENV_KEYS: tuple[str, ...] = (
    "EGG_CONTEXT_DISCIPLINE",
    "EGG_CONTEXT_MEASUREMENT",
    "EGG_SESSION_RESUME",
)


# --- Orchestrator-owned one-shot event spawns --------------------------
#
# When the orchestrator (not the in-pod wait-loop) owns the BRC event loop
# it spawns a per-event Job that handles exactly one ``propose|ack|nack``
# and exits (the wrapper's one-shot event arm). The Job carries the event identity in
# env so the wrapper arm engages, and the dedupe key as a *label* so the
# event loop can reconcile in-flight Jobs after an orchestrator restart
# without persisting any spawn bookkeeping.
LABEL_EVENT_DEDUPE = "egg.event.dedupe-key"
LABEL_EVENT_ACTION = "egg.event.action"

# Env keys read by the consensus wrapper's one-shot event handler
# (``consensus_wrapper.py``). ``EGG_EVENT_ACTION`` engages the handler;
# ``EGG_EVENT_DEDUPE_KEY`` is the stale-event backstop / reconciliation
# handle. None are in ``_PROTECTED_ENV_KEYS`` — the one-shot spawn is
# their only writer. (The ``EGG_EVENT_LOOP_OWNER`` ownership flag was
# retired in #3164 — the orchestrator always owns the event loop.)
ENV_EVENT_ACTION = "EGG_EVENT_ACTION"
ENV_EVENT_DEDUPE_KEY = "EGG_EVENT_DEDUPE_KEY"
ENV_EVENT_PAYLOAD_REFS = "EGG_EVENT_PAYLOAD_REFS"

# A short, deterministic Job-name discriminator so distinct events for one
# role get distinct Job names (the same event always yields the same name,
# which keeps the pre-spawn cleanup + adoption coherent). 8 hex chars of the
# already-hashed dedupe key is plenty of separation.
_EVENT_JOB_NAME_DISCRIMINATOR_LEN = 8

# Kubernetes caps label VALUES (and names) at 63 characters and rejects any
# overflow at the API server. The dedupe key is a 64-char sha256 hexdigest, so
# it must be shortened to a label-safe form before it can ride as a Job label
# or be queried in a label selector. The full key still rides in env
# (``EGG_EVENT_DEDUPE_KEY``, no length cap) and remains the in-memory dedupe
# identity; only the label/selector use this shortened form — and they MUST use
# the IDENTICAL value or restart reconciliation can never match.
_LABEL_VALUE_MAXLEN = 63


# --- spawn-retry policy (#1839) -------------------------------------------
# A single transient gateway error used to kill the whole pipeline. We now
# retry a bounded number of times with exponential backoff for failures
# that look transient (network/timeout/5xx), and fail fast for permanent
# errors (404 "Repository not found", 400/422 validation, etc.).

DEFAULT_SPAWN_MAX_RETRIES = 2
DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS = 2.0
_SPAWN_RETRY_BACKOFF_MULTIPLIER = 2.5
_TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_PERMANENT_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 404, 422})
_PERMANENT_MESSAGE_FRAGMENTS: tuple[str, ...] = (
    "repository not found",
    "invalid container_id",
)


# ------------------------------------------------------------------
# Worktree re-attach helpers
# ------------------------------------------------------------------


# Roles that can run without a per-agent git worktree.  Reviewers and
# operator-facing roles only poll consensus messages / pipeline state —
# they never commit or push, so spawning them with ``repos=[]`` is a
# valid configuration.  Every other role produces or edits files in a
# worktree, so spawning it without one would stall the pipeline with
# "Worktree not found" on the first git call (#1869).
_ROLES_WITHOUT_WORKTREE: frozenset[AgentRole] = frozenset(
    {
        AgentRole.REVIEWER_CODE,
        AgentRole.REVIEWER_CODE_HOLISTIC,
        AgentRole.REVIEWER_CONTRACT,
        AgentRole.REVIEWER_AGENT_DESIGN,
        AgentRole.REVIEWER_REFINE,
        AgentRole.REVIEWER_PLAN,
        AgentRole.REVIEWER_SECURITY,
        AgentRole.REVIEWER_CONCURRENCY,
        AgentRole.OVERSEER,
    }
)


class KubernetesSpawner:
    """Spawns Kubernetes Jobs with integrated gateway session management.

    Handles the full lifecycle:
    1. Validate gateway health
    2. Register gateway session (token-only, no IP binding)
    3. Create Kubernetes Job via KubernetesClient
    4. Clean up session on Job removal
    """

    DEFAULT_SANDBOX_IMAGE = os.environ.get("EGG_SANDBOX_IMAGE", "egg:latest")
    JOB_NAME_FORMAT = "egg-agent-{pipeline_id}-{role}"
    JOB_NAME_FORMAT_SLICE = "egg-agent-{pipeline_id}-{slice_id}-{role}"

    @classmethod
    def _build_k8s_job_names(
        cls,
        pipeline_id: str,
        agent_role: AgentRole,
        slice_id: str | None = None,
    ) -> tuple[str, str]:
        """Build the two identifiers an agent Job is known by.

        Returns a ``(job_name, actual_k8s_job_name)`` pair where:

        - ``job_name`` is the unprefixed identifier used as the gateway
          session ``container_id`` and in labels (e.g.
          ``egg-agent-issue-1962-task-planner`` or, for slice-scoped
          spawns, ``egg-agent-issue-2261-v7-slice-2-coder``).
        - ``actual_k8s_job_name`` is the real k8s Job name after
          ``KubernetesClient`` prepends ``JOB_PREFIX`` during
          ``create_container``.

        Underscores in ``agent_role.value`` (``task_planner``,
        ``reviewer_refine``, …) are converted to hyphens because k8s
        resource names are RFC-1123 labels and reject underscores.

        Slice scope (#2403): when ``slice_id`` is supplied, it is
        embedded between the pipeline id and the role so concurrent
        slices in the same pipeline don't collide on a single Job name
        (which would cause ``spawn_agent_job``'s pre-spawn cleanup to
        delete the in-flight sibling slice's Job — see line 405).
        """
        if slice_id:
            job_name = cls.JOB_NAME_FORMAT_SLICE.format(
                pipeline_id=pipeline_id,
                slice_id=slice_id,
                role=agent_role.value.replace("_", "-"),
            )
        else:
            job_name = cls.JOB_NAME_FORMAT.format(
                pipeline_id=pipeline_id,
                role=agent_role.value.replace("_", "-"),
            )
        return job_name, f"{KubernetesClient.JOB_PREFIX}{job_name}"

    @staticmethod
    def _build_agent_worktree_id(
        pipeline_id: str,
        agent_role: AgentRole,
        slice_id: str | None = None,
    ) -> str:
        """Build the per-agent worktree identifier.

        The id is the gateway worktree key (``container_id`` for
        ``create_worktrees`` / ``delete_worktrees``) and the agent's
        ``CONTAINER_ID`` env var. For slice-scoped spawns it embeds the
        slice id (#2403) so concurrent slices don't share a worktree —
        otherwise slice-N's coder would inherit slice-(N-1)'s worktree
        contents (or step on them mid-flight).
        """
        if slice_id:
            return f"{pipeline_id}-{slice_id}-{agent_role.value}"
        return f"{pipeline_id}-{agent_role.value}"

    def __init__(
        self,
        k8s_client: KubernetesClient | None = None,
        gateway_client: GatewayClient | None = None,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        docker_client: Any | None = None,
        clock: "Callable[[], float] | None" = None,  # noqa: UP037
    ):
        """Initialize Kubernetes spawner.

        Args:
            k8s_client: Kubernetes client (default: singleton)
            gateway_client: Gateway client (default: singleton)
            namespace: Kubernetes namespace for agent Jobs
            docker_client: Backward-compat alias for ``k8s_client``.
                Accepted so that code written for ``ContainerSpawner``
                continues to work via the shim.
        """
        # Accept docker_client as backward-compat alias for k8s_client
        if docker_client is not None and k8s_client is None:
            k8s_client = docker_client
        self._k8s = k8s_client
        self._gateway = gateway_client
        self._namespace = namespace
        # Track restart counts per (pipeline_id, agent_role, slice_id) tuple.
        # ``slice_id`` is ``None`` for pipeline-level agents and
        # ``"slice-<N>"`` for slice-scoped agents (#2410), so concurrent
        # slices each get an independent budget.
        self._restart_counts: dict[tuple[str, str, str | None], int] = {}
        # Per-(pipeline_id, agent_role, slice_id) locks for serialising
        # concurrent restarts. Protected by _restart_locks_lock (same
        # pattern as state_store.py).
        self._restart_locks: dict[tuple[str, str, str | None], threading.Lock] = {}
        self._restart_locks_lock = threading.Lock()
        # Per-role gateway-session token cache.
        # Key: (pipeline_id, agent_role_value, slice_id, session_container_id)
        # where ``session_container_id`` is the STABLE per-role+slice base Job
        # name (``_build_k8s_job_names``) — NOT the per-event discriminated Job
        # name. Keying on the stable id is what lets a session be reused across
        # a role's successive one-shot events (propose, ack, …); keying on the
        # per-event name would miss the cache on every distinct event and
        # re-register. Both the write side (``spawn_agent_
        # job`` via ``session_container_id``) and the read side (``_get_or_
        # create_session``) build this element from the same stable base name
        # and use ``agent_role.value`` (str), never the enum member directly.
        # ``_teardown_session`` / ``cleanup_pipeline`` evict entries so the
        # cache stays bounded by roster size.
        self._session_token_cache: dict[tuple[str, str, str | None, str], str] = {}
        # Monotonic clock for the spawn→invoke latency budget.
        # Injectable so the p50<60s budget test can drive a simulated clock
        # (no real sleeps). Defaults to ``time.monotonic``.
        self._clock: "Callable[[], float]" = clock or time.monotonic  # noqa: UP037

    # ------------------------------------------------------------------
    # Worktree re-attach instance methods
    # ------------------------------------------------------------------

    @property
    def k8s(self) -> KubernetesClient:
        """Get Kubernetes client (lazy initialization)."""
        if self._k8s is None:
            self._k8s = get_kubernetes_client(self._namespace)
        return self._k8s

    @property
    def backend(self) -> KubernetesClient:
        """Get the container backend client.

        Provides a runtime-agnostic accessor so callers don't need to
        branch on ``spawner.k8s`` vs ``spawner.docker``.
        """
        return self.k8s

    # Backward-compat alias so code that references ``spawner.docker`` still works.
    docker = backend

    @property
    def gateway(self) -> GatewayClient:
        """Get Gateway client (lazy initialization)."""
        if self._gateway is None:
            self._gateway = get_gateway_client()
        return self._gateway

    def _get_restart_lock(self, key: tuple[str, str, str | None]) -> threading.Lock:
        """Get or create a per-(pipeline_id, agent_role, slice_id) restart lock."""
        with self._restart_locks_lock:
            if key not in self._restart_locks:
                self._restart_locks[key] = threading.Lock()
            return self._restart_locks[key]

    # ------------------------------------------------------------------
    # Orchestrator-owned one-shot event spawns
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Backward-compatibility aliases for ContainerSpawner method names
    # ------------------------------------------------------------------


class KubernetesSpawnError(Exception):
    """Error during Kubernetes Job spawning."""

    pass


class SpawnFailureError(KubernetesSpawnError):
    """Raised when one or more agents fail to spawn in a concurrent phase.

    Subclasses KubernetesSpawnError (which is aliased as ContainerSpawnError)
    so existing ``except (ContainerSpawnError, KubernetesSpawnError)`` handlers
    catch it without modification. The message distinguishes spawn-time
    failures from container exits so ``pipeline.error`` is accurate.
    """

    def __init__(self, failures: list[tuple[str, str | None]]) -> None:
        self.failures = failures
        parts = [f"{role}: {reason or 'unknown error'}" for role, reason in failures]
        roles_csv = ", ".join(role for role, _ in failures)
        super().__init__(f"Spawn failed for {roles_csv} — {'; '.join(parts)}")


# Singleton spawner instance
_spawner: KubernetesSpawner | None = None


def get_kubernetes_spawner(
    namespace: str = DEFAULT_NAMESPACE,
) -> KubernetesSpawner:
    """Get the singleton Kubernetes spawner.

    Args:
        namespace: Kubernetes namespace (only used on first call).

    Returns:
        KubernetesSpawner instance
    """
    global _spawner
    if _spawner is None:
        _spawner = KubernetesSpawner(namespace=namespace)
    return _spawner


# -------------------------------------------------------------------------
# Sub-package wiring (#3312): method-modules-on-class decomposition.
# The barrel keeps the KubernetesSpawner class identity + the patched
# module globals; submodules hold extracted helper functions and method
# bodies. Imports sit at the bottom so submodules can value-import the
# barrel constants defined above.
# -------------------------------------------------------------------------
from . import (  # noqa: E402
    _concurrent,
    _events,
    _jobs,
    _restart,
    _session,
    _spawn,
    _worktree,
)
from ._env import (  # noqa: E402
    _dedupe_label_value,
    _forwarded_discipline_env,
    _resolve_wait_producer_allowlist,
)
from ._errors import (  # noqa: E402
    _classify_spawn_error,
    _fit_k8s_name,
    _is_transient_spawn_failure,
)
from ._models import SpawnedContainer, _EventJobStatusView  # noqa: E402
from ._worktree import (  # noqa: E402
    _host_to_local_volumes,
    _role_needs_worktree,
    _validate_worktree_for_reuse,
)

# Bind extracted method bodies back onto the class.
KubernetesSpawner._try_reuse_worktree = _worktree._try_reuse_worktree
KubernetesSpawner._clean_reused_worktree = _worktree._clean_reused_worktree
KubernetesSpawner._find_missing_worktrees = _worktree._find_missing_worktrees
KubernetesSpawner._get_or_create_session = _session._get_or_create_session
KubernetesSpawner._teardown_session = _session._teardown_session
KubernetesSpawner.spawn_agent_job = _spawn.spawn_agent_job
KubernetesSpawner._event_dedupe_key_live = _events._event_dedupe_key_live
KubernetesSpawner.create_event_job_status_view = _events.create_event_job_status_view
KubernetesSpawner.spawn_event_job = _events.spawn_event_job
KubernetesSpawner.stop_agent_job = _jobs.stop_agent_job
KubernetesSpawner.remove_agent_job = _jobs.remove_agent_job
KubernetesSpawner.list_pipeline_jobs = _jobs.list_pipeline_jobs
KubernetesSpawner.list_slice_jobs = _jobs.list_slice_jobs
KubernetesSpawner.cleanup_pipeline = _jobs.cleanup_pipeline
KubernetesSpawner._apply_restart_budget = _restart._apply_restart_budget
KubernetesSpawner.check_and_increment_restart_count = _restart.check_and_increment_restart_count
KubernetesSpawner.restart_agent_job = _restart.restart_agent_job
KubernetesSpawner.get_restart_count = _restart.get_restart_count
KubernetesSpawner.reset_restart_counts = _restart.reset_restart_counts
KubernetesSpawner.detect_uncommitted_changes = _concurrent.detect_uncommitted_changes
KubernetesSpawner.create_concurrent_spawn_fn = _concurrent.create_concurrent_spawn_fn

# Backward-compat ContainerSpawner method aliases (relocated
# from the class body since the targets are now bound above).
KubernetesSpawner.spawn_agent_container = KubernetesSpawner.spawn_agent_job
KubernetesSpawner.stop_agent_container = KubernetesSpawner.stop_agent_job
KubernetesSpawner.remove_agent_container = KubernetesSpawner.remove_agent_job
KubernetesSpawner.list_pipeline_containers = KubernetesSpawner.list_pipeline_jobs
KubernetesSpawner.restart_agent_container = KubernetesSpawner.restart_agent_job

__all__ = [
    "KubernetesSpawner",
    "KubernetesSpawnError",
    "SpawnFailureError",
    "SpawnedContainer",
    "_EventJobStatusView",
    "get_kubernetes_spawner",
    "WORKTREE_BASE_DIR",
    "LABEL_EVENT_DEDUPE",
    "_PROTECTED_ENV_KEYS",
    "_ROLES_WITHOUT_WORKTREE",
    "ContainerInfo",
    "ContainerStatus",
    "_host_to_local_volumes",
    "_validate_worktree_for_reuse",
    "_role_needs_worktree",
    "_is_transient_spawn_failure",
    "_classify_spawn_error",
    "_fit_k8s_name",
    "_dedupe_label_value",
    "_forwarded_discipline_env",
    "_resolve_wait_producer_allowlist",
]

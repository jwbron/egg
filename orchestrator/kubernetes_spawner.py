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

import hashlib
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
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


import agent_salvage
from egg_config import GATEWAY_PORT, GATEWAY_PROXY_PORT
from gateway_client import (
    GatewayClient,
    GatewayError,
    SessionInfo,
    get_gateway_client,
)
from kubernetes_client import (
    DEFAULT_NAMESPACE,
    LABEL_AGENT_ROLE,
    LABEL_CONTAINER_NAME,
    LABEL_ORCHESTRATOR,
    LABEL_PIPELINE_ID,
    LABEL_SLICE_ID,
    JobOperationError,
    KubernetesClient,
    KubernetesClientError,
    PodNotFoundError,
    get_kubernetes_client,
)
from models import AgentRole, ContainerInfo
from review_graph import get_review_graph_for_phase

# #2725: senders we always include in EGG_WAIT_PRODUCER_ALLOWLIST so
# system-emitted bus messages keep waking slice-scoped waiters. The
# overseer emits OVERSEER_ALERT and the orchestrator emits
# CONSENSUS_RE_REVIEW + the "ready to confirm" STATUS nudge; both
# carry these literal ``from_role`` values, so excluding them would
# turn the filter into a deadlock surface.
_WAIT_ALLOWLIST_SYSTEM_SENDERS: tuple[str, ...] = ("overseer", "orchestrator")


def _resolve_wait_producer_allowlist(phase: str | None, role: str, repo: str | None) -> str | None:
    """Build the ``EGG_WAIT_PRODUCER_ALLOWLIST`` value for a spawn (#2725).

    Looks the role up in the BRC review graph for the supplied phase and
    returns a comma-separated allowlist of:

    - the role's graph neighbors — reviewers get the producers they
      review; producers get their reviewers so they wake on ACK/NACK
      and (for dual-role) any producers they also review. For
      dual-role agents (e.g. ``tester`` is both a producer reviewed by
      ``reviewer_code`` and a reviewer of ``coder``) the union of both
      neighbor sets is used so the agent wakes on both directions of
      cross-graph traffic.
    - the system senders ``overseer`` and ``orchestrator`` so
      ``OVERSEER_ALERT`` and ``CONSENSUS_RE_REVIEW`` keep waking the
      agent regardless of the producer set.

    Returns ``None`` when the role has no graph neighbors in the
    requested phase. This omits the env var entirely so the spawn
    preserves legacy wake-on-anything behavior — the wake-storm fix
    is opt-in via graph membership, not a default ratchet.

    ``get_review_graph_for_phase`` is documented to return an empty
    :class:`ReviewGraph` for unknown phases rather than raising, so
    this function intentionally does NOT wrap it in a ``try/except``:
    a programmer-error exception from a future refactor should surface
    loudly during spawn rather than degrade silently to "no allowlist,
    wake on everything," which is the wake-storm we are fixing.
    """
    if not phase:
        return None
    graph = get_review_graph_for_phase(phase, repo)

    neighbors: set[str] = set()
    if graph.is_reviewer(role):
        neighbors.update(graph.producers_for(role))
    if graph.is_producer(role):
        neighbors.update(graph.reviewers_for(role))
    if not neighbors:
        # Role not in the graph (pipeline-level helpers, ad-hoc roles)
        # — no allowlist, no filter, no behavior change.
        return None
    allowlist = sorted(neighbors | set(_WAIT_ALLOWLIST_SYSTEM_SENDERS))
    return ",".join(allowlist)


if TYPE_CHECKING:
    from egg_container import MountSpec

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
        # Slice scope (#2410, v2 review follow-up). The spawner is the
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


# --- #3064 slice-2: orchestrator-owned one-shot event spawns ------------
#
# When the orchestrator (not the in-pod wait-loop) owns the BRC event loop
# it spawns a per-event Job that handles exactly one ``propose|ack|nack``
# and exits (the slice-1 wrapper arm). The Job carries the event identity in
# env so the wrapper arm engages, and the dedupe key as a *label* so the
# event loop can reconcile in-flight Jobs after an orchestrator restart
# without persisting any spawn bookkeeping.
LABEL_EVENT_DEDUPE = "egg.event.dedupe-key"
LABEL_EVENT_ACTION = "egg.event.action"

# Env keys read by the consensus wrapper's one-shot arm
# (``consensus_wrapper.py``). ``EGG_EVENT_LOOP_OWNER=orchestrator`` +
# ``EGG_EVENT_ACTION`` engage the arm; ``EGG_EVENT_DEDUPE_KEY`` is the
# stale-event backstop / reconciliation handle. None are in
# ``_PROTECTED_ENV_KEYS`` — the one-shot entry is their only writer.
ENV_EVENT_LOOP_OWNER = "EGG_EVENT_LOOP_OWNER"
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


def _dedupe_label_value(dedupe_key: str) -> str:
    """Shorten a dedupe key to a Kubernetes-label-safe value (<=63 chars).

    The dedupe key is a 64-char sha256 hexdigest; k8s rejects label values
    longer than 63 chars. Deterministic truncation keeps the value stable
    across restarts so the spawn-side label and the reconcile-side selector
    always agree on the same string (a 63-hex-char sha256 prefix is 252 bits —
    collision-free for spawn dedupe). Every char of a hex digest is
    alphanumeric, so the truncated prefix is always a valid label value.
    Idempotent for already-short keys.
    """
    return dedupe_key[:_LABEL_VALUE_MAXLEN]


def _fit_k8s_name(name: str, maxlen: int = 63) -> str:
    """Fit an (unprefixed) k8s name to ``maxlen`` chars, RFC-1123-safe.

    Mirrors ``KubernetesClient._normalize_k8s_job_name``'s truncation shape —
    ``readable[:maxlen-9] + '-' + 8-char sha1`` — so a long
    ``egg-agent-<pipeline>-<slice>-<role>-<event>`` one-shot name stays within
    the 63-char budget while preserving the ``egg-agent-`` prefix. Idempotent
    for already-short names.
    """
    if len(name) <= maxlen:
        return name
    digest = hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{name[: maxlen - 9].rstrip('-')}-{digest}"


@dataclass
class SpawnedContainer:
    """Information about a spawned Job with gateway session.

    Reuses the same dataclass as ContainerSpawner for compatibility.
    """

    container_info: ContainerInfo
    session_info: SessionInfo | None
    agent_role: AgentRole
    pipeline_id: str
    environment: dict[str, str]


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


def _is_transient_spawn_failure(e: BaseException) -> bool:
    """Classify whether a worktree-creation failure should be retried.

    Coarse classifier based on ``GatewayError.status_code`` and message
    content. Refinement via ``GatewayError.details["errors"]`` is blocked
    on #1838.

    Rules (in priority order):
    1. Message contains a permanent fragment (e.g. "Repository not found"): permanent.
    2. ``status_code`` is a known permanent code (400/401/403/404/422): permanent.
    3. ``status_code`` is a known transient code (408/429/5xx): transient.
    4. ``status_code`` is any other HTTP status: permanent (fail fast).
    5. No ``status_code`` (connection-level failure or non-HTTP exception):
       transient by default — matches the issue #1839 recommendation of
       "retry by default but bounded."
    """
    message = str(e).lower()
    if any(frag in message for frag in _PERMANENT_MESSAGE_FRAGMENTS):
        return False
    status_code = getattr(e, "status_code", None)
    if status_code is None:
        return True
    if status_code in _PERMANENT_STATUS_CODES:
        return False
    if status_code in _TRANSIENT_STATUS_CODES:
        return True
    return False


def _classify_spawn_error(e: BaseException | None) -> str | None:
    """Short tag used in structured spawn-attempt logs.

    Priority order matches ``_is_transient_spawn_failure`` so the logged
    category always agrees with the actual retry decision.
    """
    if e is None:
        return None
    message = str(e).lower()
    if any(frag in message for frag in _PERMANENT_MESSAGE_FRAGMENTS):
        return "permanent_message"
    status_code = getattr(e, "status_code", None)
    if status_code in _PERMANENT_STATUS_CODES:
        return f"permanent_{status_code}"
    if status_code in _TRANSIENT_STATUS_CODES:
        return f"transient_{status_code}"
    if status_code is None:
        return type(e).__name__
    return f"unknown_{status_code}"


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


def _role_needs_worktree(role: AgentRole) -> bool:
    """Return True for roles whose work cannot proceed without a worktree."""
    return role not in _ROLES_WITHOUT_WORKTREE


def _host_to_local_volumes(repo_volumes: dict[str, str]) -> dict[str, str]:
    """Translate host paths to orchestrator-local paths for filesystem ops.

    The gateway returns worktree paths relative to the Docker host
    (e.g. ``/home/user/.egg-worktrees/...``), but the orchestrator
    container only sees these via a volume mount at ``/home/egg/...``.
    Uses the ``HOST_HOME`` env var to perform the translation.
    """
    host_home = os.environ.get("HOST_HOME", "").rstrip("/")
    container_home = "/home/egg"
    if not host_home or host_home == container_home:
        return repo_volumes
    return {
        name: path.replace(host_home, container_home, 1) if path.startswith(host_home) else path
        for name, path in repo_volumes.items()
    }


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

    def _find_missing_worktrees(self, agent_worktree_id: str, repos: list[str]) -> list[str]:
        """Return the list of per-agent worktree paths that don't exist on disk.

        Called right before spawning the k8s Job to catch the #1869 class
        of failure: ``create_worktrees`` returned success but the directory
        is gone by the time we'd spawn the Job (concurrent cleanup race,
        or create_worktrees was never called because ``repos`` was empty).
        Returns an empty list when all worktrees are in place.  Split out
        as an instance method so tests can monkey-patch it without having
        to manage a tmp filesystem hierarchy.
        """
        missing: list[str] = []
        for repo in repos:
            repo_name = repo.split("/")[-1] if "/" in repo else repo
            expected = WORKTREE_BASE_DIR / agent_worktree_id / repo_name
            if not expected.exists():
                missing.append(str(expected))
        return missing

    def spawn_agent_job(
        self,
        pipeline_id: str,
        agent_role: AgentRole,
        issue_number: int | None = None,
        repo_volumes: dict[str, str] | None = None,
        mode: str = "public",
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
        wait_for_gateway: bool = True,
        repos: list[str] | None = None,
        phase: str | None = None,
        command: list[str] | None = None,
        branch: str | None = None,
        base_branch: str | None = None,
        extra_mounts: list["MountSpec"] | None = None,  # noqa: UP037
        preserve_worktree_on_failure: bool = False,
        certs_volume: str | None = None,  # noqa: ARG002 — Docker-era compat
        spawn_max_retries: int = DEFAULT_SPAWN_MAX_RETRIES,
        spawn_retry_initial_backoff_seconds: float = (DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS),
        jira_ticket: str | None = None,
        slice_id: str | None = None,
        upstream: str | None = None,
        upstream_model: str | None = None,
        extra_labels: dict[str, str] | None = None,
        job_name_suffix: str | None = None,
    ) -> SpawnedContainer:
        """Spawn a Kubernetes Job for an agent.

        Args:
            pipeline_id: Pipeline ID
            agent_role: Agent role
            issue_number: GitHub issue number (optional)
            repo_volumes: Mapping of repo_name -> host_path for volume mounts.
            mode: Gateway mode (public, private, or local)
            image: Container image (default: egg:latest)
            extra_env: Additional environment variables
            wait_for_gateway: Wait for gateway health before spawning
            repos: List of repositories in owner/name format for gateway session
            phase: SDLC pipeline phase for gateway session
            command: Command to execute in the container
            branch: Git branch for the agent
            base_branch: Branch to base worktrees on
            extra_mounts: Additional mount specs (not used in k8s — handled by pod template)
            preserve_worktree_on_failure: If True, do not delete worktree on failure
            spawn_max_retries: Additional retry attempts for transient gateway
                worktree-creation failures. ``0`` disables retry (#1839).
            spawn_retry_initial_backoff_seconds: Initial backoff between retries;
                subsequent attempts scale by ``_SPAWN_RETRY_BACKOFF_MULTIPLIER``.
            upstream: Per-agent upstream identifier (#2769 slice-2).
                Forwarded to the gateway session-create call only when
                set; ``None`` keeps the default Anthropic routing.
            upstream_model: Upstream-side model name to rewrite the
                request body's ``model`` field to (#2769 slice-2).
                ``None`` on the Anthropic path — the body is forwarded
                unchanged.

        Returns:
            SpawnedContainer with Job and session info

        Raises:
            KubernetesSpawnError: If spawning fails
        """
        job_name, actual_k8s_job_name = self._build_k8s_job_names(
            pipeline_id, agent_role, slice_id=slice_id
        )
        # One-shot event spawns (#3064 slice-2) append a deterministic
        # per-event discriminator so distinct events for one role don't
        # collide on a single Job name (which would make the pre-spawn
        # cleanup below delete a sibling event's in-flight Job). The
        # discriminated name is fitted to the 63-char RFC-1123 budget here
        # (54 readable chars + ``-`` + 8-char digest, mirroring the k8s
        # client's ``_normalize_k8s_job_name``) so the ``egg-agent-`` name
        # we hand to ``create_container`` is already within budget.
        if job_name_suffix:
            job_name = _fit_k8s_name(f"{job_name}-{job_name_suffix}")
            actual_k8s_job_name = f"{KubernetesClient.JOB_PREFIX}{job_name}"

        # Clean up any existing Job with the same name.
        try:
            self.k8s.delete_job(actual_k8s_job_name, self._namespace)
            logger.info(
                "Removed existing Job with same name",
                job_name=job_name,
            )
        except PodNotFoundError:
            pass  # No existing Job, good to proceed
        except KubernetesClientError as e:
            logger.debug(
                "Failed to clean up existing Job",
                job_name=job_name,
                error=str(e),
            )

        # Check gateway health
        if wait_for_gateway:
            health = self.gateway.check_health()
            if not health.healthy:
                raise KubernetesSpawnError(
                    f"Gateway is not healthy: {health.error or health.status}"
                )

        # Labels for the Job — includes app.kubernetes.io/component:agent
        # so that NetworkPolicies (which select on this label) apply correctly.
        labels = {
            LABEL_ORCHESTRATOR: "true",
            LABEL_PIPELINE_ID: pipeline_id,
            LABEL_AGENT_ROLE: agent_role.value,
            LABEL_CONTAINER_NAME: job_name,
            "app.kubernetes.io/component": "agent",
            "app.kubernetes.io/part-of": "egg",
        }
        if issue_number is not None:
            labels["egg.issue.number"] = str(issue_number)
        # Slice-scoped agents get an additional label so operators can
        # select on the slice from kubectl (and ``list_slice_jobs`` can
        # filter without parsing Job names) — see #2666.
        if slice_id is not None:
            labels[LABEL_SLICE_ID] = slice_id
        # One-shot event labels (#3064 slice-2): the dedupe-key label is the
        # reconciliation handle the orchestrator event loop queries to detect
        # an in-flight Job for a given event after a restart. Applied last so
        # the caller's event labels are authoritative.
        if extra_labels:
            labels.update(extra_labels)

        # Host UID/GID for file ownership in worktrees
        host_uid = int(os.environ.get("HOST_UID", 1000))
        host_gid = int(os.environ.get("HOST_GID", 1000))

        # Per-agent worktree isolation: create a dedicated worktree.
        # Slice scope (#2403): concurrent slices in the same pipeline
        # MUST get distinct worktree ids — otherwise slice-N's coder
        # spawns onto slice-(N-1)'s already-mounted worktree (or
        # races with it during cleanup). The id is also the agent's
        # ``CONTAINER_ID`` env and the gateway worktree key, so the
        # whole gateway / agent / orchestrator triangle agrees on it.
        agent_worktree_id = self._build_agent_worktree_id(
            pipeline_id, agent_role, slice_id=slice_id
        )
        worktree_created_this_call = False

        if repos:
            max_attempts = max(1, spawn_max_retries + 1)
            for attempt in range(max_attempts):
                attempt_started = time.monotonic()
                try:
                    wt_result = self.gateway.create_worktrees(
                        container_id=agent_worktree_id,
                        repos=repos,
                        uid=host_uid,
                        gid=host_gid,
                        base_branch=base_branch,
                        # Wire the per-agent worktree's local branch to push to
                        # the pipeline's assigned branch.  Without this, a naive
                        # ``git push`` from the agent targets the per-agent
                        # local branch name, which the gateway rejects as
                        # ``push_denied_wrong_branch`` — and agents sometimes
                        # "recover" from that rejection with ``git reset --hard``,
                        # destroying their own committed work (#1809).
                        assigned_branch=branch,
                    )
                except Exception as e:  # noqa: BLE001 — classify below
                    duration_ms = int((time.monotonic() - attempt_started) * 1000)
                    transient = _is_transient_spawn_failure(e)
                    is_last = attempt >= max_attempts - 1
                    logger.info(
                        "Spawn attempt outcome",
                        event_type="spawn_attempt",
                        pipeline_id=pipeline_id,
                        agent_role=agent_role.value,
                        agent_worktree_id=agent_worktree_id,
                        phase=phase,
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        outcome="failed",
                        error_category=_classify_spawn_error(e),
                        error_detail=str(e),
                        duration_ms=duration_ms,
                        will_retry=(transient and not is_last),
                    )
                    if transient and not is_last:
                        delay = spawn_retry_initial_backoff_seconds * (
                            _SPAWN_RETRY_BACKOFF_MULTIPLIER**attempt
                        )
                        logger.warning(
                            "Transient worktree creation failure, retrying",
                            agent_worktree_id=agent_worktree_id,
                            attempt=attempt + 1,
                            max_attempts=max_attempts,
                            delay_seconds=delay,
                            error=str(e),
                        )
                        time.sleep(delay)
                        continue
                    raise KubernetesSpawnError(
                        f"Per-agent worktree creation failed for "
                        f"{agent_worktree_id} after {attempt + 1} attempt(s): {e}"
                    ) from e

                duration_ms = int((time.monotonic() - attempt_started) * 1000)
                if wt_result and wt_result.success and wt_result.worktrees:
                    repo_volumes = wt_result.worktrees
                    worktree_created_this_call = True
                    logger.info(
                        "Spawn attempt outcome",
                        event_type="spawn_attempt",
                        pipeline_id=pipeline_id,
                        agent_role=agent_role.value,
                        agent_worktree_id=agent_worktree_id,
                        phase=phase,
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        outcome="success",
                        error_category=None,
                        error_detail=None,
                        duration_ms=duration_ms,
                        will_retry=False,
                    )
                    logger.info(
                        "Per-agent worktree created",
                        agent_worktree_id=agent_worktree_id,
                        role=agent_role.value,
                        pipeline_id=pipeline_id,
                        worktrees=list(repo_volumes.keys()),
                        attempt=attempt + 1,
                    )
                    break
                # Empty / unsuccessful result — treat as non-retryable;
                # the gateway returned structured errors which usually
                # reflect permanent issues (missing repo, bad ref).
                errors = wt_result.errors if wt_result else []
                logger.info(
                    "Spawn attempt outcome",
                    event_type="spawn_attempt",
                    pipeline_id=pipeline_id,
                    agent_role=agent_role.value,
                    agent_worktree_id=agent_worktree_id,
                    phase=phase,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    outcome="failed",
                    error_category="empty_result",
                    error_detail=str(errors),
                    duration_ms=duration_ms,
                    will_retry=False,
                )
                raise KubernetesSpawnError(
                    f"Per-agent worktree creation returned no worktrees "
                    f"for {agent_worktree_id}: {errors}"
                )

        # Defensive sanity check: confirm the per-agent worktree actually
        # exists on disk before we spawn the Job.  Producers silently burn
        # minutes of tokens retrying git against a missing worktree when
        # ``create_worktrees`` looked like it succeeded but the directory
        # is gone — e.g. if a concurrent cleanup raced in between creation
        # and Job spawn, or if ``repos`` was empty so no worktree was ever
        # made for a role that needs one.  Surface that at spawn time
        # with an actionable message instead (#1869).
        if repos:
            missing = self._find_missing_worktrees(agent_worktree_id, repos)
            if missing:
                raise KubernetesSpawnError(
                    f"Per-agent worktree missing at spawn time for "
                    f"{agent_worktree_id} (role={agent_role.value}): "
                    f"{', '.join(missing)}. The worktree was either never "
                    f"created or deleted before the Job could start — see #1869."
                )
        elif _role_needs_worktree(agent_role):
            # No repos were provided but this role cannot function without
            # a worktree (any non-reviewer role).  Previously we spawned
            # anyway; the container would come up, issue a ``git status``,
            # and get a 500 "Worktree not found" from the gateway on every
            # call — pipelines stalled until manual cancellation (#1869).
            raise KubernetesSpawnError(
                f"Cannot spawn {agent_role.value} for pipeline "
                f"{pipeline_id}: no repos provided so no per-agent worktree "
                f"can be created, and this role requires one for git "
                f"operations. See #1869."
            )

        # Register gateway session (token-only, no container_ip)
        session_info = None
        session_token = None
        agent_anchor_id = f"{agent_role.value}-{job_name[:8]}"

        try:
            try:
                session_info = self.gateway.register_session(
                    container_id=job_name,
                    container_ip=None,  # Token-only auth for k8s
                    mode=mode,
                    repos=repos,
                    uid=host_uid,
                    gid=host_gid,
                    phase=phase,
                    pipeline_id=pipeline_id,
                    agent_role=agent_role.value,
                    agent_anchor_id=agent_anchor_id,
                    issue_number=issue_number,
                    claude_code_version=os.environ.get("CLAUDE_CODE_VERSION"),
                    branch=branch,
                    # Pipeline base branch — the gateway uses it as the preferred
                    # diff base for the new-branch restricted-path push check so a
                    # branch forked from a non-trunk base is not blamed for files
                    # inherited unchanged from that base (#3024).
                    base_branch=base_branch,
                    jira_ticket=jira_ticket,
                    # Reuse the per-agent worktrees created above under
                    # agent_worktree_id.  Without this, the gateway would
                    # race to create a second worktree under job_name and
                    # intermittently fail on .git/config.lock (#1857).
                    worktree_container_id=(
                        agent_worktree_id if worktree_created_this_call else None
                    ),
                    # Per-agent upstream routing (#2769 slice-2). Both fields
                    # are forwarded to the gateway only when set, so the
                    # default-Claude case keeps the request body byte-
                    # identical to the pre-#2769 wire shape.
                    upstream=upstream,
                    upstream_model=upstream_model,
                    # #2869 — a transient DNS/connection blip to the gateway
                    # during spawn-time session registration must not hard-
                    # fail the whole pipeline; retry with bounded backoff
                    # before raising SpawnFailureError.
                    retry_transient=True,
                )
                session_token = session_info.session_token

                logger.info(
                    "Pre-registered gateway session (token-only)",
                    job_name=job_name,
                    session_token=session_token[:12] + "...",
                )

            except GatewayError as e:
                raise KubernetesSpawnError(
                    f"Failed to register gateway session for {job_name}: {e}"
                ) from e

            # Build environment variables for the agent container.
            # Derive repo name from the first repo in the list (owner/name format).
            repo_base = "/home/egg/repos"
            if repos:
                repo_name = repos[0].split("/")[-1]
                repo_path = f"{repo_base}/{repo_name}"
            else:
                repo_path = repo_base

            # EGG_PIPELINE_REPO is the GitHub-style "owner/repo" string the
            # gateway and the auto-issue dedup signature rely on (issue
            # #1962). EGG_REPO_PATH is the *filesystem* path; this is a
            # separate, distinct env var. Sourced from the first repo in
            # `repos`; an empty repos list leaves it unset and the sandbox
            # entrypoint will fail-fast (no silent fallback per the
            # implementation plan).
            pipeline_repo = repos[0] if repos else None

            environment: dict[str, str] = {
                "CONTAINER_ID": agent_worktree_id,
                "EGG_REPO_PATH": repo_path,
                "EGG_AGENT_ROLE": agent_role.value,
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_ORCHESTRATOR_URL": ORCHESTRATOR_K8S_URL,
                "GATEWAY_URL": GATEWAY_K8S_URL,
                "HTTP_PROXY": PROXY_URL,
                "HTTPS_PROXY": PROXY_URL,
                "NO_PROXY": "gateway.egg-system.svc.cluster.local,orchestrator.egg-system.svc.cluster.local",
                "AGENT_ANCHOR_ID": agent_anchor_id,
                # Route Anthropic API calls through the gateway for
                # credential injection. The session-token placeholder that
                # lets the gateway's /v1/messages proxy identify the session
                # from the request header (issue #2829) is set just below,
                # alongside EGG_SESSION_TOKEN — under k8s the sandbox's
                # setup_anthropic_api() never runs (the container command
                # overrides the image ENTRYPOINT). Real credentials never
                # enter the sandbox environment.
                "ANTHROPIC_BASE_URL": GATEWAY_K8S_URL,
            }
            if session_token:
                environment["EGG_SESSION_TOKEN"] = session_token

                # Inject the session-token placeholder credential.
                #
                # In k8s the container ``command`` (the consensus wrapper /
                # ``python3 -m egg_agent``) overrides the image ENTRYPOINT, so
                # ``sandbox/entrypoint.py::setup_anthropic_api()`` — which
                # normally derives this placeholder from EGG_SESSION_TOKEN at
                # boot — never runs. Without a credential in its env, Claude
                # Code aborts every turn with the synthetic "Not logged in ·
                # Please run /login" before any request reaches the gateway,
                # and the producer crash-loops to permanent death (#2817).
                #
                # This is NOT a real credential: it's the session-token
                # envelope (``sk-ant-oat01-PROXY-INJECTED-egg-session-<token>``)
                # whose payload is the same session token already present in
                # EGG_SESSION_TOKEN above. The gateway strips it and injects
                # the real upstream credential at proxy time — real secrets
                # never enter the sandbox. On the LiteLLM path Claude Code
                # sends api_key auth via x-api-key (ANTHROPIC_API_KEY); on the
                # Anthropic path it sends OAuth via the Anthropic-Header
                # (CLAUDE_CODE_OAUTH_TOKEN). Both reach the gateway's x-api-key
                # extraction. Mirrors setup_anthropic_api (PR #2864) on the
                # only code path that runs under k8s. See #2829.
                from agent_model_resolution import UPSTREAM_LITELLM
                from egg_session_placeholder import to_placeholder

                placeholder = to_placeholder(session_token)
                if upstream == UPSTREAM_LITELLM:
                    environment["ANTHROPIC_API_KEY"] = placeholder
                else:
                    environment["CLAUDE_CODE_OAUTH_TOKEN"] = placeholder
            if pipeline_repo:
                # owner/repo string used by the overseer auto-issue verb
                # (issue #1962) and the gateway's overseer guardrails.
                environment["EGG_PIPELINE_REPO"] = pipeline_repo
                # #2528: sandbox containers don't have repositories.yaml
                # mounted, so the orchestrator pre-resolves the per-repo
                # role-pattern override here and passes it as a JSON
                # env var. ``shared.egg_restrictions.patterns`` checks
                # this env var first before attempting a filesystem
                # lookup. Failures are non-fatal — the lookup degrades
                # to the global defaults.
                try:
                    import json as _json

                    from egg_restrictions.patterns import (
                        load_repo_pattern_override,
                    )

                    snapshot = load_repo_pattern_override(pipeline_repo)
                    if snapshot:
                        environment["EGG_PIPELINE_REPO_PATTERNS_JSON"] = _json.dumps(
                            {pipeline_repo: snapshot}
                        )
                except Exception:
                    logger.exception(
                        "Failed to pre-resolve role-pattern override for sandbox; "
                        "falling back to defaults",
                        repo=pipeline_repo,
                    )
            if issue_number is not None:
                environment["EGG_ISSUE_NUMBER"] = str(issue_number)
            if phase:
                environment["EGG_PHASE"] = phase
            if branch:
                environment["EGG_BRANCH"] = branch
            elif pipeline_id:
                environment["EGG_BRANCH"] = f"egg/{pipeline_id}/work"

            # Slice scope (#2403, #2410): when this spawn is for a per-slice
            # agent, propagate ``EGG_SLICE_ID`` so the agent's BRC handlers
            # tag CONSENSUS_* signals with the slice and the orchestrator
            # routes them to the per-slice tracker. Without this, the
            # ``slice_id`` parameter only drove naming + worktree id and the
            # restarted Job came up with no slice scope in its env — its
            # signals would land on the pipeline-level tracker, which has
            # no record of the agent (failure mode #3 from #2410).
            #
            # Single source of truth (v2 review follow-up): the spawner is
            # the only writer; ``EGG_SLICE_ID`` is in ``_PROTECTED_ENV_KEYS``
            # so any ``extra_env`` value is logged and dropped, guaranteeing
            # the env stays consistent with the Job name + worktree id that
            # are also derived from this same ``slice_id`` parameter.
            if slice_id is not None:
                environment["EGG_SLICE_ID"] = slice_id

            # Base branch for the BRC event-pump's per-producer
            # ``git log {sha}..HEAD --not origin/<base> -p`` delta (#2967).
            # Both consumers — the consensus wrapper and the event-prompt
            # composer — read ``EGG_BASE_BRANCH`` and default to ``main`` when
            # it's unset; nothing exported it before, so the delta errored on
            # every non-``main`` repo and reviewers silently lost the slice-3
            # diff. The caller (``_run_concurrent_phase``) hands us the
            # already-resolved branch (explicit base, else the repo's detected
            # default), so this is the single source of truth. Protected below
            # so an ``extra_env`` override can't desync it from the worktree
            # base. Left unset when unresolved (None) so the consumers' own
            # documented ``main`` default still applies rather than an empty.
            if base_branch:
                environment["EGG_BASE_BRANCH"] = base_branch

            # #2725: pre-resolve the producer allowlist for this role +
            # phase so the wait-loop CLI auto-applies it. The allowlist
            # lives in env so the agent rubric stays graph-agnostic; a
            # change to the BRC review graph propagates on the next
            # spawn without prompt edits. Skipped for pipeline-level
            # spawns (no phase or no graph neighbors) so legacy
            # behavior is preserved unchanged.
            wait_allowlist = _resolve_wait_producer_allowlist(
                phase=phase, role=agent_role.value, repo=pipeline_repo
            )
            if wait_allowlist:
                environment["EGG_WAIT_PRODUCER_ALLOWLIST"] = wait_allowlist

            # Caller's extra_env overrides defaults, except protected keys
            if extra_env:
                for key, value in extra_env.items():
                    if key in _PROTECTED_ENV_KEYS:
                        logger.warning(
                            "Ignoring protected env var override",
                            key=key,
                        )
                        continue
                    environment[key] = value

            # Build hostPath mounts so the agent pod sees its worktree at
            # /home/egg/repos/<repo>. repo_volumes maps owner/repo →
            # host_path of the per-agent worktree (returned by the
            # gateway's create_worktrees). The sandbox does not get a
            # mount of the full /home/egg/.egg-worktrees tree — the
            # worktree content it needs is already reachable at
            # /home/egg/repos/<repo>, and exposing the sibling tree
            # confused agents into hunting across both paths (see #1954).
            host_path_mounts: list[dict[str, Any]] = []
            for owner_repo, host_path in (repo_volumes or {}).items():
                # Include the owner in the k8s volume name so two repos
                # with the same basename from different orgs don't collide
                # (e.g. "my-org/webapp" and "other-org/webapp" both produce
                # container path /home/egg/repos/webapp, but need distinct
                # volume names). Normalize to RFC-1123 (lowercase, hyphens)
                # and truncate to fit the 63-char name limit.
                volume_name = f"repo-{owner_repo.lower().replace('/', '-').replace('_', '-')}"
                if len(volume_name) > 63:
                    import hashlib

                    digest = hashlib.sha1(volume_name.encode(), usedforsecurity=False).hexdigest()[
                        :8
                    ]
                    volume_name = f"{volume_name[:54].rstrip('-')}-{digest}"
                host_path_mounts.append(
                    {
                        "name": volume_name,
                        "host_path": host_path,
                        "container_path": f"/home/egg/repos/{owner_repo.split('/')[-1]}",
                        "read_only": False,
                    }
                )

            # Create the Kubernetes Job
            container_info = self.k8s.create_container(
                name=job_name,
                image=image or self.DEFAULT_SANDBOX_IMAGE,
                environment=environment,
                labels=labels,
                command=command,
                host_path_mounts=host_path_mounts or None,
            )

            logger.info(
                "Agent Job created",
                job_name=job_name,
                container_id=container_info.container_id[:12],
                pipeline_id=pipeline_id,
                role=agent_role.value,
                has_session=session_info is not None,
            )

            return SpawnedContainer(
                container_info=container_info,
                session_info=session_info,
                agent_role=agent_role,
                pipeline_id=pipeline_id,
                environment=environment,
            )

        except KubernetesClientError as e:
            # Clean up gateway session if we registered one
            if session_info:
                try:
                    self.gateway.delete_session(session_info.session_token)
                except GatewayError:
                    pass  # Best effort cleanup
            # Only clean up the worktree if we created it in this call
            if worktree_created_this_call and not preserve_worktree_on_failure:
                try:
                    self.gateway.delete_worktrees(container_id=agent_worktree_id, force=True)
                except Exception:
                    pass  # Best effort cleanup
            raise KubernetesSpawnError(f"Failed to spawn Job: {e}") from e

    # ------------------------------------------------------------------
    # #3064 slice-2 — orchestrator-owned one-shot event spawns
    # ------------------------------------------------------------------
    def _event_dedupe_key_live(self, dedupe_key: str) -> bool:
        """Return True iff a Job already carries this dedupe-key label.

        The reconciliation handle: a fresh orchestrator process re-derives
        every event and the spawner asks this before creating a Job, so an
        in-flight Job from a prior process (or a racing duplicate request) is
        adopted rather than duplicated. No spawn state is persisted — the
        label IS the state. Queried via a label selector so the API returns
        only matching Jobs; best-effort (a list failure ⇒ "not live" ⇒ spawn
        proceeds rather than wedging).
        """
        # The selector value MUST use the same label-safe shortening applied
        # to the label on the spawn side, or it can never match the live Job.
        selector = f"{LABEL_EVENT_DEDUPE}={_dedupe_label_value(dedupe_key)}"
        try:
            jobs = self.k8s.list_jobs(self._namespace, label_selector=selector)
        except Exception as exc:  # noqa: BLE001 — adoption is best-effort
            logger.warning(
                "Failed to list Jobs for dedupe-key reconciliation",
                dedupe_key=dedupe_key,
                error=str(exc),
            )
            return False
        # The selector already scopes to matching Jobs; treat any returned
        # sequence as a hit. A non-sequence (e.g. an unconfigured mock) is
        # treated as "no live Job" so the spawn proceeds.
        return isinstance(jobs, (list, tuple)) and len(jobs) > 0

    def spawn_event_job(
        self,
        pipeline_id: str,
        agent_role: AgentRole,
        *,
        action: str,
        dedupe_key: str,
        event_payload_refs: str | None = None,
        slice_id: str | None = None,
        **spawn_kwargs: Any,
    ) -> SpawnedContainer | None:
        """Spawn (or adopt) a one-shot Job for a single BRC event (#3064).

        The Job's env carries the full event identity so the consensus
        wrapper's one-shot arm engages (``EGG_EVENT_LOOP_OWNER=orchestrator``
        + ``EGG_EVENT_ACTION`` ∈ ``propose|ack|nack`` + ``EGG_EVENT_DEDUPE_KEY``)
        and the dedupe key rides as a Job *label* — the reconciliation handle
        the event loop rebuilds its live set from on restart.

        **Adoption**: requesting a spawn for an already-live dedupe key
        returns ``None`` (the existing Job is adopted) rather than creating a
        duplicate — the defense-in-depth backstop for the loop's own dedupe
        set racing a restart.

        Everything else (worktree create-with-retry, gateway-session
        registration) flows through :meth:`spawn_agent_job` unchanged; this
        method only adds the event identity (env + labels) and the
        deterministic per-event Job-name discriminator. ``slice_id``/``phase``
        ride through ``spawn_kwargs`` to ``spawn_agent_job``, which is the
        single source of truth for ``EGG_SLICE_ID``/``EGG_PHASE``.
        """
        if action not in ("propose", "ack", "nack"):
            # confirm/complete run orchestrator-side with no pod, and ``wait``
            # spawns nothing — reaching the spawner with one is a caller bug.
            raise ValueError(
                f"spawn_event_job called with non-spawn action {action!r}; "
                "only propose|ack|nack ever spawn a pod (confirm/complete are "
                "agent-free, wait is a no-op)."
            )

        if self._event_dedupe_key_live(dedupe_key):
            logger.info(
                "Adopting existing live Job for event (dedupe hit)",
                pipeline_id=pipeline_id,
                role=agent_role.value,
                action=action,
                dedupe_key=dedupe_key,
            )
            return None

        event_env: dict[str, str] = {
            ENV_EVENT_LOOP_OWNER: "orchestrator",
            ENV_EVENT_ACTION: action,
            ENV_EVENT_DEDUPE_KEY: dedupe_key,
        }
        if event_payload_refs:
            event_env[ENV_EVENT_PAYLOAD_REFS] = event_payload_refs
        # Merge with any caller-supplied extra_env (caller's non-event keys
        # win for their own keys; event identity keys are set by us).
        caller_env = spawn_kwargs.pop("extra_env", None) or {}
        merged_env = {**caller_env, **event_env}

        event_labels = {
            # Shortened to the k8s 63-char label-value limit; the full key
            # rides in env (ENV_EVENT_DEDUPE_KEY) above. The selector in
            # _event_dedupe_key_live applies the identical shortening so
            # restart reconciliation matches.
            LABEL_EVENT_DEDUPE: _dedupe_label_value(dedupe_key),
            LABEL_EVENT_ACTION: action,
        }
        caller_labels = spawn_kwargs.pop("extra_labels", None) or {}
        merged_labels = {**caller_labels, **event_labels}

        return self.spawn_agent_job(
            pipeline_id,
            agent_role,
            slice_id=slice_id,
            extra_env=merged_env,
            extra_labels=merged_labels,
            job_name_suffix=dedupe_key[:_EVENT_JOB_NAME_DISCRIMINATOR_LEN],
            **spawn_kwargs,
        )

    def stop_agent_job(
        self,
        job_name: str,
        cleanup_session: bool = True,
        timeout: int = 10,
    ) -> ContainerInfo:
        """Stop an agent Job and optionally clean up session.

        Args:
            job_name: Job name or container ID
            cleanup_session: Whether to delete gateway session
            timeout: Grace period in seconds (passed to stop_container)

        Returns:
            ContainerInfo after stopping
        """
        try:
            info = self.k8s.stop_container(job_name, timeout=timeout)

            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(job_name)
                except GatewayError as e:
                    logger.warning(
                        "Failed to clean up gateway session",
                        job_name=job_name,
                        error=str(e),
                    )

            return info

        except PodNotFoundError:
            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(job_name)
                except GatewayError:
                    pass
            raise

    def remove_agent_job(
        self,
        job_name: str,
        force: bool = False,
        cleanup_session: bool = True,
    ) -> None:
        """Remove an agent Job and clean up session.

        Args:
            job_name: Job name or container ID
            force: Force removal (foreground propagation)
            cleanup_session: Whether to delete gateway session
        """
        try:
            self.k8s.remove_container(job_name, force=force)
        finally:
            if cleanup_session:
                try:
                    self.gateway.delete_session_by_container(job_name)
                except GatewayError as e:
                    logger.warning(
                        "Failed to clean up gateway session",
                        job_name=job_name,
                        error=str(e),
                    )

    def list_pipeline_jobs(
        self,
        pipeline_id: str,
    ) -> list[ContainerInfo]:
        """List all Jobs for a pipeline.

        Args:
            pipeline_id: Pipeline ID

        Returns:
            List of ContainerInfo
        """
        return self.k8s.list_containers(
            labels={LABEL_PIPELINE_ID: pipeline_id},
        )

    def list_slice_jobs(
        self,
        pipeline_id: str,
        slice_id: str,
    ) -> list[ContainerInfo]:
        """List slice-scoped Jobs within *pipeline_id*.

        Filters on ``egg.slice.id`` (#2666) so callers don't have to
        parse Job names to scope an operation to a single slice.
        Returns an empty list when no Jobs match.
        """
        return self.k8s.list_containers(
            labels={LABEL_PIPELINE_ID: pipeline_id, LABEL_SLICE_ID: slice_id},
        )

    def cleanup_pipeline(
        self,
        pipeline_id: str,
        force: bool = True,
        preserve_worktrees: bool = False,
        salvage_mode: str | None = None,
        salvage_base_branch: str | None = None,
    ) -> int:
        """Clean up all Jobs and sessions for a pipeline.

        Args:
            pipeline_id: Pipeline ID
            force: Force removal
            preserve_worktrees: When True, skip worktree deletion so a
                subsequent retry can reuse the pipeline-level and per-agent
                worktrees. Jobs and gateway sessions are still removed so the
                retry spawns fresh pods. Default False preserves the prior
                behavior of deleting every worktree.
            salvage_mode: Gateway session mode (``"public"`` / ``"private"``)
                used by the auto-salvage hook for the launcher-auth push to
                ``egg/recovered/...``. Callers with a ``Pipeline`` in scope
                should compute this via ``_compute_gateway_mode(pipeline)``
                so private-repo / private-network pipelines salvage with
                the policy they ran under. ``None`` (the default) falls
                back to ``"public"`` and is only correct for callers that
                cannot load the pipeline.
            salvage_base_branch: Base branch (e.g. ``"main"``) used by the
                salvage hook as the secondary ``^anchor`` cut when
                ``origin/<assigned_branch>`` is missing. ``None`` is safe
                — the hook falls back to the full HEAD history (capped at
                200 commits) — but threading ``pipeline.base_branch``
                produces tighter recovery refs.

        Returns:
            Number of Jobs removed
        """
        jobs = self.list_pipeline_jobs(pipeline_id)
        removed = 0

        for job in jobs:
            try:
                self.remove_agent_job(
                    job.job_name or job.container_id,
                    force=force,
                    cleanup_session=True,
                )
                removed += 1
            except (PodNotFoundError, JobOperationError) as e:
                logger.warning(
                    "Failed to remove Job during cleanup",
                    job_name=job.job_name,
                    error=str(e),
                )

        if preserve_worktrees:
            logger.info(
                "Pipeline cleanup complete (worktrees preserved for retry)",
                pipeline_id=pipeline_id,
                jobs_removed=removed,
            )
            return removed

        # Clean up per-agent worktrees
        worktree_ids_to_clean: set[str] = {pipeline_id}
        for job in jobs:
            role_label = None
            # Extract role string from AgentRole enum
            if hasattr(job, "agent_role") and job.agent_role is not None:
                try:
                    role_label = (
                        job.agent_role.value
                        if isinstance(job.agent_role, AgentRole)
                        else str(job.agent_role)
                    )
                except AttributeError, TypeError:
                    pass
            if role_label and isinstance(role_label, str):
                worktree_ids_to_clean.add(f"{pipeline_id}-{role_label}")

        # Also scan filesystem for any per-agent worktrees.  Only match
        # entries that are either the pipeline-level worktree, a
        # "{pipeline_id}-{role}" directory, or a slice-scoped
        # "{pipeline_id}-slice-{N}-{role}" directory where {role} is a
        # known AgentRole value (#2403). A naive `startswith(f"{pipeline_id}-")`
        # collides with longer pipeline IDs that share the prefix — e.g.
        # cleanup of `issue-1758` would match active worktrees of
        # `issue-1758-worktree-fix-tester`, wiping another pipeline's
        # state mid-phase (#1865).
        if WORKTREE_BASE_DIR.exists():
            valid_role_suffixes = {f"-{role.value}" for role in AgentRole}
            slice_segment_re = re.compile(r"^-slice-[0-9]+(-.+)$")
            try:
                for entry in WORKTREE_BASE_DIR.iterdir():
                    if not entry.is_dir():
                        continue
                    name = entry.name
                    if name == pipeline_id:
                        worktree_ids_to_clean.add(name)
                        continue
                    if not name.startswith(pipeline_id):
                        continue
                    suffix = name[len(pipeline_id) :]
                    if suffix in valid_role_suffixes:
                        worktree_ids_to_clean.add(name)
                        continue
                    # Slice-scoped: "{pipeline_id}-slice-{N}-{role}".
                    # The trailing "-{role}" inside the captured group
                    # is matched against the role allowlist so this
                    # branch can't sweep an unrelated sibling worktree.
                    slice_match = slice_segment_re.match(suffix)
                    if slice_match and slice_match.group(1) in valid_role_suffixes:
                        worktree_ids_to_clean.add(name)
            except Exception as e:
                logger.warning(
                    "Filesystem worktree scan failed during cleanup",
                    pipeline_id=pipeline_id,
                    error=str(e),
                )

        # Auto-salvage unpushed agent commits before deleting worktrees
        # (#2429). Best-effort: any failure logs and continues so cleanup
        # cannot be blocked by salvage. The default policy here used to
        # be silent loss when an agent's pushes were wedged — this hook
        # makes the default policy "push to egg/recovered/<pipeline>/...
        # then delete" so salvageable work is always reachable from
        # origin before the worktree filesystem state is gone.
        try:
            agent_salvage.auto_salvage_pipeline(
                self.gateway,
                pipeline_id,
                worktree_filter=worktree_ids_to_clean,
                # Mismatching the running-pipeline mode would re-create
                # the silent-loss class this hook exists to prevent for
                # private-mode pipelines. Callers without a Pipeline in
                # scope keep the historical default ("public") via
                # ``mode=None`` → omitted-kwarg below.
                **({"mode": salvage_mode} if salvage_mode is not None else {}),
                base_branch=salvage_base_branch,
            )
        except Exception as e:
            logger.warning(
                "Auto-salvage failed during cleanup; proceeding with worktree deletion",
                pipeline_id=pipeline_id,
                error=str(e),
            )

        for wt_id in worktree_ids_to_clean:
            try:
                self.gateway.delete_worktrees(container_id=wt_id, force=True)
                logger.info(
                    "Worktree cleaned up",
                    pipeline_id=pipeline_id,
                    worktree_id=wt_id,
                )
            except Exception as e:
                logger.warning(
                    "Worktree cleanup failed",
                    pipeline_id=pipeline_id,
                    worktree_id=wt_id,
                    error=str(e),
                )

        logger.info(
            "Pipeline cleanup complete",
            pipeline_id=pipeline_id,
            jobs_removed=removed,
        )

        return removed

    def restart_agent_job(
        self,
        pipeline_id: str,
        agent_role: AgentRole,
        issue_number: int | None = None,
        repo_volumes: dict[str, str] | None = None,
        mode: str | None = "public",
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
        repos: list[str] | None = None,
        phase: str | None = None,
        command: list[str] | None = None,
        branch: str | None = None,
        base_branch: str | None = None,
        extra_mounts: list["MountSpec"] | None = None,  # noqa: UP037
        max_restarts: int = 2,
        reason: str = "",
        spawn_max_retries: int = DEFAULT_SPAWN_MAX_RETRIES,
        spawn_retry_initial_backoff_seconds: float = (DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS),
        slice_id: str | None = None,
        wait_for_gateway: bool = True,
        upstream: str | None = None,
        upstream_model: str | None = None,
    ) -> SpawnedContainer:
        """Restart an agent Job: delete and respawn preserving worktree.

        Args:
            pipeline_id: Pipeline ID.
            agent_role: Agent role to restart.
            issue_number: GitHub issue number.
            repo_volumes: Repo name to host path mappings.
            mode: Gateway mode ('public' or 'private'). Must be explicitly provided.
            image: Container image override.
            extra_env: Additional environment variables.
            repos: Repositories for gateway session.
            phase: Current pipeline phase.
            command: Command to execute in the container.
            branch: Branch name.
            base_branch: Branch to base worktrees on.
            extra_mounts: Additional mount specs.
            max_restarts: Maximum restart attempts per agent per phase.
            reason: Human-readable reason for the restart.
            spawn_max_retries: Retry attempts for transient gateway failures
                during worktree creation (forwarded to ``spawn_agent_job``).
            spawn_retry_initial_backoff_seconds: Initial backoff for spawn
                retries (forwarded to ``spawn_agent_job``).
            slice_id: Optional slice scope (#2410). When supplied, the
                slice-scoped Job name (``egg-agent-{pid}-{slice_id}-{role}``)
                is the one deleted and respawned, the slice-scoped worktree
                id is preserved, and ``EGG_SLICE_ID`` is propagated so the
                restarted agent re-enters the per-slice consensus tracker.
                The restart-budget key includes the slice scope so each
                slice gets an independent budget.
            wait_for_gateway: Wait for gateway health before respawning.
                Forwarded to ``spawn_agent_job``.
            upstream: Per-agent upstream identifier (#2769 slice-2),
                forwarded to ``spawn_agent_job`` so the restarted Job
                registers its gateway session against the same upstream
                as the initial spawn. ``None`` keeps the default
                Anthropic routing.
            upstream_model: Upstream-side model name to rewrite the
                request body's ``model`` field to (#2769 slice-2),
                forwarded to ``spawn_agent_job``. ``None`` on the
                Anthropic path — the body is forwarded unchanged.

        Returns:
            SpawnedContainer with new Job info.

        Raises:
            ValueError: If mode is None.
            KubernetesSpawnError: If restart limit exceeded or spawning fails.
        """
        if mode is None:
            raise ValueError("mode must be explicitly provided ('public' or 'private')")

        # Slice scope is part of the restart key so concurrent slice-N
        # and slice-M agents of the same role each get an independent
        # restart budget and lock. ``reset_restart_counts(pipeline_id)``
        # still clears all of them because it filters on ``k[0]``.
        restart_key = (pipeline_id, agent_role.value, slice_id)
        lock = self._get_restart_lock(restart_key)

        # Timeout prevents indefinite blocking if a concurrent restart of the
        # same agent is stuck — the lock is held across remove_agent_job() and
        # spawn_agent_job(), both of which invoke k8s API calls that can hang
        # on network or control-plane issues.
        if not lock.acquire(timeout=120):
            raise KubernetesSpawnError(
                f"Timed out waiting to acquire restart lock for "
                f"{agent_role.value} in pipeline {pipeline_id}"
            )
        try:
            current_count = self._restart_counts.get(restart_key, 0)

            if current_count >= max_restarts:
                raise KubernetesSpawnError(
                    f"Restart limit ({max_restarts}) exceeded for {agent_role.value} "
                    f"in pipeline {pipeline_id} (restarted {current_count} times)"
                )

            # Increment count before spawn so failed attempts burn a restart budget slot
            self._restart_counts[restart_key] = current_count + 1

            # ``job_name`` matches the gateway session container_id used at
            # spawn time (hyphenated, no JOB_PREFIX); ``actual_k8s_job_name``
            # is the real k8s Job name. Using the wrong form for either side
            # broke restart for every role with an underscore — see #2070.
            # Slice scope (#2410) must be threaded through here so the
            # delete + respawn target the slice-scoped Job name, not the
            # pipeline-level one.
            job_name, actual_k8s_job_name = self._build_k8s_job_names(
                pipeline_id, agent_role, slice_id=slice_id
            )

            logger.info(
                "Restarting agent Job",
                pipeline_id=pipeline_id,
                role=agent_role.value,
                restart_count=current_count + 1,
                max_restarts=max_restarts,
                reason=reason,
            )

            # Delete the existing Job (best effort) and clean up the
            # gateway session.  We can't go through ``remove_agent_job``
            # here because it would route both the k8s and gateway calls
            # through the same identifier, but k8s wants the prefixed form
            # and the gateway session is keyed by the unprefixed form.
            delete_attempted = False
            try:
                self.k8s.delete_job(
                    actual_k8s_job_name,
                    self._namespace,
                    propagation_policy="Foreground",
                )
                delete_attempted = True
            except PodNotFoundError:
                logger.debug(
                    "No existing Job found during restart (already removed)",
                    job_name=actual_k8s_job_name,
                )
            except JobOperationError as e:
                logger.warning(
                    "Failed to delete existing Job during restart, continuing",
                    job_name=actual_k8s_job_name,
                    error=str(e),
                )
            # Foreground propagation returns as soon as the deletion is
            # accepted; the Job lingers with its finalizer until pods are
            # gone. Wait for the API server to actually remove it before
            # spawning a Job with the same name, otherwise we race the
            # finalizer and 409 on AlreadyExists (#2655).
            if delete_attempted and not self.k8s.wait_for_job_gone(
                actual_k8s_job_name, self._namespace, timeout_s=30.0
            ):
                logger.warning(
                    "Job still present after 30s wait; respawn may 409 on AlreadyExists",
                    job_name=actual_k8s_job_name,
                )
            try:
                self.gateway.delete_session_by_container(job_name)
            except GatewayError as e:
                logger.warning(
                    "Failed to clean up gateway session during restart",
                    job_name=job_name,
                    error=str(e),
                )

            # Salvage agent work before respawning (#2807). The respawn
            # reuses the on-disk worktree and hard-resets it to a remote
            # ref (gateway _reset_reused_worktree_to_safe_ref), destroying
            # both unpushed commits and the dirty working tree. The modal
            # #2807 crash window is mid-Edit, before any commit, so
            # salvage_uncommitted=True commits the dirty tree onto the work
            # branch first; auto-salvage then pushes everything to
            # egg/recovered/<pipeline>/<scope>/<sha> for manual triage.
            #
            # This runs on the respawn critical path while the restart lock
            # is held. It is scoped to just this agent's own worktree, the
            # working-tree commit is local git, and the gateway push carries
            # its own HTTP timeout, so the added lock-hold is bounded. The
            # best-effort try/except keeps a salvage failure from blocking
            # the respawn.
            agent_worktree_id = self._build_agent_worktree_id(pipeline_id, agent_role, slice_id)
            try:
                agent_salvage.auto_salvage_pipeline(
                    self.gateway,
                    pipeline_id,
                    worktree_filter={agent_worktree_id},
                    mode=mode,
                    base_branch=base_branch,
                    salvage_uncommitted=True,
                )
            except Exception as e:
                logger.warning(
                    "Auto-salvage failed during agent restart; proceeding",
                    pipeline_id=pipeline_id,
                    agent_role=agent_role.value,
                    worktree_id=agent_worktree_id,
                    error=str(e),
                )

            # Respawn — gateway's create_worktrees() is idempotent.
            # ``slice_id`` is forwarded so spawn_agent_job builds the
            # slice-scoped Job + worktree id and sets ``EGG_SLICE_ID``
            # on the new Job (#2410).
            spawned = self.spawn_agent_job(
                pipeline_id=pipeline_id,
                agent_role=agent_role,
                issue_number=issue_number,
                repo_volumes=repo_volumes,
                mode=mode,
                image=image,
                extra_env=extra_env,
                wait_for_gateway=wait_for_gateway,
                repos=repos,
                phase=phase,
                command=command,
                branch=branch,
                base_branch=base_branch,
                extra_mounts=extra_mounts,
                preserve_worktree_on_failure=True,
                spawn_max_retries=spawn_max_retries,
                spawn_retry_initial_backoff_seconds=spawn_retry_initial_backoff_seconds,
                slice_id=slice_id,
                # Per-agent upstream routing (#2769 slice-2). Forwarded so a
                # restart picks the same upstream as the initial spawn — the
                # gateway session is otherwise rebuilt against the
                # ``anthropic`` default and would silently route the
                # restarted agent to the wrong upstream.
                upstream=upstream,
                upstream_model=upstream_model,
            )

            logger.info(
                "Agent Job restarted successfully",
                pipeline_id=pipeline_id,
                role=agent_role.value,
                new_job_name=spawned.container_info.job_name,
                restart_count=current_count + 1,
            )

            return spawned
        finally:
            lock.release()

    def get_restart_count(
        self,
        pipeline_id: str,
        agent_role: str,
        slice_id: str | None = None,
    ) -> int:
        """Get the current restart count for an agent.

        Args:
            pipeline_id: Pipeline ID.
            agent_role: Agent role value string.
            slice_id: Optional slice scope (#2410). Pipeline-level callers
                pass ``None``; slice-aware callers pass the same
                ``slice-<N>`` string they used at restart time so each
                slice's budget is reported independently.

        Returns:
            Number of times the agent has been restarted.
        """
        key = (pipeline_id, agent_role, slice_id)
        lock = self._get_restart_lock(key)
        with lock:
            return self._restart_counts.get(key, 0)

    def reset_restart_counts(self, pipeline_id: str) -> None:
        """Reset all restart counts for a pipeline (e.g., on phase transition).

        Args:
            pipeline_id: Pipeline ID.
        """
        # Acquire the global lock to iterate safely, then clear matching count
        # entries.  We intentionally do NOT delete per-key locks from
        # _restart_locks: a concurrent restart_agent_job may still hold one of
        # those locks, and deleting it would allow _get_restart_lock to create a
        # new lock for the same key — breaking mutual exclusion.  The per-key
        # locks are lightweight and bounded by the number of (pipeline, role)
        # pairs, so the growth is negligible.
        with self._restart_locks_lock:
            keys_to_remove = [k for k in self._restart_counts if k[0] == pipeline_id]
            for k in keys_to_remove:
                del self._restart_counts[k]

    def detect_uncommitted_changes(
        self,
        pipeline_id: str,
        agent_role: str,
        slice_id: str | None = None,
    ) -> dict | None:
        """Detect uncommitted changes in an agent's worktree after Job exit.

        Checks the agent's worktree directly on the filesystem for uncommitted
        changes. Per-agent worktrees are at:
        /home/egg/.egg-worktrees/{pipeline_id}-{role}/{repo}/ (pipeline-level)
        /home/egg/.egg-worktrees/{pipeline_id}-{slice_id}-{role}/{repo}/
        (slice-scoped, #2410).

        Args:
            pipeline_id: Pipeline ID.
            agent_role: Agent role value string.
            slice_id: Optional slice scope. When supplied, the slice-scoped
                worktree id is inspected; pipeline-level callers omit this.

        Returns:
            Dict with change info if uncommitted changes found, None otherwise.
        """
        import subprocess

        # Mirrors ``_build_agent_worktree_id`` so a slice-scoped restart
        # path can detect uncommitted work in the slice's worktree, not
        # the (possibly absent) pipeline-level one.
        agent_worktree_id = (
            f"{pipeline_id}-{slice_id}-{agent_role}" if slice_id else f"{pipeline_id}-{agent_role}"
        )
        worktree_base = WORKTREE_BASE_DIR / agent_worktree_id

        if not worktree_base.exists():
            return None

        for repo_dir in worktree_base.iterdir():
            if not repo_dir.is_dir():
                continue
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/git",
                        "-c",
                        "safe.directory=*",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "gc.auto=0",
                        "status",
                        "--porcelain",
                    ],
                    cwd=str(repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    files = [
                        line[3:].strip()
                        for line in result.stdout.splitlines()
                        if line and len(line) > 3
                    ]
                    logger.info(
                        "Agent exited with uncommitted changes",
                        event_type="agent_uncommitted_changes",
                        pipeline_id=pipeline_id,
                        agent_role=agent_role,
                        slice_id=slice_id,
                        worktree_path=str(repo_dir),
                        file_count=len(files),
                        changed_files=files[:20],
                    )
                    return {
                        "pipeline_id": pipeline_id,
                        "agent_role": agent_role,
                        "slice_id": slice_id,
                        "worktree_id": agent_worktree_id,
                        "worktree_path": str(repo_dir),
                        "file_count": len(files),
                        "changed_files": files[:20],
                    }
            except Exception as e:
                logger.warning(
                    "Failed to check worktree status",
                    repo_dir=str(repo_dir),
                    error=str(e),
                )
        return None

    def spawn_overseer_job(
        self,
        pipeline_id: str,
        issue_number: int | None = None,
        mode: str = "public",
        poll_interval: int = 30,
        decision_model: str = "sonnet",
        max_turns: int = 2000,
        image: str | None = None,
        wait_for_gateway: bool = True,
        repos: list[str] | None = None,
        certs_volume: str | None = None,  # noqa: ARG002 — Docker-era compat
    ) -> SpawnedContainer:
        """Spawn an overseer Job for phase-scoped health monitoring.

        Args:
            pipeline_id: Pipeline ID.
            issue_number: GitHub issue number (optional).
            mode: Gateway mode (public or private).
            poll_interval: Polling interval in seconds.
            decision_model: LLM model for overseer decisions.
            max_turns: Maximum Agent SDK turns.
            image: Container image override.
            wait_for_gateway: Wait for gateway health before spawning.
            repos: List of repositories for gateway session.

        Returns:
            SpawnedContainer with overseer Job and session info.
        """
        # Classify the overseer's decision model so that ``AgentModelDecision.effort``
        # threads to ``--effort`` for fable-routed overseers — same drift defense as
        # the refine/plan path. Default ``sonnet`` yields ``effort=None`` (no behavior
        # change); only ``overseer_decision_maker_model=fable`` flips the pin on.
        from agent_model_resolution import classify_model
        from egg_agent import build_agent_command

        overseer_decision = classify_model(decision_model)

        extra_env = {
            "EGG_OVERSEER_MODE": "true",
            "EGG_OVERSEER_POLL_INTERVAL": str(poll_interval),
            "EGG_OVERSEER_DECISION_MODEL": decision_model,
            "BASH_COMMAND_TIMEOUT": "0",
        }

        overseer_prompt = (
            f"You are the overseer agent for pipeline {pipeline_id}. "
            "Your first action is to run the pre-built monitoring script: "
            "`python3 /opt/egg-runtime/sandbox/overseer_monitor.py --once`. "
            "That path is a build-time copy of `sandbox/overseer_monitor.py` "
            "from this repo, baked into the container image at "
            "`sandbox/Dockerfile` (the `COPY . /opt/egg-runtime/` layer). "
            "It is the canonical script the orchestrator expects you to run; "
            "the orchestrator vouches for it, and you do not need to verify "
            "its provenance against any other copy. Don't write your own "
            "monitoring loop or bash script; the pre-built script already "
            "handles polling, heartbeats, and JSON output. "
            "Run the script in single-cycle mode (`--once`) so you can "
            "classify and act between cycles. Each call outputs one JSON "
            "line to stdout. Read the output, classify alerts using the "
            "Haiku tier, decide corrective actions using the Sonnet tier, "
            "and execute them via egg-orch CLI commands. Then call the "
            "script with `--once` again. Repeat until the pipeline reaches "
            "a terminal state (complete, failed, or cancelled). After the "
            "pipeline ends, generate a final health summary."
        )
        command = build_agent_command(
            prompt=overseer_prompt,
            model=decision_model,
            max_turns=max_turns,
            effort=overseer_decision.effort,
        )

        return self.spawn_agent_job(
            pipeline_id=pipeline_id,
            agent_role=AgentRole.OVERSEER,
            issue_number=issue_number,
            repo_volumes=None,
            mode=mode,
            image=image,
            extra_env=extra_env,
            wait_for_gateway=wait_for_gateway,
            repos=repos,
            command=command,
        )

    def create_concurrent_spawn_fn(
        self,
        pipeline_id: str,
        issue_number: int | None,
        repo_volumes: dict[str, str] | None,
        mode: str,
        repos: list[str] | None,
        phase: str | None,
        sandbox_env: dict[str, str] | None = None,
        image: str | None = None,
        base_branch: str | None = None,
        certs_volume: str | None = None,  # noqa: ARG002 — Docker-era compat
        spawn_max_retries: int = DEFAULT_SPAWN_MAX_RETRIES,
        spawn_retry_initial_backoff_seconds: float = (DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS),
        slice_id: str | None = None,
    ):
        """Create a spawn callable compatible with ConcurrentPhaseExecutor.

        Returns a function with signature (role, branch, extra_env, command)
        that spawns a Job via spawn_agent_job.

        Args:
            pipeline_id: Pipeline ID.
            issue_number: GitHub issue number.
            repo_volumes: Repo name to host path mappings.
            mode: Gateway mode (public/private/local).
            repos: Repositories for gateway session.
            phase: Current pipeline phase.
            sandbox_env: Base environment variables.
            image: Container image override.
            base_branch: Branch to base worktrees on.
            slice_id: Optional slice scope (#2403). When supplied, every
                spawn (including ``spawn_specific_roles`` retries) is
                tagged with this slice so concurrent slices in the same
                pipeline get distinct Job names and worktree ids. Without
                this, slice-N spawning ``coder`` would delete slice-(N-1)'s
                still-running ``coder`` Job during the pre-spawn cleanup.

        Returns:
            Callable suitable for ConcurrentPhaseExecutor.spawn_fn.
        """

        def _spawn(
            role: AgentRole,
            branch: str | None = None,
            extra_env: dict[str, str] | None = None,
            command: list[str] | None = None,
            upstream: str | None = None,
            upstream_model: str | None = None,
            event_action: str | None = None,
            event_dedupe_key: str | None = None,
            event_payload_refs: str | None = None,
        ) -> SpawnedContainer | None:
            merged_env = {**(sandbox_env or {}), **(extra_env or {})}
            common_kwargs: dict[str, Any] = {
                "issue_number": issue_number,
                "repo_volumes": repo_volumes,
                "mode": mode,
                "image": image,
                "extra_env": merged_env,
                "repos": repos,
                "phase": phase,
                "branch": branch,
                "base_branch": base_branch,
                "command": command,
                "spawn_max_retries": spawn_max_retries,
                "spawn_retry_initial_backoff_seconds": (spawn_retry_initial_backoff_seconds),
                "upstream": upstream,
                "upstream_model": upstream_model,
            }
            # #3064 slice-2: orchestrator-owned one-shot event spawn. Routes
            # through ``spawn_one_shot_event_job`` so the Job gets the event
            # identity (env + labels) and adoption-on-dedupe-hit; the
            # long-lived ``spawn_agent_job`` pod-mode path is taken otherwise,
            # byte-identical to before.
            if event_dedupe_key is not None and event_action is not None:
                return self.spawn_event_job(
                    pipeline_id,
                    role,
                    action=event_action,
                    dedupe_key=event_dedupe_key,
                    event_payload_refs=event_payload_refs,
                    slice_id=slice_id,
                    **common_kwargs,
                )
            return self.spawn_agent_job(
                pipeline_id=pipeline_id,
                agent_role=role,
                slice_id=slice_id,
                **common_kwargs,
            )

        return _spawn

    # ------------------------------------------------------------------
    # Backward-compatibility aliases for ContainerSpawner method names
    # ------------------------------------------------------------------
    spawn_agent_container = spawn_agent_job
    stop_agent_container = stop_agent_job
    remove_agent_container = remove_agent_job
    list_pipeline_containers = list_pipeline_jobs
    restart_agent_container = restart_agent_job
    spawn_overseer_container = spawn_overseer_job


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

"""Event-job dedupe + spawn + status view factory (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

from typing import Any

import kubernetes_spawner as _pkg
from kubernetes_spawner import (
    _EVENT_JOB_NAME_DISCRIMINATOR_LEN,
    ENV_EVENT_ACTION,
    ENV_EVENT_DEDUPE_KEY,
    ENV_EVENT_PAYLOAD_REFS,
    LABEL_EVENT_ACTION,
    LABEL_EVENT_DEDUPE,
    logger,
)
from models import LIVE_POD_STATUSES, AgentRole


def _event_dedupe_key_live(self, dedupe_key: str) -> bool:
    """Return True iff a Job already carries this dedupe-key label.

    The reconciliation handle: a fresh orchestrator process re-derives
    every event and the spawner asks this before creating a Job, so an
    in-flight Job from a prior process (or a racing duplicate request) is
    adopted rather than duplicated. No spawn state is persisted — the
    label IS the state. Queried via a label selector so the API returns
    only matching Jobs; best-effort (a list failure ⇒ "not live" ⇒ spawn
    proceeds rather than wedging).

    Only Jobs in a non-terminal status (``PENDING``/``RUNNING``) count as
    live. ``list_jobs`` returns *all* label-matching Jobs regardless of
    status, and one-shot event Jobs linger for ``ttl_seconds_after_finished``
    (10 min) after completing, so a terminated Job (``EXITED``/``FAILED``)
    must NOT adopt a re-derived identical event — otherwise an event whose
    pod failed without advancing the tracker would be silently swallowed
    for the TTL window instead of respawned.
    """
    # The selector value MUST use the same label-safe shortening applied
    # to the label on the spawn side, or it can never match the live Job.
    selector = f"{LABEL_EVENT_DEDUPE}={_pkg._dedupe_label_value(dedupe_key)}"
    try:
        jobs = self.k8s.list_jobs(self._namespace, label_selector=selector)
    except Exception as exc:  # noqa: BLE001 — adoption is best-effort
        logger.warning(
            "Failed to list Jobs for dedupe-key reconciliation",
            dedupe_key=dedupe_key,
            error=str(exc),
        )
        return False
    # Count only Jobs whose pod is still doing work (PENDING / CREATING /
    # RUNNING). A *terminal* Job — FAILED (crashed) or EXITED (clean rc=0)
    # — lingers for the ~600s ``ttlSecondsAfterFinished`` window, and
    # adopting one would dead-end the supervisor's bounded respawn: a
    # crashed propose arm would be "adopted" (no new pod) for the whole TTL
    # while its FAILED status keeps re-incrementing the abort streak, so a
    # transient crash falsely escalates to AGENT_FAILED without ever
    # retrying (#3181). Mirrors ``LIVE_POD_STATUSES`` — the single
    # source of truth shared with ``_count_live_pods_for_pipeline`` /
    # startup reconciliation. A non-sequence (e.g. an unconfigured mock) is
    # treated as "no live Job" so the spawn proceeds.
    if not isinstance(jobs, (list, tuple)):
        return False
    return any(getattr(j, "status", None) in LIVE_POD_STATUSES for j in jobs)


def create_event_job_status_view(self) -> _pkg._EventJobStatusView:
    """Return the loop's Job-status observer for event-loop supervision.

    The orchestrator-owned event loop calls ``outcome_for(dedupe_key)``
    once per live key per poll to drive ``JobSupervisor``. Classification
    of a one-shot event Job (located by its dedupe-key label) onto the
    loop's outcome vocabulary:

      * any matching Job ``FAILED`` (non-zero rc / pod died mid-event)
        → ``abnormal`` (increment the streak / back off / respawn);
      * any matching Job still ``RUNNING``/``PENDING`` → ``running``;
      * all matching Jobs ``EXITED`` (clean rc=0) → ``success``;
      * no Job found / list error → ``running``.

    A missing or unreadable Job is deliberately treated as still-running
    rather than a failure: a completed Job that has already been
    garbage-collected, or a transient list error, must never manufacture
    a spurious abort streak. Real abnormal terminations leave a FAILED
    Job behind for at least the TTL window, which this poll observes — and
    which :meth:`_EventJobStatusView.reap_terminated` removes once the loop
    has recorded the abort, so it is observed exactly once.
    """
    return _pkg._EventJobStatusView(self)


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
) -> _pkg.SpawnedContainer | None:
    """Spawn (or adopt) a one-shot Job for a single BRC event (#3064).

    The Job's env carries the full event identity so the consensus
    wrapper's one-shot event handler engages (``EGG_EVENT_ACTION`` ∈
    ``propose|ack|nack`` + ``EGG_EVENT_DEDUPE_KEY``) and the dedupe key
    rides as a Job *label* — the reconciliation handle the event loop
    rebuilds its live set from on restart.

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

    # --- Attempt worktree re-attach + session reuse ---
    reuse_worktree_id: str | None = None
    reuse_repo_volumes: dict[str, str] | None = None
    reuse_session_token: str | None = None
    branch = spawn_kwargs.get("branch")
    repos = spawn_kwargs.get("repos")

    # Build the candidate worktree id matching the existing convention.
    candidate_id = self._build_agent_worktree_id(pipeline_id, agent_role, slice_id=slice_id)
    if repos:
        # Use the composed method that validates AND cleans dirty state
        # (R6 dirty-state policy) so re-attached worktrees are always
        # pristine before the agent runs.
        result = self._try_reuse_worktree(candidate_id, branch, repos)
        if result is not None:
            reuse_worktree_id = candidate_id
            reuse_repo_volumes = result[1]
            logger.info(
                "Event spawn: worktree re-attach succeeded",
                agent_worktree_id=candidate_id,
                pipeline_id=pipeline_id,
                role=agent_role.value,
            )
        else:
            logger.info(
                "Event spawn: worktree re-attach failed — falling back to create-with-retry",
                agent_worktree_id=candidate_id,
                pipeline_id=pipeline_id,
                role=agent_role.value,
            )

    # Session reuse. The gateway session is keyed by the
    # STABLE per-role+slice base Job name (no per-event discriminator), so
    # it survives across the distinct Job names of successive events and
    # can actually be reused. ``_get_or_create_session`` owns the
    # cache-lookup → heartbeat → re-register logic in ONE place (the same
    # path the session-reuse tests drive) — there is no second, divergent
    # inline lookup. When re-attach succeeded the worktree is already
    # present, so we resolve the session here and hand the token to
    # ``spawn_agent_job`` (which then skips its own registration). On a
    # re-attach miss we leave registration to ``spawn_agent_job``'s
    # create-with-retry path so the worktree-creation/session linkage
    # (#1857) stays intact — but still under the stable ``session_base_id``
    # so the next event reuses it.
    session_base_id, _jn2 = self._build_k8s_job_names(pipeline_id, agent_role, slice_id=slice_id)
    if reuse_worktree_id is not None:
        session_info = self._get_or_create_session(
            pipeline_id,
            agent_role,
            slice_id=slice_id,
            mode=spawn_kwargs.get("mode", "public"),
            repos=repos,
            branch=branch,
            base_branch=spawn_kwargs.get("base_branch"),
            phase=spawn_kwargs.get("phase"),
            issue_number=spawn_kwargs.get("issue_number"),
            upstream=spawn_kwargs.get("upstream"),
            upstream_model=spawn_kwargs.get("upstream_model"),
            jira_ticket=spawn_kwargs.get("jira_ticket"),
        )
        if session_info is not None:
            reuse_session_token = session_info.session_token
            logger.info(
                "Event spawn: resolved gateway session (reuse-or-register)",
                pipeline_id=pipeline_id,
                role=agent_role.value,
                session_container_id=session_base_id,
            )

    event_env: dict[str, str] = {
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
        LABEL_EVENT_DEDUPE: _pkg._dedupe_label_value(dedupe_key),
        LABEL_EVENT_ACTION: action,
    }
    caller_labels = spawn_kwargs.pop("extra_labels", None) or {}
    merged_labels = {**caller_labels, **event_labels}

    # Pop ``repo_volumes`` from ``spawn_kwargs`` to prevent the stale
    # pre-allocation value (from ``_spawn``'s ``common_kwargs``) from
    # leaking through ``**spawn_kwargs`` to ``spawn_agent_job``. When
    # re-attach succeeded, the
    # validated ``reuse_repo_volumes`` replaces it; when re-attach
    # failed, the original value from ``spawn_kwargs`` is passed
    # explicitly so ``spawn_agent_job``'s create-with-retry path
    # produces fresh volumes.
    spawn_repo_volumes = spawn_kwargs.pop("repo_volumes", None)
    resolved_repo_volumes = (
        reuse_repo_volumes if reuse_repo_volumes is not None else spawn_repo_volumes
    )

    return self.spawn_agent_job(
        pipeline_id,
        agent_role,
        slice_id=slice_id,
        extra_env=merged_env,
        extra_labels=merged_labels,
        job_name_suffix=dedupe_key[:_EVENT_JOB_NAME_DISCRIMINATOR_LEN],
        repo_volumes=resolved_repo_volumes,
        reuse_worktree_id=reuse_worktree_id,
        existing_session_token=reuse_session_token,
        # Register/cache/heartbeat the session under the stable base id so
        # it is reused across the role's successive event spawns, rather
        # than re-registered under each per-event Job name.
        session_container_id=session_base_id,
        **spawn_kwargs,
    )

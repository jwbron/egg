"""Event-job dedupe + spawn + status view factory (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import json
from datetime import datetime
from typing import Any

import kubernetes_spawner as _pkg
from kubernetes_spawner import (
    _EVENT_JOB_NAME_DISCRIMINATOR_LEN,
    _EVENT_JOB_TERMINATION_WAIT_S,
    ENV_EVENT_ACTION,
    ENV_EVENT_DEDUPE_KEY,
    ENV_EVENT_PAYLOAD_REFS,
    ENV_WORKTREE_RECOVERY,
    LABEL_EVENT_ACTION,
    LABEL_EVENT_DEDUPE,
    logger,
)
from models import LIVE_POD_STATUSES, AgentRole


def _list_event_jobs(self, dedupe_key: str) -> list[Any]:
    """Return every Job carrying ``dedupe_key``'s label, or ``[]``.

    Queried via a label selector so the API returns only matching Jobs;
    best-effort (a list failure ⇒ ``[]`` ⇒ callers treat the key as
    un-owned and spawn rather than wedging). A non-sequence (e.g. an
    unconfigured mock) is normalized to ``[]`` for the same reason.
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
        return []
    if not isinstance(jobs, (list, tuple)):
        return []
    return list(jobs)


def _job_is_terminating(job: Any) -> bool:
    """Return True iff *job* has been deleted but has not gone away yet.

    Kubernetes Job deletion is asynchronous: the API server stamps
    ``metadata.deletionTimestamp`` and the object lingers — still
    reporting ``active > 0`` ⇒ ``RUNNING`` — until its dependent pods
    finish terminating.

    Tested with ``isinstance`` rather than ``is not None`` because
    ``ContainerInfo.deletion_timestamp`` is a ``datetime | None`` and
    anything else is not a real stamp — most notably an auto-attribute on
    an unconfigured mock, which would otherwise read as "terminating" and
    silently disable adoption. Same "a mock is not evidence" convention
    the live-Job list already applies to a non-sequence ``list_jobs``.
    """
    return isinstance(getattr(job, "deletion_timestamp", None), datetime)


def _job_is_live(job: Any) -> bool:
    """Return True iff *job* still has a pod that will do the event's work."""
    return getattr(job, "status", None) in LIVE_POD_STATUSES and not _job_is_terminating(job)


def _event_dedupe_key_live(self, dedupe_key: str) -> bool:
    """Return True iff a *live* Job already carries this dedupe-key label.

    The reconciliation handle: a fresh orchestrator process re-derives
    every event and the spawner asks this before creating a Job, so an
    in-flight Job from a prior process (or a racing duplicate request) is
    adopted rather than duplicated. No spawn state is persisted — the
    label IS the state.

    "Live" means a Job that still has a pod which will do this event's
    work. Three states must NOT qualify:

    * *terminal* — ``list_jobs`` returns all label-matching Jobs regardless
      of status, and one-shot event Jobs linger for
      ``ttl_seconds_after_finished`` (10 min) after completing. Adopting a
      ``FAILED``/``EXITED`` Job would dead-end the supervisor's bounded
      respawn: a crashed propose arm would be "adopted" (no new pod) for
      the whole TTL while its FAILED status keeps re-incrementing the abort
      streak, so a transient crash falsely escalates to AGENT_FAILED
      without ever retrying (#3181);
    * *terminating* — a deleted Job keeps reporting ``RUNNING``/``PENDING``
      until its pod actually terminates, so status alone cannot see that it
      is on its way out. ``restart_agent`` deletes the role's Job and
      delegates the respawn to the event loop; if the next poll landed
      inside that deletion window the loop adopted the corpse, declined to
      spawn, and the role silently vanished — no pod, no Job, state still
      ``running`` (#3597). Adoption is only ever correct for a Job that
      will still run the event, which a terminating one never will;
    * *unknown* — a list failure yields no Jobs, which spawns (a duplicate
      Job is recoverable; a swallowed event is not).

    The live status set mirrors ``LIVE_POD_STATUSES`` — the single source
    of truth shared with ``_count_live_pods_for_pipeline`` / startup
    reconciliation.
    """
    return any(_job_is_live(j) for j in self._list_event_jobs(dedupe_key))


def _await_terminating_event_jobs(
    self,
    jobs: list[Any],
    *,
    pipeline_id: str,
    role: str,
    action: str,
    dedupe_key: str,
) -> None:
    """Block until the terminating Jobs among *jobs* are gone (bounded) (#3597).

    A one-shot event Job's name is derived from the dedupe key, so the
    replacement this spawn is about to create carries the *same* name as
    the Job that was just deleted. ``delete_job`` returns as soon as the
    deletion is accepted, and the object then lingers with its finalizer
    until its pods finish terminating — creating into that window returns
    409 ``AlreadyExists`` (the #2655 race, which ``restart_agent_job``
    already waits out on its own path).

    Best-effort and bounded by ``_EVENT_JOB_TERMINATION_WAIT_S`` shared
    across all matching Jobs: on timeout (or a k8s client without the wait
    helper) we log and let the spawn proceed — a 409 there is raised as a
    ``KubernetesSpawnError``, which the event loop isolates per-role and
    retries on the next poll, so a slow reap costs a poll interval rather
    than the silent vanish this whole path exists to prevent.

    Each of those outcomes is logged for what it actually is, matching the
    taxonomy the restart route applies: "still present" is only claimed
    after a wait ran and observed the Job, and every path that skips the
    wait entirely — a Job the listing did not name, no helper on the
    client, budget already spent — says so instead of borrowing an
    observation it never made.

    The wait runs on the event-loop poll thread, so it delays the roles
    handled later in the same ``poll_once`` pass by up to the budget. That
    is the accepted tradeoff: the wait is bounded, only reachable when a
    matching Job is mid-deletion, and the alternative is the role
    vanishing outright. Mid-deletion is usually a restart, but not only:
    ``_job_is_terminating`` does not filter on status, so the TTL
    controller reaping a finished prior Job with the same dedupe key
    (one-shot Jobs carry ``ttl_seconds_after_finished``) also enters the
    wait. That case is harmless and wanted — its pods are already gone so
    the wait returns near-instantly, and the 409 protection still applies
    to the recycled name.
    """
    terminating = [j for j in jobs if _job_is_terminating(j)]
    if not terminating:
        return
    # Partition before anything reports a count: a Job the listing did not
    # name is not one we could ever have waited on, so it gets its own line
    # rather than being folded into the counts the skip-paths below report.
    #
    # Defensive: ``KubernetesClient.list_jobs`` always populates ``job_name``
    # (and mirrors it onto ``container_name``), so the unnamed branch is
    # unreachable against the production lister — it guards a future/alternate
    # backend whose listing is thinner, mirroring the restart route's
    # ``addressable=False`` branch.
    pending: list[str] = []
    unnamed: list[str] = []
    for job in terminating:
        job_name = getattr(job, "job_name", None) or getattr(job, "container_name", None)
        if job_name:
            pending.append(job_name)
        else:
            # No name to wait on, but the listing still carries a container id
            # (``KubernetesClient.list_jobs`` sets it from the Job's uid). Carry
            # it through so this line hands the operator the same actionable
            # handle the restart route's counterpart does, rather than a bare
            # count they cannot trace back to an object.
            unnamed.append(str(getattr(job, "container_id", None) or "<unknown>"))
    if unnamed:
        logger.warning(
            "Event spawn: teardown wait not performed; terminating Job(s) unobserved",
            pipeline_id=pipeline_id,
            role=role,
            action=action,
            dedupe_key=dedupe_key,
            terminating=len(unnamed),
            container_ids=unnamed,
            reason="unaddressable",
        )
    if not pending:
        return
    waiter = getattr(self.k8s, "wait_for_job_gone", None)
    if waiter is None:
        # Defensive: ``KubernetesClient`` implements ``wait_for_job_gone``,
        # so this guards a future/alternate backend. Logged rather than
        # returned silently — the spawn proceeds into a window that may 409,
        # and the operator should be able to see why nothing waited.
        logger.warning(
            "Event spawn: teardown wait not performed; terminating Job(s) unobserved",
            pipeline_id=pipeline_id,
            role=role,
            action=action,
            dedupe_key=dedupe_key,
            terminating=len(pending),
            reason="no_wait_helper",
        )
        return
    logger.info(
        "Event spawn: waiting for terminating Job(s) to be reaped before respawn",
        pipeline_id=pipeline_id,
        role=role,
        action=action,
        dedupe_key=dedupe_key,
        terminating=len(pending),
    )
    deadline = _pkg.time.monotonic() + _EVENT_JOB_TERMINATION_WAIT_S
    for job_name in pending:
        remaining = deadline - _pkg.time.monotonic()
        if remaining <= 0:
            # No wait ran for this Job, so "still present" is not ours to
            # claim — the snapshot said terminating, nothing observed since.
            logger.warning(
                "Event spawn: teardown wait not performed; terminating Job unobserved",
                pipeline_id=pipeline_id,
                role=role,
                action=action,
                dedupe_key=dedupe_key,
                job_name=job_name,
                reason="budget_exhausted",
            )
            continue
        try:
            gone = bool(waiter(job_name, self._namespace, timeout_s=remaining))
        except Exception as exc:  # noqa: BLE001 — the wait is best-effort
            logger.warning(
                "Failed to wait out a terminating event Job; spawning anyway",
                pipeline_id=pipeline_id,
                role=role,
                dedupe_key=dedupe_key,
                job_name=job_name,
                error=str(exc),
            )
            continue
        if not gone:
            logger.warning(
                "Terminating event Job still present; spawn may 409 and retry next poll",
                pipeline_id=pipeline_id,
                role=role,
                action=action,
                dedupe_key=dedupe_key,
                job_name=job_name,
            )


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


def _recovery_env_json(notices: list[dict[str, Any]]) -> str:
    """Serialize the #3684 worktree-recovery notices for the pod env, capped.

    Every field is a sha, a count, or a ref name, so a single notice is well
    under 300 bytes and the cap only bites on a pathological many-repo
    discard. Overshoot drops whole trailing entries rather than truncating
    the JSON: a half-serialised value decodes to nothing on the pod side,
    which would lose the ref for repo 1 to save bytes on repo 8. Dropping
    from the tail keeps the earliest (and, on a multi-repo pipeline, the
    primary) recovery ref intact; the bus record still carries all of them.
    """
    kept = list(notices)
    while kept:
        raw = json.dumps(kept, ensure_ascii=False)
        if len(raw.encode("utf-8")) <= _pkg._WORKTREE_RECOVERY_ENV_MAX_BYTES:
            return raw
        kept.pop()
    return "[]"


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
    set racing a restart. A Job that is merely *terminating* is not live
    (see :meth:`_event_dedupe_key_live`): adopting one produced a role
    with no pod at all (#3597), so we wait it out and spawn instead.

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

    existing_jobs = self._list_event_jobs(dedupe_key)
    if any(_job_is_live(j) for j in existing_jobs):
        logger.info(
            "Adopting existing live Job for event (dedupe hit)",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            action=action,
            dedupe_key=dedupe_key,
        )
        return None

    # #3597: no live Job owns this key, but a *terminating* one may still be
    # holding its name. The Job name is deterministic in the dedupe key, so
    # creating now would 409 ``AlreadyExists`` against the corpse. Wait for
    # the API server to actually reap it (same reasoning as the #2655
    # restart-path wait) before spawning the replacement.
    _await_terminating_event_jobs(
        self,
        existing_jobs,
        pipeline_id=pipeline_id,
        role=agent_role.value,
        action=action,
        dedupe_key=dedupe_key,
    )

    # --- Attempt worktree re-attach + session reuse ---
    reuse_worktree_id: str | None = None
    reuse_repo_volumes: dict[str, str] | None = None
    reuse_session_token: str | None = None
    branch = spawn_kwargs.get("branch")
    repos = spawn_kwargs.get("repos")
    # The pipeline's real gateway network mode ("public" / "private").
    # It MUST reach the discard-salvage push: a private-mode pipeline on a
    # private repo is DENIED by the gateway's private-repo policy if the
    # push carries the "public" default, silently degrading auto-salvage
    # to record-only — exactly the silent-loss class #3509 exists to
    # prevent. The concurrent spawn fn forwards "mode" in common_kwargs.
    mode = spawn_kwargs.get("mode", "public")

    # Build the candidate worktree id matching the existing convention.
    candidate_id = self._build_agent_worktree_id(pipeline_id, agent_role, slice_id=slice_id)
    # #3684: filled by the re-attach when its hard-reset moves unpushed work
    # onto an ``egg/recovered/...`` ref. The salvage itself has been reliable
    # since #3639/#3644; what was missing is telling the agent, and the agent
    # this spawn is about to create is the one that needs to hear it.
    recovery_notices: list[dict[str, Any]] = []
    if repos:
        # Use the composed method that validates AND cleans dirty state
        # (R6 dirty-state policy) so re-attached worktrees always start
        # with a clean tree at the role branch tip, or a clean
        # fast-forward ahead of it (#3506), before the agent runs. The
        # pipeline/role/slice context (plus the gateway network mode) lets
        # the cleanup auto-salvage and durably record any commits its
        # hard-reset discards (#3509).
        result = self._try_reuse_worktree(
            candidate_id,
            branch,
            repos,
            pipeline_id=pipeline_id,
            agent_role=agent_role.value,
            slice_id=slice_id,
            mode=mode,
            phase=spawn_kwargs.get("phase"),
            recovery_out=recovery_notices,
        )
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
            # Bind a fresh registration to the worktree just validated,
            # so the gateway looks it up instead of creating an orphan
            # worktree keyed by the session id (#3502 naming split).
            worktree_container_id=reuse_worktree_id,
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
    # #3684: this spawn's worktree re-attach moved unpushed work off the tree.
    # Carry the recovery ref into the pod so the prompt composer can lead with
    # it. Without this the only record is a bus message the agent must think to
    # go read, and an agent that has just found its files missing does not
    # think to read a BRC transcript — it re-implements (the #3684 incident:
    # 8 commits / 3072 insertions re-derived from scratch while the ref sat on
    # the remote). Set on the discard spawn only; every ordinary spawn leaves
    # the key unset and renders byte-identically.
    if recovery_notices:
        event_env[ENV_WORKTREE_RECOVERY] = _recovery_env_json(recovery_notices)
        logger.info(
            "Event spawn: injecting worktree-recovery notice into pod env",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            slice_id=slice_id,
            recovery_refs=[n.get("recovery_ref") for n in recovery_notices],
        )
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

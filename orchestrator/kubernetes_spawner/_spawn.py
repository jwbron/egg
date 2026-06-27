"""Agent-job spawn + job-name building (#3312).

Private submodule of the ``kubernetes_spawner`` sub-package; import through
the barrel (``from kubernetes_spawner import ...``), not directly.
"""

import os
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import kubernetes_spawner as _pkg
from gateway_client import SessionInfo
from kubernetes_client import (
    LABEL_AGENT_ROLE,
    LABEL_CONTAINER_NAME,
    LABEL_ORCHESTRATOR,
    LABEL_PIPELINE_ID,
    LABEL_SLICE_ID,
    KubernetesClient,
    KubernetesClientError,
    PodNotFoundError,
)
from kubernetes_spawner import (
    _SPAWN_RETRY_BACKOFF_MULTIPLIER,
    DEFAULT_SPAWN_MAX_RETRIES,
    DEFAULT_SPAWN_RETRY_INITIAL_BACKOFF_SECONDS,
    GATEWAY_K8S_URL,
    ORCHESTRATOR_K8S_URL,
    PROXY_URL,
    KubernetesSpawnError,
    logger,
)
from models import AgentRole

if TYPE_CHECKING:
    from egg_container import MountSpec


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
    reuse_worktree_id: str | None = None,
    existing_session_token: str | None = None,
    session_container_id: str | None = None,
) -> _pkg.SpawnedContainer:
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
        upstream: Per-agent upstream identifier.
            Forwarded to the gateway session-create call only when
            set; ``None`` keeps the default Anthropic routing.
        upstream_model: Upstream-side model name to rewrite the
            request body's ``model`` field to.
            ``None`` on the Anthropic path — the body is forwarded
            unchanged.
        reuse_worktree_id: When set, skip ``create_worktrees()`` and use
            this id as the per-agent worktree identifier (the worktree
            was validated by the caller via
            :func:`_validate_worktree_for_reuse`). The caller supplies the
            resolved ``repo_volumes`` separately. ``None`` preserves the
            existing create-with-retry path.
        existing_session_token: When set, skip gateway session
            registration and use this token directly. ``None`` registers
            a fresh session. Used together with ``reuse_worktree_id``
            to avoid redundant gateway round-trips across successive
            one-shot event spawns.
        session_container_id: Stable identifier under which the gateway
            session is registered, heartbeat, cached, and torn down — kept
            distinct from the per-event k8s Job name so the session
            survives (and is reused) across a role's successive one-shot
            event spawns whose Job names each carry a per-event
            discriminator. ``None`` (the pod-mode default) registers the
            session under ``job_name`` exactly as before.

    Returns:
        SpawnedContainer with Job and session info

    Raises:
        KubernetesSpawnError: If spawning fails
    """
    # Spawn→invoke latency timer (p50<60s budget). Uses the
    # injectable monotonic clock so tests can drive a simulated clock.
    _spawn_start = self._clock()
    job_name, actual_k8s_job_name = self._build_k8s_job_names(
        pipeline_id, agent_role, slice_id=slice_id
    )
    # One-shot event spawns append a deterministic
    # per-event discriminator so distinct events for one role don't
    # collide on a single Job name (which would make the pre-spawn
    # cleanup below delete a sibling event's in-flight Job).
    #
    # ``_fit_k8s_name`` bounds the *unprefixed* ``egg-agent-…`` name we
    # hand to ``create_container``. Note this is NOT the final k8s budget
    # check: ``create_container`` prepends ``JOB_PREFIX`` (``egg-sandbox-``,
    # 12 chars) and re-truncates the *prefixed* form via
    # ``_normalize_k8s_job_name`` (which is what actually enforces the
    # 63-char RFC-1123 limit, with an 8-char sha1 over the full prefixed
    # name). So this pre-truncation is belt-and-suspenders — it keeps the
    # handed name readable and the delete/create call args in step, while
    # the downstream normalization guarantees budget compliance and
    # collision-freedom regardless.
    if job_name_suffix:
        job_name = _pkg._fit_k8s_name(f"{job_name}-{job_name_suffix}")
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
            raise KubernetesSpawnError(f"Gateway is not healthy: {health.error or health.status}")

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
    # One-shot event labels: the dedupe-key label is the
    # reconciliation handle the orchestrator event loop queries to detect
    # an in-flight Job for a given event after a restart. Applied last so
    # the caller's event labels are authoritative.
    if extra_labels:
        labels.update(extra_labels)

    # Host UID/GID for file ownership in worktrees
    host_uid = int(os.environ.get("HOST_UID", 1000))
    host_gid = int(os.environ.get("HOST_GID", 1000))

    # Per-agent worktree isolation: create or reuse a dedicated worktree.
    # Slice scope (#2403): concurrent slices in the same pipeline
    # MUST get distinct worktree ids — otherwise slice-N's coder
    # spawns onto slice-(N-1)'s already-mounted worktree (or
    # races with it during cleanup). The id is also the agent's
    # ``CONTAINER_ID`` env and the gateway worktree key, so the
    # whole gateway / agent / orchestrator triangle agrees on it.
    # When ``reuse_worktree_id`` is set, the caller
    # already validated the worktree via ``_validate_worktree_for_reuse``.
    if reuse_worktree_id:
        agent_worktree_id = reuse_worktree_id
        worktree_created_this_call = False
        logger.info(
            "Reusing existing validated worktree",
            agent_worktree_id=agent_worktree_id,
            role=agent_role.value,
            pipeline_id=pipeline_id,
        )
    else:
        agent_worktree_id = self._build_agent_worktree_id(
            pipeline_id, agent_role, slice_id=slice_id
        )
        worktree_created_this_call = False

    if repos and not reuse_worktree_id:
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
                transient = _pkg._is_transient_spawn_failure(e)
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
                    error_category=_pkg._classify_spawn_error(e),
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
    elif _pkg._role_needs_worktree(agent_role):
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
    session_token = existing_session_token  # reuse when supplied
    agent_anchor_id = f"{agent_role.value}-{job_name[:8]}"
    # The gateway session is keyed by a stable id (per
    # role+slice) when the event path supplies one, so it persists across
    # the per-event Job names; pod mode falls back to the Job name.
    session_id = session_container_id or job_name

    try:
        if existing_session_token:
            # Caller provided a validated, live session
            # token — skip gateway registration. Build a minimal
            # SessionInfo stub so downstream env injection works.
            logger.info(
                "Reusing existing gateway session",
                job_name=job_name,
                session_token=session_token[:12] + "..." if session_token else "(none)",
            )
            session_info = SessionInfo(
                session_token=session_token,
                container_id=session_id,
                container_ip=None,
                mode=mode or "public",
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(hours=24),
            )

        if session_info is None:
            session_info = self.gateway.register_session(
                container_id=session_id,
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
                worktree_container_id=(agent_worktree_id if worktree_created_this_call else None),
                # Per-agent upstream routing. Both fields
                # are forwarded to the gateway only when set, so the
                # default-Claude case keeps the request body byte-
                # identical to the Anthropic-default wire shape.
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

            # Cache the session token for potential reuse on the next
            # one-shot event spawn. Keyed by the stable
            # ``session_id`` (not the per-event Job name) so a subsequent
            # event for the same role+slice hits this entry.
            cache_key = (pipeline_id, agent_role.value, slice_id, session_id)
            self._session_token_cache[cache_key] = session_token

    except _pkg.GatewayError as e:
        raise KubernetesSpawnError(f"Failed to register gateway session for {job_name}: {e}") from e

    try:
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
        # Single source of truth: the spawner is
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
        # every non-``main`` repo and reviewers silently lost the
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
        wait_allowlist = _pkg._resolve_wait_producer_allowlist(
            phase=phase, role=agent_role.value, repo=pipeline_repo
        )
        if wait_allowlist:
            environment["EGG_WAIT_PRODUCER_ALLOWLIST"] = wait_allowlist

        # Forward operator-set context-discipline flags (#3200) from
        # the orchestrator's own env into the pod. Read in-pod but flippable
        # only on the orchestrator deployment, so this is the single
        # operator knob (kubectl set env). Placed before the extra_env merge
        # so a per-spawn override still wins. See _forwarded_discipline_env.
        environment.update(_pkg._forwarded_discipline_env(os.environ))

        # Caller's extra_env overrides defaults, except protected keys
        if extra_env:
            for key, value in extra_env.items():
                if key in _pkg._PROTECTED_ENV_KEYS:
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

                digest = hashlib.sha1(volume_name.encode(), usedforsecurity=False).hexdigest()[:8]
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

        spawn_ms = (self._clock() - _spawn_start) * 1000.0

        logger.info(
            "Agent Job created",
            job_name=job_name,
            container_id=container_info.container_id[:12],
            pipeline_id=pipeline_id,
            role=agent_role.value,
            has_session=session_info is not None,
            # Emit the per-spawn latency so the finer ``spawn_agent_job``
            # sub-metric is observable in logs (the p50 budget itself reads
            # the coarser ``spawn_dispatch_seconds`` timing field).
            spawn_ms=round(spawn_ms, 3),
        )

        return _pkg.SpawnedContainer(
            container_info=container_info,
            session_info=session_info,
            agent_role=agent_role,
            pipeline_id=pipeline_id,
            environment=environment,
            spawn_ms=spawn_ms,
        )

    except KubernetesClientError as e:
        # Clean up gateway session only if WE registered one in this call.
        # On the session-reuse path (``existing_session_token``
        # supplied) ``session_info`` is a stub wrapping a token registered
        # by an earlier event and still cached under the stable base id —
        # deleting it here would tear down the session the next event would
        # reuse and leave the ``_session_token_cache`` entry dangling. Skip
        # the delete on that path; ``_get_or_create_session`` heartbeats and
        # re-registers on the next event if the gateway has since dropped it.
        if session_info and not existing_session_token:
            try:
                self.gateway.delete_session(session_info.session_token)
            except _pkg.GatewayError:
                pass  # Best effort cleanup
        # Only clean up the worktree if we created it in this call
        if worktree_created_this_call and not preserve_worktree_on_failure:
            try:
                self.gateway.delete_worktrees(container_id=agent_worktree_id, force=True)
            except Exception:
                pass  # Best effort cleanup
        raise KubernetesSpawnError(f"Failed to spawn Job: {e}") from e

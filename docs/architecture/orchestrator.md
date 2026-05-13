# Orchestrator Architecture

This document describes the orchestrator component and the three deployment modes for egg: local, remote-single, and distributed.

## Overview

The orchestrator manages SDLC pipeline execution, agent lifecycle, and agent coordination. It provides:

- Pipeline state management (phases, tasks, decisions)
- Agent pod spawning and monitoring (Kubernetes Jobs)
- Multi-agent coordination (for parallel execution)
- Human-in-the-loop (HITL) decision handling
- Completion signaling and handoff management

## Pipeline State Persistence

The orchestrator persists pipeline state using a dedicated git worktree on an orphan branch.

**Architecture:**
- All pipeline state is stored in `.egg-state/pipelines/{id}.json` files
- Files live on the `egg/pipeline-state` orphan branch (never merged to main)
- Accessed via a persistent git worktree at `/home/egg/.egg-state/pipeline-worktree` (single-repo) or `/home/egg/.egg-state/pipeline-worktree-{repo_name}` per repo (multi-repo)
- State branch is synced to remote after every commit (best-effort, async push via daemon thread)
- On startup, restores from remote if local branch is missing (cross-host recovery)

**Key properties:**
- Read/write operations go directly to the worktree directory on disk
- Commits are made in-place and stay on the state branch
- Survives orchestrator restarts by reading from git
- Mirrors the `egg/checkpoints/v2` pattern for cross-host recovery

**Worktree lifecycle:**
- Created lazily on first state access
- Validated on each access (repairs stale/broken worktrees)
- Locked via `git worktree lock` after creation, and idempotently re-locked on the healthy fast path so worktrees that pre-date this change get the lock too — cross-pod `git worktree prune` skips a locked worktree (#2324)
- Stale admin dirs from crashed worktrees removed surgically on recreation (not via `git worktree prune`)

This differs from agent worktrees (managed by the gateway for agent isolation). The orchestrator manages its own state worktree independently.

**Startup reconciliation:**

On orchestrator restart, orphaned container state is automatically recovered:

1. **RUNNING pipelines**: For each pipeline showing `status=RUNNING`, the reconciliation process recovers orphaned state:
   - **Un-spawned PENDING phase** (crash-between-submit-and-spawn): If the current phase is `PENDING` with `started_at=None` and no containers or agents, the orchestrator crashed before `_run_pipeline` reached `executor.spawn_all`. The pipeline is immediately marked `FAILED` with an actionable message; no container scanning is performed for this pipeline. Operators restart via `POST /pipelines/{id}/start` (the `start_pipeline` MCP tool).
   - **Live-pod safety net** (#2411): before any stale-record scan, queries Kubernetes for pods labeled `egg.pipeline.id=<id>`. If **any** pods are alive for the pipeline, it is left `RUNNING` and the per-agent scan below is skipped — record drift between persisted state and the new orchestrator process's view of pods is expected after a restart and is reconciled by the running orchestrator. If the pod query itself fails, the pipeline is also left `RUNNING` (fail-safe: defer to the running orchestrator's reconciliation rather than risk a false-positive `FAILED` transition). Only when the pipeline has **zero live pods** does reconciliation fall through to the per-agent stale scan below.
   - **Stale-record scan scope** (zero-live-pod path only): scans only the **current phase** for stale containers. This applies when the phase has containers or agents — the un-spawned PENDING case above is handled separately. Containers from prior phases are intentionally terminated and their absence is expected — checking all phases caused false `FAILED` transitions when the orchestrator restarted mid-pipeline.
   - Any agent in the current phase whose pod is absent from the live Kubernetes pod set is marked `FAILED`.
   - If at least one stale entry is found, the pipeline itself is marked `FAILED` with an error message instructing operators to restart via `POST /pipelines/{id}/start`.

2. **AWAITING_HUMAN pipelines**: For each pipeline showing `status=AWAITING_HUMAN` with no pending decisions (orphaned after a restart where the decision was already resolved), the pipeline is marked `FAILED` with an error message instructing operators to restart via `POST /pipelines/{id}/start` (the `start_pipeline` MCP tool). The restart endpoint will automatically recover by parsing the latest phase_gate resolution and either advancing to the next phase (approved) or resetting the current phase for re-run (request_changes/change_approach).

3. **Concurrent pipeline consensus trackers**: For each `RUNNING` pipeline (i.e., those not already marked `FAILED` by step 1) in a concurrent phase, the in-memory `PeerConsensusTracker` (which does not persist to disk) is reconstructed by replaying `CONSENSUS_*` messages from the message store in timestamp order. This allows agents that are mid-consensus to resume correctly after a restart rather than looping. Reconstruction is best-effort — if no consensus messages are found, the tracker is left absent and agents will encounter an empty consensus state (handled by the wrapper's message-bus fallback).

This prevents pipelines from being stuck in `RUNNING` or `AWAITING_HUMAN` states indefinitely after a crash. Operators (or CI systems) can detect the `FAILED` status and restart the pipeline using the existing restart endpoint, which preserves worktrees and phase state while re-spawning containers.

See `orchestrator/state_store.py` and `orchestrator/startup_reconciliation.py` for implementation details.

**Runtime pod monitoring:**

A background `KubernetesMonitor` thread runs continuously after orchestrator startup to detect agent pod failures during execution. The monitor periodically checks pod status via the Kubernetes API and invokes registered handlers when state changes occur (pod exits, fails, or becomes unhealthy).

A pipeline reconciliation handler detects when agent pods exit or fail during runtime and updates pipeline state accordingly. The handler scans **all phases** within each `RUNNING` or `AWAITING_HUMAN` pipeline (including completed phases) to find the exited pod, as reviewer agents may continue running after their phase has transitioned to `COMPLETE`. Terminal pipelines (`FAILED`, `COMPLETE`, `CANCELLED`) are also scanned if they still have stale `RUNNING` agent or container records — an optimization that skips the common case where no cleanup is needed.

When a container running an agent exits with a non-clean code, the handler marks the container as `FAILED` and marks the owning agent as `FAILED` with an error message — unless the agent is already `COMPLETE` (i.e., it completed via BRC consensus), in which case the exit is ignored. The handler only updates agent and container sub-records; the pipeline's top-level `status` is never mutated by the monitor (see #2210). The pipeline-level decision about whether the pipeline itself has failed lives in the BRC poll loop in `_run_concurrent_phase`, which has full consensus context the monitor lacks. Containers that exit with code 0 (graceful exit after BRC protocol work) or 143 (orchestrator-initiated SIGTERM during phase teardown) are reconciled as clean exits — `_classify_exit` marks the agent `COMPLETE` and the container `EXITED` rather than `FAILED`. (Mechanism note: exit 0 emits a `STOPPED` event that bypasses reconciliation entirely; exit 143 still emits a `FAILED` event, but `_reconcile_pod_state` consults `_classify_exit` and routes it to the clean-exit branch.) When BRC consensus completes, the concurrent phase runner proactively marks agent containers as `EXITED` with exit code 0 so that subsequent monitor sweeps treat them as clean exits; the agent-COMPLETE check is a secondary defense for the event window before that update is persisted. This complements startup reconciliation by catching failures that occur during execution rather than only on orchestrator restart.

Additionally, the concurrent phase runner (`_run_concurrent_phase`) treats consensus completion (`is_complete=True`) as the **authoritative success signal**. If all agents have confirmed consensus but some containers exited with non-zero codes (e.g., the consensus wrapper exhausted restarts before the final consensus check could run), the phase runner logs a warning about the prior failures but returns success (exit code 0). This prevents the compounding failure scenario where successful BRC consensus is overridden by stale container exit codes. A final `check_consensus()` recheck is also performed on the all-container-exit path (step 5) before returning failure, closing a race window where the step-2 consensus check reads stale tracker state and step 5 returns `exit_code=1` without verifying that consensus is genuinely incomplete.

In addition to the event-driven handler, `ContainerMonitor.start_periodic_reconciliation()` runs a second background thread that sweeps every 30 seconds for stale containers that may have exited between Docker events (e.g., missed events during a partial restart). This periodic sweep checks the **current phase** of each `RUNNING` or `AWAITING_HUMAN` pipeline, and also of any terminal pipeline that still has stale `RUNNING` records. On non-`RUNNING` pipelines it reconciles only the sub-records (agent/container status), leaving the pipeline's top-level status unchanged. Like the event-driven handler, the sweep inspects the actual container exit code before reconciling: containers that exited with code 0 or 143 (clean exit — e.g., the consensus wrapper completing gracefully, or an orchestrator-initiated SIGTERM during phase teardown) do not trigger `FAILED` reconciliation. Exit 0 short-circuits the sweep loop directly; exit 143 also short-circuits when the phase has already moved past `RUNNING`, but a 143 observed while the phase is still `RUNNING` falls through to `_reconcile_pod_state`, which uses `_classify_exit` to mark the agent `COMPLETE` instead of `FAILED`. The first sweep is intentionally delayed by one interval since startup reconciliation already ran immediately before the thread was started.

The monitor uses per-pipeline locking and optimistic version checks to prevent race conditions with concurrent state writers (e.g., agent signal handlers).

See `orchestrator/kubernetes_monitor.py` for implementation details.

**Health check framework:**

A two-tier health check framework provides structured, extensible failure detection across the pipeline lifecycle. All checks implement a common `HealthCheck` protocol and produce `HealthResult` values with a status (`HEALTHY`/`DEGRADED`/`FAILED`), reasoning, and a suggested action (`CONTINUE`/`FAIL_PIPELINE`/`ALERT`).

**Tier 1 (Programmatic)** checks are fast and deterministic. They run on every lifecycle trigger and cover structural invariants: container liveness, startup state, phase output presence, state consistency, and consensus stall detection. Container liveness and startup state checks are adapters over existing `ContainerMonitor` and `reconcile_stale_containers` logic. The phase output check detects the issue-835 pattern where agents complete successfully but produce no artifacts (e.g., no commits on the remote branch after an implement phase). The state consistency check cross-references orchestrator state against Docker reality and contract data. The consensus stall check fires on `RUNTIME_TICK` and `ON_DEMAND` for concurrent execution phases: when all agents are confirmed but the phase has not advanced past a 60-second grace period, it reports `DEGRADED` and `ContainerMonitor` drives recovery (tracker reconstruction first, then aggressive agent/phase completion with optimistic locking).

**Tier 2 (Semantic)** checks are LLM-powered and evaluate whether agents made meaningful progress. The `AgentInspectorCheck` sends pipeline context (recent commits, diff stats, agent output files, SDLC contract state) to Claude via the Agent SDK (default model: `sonnet`) and interprets a structured JSON verdict. On API failure, the check gracefully degrades to HEALTHY — Tier 2 failures never block the pipeline. Tier 2 checks run conditionally — at `WAVE_COMPLETE` only when Tier 1 reports `DEGRADED`, and always at `PHASE_COMPLETE` and `ON_DEMAND`.

**Lifecycle integration:**
- `STARTUP`: Runs after startup reconciliation on all RUNNING pipelines (non-blocking)
- `RUNTIME_TICK`: Triggered by pod state changes and on every periodic reconciliation sweep via `KubernetesMonitor` (non-blocking). Firing on every sweep ensures pipelines with no pod churn (e.g. all agents quietly polling post-BRC consensus) still exercise the consensus-stall recovery path.
- `WAVE_COMPLETE`: Runs after each agent wave completes; `FAIL_PIPELINE` breaks wave execution
- `PHASE_COMPLETE`: Runs before phase advance in `routes/phases.py`; `FAIL_PIPELINE` blocks the transition (409 Conflict)
- `ON_DEMAND`: Available via `GET /api/v1/pipelines/{id}/health`

`PipelineHealthContext` provides checks with a read-only snapshot of pipeline state. Constructor parameters are cheap (already-loaded objects); expensive operations like git commands and Kubernetes API queries use lazy properties that compute on first access and cache the result.

All check results are emitted to the EventBus as `system.health_check.*` events for observability. Results can also be persisted on `PhaseExecution` records via the `HealthCheckResultModel`.

See `orchestrator/health_checks/README.md` for the full framework reference, including how to add new checks.

**Pipeline health monitoring (two-tier):**

Building on the health check framework, a two-tier pipeline health monitoring system provides continuous, real-time failure detection and corrective action:

**Orchestrator tier (deterministic):** Processes structured agent progress events with configurable tripwire rules. Handles clear-cut failures instantly — heartbeat timeouts trigger escalation to the overseer/HITL, container exits trigger HITL escalation, repeated identical errors escalate to the overseer, message volume spikes trigger auto-throttling, and **infrastructure errors** (agents reporting `blocked` state with infrastructure-related blocker text like git failures, gateway errors, or permission denied) trigger critical alerts that the overseer routes directly to HITL, bypassing the nudge/redirect ladder. No LLM involvement. Nudge messages are only sent by the Tier 2 overseer after classifying the alert. **Phase-aware thresholds:** The health monitor tracks the current pipeline phase via `set_current_phase()` and uses a higher timeout during the implement phase (`orchestrator_implement_heartbeat_timeout_seconds`, default 600s vs. the standard 120s) to avoid false-positive stall alerts during deep implementation work. **BRC-idle suppression:** Agents correctly idle in BRC protocol (waiting for an upstream producer's proposal) are excluded from heartbeat and progress stall checks entirely. See `orchestrator/health_monitor.py`.

Agents emit structured progress via `POST /api/v1/pipelines/{id}/progress` (CLI: `egg-orch progress emit`). Events include step name, state (working/blocked/complete), detail text, and optional blocker description. The orchestrator stores events in-memory with configurable retention and evaluates them against tripwire thresholds from `PipelineConfig`.

**Overseer tier (LLM-powered):** A phase-scoped agent container (no code access) that handles ambiguous cases the deterministic tier can't resolve. Uses Haiku via `shared/egg_agent/` for lightweight classification (stall vs. legitimate work, loop detection, error triage including infrastructure error detection, off-track detection, cross-phase decision consistency) and Sonnet/Opus for corrective decision-making (composing redirect messages, deciding escalation level, filing diagnostic GitHub issues). Auto-spawned at the start of each pipeline phase when `overseer_enabled` is true, and torn down when the phase completes, advances, or fails — each phase gets a fresh instance with no accumulated state. The overseer runs with a configurable Agent SDK turn budget (`overseer_max_turns`, default 2000) rather than a hardcoded limit, ensuring it can sustain its continuous monitoring loop throughout long-running phases with active consensus negotiation. If the overseer exits before the current phase completes, the orchestrator's health monitor thread automatically respawns it (up to `overseer_max_respawns` times, default 3), gated by a `phase_overseer_active` flag to avoid respawning between phases. On respawn, the orchestrator captures the exited container's last 20 log lines (best-effort) and broadcasts an `OVERSEER_ALERT` message to the pipeline's message bus with diagnostic metadata (`exit_code`, `old_container_id`, `new_container_id`, `log_tail`, `respawn_attempt`, `max_respawns`), ensuring respawn events are visible to monitoring tools and the `/sdlc` session.

The overseer follows a corrective action ladder: auto-nudge → redirect message → agent restart → HITL escalation → phase restart (HITL) → GitHub issue filing → Slack notification. **Infrastructure errors bypass the ladder** — when the Tier 1 tripwire detects an infrastructure error (or the Tier 2 classifier identifies one), the decision maker fast-paths based on the error subcategory: transient errors (unresponsive, crashed, OOM, timeout, hung, not responding) trigger an automatic agent restart; persistent errors (permission, read-only file system, EROFS, certificate/config error/configuration invalid, misconfigured, authentication/authorization/credentials, disk full, no space left, quota exceeded) escalate directly to HITL. See `RESTARTABLE_PATTERNS` and `NON_RESTARTABLE_PATTERNS` in `orchestrator/overseer/decision_maker.py` for the full keyword lists. The deny-list takes priority — if both a restartable and a non-restartable keyword appear in the error, the error escalates to HITL to avoid restart loops on persistent failures. Cross-tier deduplication prevents duplicate escalations when both tiers detect the same error. The overseer can restart individual agents autonomously (up to 2 times per phase, preserving the agent's worktree); phase-level restarts require HITL approval.

**Host → overseer migration (issue [#1962](https://github.com/jwbron/egg/issues/1962)):** Stall / silent-agent / NACK / long-running-phase / stuck-pipeline rescue detection is being migrated out of the host-side `/sdlc` skill (`skills/sdlc/SKILL.md`) and into the overseer. Per-agent timing state moves from the skill's in-memory `{role: {phase, phase_entered_at, …}}` map into `.egg-state/oversight/agent-timing.json` (schema at `egg_overseer.state.AgentTimingState`; read/modify/write guarded by an `fcntl.LOCK_EX` flock so concurrent overseer respawns at phase boundaries cannot clobber each other). Thresholds become per-pipeline configurable via `PipelineConfig` (`overseer_agent_stall_seconds`, `overseer_silent_agent_threshold_seconds`, `overseer_nack_unresolved_seconds`, `overseer_long_running_phase_seconds`, `overseer_stuck_phase_transition_seconds`). The migration is gated by a calibration-window flag, `overseer_owns_host_detection: bool = False` — while `false` (the default), the host's detectors are the active source and the overseer's `run_migrated_detectors` early-returns with no alerts; flipping to `true` short-circuits the host's detection blocks (gated on the same flag in `skills/sdlc/SKILL.md`) and the overseer becomes the sole source. The semantic is "host XOR overseer", not "host AND overseer" — the calibration window is operator-driven (an operator opts a pipeline into `true` to validate parity, then opts back if needed). After a calibration window, a follow-up PR flips the default and deletes the now-dormant host blocks.

Concurrently, the overseer's decision tier gains an Opus 4.6 advisor (the [advisor strategy](https://claude.com/blog/the-advisor-strategy)) invoked through the sandbox CLI verb `egg-orch overseer consult-advisor` (handler `cmd_overseer_consult_advisor` at `sandbox/egg_lib/orch_cli.py`, forwards to `egg_overseer.advisor.consult_advisor`), only when Haiku flags an anomaly **and** a Tier-1 health alert is active simultaneously. The CLI verb runs the underlying `run_agent_async` Opus call inside the sandbox so it stays on the LLM-execution side of the EGG200 boundary documented in [agent-mode-design.md](../guides/agent-mode-design.md) — the orchestrator pod never holds Anthropic credentials. The advisor can return `decision="file_issue"` with a fully composed title + body; the overseer surfaces this via top-level `OVERSEER_ALERT.recommendation="file_issue"` + `recommendation_payload` (first-class optional fields on the `Message` envelope; `Message.to_dict()` omits them when unset so legacy `OVERSEER_ALERT` consumers see byte-identical JSON). The human gates the actual `gh issue create` through the existing HITL flow. See [Pipeline Health Monitoring → Host Detector Migration](../guides/pipeline-health-monitoring.md#host-detector-migration), [Pipeline Health Monitoring → Advisor Gate](../guides/pipeline-health-monitoring.md#advisor-gate), and [`sandbox/agent-config/rules/overseer.md`](../../sandbox/agent-config/rules/overseer.md) for the rule-doc rewrite.

See [Pipeline Health Monitoring Guide](../guides/pipeline-health-monitoring.md) for the full reference.

## Pipeline Modes

The orchestrator supports two pipeline modes:

- **`issue`** (default): Standard SDLC pipeline triggered by a GitHub issue. Progresses through refine → plan → implement phases with structured agent teams.
- **`babysit`**: One-off implement-phase BRC cycle against an existing PR, triggered via the `/babysit-pr` MCP skill with `mode=babysit` and `pr_number=N`. Runs the standard implement-phase machinery (role-typed coder + tester + documenter producers, `reviewer_code` reviewer, BRC consensus) on a staging branch rooted at the PR head; pushes a single final commit to the PR branch on consensus. Pipeline ID uses `pr-{N}` format. Skips refine and plan phases. See [Babysit-PR Guide](../guides/babysit-pr.md).

The `babysit` mode registers with the same orchestrator infrastructure (state store, health monitoring, HITL decision queue) as issue mode. Under the hood it is an implement-phase pipeline with `has_contract=false`, which filters `reviewer_contract` out of the role roster and carries no contract/plan artifacts. The cycle runs once per invocation — there is no polling loop; CI failures, if any, are observed and addressed by the producers as part of BRC orientation.

## Orchestrator-Only Jira Transitions (`/api/v1/jira/ticket/transition`) — #1557 decision-15

The Jira-epic SDLC pipelines introduced by [issue #1557](https://github.com/jwbron/egg/issues/1557) need to transition pre-existing child tickets to **Won't Do** when the reassess flow supersedes them (consolidations, obsoletes, replanned scopes). The agent-facing Jira gateway intentionally **forbids transitions** today (`gateway/jira_client.py:133` `JIRA_WRITE_VERBS_DENIED`), and the trust-boundary decision keeps it that way: there is no Jira state-machine surface available to in-sandbox agents.

Instead, transitions land via a **separate orchestrator-only gateway route**, `POST /api/v1/jira/ticket/transition`, gated on **loopback / cluster-internal source + launcher-secret bearer token**. The applier in the sandbox writes Won't-Do candidates to a handoff JSON (see `plugins/refine-plan/skills/refine-plan/agents/applier.md`'s "Out of scope: Won't-Do transitions" section). The orchestrator-side `_drain_wontdo_batch_after_apply` hook (`orchestrator/routes/pipelines.py`) reads the handoff after apply-phase BRC consensus and calls `/transition` once per entry via `orchestrator/wontdo_drain.py::run_wontdo_drain`, out of band from the HITL HTTP response so Jira API latency does not block operator approvals.

### Trust model

The route's auth combines **two gateway-side gates** with a **deployment-side gate** the cluster operator owns:

1. **Loopback / cluster-internal source (gateway).** The request's source IP must be loopback, link-local, or RFC1918. The gateway rejects external callers with HTTP 403 even when the bearer token is correct. **This is a coarse gate**: `_is_in_cluster_source` (`gateway/gateway.py`) accepts `is_loopback | is_private | is_link_local`, so on a standard k8s overlay every pod in `10.0.0.0/8` / `172.16.0.0/12` / `192.168.0.0/16` passes — including sandbox pods. The gate's actual security value is **excluding traffic from outside the cluster** (e.g. an attacker who steals the launcher secret but cannot reach the gateway's pod-internal listener); it does **not** by itself distinguish orchestrator pods from sandbox pods.
2. **Launcher-secret bearer token (gateway).** The request must carry `Authorization: Bearer <launcher_secret>`, where `<launcher_secret>` is the same secret used by every gateway session-creation flow. Constant-time compare via `secrets.compare_digest` against the value loaded by `get_launcher_secret()`. Missing or invalid bearer → HTTP 401 (`missing_bearer_auth` / `bad_bearer_auth`); secret not configured on the gateway → HTTP 401 with reason `launcher_secret_not_configured`. Implementation: `gateway/gateway.py::_verify_orchestrator_transition_auth`.
3. **NetworkPolicy / equivalent subnet scoping (operator-owned).** Because gate 1 only excludes external traffic, the cluster operator is responsible for restricting which **in-cluster** subnets can reach the gateway's `/transition` listener. The expected deployment uses a NetworkPolicy on the gateway pod accepting `/transition` ingress only from the orchestrator's pod selector — closing the gap where a sandbox with the launcher secret could otherwise reach the route. **Without NetworkPolicy, the launcher secret is the only barrier between a compromised sandbox and the `/transition` route** (and the agent-facing path's `JIRA_WRITE_VERBS_DENIED` blocks the underlying Jira transition verb even in that scenario — see "Sandbox isolation" below). Operators deploying without NetworkPolicy should treat the launcher secret with sandbox-grade rotation discipline.

In addition to the two gates, the route allowlists `transition_name` to `{"Won't Do", "Won't Fix"}` only — the orchestrator cannot use this route to drive arbitrary workflow transitions (e.g. `Done`, `In Progress`). Other transition names return HTTP 400. The audit log records caller IP, transition name, ticket key, and outcome on every invocation (`jira_ticket_transition` event for successes, `jira_ticket_transition_unauthorized` / `_rejected` / `_denied` / `_upstream_error` for the rejection paths).

The agent-facing Jira surface (`validate_jira_api_path` + `JIRA_WRITE_VERBS_DENIED`) is **unchanged** — sandbox agents continue to be denied transitions. The `/transition` route is reachable only from inside the cluster network with the launcher secret. See `gateway/jira_client.py:491+` for the four pre-existing internal-only Jira helpers that bypass `validate_jira_api_path`; `/transition` follows the same pattern.

The route is decorated manually with the `PRIVATE_MODE_MARKER_ATTR` so the `test_every_jira_route_has_private_mode_marker` regression test stays green; the standard `@require_private_mode` decorator can't be applied because it expects a session-auth context that this orchestrator-only path deliberately does not establish. See `gateway/gateway.py:5497-5510` for the manual stamp and the rationale comment.

### Launcher-secret reuse — why no separate orchestrator token

The original plan (TASK-2-6 / TASK-2-10 acceptance text) called for a new `X-Egg-Orchestrator-Token` header authenticated against a dedicated `EGG_ORCHESTRATOR_TOKEN` env var. The landed implementation reuses the **existing launcher secret** via the standard `Authorization: Bearer …` header instead. The deliberate trade-off:

- **Loopback gate excludes external traffic only.** The gateway-side IP check rejects external callers with HTTP 403 before bearer comparison, but `_is_in_cluster_source` accepts the full RFC1918 superset and does not distinguish orchestrator pods from sandbox pods. The actual orchestrator-vs-sandbox scoping comes from the operator-owned NetworkPolicy on the gateway pod; the loopback gate is necessary-but-not-sufficient.
- **One rotation pipeline, not two.** Operators already rotate the launcher secret on a quarterly cadence (or on incident). Adding a second secret with its own bundle key, mount path, and rotation runbook doubled the operational surface for a defense-in-depth gain that NetworkPolicy already supplies more cleanly.
- **Sandbox is denied by NetworkPolicy + the agent path's transition-verb deny, not by withholding the secret.** Sandbox pods already see the launcher secret on the standard agent-facing path. With NetworkPolicy in place, a sandbox copying the secret and calling `/transition` is blocked at the network layer. Without NetworkPolicy, the agent-facing routes still enforce `JIRA_WRITE_VERBS_DENIED` on the underlying Jira surface — but the `/transition` route itself becomes the single point of trust, so operators in that configuration should rotate the launcher secret aggressively.

If the cluster's NetworkPolicy is unavailable or weakens (e.g. flat L2 between sandbox and orchestrator subnets, shared NAT egress that obscures source IPs, or a managed environment that doesn't honor NetworkPolicy primitives), the trade-off should be revisited and a dedicated `EGG_ORCHESTRATOR_TOKEN` reintroduced. The route is structured so the second gate can be added without touching the loopback check or the allowlist — a follow-up issue would extend `_verify_orchestrator_transition_auth` to also require an `X-Egg-Orchestrator-Token` header.

### Launcher-secret lifecycle (refresher)

The launcher secret is the gateway's existing session-creation bearer. Its lifecycle is managed by the standard deployment flow:

#### Generation

The launcher secret is a high-entropy random string (≥ 32 bytes, base64url-encoded). It is generated **once per cluster deployment** and stored in the cluster secret bundle alongside the other gateway credentials.

```bash
# Generate a fresh secret (run on the cluster admin host, not in a pod):
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Pipe the output into the cluster secret manager — for self-hosted k8s, this is typically a `Secret` named `egg-launcher-credentials` in the `egg-system` namespace; for a managed secret store (HashiCorp Vault, AWS Secrets Manager, etc.) follow that operator's bundle convention. The secret is **never** written to the source tree, `CLAUDE.md`, or `.egg-state/`.

#### Mounting

The launcher secret is projected into both pods the same way:

- **Gateway pod**: file at `/secrets/launcher-secret` (canonical, read by `get_launcher_secret()` at startup), with `EGG_LAUNCHER_SECRET` env-var fallback. The gateway pins the value for the lifetime of the process; constant-time comparisons in `_verify_orchestrator_transition_auth` use the pinned value.
- **Orchestrator pod**: same — `orchestrator/wontdo_drain.py::_resolve_launcher_secret` reads `/secrets/launcher-secret` first and falls back to `EGG_LAUNCHER_SECRET`. The orchestrator attaches it as `Authorization: Bearer <launcher_secret>` on every outbound call from `_drain_wontdo_batch_after_apply` (and any future orchestrator-only routes added under the same trust model).

The k8s manifests for both pods reference the same `Secret` resource so a single rotation replaces the value cluster-wide.

#### Sandbox isolation

Sandbox / agent pods **do** receive the launcher secret — they need it to authenticate session-creation against the agent-facing routes. With the launcher secret in hand, what stops a compromised sandbox from calling `/transition` directly:

1. **NetworkPolicy on the gateway pod (operator-owned, primary defense).** The expected production deployment restricts `/transition` ingress to the orchestrator's pod selector via a Kubernetes NetworkPolicy (or equivalent CNI ACL). This is what actually denies sandbox subnets — the gateway-side `_is_in_cluster_source` only excludes external traffic; on a flat overlay every pod's source IP falls inside `is_loopback | is_private | is_link_local`, including sandboxes. **If NetworkPolicy is not enforced, a sandbox with the launcher secret CAN reach the route** (gate 2 below is the only remaining barrier).

   **Reference NetworkPolicy shape** (operator-deployable starting point — adapt labels to your cluster's selector scheme):

   ```yaml
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata:
     name: gateway-transition-orchestrator-only
     namespace: egg-system
   spec:
     podSelector:
       matchLabels:
         app: gateway
     policyTypes:
       - Ingress
     ingress:
       # Allow all pods in egg-system to reach the gateway on the
       # agent-facing port (9848) — unchanged from baseline.
       - from:
           - namespaceSelector:
               matchLabels:
                 kubernetes.io/metadata.name: egg-system
           ports:
             - port: 9848
       # /transition is on the same port as other gateway routes, so
       # NetworkPolicy cannot scope by URL path. The operator can:
       #   (a) move /transition to a separate listener on a different
       #       port and apply a tighter from: rule to that port, OR
       #   (b) keep /transition on the shared listener and rely on the
       #       launcher-secret bearer + loopback gates above. The egg
       #       reference deployment uses (b) — see "Launcher-secret
       #       reuse" for the rationale.
   ```

   Path-level scoping (option a) is the cleanest fit for the trust model documented above but requires a small gateway-side refactor to split listeners; option b matches the landed code. Operators who can't enforce NetworkPolicy at all (managed environments without it) should treat the launcher secret with sandbox-grade rotation discipline — see the rotation section below.

2. **The agent-facing path explicitly denies the `transition` verb.** Even with `/transition` reachable, the agent-facing Jira routes block the underlying transition verb via `JIRA_WRITE_VERBS_DENIED` — but note that the `/transition` route is the orchestrator-only escape hatch and does **not** go through `JIRA_WRITE_VERBS_DENIED`. The agent-path deny protects only the agent-facing `/jira/ticket/*` surface, not the orchestrator-only path. So in the no-NetworkPolicy configuration, the launcher secret + the loopback gate together are the effective trust boundary on the orchestrator-only route.

#### Rotation

To rotate the launcher secret:

1. Generate a new value using the procedure above.
2. Update the secret bundle (atomic write — both pods pick up the new value on next restart, not mid-flight).
3. Roll the gateway deployment first (`kubectl rollout restart deployment/gateway -n egg-system`). Until the orchestrator is rolled, in-flight orchestrator → gateway calls to `/transition` will see HTTP 401 because the orchestrator is still sending the old token. **This is the expected fail-closed behaviour** — `run_wontdo_drain` records the per-entry failure (`http_error_401`) and the drain hook flips the task to `jira_action_status='failed'` with the reason captured in `Task.notes`. Pending Won't-Dos are re-attempted on the next apply phase or via an operator-initiated re-drain.
4. Roll the orchestrator deployment (`kubectl rollout restart deployment/orchestrator -n egg-system`). The new secret comes online and pending Won't-Dos resolve on the next apply re-run.
5. Verify by triggering a synthetic Won't-Do (e.g. a test epic with a single obsolete child) and watching the gateway audit log for the `jira_ticket_transition` entry.

Rotation does **not** require draining the cluster or pausing pipelines. The 401-on-mismatch behaviour is by design — it is preferable to fail-closed and leave a recoverable signal on the contract than to fail-open by accepting an outdated secret. The window between the gateway and orchestrator restarts should be measured in seconds for typical k8s rolling restarts; longer windows degrade gracefully into deferred Won't-Dos.

Because the same secret authenticates every other gateway-facing call, rotation also rolls every active sandbox session — schedule rotations during a maintenance window when feasible. On a credential incident (suspected leak), rotate immediately and audit the gateway log for `/transition` invocations that pre-date the rotation timestamp.

#### Why agent-facing routes still deny transitions

Even though the same launcher secret authenticates both surfaces, transitions remain denied for the agent-facing Jira routes. The reasoning:

- **Blast radius.** The agent-facing path is reachable from every sandbox in the cluster with the launcher secret. Allowing transitions on the agent path widens the attack surface to "any sandbox", whereas the orchestrator-only `/transition` route is constrained by NetworkPolicy to the orchestrator's pod selector in the expected deployment shape (see "Sandbox isolation" — without NetworkPolicy the constraint degrades to "any in-cluster pod with the secret").
- **Allowlist scope.** The agent path's policy module (`gateway/jira_client.py::JIRA_WRITE_VERBS_DENIED`) explicitly denies the `transition` verb because Jira's transition surface is a state-machine API — allowing arbitrary transition names from sandbox would mean re-implementing Jira's workflow guards on the gateway side. The orchestrator-only path narrows transitions to a `{Won't Do, Won't Fix}` allowlist, policy that can be inspected and audited without modelling Jira's full state machine.
- **Audit symmetry.** Every `/transition` call carries the ticket key and transition name in the audit-log payload (`jira_ticket_transition` event), and the orchestrator-side caller pins the pipeline context. The agent-facing path has no such correlation surface (sandbox calls are pipeline-scoped only via worktree path, which doesn't reach the gateway audit layer).

See `plugins/refine-plan/skills/refine-plan/agents/applier.md`'s "Out of scope: Won't-Do transitions" for the sandbox-side counterpart: the applier emits a handoff JSON and never attempts to call `/transition` directly.

### Cross-references

- Gateway-side route definition + audit log shape: `gateway/gateway.py` (search for `transition`); see also `gateway/README.md` for the deployment-time secret bundle layout.
- Sandbox-side Won't-Do handoff producer: `plugins/refine-plan/skills/refine-plan/agents/applier.md` (sections "Out of scope: Won't-Do transitions" and "In-flight refusal").
- Orchestrator-side drain helper: `orchestrator/wontdo_drain.py::{load_wontdo_handoff, run_wontdo_drain}`.
- Orchestrator-side drain hook: `orchestrator/routes/pipelines.py::_drain_wontdo_batch_after_apply` — invoked from both the auto-advance and HITL-resolution apply-phase exit paths; writes per-Task `jira_action_status` back via the `on_entry_result` callback.
- Issue-level decision record: [#1557 decision-15](https://github.com/jwbron/egg/issues/1557) (trust-boundary for Jira transitions).

## Network Mode

Pipelines can specify an explicit network mode that controls internet access for spawned containers:

- **`public`**: Full internet access
- **`private`**: Network lockdown - Anthropic API + private GitHub repos only (enforced by gateway proxy)
- **`None`** (auto): Auto-detected from repo visibility (see below)

**Setting network mode:**

- Via `egg-sdlc --private`: Sets `EGG_PRIVATE_MODE=true` environment variable, which `egg-sdlc` detects and passes as `network_mode="private"` when creating the pipeline
- Via `egg-orch pipeline create --network-mode <public|private>`: Explicitly sets the pipeline's network mode
- Via orchestrator API: Include `"network_mode": "public"|"private"` in the pipeline creation request body

**How it works:**

1. Network mode is stored in the pipeline model (`orchestrator/models.py:Pipeline.network_mode`)
2. The resolved mode applies to:
   - Spawned agent containers (network isolation)
   - The orchestrator's own gateway sessions (git push/fetch, branch deletion, PR creation)
3. Resolution logic:
   - If `network_mode` is explicitly set, use it
   - If not set but a repo is associated, query the gateway for repo visibility (`GatewayClient.get_repo_visibility()`)
   - Map visibility: private/internal repos → `"private"` mode, public repos → `"public"` mode
   - If no repo is associated, default to `"public"`
4. The gateway enforces network policy based on the session mode (see `gateway/README.md`)

**Special case: PR phase**

The PR phase no longer spawns an agent. Instead, the orchestrator auto-creates the PR via `GatewayClient.create_pr()`, which:
1. Extracts PR title/description from the contract's `pr` field (populated by the plan agent)
2. Falls back to the issue title or pipeline ID if no PR metadata exists
3. Appends git commit log, diff stats, and a **Pipeline Context** section (pipeline ID + issue number) to the PR body
4. Creates the PR via the gateway using a temporary session with `phase="pr"` permissions and the pipeline's resolved network mode; in `private` mode the PR is created as a draft
5. Applies `egg` and `agent:orchestrator` labels to the newly created PR

The gateway also injects an `<!-- egg-pipeline-context ... -->` HTML comment into the PR body containing machine-parseable pipeline metadata (`pipeline_id`, `agent_role`, `issue`). Labels are applied best-effort — failures are logged but non-fatal.

This eliminates the need for agent interaction during PR creation and ensures consistent PR formatting across all pipelines.

### Pipeline state writeback after auto-PR creation

After a successful auto-PR creation, `_finalize_pr_phase_failed` (in `orchestrator/routes/pipelines.py`) writes both `pipeline.pr_number` and `pipeline.pr_head_sha` alongside the existing `phases["pr"].artifacts["pr_url"]` entry, all inside the same `get_pipeline_state_lock → reload → save` transaction:

- `pr_number` is parsed from the `pr_url` via `re.search(r"/pull/(\d+)", pr_url)`. It is always populated when the URL has the expected shape.
- `pr_head_sha` is fetched via `_fetch_pr_state(pr_number, pipeline.repo)` (which shells out to `gh pr view`). It is assigned only when the returned value matches the `[0-9a-f]{7,40}` hex-SHA pattern (guarded explicitly in `_finalize_pr_phase_failed` before assignment). If `_fetch_pr_state` returns an empty dict — e.g., `gh` is unavailable, or the PR is not yet propagated — `pr_head_sha` is left `None` and the PR phase still succeeds (graceful degradation).

Issue-mode consumers (overseer stall detector, `get_pipeline_snapshot`, babysit-worker handoffs) can read `pipeline.pr_number` directly without falling back to `gh pr list` or parsing the `pr_url` artifact. These fields were added in response to issue #1911, where stale `pr_number` / `pr_head_sha` on successful runs drove false-positive `post-consensus-push-stall` alerts in the overseer.

### Per-agent commit SHA diagnostics

`_update_agents_complete` populates each `completed_agents[].commit` field from `_brc.get_proposal_commit_sha(role)`. When the BRC tracker returns `None` or the `"RECONSTRUCTED_NO_SHA"` sentinel, a structured `logger.warning("BRC tracker returned no commit sha for completed agent", pipeline_id=..., phase=..., role=..., brc_value=...)` is emitted so the missing writeback can be investigated. This is diagnostic only — there is deliberately no auto-fallback to a guessed SHA, since that would mask the underlying wiring gap.

## Context PR (slice-aware mode, [#2548](https://github.com/jwbron/egg/issues/2548))

After the plan phase completes and the plan_gate is approved, the orchestrator opens a **doc-only Context PR** before any slice spawns. The Context PR establishes the program — analysis + plan + refine/plan BRC consensus — so reviewers approaching any slice PR see the strategic context that produced it. Mechanics:

1. The orchestrator creates `egg/<id>/context` from the pipeline's configured `base_branch` (NOT hardcoded to `main`).
2. It commits these refine/plan artifacts to that branch:
   - `.egg-state/drafts/<id>-analysis.md` (refine output)
   - `.egg-state/drafts/<id>-plan.md` (plan output, with full `yaml-tasks` block)
   - `.egg-state/brc-history/<id>-refine.{md,json}` (refine BRC consensus record)
   - `.egg-state/brc-history/<id>-plan.{md,json}` (plan BRC consensus record)
   - `.egg-state/agent-outputs/<id>-refine-*.{md,json}` and `<id>-plan-*.{md,json}` (per-phase agent transcripts — included for transparency, HITL Q3)
3. It opens the PR against the configured base branch using `contract.pr.context_title` and `contract.pr.context_description` (distinct from `pr.title` / `pr.description`, which are per-slice).
4. The PR is **doc-only auto-open** (HITL decision-3): the orchestrator opens it, humans review on the PR, and the pipeline does **not** block on its merge before slicing begins.
5. Slice-1's `parent_branch` resolves to `egg/<id>/context` (rather than `egg/<id>/work`); slice-N>1 stacks on its predecessor as before. The [stacked-PR rebase reconciler](slice-dag.md#stacked-pr-rebase-reconciler) prefers the context branch over `pipeline_branch` as a last-resort fallback when retargeting orphaned children.

The context-PR rollout is a **hard switchover** (HITL decision-4) — there is no backwards-compat shim or feature flag, and in-flight pipelines are not backfilled.

The contract's `pr` field (`PRMetadata`, `shared/egg_contracts/models.py`) carries four `pr.context_*` fields introduced in schema 1.1 (#2548 — pre-1.1 contracts auto-promote on load):

| Field | Author | Description |
|-------|--------|-------------|
| `pr.context_title` | Planner | Title for the Context PR (program-level framing). |
| `pr.context_description` | Planner | Body for the Context PR (program-level narrative). |
| `pr.context_branch` | Orchestrator | Branch name (`egg/<id>/context`) — populated when the orchestrator creates it. |
| `pr.context_pr_number` | Orchestrator | GitHub PR number — populated when the PR is opened. |

These coexist with the existing `pr.title` / `pr.description` (per-slice) and `pr.test_plan` / `pr.manual_steps` / `pr.deferred_actions` fields. See the [Concurrent Execution Slice PR Stack section](../guides/concurrent-execution.md#slice-pr-stack) for the end-to-end stack shape and reviewer flow.

## BRC-history file naming

Refine and plan phases continue to emit a single per-phase aggregate file:

| Phase | File pattern |
|-------|--------------|
| `refine` | `.egg-state/brc-history/<id>-refine.{md,json}` |
| `plan` | `.egg-state/brc-history/<id>-plan.{md,json}` |

The implement phase splits per slice in slice-aware mode:

| Mode | File pattern |
|------|--------------|
| Slice-aware (issue-mode pipelines with `contract.slices`, #2548 hard switchover) | `.egg-state/brc-history/<id>-implement-slice-<N>.{md,json}` (one per slice) plus `<id>-implement-unattributed.{md,json}` for cross-cutting messages without canonical slice scope (HEARTBEAT, OVERSEER_ALERT, AGENT_FAILED, …). The aggregate `<id>-implement.{md,json}` file used by non-slice runs is **not** produced in slice-aware mode — slice-aware pipelines partition the implement-phase BRC history into per-slice + unattributed files instead. |
| Non-slice (babysit-pr; override pipelines without `contract.slices`) | A single content-addressed file: `pr-<N>-<short-sha>-implement.{md,json}` for babysit-pr; `<id>-implement.{md,json}` for non-slice override runs. The identifier shape is what differs — babysit cycles never partition into slices. |

The orchestrator commits each `<id>-implement-slice-<N>.{md,json}` to the slice integration branch as a final orchestrator-authored commit before the slice PR is opened. This is necessary because the `coder` and `tester` role boundaries forbid pushes under `.egg-state/brc-history/`; the existing `_commit_statefiles_to_worktree` pattern keeps history persistence deterministic. See [Concurrent Execution: BRC History Link in PR Body](../guides/concurrent-execution.md#brc-history-link-in-pr-body) for the link-line behaviour rendered into auto-generated PR bodies.

## Per-Pipeline Worktrees

The orchestrator reads pipeline artifacts (verdict files, draft documents, check results) from per-pipeline worktrees created by the gateway. These worktrees isolate work for each pipeline and are separate from both the orchestrator's state worktree and the main repository working directory.

**Architecture:**
- Gateway creates worktrees at `/home/egg/.egg-worktrees/{job-name}/{repo-name}/` (one per agent)
- Each agent pod mounts its own worktree via hostPath and writes artifacts to it
- All agents in a pipeline push to the same shared branch (e.g., `egg/issue-{N}/work` since #2399; babysit-pr uses the existing PR head branch)
- Orchestrator mounts `/home/egg/.egg-worktrees` and reads artifacts from pipeline-specific paths
- Worktree paths are resolved dynamically based on Job name and repository

> **Changed in issue #1481:** Previously, all agents in a pipeline shared a single worktree (keyed by `pipeline_id`). Now each agent gets its own isolated worktree (keyed by Job name). This prevents agents from overwriting each other's uncommitted work and ensures clean `git status` per agent.

**Key artifact files in worktrees:**
- `.egg-state/contracts/{identifier}.json` — Contract state (issue number for issue-driven pipelines, pipeline ID for prompt-driven pipelines)
- `.egg-state/drafts/{identifier}-analysis.md` — Draft for `refine` phase (special-cased to `analysis`)
- `.egg-state/drafts/{identifier}-{phase}.md` — Draft for other phases (e.g., `plan`). No draft for `implement` phase.
- `.egg-state/reviews/{identifier}-{phase}-{reviewer_type}-review.json` — Review verdict files
- `.egg-state/agent-outputs/{identifier}-{role}-output.json` — Agent handoff data (e.g., `871-coder-output.json`). Falls back to `{role}-output.json` for backward compatibility.
- `.egg-state/checks/{identifier}-implement-results.json` — *(Deprecated)* Previously written by the checker role. The checker has been absorbed into the tester, which reports results via its handoff output instead.

**Draft path resolution with generic fallback:**

When reading draft files (e.g., at phase gates), `_read_phase_draft` resolves the path in two steps:

1. **Issue-specific path** (primary): `.egg-state/drafts/{identifier}-{type}.md` (e.g., `1553-analysis.md`)
2. **Generic path** (fallback): `.egg-state/drafts/{type}.md` (e.g., `analysis.md`)

The primary issue-specific path is always tried first. If the file is not found, the generic unprefixed path is tried as a fallback. This handles edge cases where the worktree sync didn't bring the issue-specific file into the local worktree, or where agents wrote to the generic path.

> **Note:** The write path (`_get_draft_path`) always returns the issue-specific path — agents always write to the canonical prefixed location. Only the read path has fallback behavior. Additionally, `_cleanup_stale_generic_drafts` removes unprefixed `analysis.md`/`plan.md` at pipeline start, so the generic fallback only helps when stale files persist from prior runs or agents wrote to the wrong path.
>
> **Changed in issue #1575:** Previously, `_read_phase_draft` only checked the issue-specific path. If the file was missing, it returned `None` and the phase gate displayed "No draft was found on the work branch" even when a generic draft existed.

**Volume mounts:**
- Orchestrator: hostPath from `${HOST_HOME}/.egg-worktrees` to `/home/egg/.egg-worktrees` (read agent-written artifacts)
- Integration tests: Dedicated test namespace with per-run worktree setup

**Phase-based readonly mounts:**

During the `implement` phase, certain `.egg-state/` subdirectories are mounted readonly into agent pods to prevent direct filesystem modifications to plan/contract artifacts:

| Directory | Implement phase | Refine/Plan phases |
|-----------|----------------|-------------------|
| `.egg-state/contracts/` | Readonly | Writable |
| `.egg-state/drafts/` | Readonly | Writable |
| `.egg-state/pipelines/` | Readonly | Writable |
| `.egg-state/reviews/` | Readonly (except reviewers) | Writable |

**Reviewer exemption**: Reviewer agents (roles starting with `reviewer`, e.g., `reviewer_code`, `reviewer_contract`) are exempted from the `.egg-state/reviews/` readonly mount because they need to write verdict files to that directory. Other implement phase agents (coder, tester, documenter) still have readonly access.

The orchestrator calls `ensure_egg_state_dirs()` before spawning containers to create the required directories (bind mounts require existing source paths) and place `.egg-readonly` marker files explaining the restriction and current phase. Reviewer agents do not receive the `.egg-readonly` marker in the `reviews/` directory. Then `phase_readonly_mounts()` generates the readonly `MountSpec` entries, which are added alongside the existing `.git` shadow mounts. Only directories that exist on the host are mounted (missing directories are skipped). See `shared/egg_container/__init__.py` and `orchestrator/container_spawner.py`.

**Host path translation:** The gateway returns worktree paths relative to the host (e.g., `/home/user/.egg-worktrees/...`), but the orchestrator pod only sees these via `/home/egg/...` hostPath mounts. The spawner uses the `HOST_HOME` env var to translate host paths to orchestrator-accessible local paths for `is_dir()` checks and `ensure_egg_state_dirs()`. hostPath mount sources still use the original host paths unchanged.

**Worktree state synchronization:** The orchestrator maintains bidirectional synchronization between local worktree branches and their remote counterparts:

1. **Push to remote** (outbound): The orchestrator pushes worktree contents (including `.egg-state/` files) to the remote branch at key pipeline checkpoints — after contract initialization, after phase completion, and on pipeline failure. This ensures agents always see the latest statefiles without working on unpushed changes. Pushes use `GatewayClient.push_worktree_branch()`, which authenticates with the launcher secret (orchestrator-trusted), bypassing the agent-targeted pipeline-push enforcement. They are logged as warnings on failure (non-blocking).

2. **Fetch from remote** (inbound): Before starting pipeline phases, the orchestrator syncs the local worktree with the remote branch via `_sync_worktree_with_remote()`. This handles orchestrator restarts and phase boundaries where the local worktree branch lags behind origin: commits pushed by agents in previous phases (contracts, drafts, statefiles) exist on the remote but not in the local checkout. The function performs a gateway-authenticated fetch (`GatewayClient.fetch_worktree_branch()`), then reconciles divergence (rebasing local commits) or resets the local branch to `origin/<branch>` depending on the case described below. The sync behavior depends on the prior phase's outcome:

   - **Prior phase succeeded, local ahead:** Local commits are pushed to remote first, preserving completed work. After a successful push the local branch already matches `origin/<branch>`, so no further reset is needed; on push failure the worktree falls through to a reset against `origin/<branch>`, discarding the unpushed commits.
   - **Prior phase failed, local ahead:** Local commits are discarded and the worktree is reset to remote (removes incomplete work from a failed/killed agent).
   - **Diverged (local and remote both have unique commits):** Local commits are rebased onto `origin/<branch>` via `_rebase_with_agent_output_autoresolve` (the same helper used by the gateway-side push-reject reconcile path). If the rebase fails, the error is logged and the function returns — the caller should check logs. When `base_branch` is `None`, the helper falls back to the bare `git rebase origin/<branch>` form and a warning is emitted (contamination risk per #2222).
   - **Local behind remote:** Standard reset to remote tip.
   - **Already in-sync:** Early return — the reset is skipped since the worktree is already up to date.

   The operation is best-effort and idempotent — it skips gracefully when the remote branch doesn't yet exist (first pipeline run) or when fetch fails.

3. **Cleanup on deletion**: When a pipeline is deleted, the orchestrator cleans up remote worktree branches (`egg/{container_id}/work`) for all containers across all phases using `GatewayClient.delete_remote_branch()`.
   This prevents accumulation of dangling branches on the remote.
   Deletion authenticates with the launcher secret (orchestrator-trusted) via the same `_do_push` path as outbound pushes, bypassing the agent-targeted pipeline-push enforcement introduced in #2028 (fix #2055). Branches already absent on the remote count as success. Other failures are logged as warnings but do not block pipeline removal.

See `orchestrator/routes/pipelines.py` for implementation details.

4. **Agent-initiated sync (on review)**: During concurrent phases, each agent's worktree is frozen at the phase-start SHA. When a producer pushes commits and proposes via `CONSENSUS_PROPOSE`, reviewer worktrees do not automatically receive those commits. To address this, the BRC preamble (`_build_brc_preamble()`) instructs reviewers to sync their worktree before reviewing: `git fetch origin && git merge origin/{branch} --no-edit`. This prompt-level approach avoids orchestrator-side worktree manipulation while ensuring reviewers evaluate up-to-date code. See [Concurrent Execution: Reviewer Worktree Sync](../guides/concurrent-execution.md#reviewer-worktree-sync) for details.

This architecture ensures the orchestrator reads artifacts from the correct isolated workspace rather than the main repository, preventing cross-contamination between pipelines.

## Multi-Agent Roles

The orchestrator coordinates specialized agent roles across pipeline phases. Each role runs in its own agent pod (k8s Job) with scoped permissions enforced by the gateway.

### Refine Phase Roles

| Role | Responsibility |
|------|----------------|
| **Refiner** | Analyze task, research codebase, evaluate options, recommend approach |

**Execution model**: Refiner runs first, then reviewers validate the analysis before human approval.

**Reviewers:**
- **Refine Reviewer**: Analysis quality and completeness
- **Agent Design Reviewer**: Agent-mode alignment and anti-patterns

### Plan Phase Roles

| Role | Responsibility |
|------|----------------|
| **Architect** | Analyze task, research codebase, recommend approach |
| **Task Planner** | Break work into phases and discrete tasks with acceptance criteria |
| **Risk Analyst** | Identify technical risks, propose mitigation strategies |

**Execution model**: Architect runs first, then task planner and risk analyst run in parallel (both depend on architect's analysis).

**Reviewers:**
- **Plan Reviewer**: Plan quality, task breakdown, dependencies, test strategy, alignment with analysis

### Implement Phase Roles

| Role | Responsibility |
|------|----------------|
| **Coder** | Write code, create commits, push branches |
| **Tester** | Find gaps in implementation, write and run tests, run linters/type checkers, apply auto-fixes |
| **Documenter** | Update docs and READMEs |
| **Reviewer (Code)** | Security, correctness, code quality, testing, documentation. Reviews every changed file systematically and emits a single CRITICAL ACK / NACK on the full diff. |
| **Reviewer (Code Holistic)** | Single-pass cross-module coherence review ([#2126](https://github.com/jwbron/egg/issues/2126)) — runs alongside Reviewer (Code) and gates consensus independently on architectural-coherence findings. |
| **Reviewer (Contract)** | Verify acceptance criteria met, task completion status |
| **Reviewer (Security)** | Security-lens review focused on cross-file allowlist mismatches, handler-vs-validator path mismatches, uncommitted-artifact / Dockerfile-symlink mismatches, secret leakage, and cross-file OWASP top-10 patterns. Criteria: [`shared/prompts/security-review-criteria.md`](../../shared/prompts/security-review-criteria.md). CRITICAL — a NACK blocks consensus ([#2139](https://github.com/jwbron/egg/issues/2139)). |
| **Reviewer (Concurrency)** | Concurrency-lens review focused on race conditions, deadlocks, shared-state mutation, retry storms, resource-cleanup ordering, and BRC-protocol invariants. Criteria: [`shared/prompts/concurrency-review-criteria.md`](../../shared/prompts/concurrency-review-criteria.md). CRITICAL — same as Reviewer (Security) ([#2139](https://github.com/jwbron/egg/issues/2139)). |

**Execution model**: All implement phase agents run concurrently via the BRC consensus protocol. Agents communicate via the orchestrator message bus and reach phase completion through peer consensus.

### Prompt Context Scoping

Agent prompts are scoped to role-relevant context. Analysis roles (architect, task_planner, risk_analyst) receive the full issue body for problem understanding. Execution roles (tester, documenter) receive a summarized background with pointers to full context on demand. Reviewer prompts include the full changeset diff command (`git diff origin/{base_branch}...HEAD`) using the pipeline's base branch, ensuring reviewers see the complete set of changes rather than an arbitrary truncated window. The `base_branch` and pipeline `branch` are threaded through the prompt-building call chain (`_run_concurrent_phase` → `_build_agent_prompt` → `_build_review_prompt` / `_build_brc_preamble`). On BRC `review_cycle > 1`, `_build_review_prompt()` switches to a delta command — `git fetch origin {base_branch}` + `git log {last_reviewed_commit}..HEAD --not origin/{base_branch} -p` — so the reviewer sees only PR-side commits pushed since the last review and not commits pulled in via a base-branch merge ([#1758](https://github.com/jwbron/egg/issues/1758)). See [SDLC Pipeline Guide: Role-Specific Prompt Context](../guides/sdlc-pipeline.md#role-specific-prompt-context) for details.

## Deployment Modes

egg supports three deployment modes, each suited to different use cases:

### 1. Local Mode (Interactive)

```
┌─────────────────────────────────────────────────────────┐
│                    Host Machine                         │
│  ┌────────────────┐    ┌────────────────────────────┐  │
│  │    Gateway     │◄───│      Sandbox               │  │
│  │    Sidecar     │    │  (interactive Claude Code) │  │
│  │                │───►│                            │  │
│  │  - Proxy       │    │  - No credentials          │  │
│  │  - Policy      │    │  - Network isolated        │  │
│  │  - Credentials │    │                            │  │
│  └────────────────┘    └────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Characteristics:**
- Single sandbox container with interactive Claude Code session
- Gateway sidecar provides proxy and policy enforcement
- No orchestrator component needed
- User interacts directly via terminal

**Use case:** Local development, ad-hoc tasks, learning/experimentation

**Environment:**
```bash
# No orchestrator-specific env vars
EGG_ORCHESTRATOR_MODE=local  # (default, can be omitted)
```

### 2. Remote-Single Mode

```
┌─────────────────────────────────────────────────────────┐
│                    Host Machine                         │
│  ┌────────────┐    ┌─────────────┐    ┌─────────────┐  │
│  │Orchestrator│───►│   Gateway   │───►│   Sandbox   │  │
│  │            │    │   Sidecar   │    │  (Claude)   │  │
│  │ - Pipeline │◄───│             │◄───│             │  │
│  │ - State    │    │ - Proxy     │    │ - Signals   │  │
│  │ - HITL     │    │ - Policy    │    │   back      │  │
│  └────────────┘    └─────────────┘    └─────────────┘  │
│        │                                                │
│        │ Webhooks                                       │
│        ▼                                                │
│  ┌────────────┐                                         │
│  │  GitHub    │                                         │
│  │  (Issues,  │                                         │
│  │   PRs)     │                                         │
│  └────────────┘                                         │
└─────────────────────────────────────────────────────────┘
```

**Characteristics:**
- Orchestrator spawns and monitors single sandbox
- Sandbox signals completion/progress back to orchestrator
- Pipeline state persisted locally
- GitHub webhooks drive pipeline transitions

**Use case:** Self-hosted CI/CD integration, single-task automation

**Environment:**
```bash
EGG_ORCHESTRATOR_MODE=remote-single
EGG_ORCHESTRATOR_URL=http://orchestrator.egg-system.svc.cluster.local:9849
EGG_PIPELINE_ID=issue-123
EGG_AGENT_ROLE=coder
```

### 3. Distributed Mode

```
┌─────────────────────────────────────────────────────────┐
│                    Host Machine                         │
│  ┌────────────┐    ┌─────────────┐                     │
│  │Orchestrator│───►│   Gateway   │                     │
│  │            │    │   Sidecar   │                     │
│  │ - Pipeline │◄───│             │                     │
│  │ - Dispatch │    │             │                     │
│  │ - Handoffs │    │             │                     │
│  └────────────┘    └──────┬──────┘                     │
│        │                  │                             │
│        │    ┌─────────────┼─────────────┐              │
│        │    │             │             │              │
│        ▼    ▼             ▼             ▼              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │ Sandbox  │  │ Sandbox  │  │ Sandbox  │             │
│  │ (Coder)  │  │ (Tester) │  │(Docmter) │             │
│  │          │  │          │  │          │             │
│  │ Signals  │  │ Signals  │  │ Signals  │             │
│  │   back   │  │   back   │  │   back   │             │
│  └──────────┘  └──────────┘  └──────────┘             │
└─────────────────────────────────────────────────────────┘
```

**Characteristics:**
- Orchestrator spawns multiple sandboxes with different agent roles
- Dependency-based scheduling (coder → tester → documenter)
- Handoff data passed between agents
- Parallel execution of independent agents
**Use case:** Multi-agent workflows, complex implementations. All phases use concurrent BRC execution.

**Environment:**
```bash
EGG_ORCHESTRATOR_MODE=distributed
EGG_ORCHESTRATOR_URL=http://orchestrator.egg-system.svc.cluster.local:9849
EGG_PIPELINE_ID=issue-123
EGG_AGENT_ROLE=coder  # or tester, documenter
```

## Component Interaction

### Network Architecture

All components communicate over Kubernetes networking with namespace-based isolation enforced by Calico NetworkPolicies:

| Namespace | Purpose | Components |
|-----------|---------|------------|
| `egg-system` | Core services | Gateway (Deployment + Service), Orchestrator (Deployment + Service) |
| `egg-agents` | Agent execution | Agent Jobs (one per agent role per pipeline) |

Service endpoints:
- Gateway: `gateway.egg-system.svc.cluster.local` (ports 9848, 3129, 9851)
- Orchestrator: `orchestrator.egg-system.svc.cluster.local` (port 9849)
- Agent pods: Addressed by label selector (`pipeline-id`, `agent-role`)

NetworkPolicies (enforced by Calico CNI):
- Default-deny all ingress in `egg-agents` — agents cannot receive unsolicited traffic
- Default-deny all egress in `egg-agents` — agents cannot reach internet directly
- Allow agent egress to gateway Service only — preserves the gateway-as-single-choke-point model
- Allow orchestrator ingress to agents — for health checks and log retrieval

> **Migration note:** This replaces the Docker dual-network model (`egg-isolated` + `egg-external` with fixed IPs). See [Kubernetes Migration](kubernetes-migration.md) for details.

### API Endpoints

**Gateway (`/api/v1/`)**
- `GET /health` - Health check (includes orchestrator connectivity)
- `POST /git/*` - Policy-enforced git operations
- `POST /gh/*` - Policy-enforced GitHub CLI operations

**Orchestrator (`/api/v1/`)**
- `GET /health` - Health check
- `GET/POST /pipelines` - Pipeline CRUD (list, create). Creating a pipeline whose existing record is in a terminal state (failed, cancelled, or complete) automatically replaces it, enabling resubmission without a prior delete
- `PATCH /pipelines/{id}` - Update pipeline. When status is set to `cancelled` or `failed`, pending decisions are cancelled and agent records are marked terminated synchronously; container/worktree cleanup runs asynchronously in a background thread. Response includes `cleanup_pending: true` to signal that teardown is still in progress
- `DELETE /pipelines/{id}` - Delete pipeline (stops containers, cleans up remote worktree branches best-effort, removes state). Acts as a safety net for any containers not yet removed by the PATCH handler's background cleanup
- `POST /pipelines/{id}/start` - Start or restart a pipeline (restarts failed pipelines by resetting the failed phase; recovers orphaned AWAITING_HUMAN pipelines by parsing the latest phase_gate resolution and either advancing to next phase or resetting for re-run; worktrees are preserved across restarts)
- `GET /pipelines/{id}/visualization` - Pipeline status snapshot (JSON, text, or ASCII)
- `GET /pipelines/{id}/stream` - Real-time SSE stream for single pipeline events and visualization
- `GET /pipelines/stream` - Unified SSE stream for all active pipelines (supports `?ascii=true`, `?active_only=false`, `?full_dag=true`)
- `POST /pipelines/{id}/signal` - Sandbox signals (complete, progress, error, readiness)
- `GET|POST /pipelines/{id}/messages` - Inter-agent message bus (send/poll; concurrent mode)
- `GET /pipelines/{id}/messages/status` - Message bus statistics (concurrent mode)
- `GET /pipelines/{id}/decisions` - HITL decision queue
- `GET /pipelines/{id}/health` - On-demand pipeline health check (all tiers)
- `GET /pipelines/{id}/health/alerts` - List active health alerts
- `GET /pipelines/{id}/progress` - Query structured progress events
- `POST /pipelines/{id}/progress` - Emit a structured progress event (CLI: `egg-orch progress emit`)
- `POST /pipelines/{id}/phase/populate-contract` - Populate contract from plan artifacts (parses yaml-tasks from plan draft into contract phases/tasks)

**Anchors (`/api/v1/anchors/`)**
- `POST /anchors/{agent_id}` - Create or update an agent anchor (stored in Redis; validated against schema)
- `GET /anchors/{agent_id}` - Get an agent's anchor (cross-agent reads via API)
- `DELETE /anchors/{agent_id}` - Delete an anchor
- `GET /anchors/team/{pipeline_id}` - Get team anchor (orchestrator-generated projection of all agent anchors)
- `POST /anchors/gc/{pipeline_id}` - Garbage-collect anchors for a completed/failed pipeline

**MCP Server (`/mcp`)**
- `GET /health` - MCP server health check
- `POST /mcp` - Streamable HTTP transport endpoint (MCP protocol via JSON-RPC)

Available MCP tools (orchestrator-backed): `submit_task`, `get_status`, `provide_input`, `list_tasks`, `cancel_task`, `check_health`, `list_containers`, `get_container_logs`, `send_message`, `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`, `get_contract`, `validate_config`, `restart_agent`, `restart_phase`, `advance_phase`, `start_phase`, `complete_phase`, `populate_contract`

Blocking host-side waits run via the `egg-orch pipeline wait-status` Bash CLI rather than an MCP tool (issue [#2211](https://github.com/jwbron/egg/issues/2211)). The CLI loops the orchestrator's `/api/v1/pipelines/<id>/status/wait` route server-side and emits one JSON-line per pipeline-relevant event. See [Host-Side Waits](../reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) for the envelope, exit-code contract, and cursor protocol. The route itself stays — the CLI is a wrapper.

Available MCP tools (gateway-backed, requires `gateway_url`): `list_checkpoints`, `search_checkpoints`

The gateway-backed checkpoint tools (`list_checkpoints`, `search_checkpoints`) accept an optional `repo` parameter to specify the checkpoint repository in `owner/repo` format (e.g., `owner/repo-checkpoints`). When provided, this is forwarded as the `source_repo` query parameter to the gateway checkpoint endpoint.

**CLI Access:**
The `egg-orch` CLI (`sandbox/bin/egg-orch`) provides command-line access to all orchestrator API endpoints. Available in sandbox containers for agent use, or can be run from the host with appropriate environment variables. See the [README CLI Reference](../../README.md#egg-orch-cli) for command details.

### Signal Flow

1. **Orchestrator → Sandbox**: Container spawn with env vars
2. **Sandbox → Orchestrator**: Signal on completion/error
3. **Gateway → Orchestrator**: Health check (optional)
4. **Orchestrator → GitHub**: Webhook responses, PR updates

## Sandbox Lifecycle

### Orchestrator Mode Detection

The sandbox detects orchestrator mode via environment:

```python
# Detection priority:
# 1. Explicit mode: EGG_ORCHESTRATOR_MODE=remote-single|distributed
# 2. Implicit: EGG_PIPELINE_ID is set
# 3. Implicit: EGG_ORCHESTRATOR_URL is set
```

### Completion Signaling

On exit, orchestrator-managed sandboxes signal completion:

```python
# Success (exit code 0)
POST /api/v1/pipelines/{pipeline_id}/signal
{
    "signal_type": "complete",
    "agent_role": "coder",
    "commit": "abc1234",
    "files_changed": ["src/main.py"]
}

# Failure (non-zero exit)
POST /api/v1/pipelines/{pipeline_id}/signal
{
    "signal_type": "error",
    "agent_role": "coder",
    "error": "Container exited with code 1",
    "recoverable": false
}
```

## Shared Package

The `egg_orchestrator` shared package (`shared/egg_orchestrator/`) provides:

| Module | Purpose |
|--------|---------|
| `client.py` | `OrchestratorClient` for signal API |
| `types.py` | Typed data classes (signals, responses) |
| `detection.py` | Mode detection utilities |
| `constants.py` | Port numbers, network IPs |

Usage:
```python
from egg_orchestrator import (
    OrchestratorClient,
    is_orchestrator_mode,
    DeploymentMode,
)

if is_orchestrator_mode():
    client = OrchestratorClient()
    client.signal_complete(
        pipeline_id="issue-123",
        agent_role="coder",
        commit="abc1234",
    )
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `EGG_ORCHESTRATOR_MODE` | Deployment mode (`local`, `remote-single`, `distributed`) | `local` |
| `EGG_ORCHESTRATOR_URL` | Orchestrator API URL | None |
| `EGG_PIPELINE_ID` | Current pipeline identifier | None |
| `EGG_AGENT_ROLE` | Agent role for multi-agent mode | None |
| `EGG_BRANCH` | Target branch for the agent's worktree | `egg/{pipeline_id}/work` |
| `EGG_PRIVATE_MODE` | Private network mode (set by host wrapper, detected by `egg-sdlc`) | None |
| `HOST_HOME` | Host machine's home directory (e.g., `/home/user`); used to translate host worktree paths to orchestrator-accessible paths | None |
| `EGG_LAUNCHER_SECRET` | Bearer secret the orchestrator presents to the gateway. Reused by the orchestrator-only `/api/v1/jira/ticket/transition` route (#1557 decision-15). Canonical mount is the file `/secrets/launcher-secret`; this env var is the fallback when the file is unavailable. Read by `orchestrator/wontdo_drain.py::_resolve_launcher_secret`. See [Orchestrator-Only Jira Transitions](#orchestrator-only-jira-transitions-apiv1jiratickettransition--1557-decision-15) for the trust model. | None |
| `EGG_ORCH_MAX_PARALLEL_SLICES` | Slice-DAG: per-pipeline slice spawn concurrency cap (#2137) | `2` |
| `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` | Slice-DAG: orchestrator-process-wide cap on slices in flight across **all** running pipelines (#2241). Each slice spawns ~8 containers; the default of 4 reflects the observed host saturation ceiling. Slices that exceed the cap stay READY and re-yield next poll tick. Per-process — HA replicas each maintain their own counter. | `4` |
| `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` | Slice-DAG: per-slice BRC re-proposal ceiling before HITL escalation (#2137) | `3` |
| `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` | Slice-DAG: pipeline-wide summed slice-cycle cap (#2137) | `10` |
| `EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS` | Slice-DAG: grace window before failure-cascade marks downstream subtree `BLOCKED_ON_FAILED_DEPENDENCY` (#2137) | `60.0` |
| `EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS` | Slice-DAG: stacked-PR reconciler polling cadence for orphaned child PRs (#2137) | `30.0` |

### Constants

Defined in `shared/egg_config/constants.py`:

```python
ORCHESTRATOR_CONTAINER_NAME = "egg-orchestrator"
ORCHESTRATOR_PORT = 9849
ORCHESTRATOR_SERVICE_HOST = "orchestrator.egg-system.svc.cluster.local"
GATEWAY_SERVICE_HOST = "gateway.egg-system.svc.cluster.local"
```

> **Migration note:** Fixed IPs (`172.32.0.x`, `172.33.0.x`) are replaced by Kubernetes Service DNS names. See [Kubernetes Migration](kubernetes-migration.md).

## Related Documentation

- [Gateway README](../../gateway/README.md) - Gateway sidecar details
- [Sandbox README](../../sandbox/README.md) - Sandbox container details
- [Shared README](../../shared/README.md) - Shared packages
- [egg_contracts](../../shared/egg_contracts/) - Contract models and orchestration
- [Slice-DAG Implement Phase](slice-dag.md) — slice scheduler, stacked-PR reconciler, per-slice branches and BRC trackers (#2137)

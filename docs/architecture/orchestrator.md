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

3. **Concurrent pipeline consensus trackers**: For each `RUNNING` pipeline (i.e., those not already marked `FAILED` by step 1) in a concurrent phase, the in-memory `PeerConsensusTracker` (which does not persist to disk) is reconstructed by replaying `CONSENSUS_*` messages from the message store in timestamp order. This allows agents that are mid-consensus to resume correctly after a restart rather than looping. Reconstruction is best-effort — if no consensus messages are found, the tracker is left absent and the orchestrator's per-phase tick re-derives `next-action` on the next iteration and spawns whatever was actionable (the on-demand spawn path's re-entry contract — see [BRC On-Demand Agent Spawning](#brc-on-demand-agent-spawning)).

   **Per-slice tracker reconstruction (#2777 slice-4 TASK-4-5, closes [#2409](https://github.com/jwbron/egg/issues/2409)):** In addition to the pipeline-level reconstruction above, startup reconciliation now iterates `contract.slices` and reconstructs each per-slice tracker by calling `reconstruct_tracker_from_messages(pipeline_id, graph, slice_id=<slice>)`. Each reconstructed tracker is registered under the nested `{pipeline_id}/{slice_id}` key — the same key shape used by per-slice agents at runtime — so an orchestrator-pod recycle no longer loses in-flight slice consensus and per-slice agents that resume after the recycle find their tracker exactly where they left it. Cross-slice isolation is enforced by the strict-equality filter `_message_slice_id(m) == slice_id` inside `reconstruct_tracker_from_messages` (`peer_consensus.py` near line 2003), not by the store-level filter at `message_store.py:407-418` — the latter is intentionally lenient (it passes `metadata.slice_id is None` messages through any slice filter so OVERSEER_ALERTs fan out across slices). Contract paths are resolved via `routes.resolve_worktree_path` since active pipelines' contracts live in per-pipeline worktrees at `/home/egg/.egg-worktrees/<pipeline_id>/<repo>/`, not under `store.repo_path`; resolution failures are logged so operators see the degradation rather than silently losing per-slice reconstruction.

This prevents pipelines from being stuck in `RUNNING` or `AWAITING_HUMAN` states indefinitely after a crash. Operators (or CI systems) can detect the `FAILED` status and restart the pipeline using the existing restart endpoint, which preserves worktrees and phase state while re-spawning containers.

See `orchestrator/state_store.py` and `orchestrator/startup_reconciliation.py` for implementation details.

**Runtime pod monitoring:**

A background `KubernetesMonitor` thread runs continuously after orchestrator startup to detect agent pod failures during execution. The monitor periodically checks pod status via the Kubernetes API and invokes registered handlers when state changes occur (pod exits, fails, or becomes unhealthy).

A pipeline reconciliation handler detects when agent pods exit or fail during runtime and updates pipeline state accordingly. The handler scans **all phases** within each `RUNNING` or `AWAITING_HUMAN` pipeline (including completed phases) to find the exited pod, as reviewer agents may continue running after their phase has transitioned to `COMPLETE`. Terminal pipelines (`FAILED`, `COMPLETE`, `CANCELLED`) are also scanned if they still have stale `RUNNING` agent or container records — an optimization that skips the common case where no cleanup is needed.

When a container running an agent exits with a non-clean code, the handler marks the container as `FAILED` and marks the owning agent as `FAILED` with an error message — unless the agent is already `COMPLETE` (i.e., it completed via BRC consensus), in which case the exit is ignored. The handler only updates agent and container sub-records; the pipeline's top-level `status` is never mutated by the monitor (see #2210). The pipeline-level decision about whether the pipeline itself has failed lives in the BRC poll loop in `_run_concurrent_phase`, which has full consensus context the monitor lacks. Containers that exit with code 0 (graceful exit after BRC protocol work) or 143 (orchestrator-initiated SIGTERM during phase teardown) are reconciled as clean exits — `_classify_exit` marks the agent `COMPLETE` and the container `EXITED` rather than `FAILED`. (Mechanism note: exit 0 emits a `STOPPED` event that bypasses reconciliation entirely; exit 143 still emits a `FAILED` event, but `_reconcile_pod_state` consults `_classify_exit` and routes it to the clean-exit branch.) When BRC consensus completes, the concurrent phase runner proactively marks agent containers as `EXITED` with exit code 0 so that subsequent monitor sweeps treat them as clean exits; the agent-COMPLETE check is a secondary defense for the event window before that update is persisted. This complements startup reconciliation by catching failures that occur during execution rather than only on orchestrator restart.

Additionally, the concurrent phase runner (`_run_concurrent_phase`) treats consensus completion (`is_complete=True`) as the **authoritative success signal**. If all agents have confirmed consensus but some containers exited with non-zero codes (e.g., a per-event spawn crashed before its `propose`/`ack`/`nack` finished but the role still reached consensus on a later spawn), the phase runner logs a warning about the prior failures but returns success (exit code 0). This prevents the compounding failure scenario where successful BRC consensus is overridden by stale container exit codes. A final `check_consensus()` recheck is also performed on the all-container-exit path (step 5) before returning failure, closing a race window where the step-2 consensus check reads stale tracker state and step 5 returns `exit_code=1` without verifying that consensus is genuinely incomplete.

In addition to the event-driven handler, `ContainerMonitor.start_periodic_reconciliation()` runs a second background thread that sweeps every 30 seconds for stale containers that may have exited between Docker events (e.g., missed events during a partial restart). This periodic sweep checks the **current phase** of each `RUNNING` or `AWAITING_HUMAN` pipeline, and also of any terminal pipeline that still has stale `RUNNING` records. On non-`RUNNING` pipelines it reconciles only the sub-records (agent/container status), leaving the pipeline's top-level status unchanged. Like the event-driven handler, the sweep inspects the actual container exit code before reconciling: containers that exited with code 0 or 143 (clean exit — e.g., a per-event agent completing gracefully, or an orchestrator-initiated SIGTERM during phase teardown) do not trigger `FAILED` reconciliation. Exit 0 short-circuits the sweep loop directly; exit 143 also short-circuits when the phase has already moved past `RUNNING`, but a 143 observed while the phase is still `RUNNING` falls through to `_reconcile_pod_state`, which uses `_classify_exit` to mark the agent `COMPLETE` instead of `FAILED`. The first sweep is intentionally delayed by one interval since startup reconciliation already ran immediately before the thread was started.

The monitor uses per-pipeline locking and optimistic version checks to prevent race conditions with concurrent state writers (e.g., agent signal handlers).

See `orchestrator/kubernetes_monitor.py` for implementation details.

**Health check framework:**

A two-tier health check framework provides structured, extensible failure detection across the pipeline lifecycle. All checks implement a common `HealthCheck` protocol and produce `HealthResult` values with a status (`HEALTHY`/`DEGRADED`/`FAILED`), reasoning, and a suggested action (`CONTINUE`/`FAIL_PIPELINE`/`ALERT`).

**Tier 1 (Programmatic)** checks are fast and deterministic. They run on every lifecycle trigger and cover structural invariants: container liveness, startup state, phase output presence, state consistency, and consensus stall detection. Container liveness and startup state checks are adapters over existing `ContainerMonitor` and `reconcile_stale_containers` logic. The phase output check detects the issue-835 pattern where agents complete successfully but produce no artifacts (e.g., no commits on the remote branch after an implement phase). The state consistency check cross-references orchestrator state against Docker reality and contract data. The consensus stall check fires on `RUNTIME_TICK` and `ON_DEMAND` for concurrent execution phases: when all agents are confirmed but the phase has not advanced past a 60-second grace period, it reports `DEGRADED` and `ContainerMonitor` drives recovery (tracker reconstruction first, then aggressive agent/phase completion with optimistic locking).

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

## Slice/phase restart hardening (#2777 slice-4, bundles [#2409](https://github.com/jwbron/egg/issues/2409))

The implement-phase run loop is multi-slice (see [slice-dag.md](slice-dag.md)), so an interrupted slice DAG must resume on the **next** orchestrator startup without re-spawning slices that already did real work and without leaving slice-scoped consensus state orphaned from a manual `restart_phase`. Three behaviours hold the line:

**Slice-aware `restart_phase` (TASK-4-1).** Phase-level restart used to clear only the pipeline-level consensus tracker. In slice-aware mode every per-slice agent team registers its tracker under the nested key `{pipeline_id}/{slice_id}` (matching the runtime key shape), so the pipeline-level `clear()` left those slice-scoped trackers untouched and a restarted phase deadlocked on stale per-slice consensus state. `restart_phase` now loads the contract via `routes.resolve_worktree_path` (active pipelines' contracts live in per-pipeline worktrees, not under the orchestrator's repo root), iterates `contract.slices`, and calls `clear()` on each per-slice tracker via `get_peer_consensus_tracker(pipeline_id, slice_id=<slice>)` in the same operation. A contract-load failure preserves the historical pipeline-level-only behaviour rather than blocking the restart (best-effort: a misconfigured worktree should not gate manual recovery).

**Eager `parent_branch_at_creation` + IN_PROGRESS flip (TASK-4-2, cq-9).** When a slice is spawned the run loop now persists `Slice.parent_branch_at_creation` and flips `SliceStatus.PENDING → IN_PROGRESS` in the **same** contract write under the per-pipeline state lock. Crash-recovery (Layer C below) reads `SliceStatus.IN_PROGRESS` as the single signal distinguishing a fresh slice from one that was interrupted mid-run. The flip is idempotent on re-entry: `COMPLETE` / `BLOCKED` / `IN_PROGRESS` are left untouched so a re-issued spawn does not overwrite a terminal state.

**Merge-base validation in `_resolve_slice_base_branch` (TASK-4-3, cq-9 part 2).** Legacy or orphaned slices may pre-date the eager-persist contract and have `parent_branch_at_creation` empty. The resolver now exposes a `merge_base_lookup` callback (wired by the slice-loop call site to a new `GatewayClient.merge_base` method that runs `git merge-base` through `/api/v1/git/execute`). When `parent_branch_at_creation` is empty, the resolver computes `git merge-base <integration_branch> <derived_parent>`: a real SHA confirms a valid fork point and the resolver returns the dependency-derived branch name (downstream callers like `create_slice_integration_branch` need branch names, not SHAs — the merge-base SHA is logged for audit only); a `None` SHA means no fork point and the resolver routes onto `pipeline_branch` (the safe root-stack fallback). The three-tier resolution order is therefore: eager-persisted `parent_branch_at_creation` → merge-base-validated derived parent → `pipeline_branch`. The slice-loop's `_probe_merge_base` wrapper best-effort `fetch_branch`-es both refs into the local odb before invoking `merge_base`, so a transient fetch lag does not make `git merge-base` return `None` (return-code 1) and silently route the slice onto `pipeline_branch`; fetch failures are logged at debug and the merge-base call still runs against whatever the local odb has, and a transient gateway error on one ref's fetch does not skip the other ref's fetch.

**Layer C bootstrap reconciliation (TASK-4-4, 5-way classification).** The implement-phase run loop's bootstrap pass (`_run_implement_phase_slices`) used to classify only `COMPLETE` slices. After Layer A/B, a third layer now classifies each remaining non-`COMPLETE` slice using `SliceStatus`, the integration branch's commit count on origin (via the gateway probe), and the slice's consensus tracker:

| # | Observed state | Action |
|---|----------------|--------|
| 1 | `IN_PROGRESS`, no commits on integration branch | No-op — scheduler re-yields the slice as `READY` and a fresh team spawns |
| 2 | `IN_PROGRESS` + commits + no consensus tracker | Mark the slice as already spawned so the scheduler does not re-yield it |
| 3 | `IN_PROGRESS` + commits + consensus reached but unrecorded | Mark `COMPLETE`; emit a louder-than-fresh-spawn audit warning |
| 4 | `BLOCKED` | Preserve the BLOCKED state; if no pending HITL decision exists, escalate to HITL via `_escalate_blocked_slice_to_hitl` |
| 5 | Corrupt / unclassifiable state | Escalate to HITL via `_escalate_corrupt_slice_to_hitl` |

Cases 4 and 5 create an unresolved `Decision` on the contract (via the shared `_escalate_layer_c_hitl` helper) with three options — mark complete, restart slice, cancel — and pause the pipeline rather than silently re-yielding as `READY`. Per the plan task body: "silent classification error is worse than an operator pause."

**Case-4 suppression scope.** The case-4 anomaly check (`_slice_has_pending_decision`) returns `True` whenever the contract carries *any* unresolved `Decision`, regardless of which slice or phase it originated in — the contract's `Decision` schema does not carry a structured `slice_id` tag today, so the classifier conservatively treats any unresolved HITL as "potentially the reason this slice is BLOCKED" and skips the missing-HITL escalation. Practical consequence operators should know: a single unrelated pending HITL on the pipeline (e.g. a refine-phase question) silently suppresses every BLOCKED-slice case-4 escalation until that decision resolves. The trade-off favours not double-alerting an operator who is already engaged with a HITL over surfacing a real cross-slice mismatch; a future schema bump that adds slice scoping to `Decision` can tighten this without changing call sites. The classifier itself is extracted to a module-level helper `_classify_non_complete_slice` so tests can fake the gateway probe and consensus-tracker lookup without spinning up the run loop. The classifier's probe-failure default is "fresh, re-yield `READY`" (the safer direction for the scheduler), while the resolver's probe-failure default is "derived parent" (the safer direction for the next push) — the asymmetry is deliberate and each picks the direction least likely to break its own caller.

**Per-slice consensus tracker reconstruction at startup ([#2409](https://github.com/jwbron/egg/issues/2409) closure, TASK-4-5).** Documented in the [Startup reconciliation](#pipeline-state-persistence) section above. The signals route (`handle_consensus_confirmed_signal`) also no longer skips reconstruction when `slice_id` is supplied — the strict-equality filter in `reconstruct_tracker_from_messages` is the canonical scope mechanism, so slice-scoped reconstruction is safe at the CONFIRMED handler too.

See `orchestrator/routes/pipelines.py` (`restart_phase`, `_run_one_slice_inner`, `_resolve_slice_base_branch`, `_run_implement_phase_slices` Layer-C block, `_classify_non_complete_slice`, `_escalate_layer_c_hitl`), `orchestrator/routes/signals.py` (`handle_consensus_confirmed_signal`), `orchestrator/startup_reconciliation.py` (`_enumerate_contract_slices` + per-slice reconstruction loop), and `orchestrator/gateway_client.py` (`merge_base`) for implementation details.

## Pipeline Modes

- **`issue`** (default): Standard SDLC pipeline triggered by a GitHub issue. Progresses through refine → plan → implement phases with structured agent teams.

## Orchestrator-Only Jira Transitions (`/api/v1/jira/ticket/transition`) — #1557 decision-15

The Jira-epic SDLC pipelines introduced by [issue #1557](https://github.com/jwbron/egg/issues/1557) need to transition pre-existing child tickets to **Won't Do** when the reassess flow supersedes them (consolidations, obsoletes, replanned scopes). The agent-facing Jira gateway intentionally **forbids transitions** today (`gateway/jira_client.py:133` `JIRA_WRITE_VERBS_DENIED`), and the trust-boundary decision keeps it that way: there is no Jira state-machine surface available to in-sandbox agents.

Instead, transitions land via a **separate orchestrator-only gateway route**, `POST /api/v1/jira/ticket/transition`, gated on **loopback / cluster-internal source + launcher-secret bearer token**. The applier in the sandbox writes Won't-Do candidates to a handoff JSON. The orchestrator-side `_drain_wontdo_batch_after_apply` hook (`orchestrator/routes/pipelines.py`) reads the handoff after apply-phase BRC consensus and calls `/transition` once per entry via `orchestrator/wontdo_drain.py::run_wontdo_drain`, out of band from the HITL HTTP response so Jira API latency does not block operator approvals.

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

The sandbox-side applier emits a handoff JSON and never attempts to call `/transition` directly.

### Cross-references

- Gateway-side route definition + audit log shape: `gateway/gateway.py` (search for `transition`); see also `gateway/README.md` for the deployment-time secret bundle layout.
- Orchestrator-side drain helper: `orchestrator/wontdo_drain.py::{load_wontdo_handoff, run_wontdo_drain}`.
- Orchestrator-side drain hook: `orchestrator/routes/pipelines.py::_drain_wontdo_batch_after_apply` — invoked from both the auto-advance and HITL-resolution apply-phase exit paths; writes per-Task `jira_action_status` back via the `on_entry_result` callback.
- Issue-level decision record: [#1557 decision-15](https://github.com/jwbron/egg/issues/1557) (trust-boundary for Jira transitions).

## Upstream Routing (Per-Agent Model Backends, [#2769](https://github.com/jwbron/egg/issues/2769))

Per-agent `/v1/messages` traffic routes through an `UpstreamRegistry`
in the gateway that resolves the upstream (Anthropic vs LiteLLM) from
per-session metadata declared by the orchestrator at session-create
time — the same IP-keyed session lookup that already drives
`session_mode`. Slice 1 of #2769 lands the router and the LiteLLM
Deployment + Service in `egg-system`, no-op by default; slice 2 adds
`PipelineConfig.agent_models` and a repository-level
`default_agent_model` for the orchestrator-side resolution. Until an
operator opts in, every existing pipeline keeps running on Claude
with byte-identical gateway behavior.

See [Upstream Routing](upstream-routing.md) for the gateway-side
seam (registry, credential layout, request lifecycle, failure
policy, and the no-op-by-default invariant). The operator-facing
setup ships in slice 2 as `docs/guides/per-agent-models.md` (that
file does not exist until slice 2 lands).

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

**PR creation (auto-PR at plan→implement boundary, [#2777](https://github.com/jwbron/egg/issues/2777))**

The PR phase was **deleted** as a separate pipeline stage in [#2777](https://github.com/jwbron/egg/issues/2777). The context PR is now opened up-front, idempotently and hard-required, at the plan→implement boundary; there is no longer a terminal "PR" step. The orchestrator auto-creates the PR via `GatewayClient.create_pr()` (and `create_slice_pr` for per-slice PRs in slice-aware mode), which:
1. Extracts PR title/description from the contract's `pr` field (populated by the plan agent)
2. Falls back to the issue title or pipeline ID if no PR metadata exists
3. Appends git commit log, diff stats, and a **Pipeline Context** section (pipeline ID + issue number) to the PR body
4. Creates the PR via the gateway using a temporary synthetic session (`synthetic=True`, `phase=None`; see the `GatewayClient.create_pr` docstring at `orchestrator/gateway_client.py:1546`) and the pipeline's resolved network mode; in `private` mode the PR is created as a draft
5. Applies `egg` and `agent:orchestrator` labels to the newly created PR

Idempotency is enforced before every `gh pr create` call, via a single shared primitive. Both the context-PR opener (`_open_context_pr_at_implement_start`) and per-slice PR creation (`create_slice_pr`) call `GatewayClient.lookup_open_pr(head, base)`, which calls the orchestrator-only control-plane route `/api/v1/gh/find_open_pr` (gated by launcher auth, not agent session auth — [#2893](https://github.com/jwbron/egg/issues/2893)). The gateway runs `gh pr list --repo <repo> --head <branch> --base <base> --state open --limit 1 --json number` server-side and returns `{"number": <int>|null}`. On hit, the existing PR number is returned without invoking `gh pr create`; on miss, the create path runs as usual. The opener originally enumerated every open PR via `GatewayClient.list_open_prs` and filtered client-side; [#2934](https://github.com/jwbron/egg/issues/2934) unified it onto the narrow server-side filter so the two PR-idempotency paths no longer diverge. (`list_open_prs` remains for the [stacked-PR rebase reconciler](slice-dag.md#stacked-pr-rebase-reconciler), which genuinely needs to enumerate all open PRs to find orphaned children.) See [Context PR (slice-aware mode)](#context-pr-slice-aware-mode-2777) below.

The gateway also injects an `<!-- egg-pipeline-context ... -->` HTML comment into the PR body containing machine-parseable pipeline metadata (`pipeline_id`, `agent_role`, `issue`). Labels are applied best-effort — failures are logged but non-fatal.

This eliminates the need for agent interaction during PR creation, makes the PR open idempotent and recoverable, and ensures consistent PR formatting across all pipelines.

### Pipeline state writeback after auto-PR creation

After a successful context-PR open, `_persist_context_pr_number` writes `pipeline.pr_url` and `pipeline.pr_number` onto the pipeline record inside the same `get_pipeline_state_lock → reload → save` transaction. `pr_number` is parsed from the `pr_url` via `re.search(r"/pull/(\d+)", pr_url)`; on the idempotent `lookup_open_pr` hit path the URL is synthesised from `pipeline.repo` + `pr_number` so the regex parse works unchanged. The same persistence write fires on the idempotent path so a resume-from-orphaned-pipeline where the contract lost `context_pr_number` mid-run still recovers.

Issue-mode consumers (overseer stall detector, `get_pipeline_snapshot`, MCP `get_pipeline_status`, jira-reassess in-flight detection) can read `pipeline.pr_number` directly without falling back to `gh pr list` or parsing the `pr_url` artifact. These fields were added in response to issue #1911, where a stale `pr_number` on successful runs drove false-positive `post-consensus-push-stall` alerts in the overseer.

> The `pipeline.pr_head_sha` field on the model is no longer populated — its sole writer (`_finalize_pr_phase_failed`) was deleted alongside the PR phase in [#2777](https://github.com/jwbron/egg/issues/2777). The overseer's post-consensus short-circuit no longer reads `pr_head_sha`; it now keys on two independent signals (`pipeline.current_phase != "implement"` or `pipeline.pr_number is not None` — see [`pipeline-health-monitoring.md` → Transition-completion short-circuit](../guides/pipeline-health-monitoring.md#post-consensus-stall-detection)), so the field is effectively unused. The model column is retained only for backwards-compatible deserialisation of older state files.

### Per-agent commit SHA diagnostics

`_update_agents_complete` populates each `completed_agents[].commit` field from `_brc.get_proposal_commit_sha(role)`. When the BRC tracker returns `None` or the `"RECONSTRUCTED_NO_SHA"` sentinel, a structured `logger.warning("BRC tracker returned no commit sha for completed agent", pipeline_id=..., phase=..., role=..., brc_value=...)` is emitted so the missing writeback can be investigated. This is diagnostic only — there is deliberately no auto-fallback to a guessed SHA, since that would mask the underlying wiring gap.

## Context PR (slice-aware mode, [#2777](https://github.com/jwbron/egg/issues/2777))

After the plan phase completes and the plan_gate is approved, the orchestrator opens the **context PR** — `egg/<pipeline_id>/work → main` — up-front, idempotently and hard-required, at the plan→implement boundary, before any slice spawns. The context PR establishes the program — analysis + plan + refine/plan BRC consensus, plus the program-level test plan, manual steps and pre-merge obligations — so reviewers approaching any slice PR see the strategic context that produced it, and so the program-level narrative has a permanent home that no longer depends on a "terminal slice umbrella" treatment.

This collapses the earlier two-branch topology (`egg/<id>/context → main` for program-level content, `egg/<id>/work → main` for the actual implementation) onto a single branch: the pipeline's work branch is itself the context PR's head. Mechanics:

1. The orchestrator validates that the plan agent has produced the artifacts the context PR will reference (drafts + BRC history + populated contract). If validation fails at the plan→implement boundary the orchestrator refuses to transition — the context PR is hard-required.
2. It opens the PR against the pipeline's `base_branch` (typically `main`) using `contract.pr.title` and `contract.pr.description` — the same fields that already framed the implementation PR. The "context_*" fields were removed in schema v1.2 (see below); program-level framing now lives on the standard `pr.title` / `pr.description`.
3. The open is **idempotent**: `GatewayClient.lookup_open_pr(head, base)` is called with head `egg/<id>/work` and base `base_branch`; the gateway runs the narrow `gh pr list --head --base --state open` filter server-side. On hit, the existing PR number is reused; on miss, `gh pr create` runs. This is the same control-plane primitive `create_slice_pr` uses — [#2934](https://github.com/jwbron/egg/issues/2934) migrated this path off the older client-side `list_open_prs` filter so both PR-idempotency sites share one seam.
4. Slice-1's `parent_branch` resolves to `egg/<id>/work` (the pipeline work branch is itself the context PR head); slice-N>1 stacks on its predecessor as before. The [stacked-PR rebase reconciler](slice-dag.md#stacked-pr-rebase-reconciler) uses the work branch as the canonical fallback when retargeting orphaned children.

The context-PR rollout is a **hard switchover** (HITL decision-4) — there is no backwards-compat shim or feature flag, and in-flight pipelines are not backfilled. The legacy "PR phase" as a separate stage is gone; the up-front idempotent open is the only PR-creation site.

The contract's `pr` field (`PRMetadata`, `shared/egg_contracts/models.py`) was simplified in schema v1.2 (issue [#2777](https://github.com/jwbron/egg/issues/2777)): three redundant `pr.context_*` framing fields introduced in v1.1 (#2548) were **hard-removed**. Program-level framing now uses the standard `pr.title` / `pr.description` fields. See the [v1.1 → v1.2 schema migration note](sdlc-pipeline.md#schema-v11--v12-migration-note-2777) for the exact field names and replacements. The remaining context-PR field is:

| Field | Author | Description |
|-------|--------|-------------|
| `pr.context_pr_number` | Orchestrator | GitHub PR number of the `egg/<id>/work → main` context PR — populated when the PR is opened. |

These coexist with the existing `pr.title` / `pr.description` and `pr.test_plan` / `pr.manual_steps` / `pr.deferred_actions` fields. See the [Concurrent Execution Slice PR Stack section](../guides/concurrent-execution.md#slice-pr-stack) for the end-to-end stack shape and reviewer flow, and the [v1.1 → v1.2 schema migration note](sdlc-pipeline.md#schema-v11--v12-migration-note-2777) for the field removal details.

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
| Non-slice (override pipelines without `contract.slices`) | A single aggregate file: `<id>-implement.{md,json}`. |

The orchestrator commits each `<id>-implement-slice-<N>.{md,json}` to the slice integration branch as a final orchestrator-authored commit before the slice PR is opened. This is necessary because the `coder` and `tester` role boundaries forbid pushes under `.egg-state/brc-history/`; the existing `_commit_statefiles_to_worktree` pattern keeps history persistence deterministic. See [Concurrent Execution: BRC History Link in PR Body](../guides/concurrent-execution.md#brc-history-link-in-pr-body) for the link-line behaviour rendered into auto-generated PR bodies.

## Per-Pipeline Worktrees

The orchestrator reads pipeline artifacts (verdict files, draft documents, check results) from per-pipeline worktrees created by the gateway. These worktrees isolate work for each pipeline and are separate from both the orchestrator's state worktree and the main repository working directory.

**Architecture:**
- Gateway creates worktrees at `/home/egg/.egg-worktrees/{job-name}/{repo-name}/` (one per agent)
- Each agent pod mounts its own worktree via hostPath and writes artifacts to it
- All agents in a pipeline push to the same shared branch (e.g., `egg/issue-{N}/work` since #2399)
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

   - **Prior phase succeeded, local ahead:** Local commits are pushed to remote first, preserving completed work. After a successful push the local branch already matches `origin/<branch>`, so no further reset is needed. On push failure with `remote_ahead == 0` (origin holds nothing the worktree lacks), the function returns `local_ahead_push_failed` *without* resetting — resetting here would silently discard completed, committed work (e.g. agent-registered HITL contract decisions) for zero reconcile benefit (#2972). The unpushed commits remain in the worktree for the decision bridge and the next push attempt.
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

All components communicate over Kubernetes networking with namespace-based isolation enforced by NetworkPolicies (Cilium today):

| Namespace | Purpose | Components |
|-----------|---------|------------|
| `egg-system` | Core services | Gateway (Deployment + Service), Orchestrator (Deployment + Service) |
| `egg-agents` | Agent execution | Agent Jobs (one per agent role per pipeline) |

Service endpoints:
- Gateway: `gateway.egg-system.svc.cluster.local` (ports 9848, 3129, 9851)
- Orchestrator: `orchestrator.egg-system.svc.cluster.local` (port 9849)
- Agent pods: Addressed by label selector (`pipeline-id`, `agent-role`)

NetworkPolicies (enforced by Cilium CNI):
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

Available MCP tools (orchestrator-backed): `submit_task`, `get_status`, `provide_input`, `answer_feedback`, `list_tasks`, `cancel_task`, `check_health`, `list_containers`, `get_container_logs`, `send_message`, `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`, `get_contract`, `validate_config`, `restart_agent`, `restart_phase`, `advance_phase`, `start_phase`, `complete_phase`, `populate_contract`

Blocking host-side waits run via the `egg-orch pipeline wait-status` Bash CLI rather than an MCP tool (issue [#2211](https://github.com/jwbron/egg/issues/2211)). The CLI loops the orchestrator's `/api/v1/pipelines/<id>/status/wait` route server-side and emits one JSON-line per pipeline-relevant event. See [Host-Side Waits](../reference/agent-wait-patterns.md#7-host-side-waits--egg-orch-pipeline-wait-status) for the envelope, exit-code contract, and cursor protocol. The route itself stays — the CLI is a wrapper.

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

## BRC On-Demand Agent Spawning

> **[#3023](https://github.com/jwbron/egg/issues/3023) inverts the BRC
> agent lifecycle.** Before #3023 the orchestrator spawned the full team
> at phase start and each pod ran a long-lived in-pod event-pump bash
> loop (the legacy BRC pod wrapper, deleted in slice 3) that long-polled
> the bus and invoked the agent one-shot per event, staying alive
> (idle, heartbeating,
> holding a gateway session) for the whole phase. After #3023 the
> orchestrator lifts the wait out of the pods into a single blocking
> thread that covers every role: it derives per-role actionability with
> `_derive_next_action` and spawns a one-shot pod **only when a role has
> an actionable event**. The pod handles the event and exits. There are
> no idle pods. The wait-side companion is
> [agent-wait-patterns §10](../reference/agent-wait-patterns.md#10-brc-on-demand-agent-spawning).
>
> Per-phase reuse of the prompt-cache prefix, gateway session, and
> worktree is **unchanged**: the orchestrator now holds a per-(pipeline,
> role) gateway session alive via `OrchestratorSessionKeepAlive` and
> reuses the per-role worktree PVC across spawns. Only the wait moved.
> This is a resource-freeing change, not a latency optimisation — the
> refine HITL decision cq-1 explicitly declined to set a cold-start SLO.

### Why the orchestrator drives the loop

The pre-#3023 lifecycle had two parts that #3023 collapses:

1. **#2908 collapsed the per-event sequencing seam** by reframing the
   consensus agent from a *persistent participant that holds a wait* into
   a *stateless per-event handler the wrapper invokes*. The wrapper was
   the deterministic loop driver; the agent was one-shot per actionable
   event. But the wrapper itself was still a long-lived in-pod bash loop:
   one wrapper pod per role, idle most of the time, holding a CPU/memory
   reservation, a pinned gateway session, and a 30-s heartbeat across the
   full phase.
2. **#3023 lifts the wait out of the pods entirely.** The orchestrator
   has always known how to derive next-action from the tracker state
   (`routes/consensus.py::_derive_next_action`); now it consumes that
   derivation directly inside the per-phase tick (one blocking thread,
   not N per-pod waits and not per-role timers). When a role's
   next-action transitions from `wait` → `propose|ack|nack|confirm`, the
   orchestrator spawns a one-shot pod for that role with the prompt
   already composed; the pod runs `python3 -m egg_agent` to completion of
   that event and exits. Per-role state (prompt-cache prefix, gateway
   session, worktree PVC) is held by the orchestrator and rebound to the
   next spawn.

The full design — refine HITL decisions on cold-start SLO, session
ownership, idle-alert ownership, rollout shape, wrapper retirement
scope, and overseer-agent scope — is documented in the contract
decisions cq-1..cq-6 captured at refine. The plan landed in three
dependent slices that ship as a single PR.

| Slice | Lands |
|-------|-------|
| 1 | Orchestrator-side phase-level idle-budget alert (`orchestrator/phase_idle_budget.py::PhaseIdleBudgetTimer`); wrapper coexistence guard so only one path emits the idle alert during slice-2 burn-in. |
| 2 | `OnDemandSpawner` + the per-event spawn function in `KubernetesSpawner.create_on_demand_spawn_fn`; per-(pipeline, role) gateway-session keep-alive (`orchestrator/session_keepalive.py::OrchestratorSessionKeepAlive`); per-role worktree-PVC reuse across spawns; per-spawn pod-log capture; orphan-commit detector. The still-spawned wrapper pod is made passive via `EGG_EVENT_LOOP_OWNER=orchestrator` so exactly one path emits BRC verbs during slice-2 burn-in. |
| 3 | Legacy BRC pod wrapper retirement (the in-pod event-pump bash file is deleted in its entirety); pod entrypoint collapses to `python3 -m egg_agent --model {alias} --max-turns {N}` with the composed prompt on stdin; SIGTERM handling moves into `shared/egg_agent/__main__.py`; tolerant pod-state reader so a cross-version `git revert` (cq-4 big-bang) drains in-flight pipelines without crashing on a missing wrapper. |

The architecture is conceptually simple: the orchestrator owns
sequencing, lifecycle, and session state; the agent owns judgment
inside a single event's worth of work. The wrapper bash that bridged
those two before #3023 is gone.

### On-demand spawn loop

The per-phase tick in `orchestrator/routes/pipelines.py` drives the
`OnDemandSpawner` once per loop iteration:

```text
# orchestrator per-phase tick (single thread covers all roles)
for role in phase.roles:
    action = derive_next_action(tracker, role)          # in-process call
    case action.kind:
      WAIT, COMPLETE:
        continue                                         # no spawn
      PROPOSE | ACK | NACK | CONFIRM:
        if (pipeline_id, role) in in_flight:
          continue                                       # coalesce: one pod per role at a time
        prompt = compose_event_prompt(role, event)       # in-process; same composer as #2908
        spawn_fn = kubernetes_spawner.create_on_demand_spawn_fn(...)
        spawn_fn(role, prompt, model_decision)           # python3 -m egg_agent, one-shot
        in_flight.add((pipeline_id, role))

session_keepalive.refresh_all(now)                       # one keep-alive sweep per tick
phase_idle_budget.check(now, pending_hitl_count)         # phase-level idle alert
```

The orchestrator is the **sequencing oracle** — it owns "should I
spawn?" vs "should I coalesce?" vs "is the phase idle?". The model
still owns *judgment* (review verdicts, fix decisions) inside the
one-shot agent run.

### Per-(pipeline, role) gateway-session reuse (cq-2)

Per-phase reuse of the gateway session is held by the orchestrator,
not by an in-pod process. `OrchestratorSessionKeepAlive`
(`orchestrator/session_keepalive.py`) holds a
`{(pipeline_id, role): SessionRecord(token, created_at,
last_validated_at, idle_timeout_minutes)}` map; the per-phase tick
calls `refresh_all(now)` once per iteration, which validates any record
whose age exceeds `idle_timeout_minutes − 5` against the same gateway
`validate_session` endpoint the pre-#3023 wrapper heartbeat hit. There
are no new threads — this is a single multiplexed loop.

`OnDemandSpawner.record_phase_start` calls
`GatewayClient.register_session` once per (pipeline, role) at phase
entry (lifted from the pre-#3023
`kubernetes_spawner.py::register_session` block) and registers the
token with the keep-alive. `record_phase_end` calls `delete_session` +
`unregister`. Per-event spawns receive `EGG_SESSION_TOKEN` resolved via
the per-role lookup and **never** call `register_session` — the
spawner short-circuits the register branch via a `precreated_session_token`
kwarg. If the lookup returns `None` the spawn aborts with
`SessionNotRegisteredError` and fires an
`OVERSEER_ALERT(anomaly="missing_session_token")` — there is no
fresh-session fallback, by design.

### Per-role worktree-PVC reuse

Per-phase reuse of the per-role worktree PVC is unchanged in lifetime
but newly explicit in ownership. `record_phase_start` pre-warms each
role's PVC; `OnDemandSpawner.tick` reuses the warm PVC for every
subsequent spawn of that role. The PVC is `ReadWriteOnce`, so only one
pod per (pipeline, role) can hold the mount at any moment; the spawner
enforces this with a **per-role single-pod-in-flight precondition**:

- **Orchestrator-side coalescer.** `OnDemandSpawner.tick` records
  `in_flight: set[(pipeline_id, role)]`; while a spawn is in flight,
  subsequent `derive_next_action` returns for that role are coalesced
  (NACK fan-out producing N actionable events in a tight window yields
  exactly one spawn, not N).
- **Spawner-side precondition (defense in depth).**
  `KubernetesSpawner.spawn_agent_job` raises `SpawnPreconditionError`
  if a non-terminal Job already exists for the same (pipeline, role)
  label selector. The flag clears when the Job reaches a terminal phase
  via the existing pod-watch path.

### Phase-level idle-budget alert (cq-3)

The pre-#3023 in-pod wrapper emitted the `stuck-phase-transition`
overseer alert at the 30-min `EGG_BRC_IDLE_BUDGET_MIN` threshold. Under
on-demand spawning there is no in-pod process to emit it, so ownership
moves to the orchestrator as a **phase-level** alert:
`PhaseIdleBudgetTimer` (`orchestrator/phase_idle_budget.py`) records
each on-demand spawn (`record_spawn(pipeline_id, phase, role, action)`)
and is checked once per tick. The default threshold
(`DEFAULT_PHASE_IDLE_BUDGET_MIN = 30`) is preserved verbatim from the
old wrapper constant — operator UX does not regress.

The signal granularity changed: the pre-#3023 alert was **per-role**
(one alert per role per threshold bucket); the new alert is
**per-phase** (one alert per phase per threshold bucket, fired when no
role has spawned for X minutes). Operators that previously triaged a
specific role from the anomaly payload now read the structured
`per_role_state` payload field (AC-R4) attached to every alert:

```json
{
  "anomaly": "stuck-phase-transition",
  "priority": "medium",
  "per_role_state": {
    "coder": {"last_action": "propose", "last_spawn_at": "2026-06-09T14:31:02Z"},
    "tester": {"last_action": "wait", "last_spawn_at": "2026-06-09T14:18:55Z"},
    "documenter": {"last_action": "wait", "last_spawn_at": "2026-06-09T13:55:12Z"}
  }
}
```

**HITL-pending suppression (AC-R13).** When `pending_hitl_count > 0`
the 1× alert downgrades to `priority=low` and the `reason` carries the
pending HITL decision IDs verbatim; the 2× alert is suppressed
entirely. This stops the orchestrator from paging on idleness that an
operator is already addressing inside the HITL surface.

### Pod entrypoint

The pod entrypoint after slice 3 is exactly:

```text
python3 -m egg_agent --model {alias} --max-turns {N}
```

with the composed event prompt piped on stdin. There is no wrapper
bash, no heartbeat subshell, no `bash -c`. SIGTERM handling moved into
`shared/egg_agent/__main__.py` (the agent process-entry signal trap, in
#2908's lineage): a clean agent shutdown still produces the same
audit-log entries the pre-#3023 wrapper cleanup trap emitted, but the
trap is now Python-side rather than bash-side.

### Per-spawn pod logs

Each per-event spawn writes stdout/stderr to
`.egg-state/agent-outputs/<role>/spawn-<event_index>.log` on the
per-role worktree PVC. The spawner increments a per-role event index
and passes it as `EGG_SPAWN_INDEX=<N>`. Retention (AC-R5): the last
`OnDemandSpawner.PER_ROLE_LOG_RETENTION = 20` files per role are kept,
older files rotated out at each new spawn. Logs are not compressed
(cat/grep during triage). Operators that previously used `kubectl exec`
into a wrapper's heartbeat subshell to inspect a stuck phase now read
the retained `spawn-<N>.log` files (feedback Q2).

### Orphan-commit detector (AC-R6)

A pod can exit after `git commit M` but before its `propose`/`ack`/
`nack` finishes, leaving commits with no covering BRC verb. The
`OrphanCommitDetector` runs once per `OnDemandSpawner.tick` **before**
`derive_next_action` for each role: it reads the role's
`last_reviewed_commit_sha` from
`.egg-state/agent-outputs/<role>/brc-memory.md`, calls
`gateway_client.git_log_range(base=sha, head=branch)`, and
cross-references the tracker's events for the role. If commits exist
with no covering `CONSENSUS_PROPOSE` it fires
`OVERSEER_ALERT(anomaly="orphan_commit_post_spawn")` and re-spawns the
role with a prompt instructing it to propose covering exactly those
commits (no new code work).

### Atomic `brc-memory.md` writes (AC-R12)

The durable BRC memory at
`.egg-state/agent-outputs/<role>/brc-memory.md` is read by
`compose_event_prompt`; under a single-pod-in-flight regression two
pods could write it concurrently. The `brc_ack` / `brc_nack` handlers
in `sandbox/egg_agent_tools/handlers/brc_memory.py` write to
`brc-memory.md.tmp.<pid>` then `os.rename` to final (POSIX atomic
rename on the per-role PVC filesystem) so any unobserved concurrency
window produces one complete write, never a partial concatenation.

### Verification stance — unit + integration

The on-demand spawn path is covered at both the unit and integration
levels:

- **Unit.** `PhaseIdleBudgetTimer`, `OnDemandSpawner` (mocked spawner +
  tracker), `OrchestratorSessionKeepAlive` (mocked gateway),
  `KubernetesSpawner.create_on_demand_spawn_fn` (mocked k8s API), and
  the in-process / HTTP route parity of `derive_next_action`.
- **Integration.** The orchestrator run loop drives one full BRC cycle
  (propose → ack → confirm) for a role, asserting exactly one spawn per
  actionable event, zero spawns on wait, a bare
  `python3 -m egg_agent` command (no `bash -c`), per-spawn log
  retention, and the phase-level idle alert at 30 min of no spawns.
- **Restart resilience.** Orchestrator restart mid-phase; the
  reconstruct-from-messages fallback at
  `routes/consensus.py:111-154` still works; the tick re-derives next
  action and spawns whatever was actionable; session keep-alive is
  re-populated by `record_phase_start` on resume.
- **No BRC behaviour change.** The per-event prompt is byte-identical
  to the pre-#3023 wrapper-rendered prompt for the same
  `(role, event_payload)` pair; the prefix-cache hit rate is preserved
  (validated against the LiteLLM cost/cache logger).

### Rollback plan — drain-then-revert (cq-4 big-bang)

#3023 ships big-bang with no feature flag (cq-4). The supported
regression path is therefore a **drain-then-revert** protocol that
operators run before `git revert` on `main`:

```bash
egg-orch pipeline list --running
# For each running pipeline:
#   - if it can finish in <T hours, let it run to completion
#   - else: egg-orch pipeline cancel <id> --reason "pre-revert drain"
# Then `git revert` the merge commit on main; the reverted
# orchestrator's pod-state reader (TASK-3-5) tolerates either lifecycle
# so freshly spawned pipelines work.
```

The tolerant pod-state reader is the key robustness primitive: after
revert, in-flight pipelines may have on-demand pods or no pod at all
between events. When no Job with the role label is Running for a
(pipeline, role) **and** the BRC tracker has a non-empty event history
for that role, the startup reconciliation treats the pipeline as
"on-demand in-flight" and re-derives next-action instead of marking it
failed.

### Follow-up issues

The closeout PR notes two follow-up issues that were intentionally
deferred:

- **Overseer agents (cq-6)** — `orchestrator/overseer/monitor.py` spawns
  decision-maker / advisor agents on an anomaly + periodic cadence, not
  via `concurrent_executor.spawn_all`. They are arguably already
  on-demand. Unifying them onto the same `OnDemandSpawner` is the
  cleaner long-term shape but is out of scope for #3023; a follow-up
  issue captures the unification target.
- **`spawn_all` rename** — after slice 3 the method no longer spawns the
  long-lived wrapper pods; it only registers the tracker and seeds the
  pure-producer auto-ACK. The name is misleading but renaming it would
  produce a noisy rename storm; the follow-up issue tracks the rename
  separately.

### Operator-facing env vars (cross-link)

The on-demand spawn path adds five operator-visible env vars and
retires one. The env-var table is in the
[Environment Variables](#environment-variables) section below; the
short summary:

- `EGG_PHASE_IDLE_BUDGET_OWNER=orchestrator` — coexistence guard
  (slice 1) that silences the in-pod wrapper's idle alert while the
  long-lived wrapper still ships. Dead code after slice 3.
- `EGG_EVENT_LOOP_OWNER=orchestrator` — coexistence guard (slice 2)
  that makes the still-spawned wrapper pod passive (heartbeat-only) so
  exactly one path emits BRC verbs during burn-in. Dead code after
  slice 3.
- `EGG_ON_DEMAND_SPAWN=1` — set on every per-event spawn so the agent
  process can tell it was launched by the on-demand path.
- `EGG_SPAWN_INDEX=<N>` — per-role event index used to name
  `spawn-<N>.log`.
- `EGG_SPAWN_LOG_RETENTION` — operator override for
  `OnDemandSpawner.PER_ROLE_LOG_RETENTION = 20`.

`EGG_BRC_IDLE_BUDGET_MIN` is **retained verbatim** as the
orchestrator-side phase-idle-budget threshold (cq-3 — same threshold,
same anomaly tag, same priority semantics; per-role granularity is
replaced by the structured `per_role_state` payload).

The [agent-wait-patterns §10](../reference/agent-wait-patterns.md#10-brc-on-demand-agent-spawning)
section remains the wait-side companion to this architecture
description.

## BRC Per-Event Prompt Composer + Preamble Collapse

> **Landed across slices 1/3/4 of [#2908](https://github.com/jwbron/egg/issues/2908) and re-homed by [#3023](https://github.com/jwbron/egg/issues/3023).**
> Slice-3 of #2908 added the per-event composer; slice-4 flipped
> `EGG_BRC_MEMORY` from `write-only` to `full` so the composer reads
> the memory file in production. #3023 retired the wrapper that was the
> composer's caller and re-homed the composer call into the orchestrator's
> per-phase tick (the `OnDemandSpawner.tick` path). The composer signature,
> envelope shape, and output bytes are unchanged across the #3023 cutover
> — the integration test asserts byte-identity against a pre-#3023
> wrapper-rendered fixture for the same `(role, event_payload)` pair.
>
> **What's gated by what:** the composer always runs (the orchestrator's
> on-demand spawn path is the only path; see
> [BRC On-Demand Agent Spawning](#brc-on-demand-agent-spawning)).
> Only the *content* of the memory excerpt (and whether
> `last_reviewed_commit_sha` is read from the memory file vs. fallen
> back from `changed_artifacts`) is gated by `EGG_BRC_MEMORY`. The
> `_build_brc_preamble` collapse runs **unconditionally** for every
> agent spawn. See [Composer interplay with
> `EGG_BRC_MEMORY`](#composer-interplay-with-egg_brc_memory) for the
> full matrix.

### Per-event prompt composer (`compose_event_prompt`)

`compose_event_prompt(role, event_payload, memory_excerpt, nacks,
git_log_delta, base_branch) -> str` builds the one-shot user prompt the
orchestrator hands the agent at each on-demand spawn. Under #3023 the
composer is called in-process by `OnDemandSpawner.tick` once
`derive_next_action` returns `propose|ack|nack|confirm` for the role
(see [On-demand spawn loop](#on-demand-spawn-loop) above); the composer's
disk-side inputs (`memory_excerpt`, `git_log_delta`) resolve against the
orchestrator's view of the per-role worktree PVC. It assembles, in
order:

| Position | Section | Source | Bound |
|----------|---------|--------|-------|
| Top | Role banner + one-line event description | `role` + `event_payload.kind` | A few hundred bytes; identifies the producer/reviewer side of the dispatch. |
| Middle | NACK payload (per-reviewer NACK with `reason` + `artifact_refs`) | `orchestrator/peer_consensus.py` `_open_nacks_barrier_response.nacks[]` (line numbers come from the slice-3 contract spec and are drift-prone — prefer the function-name reference; the function span is around lines 949–1046 in practice) — the same envelope the producer sees on the aggregated-NACK 409 (see [§10.6](../reference/agent-wait-patterns.md#106-409-stale_version--aggregated-nack-are-event-pump-signals-not-transient-errors)). | One section per reviewer that NACKed the current version; rendered with reason text + artifact references. |
| Middle | Single expected action | `event_payload.kind` | A few hundred bytes; states whether the agent should review, fix, confirm, etc. |
| Tail | Per-producer git-log delta | `git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p` ← `last_reviewed_commit_sha` from the [BRC memory file](brc-memory.md) | Scaled by actual change size; **NOT** counted against the 10 KB envelope. |
| Tail | Memory excerpt | `.egg-state/agent-outputs/<role>/brc-memory.md` (slice-1 writer); truncated when the excerpt exceeds 2 KB | ≤ 2 KB after truncation. |

The composer is invoked per role: producer prompts carry an `INVOKE` ↦
`address NACKs and re-propose`; reviewer prompts carry an `INVOKE` ↦
`review the current proposal`; dual-role agents (e.g. `tester`) get
one prompt per side per invocation in the order the orchestrator
dispatches them.

The **prompt envelope is bounded at ≤ 10 KB** for the prose
(role banner + event description + NACK payload + memory excerpt
section header + the single action). The git-log delta is
intentionally **excluded** from the envelope cap because its size
scales with the actual change set — capping it would defeat the
adversarial re-review contract (see next subsection). The 2 KB
memory-excerpt cap is enforced inside the prompt envelope and is the
inner-loop bound; the unbounded git-log delta is appended after the
envelope is composed.

### The git-log delta is the **full** per-producer diff, by design

The git-log delta is delivered verbatim with the per-producer
`last_reviewed_commit_sha` substituted:

```
git log {last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p
```

This is intentionally the **full delta since the prior review**, not
a shortcut to the orchestrator's signal-level `changed_artifacts`
set. The rationale traces to
[`shared/prompts/REVIEWER-SYNC.md` Diff command (re-review / delta)](../../shared/prompts/REVIEWER-SYNC.md):
the BRC re-review must audit the producer's v2+ delta **as a fresh
review** — comparing the new change set against the prior reviewed
SHA — or the stateless event-pump systematically weakens adversarial
re-review (risk_analyst R6 of the slice-3 plan). The
`changed_artifacts` set is the orchestrator's notion of "what
changed", which is necessarily a subset (or sometimes a misclassified
subset) of the producer's actual `git log` delta; a reviewer that
audits only `changed_artifacts` will miss latent changes the
producer's commit graph carried without surfacing on the signal
envelope. The same diff command appears as the PR-reviewer's
re-review delta (`action/build-review-prompt.sh`) — both surfaces
align on full-delta semantics per the REVIEWER-SYNC contract.

The unit-test contract in
`orchestrator/tests/test_compose_event_prompt.py` (slice-3 task-3-6)
includes a regression assertion that the emitted git-log command
string contains `{last_reviewed_commit_sha}..HEAD --not
origin/{base_branch}` verbatim and does **not** degrade to a
`changed_artifacts`-only shortcut.

### Memory delivery — inline at user-prompt tail (architect od-6 Option B)

The memory excerpt rides at the **tail** of the user prompt, not as a
system-prompt prefix and not as a separate flag. This is architect
**open-decision od-6 Option B**: the per-event composer concatenates
the excerpt onto the prompt string and the orchestrator invokes
`python3 -m egg_agent` with the composed prompt piped on stdin exactly
as it would without memory.

The plan analysis pseudocode illustratively mentioned a
`--append-context` flag on `build_agent_command`; **that flag does
not exist** on the real `build_agent_command` surface (verified at
`shared/egg_agent/command.py:11-46`). Option B sidesteps the missing
flag without requiring a net-new CLI surface in slice-3. Option C —
a net-new `--memory-file` flag — is documented as the explicit
fallback in the slice-3 plan but is **not** the chosen path; reviewers
inspecting the implementation should expect tail-position inline
concatenation, not a CLI flag.

### Composer interplay with `EGG_BRC_MEMORY`

The composer is the **reader** complement to the slice-1 [BRC Memory
Artifact](brc-memory.md) **writer**. The slice-1 default
(`EGG_BRC_MEMORY=write-only`) keeps the writer hot and the reader
inert; the slice-3 / slice-4 default (`EGG_BRC_MEMORY=full`) turns the
reader on so the composer reads the memory file's per-producer
`last_reviewed_commit_sha` and substitutes it into the git-log delta:

| `EGG_BRC_MEMORY` | Composer behaviour |
|------------------|--------------------|
| `off` | Reader inert; `memory_excerpt = ""`. The composer falls back to the orchestrator's signal-level `changed_artifacts` as a baseline for the git-log delta when no per-producer SHA is available — strictly a degraded baseline, not the adversarial re-review path. |
| `write-only` (slice-1 rollout default; opt-in regression path after slice-4) | Writes happen; reads are no-ops. `memory_excerpt = ""` even though the file exists, preserving the inert read behaviour from the slice-1 rollout window. |
| `full` (**default after slice-4**) | Composer reads `.egg-state/agent-outputs/<role>/brc-memory.md`, extracts the per-producer `last_reviewed_commit_sha`, substitutes it into the git-log delta, and includes the truncated memory excerpt at the prompt tail. |

The default stayed `write-only` through slice-3 — operators opted into
`full` per pipeline / per pod during the slice-2/-3 rollout window.
Slice-4 flipped the default to `full` so production pipelines run the
adversarial re-review path; operators that need to fall back to the
slice-1 inert-reader behaviour can still set `EGG_BRC_MEMORY=write-only`
explicitly.

### Open-decision resolutions

The slice-1, slice-2, and slice-3 implementation collectively resolves
the architect's open decisions for the #2908 redesign. The
resolutions are cited so a future reviewer touching the surrounding
subsystem can locate the implementation:

- **od-1 — subdirectory layout (`.egg-state/agent-outputs/<role>/brc-memory.md`)
  + fail-closed path constructor.** Resolved by slice-1's writer
  (`sandbox/egg_agent_tools/handlers/brc_memory.py`); the path
  constructor raises on empty `EGG_AGENT_ROLE` per risk_analyst R14.
  See [BRC Memory Artifact — File path / Fail-closed path constructor](brc-memory.md#file-path).
- **od-2 — distill-on-write decision-log cap at 20 entries.** Resolved
  by slice-1's writer (decision log truncated to last 20 on every
  write). The alternative (append-only with prompt-side truncation)
  was ruled out because `claude -p` does not currently expose the
  prompt-construction control that would let the reader-side
  enforce the cap. See [BRC Memory Artifact — Decision-log cap
  (distill-on-write)](brc-memory.md#decision-log-cap-distill-on-write).
- **od-3 — `egg-orch brc next-action` is a new dedicated endpoint, not
  a derived view of `consensus status`.** Resolved by slice-1's
  `orchestrator/routes/consensus.py` route handler. The sequencing
  logic (WAIT / INVOKE / CONFIRM dispatch derived from
  `consensus_status` + `_open_nacks_barrier_response.nacks[]` +
  `changed_artifacts`) lives in testable orchestrator-side Python.
  After #3023 the orchestrator calls `derive_next_action` in-process
  inside the per-phase tick (no CLI hop); the `egg-orch brc next-action`
  CLI surface is retained for operator debugging and as the external
  integration surface that pre-#3023 wrapper bash and external tools
  consumed.
- **od-4 — 30-minute idle / no-progress safety budget replacing the
  3-restart FAIL cap.** Resolved by #2908 slice-2's
  `EGG_BRC_IDLE_BUDGET_MIN` (default `30`); ownership moved to the
  orchestrator under #3023 (`PhaseIdleBudgetTimer`) with the same
  threshold and anomaly tag, replacing per-role granularity with the
  structured `per_role_state` payload. 30 minutes sits well above the
  WS7-observed 10–13 min idle ceiling on real BRC phases. See
  [Phase-level idle-budget alert (cq-3)](#phase-level-idle-budget-alert-cq-3).
- **od-5 — persistent `egg-orch` daemon vs. per-invocation CLI.**
  Deferred past slice-3 (slice-6's MCP→CLI deletion captures the
  latency baseline; the daemon decision is gated on the post-deletion
  latency regression measurement). Documented here as deferred so a
  reviewer hitting this in the surrounding subsystem knows it's still
  open.
- **od-6 — memory delivered inline at user-prompt tail (Option B), not
  via a `--memory-file` flag.** Resolved by slice-3's
  `compose_event_prompt`. See [Memory delivery — inline at
  user-prompt tail (architect od-6 Option B)](#memory-delivery--inline-at-user-prompt-tail-architect-od-6-option-b)
  above.

### `_build_brc_preamble` collapse

`_build_brc_preamble` (defined in `orchestrator/routes/pipelines.py`)
is **collapsed** by #2908 slice-3: the STAY-ALIVE / wait-loop mechanics /
cursor-threading / pre-confirm-wait foot-gun guidance that previously
taught the agent the lifecycle has been deleted. The orchestrator
owns sequencing (see [On-demand spawn loop](#on-demand-spawn-loop))
so the agent no longer needs to be re-taught the lifecycle on every
spawn.

> The specific line-number references below come from the slice-3
> contract spec and reflect the **post-collapse** positions inside
> `pipelines.py` once task-3-3's coder commit lands; they may not
> match the pre-collapse positions. Prefer the **function /
> banner-name references** (`_build_brc_preamble`, the dual-mandate
> banner) over the line numbers when reading the live file, and treat
> the numbers as anchor cues for the snapshot regression test rather
> than load-bearing citations.

| Removed from preamble | Why |
|-----------------------|-----|
| Producer Lifecycle step 4 wait-loop plumbing | The orchestrator holds the wait. |
| Producer Lifecycle step 6 STAY-ALIVE loop | The orchestrator's per-phase tick re-derives `next-action` on the next iteration instead. |
| Cursor / `--since` threading guidance | Cursor threading is automatic and orchestrator-internal under the on-demand spawn path. |
| Pre-confirm-wait foot-gun guidance (anti-pattern 5) | The orchestrator never spawns the producer for a `wait` action that would self-deadlock — the regression cannot be reached at all under on-demand spawning. |

| Kept in preamble | Why |
|------------------|-----|
| Agent roster + reviewer/producer assignments | Per-event prompts assume the agent already knows who else is in the room. |
| Dual-role ordering banner | Dual-role agents (e.g. `tester`) still need the ordering invariant — the orchestrator dispatches both sides, but the agent must know to address them in the documented sequence. |
| Dual-mandate adversarial re-review banner (`_build_brc_preamble`'s "Your re-review has TWO equal-weight mandates …" block at `orchestrator/routes/pipelines.py:12561-12573` post-collapse) | Behavioural framing for re-review correctness; anchored on by risk_analyst R6 and not a wait-mechanics concern. |

The three caller sites at `orchestrator/routes/pipelines.py:13659`,
`:13692`, `:13720` (post-collapse positions per the slice-3 contract
spec; prefer the `_build_brc_preamble` function name as the
navigation anchor since line numbers drift with surrounding edits)
are **unchanged** by the collapse — only the preamble text shrinks,
the calling pattern is identical. The collapse runs
**unconditionally** at every agent spawn: the on-demand spawn path
(see [BRC On-Demand Agent Spawning](#brc-on-demand-agent-spawning)) is
the only path, and the collapsed preamble is the only preamble the
agent sees.

The snapshot regression test at
`orchestrator/tests/test_brc_preamble_collapsed.py` (slice-3 task-3-7)
pins (a) absence of STAY-ALIVE / wait-loop / cursor strings;
(b) presence of the agent roster; (c) presence of the phrase "Both
must pass to ACK" (located inside the dual-mandate banner at
`pipelines.py:12567`, post-collapse — again, prefer the banner
substring "Both must pass to ACK" as the anchor since the line
number drifts with surrounding edits); (d) a ≥ 25% byte-size drop
against the pre-collapse baseline (a softening from the
originally-proposed 40% per the reviewer_plan v2 non-blocker — the
precise number is set by the snapshot baseline rather than a
pre-fixed target).

### `mission.md` rewrite reaches the agent pod only after a sandbox rebuild

The sandbox runtime loads its agent rules from the Dockerfile-baked
copy at `/opt/claude-rules/` (see `sandbox/entrypoint.py:967`
`_CLAUDE_RULES_DIR`), populated at image build time by
`sandbox/Dockerfile:212-214` `COPY sandbox/claude-rules/*.md`. In the
working tree, `sandbox/claude-rules` is a **symlink** to
`sandbox/agent-config/rules`, so editing one path edits both — the
`diff sandbox/agent-config/rules/mission.md sandbox/claude-rules/mission.md`
acceptance assertion holds trivially because the two paths resolve
to the same file in git. The slice-3 mission.md rewrite to the
event-handler contract (task-3-4) only reaches the agent pod after:

```bash
make build        # rebuild egg-sandbox / egg-orchestrator / egg-gateway / egg-litellm
make k3s-import   # import rebuilt images into k3s
make deploy       # roll out deployments in egg-system
```

(see [Deployment guide — Claude binary not found](../guides/deployment.md#claude-binary-not-found)
for the canonical rebuild sequence; the same triplet drives any
`sandbox/claude-rules/*.md` content change). The slice-4 default flip
was gated on this rebuild having shipped — operators verified the new
image tag was deployed before slice-4 landed so pods would not run the
post-deletion wrapper against a pre-rewrite preamble.

### Composer / preamble verification stance

The composer and preamble collapse ship with **unit / snapshot tests
only**, matching the [on-demand spawn verification stance](#verification-stance--unit--integration)
and anchored in the same [#2474](https://github.com/jwbron/egg/issues/2474)
trust-boundary boundary:

- `orchestrator/tests/test_compose_event_prompt.py` covers each
  role's prompt shape, the 2 KB memory-excerpt truncation, the NACK
  delta with 0 / 1 / 2+ reviewers, the verbatim git-log delta command
  emission (regression-trap against the `changed_artifacts`-only
  shortcut), and the ≤ 10 KB envelope assertion per case.
- `orchestrator/tests/test_brc_preamble_collapsed.py` pins the
  collapsed preamble (snapshot equality + absent-strings + byte-size
  drop) at all three caller sites.
- End-to-end validation against the #2906 qwen3.7-max repro is owned
  by `egg_stack`, per the same trust-boundary reasoning that pinned
  the slice-2 stance and now defines the steady-state contract.

### Operator-facing env vars (memory cross-link)

`EGG_BRC_MEMORY` is the operator flag for the memory writer/reader; it
is documented in the
[BRC Memory Artifact — Modes](brc-memory.md#modes--egg_brc_memory)
table together with the slice-1 writer and the composer's reader
behaviour. The wait-side companion to this architecture section is
[agent-wait-patterns §10.9 BRC Per-Event Prompt Composer +
Preamble Collapse](../reference/agent-wait-patterns.md#109-brc-per-event-prompt-composer--preamble-collapse).

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
| `EGG_ORCH_MAX_PARALLEL_SLICES` | Slice-DAG: per-pipeline slice spawn concurrency cap, fallback default (#2137). The per-pipeline `PipelineConfig.max_parallel_slices` field (set at creation) takes precedence when non-null. | `1` |
| `EGG_ORCH_GLOBAL_MAX_PARALLEL_SLICES` | Slice-DAG: orchestrator-process-wide cap on slices in flight across **all** running pipelines (#2241). Each slice spawns ~8 containers; the default of 4 reflects the observed host saturation ceiling. Slices that exceed the cap stay READY and re-yield next poll tick. Per-process — HA replicas each maintain their own counter. | `4` |
| `EGG_ORCH_SLICE_LOCAL_MAX_CYCLES` | Slice-DAG: per-slice BRC re-proposal ceiling before HITL escalation (#2137) | `3` |
| `EGG_ORCH_SLICE_GLOBAL_MAX_CYCLES` | Slice-DAG: pipeline-wide summed slice-cycle cap (#2137) | `10` |
| `EGG_ORCH_SLICE_FAILURE_GRACE_SECONDS` | Slice-DAG: grace window before failure-cascade marks downstream subtree `BLOCKED_ON_FAILED_DEPENDENCY` (#2137) | `60.0` |
| `EGG_ORCH_STACKED_PR_RECONCILER_INTERVAL_SECONDS` | Slice-DAG: stacked-PR reconciler polling cadence for orphaned child PRs (#2137) | `30.0` |
| `EGG_BRC_EVENT_PUMP` | **Removed in [#2908](https://github.com/jwbron/egg/issues/2908) slice-4 task-4-2.** During the slice-2/-3 rollout this flag selected between the legacy capped-restart wrapper bash template (`false`) and the new event-pump wrapper bash template (`true`). Slice-4 deleted the legacy template, the surrounding selector logic, and the env var read itself — the orchestrator no longer consults this variable. The supported regression path is now [#3023](https://github.com/jwbron/egg/issues/3023)'s drain-then-revert protocol (see [Rollback plan — drain-then-revert (cq-4 big-bang)](#rollback-plan--drain-then-revert-cq-4-big-bang)); reverting #3023 restores the wrapper bash and the #2908 lineage on top of it. | n/a (removed) |
| `EGG_BRC_IDLE_BUDGET_MIN` | Phase-level idle / no-progress safety budget in minutes ([#2908](https://github.com/jwbron/egg/issues/2908) introduced the wrapper-side timer at this threshold; [#3023](https://github.com/jwbron/egg/issues/3023) moved ownership to the orchestrator's `PhaseIdleBudgetTimer` with the same threshold and anomaly tag). At budget threshold the orchestrator emits `mcp__progress__overseer_alert` (anomaly `stuck-phase-transition`, priority `medium`); at `2 ×` budget the priority escalates. HITL-pending suppression (AC-R13): when `pending_hitl_count > 0` the 1× alert downgrades to `priority=low` with pending HITL IDs in `reason`, and the 2× alert is suppressed. The alert never transitions the pipeline to FAILED. Default 30 min is well above the WS7-observed 10–13 min idle ceiling on real BRC phases. See [Phase-level idle-budget alert (cq-3)](#phase-level-idle-budget-alert-cq-3). | `30` |
| `EGG_PHASE_IDLE_BUDGET_OWNER` | [#3023](https://github.com/jwbron/egg/issues/3023) slice-1 coexistence guard. Set unconditionally to `orchestrator` by `concurrent_executor._spawn_agent` so the still-spawned wrapper pod's idle-alert emitter short-circuits and only the orchestrator-side `PhaseIdleBudgetTimer` fires. Dead code after slice 3 (the wrapper is deleted). Operators do not set this. | `orchestrator` |
| `EGG_EVENT_LOOP_OWNER` | [#3023](https://github.com/jwbron/egg/issues/3023) slice-2 coexistence guard. Set unconditionally to `orchestrator` by `concurrent_executor._spawn_agent` so the still-spawned wrapper pod's event-pump loop turns into a passive heartbeat-only sleep (never calls `brc next-action`, never invokes the agent). This ensures exactly one path emits BRC verbs during burn-in. Dead code after slice 3. Operators do not set this. | `orchestrator` |
| `EGG_ON_DEMAND_SPAWN` | [#3023](https://github.com/jwbron/egg/issues/3023) slice-2 marker set on every per-event spawn so the agent process can tell it was launched by the on-demand path. Read by `shared/egg_agent/__main__.py` for the SIGTERM trap and per-spawn log routing. | `1` (on per-event spawns) |
| `EGG_SPAWN_INDEX` | [#3023](https://github.com/jwbron/egg/issues/3023) slice-2 per-role event index injected by `OnDemandSpawner`. The agent writes stdout/stderr to `.egg-state/agent-outputs/<role>/spawn-<EGG_SPAWN_INDEX>.log` on the per-role worktree PVC. Increments per spawn. Operators do not set this. | per-spawn `<N>` |
| `EGG_SPAWN_LOG_RETENTION` | [#3023](https://github.com/jwbron/egg/issues/3023) slice-2 override for `OnDemandSpawner.PER_ROLE_LOG_RETENTION` — the number of `spawn-<N>.log` files kept per role before older files rotate out. | `20` |

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

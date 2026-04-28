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
- Cleaned up via `git worktree prune` on first access (not container startup)

This differs from agent worktrees (managed by the gateway for agent isolation). The orchestrator manages its own state worktree independently.

**Startup reconciliation:**

On orchestrator restart, orphaned container state is automatically recovered:

1. **RUNNING pipelines**: For each pipeline showing `status=RUNNING`, the reconciliation process recovers orphaned state:
   - **Un-spawned PENDING phase** (crash-between-submit-and-spawn): If the current phase is `PENDING` with `started_at=None` and no containers or agents, the orchestrator crashed before `_run_pipeline` reached `executor.spawn_all`. The pipeline is immediately marked `FAILED` with an actionable message; no container scanning is performed for this pipeline. Operators restart via `POST /pipelines/{id}/start`.
   - Scans only the **current phase** for stale containers. This applies when the phase has containers or agents — the un-spawned PENDING case above is handled separately. Containers from prior phases are intentionally terminated and their absence is expected — checking all phases caused false `FAILED` transitions when the orchestrator restarted mid-pipeline.
   - Any agent in the current phase whose pod is absent from the live Kubernetes pod set is marked `FAILED`.
   - If at least one stale entry is found, the pipeline itself is marked `FAILED` with an error message instructing operators to restart via `POST /pipelines/{id}/start`.

2. **AWAITING_HUMAN pipelines**: For each pipeline showing `status=AWAITING_HUMAN` with no pending decisions (orphaned after a restart where the decision was already resolved), the pipeline is marked `FAILED` with an error message instructing operators to restart via `POST /pipelines/{id}/start`. The restart endpoint will automatically recover by parsing the latest phase_gate resolution and either advancing to the next phase (approved) or resetting the current phase for re-run (request_changes/change_approach).

3. **Concurrent pipeline consensus trackers**: For each `RUNNING` pipeline (i.e., those not already marked `FAILED` by step 1) in a concurrent phase, the in-memory `PeerConsensusTracker` (which does not persist to disk) is reconstructed by replaying `CONSENSUS_*` messages from the message store in timestamp order. This allows agents that are mid-consensus to resume correctly after a restart rather than looping. Reconstruction is best-effort — if no consensus messages are found, the tracker is left absent and agents will encounter an empty consensus state (handled by the wrapper's message-bus fallback).

This prevents pipelines from being stuck in `RUNNING` or `AWAITING_HUMAN` states indefinitely after a crash. Operators (or CI systems) can detect the `FAILED` status and restart the pipeline using the existing restart endpoint, which preserves worktrees and phase state while re-spawning containers.

See `orchestrator/state_store.py` and `orchestrator/startup_reconciliation.py` for implementation details.

**Runtime pod monitoring:**

A background `KubernetesMonitor` thread runs continuously after orchestrator startup to detect agent pod failures during execution. The monitor periodically checks pod status via the Kubernetes API and invokes registered handlers when state changes occur (pod exits, fails, or becomes unhealthy).

A pipeline reconciliation handler detects when agent pods exit or fail during runtime and updates pipeline state accordingly. The handler scans **all phases** within each `RUNNING` or `AWAITING_HUMAN` pipeline (including completed phases) to find the exited pod, as reviewer agents may continue running after their phase has transitioned to `COMPLETE`. Terminal pipelines (`FAILED`, `COMPLETE`, `CANCELLED`) are also scanned if they still have stale `RUNNING` agent or container records — an optimization that skips the common case where no cleanup is needed.

When a container running an agent exits with a non-clean code, the handler marks the container as `FAILED` and marks the owning agent as `FAILED` with an error message — unless the agent is already `COMPLETE` (i.e., it completed via BRC consensus), in which case the exit is ignored. The handler only updates agent and container sub-records; the pipeline's top-level `status` is never mutated by the monitor (see #2210). The pipeline-level decision about whether the pipeline itself has failed lives in the BRC poll loop in `_run_concurrent_phase`, which has full consensus context the monitor lacks. Containers that exit with code 0 (graceful exit after BRC protocol work) or 143 (orchestrator-initiated SIGTERM during phase teardown) emit a `STOPPED` event and do not trigger failure reconciliation. When BRC consensus completes, the concurrent phase runner proactively marks agent containers as `EXITED` with exit code 0 so that subsequent monitor sweeps treat them as clean exits; the agent-COMPLETE check is a secondary defense for the event window before that update is persisted. This complements startup reconciliation by catching failures that occur during execution rather than only on orchestrator restart.

Additionally, the concurrent phase runner (`_run_concurrent_phase`) treats consensus completion (`is_complete=True`) as the **authoritative success signal**. If all agents have confirmed consensus but some containers exited with non-zero codes (e.g., the consensus wrapper exhausted restarts before the final consensus check could run), the phase runner logs a warning about the prior failures but returns success (exit code 0). This prevents the compounding failure scenario where successful BRC consensus is overridden by stale container exit codes. A final `check_consensus()` recheck is also performed on the all-container-exit path (step 5) before returning failure, closing a race window where the step-2 consensus check reads stale tracker state and step 5 returns `exit_code=1` without verifying that consensus is genuinely incomplete.

In addition to the event-driven handler, `ContainerMonitor.start_periodic_reconciliation()` runs a second background thread that sweeps every 30 seconds for stale containers that may have exited between Docker events (e.g., missed events during a partial restart). This periodic sweep checks the **current phase** of each `RUNNING` or `AWAITING_HUMAN` pipeline, and also of any terminal pipeline that still has stale `RUNNING` records. On non-`RUNNING` pipelines it reconciles only the sub-records (agent/container status), leaving the pipeline's top-level status unchanged. Like the event-driven handler, the sweep inspects the actual container exit code before reconciling: containers that exited with code 0 or 143 (clean exit — e.g., the consensus wrapper completing gracefully, or an orchestrator-initiated SIGTERM during phase teardown) are skipped and do not trigger `FAILED` reconciliation. The first sweep is intentionally delayed by one interval since startup reconciliation already ran immediately before the thread was started.

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

## Per-Pipeline Worktrees

The orchestrator reads pipeline artifacts (verdict files, draft documents, check results) from per-pipeline worktrees created by the gateway. These worktrees isolate work for each pipeline and are separate from both the orchestrator's state worktree and the main repository working directory.

**Architecture:**
- Gateway creates worktrees at `/home/egg/.egg-worktrees/{job-name}/{repo-name}/` (one per agent)
- Each agent pod mounts its own worktree via hostPath and writes artifacts to it
- All agents in a pipeline push to the same shared branch (e.g., `egg/issue-{N}`)
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

2. **Fetch from remote** (inbound): Before starting pipeline phases, the orchestrator syncs the local worktree with the remote branch via `_sync_worktree_with_remote()`. This handles orchestrator restarts where the local worktree branch lags behind origin: commits pushed by agents in previous phases (contracts, drafts, statefiles) exist on the remote but not in the local checkout. The function performs a gateway-authenticated fetch (`GatewayClient.fetch_worktree_branch()`) followed by a local `git reset --hard origin/<branch>`. The sync behavior depends on the prior phase's outcome:

   - **Prior phase succeeded, local ahead:** Local commits are pushed to remote first, preserving completed work, then the worktree is reset.
   - **Prior phase failed, local ahead:** Local commits are discarded and the worktree is reset to remote (removes incomplete work from a failed/killed agent).
   - **Diverged (local and remote both have unique commits):** A fast-forward merge is attempted. If the merge fails, the worktree is left unchanged and the error is logged (may require manual intervention).
   - **Local behind or in-sync:** Standard reset to remote tip.

   The operation is best-effort and idempotent — it skips gracefully when the remote branch doesn't yet exist (first pipeline run) or when fetch fails.

3. **Cleanup on deletion**: When a pipeline is deleted, the orchestrator cleans up remote worktree branches (`egg/{container_id}/work`) for all containers across all phases using `GatewayClient.delete_remote_branch()`.
   This prevents accumulation of dangling branches on the remote.
   Branch deletion is best-effort and logged as warnings on failure — deletion failures do not block pipeline removal.

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

Available MCP tools (orchestrator-backed): `submit_task`, `get_status`, `wait_for_status_change`, `provide_input`, `list_tasks`, `cancel_task`, `check_health`, `list_containers`, `get_container_logs`, `send_message`, `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`, `validate_config`, `restart_agent`, `restart_phase`, `advance_phase`, `start_phase`, `complete_phase`, `populate_contract`

The `wait_for_status_change` tool is the event-triggered sibling of `get_status` and is the canonical host-side poll vehicle for the SDLC skill (issue [#1932](https://github.com/jwbron/egg/issues/1932)). It blocks server-side for up to 25 s and returns immediately on a phase transition, terminal pipeline state, new HITL `DECISION_CREATED`, new `OVERSEER_ALERT`, or consensus message (`CONSENSUS_CONFIRMED` / `CONSENSUS_NACK` / `CONSENSUS_RE_REVIEW`). Callers thread the response `cursor` (opaque `msg:<id>|evt:<seq>` shape) into the next call's `since` to close the snapshot→wait race window. See [Host-Side Waits](../reference/agent-wait-patterns.md#7-host-side-waits--wait_for_status_change) for the full envelope contract and concurrency model.

Available MCP tools (gateway-backed, requires `gateway_url`): `list_checkpoints`, `search_checkpoints`, `get_contract`

The gateway-backed checkpoint tools (`list_checkpoints`, `search_checkpoints`) accept an optional `repo` parameter to specify the checkpoint repository in `owner/repo` format (e.g., `owner/repo-checkpoints`). When provided, this is forwarded as the `source_repo` query parameter to the gateway checkpoint endpoint. The `get_contract` tool also uses the gateway session but does not require the `repo` parameter.

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

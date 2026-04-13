# Pipeline Health Monitoring

Pipeline health monitoring uses a **two-tier architecture** to detect and respond to agent failures during pipeline execution. The orchestrator tier handles clear-cut failures deterministically (no LLM cost), while the overseer agent tier handles ambiguous situations requiring semantic analysis.

## Architecture Overview

```
Agent containers emit structured progress events
  │
  ▼
┌───────────────────────────────────────────────────────────────┐
│ Tier 1: Orchestrator (Deterministic)                          │
│                                                               │
│  Structured progress events → Tripwire rules → Auto-action    │
│  • Heartbeat timeout      → Escalate to overseer/HITL         │
│  • Container exit         → HITL escalation                   │
│  • Repeated errors (N×)   → Escalate to overseer              │
│  • Message volume spike   → Auto-throttle                     │
│  • Progress stall         → Escalate to overseer/HITL         │
│  • BRC progress stall     → Escalate (post-ACK timeout)       │
│                                                               │
│  Ambiguous cases ──────────────────────┐                      │
└────────────────────────────────────────┼──────────────────────┘
                                         ▼
┌───────────────────────────────────────────────────────────────┐
│ Tier 2: Overseer Agent (LLM-Powered)                          │
│                                                               │
│  ┌────────────────────┐    ┌────────────────────────────┐     │
│  │ Haiku Classifiers  │───►│ Sonnet/Opus Decision-Maker │     │
│  │                    │    │                            │     │
│  │ • Stall vs. work   │    │ • Compose redirect msgs    │     │
│  │ • Loop detection   │    │ • Decide escalation level  │     │
│  │ • Error triage     │    │ • File diagnostic issues   │     │
│  │ • Off-track check  │    │ • HITL escalation          │     │
│  └────────────────────┘    └────────────────────────────┘     │
└───────────────────────────────────────────────────────────────┘
```

## Structured Progress API

Agents emit structured progress events to the orchestrator, replacing reliance on parsing unstructured container output.

### Emitting Progress

```bash
# Report current work step
egg-orch progress emit --step "running tests" --state working --detail "pytest suite 3/5"

# Report a blocker
egg-orch progress emit --step "applying fix" --state blocked --blocker "missing dependency"

# Report step completion
egg-orch progress emit --step "code review" --state complete
```

**Progress states:**

| State | Meaning |
|-------|---------|
| `working` | Actively working on this step |
| `blocked` | Waiting on something (specify `--blocker`) |
| `complete` | Step finished |

### Querying Progress

```bash
# All progress for the current pipeline
egg-orch progress query

# Progress for a specific agent
egg-orch progress query --agent coder

# Recent progress since a timestamp
egg-orch progress query --since "2026-03-16T10:00:00Z" --limit 50
```

**API endpoint:**

```
GET /api/v1/pipelines/{id}/progress?agent_role=<role>&since=<timestamp>&limit=<n>
```

### When to Emit Progress

All agents should emit structured progress at key milestones:

- **Starting a major work step** (e.g., "analyzing codebase", "writing tests", "reviewing proposal")
- **Completing a step** (transition to next step or mark complete)
- **Encountering a blocker** (dependency, missing data, unclear requirements)
- **Long-running operations** (emit periodically so the orchestrator knows you're alive)

Progress events supplement heartbeats — they provide richer context about what an agent is doing, not just that it's alive.

## Tier 1: Orchestrator Tripwires

The orchestrator processes structured progress events with deterministic rules. No LLM is involved. Tripwires fire instantly when thresholds are exceeded.

### Tripwire Rules

| Tripwire | Condition | Auto-Action |
|----------|-----------|-------------|
| **Heartbeat timeout** | No heartbeat or progress within threshold | Escalate to overseer/HITL (overseer decides whether to nudge) |
| **Container exit** | Agent container dies unexpectedly | Immediate HITL escalation |
| **Repeated errors** | Same error N times consecutively | Escalate to overseer (or HITL if no overseer) |
| **Message volume spike** | Agent sending > N messages/minute | Auto-throttle |
| **Progress stall** | No structured progress update within threshold | Escalate to overseer/HITL (overseer decides whether to nudge) |
| **Infrastructure error** | Agent reports `blocked` state with infrastructure-related blocker (git failures, gateway errors, permission denied) | Critical alert → overseer routes to HITL fast-path (bypasses nudge/redirect ladder) |
| **BRC progress stall** | Fully-ACKed producer hasn't sent `CONSENSUS_CONFIRMED` within timeout | Escalate to overseer/HITL (detects producers stuck in heartbeat loops post-ACK) |

### Infrastructure Error Detection

When agents emit `blocked` progress events with infrastructure-related blocker text, the orchestrator detects these as infrastructure errors requiring immediate human attention — distinct from normal stalls where an agent is simply slow.

**Detection mechanism:**
- The `HealthMonitor._check_infra_errors()` method scans recent progress events for `state=blocked` entries
- Blocker text is matched against `INFRA_ERROR_PATTERNS` — regex patterns covering common infrastructure failures:
  - Git operation failures (`git add failed`, `git push rejected`)
  - Gateway errors (`gateway.*error`, `403 Forbidden`)
  - Permission/filesystem errors (`permission denied`, `EROFS`, `read-only filesystem`)
  - `.gitignore` conflicts
  - HTTP 500 errors from infrastructure services
- Matching events produce a `critical` severity alert with `type=infrastructure_error`

**Deduplication:**
- Each `AgentState` tracks an `infra_error_escalated` flag (similar to `heartbeat_escalated`)
- After an infrastructure error alert fires for an agent, the flag prevents duplicate alerts
- The flag resets when the agent emits a non-`blocked` progress event (e.g., `working` or `complete`), allowing re-detection if the agent hits a different infrastructure error later

**Example:**
```bash
# Agent emits a blocked progress event due to .gitignore conflict
egg-orch progress emit --step "committing review" --state blocked \
  --blocker "git add failed: .gitignore excludes .egg-state/reviews/"

# The orchestrator's Tier 1 tripwire:
#   1. Matches "git add failed" against INFRA_ERROR_PATTERNS
#   2. Creates a critical infrastructure_error alert
#   3. Overseer routes alert directly to HITL (no nudge/redirect)
```

### Viewing and Resolving Alerts

```bash
# List active deterministic alerts for the current pipeline
egg-orch health alerts

# List alerts for a specific pipeline
egg-orch health alerts --pipeline issue-123

# Resolve (remove) alerts after an issue is addressed
egg-orch health resolve --agent-id coder --alert-type heartbeat_timeout

# Or specify an explicit pipeline ID
egg-orch health resolve issue-123 --agent-id coder --alert-type heartbeat_timeout
```

**API endpoints:**

```
GET  /api/v1/pipelines/{id}/health/alerts
POST /api/v1/pipelines/{id}/health/alerts/resolve
     Body: {"agent_id": "<role>", "alert_type": "<type>"}
```

### Phase-Aware Thresholds

The health monitor uses **phase-aware thresholds** for heartbeat and progress stall detection. Different pipeline phases have different workload characteristics — the implement phase involves deep code reading, multi-file changes, and test execution that routinely takes 15–30+ minutes, while refine and plan phases involve lighter-weight work.

The `HealthMonitor` tracks the current pipeline phase via `set_current_phase()`, which is called at each phase transition before agents are spawned. During the **implement phase**, heartbeat and progress stall checks use the `orchestrator_implement_heartbeat_timeout_seconds` threshold (default 600s / 10 minutes). During all other phases, the standard `orchestrator_heartbeat_timeout_seconds` threshold (default 120s) applies.

**Why this matters:** In pipelines `issue-1523-v2` and `issue-1527`, the default 120s threshold generated false-positive stall alerts against agents doing legitimate deep implementation work. A 10-minute threshold for the implement phase reduces noise while the Tier 2 overseer LLM classifier provides a secondary detection layer for genuinely stuck agents.

### BRC-Idle Suppression

In concurrent execution mode (BRC protocol), reviewer-only agents sit idle until upstream producers send a `CONSENSUS_PROPOSE` message. The health monitor recognizes this as a legitimate waiting state and **suppresses heartbeat and progress stall alerts** for reviewer-only agents whose upstream producers are all still in the `WORKING` phase. Dual-role agents (those that are both producers and reviewers) are **not** suppressed, since they have their own work to complete.

The suppression logic queries the peer consensus tracker's review graph to determine each agent's role (producer, reviewer, or both) and checks the consensus phase of upstream producers. Once any upstream producer transitions out of `WORKING` (e.g., to `PROPOSED`), the downstream reviewer resumes normal monitoring.

**Example:** During the implement phase, the coder is actively working while reviewer_code and reviewer_contract wait for proposals. Without BRC-idle suppression, both reviewers would trigger heartbeat timeout alerts after the threshold. With suppression enabled, only agents with their own work to complete are monitored — pure reviewers waiting for upstream proposals are recognized as legitimately idle.

### Post-Propose Grace Period for Reviewers

When a producer sends `CONSENSUS_PROPOSE`, reviewer-only agents transition from idle (waiting for proposals) to active (reviewing). However, reviewers need time to prepare their review — reading code, verifying claims, running checks — before they can emit BRC messages. Previously, reviewers were immediately subject to normal heartbeat/progress thresholds as soon as any upstream producer proposed, causing **false positive stall alerts** that killed active reviewers mid-review.

The health monitor now provides a **post-propose grace period** for reviewer-only agents. After an upstream producer proposes, the reviewer has `post_proposal_grace_seconds` (default: 300s / 5 minutes) before heartbeat and progress stall checks apply. During this grace window, the reviewer is suppressed from alerts — similar to BRC-idle suppression but covering the transition period after a proposal arrives.

**How it works:**
- The `HealthMonitor._is_brc_idle()` method checks if the agent is a reviewer-only role (not a producer)
- It queries `PeerConsensusTracker.get_earliest_proposal_time(reviewer)` to find the earliest proposal timestamp among the reviewer's upstream producers
- If a proposal exists and `time.time() - proposal_time < post_proposal_grace_seconds`, the reviewer is suppressed from alerts
- Once the grace window expires, normal heartbeat/progress monitoring resumes

**Why 5 minutes?** Even complex reviews (e.g., verifying refine analysis against the full codebase) complete their initial orientation within 5 minutes. This window is long enough to prevent false positives but short enough to detect genuinely stuck reviewers.

**Example:** During the implement phase, coder sends `CONSENSUS_PROPOSE` after completing its work. reviewer_code begins reading the changed files, grepping the codebase, and checking type signatures — all showing tool call activity but no BRC messages. Without the post-propose grace period, reviewer_code would trigger a heartbeat timeout after 120s (or 600s in implement phase). With the grace period, reviewer_code has 5 minutes of uninterrupted review time before monitoring kicks in.

**Root cause addressed:** In pipeline runs observed in issue #1613, a reviewer_refine agent was killed after ~1 minute despite actively verifying claims from the analysis (grepping codebase, checking types). The overseer flagged it as stalled because it had zero BRC messages, ignoring the tool call activity that showed it was doing real work.

### Post-ACK Confirmation Timeout for Producers

After all reviewers ACK a producer's proposal, the producer must send `CONSENSUS_CONFIRMED` to complete the BRC protocol. In observed failure modes, producers entered tight heartbeat loops after being ACKed — heartbeating every few seconds but never sending `CONFIRMED` — and the health monitor never flagged them because liveness checks only tested heartbeat freshness.

The health monitor now adds a **post-ACK confirmation timeout** via `check_brc_progress()`. When a producer is fully ACKed (all reviewers have sent `CONSENSUS_ACK`) but hasn't yet sent `CONSENSUS_CONFIRMED`, a timeout clock starts. If the producer doesn't confirm within `orchestrator_post_ack_confirmation_timeout_seconds` (default: 180s / 3 minutes), the health monitor creates an escalation alert and fires callbacks — regardless of how frequently the producer is heartbeating.

**How it works:**
1. `check_brc_progress()` is called as part of `check_tripwires()` on each monitoring cycle
2. It queries `PeerConsensusTracker.get_fully_acked_producers()` to find producers where all reviewers have ACKed but the producer hasn't yet confirmed
3. For each such producer, it records a first-seen timestamp in `_fully_acked_first_seen`
4. If `time.time() - first_seen > orchestrator_post_ack_confirmation_timeout_seconds` and the agent hasn't already been escalated (via `brc_progress_escalated` flag on `AgentState`), it creates an escalation alert
5. When a producer confirms or is no longer in the fully-acked set, tracking is cleaned up

**Why 3 minutes?** The time between receiving an ACK and sending `CONFIRMED` should be near-instantaneous (just reading the ACK message and calling `egg-orch consensus confirmed`). A 3-minute timeout is generous enough to accommodate network delays and slow poll cycles, but catches agents stuck in heartbeat loops far faster than the previous detection mechanisms (~10 minutes via `IncompleteConsensusStallCheck`).

**Example:** In a pipeline run documented in issue #1613, a producer received reviewer ACKs and the orchestrator broadcast "All reviewers have ACKed — ready to confirm." Four out of five agents confirmed normally. The remaining producer entered a tight loop (heartbeat + message poll every ~4 seconds) for 11 minutes without ever sending `CONFIRMED`. With the post-ACK confirmation timeout, this would be detected and escalated after 3 minutes instead of 11.

**Relationship to `IncompleteConsensusStallCheck`:** The Tier 1 health check `IncompleteConsensusStallCheck` catches a broader class of incomplete consensus stalls (including reviewers that haven't ACKed). The post-ACK confirmation timeout is narrower and faster — it specifically targets the "heartbeating but not confirming" failure mode and fires within 3 minutes rather than the ~10 minutes required by the Tier 1 check's grace + tick threshold.

### Configuration

Tripwire thresholds are configurable in `PipelineConfig`:

| Field | Default | Description |
|-------|---------|-------------|
| `overseer_enabled` | `true` | Auto-spawn overseer on all pipelines |
| `orchestrator_heartbeat_timeout_seconds` | `120` | Escalate to overseer/HITL after this many seconds without heartbeat (used for all phases except implement) |
| `orchestrator_implement_heartbeat_timeout_seconds` | `600` | Escalate to overseer/HITL after this many seconds without heartbeat during the **implement phase** (must be ≥ 10) |
| `orchestrator_error_repeat_threshold` | `3` | Escalate after N identical consecutive errors |
| `orchestrator_message_rate_limit` | `20` | Auto-throttle above this many messages per minute |
| `post_proposal_grace_seconds` | `300` | Grace period (seconds) for reviewer-only agents after an upstream producer proposes, before heartbeat/progress stall checks apply (must be >= 30). This reuses the existing BRC proposal grace period setting. |
| `orchestrator_post_ack_confirmation_timeout_seconds` | `180` | Timeout (seconds) for fully-ACKed producers to send `CONSENSUS_CONFIRMED` before escalation, regardless of heartbeat activity (must be >= 30) |
| `overseer_poll_interval_seconds` | `30` | How often the overseer checks health |
| `overseer_max_redirects_before_escalation` | `2` | Redirect attempts before HITL escalation |
| `overseer_decision_maker_model` | `"sonnet"` | LLM model for overseer decision-making tier |
| `overseer_max_turns` | `2000` | Maximum Agent SDK turns for the overseer agent per phase. The overseer's continuous poll-classify-act loop consumes ~2–10 turns per 30-second cycle depending on alert activity, so the previous hardcoded value of 500 could be exhausted in ~25 minutes during active consensus negotiation. Valid range: 100–10,000. |
| `overseer_max_respawns` | `3` | Max times to auto-respawn the overseer if it exits mid-phase (0 disables respawning). The respawn counter resets at each phase boundary since each phase spawns a fresh overseer instance. |
| `overseer_rerun_min_work_seconds` | `60` | Minimum work duration required after a `request_changes` phase-gate decision; completions faster than this with `content_changed=False` are flagged as re-run anomalies |
| `overseer_hitl_propagation_timeout_seconds` | `300` | Seconds to wait for a resolved phase-gate decision to appear in the SDLC contract before raising a propagation-failure alert |
| `overseer_infra_error_dedup_window_seconds` | `300` | Time window for deduplicating infrastructure error escalations between Tier 1 and Tier 2 (same agent + same error pattern) |
| `post_proposal_grace_seconds` | `300` | Grace period after a `CONSENSUS_PROPOSE` message before blocking reviewers can be flagged as stalled. Resets on each new proposal. |
| `active_agent_stall_extension_seconds` | `120` | If a blocking agent has emitted a progress event within this window, stall responses are suppressed. Tier 1 resets its tick counter unconditionally (no cap). The overseer caps nudge deferrals at 1× the HITL threshold and HITL deferrals at 2× the HITL threshold from the absolute stall start. |
| `overseer_max_agent_restarts` | `2` | Maximum auto-restarts per agent per phase before escalating to HITL. The overseer reads the authoritative count from the spawner's REST API response (unified with all restart sources) rather than tracking independently |
| `overseer_heartbeat_failures_before_restart` | `3` | Consecutive heartbeat failures before the overseer triggers an agent restart (default: 3) |
| `overseer_nudge_timeout_before_restart_minutes` | `5` | Minutes to wait after sending a nudge with no response before triggering an agent restart |

## Tier 2: Overseer Agent

The overseer is a phase-scoped, read-only agent that handles cases the orchestrator's deterministic rules can't resolve. It is spawned at the start of each pipeline phase and torn down when the phase completes, advances, or fails — giving each phase a fresh instance with no accumulated state. It runs as a separate container with no git repository access.

### Lifecycle

- **Phase-scoped** — the overseer is spawned at the start of each pipeline phase and torn down when that phase completes, advances, or fails. Each phase gets a fresh overseer instance with no accumulated state from prior phases.
- **Auto-spawned** on every pipeline (when `overseer_enabled` is true)
- **Configurable turn budget** — the overseer runs with `overseer_max_turns` (default 2000) Agent SDK turns per phase, configurable in `PipelineConfig`. This replaced a hardcoded value of 500 that caused premature exits during active consensus negotiation (~480 turns consumed in ~25 minutes).
- **Auto-respawned** if the overseer exits before the current phase reaches a terminal state (up to `overseer_max_respawns` attempts, checked every 30 seconds by the orchestrator's health monitor thread). The respawn logic is gated by a `phase_overseer_active` flag — the health monitor thread will not attempt to respawn the overseer between phases when it has been intentionally stopped.
- **Respawn visibility** — when the overseer is respawned, the orchestrator captures the exited container's last 20 log lines (best-effort) and broadcasts an `OVERSEER_ALERT` message to the message bus with diagnostic metadata: `exit_code`, `old_container_id`, `new_container_id`, `log_tail`, `respawn_attempt`, and `max_respawns`. This ensures respawn events are visible via `get_status`/`recent_messages` and the `/sdlc` monitoring session. The broadcast is best-effort — it never blocks the respawn if the message store is unavailable or log capture fails.
- **One overseer per pipeline phase** — only one overseer container runs at a time
- **No code access** — cannot clone, checkout, or modify code

### Internal Architecture

The overseer uses a two-sub-tier LLM architecture for cost efficiency:

#### Haiku Classifiers

Lightweight Haiku agents handle classification tasks. They run only when the orchestrator escalates an ambiguous situation.

| Task | Prompt Pattern |
|------|---------------|
| **Stall classification** | "Is this agent stuck, doing legitimate long-running work, or hitting an infrastructure error?" |
| **Loop detection** | "Is this agent repeating the same actions in a cycle?" |
| **Error triage** | "Is this error recoverable or fatal? Is it an infrastructure error?" |
| **Off-track detection** | "Is this agent's work aligned with the contract?" |
| **Decision consistency** | "Does this phase's output respect prior resolved HITL decisions?" |

**Consensus-aware stall classification**: The stall classifier receives BRC consensus state as authoritative context when available. The classifier is instructed that an agent with confirmed consensus is not stalled — this prevents false stall diagnoses during the window between consensus confirmation and phase transition.

**Container-log-aware classification**: The overseer automatically fetches recent Docker container logs (last 200 lines, truncated to 8 000 chars) for each alerted agent at the start of every monitoring cycle and passes them to the stall classifier. This surfaces runtime failures — OOM kills, segfaults, tracebacks, repeated permission errors — that never appear in structured progress events. These logs are also forwarded (truncated further) to the Sonnet/Opus decision-maker when determining escalation level.

Characteristics:
- Short, focused prompts — single-purpose classification
- Results are cached to avoid re-analyzing the same log lines
- Budget: ~1-2 Haiku calls per poll cycle per agent (only on anomalies)
- Falls back to heuristic checks if the API is unavailable

#### Sonnet/Opus Decision-Maker

A Sonnet or Opus agent handles corrective decision-making when Haiku monitors escalate.

Responsibilities:
- Decide corrective action: nudge, redirect, **restart agent**, **restart phase**, HITL escalation, or issue filing
- **Agent restart**: When an agent is classified as stalled or unresponsive after escalation thresholds, the decision-maker can select the `restart_agent` action to automatically stop and respawn the stuck agent (up to the configured restart limit per agent per phase, default 2)
- **Phase restart**: When agent-level restarts are exhausted for 2+ agents, the decision-maker can select the `restart_phase` action, which creates a HITL decision for human approval before restarting the entire phase
- **Restartable infrastructure error routing**: When classification is `infrastructure_error`, the decision-maker examines the `reasoning` field for restartable subcategories (unresponsive, crashed, OOM, timeout, hung, not responding). Matching errors produce a `restart_agent` action instead of immediately escalating to HITL — this enables automatic recovery for transient infrastructure failures. Non-restartable infrastructure errors (e.g., permission denied, EROFS, read-only filesystem) bypass the nudge/redirect ladder and return `hitl` action with high priority immediately, including the original error details in the escalation message. The same routing logic applies in `escalate_redirect_decision()`
- Compose redirect messages with actionable guidance
- Determine whether a pattern warrants an issue vs. HITL escalation
- Produce pipeline health summary at completion

Characteristics:
- Only invoked when Haiku detects an anomaly requiring a decision
- Receives structured context from the Haiku tier
- Higher cost per call, but called infrequently

All LLM calls use `shared/egg_agent/` (`run_agent_async`) — no direct API calls.

### Escalation Flow

```
Orchestrator detects anomaly via structured logs (deterministic)
  → Clear-cut (heartbeat timeout, container exit, error repeat)
    → Orchestrator escalates directly to overseer/HITL
  → Infrastructure error (blocked + infra keyword match)
    → Critical alert → Overseer routes to HITL fast-path (no nudge/redirect)
  → Ambiguous
    → Escalate to overseer

Overseer receives escalation (or detects anomaly in own polling)
  → Infrastructure error alert (from Tier 1)
    → Route directly to decision maker (skip LLM classification)
      → Restartable (unresponsive/crashed/OOM/timeout/hung)
        → restart_agent action (auto-recovery, up to max restarts)
      → Non-restartable (permission denied/EROFS/filesystem)
        → HITL escalation with error details
  → Other alert
    → Haiku classifies (stall / loop / error / infrastructure_error / off-track)
      → infrastructure_error classification
        → Decision maker routes: restartable → restart_agent; non-restartable → HITL
      → Simple action needed (e.g., nudge)
        → Haiku handles directly
      → Decision needed (redirect content, escalation level)
        → Escalate to Sonnet/Opus
          → Sonnet/Opus decides corrective action
            → Execute action (nudge / redirect / HITL / file issue / Slack)
```

**Phase-scoped alert processing**: Health alerts are filtered to only include agents in the current pipeline phase. Alerts for agents from completed phases (e.g., a coder alert during the test phase) are excluded to prevent false stall diagnoses.

### Corrective Action Ladder

The system follows a progressive escalation ladder:

| Step | Action | When |
|------|--------|------|
| 1 | **Escalate to overseer/HITL** | Orchestrator detects heartbeat/progress timeout; immediately escalates to overseer (or HITL if overseer disabled) |
| 1a | **Infrastructure error → smart routing** | Orchestrator detects infrastructure error (blocked + infra keyword). **Restartable** errors (unresponsive, crashed, OOM, timeout, hung) are routed to `restart_agent` for automatic recovery. **Non-restartable** errors (permission denied, EROFS, filesystem) bypass steps 2-4 and escalate directly to HITL with error details |
| 2 | **Nudge / Redirect message** | Overseer classifies the alert and sends a nudge or actionable guidance to the agent |
| 3 | **Restart agent** | Agent still unresponsive after nudge(s); overseer auto-restarts the agent (up to max restarts per phase, default 2). Stops the container, resets consensus state, respawns with same config — the gateway's idempotent worktree creation rediscovers the existing worktree so all committed work is preserved |
| 4 | **Restart phase (HITL)** | Agent-level restarts exhausted for 2+ agents; overseer creates HITL decision for phase restart approval. Requires human confirmation before stopping all containers and respawning |
| 5 | **HITL escalation** | Agent still stuck after max restarts, or restart not applicable |
| 6 | **File GitHub issue** | Structured diagnostic report for persistent problems |
| 7 | **Slack notification** | Human escalation for urgent issues |

**Escalation safety net**: If the decision-maker selects `nudge` or `redirect` but the accompanying message indicates human intervention is required (e.g., contains phrases combining human/manual/operator with intervention/review/needed), the action is automatically upgraded to `hitl`. This prevents under-escalation caused by LLM phrasing that signals urgency without selecting the appropriate action level.

### Post-Consensus Stall Detection

If all agents have confirmed BRC consensus but the pipeline phase has not transitioned within ~90 seconds (3× the poll interval), the overseer escalates with a HITL decision, Slack notification, and message bus broadcast (`OVERSEER_ALERT`). This detects potential orchestrator transition failures after a successful concurrent phase. The escalation fires only once per consensus cycle to avoid duplicate alerts.

### Incomplete Consensus Stall Detection

A complementary scenario: consensus is **incomplete** and the same blocking agents are not progressing — typically after a re-review cycle that cleared their confirmed status, leaving them stuck in a heartbeat loop. Two layers handle this:

- **Tier 1 `IncompleteConsensusStallCheck`**: Fires on each `RUNTIME_TICK` after a 5-minute grace period. If the same set of blocking agents persists for 10 consecutive ticks, the check reports `DEGRADED`.
- **Overseer recovery**: After ~5 poll minutes (~10 cycles at the default 30s interval) with unchanged blocking agents, the overseer sends a targeted nudge to each blocking agent instructing them to re-confirm or re-review. If the stall continues for another ~5 minutes (10 more cycles), it escalates to HITL with a Slack notification.

Both layers apply suppression rules to reduce false positives:

- **Post-proposal grace** (`post_proposal_grace_seconds`, default 300s): When a `CONSENSUS_PROPOSE` message arrives, stall tracking resets and checks are skipped for 5 minutes. This prevents false stall alerts against reviewers that are actively evaluating a fresh proposal.
- **Activity-aware suppression** (`active_agent_stall_extension_seconds`, default 120s): If a blocking agent has emitted a progress event within the configured window, stall responses are deferred. The two layers apply this differently:
  - **Tier 1**: Resets the consecutive-tick counter to 0, unconditionally suppressing the `DEGRADED` report as long as progress events keep arriving. There is no absolute-time cap at this layer — the overseer's caps serve as the backstop.
  - **Overseer**: Defers nudges and HITL escalations, but with absolute-time caps to prevent indefinite suppression. Nudge deferrals are capped at 1× the HITL threshold from the absolute stall start; HITL deferrals are capped at 2× the HITL threshold.

### Additional Overseer Health Checks

Each poll cycle the overseer evaluates six targeted health checks (the fourth triggers only on phase transitions; the fifth triggers only at pipeline completion). Only the fourth (cross-phase consistency) uses an LLM classifier; the rest are deterministic (no LLM cost):

> **Note:** All checks broadcast an `OVERSEER_ALERT` message to the `all` target on the message bus, allowing the `/sdlc` monitoring session and other listeners to surface findings via `egg-orch message recent`. Each alert is routed to the correct pipeline using an explicit `pipeline_id` argument and attributed with `from_role: overseer`, ensuring alerts from internal self-tests or other pipelines never leak into unrelated pipelines' message streams.

| Check | Detects | Action |
|-------|---------|--------|
| **Re-run anomaly** | Agent completes in < `overseer_rerun_min_work_seconds` after a `request_changes` phase-gate decision with `content_changed=False` — a likely no-op re-run | HITL escalation + Slack notification + message bus broadcast (deduplicated per decision ID) |
| **Status inconsistency** | Pipeline shows `failed` while all agents show `complete` — a possible transient state | HITL escalation + Slack notification + message bus broadcast (after one poll-cycle grace period) |
| **HITL propagation failure** | A resolved phase-gate decision is not reflected in the SDLC contract after `overseer_hitl_propagation_timeout_seconds` | HITL escalation + Slack notification + message bus broadcast |
| **Cross-phase consistency** | On a phase transition, the new phase's contract output may not honour prior resolved HITL decisions (uses the Haiku `decision_consistency` classifier; requires confidence > 0.7 to escalate) | HITL escalation + Slack notification + message bus broadcast (deduplicated per phase-transition pair) |
| **PR phase no PR** | Pipeline reaches `complete` with `current_phase=pr` but no `pr_url` in phase artifacts — defense-in-depth for edge cases where primary PR creation failure handling was bypassed, so stranded branch work is not silently lost | HITL decision + Slack notification + message bus broadcast |
| **Orchestrator unreachability** | Both pipeline status and phase queries return empty for 3 consecutive poll cycles — likely orchestrator container crash or network partition | Slack notification + oversight event + message bus broadcast (re-alerts every 3 cycles until recovered; oversight event also logged on recovery) |
| **Incomplete consensus stall** | Consensus is incomplete and the same agents are blocking for ~5 minutes — likely stuck in a heartbeat loop after a re-review cycle cleared their confirmed status | Targeted nudge to each blocking agent (deferred if agents have recent progress events; nudge deferral capped at 1× HITL threshold); HITL + Slack if stall persists for ~5 more minutes (HITL deferral capped at 2× HITL threshold from absolute stall start) |
| **Infrastructure error (Tier 1)** | Agent emits `blocked` progress event with infrastructure-related blocker text (git failures, gateway errors, permission denied, EROFS) | Critical alert → overseer routes to decision maker HITL fast-path, bypassing nudge/redirect ladder. Deduplicated: same agent + same error pattern within `overseer_infra_error_dedup_window_seconds` produces only one HITL escalation across both tiers |

### Infrastructure Error Cross-Tier Deduplication

Infrastructure errors can be detected by both Tier 1 (deterministic pattern matching on progress events) and Tier 2 (LLM classification of stall context). To prevent duplicate HITL escalations:

1. When the overseer processes a Tier 1 `infrastructure_error` alert, it records the escalation in a per-agent deduplication set (agent ID + error hash + timestamp)
2. If the Tier 2 classifier independently detects an `infrastructure_error` for the same agent within the dedup window (default 5 minutes, configurable via `overseer_infra_error_dedup_window_seconds`), the duplicate HITL escalation is suppressed
3. Distinct errors for the same agent (different error text) are **not** deduplicated — each unique infrastructure error gets its own HITL escalation

When a Tier 1 `infrastructure_error` alert reaches the overseer monitor, it is routed directly to the decision maker with the infrastructure error classification pre-set, avoiding a redundant LLM classification call. This saves both latency and LLM cost.

### Autonomous Issue Filing

When the overseer files a GitHub issue (decided by the Sonnet/Opus tier), it uses a structured diagnostic template:

```markdown
## Pipeline Diagnostic: {anomaly_type}

**Pipeline**: `{pipeline_id}`
**Phase**: `{phase}`
**Agent**: `{agent_role}`
**Detected**: `{timestamp}`

### Anomaly
{anomaly description}

### Timeline
{chronological events leading to this alert}

### Classification
{Haiku classifier output}

### Actions Taken
{list of auto-nudges, redirect messages, HITL requests}

### Container Logs          <!-- only present when logs exist -->
````
{last 2 000 chars of agent pod logs}
````

### Suggested Remediation
{what a human should do}
```

Issues are auto-labeled with `overseer-alert` and the error category (e.g., `stall`, `repeated-error`).

### Pipeline Isolation

All overseer CLI operations (`_broadcast_alert`, `_send_message`, `_resolve_alert`, `_create_hitl_decision`) pass the pipeline ID explicitly as a positional argument rather than relying on the `EGG_PIPELINE_ID` environment variable. This ensures that:

- **Alerts are routed to the correct pipeline** — self-test alerts (e.g., for test pipeline IDs like `test-postconsensus-001`) never leak into real pipelines' message streams.
- **`from_role` is always `overseer`** — the `--role overseer` flag is passed explicitly, preventing misattribution when the environment's `EGG_AGENT_ROLE` differs (e.g., inherited `coder` role).
- **Targeted messages include the `--type` flag** — `_send_message` passes `--type STATUS` to satisfy the CLI parser's required argument, ensuring messages are delivered rather than silently dropped.

### Overseer Access & Restrictions

**Has access to:**
- Orchestrator APIs: pipeline status, container logs, progress queries, health alerts, message bus
- Structured agent progress data via `egg-orch progress query`
- Agent container logs via `egg-orch container logs`
- Gateway and orchestrator health endpoints
- GitHub API: `gh issue create` for diagnostic filing
- `egg-orch message send` to redirect individual agents or broadcast `OVERSEER_ALERT` notifications to all (always with explicit pipeline routing)

**Blocked from:**
- All git operations (no repo mounted)
- All source, test, doc, and config files
- `gh pr merge` and `gh pr create`
- `egg-orch phase advance` / `egg-orch phase complete`

**Agent restart capability**: The overseer can trigger agent-level restarts automatically via the `RESTART_AGENT` corrective action, which calls the `POST /api/v1/pipelines/{id}/agents/{role}/restart` endpoint. This is subject to the per-agent restart limit (default 2 per phase). Phase-level restarts (`RESTART_PHASE`) require HITL approval by default — the overseer creates a decision rather than restarting directly.

The overseer reads the authoritative restart count from the spawner's REST API response — it does not maintain an independent counter. This ensures a unified restart budget: whether a restart is triggered by the overseer, MCP, or CLI, all count toward the same per-agent per-phase limit. When an agent's restart count reaches the configured maximum, the overseer escalates to HITL instead of auto-restarting. When 2+ agents have exhausted their restart limits, the overseer escalates to a phase-level restart decision.

### Self-Monitoring

The overseer monitors itself:
- **Poll cycle timing** — warns if a cycle takes >2x expected duration
- **Message volume** — alerts if sending >10 redirects per minute
- **LLM call costs** — reduces poll frequency if exceeding budget
- **Self-reporting** — files an issue about itself and signals `BLOCKED` if malfunctioning

## Overseer vs. Mediator Boundary

| Signal | Owner | Reasoning |
|--------|-------|-----------|
| Agent stalls (no heartbeat) | **Orchestrator** | Clear-cut tripwire |
| Repeated identical errors | **Orchestrator** → **Overseer** | Orchestrator detects; overseer classifies if ambiguous |
| Ambiguous stall (working or stuck?) | **Overseer** | Requires semantic log analysis |
| Two agents disagree on approach | **Mediator** | Inter-agent conflict |
| Agent output diverges from contract | **Overseer** | Off-track detection |
| Contradictory message loop | **Mediator** | Inter-agent conflict; if no mediator, overseer escalates to HITL |

## Relationship to Existing Health Checks

Pipeline health monitoring extends the existing [health check framework](../../orchestrator/health_checks/README.md):

| Component | Role | Runs |
|-----------|------|------|
| **Tier 1 health checks** (existing) | Structural invariant checks (container liveness, state consistency, consensus stall detection) | At lifecycle triggers (STARTUP, RUNTIME_TICK, etc.) |
| **Tier 2 health checks** (existing) | LLM-powered semantic analysis of agent progress | At WAVE_COMPLETE (if Tier 1 degraded), PHASE_COMPLETE, ON_DEMAND |
| **Orchestrator tripwires** (new) | Deterministic real-time monitoring of structured progress events | Continuously, event-driven |
| **Overseer agent** (new) | LLM-powered analysis of ambiguous failures, corrective action | Per-phase, poll-based + escalation-driven (spawned/torn down at phase boundaries) |

The orchestrator tripwires process structured agent logs in real-time (event-driven), while the existing health check framework runs at discrete lifecycle points. The overseer agent provides deeper semantic analysis than Tier 2 health checks, with the ability to take corrective action (redirects, issue filing) rather than just reporting status.

## Related Documentation

- [Concurrent Execution Guide](concurrent-execution.md) — BRC consensus protocol and agent coordination
- [Orchestrator Architecture](../architecture/orchestrator.md) — Deployment modes, health check framework
- [Agent Roles Reference](../reference/agent-roles.md) — All agent roles including overseer
- [SDLC Pipeline Guide](sdlc-pipeline.md) — Phase execution and agent orchestration

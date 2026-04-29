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
| **BRC progress stall** | Fully-ACKed producer hasn't sent `CONSENSUS_CONFIRMED` within timeout | Send direct `OVERSEER_ALERT` to stuck producer instructing it to call `mcp__brc__confirm`; also escalate to overseer/HITL |
| **Branch divergence** | Pipeline branch is >20 commits ahead of base AND ahead-commits contain merged-PR subject signatures (`(#NNNN)`) — the contamination shape from #2222 | Publish `OVERSEER_ALERT` with `anomaly_type: "branch-divergence"` listing offending commits; deduplicates per SHA |

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

The health monitor now adds a **post-ACK confirmation timeout** via `check_brc_progress()`. When a producer is fully ACKed (all reviewers have sent `CONSENSUS_ACK`) but hasn't yet sent `CONSENSUS_CONFIRMED`, a timeout clock starts. If the producer doesn't confirm within `orchestrator_post_ack_confirmation_timeout_seconds` (default: 180s / 3 minutes), the health monitor fires an escalation callback that **directly sends an `OVERSEER_ALERT` to the stuck producer** instructing it to call `mcp__brc__confirm` — bypassing the overseer agent's decision loop for this deterministic failure mode. The alert also triggers the standard overseer/HITL escalation path.

**Plan-phase override:** The plan phase uses a higher threshold of `orchestrator_plan_post_ack_confirmation_timeout_seconds` (default: 300s / 5 minutes) instead of the standard 180s. Plan-phase post-ACK reconciliation (resolved decisions, feedback bodies, slice-DAG sanity checks) legitimately takes longer on heavy pipelines. (#2242)

**How it works:**
1. `check_brc_progress()` is called as part of `check_tripwires()` on each monitoring cycle
2. It queries `PeerConsensusTracker.get_fully_acked_producers()` to find producers that are actually ready to confirm — all of their reviewers have ACKed *and* `check_confirm_guard` would allow `mcp__brc__confirm` to succeed (notably the global zero-proposal guard, #1648). A producer that is fully ACKed but blocked because a peer producer still has `proposal_version == 0` is intentionally excluded so the timeout doesn't fire against an agent that is correctly waiting on its peer (#2187).
3. For each such producer, it records a first-seen timestamp in `_fully_acked_first_seen`
4. If `time.time() - first_seen > orchestrator_post_ack_confirmation_timeout_seconds` and the agent hasn't already been escalated (via `brc_progress_escalated` flag on `AgentState`), it creates an escalation with `alert_type: "brc_confirmation_timeout"` and fires registered callbacks
5. The `_send_brc_confirmation_nudge` callback sends an `OVERSEER_ALERT` directly to the stuck producer (bypassing `MESSAGE_SENT` tracking to avoid rate-limit and heartbeat-tracking side-effects). The message body tells the producer to call `mcp__brc__confirm` and explains how to handle `status='pending_acks'` guard failures.
6. When a producer confirms or is no longer in the fully-acked set, tracking is cleaned up

**Why 3 minutes?** The time between receiving an ACK and sending `CONFIRMED` should be near-instantaneous (just reading the ACK message and calling `egg-orch consensus confirmed`). A 3-minute timeout is generous enough to accommodate network delays and slow poll cycles, but catches agents stuck in heartbeat loops far faster than the previous detection mechanisms (~10 minutes via `IncompleteConsensusStallCheck`).

**Example:** In a pipeline run documented in issue #1613, a producer received reviewer ACKs and the orchestrator broadcast "All reviewers have ACKed — ready to confirm." Four out of five agents confirmed normally. The remaining producer entered a tight loop (heartbeat + message poll every ~4 seconds) for 11 minutes without ever sending `CONFIRMED`. With the post-ACK confirmation timeout, this would be detected and escalated after 3 minutes instead of 11.

**Relationship to `IncompleteConsensusStallCheck`:** The Tier 1 health check `IncompleteConsensusStallCheck` catches a broader class of incomplete consensus stalls (including reviewers that haven't ACKed). The post-ACK confirmation timeout is narrower and faster — it specifically targets the "heartbeating but not confirming" failure mode and fires within 3 minutes rather than the ~10 minutes required by the Tier 1 check's grace + tick threshold.

### Alive-Signal Gate for Heartbeat and Progress Alerts

Before firing a per-agent `heartbeat_timeout` or `progress_stall` alert, the health monitor checks whether the pipeline still has observable forward progress from peers. This **alive-signal gate** defers the alert when any of the following has fired within `orchestrator_alert_progress_gate_seconds` (default: 300s):

- The BRC tracker's most recent `CONSENSUS_PROPOSE` or ACK/NACK timestamp on this pipeline
- A heartbeat from any **other** agent in the current-phase active-agent set

If a peer signal is found within the window, the alert is deferred. The deferral is **gate-window-bounded, not indefinite**: the `escalated` flag is intentionally not set on defer, so each subsequent monitor cycle re-evaluates the gate and the alert fires once `gate_seconds` elapses past the most recent peer signal. This prevents false-positive escalations when one agent is slow but the pipeline as a whole is still moving. The gate mirrors `brc_consensus_progress_gate_seconds` but applies to the per-agent tripwires rather than the BRC consensus route.

**Active-agent filter:** When a BRC tracker is registered, peer heartbeats are filtered to the tracker graph's current-phase roster. Prior-phase agents from non-recurring roles (e.g., a `refiner` role that last heartbeated during the previous refine phase) are excluded. When no tracker is registered, the filter is skipped and any known peer heartbeat within the window defers the alert.

**Caveat — same-role cross-phase pollution:** Heartbeat keys in `_last_heartbeat` are not phase-stamped, so a role that recurs across phase transitions (e.g., `coder` across `implement → implement-fix`) can pass the active-agent filter with a stale heartbeat from the prior phase. Tracked under #2242 alongside the equivalent TODO in `_check_brc_progress_gate`.

**Caveat — single-producer self-deferral:** Self-exclusion only applies to the peer-heartbeat path. The BRC-bus path (`get_latest_progress_timestamp`) aggregates proposals + ACK/NACK timestamps across the whole tracker and is **not** filtered by focal agent. On a single-producer pipeline (BRC tracker registered, no peers) the producer's own recent `CONSENSUS_PROPOSE` / ACK therefore defers its own heartbeat alert until `gate_seconds` elapses past that timestamp. The effective stall-detection window in that case is `heartbeat_threshold + gate_seconds` (≈360s with defaults) rather than `heartbeat_threshold` (≈60s). Genuinely-dead containers are still caught by `CONTAINER_STOPPED`; for a hung process inside a live container, detection is delayed by up to `gate_seconds`. Operators tuning these values on single-producer pipelines should size them with this combined window in mind.

**Setting `orchestrator_alert_progress_gate_seconds = 0` disables the gate.** (#2242)

### Branch-Divergence Detection

When a pipeline's branch absorbs merged-main commits (the contamination shape investigated in #2222), the resulting PR shows a diff against main that includes unrelated merged work. The branch-divergence detector catches this at phase-boundary granularity — strictly better than detecting at PR open, but complementary to the primary gate in #2282.

**Detection mechanism:**
- Each 30-second health monitor tick calls `_branch_divergence_tick()`, which runs two git commands against `origin/<pipeline_branch>`:
  1. `git rev-list --count origin/<base>..origin/<pipeline_branch>` — count commits ahead
  2. If count > `BRANCH_DIVERGENCE_THRESHOLD` (20), `git log --no-merges --pretty=format:%H%x09%s` — list non-merge subjects
- If any subjects match the `(#NNNN)` pattern (merged-PR signatures), those commits are "offenders"
- An `OVERSEER_ALERT` with `anomaly_type: "branch-divergence"` is published listing the offending SHAs and subjects
- All git errors are logged and swallowed — observability must never block the pipeline

**Detection latency:** The local `origin/<pipeline_branch>` ref only refreshes when the orchestrator fetches — at pipeline start, phase boundaries, and a few resume/signal paths. The poll thread itself does not fetch. Contamination introduced mid-phase is therefore detected at the next phase boundary's fetch, not within 30 seconds.

**Deduplication:** A per-pipeline `divergence_alerted_shas` set tracks which offending commit SHAs have already fired an alert. New alerts fire only for newly-discovered offenders. The set clears when the contamination window goes empty (including on transient git errors), so re-introduced contamination re-fires — consistent with the "rather over-alert than miss" posture of #2224.

**False positives:** An agent legitimately including a `(#NNNN)` literal (with parentheses) in a commit subject — e.g., `"Reference benchmark suite (#2222)"` — would trigger the detector. The regex (`\(#\d+\)`) requires the literal `(` and `)` characters, so a bare `#2222` reference does not match. The alert body explains the false-positive scenario and instructs that no action is required if the diff against main looks clean.

### Configuration

Tripwire thresholds are configurable in `PipelineConfig`:

| Field | Default | Description |
|-------|---------|-------------|
| `overseer_enabled` | `true` | Auto-spawn overseer on all pipelines |
| `orchestrator_heartbeat_timeout_seconds` | `120` | Escalate to overseer/HITL after this many seconds without heartbeat (used for all phases except implement) |
| `orchestrator_implement_heartbeat_timeout_seconds` | `600` | Escalate to overseer/HITL after this many seconds without heartbeat during the **implement phase** (must be ≥ 10) |
| `orchestrator_error_repeat_threshold` | `3` | Escalate after N identical consecutive errors |
| `orchestrator_message_rate_limit` | `20` | Auto-throttle above this many messages per minute |
| `post_proposal_grace_seconds` | `300` | Grace period (seconds) for reviewer-only agents after an upstream producer sends `CONSENSUS_PROPOSE`, before heartbeat/progress stall checks apply (must be >= 30). Resets on each new proposal. |
| `orchestrator_post_ack_confirmation_timeout_seconds` | `180` | Timeout (seconds) for fully-ACKed producers to send `CONSENSUS_CONFIRMED` before escalation, regardless of heartbeat activity (must be >= 30) |
| `orchestrator_plan_post_ack_confirmation_timeout_seconds` | `300` | Plan-phase override for the post-ACK confirm timeout. Plan-phase reconciliation (resolved decisions, feedback bodies, slice-DAG sanity) legitimately exceeds 180s on heavy pipelines. (must be >= 30; #2242) |
| `orchestrator_alert_progress_gate_seconds` | `300` | Defer `heartbeat_timeout` and `progress_stall` per-agent alerts while any peer agent or the BRC bus has emitted a signal within this many seconds. Mirrors `brc_consensus_progress_gate_seconds` but for per-agent tripwires. 0 disables the gate. (#2242) |
| `overseer_poll_interval_seconds` | `30` | How often the overseer checks health |
| `overseer_max_redirects_before_escalation` | `2` | Redirect attempts before HITL escalation |
| `overseer_decision_maker_model` | `"sonnet"` | LLM model for overseer decision-making tier |
| `overseer_max_turns` | `2000` | Maximum Agent SDK turns for the overseer agent per phase. The overseer's continuous poll-classify-act loop consumes ~2–10 turns per 30-second cycle depending on alert activity, so the previous hardcoded value of 500 could be exhausted in ~25 minutes during active consensus negotiation. Valid range: 100–10,000. |
| `overseer_max_respawns` | `3` | Max times to auto-respawn the overseer if it exits mid-phase (0 disables respawning). The respawn counter resets at each phase boundary since each phase spawns a fresh overseer instance. |
| `overseer_rerun_min_work_seconds` | `60` | Minimum work duration required after a `request_changes` phase-gate decision; completions faster than this with `content_changed=False` are flagged as re-run anomalies |
| `overseer_hitl_propagation_timeout_seconds` | `300` | Seconds to wait for a resolved phase-gate decision to appear in the SDLC contract before raising a propagation-failure alert |
| `overseer_infra_error_dedup_window_seconds` | `300` | Time window for deduplicating infrastructure error escalations between Tier 1 and Tier 2 (same agent + same error pattern) |
| `active_agent_stall_extension_seconds` | `120` | If a blocking agent has emitted a progress event within this window, stall responses are suppressed. Tier 1 resets its tick counter unconditionally (no cap). The overseer caps nudge deferrals at 1× the HITL threshold and HITL deferrals at 2× the HITL threshold from the absolute stall start. |
| `overseer_max_agent_restarts` | `2` | Maximum auto-restarts per agent per phase before escalating to HITL. The overseer reads the authoritative count from the spawner's REST API response (unified with all restart sources) rather than tracking independently |
| `overseer_heartbeat_failures_before_restart` | `3` | Consecutive heartbeat failures before the overseer triggers an agent restart (default: 3) |
| `overseer_nudge_timeout_before_restart_minutes` | `5` | Minutes to wait after sending a nudge with no response before triggering an agent restart |
| `overseer_advisor_model` | `"opus"` | LLM model used by the Tier-2 advisor when Haiku flags an anomaly that intersects with a Tier-1 health alert. The default is the canonical `opus` alias (resolved by `shared/egg_harness/config.py` to the latest pinned Opus ID, currently Opus 4.6) so cost telemetry resolves correctly. See [Advisor Gate](#advisor-gate). |
| `overseer_advisor_recent_log_bytes_cap` | `256000` | Byte cap for the `recent_log_lines` block in the advisor prompt (issue #2120). When the joined block exceeds the cap, the prompt-builder drops oldest lines first so the most-recent lines (highest signal) survive, and prepends a marker so the advisor knows truncation happened. Set to `0` to disable (not recommended — leaves the prompt open to pathological log payloads). |
| `overseer_auto_file_issues_mode` | `"shadow"` | Auto-issue-filing mode: `shadow` surfaces the advisor's `recommendation=file_issue` as an `OVERSEER_ALERT` + HITL decision (the human approves before any `gh issue create` runs); `live` runs the same HITL flow but allows the CLI verb to file once approval lands. The HITL approval is *never* bypassed. To disable issue filing entirely, set `overseer_enabled=false`. |
| `overseer_owns_host_detection` | `false` | Calibration-window flag for the host → overseer migration. While `false` (the default), the `/sdlc` host skill keeps its stall / silent-agent / NACK / long-running-phase / stuck-pipeline rescue detectors live. While `true`, those host detectors short-circuit and the overseer is the sole source of these alerts. See [Host Detector Migration](#host-detector-migration). |
| `overseer_stuck_phase_transition_seconds` | `180` | Threshold (seconds) for the existing overseer `stuck-phase-transition` trigger (orchestrator-level signal). Raised from the previous hardcoded ~60s default per operator feedback during long phase transitions. |
| `overseer_agent_stall_seconds` | `180` | Threshold (seconds) for the new `detect_agent_stall` detector migrated from `/sdlc` (per-agent elapsed-time signal). Distinct from `overseer_stuck_phase_transition_seconds` so the two anomalies can be tuned independently. |
| `overseer_silent_agent_threshold_seconds` | `600` | Threshold (seconds) for the migrated `detect_agent_silent` detector (running agent with zero messages). Matches the previous `/sdlc` default. |
| `overseer_long_running_phase_seconds` | `3600` | Threshold (seconds) for the migrated `detect_phase_long_running` detector during the implement phase. Matches the previous `/sdlc` default. |
| `overseer_nack_unresolved_seconds` | `180` | Threshold (seconds) for the migrated `detect_nack_unresolved` detector (NACK outstanding without progress). Matches the previous `/sdlc` default. |

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

**Transition-completion short-circuit:** Before applying the 90-second grace window, the detector loads the pipeline and returns early (no alert, no HITL decision, no Slack) when any of the following indicate the post-consensus transition already succeeded:

- `pipeline.current_phase != "implement"` — the pipeline has already advanced out of implement (e.g., into `pr` or `complete`)
- `pipeline.pr_number is not None` — an auto-created PR number has been written back to the pipeline record (see [Pipeline state writeback after auto-PR creation](../architecture/orchestrator.md#pipeline-state-writeback-after-auto-pr-creation))
- `phases["pr"].artifacts["pr_url"]` is set — the PR phase has already recorded a `pr_url` artifact

When the short-circuit fires, the grace-period timer (`_post_consensus_stall_first_seen`) is reset so a subsequent genuine stall gets a fresh grace window. If loading the pipeline raises an exception, the detector falls through to the existing behaviour (fail open — a bug in the short-circuit must not suppress genuine alerts).

This short-circuit was added in response to issue #1911, where successful `/sdlc` runs were producing false-positive `post-consensus-push-stall` alerts because the overseer observed `consensus.is_complete` and `pipeline.status == "running"` before the post-consensus push/PR flow had a chance to advance the phase. The three conditions above give the detector three independent signals of successful transition; a genuine post-consensus stall populates none of them.

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

### Advisor Gate

Issue [#1962](https://github.com/jwbron/egg/issues/1962) introduces the **advisor strategy** for the overseer's decision tier: Haiku continues to drive every cycle (`max_turns=1`), and the configured advisor model (`PipelineConfig.overseer_advisor_model`, defaulting to the `opus` alias) is invoked **only when both** of these conditions hold simultaneously:

1. **Haiku flags an anomaly** with classification confidence ≥ 0.8.
2. **A Tier-1 orchestrator health alert is currently active** (the same intersection precedent shipped in [#2012](https://github.com/jwbron/egg/issues/2012)).

The intersection gate keeps the heavy-tier model out of every poll cycle while still giving the overseer an Opus-grade reasoner for the cases that warrant it. The advisor returns a structured `AdvisorVerdict` (`shared/egg_overseer/advisor.py`) with one of three decisions:

| Decision | Effect |
|----------|--------|
| `watch` | Emit nothing this cycle. The intersection was suspicious enough to consult the advisor but not actionable. |
| `alert` | Emit an `OVERSEER_ALERT` carrying the advisor's `alert_summary`, `alert_detail`, and translated `priority`. The advisor returns `priority` as `p0..p3`; `egg_overseer.priority.label_to_alert` maps to the alert verb's `low|medium|high` dimension. |
| `file_issue` | Emit an `OVERSEER_ALERT` whose `recommendation=file_issue` carries a fully composed `issue_title` + `issue_body` + `priority` + `anomaly_signature` in `recommendation_payload`. The CLI verb is **not** invoked here — see [Auto-Issue Filing (Shadow vs Live)](#auto-issue-filing-shadow-vs-live). |

The advisor is exposed to the sandbox as a CLI verb (`egg-orch overseer consult-advisor`); the handler at `sandbox/egg_lib/orch_cli.py::cmd_overseer_consult_advisor` calls `consult_advisor` from `shared/egg_overseer/advisor.py` directly. The underlying `run_agent_async` call therefore runs sandbox-side and stays on the LLM-execution side of the EGG200 boundary documented in [agent-mode-design.md](agent-mode-design.md) — the orchestrator pod never holds Anthropic credentials. The model and byte cap are resolved from `PipelineConfig.overseer_advisor_model` and `PipelineConfig.overseer_advisor_recent_log_bytes_cap` (read via the orchestrator status endpoint) when a pipeline ID is available; falls back to the `opus` model default and 256 KiB byte cap when absent or the lookup fails. The CLI verb reads the keyword arguments (`classification`, `health_alerts`, `progress_events`, `recent_log_lines`) that comprise the executor → advisor prompt contract from a JSON file passed via `--inputs-file`.

**No advisor cap is enforced in this PR** — the existing `max_llm_cost_per_hour=$5` envelope at `sandbox/agent-config/rules/overseer.md` remains the only budget control. A follow-up issue tracks an `overseer_advisor_max_uses_per_phase` (or equivalent) knob if production data shows the cap is needed.

### Auto-Issue Filing (Shadow vs Live)

When the advisor returns `decision="file_issue"`, the overseer:

1. **Composes the issue body** by calling `compose_issue_body(...)` from `sandbox/egg_lib/overseer_issue_body.py`. The body is built on the canonical template literal in `shared/egg_overseer/issue_template.py` (the byte-for-byte source preserved at `orchestrator/overseer/issue_filer.py:86-107` is kept as a regression anchor; the runtime renderer reads from the shared source). The composed body adds a "Pipeline Links" sub-block with branch URL, phase, branch name, commit SHA, and parent `OVERSEER_ALERT` message ID.
2. **Scrubs secrets.** The body passes through `shared/egg_overseer/scrubbing.py`'s `scrub_secrets()` pass before the advisor returns its verdict. The pattern set covers GitHub PATs (`ghp_…`, `ghs_…`, `gho_…`, `ghu_…`, `ghr_…`), AWS access keys (`AKIA…`), Slack webhooks (`https://hooks.slack.com/services/…`), and `GITHUB_TOKEN=` / `GH_TOKEN=` / `ANTHROPIC_API_KEY=` env exports. Each match becomes `[REDACTED:<kind>]`. The gateway re-runs the same scan as defense-in-depth and **rejects** any body that still contains a secret pattern (which surfaces the advisor bug rather than silently scrubbing).
3. **Emits `OVERSEER_ALERT` with top-level `recommendation="file_issue"`**, embedding the composed title and body in the top-level `recommendation_payload` field (sized ≤ 50 KB at the sandbox handler before the message reaches the bus). The fields are first-class optional on the `Message` envelope (`orchestrator/message_store.py`) — pre-#1962 alerts that don't set them serialize byte-identically thanks to `to_dict()` omitting unset values.
4. **Surfaces a HITL decision** so the human approves before any `gh issue create` runs. This is true in both modes:
   - `overseer_auto_file_issues_mode="shadow"` (the default rollout setting): the advisor's recommendation surfaces as an `OVERSEER_ALERT` + a `pending_decision` for the human; the human's approval triggers the `egg-orch overseer file-issue` verb.
   - `overseer_auto_file_issues_mode="live"`: the same HITL flow still runs — `mode` only controls whether the CLI verb is allowed to call `gh` once approval lands.
5. **Files the issue.** On HITL approval, the sandbox-side `egg-orch overseer file-issue` CLI verb runs `gh issue create` itself, mediated by the gateway. Issues land with the existing `agent:overseer` label plus the matching priority label (`p0`/`p1`/`p2`/`p3`) — no new labels are created. The title format embeds the first 8 hex characters of the anomaly signature: `[Pipeline Diagnostic] {anomaly_type} - {agent_role} [{anomaly_signature[:8]}]`.

**Dedup before recommend.** The advisor MUST call `find_existing_issue(repo, anomaly_signature)` first; only if it returns `None` does it return `decision="file_issue"`. Dedup state lives in two places:

- **Local fast path** — `.egg-state/oversight/filed-issues.jsonl` (append-only JSON Lines, header `{"_kind": "header", "schema_version": 1}` on line 1, one `FiledIssueRecord` per subsequent line). `append_filed_issue` and `load_filed_issues` acquire an `fcntl.LOCK_EX` flock on a per-state-file sentinel `.egg-state/oversight/filed-issues.jsonl.lock` (computed as `path.parent / f"{path.name}.lock"` by `_lock_path_for` — the agent-timing helpers do the same on `agent-timing.json.lock` for their state file; the two locks are independent). This file is **intra-phase only** — each phase spawns a fresh overseer container and `.egg-state/oversight/` is not preserved across phase boundaries.
- **Cross-phase fallback** — `gh issue list --label agent:overseer --state open --search "{anomaly_signature[:8]}" --json number,title --limit 100`. The 8-char signature prefix embedded in the issue title makes this query reliable.

The anomaly signature is computed deterministically by `egg_overseer.state.compute_anomaly_signature(anomaly_type, agent_role, repo, sorted(tier1_alert_types))` (SHA-1 of the concatenation, truncated to 16 hex characters; the first 8 chars travel in the title, all 16 in the dedup record). Tier-1 alert types participate in the signature so two genuinely different incidents that share `(anomaly_type, agent_role, repo)` but were triggered by different Tier-1 alerts (e.g., `agent-loop` on `coder` triggered by `heartbeat_timeout` vs by `repeated_error`) do not collapse onto the same signature.

`HITL outcome tracking`. Each `FiledIssueRecord` records `hitl_outcome` (`filed`, `skipped`, `modified_and_filed`, or `null`) so that when the human declines a recommendation the overseer doesn't re-prompt on the same anomaly after a respawn. `skipped` records carry `issue_number=null` and dedup the recommendation for `hitl_skip_lookback_seconds` (default 86400).

#### Diagnostic body template

The advisor populates the canonical body template (frozen at `orchestrator/overseer/issue_filer.py:86-107` and also exported from `shared/egg_overseer/issue_template.py`):

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

### Pipeline Links          <!-- added by sandbox-side composer -->
- Pipeline: [{pipeline_id}](https://github.com/{repo}/tree/{branch})
- Phase: {phase}
- Branch: {branch}
- Commit SHA at filing: {commit_sha}
- Parent OVERSEER_ALERT message ID: {parent_alert_message_id}
```

#### `EGG_PIPELINE_REPO` env var

The CLI verb and gateway require an `owner/repo`-formatted `EGG_PIPELINE_REPO` env var (distinct from `EGG_REPO_PATH`, which is the filesystem path). The orchestrator injects it via `kubernetes_spawner.py`; sandbox `entrypoint.py` raises if the variable is missing — a misconfigured pipeline failing fast is preferred over one that silently files an issue against the wrong repo. The CLI verb sets `--repo $EGG_PIPELINE_REPO` on every `gh issue create`; the gateway cross-checks the `--repo` argument against the env var and rejects mismatches. See [Agent Roles Reference](../reference/agent-roles.md#overseer) for the env-var contract.

### Host Detector Migration

Issue [#1962](https://github.com/jwbron/egg/issues/1962) also migrates five host-side `/sdlc` skill detectors into the overseer:

| Detector | Threshold knob | Migrated trigger |
|----------|----------------|------------------|
| `agent-stall` | `overseer_agent_stall_seconds` (default 180) | Per-agent: `phase_entered_at` older than threshold while in WORKING/PROPOSED |
| `agent-silent` | `overseer_silent_agent_threshold_seconds` (default 600) | Running agent with zero messages and `first_seen_at` older than threshold |
| `agent-nack-unresolved` | `overseer_nack_unresolved_seconds` (default 180) | NACK outstanding without producer progress for the threshold |
| `phase-long-running` | `overseer_long_running_phase_seconds` (default 3600) | Implement phase WORKING for longer than threshold |
| (host-side rescue) | `overseer_stuck_phase_transition_seconds` (default 180) | Existing overseer trigger; threshold raised from the previous ~60s hardcoded value |

Per-agent timing state moves from `/sdlc`'s in-memory `{role: {phase, phase_entered_at, …}}` map into `.egg-state/oversight/agent-timing.json` (see `shared/egg_overseer/state.py::AgentTimingState`). Read/modify/write is guarded by an `fcntl.LOCK_EX` flock on its own per-state-file sentinel `.egg-state/oversight/agent-timing.json.lock` so concurrent overseer respawns at phase boundaries cannot clobber each other's updates. Per-anomaly suppression uses `AgentTimingEntry.alerted_anomalies` so each `(role, anomaly)` pair fires at most once per `2× threshold` window per phase.

**Calibration-window flag semantics.** `overseer_owns_host_detection` defaults to `false` for the first release. The flag selects **one** active source of these alerts:

- **`false` (default)** — the host's `/sdlc` detectors run; the overseer's `run_migrated_detectors` function (`sandbox/overseer_monitor.py`) early-returns `[]` so the overseer emits no migrated-detector alerts at all. Pipelines see today's behavior.
- **`true`** — the host's `/sdlc` detection blocks short-circuit (gated on the same flag in `skills/sdlc/SKILL.md`); the overseer becomes the sole source.

This is "host XOR overseer", not "host AND overseer" — the calibration window is **operator-driven**: an operator opts a pipeline into `true` to validate overseer parity, then opts back to `false` if needed. After a calibration window (≥ 2 weeks) of validating overseer-side detection, a follow-up PR flips the default to `true` and deletes the now-dormant host blocks. Running both in parallel for observability comparison is tracked as a follow-up enhancement (out of scope for this PR; would require the overseer to compute alerts but suppress emission while the flag is False).

When `overseer_owns_host_detection=true` and the host sees no `OVERSEER_ALERT` from the overseer for `2 × overseer_agent_stall_seconds` while running agents are present, the host raises a single `AskUserQuestion` ("Overseer appears unresponsive; would you like to (a) check the overseer container logs, (b) restart the overseer, (c) continue with host detection only for this pipeline, (d) cancel?"). A sentinel file at `.egg-state/oversight/sdlc-fallback-fired-{pipeline_id}-{phase}.flag` ensures the fallback fires at most once per phase.

### Sandbox CLI Verb: `egg-orch overseer consult-advisor`

The sandbox-side CLI verb is the one and only way the overseer invokes the advisor:

| Inputs-file key | Type | Description |
|-----------------|------|-------------|
| `classification` | `dict` | Haiku classifier output — `{anomaly_type, confidence, reasoning, ...}` |
| `health_alerts` | `list[dict]` | Tier-1 health alerts active for the agent |
| `progress_events` | `list[dict]` | Recent structured progress events |
| `recent_log_lines` | `list[str]` | Tail of agent container log lines |

The handler at `sandbox/egg_lib/orch_cli.py::cmd_overseer_consult_advisor` calls `egg_overseer.advisor.consult_advisor()` directly. Output is the JSON-serialized `AdvisorVerdict` written to `--output-file` (or stdout when omitted). The `run_agent_async` call runs sandbox-side, keeping the LLM call on the LLM-execution side of the EGG200 boundary; the orchestrator pod never holds Anthropic credentials. The model alias and byte cap are resolved from `PipelineConfig.overseer_advisor_model` and `PipelineConfig.overseer_advisor_recent_log_bytes_cap` when a pipeline ID is provided (positional arg or `EGG_PIPELINE_ID`), falling back to `opus` and 256 KiB respectively when absent or the lookup fails.

**Backwards compatibility — top-level optional fields with omit-when-unset serialization.** The `OVERSEER_ALERT` schema gains three first-class optional fields on the `Message` envelope (`orchestrator/message_store.py`): `recommendation: str | None`, `recommendation_payload: dict | None`, and `schema_version: int = 1`. The `Message.to_dict()` serializer **omits** each of the three fields when they hold their defaults, so legacy callers that don't set them produce JSON byte-identical to the pre-#1962 shape. New consumers branch on the presence of `recommendation` (or, equivalently, `schema_version >= 2`); old consumers see no envelope change. The `egg-orch overseer alert` CLI gains `--recommendation file_issue --recommendation-payload-file /path/to/payload.json` flags that populate the new fields; the sandbox handler at `sandbox/egg_agent_tools/handlers/progress.py::progress_overseer_alert` enforces a 50 KB cap on the payload and validates that `recommendation` is one of the legal values (`file_issue` is currently the only accepted value).

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
- `egg-orch message send` to redirect individual agents
- `egg-orch overseer alert` to broadcast `OVERSEER_ALERT` notifications to all (always with explicit pipeline routing; use this instead of `message send` for anomaly escalation — `HANDOFF`/`STATUS` types blend into normal inter-agent traffic)

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

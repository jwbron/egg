# Analysis: Overseer should escalate infrastructure errors to human, not just nudge

> Issue: #1489 | Phase: refine

## Problem Statement

When an agent encounters an infrastructure error (e.g., `git add` fails due to `.gitignore` rules, gateway returns 403, permission denied on file write), the overseer currently treats it the same as a generic slow agent. It sends nudge messages like "please ACK or NACK" rather than recognizing that the agent has hit an unrecoverable infrastructure problem and escalating to a human who can fix the underlying issue.

**Current state**: The overseer detects agent slowness via heartbeat/progress stalls and classifies the situation using the Haiku `classify_stall` classifier. But this classifier only sees high-level progress events and logs — it doesn't have a dedicated code path for recognizing infrastructure errors, so it often classifies them as "stuck" or "working" and produces generic nudge/redirect actions.

**Desired outcome**: The overseer should distinguish between "agent is slow" (nudge) and "agent hit an infrastructure error it cannot resolve" (escalate to HITL with error details).

## Current Behavior

The pipeline health monitoring uses a two-tier architecture:

**Tier 1 (Orchestrator — deterministic tripwires)** in `orchestrator/health_monitor.py`:
- Tracks heartbeat timeouts, container exits, repeated errors, message rate spikes, progress stalls
- The `_on_error` handler (line 162) tracks repeated identical errors and escalates after a threshold (default: 3 identical errors)
- However, this only fires on explicit `ERROR` events emitted by the orchestrator itself — it does **not** inspect agent tool results or progress events for infrastructure errors

**Tier 2 (Overseer — LLM-powered)** in `orchestrator/overseer/`:
- `monitor.py`: Polls every 30s, queries health alerts and progress events, routes through classifiers
- `classifier.py`: Has `classify_stall()` (line 80) and `classify_error()` (line 138), but:
  - `classify_stall()` receives generic logs/progress and consensus state — no infrastructure error context
  - `classify_error()` is capable of classifying error severity but is only invoked when an error has already been identified — there's no code path that proactively scans progress events for infrastructure error patterns
- `decision_maker.py`: Has a corrective action ladder (nudge → redirect → HITL → issue → Slack), but infrastructure errors get routed through the same stall-detection path as legitimate slowness

**The specific gap observed in pipeline `issue-1481`**:
1. `reviewer_refine` hit a `.gitignore` error preventing `git add .egg-state/reviews/`
2. The orchestrator detected the reviewer was slow (~6 min after proposal)
3. The orchestrator escalated to the overseer with a heartbeat/progress stall alert
4. The overseer's `classify_stall` saw stale progress events but no explicit infrastructure error signal
5. The overseer sent a generic "please ACK or NACK" nudge
6. The reviewer worked around the issue by NACKing without a committed verdict

## Constraints

- **No code access for overseer**: The overseer container has no git repository access. It can only observe agents through the progress API, health alerts, message bus, and checkpoint data.
- **Structured progress events are agent-emitted**: Agents must explicitly emit `egg-orch progress emit --state blocked --blocker "..."` for the overseer to see infrastructure blockers. If agents don't emit blocked state, the overseer won't know about the error.
- **Haiku cost budget**: The overseer aims for ~1-2 Haiku calls per poll cycle per agent. Adding more classification calls increases cost.
- **Existing escalation safety net**: The `_execute_action` method (monitor.py line 448) already auto-upgrades nudge/redirect to HITL if the message contains phrases like "human intervention required." This provides a partial safety net but depends on the LLM phrasing.
- **Backward compatibility**: The progress event schema (`working`, `blocked`, `complete` states) already supports a `blocker` field that could carry infrastructure error context.
- **Gateway error detection**: The gateway health endpoint is already accessible to the overseer at `http://egg-gateway:9848/api/v1/health`.

## Options Considered

### Option A: Add infrastructure error detection to the overseer poll cycle

**Approach**: Add a new deterministic health check in the overseer's poll cycle that inspects progress events for `blocked` state with infrastructure-related blocker keywords (e.g., "git", "permission denied", "EROFS", "gateway", ".gitignore", "403 Forbidden"). When detected, skip the stall classifier and directly escalate to HITL with the error details.

**Pros**:
- Deterministic detection — no LLM cost for clear-cut infrastructure errors
- Fast response — detects on the next poll cycle (30s) rather than waiting for multiple redirect attempts
- Works within the existing progress event schema (agents already can emit `blocked` state with blocker details)
- Follows the existing pattern of deterministic health checks in the overseer (like `_check_rerun_anomaly`, `_check_status_consistency`)

**Cons**:
- Depends on agents actually emitting `blocked` progress events with descriptive blocker text — if agents don't emit these, the detection won't fire
- Keyword-based matching is brittle — new infrastructure error types might be missed
- Only covers the overseer tier — doesn't improve the orchestrator's Tier 1 detection

### Option B: Enhance the Haiku error classifier with infrastructure error awareness

**Approach**: Modify the `classify_error` and `classify_stall` classifier prompts to explicitly recognize infrastructure error patterns. Add a new classification output category (e.g., `infrastructure_error`) and configure the decision maker to auto-escalate infrastructure errors to HITL regardless of redirect count.

**Pros**:
- Leverages existing LLM-based classification — handles novel error patterns beyond keyword lists
- No changes needed to agent progress emission behavior
- Can classify errors from unstructured log data (e.g., checkpoint transcripts)

**Cons**:
- LLM classification is probabilistic — may still misclassify infrastructure errors as generic stalls
- Higher cost per classification (Haiku call per anomaly)
- Slower response — must wait for the classification round-trip
- Doesn't address the root issue that the overseer's stall handler doesn't pass rich error context to the classifier

### Option C: Tier 1 orchestrator infrastructure error tripwire

**Approach**: Add a new deterministic tripwire rule in `orchestrator/health_monitor.py` that listens for progress events with `state=blocked` and infrastructure-related blocker text. When detected, create a `critical` severity alert with type `infrastructure_error` that triggers immediate HITL escalation (bypassing the overseer entirely for clear-cut cases).

**Pros**:
- Fastest response — no LLM or overseer involvement needed
- Most deterministic — simple keyword/pattern matching on structured progress data
- Aligns with the existing Tier 1 design principle: handle clear-cut cases without LLM cost
- Creates alerts visible to both the overseer and the health endpoint

**Cons**:
- Depends on agents emitting structured blocked progress events
- May bypass the overseer's contextual analysis that could provide richer diagnostic information
- New alert type requires updates to the alert resolution endpoint and monitoring UIs

### Option D: Combined approach (Recommended)

**Approach**: Implement both Tier 1 detection (Option C) and enhanced Tier 2 classification (Option B), with the Tier 1 tripwire handling clear-cut infrastructure errors immediately and the enhanced classifier catching ambiguous cases.

**Pros**:
- Defense in depth — clear-cut errors handled instantly, ambiguous cases still classified by LLM
- Follows the existing two-tier architecture pattern
- Agents that emit structured blocked events get fast resolution; agents that don't still benefit from enhanced classification

**Cons**:
- More implementation work than a single-tier approach
- Need to avoid duplicate escalations when both tiers detect the same error
- Requires updates across multiple components (orchestrator, overseer, possibly classifier prompts)

## Recommended Approach

**Option D (Combined approach)** is recommended because it follows the established two-tier architecture pattern and provides defense in depth:

1. **Tier 1 (Orchestrator)**: A new `infrastructure_error` tripwire that detects `blocked` progress events with infrastructure-related blocker keywords. This provides instant, zero-LLM-cost detection for agents that emit structured progress events.

2. **Tier 2 (Overseer)**: Enhanced `classify_stall` and `classify_error` prompts that explicitly recognize infrastructure error patterns and produce a distinct classification category. The decision maker is updated to auto-escalate `infrastructure_error` classifications to HITL.

3. **Agent-side guidance**: Update agent rules/prompts to encourage emitting `egg-orch progress emit --state blocked --blocker "git add failed: .gitignore rule"` when infrastructure errors are encountered.

The Tier 1 tripwire should include a deduplication mechanism (similar to `heartbeat_escalated` flags) to prevent both tiers from creating duplicate escalations.

**Complexity assessment**: **medium** — Multi-file change across orchestrator (health_monitor.py, overseer/classifier.py, overseer/monitor.py, overseer/decision_maker.py) and possibly sandbox (agent rules), but follows well-established patterns in the codebase.

## Open Questions

> **Note**: `egg-contract add-decision` and `egg-contract add-feedback` commands failed because the gateway's contract endpoint returned "Contract for issue #1489 not found" (the contract file exists in the branch but is not visible to the gateway). The questions below should be registered once the contract is accessible. Commands to run:

### Decision 1: Detection approach

```bash
egg-contract add-decision \
  --question "Should the overseer detect infrastructure errors proactively by inspecting progress events, reactively by enhancing classifiers, or both?" \
  --options \
    "Proactive: Add new infra error detection in overseer poll cycle (Option A/C)" \
    "Reactive: Enhance existing classifiers to recognize infrastructure errors (Option B)" \
    "Both: Proactive detection for structured events plus enhanced classification (Option D — recommended)" \
  --format markdown
```

### Decision 2: Escalation behavior for infrastructure errors

```bash
egg-contract add-decision \
  --question "When the overseer detects an infrastructure error, should it skip the nudge/redirect steps entirely and go straight to HITL escalation?" \
  --options \
    "Direct HITL: Infrastructure errors are not agent-fixable, skip nudge/redirect entirely" \
    "One redirect attempt: Give agent one chance with specific guidance, then HITL if unresolved" \
    "Configurable: Add a config option (e.g. overseer_infra_error_max_redirects) defaulting to 0" \
  --format markdown
```

### Feedback 1: Error pattern scope

```bash
egg-contract add-feedback \
  --question "Are there specific infrastructure error patterns beyond git failures, gateway errors, and permission denied that should be included in the detection logic? (e.g., Docker socket errors, DNS resolution failures, disk space issues)" \
  --question "Should the overseer also attempt to diagnose the root cause (e.g., checking .gitignore rules, gateway health endpoint) before escalating, or should it simply surface the raw error to the human?" \
  --format markdown
```

### Additional open questions

- **Agent progress emission**: Should agents be required to emit `blocked` progress events on infrastructure errors, or is this only a best-effort enhancement? If required, should the agent rules be updated to enforce this?
- **Alert type naming**: Should the new Tier 1 alert type be `infrastructure_error`, or something more specific like `git_error`, `gateway_error`, etc.? A single category is simpler but less informative; multiple categories enable finer-grained alerting rules.
- **Deduplication window**: When both Tier 1 and Tier 2 detect the same infrastructure error, how long should the deduplication window be? The current pattern uses per-agent boolean flags (`heartbeat_escalated`), but a time-based window might be more appropriate for errors that persist across multiple poll cycles.
- **Scope of #1487**: Issue #1487 (`.egg-state/reviews/` is gitignored) is the root cause of the specific incident. Should this issue (#1489) also address that root cause, or focus solely on the overseer's detection and escalation behavior?

---

*Authored-by: egg*

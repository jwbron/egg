# Risk Register — Issue #3665 — Supervision, Second Pass

**Plan under review:** `issue-3665-v3-plan.md` (task_planner, commit 6092b5a7a)
**Risk analyst:** risk_analyst
**Date:** 2026-07-27
**Base:** main @ 1cd0c8ad7

## Executive Summary

Overall risk: **MEDIUM**. The plan addresses a real and documented problem (seven undetected
repetition loops, false-positive alerts, timeout kills counted as crashes) with a well-structured
five-slice linear chain. All technical claims have been verified against the live tree. The primary
risks are:

1. **Slice-2 hot-loop integration** — wiring the detection plane into RUNTIME_TICK is the highest-risk
   change. The double-evaluation guard is critical since `_run_runtime_tick_checks()` is called from
   both `_check_pod` (line 219) and `_reconciliation_sweep` (line 621).
2. **Snapshot field population** — slice-1 populates 8 new fields on `EventStreamSnapshot`. The
   `consensus` field depends on `get_peer_consensus_tracker()` which may return `None`; the plan
   correctly notes this must be handled gracefully.
3. **Log fidelity for loop detection** — slice-3 TASK-3-2 increases log capture fidelity, but the
   k8s log API truncation (~100 chars/line) is a platform constraint, not just a config issue.

## Risks by Slice

### Slice 1 — Populate EventStreamSnapshot fields (Medium risk)

**R1.1: `consensus` field population depends on tracker availability**
- **Severity:** MEDIUM
- **Likelihood:** MEDIUM
- **Impact:** If `get_peer_consensus_tracker()` returns `None` (no tracker registered for the
  pipeline), the consensus field will be empty and BRC-thrash / incomplete-consensus-deferral
  detectors won't fire. This is a silent failure — the detector simply won't trigger.
- **Mitigation:** The plan's TASK-1-2 must handle `None` tracker gracefully (default to empty dict,
  not crash). The detection plane's `evaluate()` is already exception-isolated, so a crash in one
  detector won't take down the whole plane.
- **Status:** Addressed by plan design (exception isolation in `DetectionPlane.evaluate()`)

**R1.2: `RunningAgent` role fix may break existing consumers**
- **Severity:** LOW
- **Likelihood:** LOW
- **Impact:** Changing `RunningAgent.role` from container ID to agent role is a semantic change.
  Any existing code that reads `RunningAgent.role` and expects a container ID will break.
- **Mitigation:** The plan correctly scopes this to the detection plane's snapshot builder. The
  `RunningAgent` dataclass is only consumed by the detection plane's detectors, not by other
  subsystems.
- **Status:** Low risk, properly scoped

**R1.3: `midturn_messages` population depends on log parsing**
- **Severity:** MEDIUM
- **Likelihood:** MEDIUM
- **Impact:** The `midturn_messages` field feeds the loop detector in slice-3. If the log parsing
  is unreliable (truncated lines, format changes), the loop detector will have false negatives.
- **Mitigation:** TASK-1-5 and TASK-3-2 together address this — slice-1 populates the field,
  slice-3 improves log fidelity. The plan correctly sequences these.
- **Status:** Addressed by plan sequencing

### Slice 2 — Wire detection plane into RUNTIME_TICK (High risk)

**R2.1: Hot-loop integration — double-evaluation guard**
- **Severity:** HIGH
- **Likelihood:** MEDIUM
- **Impact:** `_run_runtime_tick_checks()` is called from both `_check_pod` (line 219) and
  `_reconciliation_sweep` (line 621). If the detection plane is called from both paths without
  a guard, detectors will fire twice per sweep, potentially doubling alert volume and wasting
  CPU. The plan correctly identifies this as a risk.
- **Mitigation:** TASK-2-1 must implement a deduplication mechanism — either a per-pipeline
  timestamp check (only evaluate if N seconds have passed since last evaluation) or a
  per-sweep flag. The plan's acceptance criteria explicitly require "no double-evaluation."
- **Status:** Properly identified and scoped

**R2.2: Exception isolation on the hot path**
- **Severity:** HIGH
- **Likelihood:** LOW
- **Impact:** If the detection plane raises an unhandled exception, it could crash the
  RUNTIME_TICK sweep, taking down health monitoring for all pipelines. The `DetectionPlane.evaluate()`
  method is already exception-isolated per-detector, but the snapshot builder
  (`snapshot_from_health_context()`) is NOT — if it raises, the whole tick fails.
- **Mitigation:** The snapshot builder is already defensive ("Best-effort and defensive: the
  detection plane runs on the event loop and must never crash on a partially-populated context").
  TASK-2-1 should wrap the detection plane call in a try/except as a belt-and-suspenders measure.
- **Status:** Largely mitigated by existing defensive design

**R2.3: Finding routing — overseer vs corrective executor**
- **Severity:** MEDIUM
- **Likelihood:** MEDIUM
- **Impact:** The plan routes `requires_adjudication` findings to the overseer agent and routine
  findings to the corrective executor. If this routing is misconfigured, either the overseer gets
  spammed with routine findings (alert fatigue) or critical findings are silently swallowed
  by the corrective executor.
- **Mitigation:** The `Finding` dataclass already has a `requires_adjudication` field. The routing
  logic in TASK-2-1 must check this field explicitly. Tests in TASK-2-3 should verify both paths.
- **Status:** Properly scoped, needs careful implementation

### Slice 3 — Deterministic loop detector (Medium risk)

**R3.1: Log truncation limits detection fidelity**
- **Severity:** MEDIUM
- **Likelihood:** HIGH
- **Impact:** The k8s log API truncates at ~100 chars per line (`read_job_log_snapshot` at
  `kubernetes_client.py:455`). The issue explicitly states "tool inputs are truncated at about
  100 characters and distinct commands sharing a prefix collapse together." TASK-3-2 addresses
  this by increasing log fidelity, but the k8s API truncation is a platform constraint that
  may not be fully solvable.
- **Mitigation:** The plan's approach of counting *distinct* tool-input strings (not just
  counting calls) is correct — even with truncation, if no new inputs appear in a window,
  it's a loop. TASK-3-2 should focus on ensuring the agent's own log store captures full
  tool inputs, not just relying on k8s log API.
- **Status:** Properly identified, partial mitigation possible

**R3.2: False positives on legitimate single-tool agents**
- **Severity:** MEDIUM
- **Likelihood:** MEDIUM
- **Impact:** An agent that legitimately makes the same tool call repeatedly (e.g., polling an
  API with the same parameters) could trigger the loop detector. The issue's empirical finding
  is that "a working agent produces new ones and a loop of any length produces none" — but
  some legitimate work patterns involve repeated identical calls.
- **Mitigation:** The trailing-window approach (5 polls) with N consecutive zero-new-input polls
  before firing provides a natural debounce. The `requires_adjudication=False` flag means these
  fire as routine findings, not operator alerts, reducing noise.
- **Status:** Mitigated by design

### Slice 4 — Timeout visibility and classification (Medium risk)

**R4.1: Distinguishing timeout-kills from real crashes**
- **Severity:** MEDIUM
- **Likelihood:** MEDIUM
- **Impact:** Both timeout-kills and real crashes produce exit code -1. The plan proposes
  distinguishing them via the agent result's error message ("Timed out after {timeout} seconds").
  If this string matching is fragile (e.g., the message format changes), timeout-kills will
  be misclassified as crashes and increment the failure streak.
- **Mitigation:** The error message is generated in a single location (`client.py:914`), so
  the string is stable. TASK-4-3 should use a structured field (e.g., a `timeout` flag on
  `AgentResult`) rather than string matching if possible.
- **Status:** Low risk due to single source of truth

**R4.2: `agent_timeout_seconds` config field**
- **Severity:** LOW
- **Likelihood:** LOW
- **Impact:** Adding a new config field to `PipelineConfig` is low-risk. The default of 7200
  preserves existing behavior. Validation `>= 60` prevents nonsensical values.
- **Status:** Low risk, properly scoped

**R4.3: Heartbeat warning timing**
- **Severity:** LOW
- **Likelihood:** LOW
- **Impact:** Emitting a heartbeat at 90 minutes is informational. If the timing is slightly
  off, the impact is minimal — the agent gets a slightly early or late warning.
- **Status:** Low risk

### Slice 5 — Alert evidence + false-positive fixes (Medium risk)

**R5.1: Timestamp unification may regress progress gate behavior**
- **Severity:** MEDIUM
- **Likelihood:** MEDIUM
- **Impact:** The plan unifies the timestamp source between `_check_convergence_stall()` and
  `_has_recent_peer_progress()`. Both currently use `tracker.get_latest_progress_timestamp()`
  but with different gate windows. If the unification changes the gate window behavior, it
  could either suppress legitimate alerts or generate false positives.
- **Mitigation:** The plan correctly notes this depends on slice-4 TASK-4-3. The fix should
  preserve the existing gate window semantics while using a single timestamp source. Tests in
  TASK-5-4 must verify convergence-stall doesn't fire when peer heartbeat is recent.
- **Status:** Properly identified, needs careful testing

**R5.2: Alert evidence enrichment**
- **Severity:** LOW
- **Likelihood:** LOW
- **Impact:** Adding structured evidence to `_broadcast_alert()` is additive — it doesn't
  change the alert logic, just the payload. The risk is that the evidence collection itself
  could fail and suppress the alert.
- **Mitigation:** The evidence collection should be best-effort (wrapped in try/except),
  similar to the existing `_emit_finding` pattern in `runner.py:184`.
- **Status:** Low risk, properly scoped

## Overall Assessment

**Verdict: PROCEED_WITH_MITIGATIONS**

The plan is well-structured, properly scoped, and all technical claims have been verified
against the live tree. The five-slice linear chain correctly accounts for file overlaps
(#3046). The riskiest item is slice-2's hot-loop integration, but the plan correctly
identifies the double-evaluation guard as critical and includes tests for it.

Key mitigations to verify during implementation:
1. Slice-2: Exception isolation around the detection plane call in RUNTIME_TICK
2. Slice-3: The loop detector's trailing-window debounce (N consecutive zero-new-input polls)
3. Slice-4: Use structured fields, not string matching, for timeout classification
4. Slice-5: Preserve existing gate window semantics when unifying timestamp sources

## What to leave out (confirmed)

- Do not rebuild the overseer agent (working; problem is input pipeline).
- Do not remove the HealthMonitor tripwires (wired and working).
- Do not add LLM classification to the hot path.
- Do not change the 2-hour timeout default.
- Tier 3–5 from the candidate list are input to the gate, not a work queue.

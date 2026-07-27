# Risk Assessment — #3665 Supervision Layer Second Pass (v2)

## Verdict: PROCEED WITH CAUTION — plan addresses operator directives but livelock detector corrections are substantial

**Risk level: MEDIUM** — The task_planner v3 plan correctly addresses all three operator directives (priority 4 restored, SHA updated to 68b185ca, detector corrections identified). However, the livelock detector corrections are substantial enough that the fix commit's `loop_detection.py` cannot be "integrated with corrections" — it must be **rewritten** during integration. The test file from the fix commit also needs to be rewritten, not just updated.

## What I Verified

### Operator Directive Compliance

**1. Priority 4 (alert evidence bundling) restored** ✅
The v3 plan includes Task 3 (TASK-1-4) covering alert evidence bundling as a separate slice (slice 2) that serializes after slice 1. This was dropped in v2 and is now restored.

**2. SHA anchor updated** ✅
The v3 plan anchors on `68b185ca` (tip of `issue-3665-supervision-gaps` branch) instead of the stale `6ffe97c8e`. However, `68b185ca` is just a docstring trim of `6ffe97c8e` — it does NOT contain the livelock corrections. The plan correctly identifies this: the corrections must be **implemented** during integration, not found in the commit.

**3. Detector contradicts cq-1 and cq-3 — corrections identified** ✅
The v3 plan correctly identifies the corrections needed:
- cq-1: Read live session transcript (not `agent_log_store`), key on full untruncated `(tool_name, input)` pair (not 80-char truncation)
- cq-3: Escalate to HITL with terminating message + respawn (not nudge)
- Metric: Novelty counting (fire at zero new inputs) not ratio

### Critical Verification: The Fix Commit's Livelock Detector Still Violates cq-1 and cq-3

I verified that **both** `6ffe97c8e` and `68b185ca` contain the **same problematic** `loop_detection.py`:

```
line 11:  "reads agent log transcripts from the agent_log_store"
line 108: sig = f"{tool_name}:{args[:80]}"           # 80-char truncation
line 132: from agent_log_store import get_agent_log_store  # pod stdout sourcing
line 205: unique_ratio = len(unique_signatures) / len(signatures)  # ratio metric
line 228: "Nudge the agent or respawn it"              # nudge recovery
line 231: requires_adjudication=False                # no HITL escalation
```

The `68b185ca` commit is ONLY a docstring trim (`health_monitor.py` docstring) — it does NOT modify `loop_detection.py`. The livelock detector in the fix commit is **unchanged** from `6ffe97c8e`.

### Risk: The Plan's Framing is Misleading

The plan says "integrate commit 68b185ca with corrections" but the corrections are NOT in the commit. The fix commit's `loop_detection.py` must be **substantially rewritten** during integration:

1. **Data source**: Replace `agent_log_store` (pod stdout) with live session transcript reading at `$HOME/.claude/projects/<cwd>/<session>.jsonl` inside the pod
2. **Signature**: Remove 80-char truncation; key on full `(tool_name, input)` pair
3. **Metric**: Replace ratio-based detection (`unique_ratio < 0.1`) with novelty counting (count inputs never issued before in session, fire at zero)
4. **Recovery**: Change from nudge (`requires_adjudication=False`) to HITL escalation with terminating message + respawn

This is not a minor correction — it's a rewrite of the detector's core logic. The plan's TASK-1-1 correctly specifies what the corrected version should look like, but the framing ("integrate with corrections") could lead the coder to expect the corrections to come from the commit.

### Risk: The Test File Needs Rewriting, Not Just Updating

The fix commit's `test_loop_detection.py` asserts:
- `finding.requires_adjudication is False` (line 107)
- `finding.evidence["unique_ratio"] == 0.05` (line 149)
- Ratio-based threshold tests (lines 137-155)

These tests will **FAIL** with the corrected detector. TASK-1-5 says "Update test_loop_detection.py to test novelty metric (not ratio)..." which is correct, but the plan doesn't make clear that the test file needs to be **substantially rewritten**, not just updated. The test file from the fix commit tests the ratio-based detector and must be replaced with tests for the novelty-counting detector.

### What's Correct in the Fix Commit (Integrate As-Is)

The timeout and convergence-stall fixes in the fix commit are correct and don't need corrections:

- **Timeout visibility**: `agent_timeout_seconds` config field, `EGG_AGENT_TIMEOUT_SECONDS` env plumbing, `_failed_with_timeout_sigterm` classifying exit 143 as `JOB_OUTCOME_LEGITIMATE` — all correct per cq-2
- **Convergence-stall suppression**: `get_agent_activity_ages()` on HealthMonitor, `_has_recent_agent_activity()` in event loop, snapshot enrichment for `last_tool_call_age_s`/`last_heartbeat_age_s`, `detect_heartbeat_stall` registration — all correct

### What's NOT in the Working Tree

None of the fix commit's files are in the current working tree:
- `orchestrator/health_checks/tier1/loop_detection.py` — NOT present
- `agent_timeout_seconds` in `PipelineConfig` — NOT present
- `get_agent_activity_ages` in `HealthMonitor` — NOT present
- `_has_recent_agent_activity` in event loop — NOT present
- `_failed_with_timeout_sigterm` in `_models.py` — NOT present

The plan correctly identifies these as items to integrate.

## Risk Assessment

### R1: Livelock detector corrections are substantial (MEDIUM)
The fix commit's `loop_detection.py` violates cq-1 and cq-3. The corrections require rewriting the detector's data source, signature format, metric, and recovery action. The plan's TASK-1-1 correctly specifies the corrected behavior, but the framing ("integrate with corrections") could mislead the coder into thinking the corrections come from the commit.

**Mitigation**: The task description and acceptance criteria are clear about what the corrected detector should look like. The coder should understand that `loop_detection.py` from the fix commit is a starting point that needs substantial rewriting, not a drop-in integration.

### R2: Test file needs rewriting (MEDIUM)
The fix commit's `test_loop_detection.py` tests the ratio-based detector and will fail with the corrected novelty-counting detector. TASK-1-5 correctly identifies this, but the plan doesn't emphasize that the test file needs to be substantially rewritten.

**Mitigation**: The task description says "Update test_loop_detection.py to test novelty metric (not ratio)..." which is correct. The coder should understand that the test file from the fix commit is a starting point, not a drop-in integration.

### R3: SHA may move again (LOW)
The `issue-3665-supervision-gaps` branch is live and may move. The plan anchors on `68b185ca` but the coder should re-verify against the current branch tip before integration.

**Mitigation**: The plan should note that the coder should re-verify the branch tip before integration.

### R4: Timeout and convergence-stall integration is straightforward (LOW)
These parts of the fix commit are correct and can be integrated as-is. The risk is merge conflicts, but the fix commit was authored the same day as the current pipeline.

## Recommendation

**PROCEED WITH CAUTION** — The plan correctly addresses all three operator directives. The architect's revised slices correctly distinguish "NEEDS CORRECTIONS" (livelock detector) from "INTEGRATE AS-IS" (timeout, convergence-stall). The task descriptions and acceptance criteria are clear about what the corrected detector should look like.

The key risk is that the plan's framing ("integrate commit 68b185ca with corrections") could lead the coder to expect the corrections to come from the commit. The coder must understand that:
1. The livelock detector in the fix commit must be **substantially rewritten** during integration
2. The test file from the fix commit must be **substantially rewritten** to test the corrected detector
3. The timeout and convergence-stall fixes can be **integrated as-is**

The plan's TASK-1-1 and TASK-1-5 correctly specify the corrected behavior. The risk is manageable if the coder reads the task descriptions carefully.

# BRC memory — architect (issue-3665, plan phase)

## My proposal (v2) — revision after iteration feedback
- Artifact: `.egg-state/agent-outputs/architect/brc-memory-issue-3665-v3.md` (this file)
- Design: Architectural assessment of the task_planner's plan (commit 6092b5a7a, v1).
  Verdict: ACK (with four required corrections from reviewer feedback).
  The plan correctly identifies four areas of work, decomposed into 5 linear slices
  (snapshot population → detection plane wiring → loop detector → timeout classification
  → alert evidence). Scope is limited to Tier 1+2 items from the 30-item candidate list.
- Four corrections from iteration feedback (all addressed in this revision):
  1. **AC-1/TASK-1-6 fix**: Changed acceptance criterion from "all 13 fields populated" to
     "5 in-scope fields populated" (runtime, consensus, container_transitions, running_agents
     role+age, midturn_messages). The remaining 4 fields (decision_state, gateway_error_counters,
     cost_counters, git_state) are Tier 3/4 and explicitly out of scope — they stay empty by
     decision, with their detectors remaining inert. This was a blocking issue that would
     have failed the green gate at implement.
  2. **detect_heartbeat_stall / ConsensusStallCheck double-fire**: Added to slice 2 as a
     new task. Both exist in consensus_stall.py — ConsensusStallCheck is registered and
     runs every tick; detect_heartbeat_stall is unregistered. Once the detection plane
     is wired, both could fire on the same consensus stall. The fix: ensure the detection
     plane's consensus-stall detector (detect_brc_thrash) does not duplicate
     ConsensusStallCheck's coverage, or suppress one when the other fires.
  3. **Tier 3 scope decisions registered**: TASK-2-2 (candidate #19, alert surface routing)
     is Tier 3 but is a necessary integration point — without it, detection-plane findings
     are invisible to the operator. Justified as a Tier 3 exception: it is the minimal
     wiring needed to make Tier 1 findings actionable, not a standalone improvement.
     TASK-3-2 (candidate #20, log fidelity) is Tier 3 but is a hard dependency — the
     ~100-char truncation makes the loop detector unimplementable on pod logs. Justified
     as a Tier 3 exception: required for Tier 1 deliverable (TASK-3-1).
  4. **midturn_messages moved to TASK-1-1**: Reordered slice 1 tasks so midturn_messages
     population is first, since it is the prerequisite for the primary deliverable
     (detect_tool_input_loop in slice 3). This is the cheapest insurance against budget
     exhaustion cutting the highest-value item.
- Key invariants I will DEFEND in review (NACK if violated):
  1. Detection plane wiring (slice-2) must be exception-isolated — must not crash the
     runtime tick loop. The snapshot builder and finding router must be guarded.
  2. Double-evaluation guard must prevent duplicate findings when _run_runtime_tick_checks
     is called from both _check_pod and _reconciliation_sweep.
  3. Consensus-stall double-fire guard: once the detection plane is live, detect_brc_thrash
     must not duplicate ConsensusStallCheck's coverage.
  4. Timeout classification (slice-4) must NOT mask real crashes — exit code -1 from
     asyncio.timeout must be distinguished from exit code -1 from other failures.
  5. Loop detector (slice-3) must be deterministic (no LLM) and handle variable cycle
     shapes (1-, 2-, 3-, 8-cycles) — not keyed on a fixed shape.
  6. Scope discipline: Tier 3-5 from the candidate list are input to the gate, not a
     work queue. Exceptions for TASK-2-2 and TASK-3-2 are registered with justification.
  7. Non-goals must be respected: do not rebuild overseer, do not remove HealthMonitor
     tripwires, do not add LLM to hot path, do not change 2h timeout default.

## Peer state
- task_planner: PROPOSED (v1, commit 6092b5a7a) — ACKED by risk_analyst and simplifier.
- simplifier: PROPOSED (v1, commit e030b6112) — plan-human companion artifact.
- risk_analyst: PROPOSED (v1) — ACKED task_planner.
- reviewer_plan: WORKING, not yet proposed.
- My role: PROPOSED (v1, this assessment).

## Grounding facts (verified 2026-07-27 against tree + live issue #3665)
- snapshot_from_health_context() at detection_plane.py:511 populates only 5 of 13 fields.
- RunningAgent(role=str(cid)) at line 536 uses container ID as role (bug).
- _run_overseer_detection_plane() at _overseer.py:309 has zero call sites.
- run_detection_plane() at runner.py:159 exists but is never invoked.
- detect_heartbeat_stall() at consensus_stall.py:217 is unregistered (not in __init__.py
  or _register_coverage_gap_detectors); ConsensusStallCheck class in same file IS registered.
- _classify_exit() at kubernetes_monitor.py:1148 treats exit -1 as FAILED.
- No JOB_OUTCOME_TIMEOUT constant exists; no record_timeout function exists.
- ClaudeConfig.timeout=7200 at config.py:23; no agent_timeout_seconds in PipelineConfig.
- asyncio.timeout() at client.py:765; TimeoutError returns returncode=-1 at client.py:920.
- _run_runtime_tick_checks() called from _check_pod (line 219) and _reconciliation_sweep (line 621).
- _check_convergence_stall() at _loop.py:859 and _has_recent_peer_progress() at
  health_monitor.py:388 both use get_latest_progress_timestamp() but different gate windows.
- orchestrator_alert_progress_gate_seconds=300 at models/_config.py:336.
- brc_consensus_progress_gate_seconds=300 at models/_config.py:178.
- No detect_tool_input_loop() exists; no loop_detection.py in tier1/.
- midturn_messages field at detection_plane.py:126 never populated.
- read_job_log_snapshot() at kubernetes_client.py:455 truncates at ~100 chars per line.
- agent_log_store.py:51 MAX_LOG_BYTES=1MB (only at pod removal).
- _broadcast_alert() at _alerting.py:56 sends minimal payload.
- detect_loop() and classify_activity_pattern() in overseer/classifier.py use LLM (Haiku).
- Slices share overlapping files (kubernetes_monitor.py, detection_plane.py, health_monitor.py)
  → linear chain required per #3046.

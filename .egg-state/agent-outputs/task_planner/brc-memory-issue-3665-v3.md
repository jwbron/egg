# BRC Memory — task_planner, Issue #3665

## Pipeline identity
- Issue: #3665 — Supervision, second pass: the layer was silent on seven livelocks and loud at healthy agents
- Role: task_planner
- Phase: plan

## Verdict / state
- Proposed plan: `.egg-state/drafts/issue-3665-v3-plan.md` (5 linear slices, 21 tasks).
- Commit: 6092b5a7afd4e018c2c7f83fdf1161cbab01430e
- Status: proposed (version 1) to reviewers (reviewer_plan, risk_analyst, simplifier)
- Phase: plan (current_phase confirmed via BRC state)

## Plan shape (for consistency across re-invocations)

Five linear slices (per #3046 overlap constraint — slices share overlapping files):

- **slice-1 (root):** Populate all 13 EventStreamSnapshot fields in
  `snapshot_from_health_context()`. Tasks: runtime, consensus, container_transitions,
  running_agents fix, midturn_messages, tests. Prerequisite for everything.
  6 tasks (5 coder, 1 tester).

- **slice-2 (dep 1):** Wire `_run_overseer_detection_plane()` into RUNTIME_TICK
  with double-evaluation guard + route findings to alert surface.
  3 tasks (2 coder, 1 tester).

- **slice-3 (dep 2):** Deterministic unique-tool-input loop detector
  `detect_tool_input_loop()` in `health_checks/tier1/` + increase log capture fidelity.
  3 tasks (2 coder, 1 tester).

- **slice-4 (dep 3):** Timeout visibility and classification — add
  `agent_timeout_seconds` to PipelineConfig, pass EGG_AGENT_TIMEOUT env, classify
  timeout-killed pods (exit -1) as clean timeouts, surface heartbeat warning.
  5 tasks (4 coder, 1 tester).

- **slice-5 (dep 4):** Alert evidence + false-positive fixes — enrich OVERSEER_ALERT
  payloads with evidence, unify convergence-stall timestamp source, name timeout
  explicitly in alerts.
  4 tasks (3 coder, 1 tester).

Total: 21 tasks (16 coder, 5 tester).

## Key decisions (defend on NACK unless reviewer shows them wrong)

1. **Linear chain, not parallel slices.** The four analysis areas share overlapping
   files (kubernetes_monitor.py, detection_plane.py, health_monitor.py). Per #3046,
   overlapping slices must be ordered along one dependency chain. The linear chain
   slice-1 → slice-2 → slice-3 → slice-4 → slice-5 satisfies this constraint.

2. **Snapshot population first.** The gate's carry-into-plan note explicitly says
   "Area 1 steps 1-4 before step 5" and "Area 3 step 1 is the highest-value item."
   The midturn_messages field (TASK-1-5) is sequenced before the loop detector
   (slice-3) to satisfy this dependency.

3. **Timeout classification via error message, not new exit code.** The timeout-killed
   pod exits with code -1 (from asyncio.timeout in client.py), not a custom exit code.
   The discriminator is the agent result's error message "Timed out after {timeout}
   seconds". This avoids changing the agent CLI's exit code contract.

4. **Scope discipline: Tier 1-2 only.** Per the gate's instruction: "Do not expand
   beyond the Tier 1 and Tier 2 items without registering a decision. Tier 3 through
   Tier 5 are input to the gate, not a work queue." All 21 tasks map to Tier 1-2 items.

5. **Do not change the 2-hour timeout default.** 7200s is reasonable. The fix is to
   make it visible and classify timeout-kills distinctly, not to shorten it.

6. **Do not add LLM classification to the hot path.** The classify_stall() /
   classify_activity_pattern() functions in overseer/classifier.py are expensive
   (Haiku calls). Keep them in the overseer agent, not in the deterministic detection
   plane.

## Grounded anchors (verified @ 1cd0c8ad7)

- `snapshot_from_health_context()` at `health_checks/detection_plane.py:511` —
  populates only 5 of 13 fields.
- `RunningAgent(role=str(cid))` at `detection_plane.py:536` — uses container ID as role.
- `_run_overseer_detection_plane()` at `routes/pipelines/_overseer.py:309` — zero call sites.
- `_run_runtime_tick_checks()` at `kubernetes_monitor.py:221` — called from `_check_pod`
  (line 218) and `_reconciliation_sweep` (line 616).
- `run_detection_plane()` at `health_checks/runner.py:159` — exists but never invoked.
- `_classify_exit()` at `kubernetes_monitor.py:1148` — treats -1 as FAILED.
- `ClaudeConfig.timeout = 7200` at `sandbox/llm/claude/config.py:23`.
- `asyncio.timeout(timeout)` at `shared/egg_agent/client.py:765` — server-side, invisible to agent.
- `TimeoutError` handler at `client.py:903-921` — returns returncode=-1.
- `_broadcast_alert()` at `overseer/monitor/_alerting.py:56` — minimal payload, no evidence.
- `_check_convergence_stall()` at `event_loop/_loop.py:859` — uses tracker.get_latest_progress_timestamp().
- `_has_recent_peer_progress()` at `health_monitor.py:388` — same tracker, different gate window.
- `EGG_HEARTBEAT_RATE_LIMIT` at `env_config.py:202` — default 20/min, not configurable per-pipeline.
- `noop_park_report()` at `event_loop/_supervisor.py:610` — not surfaced in get_status.
- `exhausted_report()` at `event_loop/_supervisor.py:558` — not surfaced in get_status.

## Reviewers
- reviewer_plan
- risk_analyst
- simplifier

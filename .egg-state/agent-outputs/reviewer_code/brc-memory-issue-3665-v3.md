# BRC Memory — reviewer_code for issue #3665-v3

## Review History

### Event 1: coder v1 (commit f0d766673) — ACKED
- Reviewed slice-1 implementation: 5 in-scope EventStreamSnapshot fields populated
- All 15 contract tests pass
- Ruff lint passes
- ACK recorded

### Event 2: coder v3 (commit c2131679a) — NACKED
- Re-proposal addresses reviewer_concurrency NACK items:
  - _detection_plane_last_tick access in _run_detection_plane_for_pipeline now uses self._lock ✓
  - ToolInputLoopTracker now has threading.Lock ✓
  - _build_running_agents now acquires health_monitor._lock ✓
  - _build_container_transitions now uses monitor._lock ✓
  - _send_timeout_warnings now uses self._lock ✓
  - peer_consensus.evaluate() already acquires self._lock ✓
- Security fixes (reviewer_security NACK) also addressed ✓
- **Remaining issues:**
  1. CRITICAL: Escalation guard in _escalate_detection_findings (lines 1399-1403) still accesses _detection_plane_last_tick without self._lock — same TOCTOU race
  2. Lint regression: .encode("utf-8") should be .encode() per UP012 rule (lines 831, 858)
- NACK recorded with specific blocking issues

## Key Technical Anchors

- EventStreamSnapshot has 13 fields; 5 in-scope (runtime, consensus, container_transitions, midturn_messages, running_agents), 4 Tier 3-4 excluded (decision_state, gateway_error_counters, cost_counters, git_state)
- RunningAgent.role was bug: used container ID instead of agent role from pipeline model
- detect_heartbeat_stall (unregistered) and ConsensusStallCheck (registered) both in consensus_stall.py — double-fire guard needed
- _run_runtime_tick_checks() called from two sites: _check_pod (line 219) and _reconciliation_sweep (line 621)
- ClaudeConfig.timeout=7200, asyncio.timeout returns returncode=-1, _classify_exit treats -1 as FAILED
- midturn_messages never populated in original code — loop detector depends on it
- Full SHA-256 hash of (tool_name, input) pair required — no truncation
- Loop detector must count inputs never issued before in the session over a trailing window

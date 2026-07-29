# BRC Memory — reviewer_code for issue #3665

## Current Status

**Slice 1 (coder)** — ACKED (v1, commit 4919cb322)

### Assessment

The coder's v4 re-proposal (commit 4919cb322) addresses both issues from the v3 NACK:

1. **CRITICAL — Escalation guard TOCTOU race**: The `_escalate_detection_findings` method in `kubernetes_monitor.py` (lines 1397-1403) now wraps the `_detection_plane_last_tick` read-check-write sequence in `with self._lock`, fixing the same TOCTOU race that was already fixed in `_run_detection_plane_for_pipeline`. The guard is now atomic — two threads cannot both pass the 5-second check and double-escalate findings.

2. **Lint regression (UP012)**: Both `.encode("utf-8")` calls in `_parse_tool_calls_from_logs` (detection_plane.py lines 831 and 858) have been reverted to `.encode()`. Ruff passes clean.

All prior concurrency and security fixes from v3 are preserved:
- `_detection_plane_last_tick` access in `_run_detection_plane_for_pipeline` uses `with self._lock` ✓
- `ToolInputLoopTracker` has `threading.Lock` protecting `observe()` and `reset()` ✓
- `_build_running_agents` moves AgentState attribute reads inside the health_monitor lock ✓
- `_build_container_transitions` uses `with getattr(monitor, "_lock", _NullLock())` ✓
- `_send_timeout_warnings` uses `with self._lock` for `_timeout_warning_last_sent` ✓
- Runtime section uses correct field names (`thread_last_tick_age_s`, `run_pipeline_thread_alive`) ✓
- Container transitions format matches `container_k8s.py` detector expectations ✓
- Consensus section augmented with `nack_cycles`, `late_confirmed_then_renack`, etc. ✓
- Regex fallback parser added for non-JSON log formats ✓
- Slice-1 tests re-added ✓

All 70 tests pass across 4 test files. Ruff lint is clean on both changed files.

### Files reviewed
- orchestrator/health_checks/detection_plane.py
- orchestrator/kubernetes_monitor.py
- orchestrator/health_checks/tier1/loop_detection.py
- orchestrator/tests/test_detection_plane_wiring.py
- orchestrator/tests/test_loop_detection.py
- orchestrator/tests/test_timeout_classification.py
- orchestrator/tests/test_alert_evidence.py
- scripts/file-size-allowlist.yaml

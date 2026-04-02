### Task Analysis

**Problem statement**: The overseer's health check methods call `_broadcast_alert()` which sends `OVERSEER_ALERT` messages to the pipeline message bus. These alerts leak into real pipelines' `recent_messages` with wrong `from_role`, causing spurious critical alerts.

**System context**: The `OverseerMonitor` class (`orchestrator/overseer/monitor.py`) runs a poll cycle executing health checks. When anomalies are detected, `_broadcast_alert()` (line 1269) sends messages via `egg-orch message send --to all --type OVERSEER_ALERT`. The CLI command `cmd_message_send()` (`sandbox/egg_lib/orch_cli.py:978`) resolves pipeline_id via `require_pipeline_id()` which falls back to `EGG_PIPELINE_ID` env var when no positional arg is provided.

**Technical root cause**:
1. `_broadcast_alert()` (line 1286) calls `_run_cli("egg-orch", "message", "send", "--to", "all", ...)` without a pipeline_id positional arg → CLI uses `EGG_PIPELINE_ID` from environment
2. `_broadcast_alert()` doesn't pass `--role` → CLI uses `EGG_AGENT_ROLE` from environment, which may be "coder" instead of "overseer"
3. `_send_message()` (line 1305) has the same issues plus is missing the required `--type` flag
4. `_resolve_alert()` (line 1322) and `_create_hitl_decision()` (line 1337) also don't pass pipeline_id
5. Unit tests in `TestPostConsensusStall`, `TestStatusConsistency`, `TestHitlResolutionPropagation`, `TestPrPhaseOutcomeCheck`, `TestOrchestratorReachability`, `TestCrossPhaseConsistency`, and `TestRerunAnomaly` don't mock `_broadcast_alert`

**Files affected**:
- `orchestrator/overseer/monitor.py` — Fix `_broadcast_alert()`, `_send_message()`, `_resolve_alert()`, `_create_hitl_decision()` to pass `self.pipeline_id` and `--role overseer`
- `orchestrator/tests/test_overseer_monitor.py` — Mock `_broadcast_alert` in test classes that don't already mock it

**Risks / edge cases**: `_send_message()` is missing `--type` (required by CLI parser), so it silently fails today. Adding `--type STATUS` means messages will start being delivered — correct behavior but worth noting.
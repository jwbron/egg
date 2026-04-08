### Task Analysis

**Problem statement**: The overseer's Tier 1 health monitor fires false-positive `heartbeat_timeout` and `progress_stall` alerts during the implement phase. Coders doing legitimate multi-file implementation work (~15-30 min) get flagged as stalled after just 2 minutes. Agents correctly idle in BRC protocol (reviewers/tester/documenter waiting for the coder's proposal) also get falsely flagged.

**Source context**: Reproduced on pipelines `issue-1523-v2` and `issue-1527`. In `issue-1527`, the overseer generated 10 alerts against 4 idle agents that were correctly polling for the coder's `CONSENSUS_PROPOSE`. The overseer's own LLM analysis even confirmed they were healthy.

**System context**: The `HealthMonitor` class (`orchestrator/health_monitor.py`) implements six deterministic tripwire rules. Two are relevant: `check_heartbeats()` (line 303) and `check_progress()` (line 377). Both use the same threshold: `PipelineConfig.orchestrator_heartbeat_timeout_seconds` (default 120s, defined at `orchestrator/models.py:299`). The health monitor has **no concept of the current pipeline phase** — the same 120s threshold applies to refine, plan, and implement equally. It also has **no awareness of BRC protocol state** — it doesn't check whether an agent is legitimately idle because it's waiting for an upstream proposal. The monitor is initialized once at pipeline start (`routes/pipelines.py:6586`) and runs in a background thread calling `check_tripwires()` every 30s.

**Technical root cause**: Two gaps in `HealthMonitor`:
1. **No phase-aware thresholds** — 120s is reasonable for refine/plan phases where agents are doing lightweight work, but the implement phase involves code reading, multi-file changes, and test execution that routinely takes 15-30+ minutes. The single global threshold can't distinguish these.
2. **No BRC-idle suppression** — In concurrent BRC execution, non-producer agents (reviewers, tester, documenter) sit idle until the producer (coder) sends `CONSENSUS_PROPOSE`. The health monitor sees them as "no heartbeat/progress" and fires alerts. The consensus tracker (`peer_consensus.py`) already tracks each agent's `producer_phase` — agents whose upstream producer is still in `WORKING` are legitimately idle.

**Files affected**:
- `orchestrator/models.py` — Add `orchestrator_implement_heartbeat_timeout_seconds` config field (higher default, e.g. 600s)
- `orchestrator/health_monitor.py` — Add `set_current_phase()` method; use phase-aware threshold in `check_heartbeats()` and `check_progress()`; add BRC-idle suppression logic that skips alerts for agents waiting on an upstream proposal
- `orchestrator/routes/pipelines.py` — Call `set_current_phase()` when phases transition
- `orchestrator/tests/test_health_monitor.py` — Tests for phase-aware thresholds and BRC-idle suppression

**Risks / edge cases**:
- Agents that are genuinely stuck during implement phase will take longer to detect (10 min vs 2 min) — acceptable tradeoff per the issue; the Tier 2 overseer LLM classifier provides a second layer of detection
- BRC suppression must only apply to agents whose upstream producer is in `WORKING` — once the producer has `PROPOSED`, reviewers should resume normal monitoring
- The `set_current_phase()` call must happen before agents are spawned for each phase, otherwise the old threshold briefly applies
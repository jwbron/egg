### Task Analysis

**Problem statement**: The overseer's stall detection treats "container is alive" (heartbeats, tool calls) as synonymous with "agent is making protocol progress" (BRC state transitions). This causes false positives (killing active reviewers pre-BRC) and false negatives (ignoring producers stuck in heartbeat loops post-ACK).

**Source context**: Issue #1613 documents two failure modes from a real pipeline run: (1) reviewer_refine was killed after ~1 min despite actively grepping the codebase to verify claims, because it had zero BRC messages; (2) a producer heartbeated every 4 seconds for 11 minutes after being ACKed without ever sending CONSENSUS_CONFIRMED, and was never flagged.

**System context**: Stall detection spans three tiers:
- **Tier 1** (`health_monitor.py:HealthMonitor`): Deterministic tripwires. `check_heartbeats()` and `check_progress()` use time-since-last-event thresholds. `_on_progress()` (line 213) resets `last_heartbeat` on heartbeat events, but only resets `last_progress` on non-heartbeat progress events. `_is_brc_idle()` (line 173) suppresses alerts for reviewer-only agents whose upstream producers haven't proposed yet — but **stops suppressing once the producer has PROPOSED**, at which point the reviewer is subject to normal thresholds despite doing legitimate pre-review work.
- **Tier 1 health checks** (`tier1/incomplete_consensus_stall.py`): `IncompleteConsensusStallCheck` tracks the set of blocking agents across ticks, but uses a 5-min grace + 10-tick threshold (~10 min total), far too slow for the post-ACK case.
- **Tier 2** (`overseer/monitor.py:OverseerMonitor`): LLM-based classification. `_check_incomplete_consensus_stall()` has a nudge threshold of 10 poll cycles (~5 min at 30s interval). The LLM classifier `classify_stall()` in `classifier.py` receives consensus context but doesn't distinguish "no BRC messages because working pre-BRC" from "no BRC messages because stuck".

**Technical root cause**:
1. **False positive**: When a producer sends CONSENSUS_PROPOSE, `_is_brc_idle()` returns `False` for the reviewer (because `are_all_producers_working()` is no longer true). The reviewer then faces the standard heartbeat/progress threshold (120s for refine phase). If the reviewer is only heartbeating (tool calls don't emit non-heartbeat progress events), `check_progress()` fires and escalates. The LLM classifier sees no BRC messages and classifies "stuck" → decision maker says "restart_agent" → reviewer killed.
2. **False negative**: A producer heartbeating every 4 seconds keeps `last_heartbeat` constantly fresh (`_on_progress` line 229-230). Heartbeat events also reset `heartbeat_escalated` (line 241), preventing escalation. Since the producer is alive, Tier 1 never fires. The `IncompleteConsensusStallCheck` eventually catches it but only after ~10 minutes due to its grace period and tick threshold.

**Files affected**:
- `orchestrator/peer_consensus.py` — Add two public methods for health monitor to read BRC state
- `orchestrator/models.py` — Add config fields for grace period and confirmation timeout
- `orchestrator/health_monitor.py` — Extend `_is_brc_idle()` with post-propose grace, add `check_brc_progress()` for post-ACK timeout
- `orchestrator/tests/test_health_monitor.py` — Tests for both new behaviors

**Risks / edge cases**:
- The pre-BRC grace period must not be so long that genuinely stuck reviewers go undetected. 5 min is safe.
- The post-ACK confirmation timeout (3 min) must not false-positive during legitimate commit/push operations between ACK receipt and CONFIRMED send. These operations typically complete in seconds.
- Must not break existing BRC-idle suppression for reviewers whose producers haven't proposed yet.
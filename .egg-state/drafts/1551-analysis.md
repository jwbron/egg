### Task Analysis

**Problem statement**: Agents can hang indefinitely with no log output, becoming unrecoverable without cancelling the entire pipeline. The platform needs force-retry at two levels: **individual agents** and **entire phases** — without killing the whole pipeline.

**Source context**: Issue #1551 documents two occurrences — a coder agent hanging after an Edit tool error (pipeline-4b096ac1, 2026-04-02) and an architect agent hanging during plan phase (issue-1570, 2026-04-08). The issue proposes two tiers of restart: Level 1 (single agent) and Level 2 (entire phase).

**Workarounds**: Currently the only option is cancelling the entire pipeline via `cancel_task`, which discards all agent work.

**System context**: Agent containers are spawned by `ContainerSpawner.spawn_agent_container()` (`container_spawner.py:195`), which creates per-agent worktrees, registers gateway sessions, and builds Docker containers. For BRC concurrent mode, agents are spawned through `ConcurrentExecutor._spawn_agent()` (`concurrent_executor.py:230`), which wraps the command in `build_consensus_wrapped_command()` from `consensus_wrapper.py`. The consensus wrapper already handles restart-on-exit for clean exits and transient crashes (up to 2 restarts), but it cannot handle a completely hung agent that never exits. The overseer (`overseer/monitor.py`) runs a poll-classify-decide-act loop with actions: `nudge`, `redirect`, `hitl`, `issue`, `slack` — no restart capability. Consensus state is tracked by `PeerConsensusTracker` (`peer_consensus.py:57`) with a `remove_agent()` method at line 796 and `ConsensusEvaluator` (`consensus.py:136`) with its own `remove_agent()`. The spawner already handles existing containers with the same name by removing them before spawning (lines 245-264). Phase execution state is tracked in the `Pipeline` model with per-phase `PhaseExecution` objects containing containers and agent lists.

**Technical root cause**: When an agent hangs (no output, no exit), neither the consensus wrapper (only triggers on exit) nor the overseer (strongest action is HITL escalation) can recover. The missing capabilities are: (1) stop+respawn a specific agent, (2) stop+respawn an entire phase, (3) consensus state cleanup on restart, and (4) overseer action types for both levels.

**Files affected**:
- `orchestrator/routes/pipelines.py` — Two new endpoints: `POST /<pipeline_id>/agents/<role>/restart` and `POST /<pipeline_id>/phases/<phase>/restart`
- `orchestrator/container_spawner.py` — New `restart_agent_container()` method for single-agent respawn
- `orchestrator/overseer/monitor.py` — Add `restart_agent` and `restart_phase` action handling in `_execute_action()` (line 486)
- `orchestrator/overseer/decision_maker.py` — Add `restart_agent` and `restart_phase` to available actions in the LLM prompt (line 68-81)
- `orchestrator/mcp_tools.py` — Two new tools: `restart_agent` and `restart_phase`, with corresponding handlers
- `orchestrator/tests/` — Tests for all new components

**Risks / edge cases**:
- **Worktree preservation**: Restarts must NOT delete per-agent worktrees — committed work lives there. The spawner's existing cleanup (line 245) removes old containers but not worktrees, which is correct
- **Phase restart vs prior artifacts**: A phase restart must preserve prior phase outputs (e.g., refine output carries into plan restart). Only the current phase's consensus, review cycles, and containers get reset
- **Consensus state**: Agent restart calls `PeerConsensusTracker.remove_agent()` to withdraw stale state. Phase restart calls `PeerConsensusTracker.clear()` to reset all consensus
- **Restart limits**: Agent-level capped at 2 per agent per phase. Phase-level requires HITL approval by default
- **Race condition**: Simultaneous agent+phase restarts — the spawner's name-conflict handling (line 245-264) guards against duplicate containers
- **Review cycle counter**: Phase restart must reset the cycle counter so the restarted phase gets a fresh set of review cycles
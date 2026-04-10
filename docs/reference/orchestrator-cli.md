# Orchestrator CLI

Use `egg-orch` to interact with the orchestrator and gateway APIs.

Run `egg-orch --help` for full usage. All commands support `--json` for machine-readable output.

## Quick Reference

| Command | Purpose |
|---------|---------|
| `egg-orch health` | Check orchestrator + gateway health |
| `egg-orch env` | Show orchestrator environment variables |
| `egg-orch pipeline list` | List all pipelines |
| `egg-orch pipeline get <id>` | Get pipeline details |
| `egg-orch pipeline status <id>` | Get pipeline status |
| `egg-orch pipeline create --repo <owner/name>` | Create a pipeline |
| `egg-orch pipeline delete <id>` | Delete a pipeline |
| `egg-orch signal complete [<id>] --role <role>` | Signal agent completion |
| `egg-orch signal progress [<id>] --role <role> --percent <n>` | Signal progress |
| `egg-orch signal error [<id>] --role <role> --error <msg>` | Signal error |
| `egg-orch signal heartbeat [<id>] --role <role>` | Send heartbeat |
| `egg-orch phase get [<id>]` | Get current pipeline phase |
| `egg-orch phase advance [<id>]` | Advance to next phase |
| `egg-orch phase start [<id>]` | Start current phase |
| `egg-orch phase complete [<id>]` | Complete current phase |
| `egg-orch decision list [<id>]` | List HITL decisions |
| `egg-orch decision create [<id>] --question <text>` | Queue a decision |
| `egg-orch decision resolve [<id>] <did> --resolution <text>` | Resolve a decision |
| `egg-orch decision status [<id>]` | Decision queue summary |
| `egg-orch container list [<id>]` | List containers |
| `egg-orch container spawn [<id>] --role <role>` | Spawn a container |
| `egg-orch container logs [<id>] <cid>` | Get container logs |
| `egg-orch container stop [<id>] <cid>` | Stop a container |
| `egg-orch agent restart [<id>] <role> [--reason "..."]` | Restart a single stuck agent (stop, reset consensus, respawn). *CLI pending — use the REST API directly for now* |
| `egg-orch phase restart [<id>] <phase> [--reason "..."] [--context "..."]` | Restart an entire phase (stop all containers, reset consensus, respawn all). *CLI pending — use the REST API directly for now* |
| `egg-orch gateway health` | Check gateway health |
| `egg-orch gateway phase --issue <n>` | Get current phase from gateway |
| `egg-orch gateway permissions <phase>` | Get allowed ops for a phase |
| `egg-orch message send [<id>] --to <role\|all> --type <type> --subject "..." --body "..."` | Send inter-agent message (concurrent mode) |
| `egg-orch message poll [<id>] [--since <id>] [--limit <n>]` | Poll for messages from other agents (concurrent mode) |
| `egg-orch message status [<id>]` | Get message bus status (concurrent mode) |
| `egg-orch signal readiness [<id>] --state <WORKING\|READY\|BLOCKED\|OBJECTING> [--reason "..."]` | Signal readiness state (concurrent mode) |
| `egg-orch push [--scope-filter]` | Push current branch; with `--scope-filter`, strips out-of-scope files before pushing |
| `egg-orch progress emit --step <text> --state <working\|blocked\|complete> [--detail <text>] [--blocker <text>]` | Emit structured progress event |
| `egg-orch progress query [--agent <role>] [--since <timestamp>] [--limit <n>]` | Query structured progress events |
| `egg-orch health alerts [--pipeline <id>]` | List active deterministic health alerts |
| `egg-orch health resolve [<id>] --agent-id <id> --alert-type <type>` | Resolve (remove) health alerts for an agent |
| `egg-orch anchor init --task <text>` | Create initial anchor for current agent |
| `egg-orch anchor update [--status <s>] [--progress <json>] [--decision <json>] [--key-context <text>] [--error <text>] [--file <path>]` | Update agent anchor (atomic, all-or-nothing) |
| `egg-orch anchor show [--agent <id>] [--team]` | Show own anchor, another agent's, or team anchor |
| `egg-orch anchor validate` | Validate anchor schema and size limits |
| `egg-orch anchor cleanup` | Remove orphaned anchor files |

Pipeline ID can be omitted when `EGG_PIPELINE_ID` is set (auto-set in orchestrated mode).
Agent role can be omitted when `EGG_AGENT_ROLE` is set.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `EGG_ORCHESTRATOR_URL` | Orchestrator URL (default: `http://egg-orchestrator:9849`) |
| `EGG_PIPELINE_ID` | Current pipeline ID (auto-set in orchestrated mode) |
| `EGG_AGENT_ROLE` | Current agent role (auto-set in orchestrated mode) |
| `EGG_ISSUE_NUMBER` | Current issue number |
| `EGG_BRANCH` | Target branch for the agent's worktree (auto-set; defaults to `egg/{pipeline_id}/work`) |
| `EGG_REPO_PATH` | Repository path (auto-set; points to specific repo when one exists, otherwise `~/repos/`) |
| `GATEWAY_URL` | Gateway URL (default: `http://egg-gateway:9848`) |
| `EGG_CONCURRENT_MODE` | `true` when running in concurrent execution mode |
| `EGG_MESSAGE_POLL_INTERVAL` | Suggested message polling interval in seconds (default: 30) |
| `AGENT_ANCHOR_ID` | Agent anchor ID (`{role}-{short_container_id}`), auto-set by container spawner |

## Common Workflows

**Check system health:**
```bash
egg-orch health
```

**Monitor a pipeline:**
```bash
egg-orch pipeline status <pipeline-id>
egg-orch phase get <pipeline-id>
egg-orch decision list <pipeline-id>
```

**Signal from within agent execution:**
```bash
egg-orch signal progress --percent 50 --task "Running tests"
egg-orch signal complete --commit abc1234
egg-orch signal error --error "Test failure" --recoverable
```

**Emit structured progress (health monitoring):**
```bash
egg-orch progress emit --step "running tests" --state working --detail "pytest suite 3/5"
egg-orch progress emit --step "blocked on dependency" --state blocked --blocker "waiting for coder"
egg-orch progress query --agent coder
```

**Check health monitoring alerts:**
```bash
egg-orch health alerts
egg-orch health alerts --pipeline issue-123

# Resolve (remove) alerts after an issue is addressed
egg-orch health resolve --agent-id coder --alert-type heartbeat_timeout

# Or specify an explicit pipeline ID
egg-orch health resolve issue-123 --agent-id coder --alert-type heartbeat_timeout
```

**Restart a stuck agent or phase:**
```bash
# Restart a single agent (preserves worktree, resets consensus state)
egg-orch agent restart coder --reason "Agent hung after Edit tool error"

# Restart with explicit pipeline ID
egg-orch agent restart issue-1551 coder --reason "No heartbeat for 10 minutes"

# Restart an entire phase (stops all containers, resets consensus + review cycles)
egg-orch phase restart implement --reason "Multiple agents stalled"

# Restart phase with additional context injected into respawned agents
egg-orch phase restart implement \
  --reason "Consensus corrupted after restart cascade" \
  --context "Previous attempt stalled during BRC convergence — focus on completing reviews first"
```

Agent restart preserves the agent's existing worktree (including committed work on the branch) and resets only that agent's consensus state (proposals, ACKs, NACKs, confirmations). Phase restart resets all consensus state and review cycle counters for the phase, then respawns all agents from scratch while preserving prior phase artifacts and branch commits.

> **Note:** CLI commands for restart are pending implementation. In the meantime, use the REST API directly or the MCP tools:
> ```bash
> # Agent restart via REST API
> curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/agents/<role>/restart \
>   -H "Content-Type: application/json" -d '{"reason": "Agent hung"}'
>
> # Phase restart via REST API
> curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phases/<phase>/restart \
>   -H "Content-Type: application/json" -d '{"reason": "Phase stalled", "context": "Focus on X"}'
> ```

**Manage agent anchors (post-compaction recovery):**
```bash
# Initialize anchor for current task
egg-orch anchor init --task "Fix auth bypass in gateway/auth.py"

# Update anchor with progress, decisions, context
egg-orch anchor update --status in_progress \
  --progress '{"state":"current","description":"Fixing token validation"}' \
  --decision '{"with_agent":"tester-def67890","decided":"Use parametrized tests"}'

# View own anchor, another agent's, or team view
egg-orch anchor show
egg-orch anchor show --agent coder-abc12345
egg-orch anchor show --team

# Validate schema and size budget
egg-orch anchor validate
```

## Phase Management MCP Tools

Four MCP tools expose phase management operations for pipeline recovery and manual intervention, eliminating the need for raw `curl` calls during stuck pipeline scenarios (see [#1570](https://github.com/jwbron/egg/issues/1570) for motivation).

| MCP Tool | REST Endpoint | Description |
|----------|---------------|-------------|
| `advance_phase` | `POST /pipelines/{id}/phase` | Advance pipeline to a target phase. With `force=true`, stops running containers first to prevent SIGTERM cascading |
| `start_phase` | `POST /pipelines/{id}/phase/start` | Start the current phase (spawns agents). Use when a phase is in state but no containers are running |
| `complete_phase` | `POST /pipelines/{id}/phase/complete` | Mark a phase as complete. Use when automatic transition is stuck |
| `populate_contract` | `POST /pipelines/{id}/phase/populate-contract` | Populate contract from plan artifacts. Parses yaml-tasks from the plan draft into contract phases/tasks |

**Parameters:**

All tools require `task_id` (the pipeline ID). Additional parameters:

- **`advance_phase`**: `target_phase` (string, required) — the phase to advance to (e.g., `"plan"`, `"implement"`, `"pr"`). `force` (boolean, optional, default `false`) — skip validation and stop running containers before advancing. **Important:** When `force=true`, containers from the current phase are stopped before the transition to prevent their SIGTERM signals from being misinterpreted as failures in the new phase.
- **`start_phase`**: No additional parameters.
- **`complete_phase`**: `artifacts` (object, optional) — phase completion artifacts to store (e.g., commit SHAs, PR URLs).
- **`populate_contract`**: No additional parameters. Resolves the pipeline's worktree path, reads the plan document, extracts task structure, and writes tasks and acceptance criteria to the contract. Returns phase and task counts on success.

**Recovery workflow example (stuck pipeline):**
```bash
# 1. Check current state
egg-orch phase get <pipeline-id>

# 2. Force-advance past a stuck phase (stops running containers first)
# Via MCP tool: advance_phase(task_id="<id>", target_phase="implement", force=true)
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase \
  -H "Content-Type: application/json" \
  -d '{"target_phase": "implement", "force": true}'

# 3. Populate contract if it's empty after manual phase setup
# Via MCP tool: populate_contract(task_id="<id>")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase/populate-contract

# 4. Start the phase (spawn agents)
# Via MCP tool: start_phase(task_id="<id>")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase/start

# 5. If needed, manually complete a phase
# Via MCP tool: complete_phase(task_id="<id>")
# Via REST:
curl -X POST http://egg-orchestrator:9849/api/v1/pipelines/<id>/phase/complete
```

## BRC Consensus Protocol

The BRC (Broadcast-Review-Converge) protocol is used during concurrent execution for multi-agent consensus. All protocol actions are gated by formal **action guards** defined in `orchestrator/action_guards.py` — see [Concurrent Execution — Action Guards](../guides/concurrent-execution.md#action-guards) for the complete guard table.

**Consensus commands:**
```bash
# Producer: propose work for review (commit SHA defaults to HEAD if omitted)
egg-orch consensus propose --summary "Implemented feature X" --artifacts src/feature.py --commit-sha $(git rev-parse HEAD)

# Producer: push and propose atomically (suppresses auto re-propose for the push)
egg-orch consensus propose --push --summary "Implemented feature X" --artifacts src/feature.py --commit-sha $(git rev-parse HEAD)

# Reviewer: ACK a producer's proposal
egg-orch consensus ack coder --files-reviewed src/feature.py tests/test_feature.py

# Reviewer: NACK a producer's proposal
egg-orch consensus nack coder --reason "Missing error handling" --files-reviewed src/feature.py

# Producer: withdraw proposal (requires reason citing new information)
egg-orch consensus withdraw --reason "Addressing NACK: adding error handling"

# Agent: confirm after all reviews complete
# Exit 0 = confirmed. Exit 1 = error. Exit 2 = waiting for reviewer re-ACKs (retry after polling).
egg-orch consensus confirmed

# Check overall consensus status
egg-orch consensus status
```

**Signal types for consensus:**

| Signal type | Purpose |
|-------------|---------|
| `consensus_propose` | Producer proposes artifacts for review |
| `consensus_ack` | Reviewer ACKs a producer's proposal |
| `consensus_nack` | Reviewer NACKs a producer's proposal |
| `consensus_withdraw` | Producer withdraws proposal (cooldown + flip-flop limits apply) |
| `consensus_confirmed` | Agent confirms consensus (action guards enforced) |
| `consensus_producer_push` | Triggers auto re-proposal when a producer pushes new commits after proposing — invalidates stale ACKs and notifies reviewers |

The `consensus_producer_push` signal accepts `agent_role`, `commit_sha`, and optional `changed_files` parameters. When the producer is still in `WORKING` state, the signal is a no-op. See [Auto Re-Propose on Push/Commit](../guides/concurrent-execution.md#auto-re-propose-on-pushcommit).

## Related CLIs

- `egg-contract` — SDLC contract operations (tasks, decisions, feedback)
- `egg-pipeline-watch` — Live pipeline status polling
- `egg-checkpoint` — Browse agent checkpoints

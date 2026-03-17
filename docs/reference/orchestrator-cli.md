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
| `egg-orch gateway health` | Check gateway health |
| `egg-orch gateway phase --issue <n>` | Get current phase from gateway |
| `egg-orch gateway permissions <phase>` | Get allowed ops for a phase |
| `egg-orch message send [<id>] --to <role\|all> --type <type> --subject "..." --body "..."` | Send inter-agent message (concurrent mode) |
| `egg-orch message poll [<id>] [--since <id>] [--limit <n>]` | Poll for messages from other agents (concurrent mode) |
| `egg-orch message status [<id>]` | Get message bus status (concurrent mode) |
| `egg-orch signal readiness [<id>] --state <WORKING\|READY\|BLOCKED\|OBJECTING> [--reason "..."]` | Signal readiness state (concurrent mode) |
| `egg-orch progress emit --step <text> --state <working\|blocked\|complete> [--detail <text>] [--blocker <text>]` | Emit structured progress event |
| `egg-orch progress query [--agent <role>] [--since <timestamp>] [--limit <n>]` | Query structured progress events |
| `egg-orch health alerts [--pipeline <id>]` | List active deterministic health alerts |
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
```

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

## Related CLIs

- `egg-contract` — SDLC contract operations (tasks, decisions, feedback)
- `egg-pipeline-watch` — Live pipeline status polling
- `egg-checkpoint` — Browse agent checkpoints

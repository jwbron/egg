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

Pipeline ID can be omitted when `EGG_PIPELINE_ID` is set (auto-set in orchestrated mode).
Agent role can be omitted when `EGG_AGENT_ROLE` is set.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `EGG_ORCHESTRATOR_URL` | Orchestrator URL (default: `http://egg-orchestrator:9849`) |
| `EGG_PIPELINE_ID` | Current pipeline ID (auto-set in orchestrated mode) |
| `EGG_AGENT_ROLE` | Current agent role (auto-set in orchestrated mode) |
| `EGG_ISSUE_NUMBER` | Current issue number |
| `GATEWAY_URL` | Gateway URL (default: `http://egg-gateway:9848`) |

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

## Related CLIs

- `egg-contract` — SDLC contract operations (tasks, decisions, feedback)
- `egg-pipeline-watch` — Live pipeline status polling
- `egg-checkpoint` — Browse agent checkpoints

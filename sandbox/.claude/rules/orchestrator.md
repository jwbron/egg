# Orchestrator CLI

Run `egg-orch --help` for full usage. All commands support `--json`. Full reference: `$EGG_REPO_PATH/docs/reference/orchestrator-cli.md`

**Essential commands:**

| Command | Purpose |
|---------|---------|
| `egg-orch health` | Check orchestrator + gateway health |
| `egg-orch pipeline status <id>` | Get pipeline status |
| `egg-orch phase get [<id>]` | Get current phase |
| `egg-orch signal complete --commit <sha>` | Signal completion |
| `egg-orch signal error --error <msg> --recoverable` | Signal error |
| `egg-orch signal heartbeat` | Send heartbeat |
| `egg-orch decision list [<id>]` | List HITL decisions |
| `egg-orch progress emit --step <text> --state <working\|blocked\|complete>` | Emit structured progress event |
| `egg-orch progress query [--agent <role>]` | Query structured progress events |
| `egg-orch health alerts` | List active deterministic health alerts |

Pipeline ID/agent role can be omitted when `EGG_PIPELINE_ID`/`EGG_AGENT_ROLE` are set.

**Key env vars**: `EGG_ORCHESTRATOR_URL`, `EGG_PIPELINE_ID`, `EGG_AGENT_ROLE`, `EGG_ISSUE_NUMBER`, `EGG_BRANCH`, `EGG_REPO_PATH`, `GATEWAY_URL`

**Related CLIs**: `egg-contract`, `egg-pipeline-watch`, `egg-checkpoint`

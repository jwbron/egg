# bin/

CLI entry points. Most are symlinks to the actual implementations in `gateway/` and `sandbox/`.

## egg

Start and manage egg sandbox sessions.

| Command | Description |
|---------|-------------|
| `egg` | Start interactive sandbox session (public mode, auto-setup on first run) |
| `egg --public` | Explicit public mode (full internet access, default) |
| `egg --private` | Private mode (Anthropic API only, network lockdown) |
| `egg --setup` | Run interactive setup wizard |
| `egg --reset` | Reset configuration and start over |
| `egg --exec <cmd>` | Execute command in ephemeral container |
| `egg --compose` | Start gateway via Docker Compose (auto-rebuilds images when code changes) |
| `egg --compose --down` | Stop the Docker Compose stack (gateway + orchestrator) |

**Common flags:**

| Flag | Description |
|------|-------------|
| `--private` / `--public` | Network mode (private locks down to Anthropic API + private GitHub repos) |
| `--compose` | Use Docker Compose to manage the gateway stack |
| `--down` | Stop the Docker Compose stack (use with `--compose`) |
| `--build` | Rebuild compose images before starting (no-op — `--compose` auto-rebuilds by default) |
| `--multi-agent` / `--no-multi-agent` | Enable/disable multi-agent execution (wave-based parallel agents) |
| `--max-parallel <n>` | Maximum parallel agents per wave (default: 10) |
| `--exec <cmd>` | Execute command in new ephemeral container |
| `--timeout <min>` | Timeout for `--exec` commands (default: 30) |
| `--auth <method>` | Anthropic auth method for `--exec`: `oauth-token` (default) or `api-key` |
| `--rebuild` | Force rebuild Docker image |
| `--time` | Show startup timing breakdown for debugging |
| `-v, --verbose` | Show detailed output instead of progress bar |

## egg-sdlc

Launch the full SDLC pipeline with DAG visualization and HITL checkpoints.

| Command | Description |
|---------|-------------|
| `egg-sdlc -r <repo> -i <issue>` | Issue mode — GitHub-issue-driven pipeline |
| `egg-sdlc -r <repo> <issue>` | Short form (positional issue number) |
| `egg-sdlc --private -r <repo> -i <issue>` | Private mode (network lockdown) |
| `egg-sdlc` | Local mode — interactive prompt |
| `egg-sdlc -r <repo> -p "prompt"` | Local mode — non-interactive with prompt |

Also available as `/sdlc` inside an interactive egg session.

## egg-deploy

Deploy and manage the gateway stack via Docker Compose (production/advanced).

| Command | Description |
|---------|-------------|
| `bin/egg-deploy init` | Initialize configuration files |
| `bin/egg-deploy up` | Start the gateway stack |
| `bin/egg-deploy down` | Stop the gateway stack |
| `bin/egg-deploy status` | Show container status and health |
| `bin/egg-deploy logs` | Follow gateway logs |
| `bin/egg-deploy build` | Rebuild Docker images |

## egg-status

Monitor all active SDLC pipelines in real-time.

| Command | Description |
|---------|-------------|
| `bin/egg-status` | Stream real-time status for all active pipelines |
| `bin/egg-status --once` | Show current snapshot and exit |
| `bin/egg-status --all` | Include completed/failed pipelines |
| `bin/egg-status --verbose` | Show full DAG instead of compact status |
| `bin/egg-status --ascii` | Use ASCII-only characters (no Unicode) |
| `bin/egg-status --port <port>` | Specify orchestrator port (default: 9849) |

## egg-pipeline-watch

Watch a specific pipeline's progress with DAG visualization.

| Command | Description |
|---------|-------------|
| `bin/egg-pipeline-watch <pipeline-id>` | Stream DAG visualization for a specific pipeline |
| `bin/egg-pipeline-watch <pipeline-id> --compact` | Show compact single-line status instead of full DAG |
| `bin/egg-pipeline-watch <pipeline-id> --once` | Show current state and exit (no streaming) |
| `bin/egg-pipeline-watch <pipeline-id> --ascii` | Use ASCII-only characters (no Unicode) |

## egg-orch

Programmatic interaction with the orchestrator API (usable by both agents and humans).

| Command Group | Description |
|---------------|-------------|
| `egg-orch health` | Check orchestrator + gateway health |
| `egg-orch pipeline list/get/create/status/delete` | Pipeline management |
| `egg-orch signal complete/progress/error/heartbeat` | Send agent signals |
| `egg-orch phase get/advance/start/complete` | Phase transitions |
| `egg-orch decision list/create/resolve/status` | HITL decision queue |
| `egg-orch container list/spawn/get/stop/logs` | Container operations |
| `egg-orch gateway health/phase/permissions` | Gateway operations |
| `egg-orch env` | Show orchestrator environment variables |

All commands support `--json` for machine-readable output. Run `egg-orch <command> --help` for detailed usage.

## egg-contract

Track SDLC pipeline progress (used by agents and humans).

| Command | Description |
|---------|-------------|
| `egg-contract show` | View current contract state |
| `egg-contract add-commit --task <id> --commit <sha>` | Link a commit to a task |
| `egg-contract update-notes --task <id> --notes <text>` | Add implementation notes to a task |
| `egg-contract add-decision --question <text> --options <a> <b>` | Create a HITL decision (multiple choice) |
| `egg-contract add-feedback --question <text>` | Create a HITL feedback request (open-ended) |
| `egg-contract verify-criterion --criterion <id>` | Mark acceptance criterion as verified (reviewer role) |
| `egg-contract agent-status` | Show agent execution status for multi-agent orchestration |
| `egg-contract agent-start --role <role>` | Mark an agent as started (running) |
| `egg-contract agent-complete --role <role>` | Mark an agent as complete |
| `egg-contract agent-fail --role <role> --error <msg>` | Mark an agent as failed |
| `egg-contract agent-next` | Get the next wave of agents to dispatch |

## egg-onboarding-docs

Generate repository documentation to onboard agents.

| Command | Description |
|---------|-------------|
| `egg-onboarding-docs <name>` | Generate onboarding docs (`<name>` is a directory under `~/repos/`) |
| `egg-onboarding-docs --dry-run <name>` | Survey and report without creating files or a PR |
| `egg-onboarding-docs --scope <pattern> <name>` | Limit documentation to files matching the pattern |

## egg-checkpoint

Query agent session checkpoints across multi-agent pipelines.

| Command | Description |
|---------|-------------|
| `egg-checkpoint list [filters]` | List checkpoints with multi-dimensional filtering |
| `egg-checkpoint show <id-or-commit>` | Display full checkpoint details (transcript, tool calls, files touched) |
| `egg-checkpoint browse --issue <n>` | Filter checkpoints by issue number |
| `egg-checkpoint context [filters]` | Cross-agent context summary grouped by phase and agent type |
| `egg-checkpoint cost [filters]` | Show cost breakdown (token usage and USD) by phase and agent type |

**Filters**: `--issue`, `--pr`, `--pipeline`, `--session`, `--branch`, `--trigger`, `--status`, `--agent-type`, `--phase`, `--limit`, `--json`

See the [Checkpoint Access Guide](../docs/guides/checkpoint-access.md) for detailed usage examples.

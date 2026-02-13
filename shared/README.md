# Shared Libraries

Reusable Python libraries shared between the gateway sidecar and sandbox container.

## Packages

### [egg_config](egg_config/README.md)

Unified configuration framework for egg services.

- Centralized config loading from environment variables, `secrets.env`, and `config.yaml`
- Validation with clear error messages
- Health checks for API connectivity
- Secret masking in logs

```python
from egg_config import GitHubConfig, GatewayConfig, LLMConfig

github = GitHubConfig.from_env()
result = github.validate()
health = github.health_check(timeout=10.0)
```

See [egg_config README](egg_config/README.md) for full documentation.

### egg_logging

Structured JSON logging with context support.

- Structured JSON output compatible with GCP Cloud Logging
- Request context tracking across log entries
- Function signature logging for debugging
- Configurable formatters (JSON, text)

```python
from egg_logging import get_logger

logger = get_logger(__name__)
logger.info("Operation completed", extra={"operation": "push", "repo": "owner/repo"})
```

**Files:**
- `logger.py` - Logger implementation and configuration
- `context.py` - Logging context management (request IDs, session tracking)
- `signatures.py` - Function signature capture for debug logging
- `formatters.py` - Log formatters (JSON, text)
- `cli.py` - CLI logging utilities

### egg_git

Git utility functions.

- Default branch detection for repositories

```python
from egg_git import get_default_branch

branch = get_default_branch("/path/to/repo")  # Returns "main", "master", etc.
```

**Files:**
- `default_branch.py` - Default branch detection logic

### egg_container

Shared container-launch config builder that unifies container configuration for both CLI and orchestrator.

**Core functions:**
- `build_sandbox_config()` — Framework-agnostic config builder that produces `SandboxContainerConfig`
- `build_sandbox_docker_cmd()` — Converts config to `docker run` command args (used by CLI)
- `to_dockerpy_kwargs()` — Converts config to docker-py SDK kwargs (used by orchestrator)
- `git_shadow_mounts()` — Generates .git shadow mounts to prevent local git operations

**Data classes:**
- `SandboxContainerConfig` — Framework-agnostic container configuration
- `MountSpec` — Specification for a single container mount (bind, tmpfs, volume)
- `ContainerNetworkConfig` — Network parameters (network name, gateway hostname/IP, port, repo mode)

```python
from egg_container import (
    build_sandbox_config,
    build_sandbox_docker_cmd,
    to_dockerpy_kwargs,
    git_shadow_mounts,
    ContainerNetworkConfig,
)

# Build framework-agnostic config
net_config = ContainerNetworkConfig(
    network_name="egg-isolated",
    gateway_hostname="egg-gateway",
    gateway_ip="172.32.0.2",
    gateway_port=9848,
    repo_mode="public",
)
config = build_sandbox_config(
    container_name="egg-sandbox",
    network=net_config,
    repo_volumes={"egg": "/home/user/repos/egg"},
    container_id="abc123",
)

# CLI path: convert to docker run command
cmd = build_sandbox_docker_cmd(net_config, ...)

# Orchestrator path: convert to docker-py kwargs
kwargs = to_dockerpy_kwargs(config)
client.containers.run(**kwargs)

# Generate .git shadow mounts
mounts = git_shadow_mounts({"egg": "/home/user/repos/egg"})
```

### egg_orchestrator

Shared orchestrator types and utilities for sandbox-to-orchestrator communication.

- Typed orchestrator API client (OrchestratorClient)
- Orchestrator mode detection (is_orchestrator_mode)
- Deployment mode enum (LOCAL, REMOTE_SINGLE, DISTRIBUTED)
- Signal types for completion reporting (complete, progress, error, heartbeat)

```python
from egg_orchestrator import (
    OrchestratorClient,
    is_orchestrator_mode,
    DeploymentMode,
    SignalType,
)

# Check if running in orchestrator mode
if is_orchestrator_mode():
    client = OrchestratorClient()

    # Signal completion
    client.signal_complete(
        pipeline_id="issue-123",
        agent_role="coder",
        commit="abc1234",
    )

# Detect deployment mode
mode = DeploymentMode.from_env()
```

**Files:**
- `client.py` - OrchestratorClient for API communication
- `types.py` - Data types (CompletionData, ErrorData, SignalResponse)
- `detection.py` - Orchestrator mode detection utilities
- `constants.py` - Configuration constants

### egg_contracts

SDLC contract models, role-based validation, plan parsing, resilience utilities, and agent checkpoint capture.

- Pydantic models for contract schema validation
- Role-based mutation validation (implementer, reviewer, human, system)
- Plan parser for extracting tasks from markdown documents
- HITL (Human-in-the-Loop) checkbox generation and parsing
- Resilience utilities (rate limiting, retry with backoff, timeout checkpoints)
- Agent recovery (retry management, circuit breaker, conflict detection for multi-agent workflows)
- Checkpoint models and utilities for capturing agent session context

```python
from pathlib import Path
from egg_contracts import Contract, parse_plan
from egg_contracts import generate_full_hitl_block, parse_checkbox_state
from egg_contracts import retry_with_backoff, parse_rate_limit_headers
from egg_contracts import AgentRetryManager, AgentCircuitBreaker, ConflictDetector
from egg_contracts.checkpoints import Checkpoint, CheckpointIndex
from egg_contracts.checkpoint_loader import save_checkpoint, load_checkpoint

# Load and validate contract
contract = Contract.model_validate_json(contract_json)

# Generate HITL decision UI
block = generate_full_hitl_block(issue_number=123, stuck_task_id="task-1")

# Retry with exponential backoff (decorator usage)
@retry_with_backoff()
def call_external_api():
    ...

# Agent recovery for multi-agent workflows
retry_mgr = AgentRetryManager()
circuit_breaker = AgentCircuitBreaker()
conflict_detector = ConflictDetector(repo_path=Path("/repo"))

# Work with checkpoints
checkpoint = Checkpoint(...)
checkpoint_path = Path("/checkpoints/ab/ckpt-abcdef123456.json")
save_checkpoint(checkpoint, checkpoint_path)
loaded = load_checkpoint(checkpoint_path)
```

**Key modules:**
- `models.py` - Pydantic models (Contract, Task, Phase, Feedback, CheckDefinition, CheckResult, PhaseConfig, etc.)
- `hitl.py` - Human-in-the-loop checkbox UI generation and parsing
- `feedback.py` - Feedback comment generation and parsing for open-ended questions
- `resilience.py` - Rate limit handling, retry logic, timeout checkpoints
- `plan_parser.py` - Markdown plan parsing and task extraction
- `roles.py` - Role-based field ownership validation
- `audit.py` - Audit log utilities
- `agent_recovery.py` - Multi-agent recovery (retry manager, circuit breaker, conflict detector)
- `agent_roles.py` - Agent role definitions and file access patterns
- `checkpoints.py` - Checkpoint models (Checkpoint, SessionMetadata, Transcript, ToolCall, TokenUsage)
- `checkpoint_loader.py` - Checkpoint I/O (atomic save, load, indexing)
- `checkpoint_cli.py` - CLI for browsing and querying checkpoints
- `dependency_graph.py` - Task dependency graph for multi-agent orchestration
- `loader.py` - Contract loading from filesystem
- `orchestration.py` - Orchestration state management
- `orchestrator.py` - Multi-agent orchestrator logic
- `phase_defaults.py` - Default check definitions per SDLC phase
- `redactor.py` - Sensitive data redaction (env vars, tokens, credentials)
- `transcript_extractor.py` - Claude Code session transcript extraction from JSONL files
- `usage.py` - Usage tracking models
- `usage_cli.py` - Usage tracking CLI
- `usage_loader.py` - Usage data loading
- `validator.py` - Contract validation logic

## Installation

These libraries are installed as Python packages in both containers via `pyproject.toml`:

```toml
[project]
dependencies = [
    "egg-config",
    "egg-logging",
    "egg-git",
]
```

## Testing

```bash
# Run all shared library tests
.venv/bin/pytest tests/shared/ -v
.venv/bin/pytest tests/egg_config/ -v
```

## Related Documentation

- [egg_config README](egg_config/README.md) - Full config framework documentation
- [ADR: Standardized Logging](../docs/adr/implemented/ADR-Standardized-Logging-Interface.md) - Logging design decisions
- [ADR: Declarative Setup](../docs/adr/implemented/ADR-Declarative-Setup-Architecture.md) - Config architecture

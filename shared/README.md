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

Shared container-launch command builder used by production launchers and tests.

- `build_sandbox_docker_cmd()` — Pure function that constructs the `docker run` argument list
- `ContainerNetworkConfig` — Dataclass for network parameters (network name, gateway hostname/IP, port, repo mode)

```python
from egg_container import build_sandbox_docker_cmd, ContainerNetworkConfig

net_config = ContainerNetworkConfig(
    network_name="egg-isolated",
    gateway_hostname="egg-gateway",
    gateway_ip="172.32.0.2",
    gateway_port=9848,
    repo_mode="public",
)
cmd = build_sandbox_docker_cmd(net_config, ...)
```

### egg_contracts

SDLC contract models, role-based validation, plan parsing, and resilience utilities.

- Pydantic models for contract schema validation
- Role-based mutation validation (implementer, reviewer, human, system)
- Plan parser for extracting tasks from markdown documents
- HITL (Human-in-the-Loop) checkbox generation and parsing
- Resilience utilities (rate limiting, retry with backoff, timeout checkpoints)
- Agent recovery (retry management, circuit breaker, conflict detection for multi-agent workflows)

```python
from pathlib import Path
from egg_contracts import Contract, parse_plan
from egg_contracts import generate_full_hitl_block, parse_checkbox_state
from egg_contracts import retry_with_backoff, parse_rate_limit_headers
from egg_contracts import AgentRetryManager, AgentCircuitBreaker, ConflictDetector

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
- `checkpoints.py` - Checkpoint model definitions
- `checkpoint_cli.py` - Checkpoint CLI for saving/restoring agent state
- `checkpoint_loader.py` - Checkpoint loading utilities
- `dependency_graph.py` - Task dependency graph for multi-agent orchestration
- `loader.py` - Contract loading from filesystem
- `orchestration.py` - Orchestration state management
- `orchestrator.py` - Multi-agent orchestrator logic
- `phase_defaults.py` - Default check definitions per SDLC phase
- `redactor.py` - Sensitive content redaction
- `transcript_extractor.py` - Agent transcript extraction
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

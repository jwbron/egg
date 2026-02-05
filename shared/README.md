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

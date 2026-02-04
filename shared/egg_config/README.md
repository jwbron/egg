# jib_config

Unified configuration framework for egg services.

## Overview

`jib_config` provides:
- **Centralized config loading** from environment variables, `secrets.env`, and `config.yaml`
- **Validation** with clear error messages
- **Health checks** to verify API connectivity
- **Secret masking** in logs and debug output

## Quick Start

```python
from jib_config import SlackConfig, GitHubConfig

# Load and validate Slack config
slack = SlackConfig.from_env()
result = slack.validate()
if not result.is_valid:
    raise ValueError(f"Invalid config: {result.errors}")

# Test API connectivity
health = slack.health_check(timeout=10.0)
if not health.healthy:
    print(f"Slack unhealthy: {health.message}")
```

## Configuration Files

All configuration lives in `~/.config/egg/`:

```
~/.config/egg/
├── config.yaml      # Non-secret settings
├── secrets.env      # API tokens and keys (chmod 600)
└── repositories.yaml # Repository-specific settings
```

### config.yaml Structure

```yaml
# Service-specific settings (non-secrets)
slack:
  channel: "C12345678"
  allowed_users: ["U123", "U456"]
  owner_user_id: "U123"
  batch_window_seconds: 15

github:
  username: "egg"

# Other settings
bot_name: "egg"
```

### secrets.env Structure

```bash
# Slack tokens
SLACK_TOKEN="xoxb-your-bot-token"
SLACK_APP_TOKEN="xapp-your-app-token"

# GitHub tokens
GITHUB_TOKEN="ghp_your-primary-token"
GITHUB_READONLY_TOKEN="ghp_your-readonly-token"

# Atlassian (JIRA/Confluence)
JIRA_BASE_URL="https://your-domain.atlassian.net"
JIRA_USERNAME="your-email@example.com"
JIRA_API_TOKEN="your-atlassian-api-token"
CONFLUENCE_BASE_URL="https://your-domain.atlassian.net/wiki"
CONFLUENCE_USERNAME="your-email@example.com"
CONFLUENCE_API_TOKEN="your-atlassian-api-token"

# Gateway
GATEWAY_SECRET="your-gateway-secret"
```

## Available Configs

| Config | Purpose | Key Fields |
|--------|---------|------------|
| `SlackConfig` | Slack bot integration | `bot_token`, `app_token`, `channel` |
| `GitHubConfig` | GitHub API access | `token`, `readonly_token`, `incognito_token` |
| `JiraConfig` | JIRA issue sync | `base_url`, `username`, `api_token` |
| `ConfluenceConfig` | Confluence page sync | `base_url`, `username`, `api_token` |
| `GatewayConfig` | Gateway sidecar auth | `secret`, `port` |
| `LLMConfig` | LLM provider settings | `anthropic_api_key`, `model` |

## Config Priority

Each config loads from multiple sources in priority order:

1. **Environment variables** (highest priority)
2. **`~/.config/egg/secrets.env`** (for tokens/secrets)
3. **`~/.config/egg/config.yaml`** (for other settings)
4. **Default values** (lowest priority)

## Validation Script

Validate your configuration after setup or when troubleshooting:

```bash
# Validate config files load correctly
./scripts/validate-config.py

# Also test API connectivity (Slack, GitHub, JIRA, Confluence)
./scripts/validate-config.py --health
```

This script is automatically run after `setup.py` completes to verify secrets are valid.

## Health Checks

Each config has a `health_check()` method that tests actual API connectivity:

```python
from jib_config import SlackConfig, GitHubConfig, JiraConfig

slack = SlackConfig.from_env()
result = slack.health_check(timeout=10.0)
# Returns: HealthCheckResult(healthy=True, message="Authenticated as bot_name", latency_ms=150)
```

## Validation

Configs validate:
- **Required fields** are present
- **Token formats** match expected patterns (e.g., `xoxb-` for Slack bot tokens)
- **URLs** are valid HTTPS
- **Emails** are properly formatted

```python
config = SlackConfig.from_env()
result = config.validate()

if not result.is_valid:
    for error in result.errors:
        print(f"Error: {error}")

for warning in result.warnings:
    print(f"Warning: {warning}")
```

## Secret Masking

Use `to_dict()` to get config values with secrets masked (safe for logging):

```python
config = SlackConfig.from_env()
print(config.to_dict())
# {'bot_token': 'xoxb-****...****', 'channel': 'C12345', ...}
```

## Adding a New Config

1. Create `shared/jib_config/configs/myservice.py`:

```python
from dataclasses import dataclass
from ..base import BaseConfig, ValidationResult, HealthCheckResult

@dataclass
class MyServiceConfig(BaseConfig):
    api_key: str = ""
    endpoint: str = ""

    def validate(self) -> ValidationResult:
        errors = []
        if not self.api_key:
            errors.append("api_key is required")
        if errors:
            return ValidationResult.invalid(errors)
        return ValidationResult.valid()

    def health_check(self, timeout: float = 5.0) -> HealthCheckResult:
        # Test actual API connectivity
        ...

    def to_dict(self) -> dict:
        return {
            "api_key": mask_secret(self.api_key),
            "endpoint": self.endpoint,
        }

    @classmethod
    def from_env(cls) -> "MyServiceConfig":
        # Load from env vars and config files
        ...
```

2. Export from `shared/jib_config/__init__.py`:

```python
from .configs.myservice import MyServiceConfig
```

3. Add tests in `tests/jib_config/test_configs.py`

## Testing

```bash
# Run all jib_config tests
python -m pytest tests/jib_config/ -v
```

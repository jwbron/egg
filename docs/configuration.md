# Configuration

This document describes how to configure egg.

## Configuration Files

egg uses two configuration files:

| File | Purpose | Git Status |
|------|---------|------------|
| `egg.yaml` | Main configuration (policies, repositories, logging) | Can be committed |
| `secrets.yaml` | Sensitive credentials (tokens, keys) | **Never commit** |

## egg.yaml

The main configuration file defines policies and settings:

```yaml
egg:
  name: "my-sandbox"

  # Git policies
  git:
    branch_prefix: "egg/"  # Branches must start with this
    protected_branches:
      - "main"
      - "master"
    allow_force_push: false
    merge_blocking: true  # Gateway has no merge endpoint

  # Authentication sources
  auth:
    sources:
      - name: "bot-account"
        type: "github_app"
      - name: "personal"
        type: "pat"
    # Associate repos with auth sources
    repo_auth:
      "owner/repo1": "bot-account"
      "owner/*": "bot-account"

  # Repository allowlist
  repositories:
    allowed:
      - "owner/repo1"
      - "owner/repo2"
      - "owner/*"  # Wildcard support

  # Logging
  logging:
    level: "INFO"  # DEBUG, INFO, WARNING, ERROR
    format: "json"  # json or text
    output: "stdout"  # stdout or file path

  # Container settings
  container:
    mounts:
      - source: "./workspace"
        target: "/workspace"
        read_only: false
```

### Configuration Options

#### git

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `branch_prefix` | string | `"egg/"` | Required prefix for branches agents can push to |
| `protected_branches` | list | `["main", "master"]` | Branches that cannot be pushed to directly |
| `allow_force_push` | bool | `false` | Allow force push to owned branches |
| `merge_blocking` | bool | `true` | Block merge operations (humans must merge) |

#### auth

| Option | Type | Description |
|--------|------|-------------|
| `sources` | list | Authentication sources (name, type) |
| `repo_auth` | map | Repository to auth source mapping |

Supported auth types:
- `github_app` - GitHub App authentication
- `pat` - Personal Access Token

#### repositories

| Option | Type | Description |
|--------|------|-------------|
| `allowed` | list | Repositories the agent can access |

Supports wildcards: `owner/*` matches all repos from that owner.

#### logging

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `level` | string | `"INFO"` | Log level |
| `format` | string | `"json"` | Output format (json/text) |
| `output` | string | `"stdout"` | Output destination |

## secrets.yaml

Sensitive credentials are stored separately:

```yaml
secrets:
  # GitHub App authentication
  github_app:
    app_id: "123456"
    private_key_path: "/path/to/key.pem"

  # Personal Access Tokens
  pats:
    personal: "ghp_xxxxxxxxxxxx"

  # Anthropic credentials (choose one)
  anthropic:
    api_key: "sk-ant-xxxxxxxxxxxx"
    # OR
    oauth_token: "oauth-xxxxxxxxxxxx"
```

### Anthropic Authentication

Choose one authentication method:

| Method | Config Key | Use Case |
|--------|------------|----------|
| API Key | `api_key` | Anthropic API accounts, teams |
| OAuth Token | `oauth_token` | Claude Pro/Max subscriptions |

OAuth tokens are obtained via `claude auth login` or `claude setup-token`.

## CLI Flags

Network mode and other runtime options are set via CLI flags:

```bash
# Default: public mode
egg start --config egg.yaml

# Private mode: network locked down
egg start --config egg.yaml --private

# Headless mode: non-interactive
egg start --config egg.yaml --headless
```

| Flag | Description |
|------|-------------|
| `--config <path>` | Path to egg.yaml (default: `./egg.yaml`) |
| `--private` | Enable private network mode |
| `--headless` | Run without interactive terminal |

## Environment Variables

Some settings can be overridden via environment variables:

| Variable | Description |
|----------|-------------|
| `EGG_CONFIG` | Path to egg.yaml |
| `EGG_SECRETS` | Path to secrets.yaml |
| `EGG_LOG_LEVEL` | Override log level |

## File Locations

| Path | Purpose |
|------|---------|
| `./egg.yaml` | Default config file location |
| `~/.config/egg/egg.yaml` | User-level config |
| `~/.config/egg/secrets.yaml` | Credentials |
| `~/.egg/sessions.json` | Session storage |

## Validation

Validate your configuration:

```bash
egg config validate
```

This checks:
- YAML syntax
- Required fields present
- Auth sources referenced in repo_auth exist
- Secrets file readable (without exposing values)

# Setup Guide

This guide walks through setting up egg for the first time.

## Prerequisites

- Docker installed and running
- Git
- A GitHub account (for repository access)
- Anthropic API key or Claude Pro/Max subscription

## Quick Setup

```bash
# 1. Clone the repository
git clone https://github.com/jwbron/egg.git
cd egg

# 2. Run the development setup
./dev setup

# 3. Create configuration files
cp egg.yaml.example egg.yaml
cp secrets.yaml.example secrets.yaml

# 4. Edit secrets.yaml with your credentials
# (see Configuration section below)

# 5. Start the sandbox
egg start
```

## Configuration

### 1. GitHub Authentication

egg supports two authentication methods:

#### Option A: GitHub App (Recommended)

1. Create a GitHub App at https://github.com/settings/apps
2. Configure permissions:
   - Repository: Contents (Read & Write)
   - Repository: Pull requests (Read & Write)
   - Repository: Issues (Read & Write)
3. Generate a private key
4. Install the app on your repositories
5. Add to secrets.yaml:

```yaml
secrets:
  github_app:
    app_id: "YOUR_APP_ID"
    private_key_path: "/path/to/private-key.pem"
```

#### Option B: Personal Access Token

1. Create a PAT at https://github.com/settings/tokens
2. Select scopes: `repo`, `workflow` (if needed)
3. Add to secrets.yaml:

```yaml
secrets:
  pats:
    personal: "ghp_xxxxxxxxxxxx"
```

### 2. Anthropic Authentication

#### Option A: API Key

For Anthropic API accounts (teams, enterprise):

```yaml
secrets:
  anthropic:
    api_key: "sk-ant-xxxxxxxxxxxx"
```

#### Option B: OAuth Token

For Claude Pro/Max subscriptions:

1. Run `claude auth login` to authenticate
2. Add the token to secrets.yaml:

```yaml
secrets:
  anthropic:
    oauth_token: "oauth-xxxxxxxxxxxx"
```

### 3. Repository Configuration

Edit egg.yaml to specify allowed repositories:

```yaml
egg:
  repositories:
    allowed:
      - "your-org/repo1"
      - "your-org/repo2"
      - "your-org/*"  # Allow all repos from org
```

## Verify Setup

```bash
# Validate configuration
egg config validate

# Start the sandbox
egg start

# Check status
egg status
```

## Network Modes

### Public Mode (Default)

Full internet access. Use for:
- Open source development
- Public repositories
- Tasks requiring npm/pip package installation

```bash
egg start
```

### Private Mode

Network locked down to Anthropic API only. Use for:
- Private repositories
- Sensitive codebases
- Maximum security

```bash
egg start --private
```

## Troubleshooting

### Docker Issues

```bash
# Check Docker is running
docker info

# Check gateway health
curl http://localhost:9847/api/v1/health
```

### Authentication Issues

```bash
# Verify GitHub authentication
gh auth status

# Test Anthropic API
curl https://api.anthropic.com/v1/messages \
  -H "x-api-key: YOUR_KEY" \
  -H "anthropic-version: 2023-06-01"
```

### Permission Issues

Ensure the private key file has correct permissions:

```bash
chmod 600 /path/to/private-key.pem
```

## Next Steps

- Read the [Security Model](security.md) to understand how egg protects your code
- See [Configuration](configuration.md) for all options
- Check [API Reference](api.md) for gateway endpoints

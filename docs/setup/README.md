# Setup Guides

Installation and configuration documentation.

## Quick Start

```bash
# Clone and set up
git clone https://github.com/YOUR_USERNAME/egg.git
cd egg
./dev setup

# Configure credentials
cp secrets.yaml.example ~/.config/egg/secrets.yaml
# Edit with your GitHub App / PAT and Anthropic credentials

# Start the sandbox (public mode)
egg start --config egg.yaml

# Start with network lockdown (private mode)
egg start --config egg.yaml --private
```

## Setup Requirements

Before running setup, you'll need:

1. **GitHub App** (required for PR creation)
   - See [GitHub App Setup](github-app-setup.md) for instructions
   - Alternative: Personal Access Token (PAT)
   - See [GitHub Auth Comparison](github-auth-comparison.md) for trade-offs

2. **Anthropic API Key** (required for Claude)

3. **Prerequisites**
   - Docker installed and running
   - Python 3 installed

## Available Guides

### [GitHub App Setup](github-app-setup.md)
GitHub App configuration for automated PR creation:
- Required permissions (read-only and read-write)
- Installation steps
- Token configuration
- Troubleshooting common permission errors

### [GitHub Auth Comparison](github-auth-comparison.md)
Comparison of authentication methods (GitHub App vs PAT).

## Configuration Location

All configuration is stored in `~/.config/egg/`:

| File | Purpose |
|------|---------|
| `secrets.yaml` | Anthropic API key, GitHub credentials |
| `github-app-id` | GitHub App ID |
| `github-app-installation-id` | GitHub App Installation ID |
| `github-app.pem` | GitHub App private key |

## See Also

- [Main README](../../README.md) - Project overview and quick start
- [Architecture](../architecture/) - System design
- [Reference](../reference/) - Quick reference guides

# Setup Guides

Installation and configuration documentation.

## Quick Start

Run the main setup script to configure everything:

```bash
./setup.py
```

This single command handles:
- Host service installation (Slack, GitHub watcher, context sync, etc.)
- Configuration validation (Slack tokens, GitHub auth)
- Docker image building
- All component setup

**Note:** You can also run `egg --setup` which delegates to `./setup.py`. If egg detects incomplete setup when you run it, it will prompt to run setup automatically.

## Setup Requirements

Before running setup, you'll need:

1. **Slack App** (required for notifications)
   - See [Slack App Setup](slack-app-setup.md) for instructions
   - You'll need: Bot Token (xoxb-...) and App Token (xapp-...)

2. **GitHub App** (required for PR creation)
   - See [GitHub App Setup](github-app-setup.md) for instructions
   - Alternative: Personal Access Token (PAT)

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

### [Slack App Setup](slack-app-setup.md)
Detailed Slack app configuration including:
- Creating the Slack app
- Configuring OAuth scopes
- Setting up Socket Mode
- Getting required tokens

## Setup Order

1. **Create Slack App** - [Slack app setup](slack-app-setup.md) (get tokens first)
2. **Run Setup** - `./setup.py` (interactive, validates and installs everything)
3. **Verify** - Check services with `systemctl --user status slack-notifier slack-receiver`

## Configuration Location

All configuration is stored in `~/.config/egg/`:

| File | Purpose |
|------|---------|
| `secrets.env` | Slack tokens, GitHub PAT, API keys |
| `github-app-id` | GitHub App ID |
| `github-app-installation-id` | GitHub App Installation ID |
| `github-app.pem` | GitHub App private key |
| `repositories.yaml` | Repository access configuration (created by setup.py) |

Docker build cache and staging files are stored in `~/.cache/egg/` (XDG-compliant):

| File | Purpose |
|------|---------|
| `Dockerfile` | Generated Dockerfile for the egg image |
| `docker-setup.py` | Container setup script |
| `shared/` | Shared modules copied for container build |

Note: Volume mounts are configured dynamically from `repositories.yaml`.

## Service Overview

| Service | Purpose | Setup |
|---------|---------|-------|
| slack-notifier | Claude → You notifications | Included in main setup |
| slack-receiver | You → Claude messages | Included in main setup |
| context-sync | Confluence/JIRA sync | Included in main setup |
| gateway | Git/GitHub operations + token refresh | Included in main setup |

## Updating Configuration

To update your configuration (secrets, repositories, etc.):

```bash
./setup.py --update
```

This will prompt you for each setting with your current values as defaults:
- Press Enter to keep the existing value
- Enter a new value to update it

Settings you can update:
- GitHub username
- Bot name
- Slack tokens (bot and app tokens)
- GitHub authentication token
- Repository configuration (writable and read-only repos)
- Slack channel and user ID

After updating, restart services to apply changes:
```bash
systemctl --user restart slack-notifier slack-receiver
```

## Troubleshooting

### Setup fails with missing Slack tokens
1. Create a Slack app at https://api.slack.com/apps
2. Follow [Slack App Setup](slack-app-setup.md)
3. Add tokens to `~/.config/egg/secrets.env`
4. Run `./setup.py` again

### Services not starting
```bash
# Check service status
systemctl --user status slack-notifier slack-receiver

# View logs
journalctl --user -u slack-notifier.service -f
```

### Docker image build fails
```bash
# Try building manually
bin/egg --setup

# Or reset and rebuild
bin/egg --reset
./setup.py
```

## See Also

- [Main README](../../README.md) - Project overview and quick start
- [Architecture](../architecture/) - System design
- [Reference](../reference/) - Quick reference guides

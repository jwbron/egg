# Configuration

Configuration for egg (jib). There are two types of config:

1. **In-repo configs** (this directory) - Version-controlled templates and non-secret settings
2. **Host configs** - User-specific settings, secrets, cache, and runtime data

## Directory Structure Overview

jib uses several directories under `~/`, each with a specific purpose:

| Directory | Purpose | XDG Compliance |
|-----------|---------|----------------|
| `~/.config/egg/` | User configuration and secrets | `$XDG_CONFIG_HOME` |
| `~/.cache/jib/` | Docker build staging and cache | `$XDG_CACHE_HOME` |
| `~/.jib-sharing/` | Runtime data shared with containers | Kept at `~/` for visibility |
| `~/.jib-worktrees/` | Git worktrees for isolated development | Kept at `~/` for visibility |

### Why `~/.jib-sharing/` and `~/.jib-worktrees/` are at `~/`

While XDG spec suggests `~/.local/share/` for runtime data, we keep these at `~/` for:
- **Visibility**: Users frequently inspect these for debugging
- **Docker simplicity**: Shorter paths are easier to mount
- **Discoverability**: New users can see jib directories with `ls ~`

## Host Configuration (`~/.config/egg/`)

All persistent user configuration is consolidated under `~/.config/egg/`:

```
~/.config/egg/
├── config.yaml        # Non-secret settings (Slack channel, sync intervals, etc.)
├── secrets.env        # All secrets (Slack, GitHub, Confluence, JIRA tokens)
├── github-token       # GitHub token (dedicated file)
├── github-app-id      # GitHub App ID (if using App auth)
├── github-app-installation-id  # GitHub App Installation ID
├── github-app.pem     # GitHub App private key
└── repositories.yaml  # Repository access configuration (created by setup.py)
```

## Cache Directory (`~/.cache/jib/`)

Docker build staging and cache files (auto-managed, safe to delete):

```
~/.cache/jib/
├── Dockerfile         # Generated Dockerfile for jib image
├── docker-setup.py    # Container setup script
├── entrypoint.py      # Container entrypoint
├── shared/            # Shared modules for container build
├── claude-commands/   # Claude command definitions
├── claude-rules/      # Claude rules/instructions
└── .claude/hooks/     # Claude hooks configuration
```

This directory respects `$XDG_CACHE_HOME` if set.

**Note**: Previously this was `~/.jib/`. The jib script auto-migrates on first run.

**Migration from legacy locations:**
Run manual migration if you have existing configs:
```bash
python3 config/host_config.py --migrate
```

This migrates from:
- `~/.config/egg-notifier/config.json` → `~/.config/egg/`
- `~/.config/context-sync/.env` → `~/.config/egg/secrets.env`

**Usage:**
```python
from config.host_config import HostConfig

config = HostConfig()
slack_token = config.get_secret('SLACK_TOKEN')
slack_channel = config.get('slack_channel')
```

**CLI:**
```bash
python config/host_config.py                   # Show config status
python config/host_config.py --list            # Show non-secret config
python config/host_config.py --list-secrets    # Show secret keys (values hidden)
python config/host_config.py --migrate         # Migrate from legacy locations
```

**Templates:**
- `config/host-config.template.yaml` - Non-secret settings template
- `config/secrets.template.env` - Secrets template

### GitHub Tokens

JIB supports separate tokens for writable and readable repositories:

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | Token for writable repos (or use GitHub App for auto-refresh) |
| `GITHUB_READONLY_TOKEN` | Separate PAT for read-only repos (optional, falls back to `GITHUB_TOKEN`) |

Using a separate read-only token provides security benefits. See [GitHub Integration](../docs/features/github-integration.md) for details.

## repositories.yaml (Source of Truth for Repo Access)

**Single source of truth** for which GitHub repositories jib has read/write access to.

**Location:** `~/.config/egg/repositories.yaml` (created by `./setup.py`)

This file is **not checked into the repo** because it contains user-specific configuration.
See `config/repositories.yaml.example` for the template with all available options.

This file controls:
- Which repos jib can respond to comments on
- Which repos jib can push changes to
- Which repos jib can create PRs in
- Default reviewer for PRs
- GitHub sync configuration
- Docker container extra packages

**Usage:**
- Python: `from config.repo_config import get_writable_repos, is_writable_repo`
- CLI: `python config/repo_config.py --list-writable`

**To add a new repo with write access:**
1. Edit `~/.config/egg/repositories.yaml` and add to `writable_repos` list
2. Reload github-sync service: `systemctl --user daemon-reload && systemctl --user restart github-sync.timer`

## context-filters.yaml

Controls which Confluence spaces, JIRA projects, and repositories are synced.

**Phase 1 (Current)**: MEDIUM risk - Human review + filtering
**Phase 3 (Target)**: LOW risk - DLP scanning + output monitoring

See file for detailed allowlists and blocked patterns.

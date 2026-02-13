# Configuration

Configuration for egg. There are two types of config:

1. **In-repo configs** (this directory) - Version-controlled templates and non-secret settings
2. **Host configs** - User-specific settings, secrets, cache, and runtime data

## Directory Structure Overview

Egg uses several directories under `~/`, each with a specific purpose:

| Directory | Purpose | XDG Compliance |
|-----------|---------|----------------|
| `~/.config/egg/` | User configuration and secrets | `$XDG_CONFIG_HOME` |
| `~/.cache/egg/` | Docker build staging and cache | `$XDG_CACHE_HOME` |
| `~/.egg-sharing/` | Runtime data shared with containers | Kept at `~/` for visibility |
| `~/.egg-worktrees/` | Git worktrees for isolated development | Kept at `~/` for visibility |

### Why `~/.egg-sharing/` and `~/.egg-worktrees/` are at `~/`

While XDG spec suggests `~/.local/share/` for runtime data, we keep these at `~/` for:
- **Visibility**: Users frequently inspect these for debugging
- **Docker simplicity**: Shorter paths are easier to mount
- **Discoverability**: New users can see egg directories with `ls ~`

## Host Configuration (`~/.config/egg/`)

All persistent user configuration is consolidated under `~/.config/egg/`:

```
~/.config/egg/
├── config.yaml        # Non-secret settings (Slack channel, sync intervals, etc.)
├── secrets.env        # All secrets (Slack, GitHub, Confluence, JIRA tokens)
├── github-token       # GitHub token (dedicated file)
├── github-app-id      # GitHub App ID (if using App auth)
├── github-app-installation-id  # GitHub App Installation ID
├── github-app.pem     # GitHub App private key (bot identity)
├── reviewer-app.pem   # Reviewer GitHub App private key (optional, for separate reviewer bot)
└── repositories.yaml  # Repository access configuration (created by setup.py)
```

## Cache Directory (`~/.cache/egg/`)

Docker build staging and cache files (auto-managed, safe to delete):

```
~/.cache/egg/
├── Dockerfile         # Generated Dockerfile for egg image
├── docker-setup.py    # Container setup script
├── entrypoint.py      # Container entrypoint
├── shared/            # Shared modules for container build
├── claude-commands/   # Claude command definitions
├── claude-rules/      # Claude rules/instructions
└── .claude/hooks/     # Claude hooks configuration
```

This directory respects `$XDG_CACHE_HOME` if set.

**Note**: Previously this was `~/.egg/`. The egg script auto-migrates on first run.

**Templates:**
- `config/secrets.template.env` - Secrets template

**In-repo modules:**
- `config/repo_config.py` - Repository access configuration loader
- `config/repositories.yaml.example` - Example repository configuration

### GitHub Tokens

Egg supports separate tokens for writable and readable repositories:

| Variable | Purpose |
|----------|---------|
| `GITHUB_TOKEN` | Token for writable repos (or use GitHub App for auto-refresh) |
| `GITHUB_READONLY_TOKEN` | Separate PAT for read-only repos (optional, falls back to `GITHUB_TOKEN`) |

Using a separate read-only token provides security benefits. GitHub App setup is covered in `./setup.py` and the ADRs.

### Reviewer GitHub App (Optional)

For workflows that post code reviews, a separate reviewer GitHub App can be configured to enable approve/request-changes capabilities on bot-authored PRs (GitHub blocks self-reviews). Reviewer credentials are stored separately from the bot credentials:

| File | Purpose |
|------|---------|
| `secrets.env` | Contains `REVIEWER_APP_ID` and `REVIEWER_APP_INSTALLATION_ID` |
| `reviewer-app.pem` | Reviewer GitHub App private key (PEM format, multiline) |

The gateway's token refresher reads reviewer credentials from these files, following the same pattern as bot credentials. See the [GitHub Automation Guide](../docs/guides/github-automation.md#separate-reviewer-bot-recommended) for setup details.

## repositories.yaml (Source of Truth for Repo Access)

**Single source of truth** for which GitHub repositories egg has read/write access to.

**Location:** `~/.config/egg/repositories.yaml` (created by `./setup.py`)

This file is **not checked into the repo** because it contains user-specific configuration.
See `config/repositories.yaml.example` for the template with all available options.

This file controls:
- Which repos egg can respond to comments on
- Which repos egg can push changes to
- Which repos egg can create PRs in
- Default reviewer for PRs
- GitHub sync configuration
- Docker container extra packages
- Per-repo check commands for SDLC pipeline

**Usage:**
- Python: `from config.repo_config import get_writable_repos, is_writable_repo`
- CLI: `python config/repo_config.py --list-writable`

**To add a new repo with write access:**
1. Edit `~/.config/egg/repositories.yaml` and add to `writable_repos` list
2. Reload github-sync service: `systemctl --user daemon-reload && systemctl --user restart github-sync.timer`

### Per-Repo Check Commands

The `repo_settings` section supports configuring explicit check commands for the SDLC pipeline implement phase. When configured, the checker agent runs these commands instead of auto-discovering test/lint commands.

**Example:**
```yaml
repo_settings:
  your-org/web-app:
    checks:
      - name: lint
        command: npm run lint
      - name: test
        command: npm test
      - name: build
        command: npm run build
```

Each check has:
- `name`: Display label (e.g., "lint", "test", "integration")
- `command`: Shell command to execute

Checks run sequentially during the implement phase checker step. If not configured, the checker falls back to auto-discovery (scanning for Makefile, package.json, pyproject.toml, etc.).

**Configuration:**
- Setup flow: Run `./setup.py` and answer "yes" to "Configure SDLC check commands?"
- Manual: Edit `~/.config/egg/repositories.yaml` and add `checks` under `repo_settings.{repo}`
- Runtime: The gateway injects repo checks via the `EGG_REPO_CHECKS` environment variable (JSON-encoded)

## context-filters.yaml

Controls which Confluence spaces, JIRA projects, and repositories are synced.

**Phase 1 (Current)**: MEDIUM risk - Human review + filtering
**Phase 3 (Target)**: LOW risk - DLP scanning + output monitoring

See file for detailed allowlists and blocked patterns.

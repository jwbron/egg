---
name: egg-setup
description: Walk the user through initial egg setup or update an existing configuration — checks dependencies, configures secrets, repositories, and validates the installation.
disable-model-invocation: true
argument-hint: "[--check | --update secrets | --update repos | --update config]"
---

# Setup

You are guiding the user through egg setup or configuration updates. Walk through the phases below, adapting based on whether this is a fresh install or an update.

## Argument Parsing

Parse arguments after `/egg-setup`:

| Input | Interpretation |
|-------|---------------|
| `/egg-setup` | Full setup — run all phases in order |
| `/egg-setup --check` | Health check only — verify dependencies and config, report status |
| `/egg-setup --update secrets` | Update secrets only (Phase 3) |
| `/egg-setup --update repos` | Update repository configuration only (Phase 4) |
| `/egg-setup --update config` | Update general config only (Phase 5) |

## Phase 1 — Dependency Check

Verify all required dependencies are installed and meet minimum versions. Run these checks using Bash commands:

### Required Dependencies

| Dependency | Check command | Minimum version | Install guidance |
|------------|--------------|-----------------|------------------|
| **Python** | `python3 --version` | 3.11+ | python.org or system package manager |
| **Docker** | `docker --version` | 20.10+ | docker.com/get-docker |
| **Docker Compose** | `docker compose version` | 2.0+ | Included with Docker Desktop; Linux: install docker-compose-plugin |
| **Git** | `git --version` | 2.30+ | git-scm.com |
| **GitHub CLI** | `gh --version` | 2.0+ | cli.github.com |
| **uv** (optional) | `uv --version` | any | For development; `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

### Additional Checks

- **Docker running**: `docker info >/dev/null 2>&1` — if this fails, Docker daemon is not running
- **gh authenticated**: `gh auth status` — if not authenticated, guide user to run `gh auth login`
- **egg CLI installed**: `egg --version 2>/dev/null || which egg` — if missing, advise `pip install -e ./sandbox`

### Reporting

Present results as a compact checklist:

```
## Dependency Check

- [x] Python 3.13.2
- [x] Docker 27.5.1
- [x] Docker Compose v2.32.4
- [x] Git 2.48.1
- [x] GitHub CLI 2.67.0
- [x] Docker daemon running
- [x] gh authenticated as <username>
- [ ] egg CLI not found — run: pip install -e ./sandbox
- [ ] uv not found (optional) — install for development
```

If any **required** dependency is missing or below minimum version, use `AskUserQuestion`:
- **Question**: "Some required dependencies are missing or outdated. Would you like help installing them?"
- **Header**: "Dependencies"
- **Options**:
  - **"Show install instructions"** — description: "Display install commands for each missing dependency"
  - **"Skip and continue"** — description: "Proceed with setup anyway (may fail later)"
  - **"Abort setup"** — description: "Exit setup to install dependencies manually"

If "Show install instructions", detect the platform and show appropriate commands:
- **macOS**: `brew install` commands
- **Linux (Debian/Ubuntu)**: `apt` commands
- **Linux (Fedora/RHEL)**: `dnf` commands
- **Generic**: Direct download links

After showing instructions, ask if the user has installed the dependencies and wants to re-check.

If running `/egg-setup --check`, stop here after reporting the results. Do not proceed to other phases.

## Phase 2 — Existing Configuration Detection

Check for existing configuration at `~/.config/egg/`:

```bash
ls -la ~/.config/egg/ 2>/dev/null
```

If the directory exists and contains configuration files, present a summary:

```
## Existing Configuration Found

| File | Status |
|------|--------|
| config.yaml | Present (last modified: <date>) |
| repositories.yaml | Present |
| secrets.env | Present |
| github-app.pem | Not found |
| launcher-secret | Present |
```

Then use `AskUserQuestion`:
- **Question**: "Existing egg configuration found. What would you like to do?"
- **Header**: "Config"
- **Options**:
  - **"Update existing"** — description: "Keep current config and update specific sections"
  - **"Fresh setup"** — description: "Start from scratch (backs up existing config first)"
  - **"Validate only"** — description: "Check current config for errors without changing anything"

If "Update existing" → ask which section to update (secrets, repos, or general config), then jump to the relevant phase.
If "Fresh setup" → back up existing config to `~/.config/egg/backup-<timestamp>/`, then proceed through all phases.
If "Validate only" → run Phase 6 (Validation) and report results.

If no existing configuration is found, proceed directly to Phase 3.

## Phase 3 — Secrets Configuration

Create the configuration directory if it doesn't exist:

```bash
mkdir -p ~/.config/egg/
```

Configure credentials in `~/.config/egg/secrets.env`. For each secret category, check if already configured and only prompt for missing or explicitly updated values.

### Step 1: Anthropic Authentication

Use `AskUserQuestion`:
- **Question**: "How do you authenticate with Claude?"
- **Header**: "Auth"
- **Options**:
  - **"OAuth Token (Recommended)"** — description: "Uses your Claude.ai account. Run: claude auth status --json | jq -r '.oauthToken'"
  - **"API Key"** — description: "Direct Anthropic API access. Get from console.anthropic.com/settings/keys"

Based on selection:
- **OAuth**: Guide user to run `claude auth status --json | jq -r '.oauthToken'` and paste the token. Validate it starts with `sk-ant-oat`. Write to `CLAUDE_CODE_OAUTH_TOKEN` in secrets.env (this is the preferred variable name — the gateway checks it first before the legacy `ANTHROPIC_OAUTH_TOKEN`).
- **API Key**: Ask for the key. Validate it starts with `sk-ant-api`. Write to `ANTHROPIC_API_KEY` in secrets.env.

### Step 2: GitHub Authentication

Use `AskUserQuestion`:
- **Question**: "How should egg authenticate with GitHub?"
- **Header**: "GitHub"
- **Options**:
  - **"GitHub App (Recommended)"** — description: "Bot identity for PRs and pushes. Requires App ID, Installation ID, and .pem file"
  - **"Personal Access Token"** — description: "Uses your personal GitHub identity. Simpler setup but PRs are attributed to you"
  - **"Both"** — description: "GitHub App for bot repos + PAT for personal repos (auth_mode: user)"

Based on selection:

**GitHub App flow**:
1. Ask for GitHub App ID (validate it's numeric). Write to `GITHUB_APP_ID` in secrets.env.
2. Ask for Installation ID (validate it's numeric). Write to `GITHUB_APP_INSTALLATION_ID` in secrets.env.
3. Ask for path to the `.pem` private key file. Validate the file exists and ends with `.pem`. Copy it to `~/.config/egg/github-app.pem` with `chmod 600`.
4. Ask for the bot name (must match the GitHub App name exactly). Write to `GATEWAY_BOT_NAME` in secrets.env.
5. Ask for the branch prefix (default: same as bot name, typically `egg`). Write to `GATEWAY_BOT_BRANCH_PREFIX` in secrets.env.
6. Ask for the user's GitHub username for `GATEWAY_TRUSTED_USERS` in secrets.env (comma-separated list of GitHub usernames allowed to interact with the bot).

**PAT flow**:
1. Ask for the GitHub PAT. Validate it starts with `ghp_` or `github_pat_`.
2. Set both `GITHUB_TOKEN` and `GITHUB_USER_TOKEN` to the provided PAT in secrets.env. (`GITHUB_TOKEN` is used by the gateway for all repos by default; `GITHUB_USER_TOKEN` is used for repos with `auth_mode: user`.)
3. Set `GATEWAY_BOT_NAME=egg` and `GATEWAY_BOT_BRANCH_PREFIX=egg` as defaults in secrets.env.
4. Ask for the user's GitHub username for `GATEWAY_TRUSTED_USERS` in secrets.env.

**Both flow**:
1. Run GitHub App flow steps 1–6.
2. Ask for a GitHub PAT. Validate it starts with `ghp_` or `github_pat_`.
3. Set `GITHUB_USER_TOKEN` to the provided PAT in secrets.env. Do **NOT** set `GITHUB_TOKEN` — the App's token refresher manages default authentication at runtime via `GITHUB_APP_ID` and the `.pem` key.
4. Verify `GATEWAY_TRUSTED_USERS` was set in step 1 (App flow step 6). If not, ask for the user's GitHub username.

### Step 3: Optional Integrations

Use `AskUserQuestion` (multiSelect):
- **Question**: "Which optional integrations do you want to configure?"
- **Header**: "Integrations"
- **multiSelect**: true
- **Options**:
  - **"Slack"** — description: "Bot notifications and task requests (requires Slack App)"
  - **"Confluence"** — description: "Sync ADRs, runbooks, and best practices"
  - **"JIRA"** — description: "Sync tickets, requirements, and sprint info"
  - **"None"** — description: "Skip optional integrations"

For each selected integration, collect the required credentials:

- **Slack**: Collect `SLACK_TOKEN` (bot token, starts with `xoxb-`) and `SLACK_APP_TOKEN` (app-level token, starts with `xapp-`).
- **Confluence**: Collect `CONFLUENCE_BASE_URL` (e.g., `https://yoursite.atlassian.net`), `CONFLUENCE_USERNAME` (email), `CONFLUENCE_API_TOKEN`, and `CONFLUENCE_SPACE_KEYS` (comma-separated space keys).
- **JIRA**: Collect `JIRA_BASE_URL` (e.g., `https://yoursite.atlassian.net`), `JIRA_USERNAME` (email), `JIRA_API_TOKEN`, and `JIRA_JQL_QUERY` (default: `project = <KEY> AND status != Done`).

See `config/secrets.template.env` for additional details on field formats.

### Step 4: Generate Launcher Secret

Automatically generate the launcher secret if it doesn't exist:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.config/egg/launcher-secret
chmod 600 ~/.config/egg/launcher-secret
```

### Step 5: Write secrets.env

Write all collected secrets to `~/.config/egg/secrets.env` with `chmod 600`. Group by category with comments. Never overwrite values the user chose to keep.

Reference `config/secrets.template.env` for the complete list of supported variables and their formats. The required variable names are:

| Category | Variable Name | Source |
|----------|--------------|--------|
| Anthropic OAuth | `CLAUDE_CODE_OAUTH_TOKEN` | Phase 3 Step 1 (preferred over legacy `ANTHROPIC_OAUTH_TOKEN`) |
| Anthropic API key | `ANTHROPIC_API_KEY` | Phase 3 Step 1 |
| GitHub App ID | `GITHUB_APP_ID` | Phase 3 Step 2 |
| GitHub App Install ID | `GITHUB_APP_INSTALLATION_ID` | Phase 3 Step 2 |
| GitHub default token | `GITHUB_TOKEN` | Phase 3 Step 2 (PAT flow) |
| GitHub user PAT | `GITHUB_USER_TOKEN` | Phase 3 Step 2 (PAT or Both flow) |
| GitHub read-only token | `GITHUB_READONLY_TOKEN` | Phase 3 Step 2 (optional — for separate read-only credentials) |
| Gateway bot name | `GATEWAY_BOT_NAME` | Phase 3 Step 2 |
| Gateway branch prefix | `GATEWAY_BOT_BRANCH_PREFIX` | Phase 3 Step 2 |
| Gateway trusted users | `GATEWAY_TRUSTED_USERS` | Phase 3 Step 2 |
| Slack bot token | `SLACK_TOKEN` | Phase 3 Step 3 |
| Slack app token | `SLACK_APP_TOKEN` | Phase 3 Step 3 |
| Confluence base URL | `CONFLUENCE_BASE_URL` | Phase 3 Step 3 |
| Confluence username | `CONFLUENCE_USERNAME` | Phase 3 Step 3 |
| Confluence API token | `CONFLUENCE_API_TOKEN` | Phase 3 Step 3 |
| Confluence space keys | `CONFLUENCE_SPACE_KEYS` | Phase 3 Step 3 |
| JIRA base URL | `JIRA_BASE_URL` | Phase 3 Step 3 |
| JIRA username | `JIRA_USERNAME` | Phase 3 Step 3 |
| JIRA API token | `JIRA_API_TOKEN` | Phase 3 Step 3 |
| JIRA JQL query | `JIRA_JQL_QUERY` | Phase 3 Step 3 |

## Phase 4 — Repository Configuration

Configure `~/.config/egg/repositories.yaml`.

### Step 1: GitHub Username

Ask for the user's GitHub username. Try to auto-detect from `gh api user --jq .login` first.

### Step 2: Local Repositories

Collect paths to local git repositories to mount into the container. Use `AskUserQuestion` iteratively:

1. Ask: "Enter a path to a local git repository to mount into the egg container."
2. Validate the path exists and is a git repo (has `.git/` directory).
3. Auto-detect the remote URL to determine `owner/repo` format.
4. Ask: "Add another repository?" with options **"Yes"** / **"No, done adding repos"**. If "Yes", repeat from step 1.

Present the detected repos:
```
## Detected Repositories

| Local Path | Remote | Owner/Repo |
|------------|--------|------------|
| /home/user/projects/my-app | github.com | user/my-app |
| /home/user/work/api-service | github.com | org/api-service |
```

### Step 3: Writable vs Read-only

For each detected repo, use `AskUserQuestion`:
- **Question**: "What access level should egg have for <owner/repo>?"
- **Header**: "Access"
- **Options**:
  - **"Writable"** — description: "Can push code, create PRs, respond to comments"
  - **"Read-only"** — description: "Can monitor and analyze, but not modify"

### Step 4: Per-repo Settings

For each writable repo, ask about optional settings:

Use `AskUserQuestion` (multiSelect):
- **Question**: "Configure optional settings for <owner/repo>?"
- **Header**: "Settings"
- **multiSelect**: true
- **Options**:
  - **"Custom check commands"** — description: "Specify lint/test commands for SDLC pipeline (default: auto-discover)"
  - **"User auth mode"** — description: "Use personal PAT instead of bot identity for this repo"
  - **"None"** — description: "Use defaults"

If "Custom check commands": collect name/command pairs for each check.
If "User auth mode": set `auth_mode: user` for this repo.

### Step 5: Bot Username

Set `bot_username` in repositories.yaml. Default to the `GATEWAY_BOT_NAME` value from Phase 3. This is used by the gateway for bot PR identification.

### Step 6: Default Reviewer

Set the default PR reviewer to the GitHub username collected in Step 1.

### Step 7: Write repositories.yaml

Write the configuration to `~/.config/egg/repositories.yaml`. Repos marked "Writable" go under `writable_repos`, repos marked "Read-only" go under `readable_repos`.

## Phase 5 — General Configuration

Create or update `~/.config/egg/config.yaml` with system-detected defaults.

Auto-detect these values (do not prompt unless detection fails):
- `host_home`: from `$HOME`
- `host_uid`: from `id -u`
- `host_gid`: from `id -g`
- `anthropic_auth_method`: based on which credential was configured in Phase 3

Set sensible defaults for:
- `git_name`: "egg"
- `git_email`: "egg@localhost"
- `compose_project_name`: "egg"
- `gateway_api_port`: 9848
- `gateway_proxy_port`: 3129
- `orchestrator_api_port`: 9849
- `mcp_server_port`: 9850
- `mcp_rate_limit`: 30

Write to `~/.config/egg/config.yaml`.

## Phase 6 — Validation

Run a comprehensive validation of the setup. Check each component and report results.

### Configuration Validation

```bash
# Check all required config files exist
ls ~/.config/egg/config.yaml
ls ~/.config/egg/repositories.yaml
ls ~/.config/egg/secrets.env
ls ~/.config/egg/launcher-secret
```

### Secrets Validation

Parse `~/.config/egg/secrets.env` and verify each required variable is set to a non-empty value. **Never display raw secret values** — only check for presence and validate prefixes (e.g., `sk-ant-oat`, `ghp_`). Do not `cat` or print the file contents.

Verify:
- At least one Anthropic credential is set (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`)
- GitHub credentials are configured (`GITHUB_APP_ID` + `GITHUB_APP_INSTALLATION_ID` for App, or `GITHUB_TOKEN` for PAT)
- `GATEWAY_BOT_NAME` is set
- `GATEWAY_BOT_BRANCH_PREFIX` is set
- File permissions are 600

### Repository Validation

Parse `~/.config/egg/repositories.yaml` and verify:
- `github_username` is set
- At least one entry in `writable_repos` or `local_repos.paths`
- All local repo paths exist and are git repositories
- For repos with `auth_mode: user`, verify `GITHUB_USER_TOKEN` is set in secrets.env

### Docker Validation

```bash
# Check images can build (dry-run is not available, so just check Dockerfile exists)
ls Dockerfile 2>/dev/null || ls sandbox/Dockerfile 2>/dev/null

# Check for port conflicts (cross-platform)
# Linux:
ss -tlnp 2>/dev/null | grep -E ':(9848|9849|9850|3129) ' || \
# macOS:
lsof -i :9848 -i :9849 -i :9850 -i :3129 2>/dev/null || \
# Fallback (cross-platform Python):
python3 -c "
import socket
for port in [9848, 9849, 9850, 3129]:
    s = socket.socket()
    try:
        s.bind(('', port))
        s.close()
    except OSError:
        print(f'Port {port} in use')
" 2>/dev/null || echo "Ports available"
```

### Report

Present a final validation report:

```
## Setup Validation

### Configuration Files
- [x] config.yaml — valid
- [x] repositories.yaml — valid
- [x] secrets.env — valid (permissions: 600)
- [x] launcher-secret — valid

### Credentials
- [x] Anthropic: OAuth token configured
- [x] GitHub: App configured (App ID: 12345)
- [x] Gateway: bot_name=my-bot, branch_prefix=egg

### Repositories
- [x] user/my-app — writable, local path valid
- [x] org/api-service — read-only

### Infrastructure
- [x] Docker daemon running
- [x] Ports 9848, 9849, 9850, 3129 available
```

If any validation fails, highlight the issue and offer to fix it (jump back to the relevant phase).

## Phase 7 — Next Steps

After successful setup or validation, show:

```
## Setup Complete

Your egg configuration is ready. Here's what to do next:

### First Run
  egg --compose    # Start gateway + orchestrator (builds images on first run)
  egg              # Start an interactive Claude Code session

### Quick Commands
  egg --compose --down   # Stop gateway + orchestrator
  egg --private          # Run in private mode (Anthropic API only)
  egg --exec "cmd"       # Run a command in an ephemeral container

### SDLC Pipeline
  Inside the sandbox, run:
  /sdlc                        # Prompt-driven pipeline
  /sdlc -r <repo> -i <issue>   # Issue-driven pipeline

### Update Configuration Later
  /egg-setup --update secrets   # Update API keys and tokens
  /egg-setup --update repos     # Add or modify repositories
  /egg-setup --update config    # Update general settings
  /egg-setup --check            # Verify your setup is healthy
```

## Critical Rules

- **Never expose secrets** — mask tokens when displaying (show first 8 and last 4 chars only)
- **Always validate input** — check token formats, file existence, path validity before writing
- **Back up before overwriting** — when updating existing config, create a timestamped backup first
- **Respect existing values** — when updating, only change what the user explicitly asks to change
- **Use chmod 600** for secrets.env, .pem files, and launcher-secret
- **Auto-detect when possible** — minimize questions by detecting platform, username, repo info automatically
- **Keep output concise** — use checklists and tables, not verbose paragraphs

---
name: egg-setup
description: Walk the user through initial egg setup or update an existing configuration - checks dependencies, configures secrets, repositories, and validates the k3s deployment.
disable-model-invocation: true
argument-hint: "[--check | --update secrets | --update repos | --update config]"
---

# Setup

You are guiding the user through egg setup or configuration updates. egg deploys to a local k3s cluster (the Docker Compose deployment and interactive `egg` CLI were removed in #1762); the human drives pipelines from their own Claude Code session via the orchestrator's MCP server. Walk through the phases below, adapting based on whether this is a fresh install or an update.

## Argument Parsing

Parse arguments after `/egg-setup`:

| Input | Interpretation |
|-------|---------------|
| `/egg-setup` | Full setup: run all phases in order |
| `/egg-setup --check` | Health check only: verify dependencies, config, and cluster; report status |
| `/egg-setup --update secrets` | Update secrets only (Phase 3) |
| `/egg-setup --update repos` | Update repository configuration only (Phase 4) |
| `/egg-setup --update config` | Update general config only (Phase 5) |

## Phase 1 - Dependency Check

Verify all required dependencies are installed and meet minimum versions. Run these checks using Bash commands:

### Required Dependencies

| Dependency | Check command | Minimum version | Install guidance |
|------------|--------------|-----------------|------------------|
| **Git** | `git --version` | 2.30+ | git-scm.com |
| **Docker** | `docker --version` | 20.10+ | docker.com/get-docker (builds images; the cluster runs on containerd) |
| **GitHub CLI** | `gh --version` | 2.0+ | cli.github.com |
| **envsubst** | `envsubst --version` | any | GNU gettext: `dnf install gettext` / `apt install gettext` / `brew install gettext` |
| **openssl** | `openssl version` | any | System package manager |
| **Python** | `python3 --version` | 3.x (3.14+ for development) | python.org or system package manager |
| **make** | `make --version` | any | System package manager |
| **uv** (optional) | `uv --version` | any | For development: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **claude** (optional) | `claude --version` | any | Claude Code CLI; needed for MCP registration in Phase 7 |

### Cluster Check

- **k3s installed**: `kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml get nodes` succeeds and shows a Ready node. If k3s is absent, that is fine for a fresh install; `make k3s-setup` (Phase 7) installs it. Note it in the report rather than failing.
- **Platform**: on macOS, k3s requires a Linux VM (Lima or Rancher Desktop) and that path is not scripted or tested by this repo; warn the user and point at issue #3155 before continuing.

### Additional Checks

- **Docker running**: `docker info >/dev/null 2>&1`; if this fails, the Docker daemon is not running
- **gh authenticated**: `gh auth status`; if not authenticated, guide the user to run `gh auth login`

### Reporting

Present results as a compact checklist:

```
## Dependency Check

- [x] Git 2.48.1
- [x] Docker 27.5.1 (daemon running)
- [x] GitHub CLI 2.67.0 (authenticated as <username>)
- [x] envsubst 0.22
- [x] openssl 3.2.4
- [x] Python 3.14.0
- [x] make 4.4.1
- [ ] k3s not installed (make k3s-setup will install it)
- [ ] uv not found (optional; install for development)
```

If any **required** dependency is missing or below minimum version, use `AskUserQuestion`:
- **Question**: "Some required dependencies are missing or outdated. Would you like help installing them?"
- **Header**: "Dependencies"
- **Options**:
  - **"Show install instructions"** - description: "Display install commands for each missing dependency"
  - **"Skip and continue"** - description: "Proceed with setup anyway (may fail later)"
  - **"Abort setup"** - description: "Exit setup to install dependencies manually"

If "Show install instructions", detect the platform and show appropriate commands:
- **macOS**: `brew install` commands
- **Linux (Debian/Ubuntu)**: `apt` commands
- **Linux (Fedora/RHEL)**: `dnf` commands
- **Generic**: Direct download links

After showing instructions, ask if the user has installed the dependencies and wants to re-check.

If running `/egg-setup --check`, skip to Phase 6 (Validation) after reporting dependency results. Do not run Phases 2–5.

## Phase 2 - Existing Configuration Detection

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
| lifecycle-secret | Present |
```

Then use `AskUserQuestion`:
- **Question**: "Existing egg configuration found. What would you like to do?"
- **Header**: "Config"
- **Options**:
  - **"Update existing"** - description: "Keep current config and update specific sections"
  - **"Fresh setup"** - description: "Start from scratch (backs up existing config first)"
  - **"Validate only"** - description: "Check current config for errors without changing anything"

If "Update existing" → ask which section to update (secrets, repos, or general config), then jump to the relevant phase.
If "Fresh setup" → back up existing config to `~/.config/egg/backup-<timestamp>/`, then proceed through all phases.
If "Validate only" → run Phase 6 (Validation) and report results.

If no existing configuration is found, proceed directly to Phase 3.

## Phase 3 - Secrets Configuration

### Step 0: Seed the config directory

Run the non-interactive initializer first; it is idempotent and never overwrites existing files:

```bash
bin/egg-deploy init
```

This creates `~/.config/egg/` with `config.yaml` (host identity auto-detected), `launcher-secret`, `lifecycle-secret`, and templates for `secrets.env` and `repositories.yaml`. The remaining steps fill in the template values. For each secret category, check what is already configured and only prompt for missing or explicitly updated values.

### Step 1: Anthropic Authentication

Use `AskUserQuestion`:
- **Question**: "How do you authenticate with Claude?"
- **Header**: "Auth"
- **Options**:
  - **"OAuth Token (Recommended)"** - description: "Uses your Claude.ai account. Run: claude auth status --json | jq -r '.oauthToken'"
  - **"API Key"** - description: "Direct Anthropic API access. Get from console.anthropic.com/settings/keys"

Based on selection:
- **OAuth**: Guide the user to run `claude auth status --json | jq -r '.oauthToken'` and paste the token. Validate it starts with `sk-ant-oat`. Write to `CLAUDE_CODE_OAUTH_TOKEN` in secrets.env (the gateway checks this first; `ANTHROPIC_OAUTH_TOKEN` is the legacy name).
- **API Key**: Ask for the key. Validate it starts with `sk-ant-api`. Write to `ANTHROPIC_API_KEY` in secrets.env.

### Step 2: GitHub Authentication

Use `AskUserQuestion`:
- **Question**: "How should egg authenticate with GitHub?"
- **Header**: "GitHub"
- **Options**:
  - **"GitHub App (Recommended)"** - description: "Bot identity for PRs and pushes. Requires App ID, Installation ID, and .pem file"
  - **"Personal Access Token"** - description: "Uses your personal GitHub identity. Simpler setup but PRs are attributed to you"
  - **"Both"** - description: "GitHub App for bot repos + PAT for personal repos (auth_mode: user)"

Based on selection:

**GitHub App flow**:
1. Ask for GitHub App ID (validate it's numeric). Write to `GITHUB_APP_ID` in secrets.env.
2. Ask for Installation ID (validate it's numeric). Write to `GITHUB_APP_INSTALLATION_ID` in secrets.env.
3. Ask for path to the `.pem` private key file. Validate the file exists and ends with `.pem`. Copy it to `~/.config/egg/github-app.pem` with `chmod 600`.
4. Ask for the bot name (must match the GitHub App name exactly). Write to `GATEWAY_BOT_NAME` in secrets.env.
5. Ask for the branch prefix (default: same as bot name, typically `egg`). Write to `GATEWAY_BOT_BRANCH_PREFIX` in secrets.env.
6. Ask for the user's GitHub username for `GATEWAY_TRUSTED_USERS` in secrets.env (comma-separated list of GitHub usernames allowed to interact with the bot).

**PAT flow**:
1. Ask for the GitHub PAT (fine-grained, with Contents R/W, Pull requests R/W, Issues R/W). Validate it starts with `ghp_` or `github_pat_`.
2. Set both `GITHUB_TOKEN` and `GITHUB_USER_TOKEN` to the provided PAT in secrets.env. (`GITHUB_TOKEN` is used by the gateway for all repos by default; `GITHUB_USER_TOKEN` is used for repos with `auth_mode: user`.)
3. Set `GATEWAY_BOT_NAME=egg` and `GATEWAY_BOT_BRANCH_PREFIX=egg` as defaults in secrets.env.
4. Ask for the user's GitHub username for `GATEWAY_TRUSTED_USERS` in secrets.env.
5. Remind the user to set `auth_mode: user` for their repos in Phase 4 so operations attribute to their account.

**Both flow**:
1. Run GitHub App flow steps 1–6.
2. Ask for a GitHub PAT. Validate it starts with `ghp_` or `github_pat_`.
3. Set `GITHUB_USER_TOKEN` to the provided PAT in secrets.env. Do **NOT** set `GITHUB_TOKEN`; the App's token refresher manages default authentication at runtime via `GITHUB_APP_ID` and the `.pem` key.
4. Verify `GATEWAY_TRUSTED_USERS` was set in step 1 (App flow step 6). If not, ask for the user's GitHub username.

### Step 3: Optional Integrations

Use `AskUserQuestion` (multiSelect):
- **Question**: "Which optional integrations do you want to configure?"
- **Header**: "Integrations"
- **multiSelect**: true
- **Options**:
  - **"Atlassian (Jira + Confluence)"** - description: "Gateway wrappers for Jira tickets and Confluence pages (private mode)"
  - **"Slack"** - description: "Bot notifications and task requests (requires Slack App)"
  - **"LiteLLM routing"** - description: "Route individual agent roles to non-Claude models"
  - **"None"** - description: "Skip optional integrations"

For each selected integration, collect the required credentials:

- **Atlassian**: Collect `ATLASSIAN_BASE_URL` (e.g., `https://yoursite.atlassian.net`, no trailing slash), `ATLASSIAN_USERNAME` (account email), and `ATLASSIAN_API_TOKEN` (from id.atlassian.com/manage-profile/security/api-tokens). One tenant-wide account covers both Jira and Confluence; the legacy per-service `JIRA_*` / `CONFLUENCE_*` triples still work as fallbacks. Remind the user that project/space allowlists live in `config/context-filters.yaml`, not secrets.env.
- **Slack**: Collect `SLACK_TOKEN` (bot token, starts with `xoxb-`) and `SLACK_APP_TOKEN` (app-level token, starts with `xapp-`).
- **LiteLLM**: Collect `LITELLM_MASTER_KEY` (any random secret; empty disables LiteLLM routing entirely) and optionally `OPENROUTER_API_KEY`. Point the user at docs/guides/per-agent-models.md for registering model backends via `~/.config/egg/litellm-models.yaml`.

See `config/secrets.template.env` for the full list of supported variables and their formats.

### Step 4: Infrastructure Secrets

`bin/egg-deploy init` (Step 0) already generated `launcher-secret` and `lifecycle-secret`. Verify both exist with mode 600; if either is missing, generate it:

```bash
openssl rand -hex 32 > ~/.config/egg/launcher-secret && chmod 600 ~/.config/egg/launcher-secret
openssl rand -hex 32 > ~/.config/egg/lifecycle-secret && chmod 600 ~/.config/egg/lifecycle-secret
```

### Step 5: Write secrets.env

Write all collected secrets to `~/.config/egg/secrets.env` with `chmod 600`. Group by category with comments, following the layout of `config/secrets.template.env`. Never overwrite values the user chose to keep.

Note that `make k3s-secrets` (run automatically by `make deploy`) bundles every file in `~/.config/egg/` into the `gateway-secrets` k8s Secret, so changes here take effect on the next deploy (or `make k3s-secrets` standalone).

## Phase 4 - Repository Configuration

Configure `~/.config/egg/repositories.yaml` (seeded from `config/repositories.yaml.example` by Step 0; the template documents every field).

### Step 1: GitHub Username

Ask for the user's GitHub username. Try to auto-detect from `gh api user --jq .login` first.

### Step 2: Local Repositories

Collect paths to local git repositories to mount into the cluster (`make deploy` derives the hostPath mounts from these). Use `AskUserQuestion` iteratively:

1. Ask: "Enter a path to a local git repository egg should work on."
2. Validate the path exists and is a git repo (has `.git/`).
3. Auto-detect the remote URL to determine `owner/repo` format (`git -C <path> remote get-url origin`).
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
  - **"Writable"** - description: "Can push code, create PRs, respond to comments"
  - **"Read-only"** - description: "Can monitor and analyze, but not modify"

### Step 4: Per-repo Settings

For each writable repo, ask about optional settings:

Use `AskUserQuestion` (multiSelect):
- **Question**: "Configure optional settings for <owner/repo>?"
- **Header**: "Settings"
- **multiSelect**: true
- **Options**:
  - **"Custom check commands"** - description: "Specify lint/test commands for SDLC pipeline (default: auto-discover)"
  - **"User auth mode"** - description: "Use personal PAT instead of bot identity for this repo"
  - **"Build commands"** - description: "Bake dependency installs (node_modules, .venv) into the sandbox image"
  - **"None"** - description: "Use defaults"

If "Custom check commands": collect name/command pairs for each check.
If "User auth mode": set `auth_mode: user` for this repo (requires `GITHUB_USER_TOKEN` from Phase 3).
If "Build commands": walk through `build_commands` (`watch_files`, `commands`, `persist_dirs`, `persist_system_dirs`) using the worked examples in `config/repositories.yaml.example`; these have subtle traps (see the uv and Go examples there), so copy the closest example rather than improvising.

### Step 5: Bot Username

Set `bot_username` in repositories.yaml. Default to the `GATEWAY_BOT_NAME` value from Phase 3. This is used by the gateway for bot PR identification.

### Step 6: Default Reviewer

Set the default PR reviewer to the GitHub username collected in Step 1.

### Step 7: Write repositories.yaml

Write the configuration to `~/.config/egg/repositories.yaml`. Repos marked "Writable" go under `writable_repos`, repos marked "Read-only" go under `readable_repos`, and every local path goes under `local_repos.paths`.

## Phase 5 - General Configuration

Review or update `~/.config/egg/config.yaml` (created by Step 0 with system-detected defaults).

Auto-detect these values (do not prompt unless detection fails):
- `host_home`: from `$HOME`
- `host_uid`: from `id -u`
- `host_gid`: from `id -g`
- `anthropic_auth_method`: based on which credential was configured in Phase 3 (`oauth` or `api_key`)

Set sensible defaults for:
- `git_name`: "egg"
- `git_email`: "egg@localhost"
- `gateway_api_port`: 9848
- `gateway_proxy_port`: 3129
- `orchestrator_api_port`: 9849
- `mcp_server_port`: 9850
- `mcp_rate_limit`: 30

## Phase 6 - Validation

Run a comprehensive validation of the setup. Check each component and report results.

### Configuration Validation

```bash
# Check all required config files exist
ls ~/.config/egg/config.yaml
ls ~/.config/egg/repositories.yaml
ls ~/.config/egg/secrets.env
ls ~/.config/egg/launcher-secret
ls ~/.config/egg/lifecycle-secret
```

### Secrets Validation

Parse `~/.config/egg/secrets.env` and verify each required variable is set to a non-empty value. **Never display raw secret values**; only check for presence and validate prefixes (e.g., `sk-ant-oat`, `ghp_`). Do not `cat` or print the file contents.

Verify:
- At least one Anthropic credential is set (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`)
- GitHub credentials are configured (`GITHUB_APP_ID` + `GITHUB_APP_INSTALLATION_ID` for App, or `GITHUB_TOKEN` for PAT)
- `GATEWAY_BOT_NAME` is set
- `GATEWAY_BOT_BRANCH_PREFIX` is set
- File permissions are 600 on secrets.env, launcher-secret, lifecycle-secret, and any .pem file

### Repository Validation

Parse `~/.config/egg/repositories.yaml` and verify:
- `github_username` is set
- At least one entry in `writable_repos` or `local_repos.paths`
- All local repo paths exist and are git repositories
- For repos with `auth_mode: user`, verify `GITHUB_USER_TOKEN` is set in secrets.env

### Cluster Validation

Skip this section (and note it in the report) if k3s is not installed yet.

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Node ready?
kubectl get nodes

# egg deployed and healthy?
kubectl get pods -n egg-system

# Orchestrator REST health (bound to the host on 9849 by the local overlay)
curl -sf --max-time 5 http://localhost:9849/api/v1/health

# Gateway health (ClusterIP only; check from inside the cluster)
kubectl exec -n egg-system deploy/orchestrator -- curl -sf http://gateway:9848/api/v1/health
```

If egg is not deployed yet, instead check that the host ports it needs are free (9849, 9850 for the orchestrator hostPorts; 5000 for the local registry; 6443 for the k3s API):

```bash
python3 -c "
import socket, sys
conflicts = []
for port in [9849, 9850, 5000, 6443]:
    s = socket.socket()
    try:
        s.bind(('', port))
    except OSError:
        conflicts.append(port)
    finally:
        s.close()
print('Ports in use: ' + ', '.join(map(str, conflicts)) if conflicts else 'All ports available')
"
```

### MCP Registration Validation

```bash
claude mcp list 2>/dev/null | grep -i egg
```

If the `egg` server is missing and the orchestrator is running, offer to register it (Phase 7, step 3).

### Report

Present a final validation report:

```
## Setup Validation

### Configuration Files
- [x] config.yaml - valid
- [x] repositories.yaml - valid
- [x] secrets.env - valid (permissions: 600)
- [x] launcher-secret - valid
- [x] lifecycle-secret - valid

### Credentials
- [x] Anthropic: OAuth token configured
- [x] GitHub: App configured (App ID: 12345)
- [x] Gateway: bot_name=my-bot, branch_prefix=egg

### Repositories
- [x] user/my-app - writable, local path valid
- [x] org/api-service - read-only

### Cluster
- [x] k3s node Ready
- [x] egg-system pods Running (gateway, orchestrator, redis, litellm)
- [x] Orchestrator health OK (localhost:9849)
- [x] egg MCP server registered in Claude Code
```

If any validation fails, highlight the issue and offer to fix it (jump back to the relevant phase).

## Phase 7 - Next Steps

After successful setup or validation, show the steps that still apply (skip ones the validation showed as already done):

```
## Setup Complete

Your egg configuration is ready. Here's what to do next:

### First Deploy
  make k3s-setup     # one-time: install k3s + Cilium (also sets up the local registry)
  make build         # build images (the sandbox image is large; first build takes a while)
  make k3s-push      # publish images to the local registry
  make deploy        # create secrets + deploy to k3s
  kubectl get pods -n egg-system   # verify

### Connect Claude Code
  claude mcp add --transport http --scope user egg http://localhost:9850/mcp
  mkdir -p ~/.claude/skills && ln -s "$PWD"/skills/* ~/.claude/skills/   # optional: /sdlc etc.

### Run Your First Pipeline
  From your MCP-connected Claude Code session:
  /sdlc                        # prompt-driven pipeline
  /sdlc 123                    # issue-driven pipeline (GitHub issue number)
  Or call the MCP tool directly: submit_task(issue_number=123, repo="owner/name")

### After Code Changes
  make redeploy      # rebuild, publish, and redeploy in one step

### Update Configuration Later
  /egg-setup --update secrets   # update API keys and tokens
  /egg-setup --update repos     # add or modify repositories
  /egg-setup --update config    # update general settings
  /egg-setup --check            # verify your setup is healthy
```

## Critical Rules

- **Never expose secrets**: mask tokens when displaying (show first 8 and last 4 chars only)
- **Always validate input**: check token formats, file existence, path validity before writing
- **Back up before overwriting**: when updating existing config, create a timestamped backup first
- **Respect existing values**: when updating, only change what the user explicitly asks to change
- **Use chmod 600** for secrets.env, .pem files, launcher-secret, and lifecycle-secret
- **Auto-detect when possible**: minimize questions by detecting platform, username, repo info automatically
- **Keep output concise**: use checklists and tables, not verbose paragraphs
- **Never run sudo yourself**: `make k3s-setup` and `make deploy` invoke sudo internally; run them as the user from a terminal they control, or print the command for the user to run

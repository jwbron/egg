# Declarative Setup Architecture

The egg setup system uses a Python-based declarative approach with consolidated configuration files, replacing the original bash setup script for maintainability and testability.

## Configuration Files

All configuration lives in `~/.config/egg/` with two files:

### `secrets.env`

```bash
# egg Secrets Configuration — DO NOT COMMIT

# === REQUIRED ===
SLACK_TOKEN="xoxb-..."           # Bot User OAuth Token
SLACK_APP_TOKEN="xapp-..."       # App-Level Token (Socket Mode)

# === OPTIONAL ===
# GITHUB_TOKEN="ghp_..."
# GITHUB_READONLY_TOKEN="ghp_..."
# CONFLUENCE_BASE_URL="https://company.atlassian.net/wiki"
# CONFLUENCE_USERNAME="user@example.com"
# CONFLUENCE_API_TOKEN="..."
# CONFLUENCE_SPACE_KEYS="ENG,TEAM"
# JIRA_BASE_URL="https://company.atlassian.net"
# JIRA_USERNAME="user@example.com"
# JIRA_API_TOKEN="..."
# JIRA_JQL_QUERY="project = ENG AND status != Done"
```

### `config.yaml`

```yaml
# egg Configuration — safe to version control (without personal values)

bot_name: "egg"
github_username: ""

writable_repos:
  - "${github_username}/egg"
readable_repos:
  - "khan/webapp"

slack_channel: ""
allowed_users:
  - ""

context_sync_interval: 30
github_sync_interval: 5
```

## CLI Interface

> **Removal note:** The `egg --setup` / `egg --setup --full` flags and
> the `egg_lib.setup_flow` wizard they drove were removed in
> [#1762](https://github.com/jwbron/egg/issues/1762) along with the
> rest of interactive mode. The sections below are retained for
> historical reference.
>
> Replace with `bin/egg-deploy init` (generates `launcher-secret`) and
> fill in the rest of `~/.config/egg/` by hand — see the
> [Deployment Guide](../guides/deployment.md).

```bash
egg --setup                      # [removed in #1762]
egg --setup --full               # [removed in #1762]
```

## Minimal Setup Flow

The default mode prompted for:
1. **GitHub username** (auto-detected from `gh` CLI)
2. **Bot name** (must match your GitHub App name)
3. **Slack tokens** (validated for xoxb-/xapp- prefixes)
4. **GitHub App** or PAT configuration
5. **Writable/readable repositories**

All other settings use sensible defaults.

## Service Management

Services are categorized into **core** (enabled by default) and **LLM-based** (opt-in):

### Core Services

| Service | Purpose |
|---------|---------|
| slack-notifier.service | Send notifications to Slack |
| slack-receiver.service | Receive messages from Slack |
| gateway.service | Git/GitHub operations + token refresh |
| worktree-watcher.timer | Clean up orphaned worktrees |

### LLM-Based Services (Opt-in)

| Service | Purpose | Token Required |
|---------|---------|----------------|
| context-sync.timer | Sync Confluence/JIRA | Anthropic API |
| github-watcher.timer | Watch GitHub PRs/issues | Anthropic API |
| conversation-analyzer.timer | Analyze conversations daily | Anthropic API |
| egg-doc-generator.timer | Generate docs weekly | Anthropic API |
| adr-researcher.timer | Research architecture docs weekly | Anthropic API |

## Implementation Status

> The `egg_lib/setup_flow.py` wizard module was removed in
> [#1762](https://github.com/jwbron/egg/issues/1762) along with
> `bin/egg`, `egg_lib/cli.py`, and `egg_lib/compose.py`. The
> paragraph below describes the historical state.

Setup was integrated into `egg_lib/setup_flow.py` and invoked via `egg --setup`:
- Interactive wizard for `secrets.env` and `config.yaml` creation
- The old `setup.sh` bash script had been removed
- Standalone `setup.py` entry point and service management flags (`--enable-services`, `--disable-services`) were deferred

## Related Documentation

- [Architecture Overview](README.md) — System design
- [Deployment Guide](../guides/deployment.md) — Current `bin/egg-deploy` based deployment
- [repo_config.py](../../config/repo_config.py) — Repository config loader

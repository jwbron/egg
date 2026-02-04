# ADR: Declarative Setup Architecture

**Driver:** James Wiesebron
**Approver:** James Wiesebron
**Status:** Proposed
**Proposed:** December 2025

## Table of Contents

- [Context](#context)
- [Decision](#decision)
- [Implementation Details](#implementation-details)
- [Consequences](#consequences)
- [Decision Permanence](#decision-permanence)
- [Alternatives Considered](#alternatives-considered)

## Context

### Background

**Problem Statement:**

The current egg setup system has several issues:

1. **Complex bash script:** `setup.sh` is ~1097 lines of bash, making it hard to maintain, test, and extend
2. **Scattered configuration:**
   - `~/.config/egg/secrets.env` - secrets (partially implemented)
   - `~/.config/egg/config.yaml` - settings (template exists, not enforced)
   - `~/.config/egg/repositories.yaml` - repo config
   - Individual service setup scripts in `host-services/*/setup.sh`
   - Configuration logic also exists in the `egg` Python script
3. **No minimal setup path:** Users must go through the full setup even for basic configuration
4. **No centralized service management:** Enabling/disabling systemd services requires manual commands
5. **Configuration scattered between `egg` script and `setup.sh`:** Some config happens in Python (`egg`), some in bash (`setup.sh`)

**Current Configuration Locations:**

| File | Purpose | Status |
|------|---------|--------|
| `~/.config/egg/secrets.env` | Slack, GitHub, Confluence, JIRA tokens | Template exists, validation in setup.sh |
| `~/.config/egg/config.yaml` | Non-secret settings (channels, intervals) | Template exists, rarely used |
| `~/.config/egg/repositories.yaml` | Repo access configuration | Used by repo_config.py |
| `~/.config/egg/github-app-*` | GitHub App credentials | Set by setup.sh |
| `~/.config/egg/anthropic-api-key` | Anthropic API key | Read by egg script |
| `~/.cache/egg/` | Docker staging (cache) | Auto-managed by egg |

**Desired State:**

1. Single Python setup script replacing bash
2. Two consolidated config files: `secrets.env` and `config.yaml` in `~/.config/egg/`
3. Minimal setup mode for quick configuration
4. Service management flags for systemd services
5. `egg --setup` flag that invokes the Python setup

### What We're Deciding

This ADR establishes a **declarative setup architecture** with:

1. **Python-based setup:** Convert `setup.sh` to `setup.py` for maintainability and testability
2. **Consolidated configuration:** Two files in `~/.config/egg/`: `secrets.env` and `config.yaml`
3. **Minimal setup mode:** Quick configuration of essential settings (repos, bot name, secrets)
4. **Service management:** `--enable-services` and `--disable-services` flags
5. **Unified entry point:** `egg --setup` triggers setup flow, auto-runs if config missing

### Goals

**Primary Goals:**
1. **Consolidate configuration:** All settings in `~/.config/egg/{secrets.env,config.yaml}`
2. **Python-first:** Replace bash with Python for maintainability
3. **Minimal setup:** Default mode prompts only for essential configuration
4. **Service management:** Easy enable/disable of all systemd services
5. **Backward compatibility:** Support existing configurations during transition

**Non-Goals:**
- Changing the container architecture
- Modifying systemd service definitions themselves
- Changing the GitHub App authentication mechanism

## Decision

**We will implement a Python-based setup system with:**

1. **`setup.py`** - Main setup script replacing `setup.sh`
2. **Two config files** - `~/.config/egg/secrets.env` and `~/.config/egg/config.yaml`
3. **Minimal setup mode** (default) - Quick essential configuration
4. **Full setup mode** (`--full`) - Complete setup including optional components
5. **Service management** (`--enable-services`, `--disable-services`)
6. **Integration with `egg`** - `egg --setup` delegates to setup.py

### Configuration File Structure

#### `~/.config/egg/secrets.env`

```bash
# egg Secrets Configuration
# This file contains sensitive credentials - DO NOT COMMIT

# === REQUIRED ===

# Slack Integration (required for notifications)
SLACK_TOKEN="xoxb-..."           # Bot User OAuth Token
SLACK_APP_TOKEN="xapp-..."       # App-Level Token (Socket Mode)

# === OPTIONAL ===

# GitHub (if not using GitHub App)
# GITHUB_TOKEN="ghp_..."

# GitHub read-only (for monitoring external repos)
# GITHUB_READONLY_TOKEN="ghp_..."

# Confluence Integration
# CONFLUENCE_BASE_URL="https://company.atlassian.net/wiki"
# CONFLUENCE_USERNAME="user@example.com"
# CONFLUENCE_API_TOKEN="..."
# CONFLUENCE_SPACE_KEYS="ENG,TEAM"

# JIRA Integration
# JIRA_BASE_URL="https://company.atlassian.net"
# JIRA_USERNAME="user@example.com"
# JIRA_API_TOKEN="..."
# JIRA_JQL_QUERY="project = ENG AND status != Done"
```

#### `~/.config/egg/config.yaml`

```yaml
# egg Configuration
# Non-secret settings - safe to version control (without personal values)

# Bot identity
bot_name: "egg"
github_username: ""              # Your GitHub username

# Repositories
writable_repos:
  - "${github_username}/egg"
readable_repos:
  - "acme/webapp"

# Slack settings
slack_channel: ""                # Your DM channel ID
allowed_users:
  - ""                           # Your Slack user ID

# Sync intervals (minutes)
context_sync_interval: 30
github_sync_interval: 5

# Optional: Confluence/JIRA output directories
confluence_output_dir: "~/context-sync/confluence"
jira_output_dir: "~/context-sync/jira"
```

### CLI Interface

```bash
# Default: Minimal setup (prompts for essentials only)
./setup.py

# Full setup (all components including optional)
./setup.py --full

# Enable all systemd services
./setup.py --enable-services

# Disable all systemd services
./setup.py --disable-services

# Enable/disable individual service
./setup.py --enable slack-notifier
./setup.py --disable context-sync

# Update mode (reload configs, restart services)
./setup.py --update

# Force reinstall
./setup.py --force

# Via egg command
egg --setup                      # Same as ./setup.py
egg --setup --full               # Same as ./setup.py --full
```

### Minimal Setup Flow

When run with default mode, setup.py prompts for:

1. **GitHub username** (for repo configuration)
2. **Bot name** (defaults to "egg")
3. **Slack tokens** (required for core functionality)
4. **GitHub App** or PAT configuration
5. **Writable/readable repositories**

All other settings use sensible defaults. Users can run `--full` later for optional components.

### Service Management

Services are categorized into **core services** (enabled by default) and **LLM-based services** (opt-in):

#### Core Services (Enabled by Default)

These services don't require LLM tokens and are enabled automatically during setup:

| Service | Type | Purpose |
|---------|------|---------|
| slack-notifier.service | Service | Send notifications to Slack |
| slack-receiver.service | Service | Receive messages from Slack |
| gateway.service | Service | Git/GitHub operations + token refresh |
| worktree-watcher.timer | Timer | Clean up orphaned worktrees |

#### LLM-Based Services (Opt-in)

These services require LLM API tokens (Anthropic, OpenAI, etc.) and must be explicitly enabled:

| Service | Type | Purpose | Token Required |
|---------|------|---------|----------------|
| context-sync.timer | Timer | Sync Confluence/JIRA | Anthropic API |
| github-watcher.timer | Timer | Watch GitHub PRs/issues | Anthropic API |
| conversation-analyzer.timer | Timer | Analyze conversations daily | Anthropic API |
| egg-doc-generator.timer | Timer | Generate docs weekly | Anthropic API |
| adr-researcher.timer | Timer | Research ADRs weekly | Anthropic API |

#### Service Management Commands

Users can enable/disable all services, or manage individual services:

```bash
# Enable all services (core + LLM-based)
./setup.py --enable-services

# Disable all services (core + LLM-based)
./setup.py --disable-services

# Enable/disable individual service (works for both core and LLM services)
./setup.py --enable slack-notifier
./setup.py --enable context-sync
./setup.py --disable github-watcher

# Note: Core services are enabled by default during initial setup
# Use --disable to turn off individual core services if needed
```

## Implementation Details

### Implementation Roadmap

**Phase 1: Core Setup Module**
- **Goal:** Create Python setup module with basic functionality
- **Components:**
  - `setup.py` with argument parsing
  - Configuration loading/saving for secrets.env and config.yaml
  - User prompting utilities (colored output, validation)
  - Dependency checking (docker, uv, git, gh)
- **Success criteria:** Can create config files and validate dependencies

**Phase 2: Minimal Setup Flow**
- **Goal:** Implement the default minimal setup experience
- **Components:**
  - GitHub username prompt with gh CLI auto-detection
  - Bot name configuration
  - Slack token validation (xoxb-/xapp- prefixes)
  - GitHub App or PAT configuration with file creation
  - Repository configuration (writable/readable)
- **Success criteria:** New user can complete minimal setup in <5 minutes

**Phase 3: Service Management**
- **Goal:** Implement systemd service enable/disable with core/LLM service distinction
- **Components:**
  - Service categorization (core vs LLM-based services)
  - Core service auto-enable (slack-notifier, slack-receiver, gateway, worktree-watcher)
  - Batch enable/disable with `systemctl --user` for all services
  - Individual service enable/disable support
  - Status reporting (which services are enabled/running)
  - Individual service setup script calls
- **Success criteria:** Core services enabled by default, `--enable-services` starts all services, individual service control works

**Phase 4: Full Setup Mode**
- **Goal:** Complete feature parity with current setup.sh
- **Components:**
  - Docker image build
  - Beads initialization
  - Shared directories creation
  - Context sync validation
  - All optional component setup
- **Success criteria:** Full parity with existing setup.sh functionality

**Phase 5: Integration and Migration** ✓ COMPLETED
- **Goal:** Integrate with egg command and handle migration
- **Components:**
  - ✓ Update `egg` script to delegate to setup.py
  - ✓ Migration from legacy config locations (API keys migrated to secrets.env)
  - ✓ Deprecation warnings for old config paths
  - ✓ **Delete `setup.sh`** - Removed old bash setup script entirely
  - ✓ Documentation updates (README.md updated)
- **Success criteria:** `egg --setup` works, existing configs migrate cleanly, setup.sh is removed
- **Completion date:** December 16, 2025

### File Structure

```
egg/
├── setup.py                     # Main setup script (new)
├── config/
│   ├── setup/                   # Setup module (new)
│   │   ├── __init__.py
│   │   ├── cli.py               # Argument parsing
│   │   ├── config_manager.py    # Config file management
│   │   ├── prompts.py           # User prompts/validation
│   │   ├── services.py          # Systemd service management
│   │   └── docker.py            # Docker image building
│   ├── host_config.py           # Existing config loader
│   ├── secrets.template.env     # Existing template
│   └── host-config.template.yaml # Existing template
└── sandbox/
    └── egg                      # Updated to support --setup
```

### Configuration Manager

```python
# config/setup/config_manager.py
from pathlib import Path
from typing import Any
import yaml
import os

class ConfigManager:
    """Manages egg configuration files."""

    CONFIG_DIR = Path.home() / ".config" / "egg"
    SECRETS_FILE = CONFIG_DIR / "secrets.env"
    CONFIG_FILE = CONFIG_DIR / "config.yaml"

    def __init__(self):
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(self.CONFIG_DIR, 0o700)

    def load_secrets(self) -> dict[str, str]:
        """Load secrets from secrets.env."""
        secrets = {}
        if self.SECRETS_FILE.exists():
            with open(self.SECRETS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        secrets[key.strip()] = value.strip().strip('"\'')
        return secrets

    def save_secrets(self, secrets: dict[str, str]) -> None:
        """Save secrets to secrets.env with proper permissions."""
        lines = ["# egg Secrets Configuration", "# DO NOT COMMIT THIS FILE", ""]
        for key, value in secrets.items():
            lines.append(f'{key}="{value}"')

        with open(self.SECRETS_FILE, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        os.chmod(self.SECRETS_FILE, 0o600)

    def load_config(self) -> dict[str, Any]:
        """Load config from config.yaml."""
        if self.CONFIG_FILE.exists():
            with open(self.CONFIG_FILE) as f:
                return yaml.safe_load(f) or {}
        return {}

    def save_config(self, config: dict[str, Any]) -> None:
        """Save config to config.yaml."""
        with open(self.CONFIG_FILE, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
```

### Service Manager

```python
# config/setup/services.py
import subprocess
from dataclasses import dataclass
from typing import List

@dataclass
class Service:
    name: str
    description: str
    is_core: bool  # Core services are enabled by default

# Core services - enabled by default, no LLM tokens required
CORE_SERVICES = [
    Service("slack-notifier.service", "Slack Notifier", is_core=True),
    Service("slack-receiver.service", "Slack Receiver", is_core=True),
    Service("gateway.service", "Gateway Sidecar", is_core=True),
    Service("worktree-watcher.timer", "Worktree Watcher", is_core=True),
]

# LLM-based services - require API tokens, opt-in only
LLM_SERVICES = [
    Service("context-sync.timer", "Context Sync Timer", is_core=False),
    Service("github-watcher.timer", "GitHub Watcher Timer", is_core=False),
    Service("conversation-analyzer.timer", "Conversation Analyzer", is_core=False),
    Service("egg-doc-generator.timer", "Documentation Generator", is_core=False),
    Service("adr-researcher.timer", "ADR Researcher", is_core=False),
]

ALL_SERVICES = CORE_SERVICES + LLM_SERVICES

class ServiceManager:
    """Manages egg systemd services."""

    def enable_core_services(self) -> None:
        """Enable and start core services (default behavior)."""
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        for service in CORE_SERVICES:
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", service.name],
                check=False  # Don't fail if service missing
            )

    def enable_all(self) -> None:
        """Enable and start all services (core + LLM-based)."""
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        for service in ALL_SERVICES:
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", service.name],
                check=False
            )

    def disable_all(self) -> None:
        """Disable and stop all services."""
        for service in ALL_SERVICES:
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", service.name],
                check=False
            )

    def enable_service(self, service_name: str) -> None:
        """Enable and start a specific service."""
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", service_name],
            check=True
        )

    def disable_service(self, service_name: str) -> None:
        """Disable and stop a specific service."""
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", service_name],
            check=True
        )

    def status(self) -> dict[str, dict]:
        """Get status of all services."""
        status = {}
        for service in ALL_SERVICES:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service.name],
                capture_output=True, text=True
            )
            enabled = subprocess.run(
                ["systemctl", "--user", "is-enabled", service.name],
                capture_output=True, text=True
            )
            status[service.name] = {
                "active": result.stdout.strip() == "active",
                "enabled": enabled.stdout.strip() == "enabled",
                "description": service.description,
                "is_core": service.is_core,
            }
        return status
```

### Minimal Setup Prompts

```python
# config/setup/prompts.py
import subprocess
from typing import Optional

class SetupPrompts:
    """Interactive prompts for minimal setup."""

    def prompt_github_username(self) -> str:
        """Prompt for GitHub username, auto-detect from gh CLI."""
        detected = self._detect_github_username()
        if detected:
            print(f"Detected GitHub username: {detected}")
            if input("Use this username? (y/n): ").lower() == 'y':
                return detected
        return input("Enter your GitHub username: ").strip()

    def _detect_github_username(self) -> Optional[str]:
        """Try to detect username from gh CLI."""
        try:
            result = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        return None

    def prompt_slack_tokens(self) -> dict[str, str]:
        """Prompt for Slack tokens with validation."""
        print("\nSlack Integration")
        print("=" * 40)
        print("Get tokens from: https://api.slack.com/apps")
        print()

        while True:
            bot_token = input("Slack Bot Token (xoxb-...): ").strip()
            if bot_token.startswith("xoxb-"):
                break
            print("Error: Token must start with 'xoxb-'")

        while True:
            app_token = input("Slack App Token (xapp-...): ").strip()
            if app_token.startswith("xapp-"):
                break
            print("Error: Token must start with 'xapp-'")

        return {
            "SLACK_TOKEN": bot_token,
            "SLACK_APP_TOKEN": app_token,
        }

    def prompt_repos(self, github_username: str) -> dict[str, list[str]]:
        """Prompt for repository configuration."""
        print("\nRepository Configuration")
        print("=" * 40)

        default_writable = f"{github_username}/egg"
        print(f"Default writable repo: {default_writable}")

        writable = [default_writable]
        while True:
            more = input("Add more writable repos? (enter repo or blank to skip): ").strip()
            if not more:
                break
            writable.append(more)

        readable = []
        while True:
            repo = input("Add readable repos? (enter repo or blank to skip): ").strip()
            if not repo:
                break
            readable.append(repo)

        return {
            "writable_repos": writable,
            "readable_repos": readable,
        }
```

## Consequences

### Benefits

1. **Maintainability:** Python is easier to test, debug, and extend than bash
2. **Consolidated configuration:** Two files instead of many scattered locations
3. **Quick setup:** Minimal mode gets users started fast
4. **Service management:** Easy enable/disable of all services
5. **Better validation:** Python enables better input validation
6. **Testability:** Can write unit tests for setup logic

### Drawbacks

1. **Migration effort:** Need to migrate existing configurations
2. **Python dependency:** Setup now requires Python (already required for egg)
3. **Breaking change:** Users with custom setup scripts may need updates

### Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| **Config migration fails** | Keep legacy support during transition, provide migration tool |
| **Services don't start** | Validate service files before enabling, provide rollback |
| **User confusion** | Clear documentation, deprecation warnings |

## Decision Permanence

**Medium permanence.**

The configuration file locations and structure are relatively stable, but the specific prompts and defaults can evolve.

**Higher-permanence elements:**
- Two config files: secrets.env and config.yaml
- Location: ~/.config/egg/
- Service management flags

**Lower-permanence elements:**
- Specific prompts and defaults
- Optional component setup
- Validation rules

## Alternatives Considered

### Alternative 1: Keep Bash, Add Flags

**Description:** Keep setup.sh but add --minimal and service flags.

**Pros:**
- Less work
- No Python dependency

**Cons:**
- Bash is harder to test and maintain
- Configuration remains scattered

**Rejected because:** Doesn't address core maintainability issues.

### Alternative 2: Use ansible/terraform

**Description:** Use existing configuration management tools.

**Pros:**
- Proven tools
- Declarative configuration

**Cons:**
- Additional dependencies
- Overkill for single-machine setup
- Learning curve

**Rejected because:** Too heavy for the use case.

### Alternative 3: Interactive TUI

**Description:** Build a full terminal UI with menus.

**Pros:**
- Better UX
- More discoverable

**Cons:**
- More complex to implement
- Dependencies on TUI libraries

**Rejected because:** Added complexity without proportional benefit.

## References

- [Current setup.sh](../../../setup.sh) - Existing bash setup
- [host_config.py](../../../config/host_config.py) - Existing config loader
- [egg script](../../../sandbox/egg) - Current egg launcher

---

**Last Updated:** 2025-12-16

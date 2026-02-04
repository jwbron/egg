# Project Structure Guidelines

This document describes the directory structure conventions for egg.

## Top-Level Structure

```
egg/
├── bin/                    # CLI symlinks (points to actual implementations)
├── config/                 # Central configuration (repos, filters)
├── docs/                   # Cross-cutting documentation
├── host-services/          # Host-side systemd services
├── sandbox/          # Container-side code and config
├── setup.sh                # Master setup script
└── README.md               # Main documentation
```

## Directory Naming Conventions

| Directory | Purpose | Runs On |
|-----------|---------|---------|
| `host-services/slack/` | Slack communication services | Host |
| `host-services/sync/` | Data synchronization services | Host |
| `host-services/analysis/` | Code/conversation analysis and PR tools | Host |
| `host-services/utilities/` | Utility services (cleanup) | Host |
| `sandbox/egg-tasks/` | Scripts called via `egg --exec` from host services | Container (via egg --exec) |
| `sandbox/tools/` | Interactive tools used inside the container | Container |
| `sandbox/.claude/` | Claude Code configuration (rules, commands) | Container |

## Host Service Structure

Services in `host-services/` are organized by category:

```
host-services/
├── slack/                     # Slack communication
│   ├── slack-notifier/        # Outgoing notifications
│   └── slack-receiver/        # Incoming messages
├── sync/                      # Data synchronization
│   └── context-sync/          # Confluence/JIRA sync
└── utilities/                 # Utility services
    ├── egg-logs/              # Log management
    └── service-failure-notify/ # Service failure notifications
```

Each service directory should contain:

```
<service-name>/
├── README.md              # Required: Purpose, setup, usage
├── setup.sh               # Required: Installation script (for systemd services)
├── <service-name>.py      # Main implementation (if Python)
├── <service-name>.sh      # Main implementation (if shell)
├── <service-name>.service # Systemd service unit (if daemon)
├── <service-name>.timer   # Systemd timer (if timer-based)
└── requirements.txt       # Python dependencies (even if empty)
```

### When to Use Subdirectories

Most services should be **flat** (all files at root level). Use subdirectories only when:

1. **Multiple subsystems**: Service has 2+ distinct functional areas (e.g., Confluence connector + JIRA connector)
2. **10+ files**: Service complexity warrants organization
3. **Shared utilities**: Multiple scripts share common code

Example of justified subdirectory usage (`context-sync/`):
```
context-sync/
├── connectors/         # Pluggable data source connectors
│   ├── confluence/     # Confluence-specific code
│   └── jira/           # JIRA-specific code
├── docs/               # Extended documentation (8+ files)
├── utils/              # Shared utilities
├── README.md
├── setup.sh
├── requirements.txt
├── context-sync.service
└── context-sync.timer
```

## Container Task Structure

Tasks in `sandbox/egg-tasks/` are organized by product/service:

```
sandbox/egg-tasks/
├── github/                    # GitHub-related tasks
│   ├── README.md
│   ├── github-processor.py    # Processes GitHub tasks via Slack commands
│   └── command-handler.py     # Routes GitHub commands
├── jira/                      # JIRA-related tasks
│   ├── README.md
│   ├── jira-processor.py      # Analyzes JIRA tickets
│   └── analyze-sprint.py      # Sprint analysis
└── slack/                     # Slack-related tasks
    ├── README.md
    └── incoming-processor.py  # Processes incoming messages
```

These are called via `egg --exec` from host-side systemd services (no background processes).

## File Naming Conventions

| Type | Convention | Example |
|------|------------|---------|
| Python scripts | kebab-case | `conversation-analyzer.py` |
| Shell scripts | kebab-case | `setup.sh`, `manage-scheduler.sh` |
| Systemd files | `<service-name>.service`, `<service-name>.timer` | `slack-notifier.service` |
| Config files | `.yaml` (not `.yml`) | `repositories.yaml` |
| Documentation | UPPERCASE.md for guides, lowercase.md for READMEs | `SCHEDULING.md`, `README.md` |

## Symlink Strategy

The `bin/` directory contains symlinks for convenient CLI access:

```
bin/
├── egg -> ../sandbox/egg
├── setup-slack-notifier -> ../host-services/slack/slack-notifier/setup.sh
├── view-logs -> script for viewing container logs
└── ...
```

**Benefits**:
- Single location for common commands
- Actual code stays with its service
- Easy discovery of available tools

## Configuration Organization

```
config/                        # Global configuration
├── repositories.yaml          # GitHub repo permissions (source of truth)
├── context-filters.yaml       # Content filtering rules
└── repo_config.py             # Python API for repo access

host-services/<service>/       # Service-specific config
└── requirements.txt           # Python dependencies

~/.config/egg/                 # Runtime user config (not in repo)
├── config.yaml                # Non-secret settings (Slack channel, etc.)
└── secrets.env                # Secrets (Slack tokens, API keys)
```

## Documentation Organization

```
docs/
├── README.md                  # Documentation index
├── adr/                       # Architecture Decision Records
├── architecture/              # System design docs
├── development/               # Developer guides (like this file)
├── reference/                 # Quick reference guides
├── setup/                     # Setup instructions
└── user-guide/                # End-user documentation

host-services/<service>/       # Component-specific docs
└── README.md                  # Service documentation

host-services/<service>/docs/  # Extended docs (only if needed)
└── TOPIC.md                   # Detailed topic guides
```

**Rule**: Documentation should live close to code. Only cross-cutting docs belong in the central `docs/` directory.

## Adding a New Host Service

1. Create directory: `host-services/<service-name>/`
2. Add required files:
   - `README.md` - Document purpose and usage
   - `setup.sh` - Installation script
   - `<service-name>.service` - Systemd unit
   - `requirements.txt` - Dependencies (even if empty)
3. Add symlink to `bin/` if user-facing
4. Update main `README.md` architecture section
5. Add to `setup.sh` components array

## Adding a New Container Task

1. Identify the product/service category (github, jira, confluence, slack, or new)
2. Add script to appropriate directory: `sandbox/egg-tasks/<product>/`
3. Add required files:
   - `README.md` - Document purpose and usage (one per product directory)
   - Main implementation (`.py`)
4. **Integrate with Beads** - See [Beads Integration Guide](beads-integration.md)
5. Update the host-side systemd service to call via `egg --exec`
6. Update main `README.md` container components section

## Adding a New Container Tool

1. Add script to `sandbox/tools/`
2. Document in the directory's `README.md`
3. Tools are interactive utilities used inside the container (not called via egg --exec)

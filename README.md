# egg

A structurally enforced SDLC pipeline for autonomous LLM agents — turning GitHub issues into reviewed pull requests with mandatory human gates.

> *Inspired by Andy Weir's short story "The Egg" — a contained environment where development happens before emerging into the world. The agent works inside the egg; when ready, it "hatches" via human review and merge.*

## How It Works

egg takes a GitHub issue through a phased pipeline where the agent cannot skip steps, self-approve work, or bypass review. These constraints are enforced by the gateway infrastructure, not by prompts.

```
         ┌───────────┐     ┌───────────┐     ┌───────────────┐     ┌──────────┐
         │  REFINE   │────▶│   PLAN    │────▶│  IMPLEMENT    │────▶│  HUMAN   │
         │  issue    │     │           │     │  + PR review  │     │  MERGE   │
         └─────┬─────┘     └─────┬─────┘     └───────┬───────┘     └────┬─────┘
               │                 │                   │                  │
               ▼                 ▼                   ▼                  ▼
          Human gate        Human gate         CI + review          GitHub UI
        (approve plan)  (approve tasks)    (draft PR checks)    (final merge)
```

1. **Refine** — Agent analyzes the issue and produces a requirements document. Human approves.
2. **Plan** — Agent breaks work into tasks with acceptance criteria. Human approves before any code is written.
3. **Implement** — Agent creates a draft PR and implements tasks. CI runs, automated review provides line-level feedback. Re-implementation cycles continue until all checks pass.
4. **Merge** — Draft PR is marked ready. Only a human can merge via GitHub UI.

The pipeline state lives in a JSON contract (`.egg-state/contracts/{issue}.json`) committed to the feature branch, giving full auditability of every phase transition.

## The Gateway

The gateway is the enforcement engine. It sits between the agent sandbox and the outside world, validating every operation against the current pipeline phase and role permissions.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                    egg                                      │
│                                                                             │
│   ┌───────────────────────────────┐      ┌───────────────────────────────┐  │
│   │       Gateway Sidecar         │      │      Sandbox Container        │  │
│   │      (Enforcement Engine)     │      │     (Untrusted Agent)         │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │ HTTP │  ┌─────────────────────────┐  │  │
│   │  │ Phase Filter            │◀─┼──────┼──│ git/gh wrappers         │  │  │
│   │  │ (block ops by phase)    │  │      │  │ (intercept all ops)     │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │      │  ┌─────────────────────────┐  │  │
│   │  │ Role Validator          │  │ API  │  │ egg-contract CLI        │  │  │
│   │  │ (enforce field ownership│◀─┼──────┼──│ (state mutations)       │  │  │
│   │  │  for contract mutations)│  │      │  │                         │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │      │  ┌─────────────────────────┐  │  │
│   │  │ Credential Injection    │  │Proxy │  │ Claude Code             │  │  │
│   │  │ (secrets never in       │◀─┼──────┼──│ (ANTHROPIC_BASE_URL)    │  │  │
│   │  │  sandbox)               │  │      │  │                         │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  ┌─────────────────────────┐  │HTTPS │  ┌─────────────────────────┐  │  │
│   │  │ Network Policy          │◀─┼──────┼──│ All outbound traffic    │  │  │
│   │  │ (domain allowlist)      │  │Proxy │  │ (private mode)          │  │  │
│   │  └─────────────────────────┘  │      │  └─────────────────────────┘  │  │
│   │                               │      │                               │  │
│   │  HAS: GitHub tokens,          │      │  HAS: Workspace files only    │  │
│   │  Anthropic keys, network      │      │  NO: Credentials, .git/,      │  │
│   │                               │      │      direct network (private) │  │
│   └───────────────────────────────┘      └───────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key principle**: The agent cannot bypass controls because the capabilities don't exist in its environment. The gateway physically blocks operations — this is infrastructure enforcement, not behavioral controls.

## What's Enforced

The gateway enforces both process controls (SDLC phases) and security controls (credential isolation) as aspects of the same system:

| What | How It's Prevented |
|------|-------------------|
| Agent skips to implementation | Gateway blocks `git push` during refine and plan phases |
| Agent self-approves work | Role-based validation: implementer cannot modify task status |
| Agent merges its own PR | Gateway has no merge endpoint — humans must merge via GitHub UI |
| Agent steals credentials | Credentials never enter sandbox; gateway injects them at request time |
| Agent pushes to main | Gateway enforces branch policies; agent can only push to `egg/*` branches |
| Agent tampers with contracts | File-level restrictions block implementers from modifying contract files via `git push` |
| Agent exfiltrates code | Private mode restricts network to Anthropic API + private GitHub repos only |
| Agent accesses other workspaces | Each agent gets isolated git worktree; `.git/` is shadowed |

### Phase Permissions

Each pipeline phase has a defined set of permitted operations:

| Phase | Allowed Operations | Exit Requires |
|-------|-------------------|---------------|
| **Refine** | `gh issue comment/edit` | Human approval |
| **Plan** | `gh issue comment/edit`, `egg-contract add-decision` | Human approval |
| **Implement** | `git push`, `egg-contract add-commit/update-notes` | All checks pass (CI + PR review) |
| **Merge** | `gh pr edit`, `git push` | Human merge |

### How Isolation Works

- **Zero credential exposure**: Anthropic API requests route through gateway; GitHub operations use wrappers that call gateway API. Container environment is sanitized.
- **Git isolation**: Each agent gets an isolated worktree. The `.git/` directory is shadowed (tmpfs mount) — the agent cannot access git metadata directly.
- **Network modes**: Public mode allows full internet + credential-injected API calls. Private mode restricts to Anthropic API + private GitHub repos only.

## Multi-Agent Orchestration

Implementation workflows use specialized agent roles, each with scoped permissions and focused instructions:

| Role | Responsibility |
|------|----------------|
| **Coder** | Write code, create commits, push branches |
| **Tester** | Run tests, validate acceptance criteria |
| **Documenter** | Update docs, generate changelogs |
| **Integrator** | Coordinate roles, manage PR lifecycle |

Roles are enforced by the gateway — each agent can only perform operations allowed for its role.

## GitHub Automation

egg includes GitHub Actions workflows that run inside the sandbox via a unified work loop:

| Workflow | Description |
|----------|-------------|
| **SDLC Pipeline** | End-to-end issue→PR via unified work loop with structurally enforced review gates |
| **AI Code Review** | Automatic PR reviews via `reusable-review.yml` |
| **@mention Response** | Trigger tasks by mentioning egg in issues or PR comments |
| **Check Autofixer** | Diagnoses and fixes CI failures automatically |
| **Self-Improvement** | Nightly failure analysis with automatic issue creation |
| **Custom Linters** | Project-specific safety checks (container boundaries, invocations, secrets) |

### Triggering the SDLC Pipeline

```bash
# Via label (recommended)
gh issue edit 123 --add-label "sdlc:refine"

# Via workflow dispatch
gh workflow run sdlc-pipeline.yml -f issue_number=123 -f starting_phase=refine
```

The pipeline creates a draft PR automatically when entering the implement phase. Once all checks pass, the PR is marked ready for human review and merge.

### Human-in-the-Loop Decisions

When issues arise, humans interact through checkbox-based UI in GitHub comments:

- **Guidance**: Provide additional context, adjust acceptance criteria, break into subtasks
- **Override**: Mark complete, skip tasks, cancel pipeline
- **Manual**: Complete manually, reassign

A 30-second debounce prevents accidental clicks.

## Quick Start

### Local

```bash
# Clone and install
git clone https://github.com/jwbron/egg.git
cd egg
pip install ./sandbox

# Run egg — auto-setup prompts on first run
egg
```

Running `egg` starts the gateway and sandbox automatically. On first run it will prompt you to configure repositories and credentials via `egg --setup`. By default it launches in public mode (full internet access); use `egg --private` for network-locked private repo mode.

### GitHub Actions (SDLC Pipeline)

1. Install the egg GitHub App or configure workflows in your repository
2. Add the `sdlc:refine` label to an issue
3. The pipeline begins: refine → plan → implement → ready for merge
4. Review and merge the PR via GitHub UI

### Docker Compose (Advanced)

For production deployments or managing the gateway stack separately:

```bash
# Initialize configuration
bin/egg-deploy init

# Edit .env with your credentials (GITHUB_USER_TOKEN, etc.)
vim .env

# Start the gateway
bin/egg-deploy up

# Start a sandbox session against the running gateway
egg --compose
```

See the [Deployment Guide](docs/guides/deployment.md) for full production deployment options.

## GitHub Action

egg can run as a GitHub Action for CI/CD automation:

```yaml
- uses: jwbron/egg@main
  with:
    prompt: "Fix the failing tests"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

See [GitHub Action documentation](action/README.md) for details.

## CLI Reference

### egg CLI

| Command | Description |
|---------|-------------|
| `egg` | Start interactive sandbox session (public mode, auto-setup on first run) |
| `egg --public` | Explicit public mode (full internet access, default) |
| `egg --private` | Private mode (Anthropic API only, network lockdown) |
| `egg --setup` | Run interactive setup wizard |
| `egg --reset` | Reset configuration and start over |
| `egg --exec <cmd>` | Execute command in ephemeral container |
| `egg --compose` | Use Docker Compose for gateway management |
| `egg --compose --down` | Stop the Docker Compose stack |

### egg-deploy CLI

For production/advanced deployments using Docker Compose:

| Command | Description |
|---------|-------------|
| `bin/egg-deploy init` | Initialize configuration files |
| `bin/egg-deploy up` | Start the gateway stack |
| `bin/egg-deploy down` | Stop the gateway stack |
| `bin/egg-deploy status` | Show container status and health |
| `bin/egg-deploy logs` | Follow gateway logs |
| `bin/egg-deploy build` | Rebuild Docker images |

### Flags

| Flag | Description |
|------|-------------|
| `--private` | Enable private mode (Anthropic API + private GitHub repos only) |
| `--public` | Enable public mode (full internet access, default) |
| `--compose` | Use Docker Compose for gateway management |
| `--exec <cmd>` | Execute command in new ephemeral container |
| `--timeout <min>` | Timeout for --exec commands (default: 30) |
| `--auth <method>` | Anthropic auth method for --exec: `oauth-token` (default) or `api-key` |
| `--rebuild` | Force rebuild Docker image |
| `--time` | Show startup timing breakdown for debugging |
| `-v, --verbose` | Show detailed output instead of progress bar |

## Documentation

### SDLC Pipeline

- [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) — Operational guide, CLI commands, triggering
- [ADR: SDLC Pipeline](docs/adr/implemented/ADR-SDLC-Pipeline.md) — Architecture, threat model, security properties

### Architecture

- [Documentation Index](docs/index.md) — Navigation hub for all docs
- [Architecture Overview](docs/architecture/README.md) — System design and components
- [Gateway Sidecar](gateway/README.md) — Policy enforcement, API endpoints, credential injection
- [Sandbox Container](sandbox/README.md) — Agent environment, tools, wrappers

### Architecture Decision Records

- [SDLC Pipeline](docs/adr/implemented/ADR-SDLC-Pipeline.md) — Structurally enforced agent checkpoints
- [Git Isolation](docs/adr/implemented/ADR-Git-Isolation-Architecture.md) — Worktree isolation design
- [Credential Injection](docs/adr/implemented/ADR-Gateway-Credential-Injection.md) — Zero-credential sandbox design
- [All ADRs](docs/adr/README.md) — Complete index

### Component Documentation

- [Shared Libraries](shared/README.md) — Config, logging, and git utilities
- [Configuration](config/README.md) — Repository and host configuration

### Security

- [Workflow Authorization Model](docs/security/authorization-model.md) — Who can trigger automated agents

### Other

- [GitHub Automation Guide](docs/guides/github-automation.md) — Review bots, autofixer, @mention
- [Internet Tool Access Lockdown](docs/adr/in-progress/ADR-Internet-Tool-Access-Lockdown.md) — Public/private mode implementation
- [GitHub Actions ADR](docs/adr/in-progress/ADR-GitHub-Actions-Support.md) — GitHub Actions support design
- [Contributing](CONTRIBUTING.md) — Development setup and workflow
- [Why egg Works](docs/collaboration-effectiveness.md) — Safety, quality, and collaboration

## Versioning

egg uses [semantic versioning](https://semver.org/) for both Docker images and GitHub Action references.

> **Note:** Use `@main` until the first release (v0.1.0) is published, which will create the `@v0` tag.

### Version Pinning

For stability, pin to a major version:
```yaml
uses: jwbron/egg/action@v0  # Receives all v0.x.y updates
```

For full reproducibility:
```yaml
uses: jwbron/egg/action@v0.1.0  # Exact version
```

### Docker Images

```bash
# Latest stable (updated on every release)
docker pull ghcr.io/jwbron/egg-sandbox:latest

# Major version (updated on v0.x.y releases)
docker pull ghcr.io/jwbron/egg-sandbox:v0

# Exact version
docker pull ghcr.io/jwbron/egg-sandbox:v0.1.0
```

### Breaking Changes

- **v0.x.y**: Pre-stable releases. Minor versions may contain breaking changes.
- **v1.x.y and later**: Stable releases. Breaking changes only in major version bumps.

See [RELEASING.md](RELEASING.md) for the release process.

## Development

```bash
make setup           # Set up development environment
make lint            # Run all linters
make test            # Run all tests
make lint-fix        # Auto-fix lint issues
make build           # Build Docker images
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## License

MIT License — see [LICENSE](LICENSE) for details.

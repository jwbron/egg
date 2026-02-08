# egg

A hardened sandbox for autonomous LLM code agents with infrastructure-enforced security controls and a structurally enforced SDLC pipeline.

> *Inspired by Andy Weir's short story "The Egg" - a contained environment where development happens before emerging into the world. The AI agent works inside the egg; when ready, it "hatches" via human review and merge.*

## The Core Principle

**Security and quality through infrastructure, not instructions.**

Behavioral controls (telling an LLM "don't do X") can be bypassed through prompt injection, jailbreaks, or model drift. egg enforces both security and software development process at the infrastructure level — the agent physically cannot perform unauthorized actions or skip verification steps because the capabilities don't exist in its environment.

## How It Works

egg turns a GitHub issue into a reviewed pull request through a structurally enforced pipeline. The agent cannot skip steps, self-approve work, or bypass review — these constraints are enforced by the gateway, not by prompts.

```
         ┌───────────┐     ┌───────────┐     ┌───────────────┐     ┌──────────┐
         │  REFINE   │────▶│   PLAN    │────▶│  IMPLEMENT    │────▶│  HUMAN   │
         │  issue    │     │           │     │  + PR review  │     │  MERGE   │
         └─────┬─────┘     └─────┬─────┘     └───────┬───────┘     └────┬─────┘
               │                 │                   │                  │
               ▼                 ▼                   ▼                  ▼
          Human gate        Human gate         CI + review          GitHub UI
         (approve plan)   (approve tasks)    (draft PR checks)    (final merge)
```

1. **Refine** — Analyze the issue and produce a requirements document.
2. **Plan** — Break work into tasks with acceptance criteria. Human approves before any code is written.
3. **Implement** — Create a draft PR and implement tasks. CI runs and `reusable-review.yml` provides line-level code review. Re-implementation cycles continue until all checks pass.
4. **Merge** — Once checks pass, the draft PR is marked ready. Only a human can merge via GitHub UI.

The pipeline state lives in a JSON contract (`.egg-state/contracts/{issue}.json`) committed to the feature branch, giving full auditability of every phase transition and decision.

See [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) for operational details and [ADR: SDLC Pipeline](docs/adr/implemented/ADR-SDLC-Pipeline.md) for the architecture.

## What Makes egg Robust

### Structurally Enforced Process

| Threat | How egg Prevents It |
|--------|---------------------|
| Agent self-approves work | Implementer cannot modify task status — only the PR review workflow can approve changes |
| Agent skips review | Gateway blocks phase transitions without passing all check runs (CI + PR review) |
| Agent pushes code before plan approval | Gateway blocks `git push` during refine and plan phases |
| Agent creates PR before implementing | Draft PR is created automatically — the pipeline creates it when entering implement phase |

### Infrastructure-Enforced Security

| Threat | How egg Prevents It |
|--------|---------------------|
| Agent steals credentials | Credentials never enter the sandbox — gateway injects them at request time |
| Agent pushes to main | Gateway enforces branch policies; agent can only push to `egg/*` branches |
| Agent merges its own PR | Gateway has no merge endpoint — humans must merge via GitHub UI |
| Agent exfiltrates code | Private mode restricts network to Anthropic API + private GitHub repos only |
| Agent accesses other workspaces | Each agent gets isolated git worktree; `.git/` is shadowed |
| Agent bypasses controls via git config | Sandbox has no git metadata — all git ops go through gateway |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                    egg                                      │
│                                                                             │
│   ┌───────────────────────────┐          ┌───────────────────────────────┐  │
│   │      Gateway Sidecar      │          │      Sandbox Container        │  │
│   │        (Trusted)          │          │        (Untrusted)            │  │
│   │                           │          │                               │  │
│   │  ┌─────────────────────┐  │   HTTP   │  ┌─────────────────────────┐  │  │
│   │  │ Git/GH Policy       │◄─┼──────────┼──│ git/gh wrappers         │  │  │
│   │  │ Engine              │  │   API    │  │ (intercept all ops)     │  │  │
│   │  └─────────────────────┘  │          │  └─────────────────────────┘  │  │
│   │                           │          │                               │  │
│   │  ┌─────────────────────┐  │          │  ┌─────────────────────────┐  │  │
│   │  │ SDLC Pipeline       │  │ Contract │  │ egg-contract CLI        │  │  │
│   │  │ (phase enforcement, │◄─┼──────────┼──│ (state mutations)       │  │  │
│   │  │  role validation)   │  │   API    │  │                         │  │  │
│   │  └─────────────────────┘  │          │  └─────────────────────────┘  │  │
│   │                           │          │                               │  │
│   │  ┌─────────────────────┐  │   API    │  ┌─────────────────────────┐  │  │
│   │  │ Anthropic Proxy     │◄─┼──────────┼──│ Claude Code             │  │  │
│   │  │ (credential inject) │  │  Proxy   │  │ (ANTHROPIC_BASE_URL)    │  │  │
│   │  └─────────────────────┘  │          │  └─────────────────────────┘  │  │
│   │                           │          │                               │  │
│   │  ┌─────────────────────┐  │  HTTPS   │  ┌─────────────────────────┐  │  │
│   │  │ HTTP Proxy (Squid)  │◄─┼──────────┼──│ All outbound traffic    │  │  │
│   │  │ (domain allowlist)  │  │  Proxy   │  │ (private mode)          │  │  │
│   │  └─────────────────────┘  │          │  └─────────────────────────┘  │  │
│   │                           │          │                               │  │
│   │  HAS:                     │          │  HAS:                         │  │
│   │  - GitHub tokens          │          │  - Workspace files only       │  │
│   │  - Anthropic API keys     │          │  - Isolated git worktree      │  │
│   │  - Full network access    │          │                               │  │
│   │                           │          │  NO:                          │  │
│   │                           │          │  - Credentials (any kind)     │  │
│   │                           │          │  - Git metadata (.git/)       │  │
│   │                           │          │  - Direct network (private)   │  │
│   └───────────────────────────┘          └───────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## SDLC Pipeline Details

### Phase-Based Operation Filtering

Each phase has permitted operations enforced by the gateway:

| Phase | Allowed Operations | Exit Requires |
|-------|-------------------|---------------|
| **Refine** | `gh issue comment/edit` | Human approval |
| **Plan** | `gh issue comment/edit`, `egg-contract add-decision` | Human approval |
| **Implement** | `git push`, `egg-contract add-commit/update-notes` | All checks pass (CI + PR review) |
| **Merge** | `gh pr edit`, `git push` | Human merge |

### PR-Based Review

The implement phase uses PR-based automated code review:

1. **Draft PR created** — When entering implement, a draft PR is created automatically
2. **Implementer executes tasks** — The agent commits and pushes to the branch
3. **CI and review checks** — Pipeline waits for all GitHub check runs to complete
4. **Review feedback** — `reusable-review.yml` provides line-level code review comments
5. **Re-implementation cycles** — If checks fail, the implementer is re-invoked with feedback
6. **Ready for merge** — Once all checks pass, the draft PR is marked ready for human merge

This provides line-level review comments visible to humans at every implementation cycle.

### Human-in-the-Loop Decisions

When issues arise, humans interact through checkbox-based UI in GitHub issue comments:

- **Guidance**: Provide additional context, adjust acceptance criteria, break into subtasks
- **Override**: Mark complete, skip tasks, cancel pipeline
- **Manual**: Complete manually, reassign

A 30-second debounce prevents accidental clicks.

### Triggering the Pipeline

```bash
# Via label
gh issue edit 123 --add-label "egg-sdlc"

# Via workflow dispatch
gh workflow run sdlc-pipeline.yml -f issue_number=123 -f starting_phase=refine
```

## Why Use egg?

egg provides a **public, async workflow** for AI-assisted development that makes AI work visible to the whole team. Every interaction happens in GitHub: @mention to assign, review plans before implementation, and full reasoning in workflow logs.

This complements private AI tools rather than replacing them. Use egg when:
- **Team visibility matters**: Code changes that benefit from early feedback and shared context
- **Async execution fits**: Tasks you can hand off and review later
- **Audit trails help**: Work where seeing the reasoning alongside the code is valuable

Private AI tools remain ideal for rapid prototyping, exploratory coding, and individual productivity.

See [Why egg Works: Safety, Quality, and Collaboration](docs/collaboration-effectiveness.md) for the full picture.

## GitHub Automation

egg includes GitHub Actions workflows that automate development tasks — each running
inside the sandbox with full security controls:

| Capability | Description |
|-----------|-------------|
| **SDLC Pipeline** | End-to-end issue→PR workflow with structurally enforced review gates |
| **AI Code Review** | Automatic PR reviews with reusable framework and specialized reviewers |
| **@mention Response** | Trigger tasks by mentioning egg in issues or PR comments |
| **Check Autofixer** | Diagnoses and fixes CI failures on PRs automatically |
| **Self-Improvement** | Nightly failure analysis with automatic issue creation |
| **Custom Linters** | Project-specific safety checks (container boundaries, invocations, secrets) |

Workflows are customizable per-repo via `.egg/review-rules.md` and `.egg/autofixer-rules.md`.

See [GitHub Automation Guide](docs/guides/github-automation.md) for setup and configuration.

## Key Features

### Zero Credential Exposure

The sandbox container **never** has access to credentials:
- **Anthropic API**: Requests route through gateway via `ANTHROPIC_BASE_URL`; gateway injects API key
- **GitHub**: All git/gh commands intercepted by wrappers that call gateway API; gateway injects tokens
- **No environment leakage**: Container environment is sanitized of credential variables

### Git Isolation via Gateway-Managed Worktrees

Each agent session gets a fully isolated workspace:
- **Isolated worktree**: Own branch, own staging area, no visibility into other agents' work
- **Shadowed `.git/`**: Container sees empty `.git/` directory (tmpfs mount) - cannot access git metadata
- **Gateway controls all git state**: Branch creation, commits, pushes all validated by gateway

### Granular Access Control

The gateway enforces fine-grained policies on every operation:
- **Branch ownership**: Agents can only push to branches with configured prefix (default: `egg/`)
- **Protected branches**: Direct pushes to main/master blocked; must go through PR
- **No merge capability**: Gateway has no merge endpoint - humans must review and merge
- **Blocked operations**: Force push, `git config --global`, remote manipulation all blocked

### Public and Private Network Modes

| Mode | Network Access | Use Case |
|------|----------------|----------|
| **Public** | Full internet + credential-injected API calls | Open source work, package installation |
| **Private** | Anthropic API + private GitHub repos only | Confidential code, sensitive data |

In private mode:
- All traffic routes through Squid proxy with strict domain allowlist
- Only private GitHub repositories are accessible (public repos blocked)
- WebSearch, WebFetch tools are blocked
- No package manager access (dependencies pre-installed in image)
- Data exfiltration to arbitrary endpoints is impossible

### Comprehensive Audit Trail

Every operation through the gateway is logged:
- Git operations (status, diff, commit, push)
- GitHub CLI operations (PR create, comment)
- Policy violations (attempted, blocked)
- SDLC pipeline state transitions and phase changes
- Session lifecycle

## Quick Start

```bash
# Clone and set up
git clone https://github.com/YOUR_USERNAME/egg.git
cd egg
make setup

# Configure credentials
cp secrets.yaml.example ~/.config/egg/secrets.yaml
# Edit with your GitHub App / PAT and Anthropic credentials

# Start the sandbox (public mode)
egg start --config egg.yaml

# Start with network lockdown (private mode)
egg start --config egg.yaml --private
```

## GitHub Action

egg can run as a GitHub Action for CI/CD automation:

```yaml
- uses: jwbron/egg@main
  with:
    prompt: "Fix the failing tests"
    anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
```

Trigger egg via @mentions in issues and PRs, or run it on any GitHub Actions event. See the [GitHub Action documentation](action/README.md) and the [GitHub Actions ADR](docs/adr/in-progress/ADR-GitHub-Actions-Support.md) for details.

## CLI Reference

| Command | Description |
|---------|-------------|
| `egg start` | Start the sandbox environment (gateway + container) |
| `egg stop` | Stop the sandbox environment |
| `egg exec <cmd>` | Execute a command inside the sandbox container |
| `egg logs [--follow]` | View container logs |
| `egg status` | Show running containers and health status |
| `egg config validate` | Validate configuration files |

### Flags for `egg start`

| Flag | Description |
|------|-------------|
| `--config <path>` | Path to egg.yaml config file (default: `./egg.yaml`) |
| `--private` | Enable private mode (Anthropic API + private GitHub repos only) |
| `-p`, `--prompt` | Run with a prompt in non-interactive mode (for automation, CI) |

## Documentation

- [Documentation Index](docs/index.md) - Navigation hub for all docs
- [Architecture](docs/architecture/README.md) - System design and component overview
- [Contributing](CONTRIBUTING.md) - Development setup and workflow
- [GitHub Action](action/README.md) - CI/CD integration
- [GitHub Automation](docs/guides/github-automation.md) - Review bots, autofixer, @mention, self-improvement

### SDLC Pipeline

- [SDLC Pipeline Guide](docs/guides/sdlc-pipeline.md) - Operational guide, CLI commands, triggering
- [ADR: SDLC Pipeline](docs/adr/implemented/ADR-SDLC-Pipeline.md) - Architecture, threat model, security properties

### Component Documentation

- [Gateway Sidecar](gateway/README.md) - Policy enforcement, API endpoints, credential injection
- [Sandbox Container](sandbox/README.md) - Agent environment, tools, wrappers
- [Shared Libraries](shared/README.md) - Config, logging, and git utilities
- [Configuration](config/README.md) - Repository and host configuration

### Architecture Decision Records

- [SDLC Pipeline](docs/adr/implemented/ADR-SDLC-Pipeline.md) - Structurally enforced agent checkpoints
- [Git Isolation Architecture](docs/adr/implemented/ADR-Git-Isolation-Architecture.md) - Worktree isolation design
- [Credential Injection](docs/adr/implemented/ADR-Gateway-Credential-Injection.md) - Zero-credential sandbox design
- [Internet Tool Access Lockdown](docs/adr/in-progress/ADR-Internet-Tool-Access-Lockdown.md) - Public/private mode implementation
- [All ADRs](docs/adr/README.md) - Complete index

## Development

```bash
make setup           # Set up development environment
make lint            # Run all linters (via act)
make test            # Run all tests (via act)
make security        # Run security scan (via act)
make ci              # Run full CI pipeline (via act)
make lint-fix        # Auto-fix lint issues (native)
make build           # Build Docker images
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## License

MIT License - see [LICENSE](LICENSE) for details.

# Analysis: Enable SDLC workflow on local

> Issue: #437 | Phase: refine

## Problem Statement

The SDLC pipeline currently runs exclusively in GitHub Actions. This creates friction for:
1. **Development and testing** — Changes to the pipeline require commit/push cycles to test
2. **Local-first workflows** — Developers who want to run the full agent loop on their machine
3. **Deployment flexibility** — Users who want to deploy the pipeline on arbitrary servers

The desired outcome is a `bin/egg-sdlc` script that allows users to run the full SDLC workflow locally using a Claude Code session to manage human interactions, while reusing the same workflow files that run in CI (via nektos/act).

## Current Behavior

### CI-Based SDLC Pipeline

The production SDLC pipeline is implemented in GitHub Actions across several workflows:

- **`sdlc-pipeline.yml`** (530+ lines): Main orchestration workflow that handles:
  - Issue initialization and contract creation
  - Phase management (refine → plan → implement → pr)
  - Branch setup and label management
  - Calls `sdlc-work-loop.yml` for actual phase execution

- **`sdlc-work-loop.yml`** (1400+ lines): Unified work/review/respond cycle that:
  - Builds phase-specific prompts via `action/build-sdlc-prompt.sh`
  - Runs the egg agent via the `action/` composite action
  - Runs automated internal review
  - Handles check failures (lint, test, integration)
  - Posts to issues and manages human approval flow

### Existing Local Infrastructure

- **`bin/egg-deploy`**: Manages gateway and sandbox Docker containers locally
- **`docker-compose.yml`**: Defines the gateway service with dual networks
- **`action/entrypoint.sh`**: Orchestrates gateway + sandbox in GitHub Actions
- **`sandbox/egg_lib/orchestration.py`**: Python module for local gateway orchestration

### Key Differences Between CI and Local

| Aspect | CI (GitHub Actions) | Local |
|--------|---------------------|-------|
| Agent execution | Docker containers via `action/` | Already runs in sandbox via `egg-deploy` |
| Human interaction | Issue/PR comments + checkboxes | File-based or interactive terminal |
| State management | GitHub API + contract commits | Local files + git |
| Check execution | Separate workflow jobs | Local `make lint`, `make test` |
| Gateway | Started by `entrypoint.sh` | Started by `egg-deploy` |

## Constraints

### Technical Constraints

- **act limitations**: nektos/act simulates GitHub Actions but has known limitations:
  - Cannot fully replicate GitHub API authentication
  - Some actions may not work identically
  - Service containers behave differently
  - Secrets must be provided differently

- **Gateway dependency**: The sandbox requires the gateway for git operations, credential injection, and network filtering

- **Docker requirement**: act runs workflows in Docker containers, requiring Docker to be installed

- **Prompt building**: `action/build-sdlc-prompt.sh` expects `GITHUB_OUTPUT` and GitHub API access

### Business Constraints

- Must reuse existing workflow files to avoid divergent implementations
- Should work with minimal configuration for basic use cases
- Should support the same phases and quality gates as CI

### Dependencies

- Docker and Docker Compose
- nektos/act
- Anthropic API credentials
- Local repository clone

## Options Considered

### Option A: Direct act Invocation Wrapper

**Approach**: Create `bin/egg-sdlc` as a thin wrapper that invokes `act` with the appropriate workflow file, passing local secrets and configuration.

```bash
# Example usage:
bin/egg-sdlc --issue 123 --phase refine
# Internally runs:
act workflow_dispatch -W .github/workflows/sdlc-pipeline.yml \
  -e event.json --secret-file .secrets
```

**Pros**:
- Maximum reuse of existing workflow files
- Changes to CI workflows automatically apply locally
- Minimal new code to maintain
- act handles most orchestration

**Cons**:
- act may not support all GitHub Actions features used by the workflows
- Human interaction (HITL) requires external mechanism
- Complex workflows may fail in act due to missing GitHub API context
- Debugging failures is harder (logs buried in act output)
- Slow startup for each phase (Docker container boot per job)

### Option B: Native Local Orchestrator (Separate Implementation)

**Approach**: Build a purpose-built local orchestrator in Python that implements the same phases and logic as the CI workflow, but designed for local execution.

```bash
# Example usage:
bin/egg-sdlc --issue 123
# Runs a Python orchestrator that:
# - Reads issue from GitHub API
# - Runs each phase using existing prompt builders
# - Uses Claude Code session for agent execution
# - Manages local state and contracts
```

**Pros**:
- Full control over local execution environment
- Can implement interactive HITL natively
- Faster iteration (no Docker startup per phase)
- Better debugging and logging
- Can share code with existing `egg_lib` modules

**Cons**:
- Divergent implementation from CI workflows
- More code to maintain
- May drift out of sync with CI behavior
- Doesn't reuse workflow files as requested in issue

### Option C: Hybrid Approach (Recommended)

**Approach**: Create a local orchestrator that reuses the *components* of the CI workflow (prompt builders, check scripts, contract management) without requiring act. The orchestrator:

1. Uses `action/build-sdlc-prompt.sh` to build phase prompts
2. Uses the existing Claude Code session (via `cc` CLI) to run agent
3. Uses local check scripts for lint/test validation
4. Uses file-based HITL (human edits a local file to approve/provide feedback)
5. Manages the contract and git operations directly

```bash
# Example usage:
bin/egg-sdlc --issue 123 [--phase refine]
# - Creates a Claude Code session
# - Runs refine phase, writes draft to .egg-state/drafts/
# - Opens analysis for human review
# - On approval, advances to plan phase
# - Continues until PR is ready
```

**Integration with Claude Code session:**
- The wrapper script sets up the environment and Claude rules
- A `.clauderc` or `CLAUDE.md` is dynamically generated with SDLC context
- Claude Code manages the interactive loop with the human
- Phase transitions are triggered by human commands or file edits

**Pros**:
- Reuses existing components (prompt builders, check scripts, contract models)
- Fast iteration without Docker overhead per phase
- Interactive HITL via Claude Code's native interface
- Can use act for specific workflow validation when needed
- Best developer experience for local-first workflows

**Cons**:
- Doesn't use act directly for workflow execution
- Requires building orchestration logic (but simpler than Option B)
- Some testing/validation of "act compatibility" would need separate effort

## Recommended Approach

**Option C: Hybrid Approach** is recommended for the following reasons:

1. **Developer experience**: Running phases interactively in a Claude Code session provides immediate feedback and natural HITL

2. **Component reuse**: The prompt builders, check scripts, and contract management code are independent of GitHub Actions and can be called directly

3. **act as validation tool**: Rather than using act for primary execution, it can be used to validate that local changes to workflows work correctly before pushing

4. **Pragmatic scope**: This approach delivers value quickly while leaving room to add full act-based execution later if needed

### Implementation Sketch

```
bin/
  egg-sdlc               # Main entry point
  egg-sdlc-agent.sh      # Internal: runs agent for a single phase

.egg-local/
  config.yaml            # Local SDLC configuration
  session/               # Claude Code session state

CLAUDE.md additions:
  - SDLC phase context and commands
  - /sdlc-status command to show current phase
  - /sdlc-approve command to advance phase
  - /sdlc-feedback command to provide input
```

The wrapper script:
1. Validates prerequisites (gateway running, repo configured)
2. Creates/loads SDLC session state
3. Starts Claude Code with SDLC-aware rules
4. Claude Code manages the phase loop interactively

### Challenging the Requirements

The issue suggests using act to run the same workflow files. After analysis, I believe:

1. **act is valuable for CI/CD testing**, not local development workflows
2. **Interactive HITL is better** than file-based when running locally
3. **Component reuse** (prompt builders, checks) achieves the spirit of "same workflow files"

If strict act-based execution is required, Option A is feasible but will require significant work to handle HITL and may provide a suboptimal developer experience.

## Open Questions

The following questions would help refine the implementation:

- What is the primary use case: testing workflow changes, or running SDLC as a daily development tool?
- Should the local workflow support all HITL decision types, or is simple approve/reject sufficient?
- Is there a preference for how human interaction works (file-based, CLI prompts, or Claude Code session)?
- Should the local workflow create PRs on GitHub, or just prepare them locally?
- What level of act compatibility is required—full workflow execution or just validation?

---

*Authored-by: egg*

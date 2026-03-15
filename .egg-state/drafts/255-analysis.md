# Analysis: Update all SDLC related workflows to be reusable

> Issue: #255 | Phase: refine

## Problem Statement

The egg repository contains a comprehensive SDLC pipeline system with multiple GitHub workflows that automate code review, merge conflict resolution, check failure autofixing, and human-in-the-loop decision handling. Currently, these workflows are tightly coupled to the `jwbron/egg` repository and cannot be easily reused by other repositories that want to adopt the same automated development workflow.

**Current state:** All SDLC workflows contain hardcoded values (bot usernames, authorized users, action references, workflow file names) that assume the workflows run in the `jwbron/egg` repository.

**Desired outcome:** Each SDLC-related workflow should be configurable as either a reusable workflow (using `workflow_call`) or the workflows should be refactored to call the existing composite action (`action/action.yml`), allowing any repository to adopt the egg SDLC pipeline with their own bot identities and configurations.

## Current Behavior

### Workflows Identified for Reusability

| Workflow | Purpose | Lines | Hardcoded Values |
|----------|---------|-------|------------------|
| `sdlc-pipeline.yml` | Core SDLC orchestrator (init, implement, PR phases) | 2436 | `james-in-a-box`, `jwbron/egg/action@main`, `jwbron` |
| `sdlc-hitl.yml` | Human-in-the-loop decision handler | 652 | `james-in-a-box`, `jwbron` |
| `reusable-review.yml` | Generic review bot template | 411 | `james-in-a-box`, `jwbron/egg/action@main` |
| `on-pull-request.yml` | Code review trigger | 30 | Workflow reference to reusable-review |
| `on-review-feedback.yml` | Feedback addressing loop | 323 | `james-in-a-box` |
| `on-check-failure.yml` | Autofix CI failures | 175 | `james-in-a-box`, `jwbron/egg/action@main` |
| `on-mention.yml` | @mention responder | 211 | `james-in-a-box`, `@egg`, `jwbron`, `jwbron/egg/action@main` |
| `on-merge-conflict.yml` | Conflict resolver | 408 | `james-in-a-box`, `jwbron/egg/action@main` |
| `on-issue-closed.yml` | SDLC cleanup | 220 | `egg/issue-` branch pattern |
| `on-push-doc-updater.yml` | Documentation automation | 106 | `jwbron/egg/action@main` |
| `on-pull-request-agent-mode-design.yml` | Design review | 40 | Workflow reference |
| `on-pull-request-contract-verify.yml` | Contract verification | 86 | Workflow reference, `egg/issue-` pattern |
| `self-improvement.yml` | System health monitoring | 296 | `jwbron/egg/action@main` |

### Workflows NOT Requiring Reusability (Repo-Specific)

- `test.yml`, `lint.yml`, `test-integration.yml`, `test-e2e.yml` - egg's internal test suite
- `release-images.yml` - Docker image release to GHCR
- `test-action.yml` - Tests the action itself

### Key Hardcoded Values Found

1. **Bot username:** `james-in-a-box` (used 40+ times across workflows)
2. **Authorized user:** `jwbron` (authorization checks in HITL, mention workflows)
3. **Action reference:** `jwbron/egg/action@main` (used 13 times)
4. **Branch prefix:** `egg/` (used in branch patterns like `egg/issue-{N}`)
5. **Bot mention patterns:** `@james-in-a-box`, `@egg`
6. **Workflow file names:** `sdlc-pipeline.yml`, `sdlc-hitl.yml` (for dispatching)

### Existing Reusability Pattern

`reusable-review.yml` already uses `workflow_call` and demonstrates the pattern:
- Accepts `pr_number`, `bot_name`, `prompt_script`, `timeout` as inputs
- Accepts secrets via `secrets:` block
- But still hardcodes `BOT_USERNAME: james-in-a-box` internally

## Constraints

- **Technical constraints:**
  - GitHub Actions limits `workflow_call` to a single level (no nested reusable workflows)
  - Secrets must be explicitly passed to reusable workflows
  - Environment variables in `if:` conditions at job level cannot reference `env:` (must be hardcoded or use inputs)
  - Cross-repository workflow calls require the workflows to be in a public repo or the same organization

- **Backward compatibility:**
  - Must maintain functionality for existing `jwbron/egg` users during transition
  - The `action/action.yml` composite action is the core execution engine and should remain stable

- **Dependencies:**
  - All workflows depend on the `action/action.yml` composite action
  - Prompt builder scripts (`action/build-*.sh`) are specific to each workflow's purpose
  - The gateway sidecar and Docker images are separate infrastructure concerns

## Options Considered

### Option A: Convert Each Workflow to Reusable Workflow

**Approach:** Convert each SDLC workflow to use `workflow_call` triggers, accepting all hardcoded values as inputs with defaults matching current behavior. Consuming repos create thin wrapper workflows that call these reusable workflows.

**Pros:**
- Most flexible approach - full customization possible
- Clear separation between framework and consumer configuration
- Existing pattern demonstrated by `reusable-review.yml`
- Workflows can still be triggered directly in `jwbron/egg` for backwards compatibility

**Cons:**
- Requires significant refactoring (2500+ lines across 13 workflows)
- Each workflow needs extensive input/secret parameter definitions
- Consuming repos need to create wrapper workflows for each feature they want
- Complex nested dependencies (e.g., `on-pull-request.yml` calls `reusable-review.yml`) need careful handling

### Option B: Centralize Configuration in a Single Workflow Dispatch

**Approach:** Create a single "orchestrator" reusable workflow that accepts all configuration, then dispatches to the internal workflows. Internal workflows read from a configuration file or environment.

**Pros:**
- Single point of configuration for consumers
- Internal workflows remain simpler
- Easier maintenance - one place to update

**Cons:**
- Complex routing logic in orchestrator
- Loses granular control over individual features
- Configuration file approach has security implications (untrusted repos could inject config)
- Harder to debug issues

### Option C: Template Repository + GitHub App Integration

**Approach:** Create a template repository with all workflows configured with placeholder values. Provide a CLI or GitHub App that forks the template and customizes values.

**Pros:**
- Simple for consumers - just fork and configure
- No cross-repo workflow call complexity
- Each consumer owns their workflow copies

**Cons:**
- Updates to the framework don't automatically propagate
- Consumers responsible for keeping workflows in sync
- Potential for drift between implementations
- Duplicates code across many repositories

### Option D: Hybrid - Reusable Core + Thin Wrappers with Defaults

**Approach:**
1. Make the core action (`action/action.yml`) fully parameterized (already mostly done)
2. Convert `reusable-review.yml` and key workflows to accept all config as inputs
3. Provide example wrapper workflows that consuming repos can copy
4. Keep event-triggered workflows as thin wrappers that call reusable workflows
5. Use input defaults matching current `jwbron` setup for backward compatibility

**Structure:**
```
.github/workflows/
  # Reusable (workflow_call)
  reusable-review.yml          # Already exists, enhance with bot-username input
  reusable-implement.yml       # Extract implement logic from sdlc-pipeline
  reusable-conflict-resolve.yml # Extract from on-merge-conflict
  reusable-autofix.yml         # Extract from on-check-failure

  # Event-triggered (thin wrappers for jwbron/egg)
  on-pull-request.yml          # Calls reusable-review with jwbron defaults
  on-check-failure.yml         # Calls reusable-autofix with jwbron defaults
  sdlc-pipeline.yml            # Orchestrator, calls reusable-implement
  ...
```

**Pros:**
- Incremental migration - can convert one workflow at a time
- Backward compatible - jwbron/egg keeps working with defaults
- Consuming repos only need to copy thin wrappers and customize
- Core logic centralized in reusable workflows

**Cons:**
- More workflows overall (reusable + wrappers)
- Still requires some refactoring of large workflows
- Event-triggered wrappers still need customization for bot patterns

## Recommended Approach

**Option D: Hybrid - Reusable Core + Thin Wrappers with Defaults**

This approach provides the best balance of:
1. **Incremental migration** - Can convert workflows one at a time without breaking existing functionality
2. **Backward compatibility** - Default values match current `jwbron` setup
3. **Flexibility** - Consuming repos can customize any aspect via inputs
4. **Maintainability** - Core logic in reusable workflows, thin event-triggered wrappers

### Implementation Priority

1. **Phase 1 - Foundation:**
   - Add `bot-username` input to `reusable-review.yml` (replace hardcoded `james-in-a-box`)
   - Add `action-ref` input for customizable action path (default `jwbron/egg/action@main`)
   - Update `on-pull-request.yml` wrapper

2. **Phase 2 - Core SDLC:**
   - Extract `reusable-implement.yml` from `sdlc-pipeline.yml` implement job
   - Parameterize `sdlc-hitl.yml` authorized users
   - Add workflow inputs for branch prefix pattern

3. **Phase 3 - Automation:**
   - Create `reusable-autofix.yml` from `on-check-failure.yml`
   - Create `reusable-conflict-resolve.yml` from `on-merge-conflict.yml`
   - Parameterize `on-review-feedback.yml`

4. **Phase 4 - Cleanup & Documentation:**
   - Parameterize remaining workflows (`on-mention.yml`, `on-issue-closed.yml`, etc.)
   - Add documentation for consuming repos
   - Create example wrapper workflow templates

### Key Parameters to Add

All reusable workflows should accept these common inputs:

```yaml
inputs:
  bot-username:
    description: 'Bot GitHub username for identity and comment filtering'
    required: false
    type: string
    default: 'james-in-a-box'
  authorized-users:
    description: 'Comma-separated list of users authorized to trigger agent actions'
    required: false
    type: string
    default: 'jwbron'
  action-ref:
    description: 'Reference to egg action (e.g., owner/repo/action@ref)'
    required: false
    type: string
    default: 'jwbron/egg/action@main'
  branch-prefix:
    description: 'Branch prefix for SDLC branches (e.g., egg/issue-{N})'
    required: false
    type: string
    default: 'egg'
```

## Open Questions

1. **Authorization model for consuming repos:** The current workflows restrict HITL and mention responses to `jwbron`. Should consuming repos:
   - Define their own authorized users list? (recommended)
   - Use repository collaborator permissions?
   - Use team membership via GitHub API?

2. **Prompt script customization:** The `action/build-*.sh` scripts contain logic specific to egg's conventions. Should consuming repos:
   - Be expected to fork and modify these scripts?
   - Have override paths configurable via workflow inputs?
   - Use a standard interface with hooks for customization?

3. **SDLC contract state storage:** The `.egg-state/` directory pattern assumes file-based state in the repository. For consuming repos:
   - Is this acceptable? (commits to repo for every phase change)
   - Should there be an option for external state storage?

---

*Authored-by: egg*

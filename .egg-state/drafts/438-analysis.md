# Analysis: Break Down Implementation Step into Multi-Agent Workflows

> Issue: #438 | Phase: refine

## Problem Statement

The current implementation phase in the SDLC pipeline uses a single monolithic agent invocation to handle all implementation work. This approach has limitations:

1. **Context overload**: A single agent must handle coding, testing, documentation, and integration testing within one context window, leading to degraded performance on complex tasks.

2. **No specialization**: The same prompt and context structure is used regardless of whether the task requires writing tests, updating documentation, or implementing features.

3. **First-pass quality**: Complex implementations often require multiple attempts because the agent lacks the focused context that specialized agents would have.

The goal is to decompose the implementation phase into a multi-agent orchestration system where specialized agents handle specific aspects of implementation (coding, testing, documentation), either sequentially or in parallel, to improve first-pass implementation quality.

## Current Behavior

The SDLC pipeline currently operates as follows:

### Single-Agent Implementation Model

The implement phase (`sdlc-work-loop.yml:291-307`) invokes a single agent:

```yaml
- name: Run egg agent
  uses: jwbron/egg/action@main
  with:
    prompt: ${{ steps.prompt.outputs.prompt }}
```

This agent receives a prompt built by `action/build-sdlc-prompt.sh` containing:
- Issue description and requirements
- Plan with tasks and acceptance criteria
- Contract state with review feedback (if any)
- Full codebase access via Claude Code tools

### Existing Workflow Patterns

The codebase already demonstrates multi-job orchestration patterns in `sdlc-work-loop.yml`:

1. **Work phase** (lines 205-350): Single agent executes implementation
2. **Check phases** (lines 354-528): Parallel lint, test, integration checks
3. **Review phase** (lines 602-776): Separate reviewer agent evaluates work
4. **Respond phase** (lines 781-983): Routes based on review verdict

The key insight is that the infrastructure for sequential and parallel agent invocation already exists—it's just not applied to decomposing the implementation work itself.

### Agent Invocation Infrastructure

The agent runner (`sandbox/llm/claude/runner.py`) supports:
- Subprocess execution with configurable timeouts
- Streaming output handling
- Model selection (opus, sonnet aliases)
- Result capture with metadata

This infrastructure can support multiple specialized agent invocations.

## Constraints

### Technical Constraints

1. **Context isolation**: Each agent invocation runs in a fresh context window with no memory of previous runs (enforced by Claude Code's `--print` mode).

2. **State transfer**: All coordination between agents must happen through:
   - Contract JSON in `.egg-state/contracts/`
   - Git commits on the feature branch
   - Structured files (e.g., test plans, documentation drafts)

3. **Gateway enforcement**: The gateway sidecar enforces phase-based operation filtering. All specialized agents must operate within the `implement` phase permissions.

4. **Timeout budget**: The work loop has a configurable timeout (`work_timeout`, default 30 min). Multiple agents must share this budget.

### Design Constraints

1. **Agent mode principles**: Per `docs/guides/agent-mode-design.md`, agents should:
   - Receive objectives, not procedures
   - Have tool access to fetch context themselves
   - Post directly to GitHub rather than returning structured output
   - Use judgment about what to do

2. **Existing patterns**: The solution should leverage existing infrastructure (work loop, checks, contract system) rather than building parallel systems.

3. **Incremental adoption**: The change should not break existing single-agent workflows—multi-agent should be opt-in or automatically activated based on task complexity.

### Operational Constraints

1. **GitHub Actions limits**: Each job has a 6-hour timeout; workflow has 72-hour limit.
2. **API costs**: Each agent invocation incurs API costs; excessive decomposition increases cost without proportional benefit.
3. **Debugging complexity**: More agents means more potential failure points and harder debugging.

## Options Considered

### Option A: Task-Level Agent Delegation

**Approach**: Break down implementation by task from the plan. Each task gets its own agent invocation with focused context.

```
Plan Tasks:
  - task-1-1: Create API endpoint       → Coding Agent
  - task-1-2: Write unit tests          → Test Agent
  - task-1-3: Update documentation      → Documentation Agent
```

**Implementation**:
- Orchestrator reads tasks from contract
- For each task: build task-specific prompt, invoke agent, validate result
- Tasks can run sequentially (dependent) or parallel (independent)

**Pros**:
- Focused context per task
- Leverages existing task tracking in contracts
- Natural fit with plan phase output
- Enables parallel execution for independent tasks

**Cons**:
- Overhead for many small tasks
- Doesn't capture cross-task concerns (integration)
- Requires orchestration logic to manage dependencies
- Task granularity in plans may not match ideal agent boundaries

### Option B: Role-Based Specialized Agents

**Approach**: Define specialized agent "roles" that run in sequence, each handling a specific aspect of implementation.

```
Implementation Pipeline:
  1. Coding Agent      → Writes/modifies code per plan
  2. Test Agent        → Creates/updates tests for changed code
  3. Documentation Agent → Updates docs for new features
  4. Integration Agent → Verifies everything works together
```

**Implementation**:
- Define agent roles with specialized prompts
- Each role runs in sequence, building on previous work
- Use file-based handoffs (e.g., changed files list, test coverage report)
- Contract tracks which roles have completed

**Pros**:
- Clear separation of concerns
- Specialized prompts can include domain-specific guidance
- Easier to add/remove roles
- Natural checkpointing between roles

**Cons**:
- Sequential execution increases total time
- Some tasks don't need all roles
- Context loss between role transitions
- May duplicate file reading across agents

### Option C: Hybrid Orchestrator Model

**Approach**: A lightweight orchestrator agent decides how to decompose work, then delegates to specialized sub-agents.

```
Orchestrator Agent:
  1. Analyzes plan tasks
  2. Groups related tasks
  3. Decides which specialists to invoke
  4. Delegates work with specific objectives
  5. Validates combined output
```

**Implementation**:
- Orchestrator runs first, produces execution plan
- Specialized agents run based on orchestrator's decisions
- Orchestrator runs final validation pass
- Contract tracks orchestration state

**Pros**:
- Adaptive to task complexity
- Can skip unnecessary specialists
- Orchestrator provides coherent overview
- Handles edge cases intelligently

**Cons**:
- Additional agent invocation overhead
- Orchestrator decisions may be wrong
- More complex state management
- Harder to test and debug

### Option D: Phase Check Integration

**Approach**: Extend the existing phase check system to include specialized agent checks that produce/validate artifacts.

```
implement phase checks:
  - check-code: Coding agent implements tasks
  - check-test: Test agent ensures coverage
  - check-docs: Docs agent updates documentation
  - check-integration: Integration agent runs full validation
```

**Implementation**:
- Each check becomes an agent invocation
- Checks run sequentially (can specify dependencies)
- Failed checks trigger focused re-runs
- Leverages existing check infrastructure

**Pros**:
- Minimal new infrastructure
- Integrates with existing retry logic
- Phase configs allow per-issue customization
- Natural extension of current patterns

**Cons**:
- Blurs line between checks (validation) and work (production)
- Check framework designed for validation, not generation
- Would need significant check framework changes
- May not handle parallelism well

## Recommended Approach

**Option B (Role-Based Specialized Agents)** is recommended, with elements from Option A (task awareness) and Option D (phase config integration).

### Rationale

1. **Clear mental model**: Roles map to developer expertise areas (coding, testing, docs). This is intuitive and matches how human teams work.

2. **Leverages existing infrastructure**: The work loop already supports sequential job execution. Adding role-based agents is a configuration change, not an architecture change.

3. **Focused prompts**: Each role gets a specialized prompt with relevant guidance. The coding agent doesn't need testing best practices; the test agent doesn't need documentation standards.

4. **Incremental adoption**: Roles can be enabled/disabled via phase configs. Simple issues use just the coding role; complex features use the full pipeline.

5. **Composable**: Roles are independent—adding a "security review" role later doesn't require changing existing roles.

### Proposed Roles

| Role | Purpose | Runs When | Inputs | Outputs |
|------|---------|-----------|--------|---------|
| **Coder** | Implements features per plan tasks | Always | Plan, contract, codebase | Code changes, commit |
| **Tester** | Writes/updates tests | When code changed | Changed files, existing tests | Test files, commit |
| **Documenter** | Updates docs | When public API changed | Changed files, existing docs | Doc updates, commit |
| **Integrator** | Validates full system | When all other roles complete | Full codebase, test results | Integration report |

### Implementation Sketch

```yaml
# Phase config extension
implement:
  agents:
    - role: coder
      required: true
      prompt_script: action/build-coder-prompt.sh
    - role: tester
      required: false
      condition: "files_changed > 0"
      prompt_script: action/build-tester-prompt.sh
    - role: documenter
      required: false
      condition: "public_api_changed"
      prompt_script: action/build-documenter-prompt.sh
    - role: integrator
      required: true
      depends_on: [coder, tester, documenter]
      prompt_script: action/build-integrator-prompt.sh
```

### State Management

Role completions are tracked in the contract:

```json
{
  "implementation_roles": {
    "coder": { "status": "complete", "commit": "abc1234" },
    "tester": { "status": "in_progress" },
    "documenter": { "status": "pending" },
    "integrator": { "status": "blocked" }
  }
}
```

## Open Questions

### HITL Decisions Required

For questions that require human input before proceeding:

**1. Role Set**: Which specialized agents should be included in the initial implementation?

Options:
- Minimal set: Coder + Tester only (simplest, fastest iteration)
- Standard set: Coder + Tester + Documenter (covers most use cases)
- Full set: Coder + Tester + Documenter + Integrator (most thorough)
- Custom: Let me specify different roles

**2. Execution Model**: Should agents run sequentially or allow parallel execution?

Options:
- Sequential only (simpler, deterministic, easier to debug)
- Parallel where dependencies allow (faster, more complex coordination)
- Configurable per issue (maximum flexibility, more complexity)

**3. Opt-in vs. Default**: Should multi-agent be the default or opt-in?

Options:
- Opt-in via phase config (preserves current behavior as default)
- Default with opt-out (assumes multi-agent is better for most tasks)
- Automatic based on task count (e.g., >3 tasks triggers multi-agent)

### Open-Ended Questions

- What is the expected impact on total implementation time? (Multi-agent adds coordination overhead but may produce better first-pass results.)
- Are there existing repositories or issues that would be good candidates for testing the multi-agent approach?
- Should the roles have access to each other's outputs (e.g., tester sees coder's commit messages) or maintain strict isolation?

---

*Authored-by: egg*

# Plan: Break Down Implementation Step into Multi-Agent Workflows

> Issue: #438 | Phase: plan

## Summary

This plan introduces a multi-agent orchestration system for the implement phase, replacing the current single-agent model with specialized agents (Coder, Tester, Documenter, Integrator) that can run sequentially or in parallel based on dependencies. The goal is to improve first-pass implementation quality by giving each agent focused context and specialized prompts, while enabling parallel execution where possible to handle larger workloads efficiently.

Based on human feedback in the analysis phase, we will implement:
- **Full agent set**: Coder, Tester, Documenter, and Integrator agents
- **Parallel execution**: Agents run in parallel where dependencies allow
- **Default enabled**: Multi-agent is the default behavior (not opt-in)

## Implementation Phases

### Phase 1: Agent Role Definitions and Prompt Infrastructure

**Goal**: Define the specialized agent roles and create the prompt-building infrastructure for each agent type.

**Tasks**:
- [TASK-1-1] Create agent role definitions in shared library — Acceptance: Roles (coder, tester, documenter, integrator) defined with clear responsibilities, file access constraints, and dependency declarations
- [TASK-1-2] Create specialized prompt builders for each agent role — Acceptance: `build-coder-prompt.sh`, `build-tester-prompt.sh`, `build-documenter-prompt.sh`, `build-integrator-prompt.sh` generate focused prompts with role-specific context
- [TASK-1-3] Add role-based file restrictions to gateway — Acceptance: Gateway enforces file path patterns (e.g., tester can only write to test files, documenter only to docs)

**Dependencies**: None (foundational phase)

**Exit criteria**: All agent roles are defined with prompts, file restrictions are enforceable via gateway

### Phase 2: Orchestration Engine

**Goal**: Build the orchestration layer that coordinates agent execution, manages dependencies, and handles parallel dispatch.

**Tasks**:
- [TASK-2-1] Create agent orchestration state model — Acceptance: Contract model extended with `agent_executions` tracking status, dependencies, outputs per agent role
- [TASK-2-2] Implement dependency graph resolver — Acceptance: Given agent definitions, produces execution order with parallelizable groups
- [TASK-2-3] Create orchestrator dispatch logic — Acceptance: Python module that reads contract, determines which agents to run next, handles handoffs
- [TASK-2-4] Add agent execution tracking to contract CLI — Acceptance: `egg-contract agent-status`, `egg-contract agent-complete` commands for agents to report completion

**Dependencies**: Phase 1

**Exit criteria**: Orchestrator can determine execution order, track agent states, and identify parallelizable groups

### Phase 3: Workflow Integration

**Goal**: Integrate multi-agent orchestration into the GitHub Actions workflow, replacing the single-agent work step.

**Tasks**:
- [TASK-3-1] Create `sdlc-agent-orchestrator.yml` reusable workflow — Acceptance: Workflow that runs orchestrator, dispatches agents, collects results
- [TASK-3-2] Update `sdlc-work-loop.yml` to use orchestrator for implement phase — Acceptance: Implement phase delegates to orchestrator workflow, preserves existing behavior for refine/plan phases
- [TASK-3-3] Implement parallel agent dispatch in workflow — Acceptance: Uses GitHub Actions matrix or parallel jobs for independent agents
- [TASK-3-4] Add agent handoff mechanism — Acceptance: Agents write structured output (changed files, test coverage, issues found) to `.egg-state/agent-outputs/`

**Dependencies**: Phase 2

**Exit criteria**: Multi-agent workflow runs end-to-end, agents execute in parallel where allowed, results are collected and passed to next agents

### Phase 4: Specialized Agent Implementations

**Goal**: Implement the specialized behavior for each agent role with focused prompts and constraints.

**Tasks**:
- [TASK-4-1] Implement Coder agent — Acceptance: Reads plan tasks, implements code changes, links commits to tasks, outputs changed files list
- [TASK-4-2] Implement Tester agent — Acceptance: Reads changed files from coder, writes/updates tests, reports coverage gaps, cannot modify non-test code
- [TASK-4-3] Implement Documenter agent — Acceptance: Reads changed files, updates relevant docs (README, API docs, inline comments), cannot modify code/tests
- [TASK-4-4] Implement Integrator agent — Acceptance: Runs full test suite, validates all changes work together, produces integration report

**Dependencies**: Phase 3

**Exit criteria**: All four agents work correctly with proper constraints, produce expected outputs

### Phase 5: Error Handling and Recovery

**Goal**: Implement robust error handling for multi-agent scenarios including partial failures and recovery.

**Tasks**:
- [TASK-5-1] Implement agent failure handling — Acceptance: If one agent fails, workflow pauses that branch, allows others to continue, reports failure clearly
- [TASK-5-2] Add agent retry logic — Acceptance: Failed agents can be retried independently without re-running successful agents
- [TASK-5-3] Create conflict resolution for parallel agents — Acceptance: If parallel agents modify same files, orchestrator detects and handles merge conflicts
- [TASK-5-4] Add escalation for multi-agent failures — Acceptance: Circuit breaker triggers if multiple agents fail repeatedly, escalates to human

**Dependencies**: Phase 4

**Exit criteria**: Multi-agent system gracefully handles failures, supports partial retries, escalates appropriately

### Phase 6: Testing and Documentation

**Goal**: Comprehensive testing of multi-agent system and documentation for users.

**Tasks**:
- [TASK-6-1] Add unit tests for orchestration logic — Acceptance: Dependency resolution, execution ordering, state tracking have full coverage
- [TASK-6-2] Add integration tests for agent workflows — Acceptance: End-to-end tests that verify agent coordination, file restrictions, handoffs
- [TASK-6-3] Update SDLC pipeline documentation — Acceptance: `docs/guides/sdlc-pipeline.md` updated with multi-agent architecture, configuration options
- [TASK-6-4] Create agent development guide — Acceptance: `docs/guides/agent-development.md` explains how to add new specialized agents

**Dependencies**: Phase 5

**Exit criteria**: All tests pass, documentation complete, system ready for production use

## Test Strategy

- **Unit tests**:
  - Dependency graph resolution (various topologies)
  - Agent state transitions
  - File restriction patterns
  - Contract model extensions

- **Integration tests**:
  - End-to-end multi-agent workflow with mock agents
  - Parallel execution timing verification
  - Failure and retry scenarios
  - Handoff data validation

- **Manual testing**:
  - Run multi-agent workflow on this issue (#438) as dogfooding
  - Verify agent outputs are focused and don't overlap inappropriately
  - Test circuit breaker escalation

## Rollback Plan

The multi-agent system is additive to the existing workflow infrastructure. Rollback options:

1. **Configuration-based**: Set `multi_agent_enabled: false` in phase config to revert to single-agent
2. **Workflow-level**: The orchestrator workflow is separate; can revert `sdlc-work-loop.yml` to call single agent directly
3. **Git revert**: All changes are contained in this branch; full revert is straightforward

```bash
# Immediate rollback via config
egg-contract set-config --issue 438 --key multi_agent_enabled --value false

# Workflow rollback
git revert <commit-range>
git push origin egg/issue-438
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Agent coordination complexity increases debug difficulty | Medium | Medium | Structured logging per agent, clear handoff files, agent-level retry |
| Parallel agents create merge conflicts | Medium | Low | Conflict detection before commit, serialization for overlapping files |
| API cost increase from multiple agent invocations | Medium | Low | Coder runs first (bulk work), others are lightweight; monitor usage |
| File restriction bypass via gateway bugs | Low | High | Comprehensive gateway tests, conservative allowlist patterns |
| Timeout budget exceeded with multiple agents | Medium | Medium | Per-agent timeouts with orchestrator budget tracking |

## Migration Notes

**No breaking changes for existing issues.** The multi-agent system activates for new implement phases. Existing issues in progress continue with single-agent until phase restarts.

**Configuration**: Multi-agent is default-enabled. To opt out for specific issues:
```bash
egg-contract set-config --issue <N> --key multi_agent_enabled --value false
```

**Gateway updates**: New file restriction endpoints require gateway version bump. Existing deployments without gateway update fall back to no file restrictions (degraded but functional).

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add multi-agent orchestration for implement phase"
  description: |
    Introduces specialized agents (Coder, Tester, Documenter, Integrator) for the
    implement phase, replacing the single-agent model. Agents run in parallel where
    dependencies allow, improving first-pass quality and enabling larger workloads.

    Closes #438
phases:
  - id: 1
    name: Agent Role Definitions and Prompt Infrastructure
    goal: Define specialized agent roles and create prompt-building infrastructure
    tasks:
      - id: TASK-1-1
        description: Create agent role definitions in shared library
        acceptance: Roles (coder, tester, documenter, integrator) defined with clear responsibilities, file access constraints, and dependency declarations
        files:
          - shared/egg_contracts/agent_roles.py
          - shared/egg_contracts/models.py
      - id: TASK-1-2
        description: Create specialized prompt builders for each agent role
        acceptance: build-coder-prompt.sh, build-tester-prompt.sh, build-documenter-prompt.sh, build-integrator-prompt.sh generate focused prompts
        files:
          - action/build-coder-prompt.sh
          - action/build-tester-prompt.sh
          - action/build-documenter-prompt.sh
          - action/build-integrator-prompt.sh
      - id: TASK-1-3
        description: Add role-based file restrictions to gateway
        acceptance: Gateway enforces file path patterns per agent role
        files:
          - gateway/agent_restrictions.py
          - gateway/phase_filter.py
  - id: 2
    name: Orchestration Engine
    goal: Build orchestration layer for agent coordination and parallel dispatch
    tasks:
      - id: TASK-2-1
        description: Create agent orchestration state model
        acceptance: Contract model extended with agent_executions tracking
        files:
          - shared/egg_contracts/models.py
          - shared/egg_contracts/orchestration.py
          - .egg/schemas/contract.schema.json
      - id: TASK-2-2
        description: Implement dependency graph resolver
        acceptance: Produces execution order with parallelizable groups
        files:
          - shared/egg_contracts/dependency_graph.py
      - id: TASK-2-3
        description: Create orchestrator dispatch logic
        acceptance: Python module that reads contract, determines which agents to run next
        files:
          - shared/egg_contracts/orchestrator.py
      - id: TASK-2-4
        description: Add agent execution tracking to contract CLI
        acceptance: egg-contract agent-status and agent-complete commands work
        files:
          - sandbox/egg_lib/contract_cli.py
  - id: 3
    name: Workflow Integration
    goal: Integrate multi-agent orchestration into GitHub Actions workflow
    tasks:
      - id: TASK-3-1
        description: Create sdlc-agent-orchestrator.yml reusable workflow
        acceptance: Workflow runs orchestrator, dispatches agents, collects results
        files:
          - .github/workflows/sdlc-agent-orchestrator.yml
      - id: TASK-3-2
        description: Update sdlc-work-loop.yml to use orchestrator for implement phase
        acceptance: Implement phase delegates to orchestrator, refine/plan unchanged
        files:
          - .github/workflows/sdlc-work-loop.yml
      - id: TASK-3-3
        description: Implement parallel agent dispatch in workflow
        acceptance: Uses GitHub Actions matrix or parallel jobs for independent agents
        files:
          - .github/workflows/sdlc-agent-orchestrator.yml
      - id: TASK-3-4
        description: Add agent handoff mechanism
        acceptance: Agents write structured output to .egg-state/agent-outputs/
        files:
          - action/agent-handoff.sh
          - shared/egg_contracts/agent_output.py
  - id: 4
    name: Specialized Agent Implementations
    goal: Implement specialized behavior for each agent role
    tasks:
      - id: TASK-4-1
        description: Implement Coder agent
        acceptance: Reads plan tasks, implements code, links commits, outputs changed files
        files:
          - action/build-coder-prompt.sh
          - sandbox/.claude/commands/coder-mode.md
      - id: TASK-4-2
        description: Implement Tester agent
        acceptance: Reads changed files, writes tests, reports coverage, cannot modify non-test code
        files:
          - action/build-tester-prompt.sh
          - sandbox/.claude/commands/tester-mode.md
      - id: TASK-4-3
        description: Implement Documenter agent
        acceptance: Reads changed files, updates docs, cannot modify code/tests
        files:
          - action/build-documenter-prompt.sh
          - sandbox/.claude/commands/documenter-mode.md
      - id: TASK-4-4
        description: Implement Integrator agent
        acceptance: Runs full test suite, validates changes, produces integration report
        files:
          - action/build-integrator-prompt.sh
          - sandbox/.claude/commands/integrator-mode.md
  - id: 5
    name: Error Handling and Recovery
    goal: Implement robust error handling for multi-agent scenarios
    tasks:
      - id: TASK-5-1
        description: Implement agent failure handling
        acceptance: Workflow pauses failed branch, allows others to continue, reports clearly
        files:
          - .github/workflows/sdlc-agent-orchestrator.yml
          - shared/egg_contracts/orchestrator.py
      - id: TASK-5-2
        description: Add agent retry logic
        acceptance: Failed agents can be retried independently
        files:
          - shared/egg_contracts/orchestrator.py
          - .github/workflows/sdlc-agent-orchestrator.yml
      - id: TASK-5-3
        description: Create conflict resolution for parallel agents
        acceptance: Orchestrator detects and handles merge conflicts
        files:
          - shared/egg_contracts/conflict_resolution.py
          - action/agent-handoff.sh
      - id: TASK-5-4
        description: Add escalation for multi-agent failures
        acceptance: Circuit breaker triggers if multiple agents fail repeatedly
        files:
          - shared/egg_contracts/circuit_breaker.py
          - .github/workflows/sdlc-agent-orchestrator.yml
  - id: 6
    name: Testing and Documentation
    goal: Comprehensive testing and documentation
    tasks:
      - id: TASK-6-1
        description: Add unit tests for orchestration logic
        acceptance: Dependency resolution, execution ordering, state tracking have full coverage
        files:
          - shared/egg_contracts/tests/test_orchestration.py
          - shared/egg_contracts/tests/test_dependency_graph.py
      - id: TASK-6-2
        description: Add integration tests for agent workflows
        acceptance: End-to-end tests verify agent coordination, file restrictions, handoffs
        files:
          - tests/integration/test_multi_agent.py
      - id: TASK-6-3
        description: Update SDLC pipeline documentation
        acceptance: docs/guides/sdlc-pipeline.md updated with multi-agent architecture
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-6-4
        description: Create agent development guide
        acceptance: docs/guides/agent-development.md explains how to add new agents
        files:
          - docs/guides/agent-development.md
```

---

*Authored-by: egg*

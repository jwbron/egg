# Plan: Enable SDLC workflow on local

> Issue: #437 | Phase: plan

## Summary

This plan implements a local SDLC orchestrator (`bin/egg-sdlc`) that enables running the full SDLC workflow locally using Claude Code for human interaction. Based on the approved analysis (Option C: Hybrid Approach), the implementation reuses existing components (prompt builders, check scripts, contract management) while providing a native local execution environment that's faster and more interactive than act-based execution.

The system will support all phases (refine, plan, implement, pr) with the same quality gates as CI, using Claude Code sessions for agent execution and human interaction, with optional GitHub PR creation at the end.

## Implementation Phases

### Phase 1: Core Infrastructure

**Goal**: Create the foundational script infrastructure and local configuration system

**Tasks**:
- [TASK-1-1] Create `bin/egg-sdlc` entry point script — Acceptance: Script exists, is executable, displays help when run without arguments, validates prerequisites (Docker, gateway running)
- [TASK-1-2] Create local SDLC configuration directory structure (`.egg-local/`) — Acceptance: Config directory created, YAML config schema documented, default config generated on first run
- [TASK-1-3] Implement prerequisite validation (gateway health, repo state, git branch) — Acceptance: Script fails with clear error if prerequisites not met, provides actionable guidance for fixes
- [TASK-1-4] Add `--help` and argument parsing for issue number, phase, and PR flag — Acceptance: Arguments parsed correctly, validation for required issue number, `--create-pr` flag for PR creation

**Dependencies**: None (foundation phase)

**Exit criteria**: `bin/egg-sdlc --help` works, validates gateway is running, fails gracefully with missing config

### Phase 2: Prompt Building Adapter

**Goal**: Create an adapter that enables `action/build-sdlc-prompt.sh` to run locally without GitHub Actions context

**Tasks**:
- [TASK-2-1] Create `bin/egg-sdlc-prompt` wrapper for local prompt building — Acceptance: Wrapper sets required env vars (GITHUB_REPOSITORY, EGG_ISSUE_NUMBER, EGG_PIPELINE_PHASE), routes output to stdout instead of GITHUB_OUTPUT
- [TASK-2-2] Add fallback for GitHub API calls when running locally — Acceptance: If `gh` not authenticated, reads issue from local cache or prompts for manual input; graceful degradation when offline
- [TASK-2-3] Support local repo path detection for contract/draft reading — Acceptance: EGG_REPO_PATH set correctly, draft and contract files accessible

**Dependencies**: Phase 1 (needs arg parsing for issue number and phase)

**Exit criteria**: `bin/egg-sdlc-prompt --issue 437 --phase refine` outputs a valid prompt to stdout

### Phase 3: Phase Orchestration Loop

**Goal**: Implement the core phase loop that manages phase transitions and human approval

**Tasks**:
- [TASK-3-1] Implement phase state machine (refine → plan → implement → pr) — Acceptance: Current phase read from contract, transitions validated (can't skip phases), state persisted to contract
- [TASK-3-2] Create Claude Code session launcher with SDLC context — Acceptance: Launches `claude` with phase-specific system prompt injected, session runs interactively
- [TASK-3-3] Implement phase completion detection (draft file existence, commit detection) — Acceptance: After agent session ends, detects completion artifacts, reports status
- [TASK-3-4] Add human approval flow for refine/plan phases — Acceptance: After agent completes, opens draft for human review, prompts for approve/revise decision, loops on revision

**Dependencies**: Phase 2 (needs prompt builder for agent context)

**Exit criteria**: Full refine phase can complete with human approval advancing to plan phase

### Phase 4: Quality Checks Integration

**Goal**: Integrate local lint/test/check execution that mirrors CI behavior

**Tasks**:
- [TASK-4-1] Implement local check runner (make lint, make test) — Acceptance: Runs checks using repo's Makefile, captures output, reports pass/fail clearly
- [TASK-4-2] Add check failure handling with agent re-invocation — Acceptance: On check failure, re-launches agent with error context, supports fix cycle
- [TASK-4-3] Create check aggregation for implement phase gate — Acceptance: All checks must pass before prompting for PR creation

**Dependencies**: Phase 3 (needs phase loop for implement phase)

**Exit criteria**: Implement phase runs checks, fails on lint/test failures, allows fixes before completion

### Phase 5: Internal Review System

**Goal**: Implement local automated review that mirrors CI's internal review loop

**Tasks**:
- [TASK-5-1] Create local reviewer invocation using review prompt builder — Acceptance: Runs `action/build-unified-review-prompt.sh` locally, launches reviewer agent
- [TASK-5-2] Parse review verdict and handle approved/needs_revision — Acceptance: Reads `.egg-state/reviews/{issue}-{phase}-review.json`, routes to approval or re-work
- [TASK-5-3] Implement circuit breaker for review cycles — Acceptance: After 3 failed reviews, escalates to human with option to override

**Dependencies**: Phase 3 (needs phase completion detection to trigger review)

**Exit criteria**: Drafts are reviewed before human approval is requested, revision loops work

### Phase 6: PR Creation and GitHub Integration

**Goal**: Enable optional PR creation on GitHub at workflow end

**Tasks**:
- [TASK-6-1] Implement PR creation flow with `gh pr create` — Acceptance: Creates PR with title/description from contract, links to issue, uses proper format
- [TASK-6-2] Add `--create-pr` flag and interactive prompt — Acceptance: Asks user whether to create PR if flag not specified, respects flag if provided
- [TASK-6-3] Handle PR update flow for existing PRs — Acceptance: If PR already exists for branch, updates it instead of creating new one

**Dependencies**: Phase 4 (needs checks to pass before PR creation)

**Exit criteria**: Complete workflow creates/updates a PR on GitHub with proper metadata

### Phase 7: Claude Code Configuration

**Goal**: Create Claude Code rules/commands for SDLC-aware interaction

**Tasks**:
- [TASK-7-1] Create `.claude/rules/sdlc-local.md` with local SDLC context — Acceptance: Rule file explains local SDLC mode, phase restrictions, available commands
- [TASK-7-2] Create `/sdlc-status` command to show current phase and progress — Acceptance: Command reads contract, displays current phase, pending tasks, review status
- [TASK-7-3] Create `/sdlc-approve` command for human phase approval — Acceptance: Command triggers phase transition, updates contract, advances workflow
- [TASK-7-4] Create `/sdlc-feedback` command for providing input on decisions — Acceptance: Command displays pending decisions, allows human to provide answers

**Dependencies**: Phase 3 (needs phase orchestration to respond to commands)

**Exit criteria**: User can interact with SDLC workflow via Claude Code slash commands

## Test Strategy

- **Unit tests**:
  - Argument parsing validation in `bin/egg-sdlc`
  - Phase state machine transitions
  - Check result parsing

- **Integration tests**:
  - Full refine phase with mock agent (using test prompt)
  - Phase transition from refine to plan
  - Check execution and failure handling

- **Manual testing**:
  1. Clone a test repo, run `bin/egg-sdlc --issue 999 --phase refine`
  2. Verify prompt is displayed, agent session starts
  3. Complete refine phase, approve draft, verify plan phase starts
  4. Complete full workflow through PR creation
  5. Test offline mode (no GitHub access) with cached issue

## Rollback Plan

Since this is additive functionality (new script, new config directory), rollback is straightforward:

1. Remove `bin/egg-sdlc` and related helper scripts
2. Remove `.egg-local/` configuration directory
3. Remove `.claude/rules/sdlc-local.md` and related commands
4. No impact on existing CI workflows or production operation

```bash
# Rollback commands
rm -f bin/egg-sdlc bin/egg-sdlc-prompt bin/egg-sdlc-agent
rm -rf .egg-local/
rm -f .claude/rules/sdlc-local.md
rm -f .claude/commands/sdlc-*.md
```

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Prompt builder incompatibility with local execution | Med | Med | Adapter layer abstracts GitHub Actions specifics; test thoroughly before release |
| Claude Code session management complexity | Med | High | Start with simple interactive mode; add session persistence later if needed |
| Contract state corruption from concurrent runs | Low | High | Single-instance lock file prevents concurrent execution on same issue |
| Offline mode gaps (no GitHub API) | Med | Low | Graceful degradation with cached issue data; clear error messages |
| Review loop divergence from CI behavior | Low | Med | Share review prompt builder; add integration tests comparing local vs CI verdicts |

## Migration Notes

This is a new feature with no migration required. Users opt-in by running `bin/egg-sdlc` instead of using GitHub Actions.

**Documentation updates needed:**
- Update `docs/guides/sdlc-pipeline.md` with local execution section
- Add `docs/guides/local-sdlc.md` with setup and usage guide
- Update `README.md` with local SDLC quick start

**Configuration:**
- No changes to `.env` or `repositories.yaml`
- New `.egg-local/config.yaml` for local-specific settings (optional)

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add local SDLC workflow orchestrator (bin/egg-sdlc)"
  description: |
    Implements local SDLC workflow execution as an alternative to GitHub Actions.
    The new `bin/egg-sdlc` script enables running the full refine/plan/implement/pr
    cycle locally using Claude Code for agent execution and human interaction.
    Reuses existing prompt builders and check scripts for consistency with CI.

    Closes #437
phases:
  - id: 1
    name: Core Infrastructure
    goal: Create foundational script and local configuration system
    tasks:
      - id: TASK-1-1
        description: Create bin/egg-sdlc entry point script
        acceptance: Script exists, is executable, displays help, validates prerequisites
        files:
          - bin/egg-sdlc
      - id: TASK-1-2
        description: Create local SDLC configuration directory structure
        acceptance: .egg-local/ created, YAML config schema documented, default config generated
        files:
          - .egg-local/config.yaml
          - docs/guides/local-sdlc.md
      - id: TASK-1-3
        description: Implement prerequisite validation
        acceptance: Script fails with clear error if prerequisites not met
        files:
          - bin/egg-sdlc
      - id: TASK-1-4
        description: Add help and argument parsing
        acceptance: Arguments parsed correctly, validation for required issue number
        files:
          - bin/egg-sdlc
  - id: 2
    name: Prompt Building Adapter
    goal: Enable local execution of action/build-sdlc-prompt.sh
    tasks:
      - id: TASK-2-1
        description: Create bin/egg-sdlc-prompt wrapper for local prompt building
        acceptance: Wrapper sets required env vars, outputs to stdout
        files:
          - bin/egg-sdlc-prompt
      - id: TASK-2-2
        description: Add fallback for GitHub API calls when running locally
        acceptance: Graceful degradation when gh not authenticated or offline
        files:
          - bin/egg-sdlc-prompt
      - id: TASK-2-3
        description: Support local repo path detection
        acceptance: EGG_REPO_PATH set correctly, draft/contract files accessible
        files:
          - bin/egg-sdlc-prompt
  - id: 3
    name: Phase Orchestration Loop
    goal: Implement core phase loop and human approval flow
    tasks:
      - id: TASK-3-1
        description: Implement phase state machine
        acceptance: Current phase read from contract, transitions validated
        files:
          - bin/egg-sdlc
      - id: TASK-3-2
        description: Create Claude Code session launcher with SDLC context
        acceptance: Launches claude with phase-specific system prompt
        files:
          - bin/egg-sdlc
          - bin/egg-sdlc-agent
      - id: TASK-3-3
        description: Implement phase completion detection
        acceptance: Detects completion artifacts, reports status
        files:
          - bin/egg-sdlc
      - id: TASK-3-4
        description: Add human approval flow for refine/plan phases
        acceptance: Opens draft for review, prompts for approve/revise
        files:
          - bin/egg-sdlc
  - id: 4
    name: Quality Checks Integration
    goal: Integrate local lint/test/check execution
    tasks:
      - id: TASK-4-1
        description: Implement local check runner
        acceptance: Runs checks using Makefile, reports pass/fail
        files:
          - bin/egg-sdlc
      - id: TASK-4-2
        description: Add check failure handling with agent re-invocation
        acceptance: On failure, re-launches agent with error context
        files:
          - bin/egg-sdlc
      - id: TASK-4-3
        description: Create check aggregation for implement phase gate
        acceptance: All checks must pass before PR creation
        files:
          - bin/egg-sdlc
  - id: 5
    name: Internal Review System
    goal: Implement local automated review loop
    tasks:
      - id: TASK-5-1
        description: Create local reviewer invocation
        acceptance: Runs review prompt builder, launches reviewer agent
        files:
          - bin/egg-sdlc-review
      - id: TASK-5-2
        description: Parse review verdict and handle outcomes
        acceptance: Reads review JSON, routes to approval or re-work
        files:
          - bin/egg-sdlc
      - id: TASK-5-3
        description: Implement circuit breaker for review cycles
        acceptance: After 3 failed reviews, escalates to human
        files:
          - bin/egg-sdlc
  - id: 6
    name: PR Creation and GitHub Integration
    goal: Enable optional PR creation on GitHub
    tasks:
      - id: TASK-6-1
        description: Implement PR creation flow
        acceptance: Creates PR with title/description from contract, links to issue
        files:
          - bin/egg-sdlc
      - id: TASK-6-2
        description: Add --create-pr flag and interactive prompt
        acceptance: Asks user whether to create PR if flag not specified
        files:
          - bin/egg-sdlc
      - id: TASK-6-3
        description: Handle PR update flow for existing PRs
        acceptance: Updates existing PR instead of creating new one
        files:
          - bin/egg-sdlc
  - id: 7
    name: Claude Code Configuration
    goal: Create Claude Code rules/commands for SDLC interaction
    tasks:
      - id: TASK-7-1
        description: Create .claude/rules/sdlc-local.md
        acceptance: Rule file explains local SDLC mode and restrictions
        files:
          - .claude/rules/sdlc-local.md
      - id: TASK-7-2
        description: Create /sdlc-status command
        acceptance: Command reads contract, displays current phase and progress
        files:
          - .claude/commands/sdlc-status.md
      - id: TASK-7-3
        description: Create /sdlc-approve command
        acceptance: Command triggers phase transition, updates contract
        files:
          - .claude/commands/sdlc-approve.md
      - id: TASK-7-4
        description: Create /sdlc-feedback command
        acceptance: Command displays decisions, allows human input
        files:
          - .claude/commands/sdlc-feedback.md
```

---

*Authored-by: egg*

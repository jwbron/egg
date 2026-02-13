# Plan: Documentation Onboarding

> Pipeline: local-47601d1d | Phase: plan

## Summary

Generate comprehensive documentation for the egg repository by filling identified gaps rather than rewriting existing high-quality docs. The analysis found 3 missing component READMEs and 3 existing docs needing updates. All existing documentation (65 files) is current and accurate — the strategy is incremental enhancement: create missing READMEs, update the navigation index, refresh the directory structure reference, and extend the architecture overview.

## Implementation Phases

### Phase 1: Create Missing Component READMEs

**Goal**: Fill documentation gaps by creating READMEs for the three components that lack them.

**Tasks**:
- [TASK-1-1] Create `orchestrator/README.md` — Acceptance: Documents purpose, key modules (models.py, state_store.py, container_spawner.py, decision_queue.py), REST API endpoints (/api/v1/pipelines, /containers, /phases, /decisions, /signals, /metrics), configuration (env vars, Docker volumes), development/testing instructions, and component dependencies
- [TASK-1-2] Create `integration_tests/README.md` — Acceptance: Documents purpose, prerequisites (Docker required, API keys for E2E), how to run tests (make test-integration, make test-e2e, make test-security), test categories (local_pipeline/, sdlc/), key fixtures (EggStack, gateway_session), pytest markers (integration, functional, e2e, security), and how to write new tests
- [TASK-1-3] Create `scripts/README.md` — Acceptance: Documents purpose (CI quality/security checks), lists all 10 scripts with descriptions, explains architectural boundary enforcement pattern, shows how to run scripts locally and add new checks

**Dependencies**: Codebase survey (completed in analysis phase)

**Exit criteria**: All three READMEs exist, contain accurate content based on actual source files, and follow the existing documentation style (concise, table-heavy, code-block-rich)

### Phase 2: Update Existing Documentation

**Goal**: Ensure index, structure reference, and architecture docs reflect the full codebase including new READMEs.

**Tasks**:
- [TASK-2-1] Update `docs/index.md` — Acceptance: Orchestrator, integration_tests, and scripts added to Component Documentation table; "Running Tests" added to Task-Specific Guides; all links verified to resolve to real files; no broken references
- [TASK-2-2] Update `docs/development/STRUCTURE.md` — Acceptance: orchestrator/ description matches current modules; metrics/ directory added; integration_tests/ structure verified current; scripts/ listing is complete and accurate
- [TASK-2-3] Update `docs/architecture/README.md` — Acceptance: Orchestrator section added covering pipeline state management, multi-agent wave execution, container spawning, HITL decision queue, and SSE streaming; cross-references docs/architecture/orchestrator.md for deployment modes

**Dependencies**: Phase 1 (new READMEs must exist before index references them)

**Exit criteria**: All updated docs accurately reflect the repository state; every link in index.md resolves to a real file; STRUCTURE.md matches actual directory layout

### Phase 3: Cross-Reference Validation and PR

**Goal**: Verify documentation integrity, commit changes, and create PR.

**Tasks**:
- [TASK-3-1] Validate all links in docs/index.md point to real files — Acceptance: Zero broken links; every documentation file reachable from index.md
- [TASK-3-2] Verify STRUCTURE.md matches actual directory layout — Acceptance: Every top-level directory documented; no stale entries
- [TASK-3-3] Commit all documentation changes on `egg/onboarding-docs` branch — Acceptance: Clean commit with only documentation files; no source code changes
- [TASK-3-4] Create PR with title "docs: Add comprehensive documentation [doc-updater]" — Acceptance: PR body lists all files created/updated; base branch is main

**Dependencies**: Phase 2

**Exit criteria**: PR created with all documentation changes; all validation checks pass

## Test Strategy

- **Unit tests**: None — documentation-only changes
- **Integration tests**: None — no code changes
- **Manual testing**:
  - Verify all markdown files render correctly
  - Click every link in docs/index.md to confirm it resolves
  - Compare STRUCTURE.md directory listings against actual `ls` output
  - Verify new READMEs accurately describe their component's source files
  - Run `make lint` to check for any YAML/markdown issues in CI

## Rollback Plan

Documentation-only changes with minimal risk. Rollback is straightforward:
- Revert the PR branch: `git revert HEAD` on the `egg/onboarding-docs` branch
- Or close the PR without merging — no production impact

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Documentation becomes stale after code changes | Medium | Low | doc-updater workflow handles incremental updates automatically |
| Inaccurate API documentation | Low | Medium | All API endpoints verified against actual gateway.py and orchestrator route files |
| Breaking existing documentation links | Low | High | Phase 3 cross-reference validation catches broken links before PR |
| Over-documenting implementation details | Low | Low | Focus on interfaces, config, and getting-started patterns; skip internal logic |

## Migration Notes

Not applicable — documentation-only changes with no database, config, or breaking changes.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "docs: Add comprehensive documentation [doc-updater]"
  description: |
    Fill documentation gaps identified during full codebase survey. Creates 3 new
    component READMEs (orchestrator, integration_tests, scripts) and updates 3
    existing docs (index.md, STRUCTURE.md, architecture README) to ensure complete
    coverage. All existing documentation is preserved and linked from the index.
phases:
  - id: 1
    name: Create Missing Component READMEs
    goal: Fill documentation gaps by creating READMEs for components that lack them
    tasks:
      - id: TASK-1-1
        description: Create orchestrator/README.md covering purpose, key modules, REST API endpoints, configuration, development instructions, and dependencies
        acceptance: README documents all orchestrator modules, API routes, env vars, and development workflow
        files:
          - orchestrator/README.md
      - id: TASK-1-2
        description: Create integration_tests/README.md covering purpose, prerequisites, how to run tests, test categories, fixtures, and writing new tests
        acceptance: README explains all test categories, markers, running instructions, and fixture patterns
        files:
          - integration_tests/README.md
      - id: TASK-1-3
        description: Create scripts/README.md covering purpose, script index with descriptions, architectural boundary enforcement, and adding new checks
        acceptance: README lists all 10 scripts with descriptions and explains the check framework
        files:
          - scripts/README.md
  - id: 2
    name: Update Existing Documentation
    goal: Ensure index, structure, and architecture docs reflect the full codebase
    tasks:
      - id: TASK-2-1
        description: Update docs/index.md to add orchestrator, integration_tests, and scripts to component table; add testing guide to task-specific guides; verify all links
        acceptance: All components referenced in index; zero broken links
        files:
          - docs/index.md
      - id: TASK-2-2
        description: Update docs/development/STRUCTURE.md to add orchestrator modules, metrics directory, and verify all directory listings are current
        acceptance: Every top-level directory documented with accurate descriptions
        files:
          - docs/development/STRUCTURE.md
      - id: TASK-2-3
        description: Update docs/architecture/README.md to add orchestrator section covering pipeline state, multi-agent execution, container spawning, and HITL decisions
        acceptance: Orchestrator architecture documented with cross-references to orchestrator.md
        files:
          - docs/architecture/README.md
  - id: 3
    name: Cross-Reference Validation and PR
    goal: Verify documentation integrity and submit changes
    tasks:
      - id: TASK-3-1
        description: Validate all links in docs/index.md point to real files
        acceptance: Zero broken links; every doc file reachable from index
        files:
          - docs/index.md
      - id: TASK-3-2
        description: Verify STRUCTURE.md matches actual directory layout
        acceptance: Every top-level directory documented; no stale entries
        files:
          - docs/development/STRUCTURE.md
      - id: TASK-3-3
        description: Commit all documentation changes on egg/onboarding-docs branch
        acceptance: Clean commit with only documentation files
        files: []
      - id: TASK-3-4
        description: Create PR with documentation changes
        acceptance: PR created with file list in body; base branch is main
        files: []
```

---

*Authored-by: egg*

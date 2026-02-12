# Plan: Audit and update all markdown documentation

> Issue: none | Phase: plan

## Summary

Audit all 67 markdown files in the egg repository, comparing documented behavior, commands, file paths, architecture descriptions, and code examples against the actual current codebase. Fix outdated content inline — no TODO markers. The work is organized into phases by documentation category, with each phase independently verifiable.

## Implementation Phases

### Phase 1: Core Project Documentation

**Goal**: Ensure root-level docs (README.md, CONTRIBUTING.md, RELEASING.md) accurately reflect the current project structure, commands, and architecture.

**Tasks**:
- [TASK-1-1] Audit `README.md` — verify architecture diagrams, system descriptions, CLI commands, Docker service names, and port numbers match `constants.py`, `docker-compose.yml`, and `Makefile`. Acceptance: All referenced paths, commands, and ports exist and are correct.
- [TASK-1-2] Audit `CONTRIBUTING.md` — verify dev setup instructions, `make` targets, test commands, and linter config match `Makefile` and `pyproject.toml`. Acceptance: A developer following these instructions can set up and contribute.
- [TASK-1-3] Audit `RELEASING.md` — verify release process, versioning scheme, and workflow references match `.github/workflows/release-images.yml` and actual CI. Acceptance: Release instructions match actual workflow.

**Dependencies**: None

**Exit criteria**: All root-level markdown files accurately reflect the codebase.

### Phase 2: Documentation Hub & Navigation

**Goal**: Ensure docs/index.md and docs/README.md link to correct paths and describe the correct structure.

**Tasks**:
- [TASK-2-1] Audit `docs/index.md` — verify all relative links resolve to existing files, component descriptions match reality, and the navigation structure is complete. Acceptance: Every link in index.md resolves to an existing file; no dead links.
- [TASK-2-2] Audit `docs/README.md` — verify overview and links are consistent with index.md. Acceptance: No contradictions with index.md, all links valid.

**Dependencies**: None

**Exit criteria**: All navigation links are valid and descriptions are accurate.

### Phase 3: Architecture & Strategy Documents

**Goal**: Ensure architecture descriptions, component diagrams, and strategy documents match the actual codebase structure.

**Tasks**:
- [TASK-3-1] Audit `docs/architecture/README.md` — verify claims about contract schema paths (`.egg/schemas/`), contract CLI commands (`egg-contract`), plan parser, check system, and workflow file references. Acceptance: Every file path, command, and schema reference verified against codebase.
- [TASK-3-2] Audit `docs/agentic-feedback-loop.md` — verify workflow descriptions, phase names, and tool references match implementation. Acceptance: Described workflows match actual phase transitions and tools.
- [TASK-3-3] Audit `docs/collaboration-effectiveness.md` — verify claims about system capabilities and constraints. Acceptance: No factual inaccuracies about system behavior.
- [TASK-3-4] Audit `docs/hitl-decisions.md` — verify decision workflow, API endpoints, and queue mechanics match implementation. Acceptance: API paths and decision flow match `orchestrator/decision_queue.py` and `gateway/phase_api.py`.

**Dependencies**: None

**Exit criteria**: Architecture docs reflect the real system.

### Phase 4: ADR Documents

**Goal**: Ensure all 13 ADRs accurately describe decisions, implementation status, and referenced file paths.

**Tasks**:
- [TASK-4-1] Audit implemented ADRs (8 files) — verify status labels match reality, referenced paths exist, and described implementations are present. Acceptance: Each ADR's "implemented" status is correct and file references resolve.
- [TASK-4-2] Audit in-progress ADRs (3 files) — verify current progress descriptions match implementation state. Acceptance: Status accurately reflects what's been built vs. what's planned.
- [TASK-4-3] Audit not-implemented ADR (1 file) — confirm it remains not-implemented. Acceptance: Status is accurate.

**Dependencies**: Phase 3 (architecture understanding informs ADR verification)

**Exit criteria**: All ADR statuses and references are accurate.

### Phase 5: Developer Guides

**Goal**: Ensure all guides provide accurate, working instructions.

**Tasks**:
- [TASK-5-1] Audit `docs/guides/local-quickstart.md` — verify setup commands, config file paths (`~/.config/egg/`), environment variables, monitoring endpoints (ports 9848, 9849), and CLI flags. Acceptance: Following the quickstart produces a working setup.
- [TASK-5-2] Audit `docs/guides/deployment.md` and `docs/guides/deploy-migration.md` — verify deployment instructions, Docker commands, and infrastructure references. Acceptance: Deployment paths and commands are valid.
- [TASK-5-3] Audit `docs/guides/agent-development.md` and `docs/guides/agent-mode-design.md` — verify agent constraint levels, tool references, and configuration. Acceptance: Agent development instructions match sandbox implementation.
- [TASK-5-4] Audit `docs/guides/sdlc-pipeline.md` — verify phase names, transitions, contract format, and CLI commands against `shared/egg_contracts/` and `gateway/phase_api.py`. Acceptance: Pipeline documentation matches implementation.
- [TASK-5-5] Audit `docs/guides/github-automation.md` and `docs/guides/reusable-workflows.md` — verify workflow file names, trigger events, and input parameters match `.github/workflows/`. Acceptance: All workflow references are correct.

**Dependencies**: None

**Exit criteria**: All guides provide accurate instructions.

### Phase 6: Development Documentation

**Goal**: Ensure STRUCTURE.md and TEST_COVERAGE_PLAN.md reflect the actual project layout and test infrastructure.

**Tasks**:
- [TASK-6-1] Audit `docs/development/STRUCTURE.md` — verify directory tree, file descriptions, and naming conventions against actual filesystem. Known issue: references `.egg/schemas/` which does not exist. Acceptance: Every directory and file mentioned in STRUCTURE.md exists, and missing ones are removed or corrected.
- [TASK-6-2] Audit `docs/development/TEST_COVERAGE_PLAN.md` — verify test framework references, coverage targets, and test file paths against `pytest.ini` and test directories. Acceptance: Test strategy matches actual infrastructure.

**Dependencies**: None

**Exit criteria**: Development docs match the filesystem.

### Phase 7: Component READMEs

**Goal**: Ensure each component's README accurately describes its purpose, structure, and usage.

**Tasks**:
- [TASK-7-1] Audit `gateway/README.md` and `gateway/tests/README-integration.md` — verify API endpoints, configuration, and test instructions. Acceptance: Gateway documentation matches `gateway.py` and test infrastructure.
- [TASK-7-2] Audit `sandbox/README.md` and `sandbox/.claude/README.md` — verify sandbox structure, Claude Code configuration, and entry points. Acceptance: Documentation matches sandbox directory structure.
- [TASK-7-3] Audit `shared/README.md` and `shared/egg_config/README.md` — verify module descriptions, key constants (ports, IPs), and package structure. Acceptance: Shared library documentation matches implementation.
- [TASK-7-4] Audit `bin/README.md`, `config/README.md`, and `action/README.md` — verify CLI entry points, config format, and GitHub Action inputs/outputs. Acceptance: All references are accurate.

**Dependencies**: None

**Exit criteria**: All component READMEs are accurate.

### Phase 8: Sandbox Claude Code Configuration

**Goal**: Ensure all Claude Code commands and rules files match actual system behavior.

**Tasks**:
- [TASK-8-1] Audit `sandbox/.claude/commands/*.md` (7 files) — verify command descriptions, tool references, and operational instructions match implementation. Acceptance: Commands work as documented.
- [TASK-8-2] Audit `sandbox/.claude/rules/*.md` (7 files) — verify rules about environment, contracts, code standards, etc. match system behavior. Acceptance: Rules reflect actual constraints.

**Dependencies**: Phase 7 (sandbox understanding)

**Exit criteria**: All Claude Code config docs are accurate.

### Phase 9: Action Convention Documents & Internal Docs

**Goal**: Ensure action conventions and internal docs are accurate.

**Tasks**:
- [TASK-9-1] Audit `action/autofixer-conventions.md`, `action/conflict-conventions.md`, `action/review-conventions.md` — verify conventions match workflow implementations. Acceptance: Convention docs match workflow behavior.
- [TASK-9-2] Audit `.egg/contract-rules.md` — verify contract rules match `shared/egg_contracts/` implementation. Acceptance: Rules match validation logic.

**Dependencies**: None

**Exit criteria**: All convention and internal docs are accurate.

### Phase 10: Cross-Document Consistency Check

**Goal**: Ensure no contradictions between documents.

**Tasks**:
- [TASK-10-1] Verify port numbers are consistent across all docs (9848 for gateway, 9849 for orchestrator, 3129 for proxy). Acceptance: No conflicting port references.
- [TASK-10-2] Verify branch naming conventions (`egg/` prefix) are consistently documented. Acceptance: All references agree.
- [TASK-10-3] Verify all inter-document links work — ensure no broken cross-references between docs. Acceptance: Zero broken links.

**Dependencies**: Phases 1-9

**Exit criteria**: All documents are internally consistent.

## Test Strategy

- **Link validation**: For each document, verify all relative links resolve to existing files using file existence checks.
- **Command verification**: For key CLI commands referenced in docs, verify the command exists (e.g., check `egg-contract` is a real entry point, `make test` target exists).
- **Path verification**: For every file path referenced in documentation, confirm the file exists in the codebase.
- **Port/constant verification**: Cross-reference all port numbers and constants against `shared/egg_config/constants.py`.
- **Manual review**: Each document gets a final read-through for coherence and accuracy after edits.

## Rollback Plan

All changes are documentation-only (markdown files). Rollback is straightforward:
- `git diff` shows all changes made
- `git checkout -- <file>` reverts any individual file
- `git stash` or `git reset HEAD~1` reverts the entire commit
- No code changes, no migration needed, no service impact

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Misinterpreting intended vs. actual behavior | Medium | Medium | Cross-reference multiple source files; when ambiguous, preserve existing documentation and note uncertainty |
| Missing a document or section | Low | Low | Systematic file enumeration already complete (67 files identified) |
| Introducing factual errors during edits | Medium | Medium | Verify every edit against source code before applying; preserve document tone and structure |
| Scope creep into code changes | Low | Medium | Strict policy: only modify .md files, never source code |
| Breaking relative links between docs | Medium | Medium | Phase 10 cross-document consistency check catches link issues |

## Migration Notes

Not applicable — documentation-only changes with no code, configuration, or schema modifications.

## Known Issues to Investigate

During initial exploration, these discrepancies were already identified:

1. **`.egg/schemas/` directory does not exist** — referenced in `docs/architecture/README.md` and `docs/development/STRUCTURE.md`. Schema files need to be located or references corrected.
2. **`.github/scripts/checks/` contains Python files** — docs may describe them as shell scripts; verify and correct.
3. **`egg.yaml.example` vs documented config format** — quickstart references `~/.config/egg/` config files; verify consistency with example.
4. **Prior README audit exists** — a previous analysis identified 12 specific issues with README.md (stale Quick Start, missing orchestrator mention, incomplete CLI flags table, etc.). These findings should be incorporated into Phase 1.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Audit and update all markdown documentation"
  description: |
    Systematic audit of all 67 markdown files in the repository, comparing
    documented behavior against the actual codebase. Fixes outdated content
    inline including file paths, CLI commands, port numbers, architecture
    descriptions, and cross-document links.
phases:
  - id: 1
    name: Core Project Documentation
    goal: Root-level docs accurately reflect current project
    tasks:
      - id: TASK-1-1
        description: Audit README.md for architecture, commands, ports
        acceptance: All referenced paths, commands, and ports exist
        files:
          - README.md
      - id: TASK-1-2
        description: Audit CONTRIBUTING.md for dev setup and workflow
        acceptance: Developer can follow instructions successfully
        files:
          - CONTRIBUTING.md
      - id: TASK-1-3
        description: Audit RELEASING.md for release process accuracy
        acceptance: Release instructions match actual workflow
        files:
          - RELEASING.md
  - id: 2
    name: Documentation Hub & Navigation
    goal: Index and overview docs have valid links and descriptions
    tasks:
      - id: TASK-2-1
        description: Audit docs/index.md for link validity and completeness
        acceptance: Every link resolves to existing file
        files:
          - docs/index.md
      - id: TASK-2-2
        description: Audit docs/README.md for consistency with index
        acceptance: No contradictions, all links valid
        files:
          - docs/README.md
  - id: 3
    name: Architecture & Strategy Documents
    goal: Architecture docs match actual codebase structure
    tasks:
      - id: TASK-3-1
        description: Audit architecture README for schemas, CLI, checks
        acceptance: All file paths and commands verified
        files:
          - docs/architecture/README.md
      - id: TASK-3-2
        description: Audit agentic-feedback-loop.md
        acceptance: Workflows match implementation
        files:
          - docs/agentic-feedback-loop.md
      - id: TASK-3-3
        description: Audit collaboration-effectiveness.md
        acceptance: No factual inaccuracies
        files:
          - docs/collaboration-effectiveness.md
      - id: TASK-3-4
        description: Audit hitl-decisions.md
        acceptance: API paths and decision flow match code
        files:
          - docs/hitl-decisions.md
  - id: 4
    name: ADR Documents
    goal: All ADRs have accurate status and references
    tasks:
      - id: TASK-4-1
        description: Audit 8 implemented ADRs
        acceptance: Status and file references correct
        files:
          - docs/adr/implemented/
      - id: TASK-4-2
        description: Audit 3 in-progress ADRs
        acceptance: Progress descriptions match reality
        files:
          - docs/adr/in-progress/
      - id: TASK-4-3
        description: Audit 1 not-implemented ADR
        acceptance: Status is accurate
        files:
          - docs/adr/not-implemented/
  - id: 5
    name: Developer Guides
    goal: All guides provide accurate working instructions
    tasks:
      - id: TASK-5-1
        description: Audit local-quickstart.md
        acceptance: Setup commands and paths verified
        files:
          - docs/guides/local-quickstart.md
      - id: TASK-5-2
        description: Audit deployment and migration guides
        acceptance: Deployment paths and commands valid
        files:
          - docs/guides/deployment.md
          - docs/guides/deploy-migration.md
      - id: TASK-5-3
        description: Audit agent development guides
        acceptance: Instructions match sandbox implementation
        files:
          - docs/guides/agent-development.md
          - docs/guides/agent-mode-design.md
      - id: TASK-5-4
        description: Audit SDLC pipeline guide
        acceptance: Pipeline docs match implementation
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-5-5
        description: Audit GitHub automation and reusable workflow guides
        acceptance: All workflow references correct
        files:
          - docs/guides/github-automation.md
          - docs/guides/reusable-workflows.md
  - id: 6
    name: Development Documentation
    goal: STRUCTURE.md and test plan reflect actual layout
    tasks:
      - id: TASK-6-1
        description: Audit STRUCTURE.md against filesystem
        acceptance: Every referenced directory and file exists
        files:
          - docs/development/STRUCTURE.md
      - id: TASK-6-2
        description: Audit TEST_COVERAGE_PLAN.md
        acceptance: Test strategy matches infrastructure
        files:
          - docs/development/TEST_COVERAGE_PLAN.md
  - id: 7
    name: Component READMEs
    goal: Each component README matches its implementation
    tasks:
      - id: TASK-7-1
        description: Audit gateway READMEs
        acceptance: API endpoints and test docs accurate
        files:
          - gateway/README.md
          - gateway/tests/README-integration.md
      - id: TASK-7-2
        description: Audit sandbox READMEs
        acceptance: Structure and config docs accurate
        files:
          - sandbox/README.md
          - sandbox/.claude/README.md
      - id: TASK-7-3
        description: Audit shared library READMEs
        acceptance: Module docs match implementation
        files:
          - shared/README.md
          - shared/egg_config/README.md
      - id: TASK-7-4
        description: Audit bin, config, and action READMEs
        acceptance: Entry points and config format accurate
        files:
          - bin/README.md
          - config/README.md
          - action/README.md
  - id: 8
    name: Sandbox Claude Code Configuration
    goal: Claude Code commands and rules match system behavior
    tasks:
      - id: TASK-8-1
        description: Audit 7 command markdown files
        acceptance: Commands work as documented
        files:
          - sandbox/.claude/commands/
      - id: TASK-8-2
        description: Audit 7 rules markdown files
        acceptance: Rules reflect actual constraints
        files:
          - sandbox/.claude/rules/
  - id: 9
    name: Action Conventions & Internal Docs
    goal: Convention docs and internal rules are accurate
    tasks:
      - id: TASK-9-1
        description: Audit 3 action convention documents
        acceptance: Conventions match workflow behavior
        files:
          - action/autofixer-conventions.md
          - action/conflict-conventions.md
          - action/review-conventions.md
      - id: TASK-9-2
        description: Audit contract rules document
        acceptance: Rules match validation logic
        files:
          - .egg/contract-rules.md
  - id: 10
    name: Cross-Document Consistency Check
    goal: No contradictions between documents
    tasks:
      - id: TASK-10-1
        description: Verify port numbers consistent across all docs
        acceptance: No conflicting port references
        files: []
      - id: TASK-10-2
        description: Verify branch naming consistency across docs
        acceptance: All references agree
        files: []
      - id: TASK-10-3
        description: Verify all inter-document links work
        acceptance: Zero broken links
        files: []
```

---

*Authored-by: egg*

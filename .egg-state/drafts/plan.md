# Plan: Audit and update all markdown documentation

> Issue: none | Phase: plan

## Summary

Audit all 67 markdown files in the egg repository, comparing documented behavior, commands, file paths, architecture descriptions, and code examples against the actual current codebase. Fix outdated content inline — no TODO markers. The work is organized into phases by documentation category, with each phase independently verifiable.

The analysis phase identified 24 concrete issues across 11 files, classified as Critical (C1-C4), High (H1-H10), Moderate (M1-M4), and Low (L1-L2). This plan maps every identified issue to a specific task with explicit acceptance criteria. Files verified accurate in the analysis are noted to save implementer time.

## Implementation Phases

### Phase 1: Core Project Documentation

**Goal**: Ensure root-level docs accurately reflect the current project structure, commands, and architecture.

**Tasks**:
- [TASK-1-1] Audit `README.md` — verify architecture diagrams, system descriptions, CLI commands, Docker service names, and port numbers match `constants.py`, `docker-compose.yml`, and `Makefile`. Address **L1** (versioning note may be outdated — line 289 says "Use `@main` until the first release (v0.1.0) is published"; verify whether v0.1.0 has been released and update accordingly). Acceptance: L1 resolved — versioning note reflects actual release status; all other referenced paths, commands, and ports exist and are correct.
- [TASK-1-2] Audit `CONTRIBUTING.md` — verify dev setup instructions, `make` targets, test commands, and linter config match `Makefile` and `pyproject.toml`. **Verified accurate in analysis — confirm no changes since audit.** Acceptance: A developer following these instructions can set up and contribute.
- [TASK-1-3] Audit `RELEASING.md` — verify release process, versioning scheme, and workflow references match `.github/workflows/release-images.yml` and actual CI. **Verified accurate in analysis — confirm no changes since audit.** Acceptance: Release instructions match actual workflow.
- [TASK-1-4] Fix `CHANGELOG.md` — address **C3** (CLI commands don't match actual CLI: lists `egg start`, `egg stop`, `egg exec`, `egg logs`, `egg status`, `egg config validate` but actual CLI is flag-based: `egg`, `egg --exec`, `egg --setup`, `egg --compose --down`; `config validate` is a separate `egg-config validate` tool) and **C4** (claims "43+ test files" but actual count is 153 test files across `tests/`, `gateway/tests/`, and `integration_tests/`). Acceptance: C3 resolved — CLI description updated to `egg, egg --setup, egg --exec, egg --compose` with separate mention of `egg-deploy` and `egg-config`; C4 resolved — test count updated to "150+ test files" or exact count.

**Dependencies**: None

**Exit criteria**: All root-level markdown files accurately reflect the codebase.

### Phase 2: Documentation Hub & Navigation

**Goal**: Ensure docs/index.md and docs/README.md link to correct paths and describe the correct structure.

**Tasks**:
- [TASK-2-1] Audit `docs/index.md` — verify all relative links resolve to existing files, component descriptions match reality, and the navigation structure is complete. **Verified accurate in analysis (all links verified) — confirm no changes since audit.** Acceptance: Every link in index.md resolves to an existing file; no dead links.
- [TASK-2-2] Audit `docs/README.md` — verify overview and links are consistent with index.md. Acceptance: No contradictions with index.md, all links valid.

**Dependencies**: None

**Exit criteria**: All navigation links are valid and descriptions are accurate.

### Phase 3: Architecture & Strategy Documents

**Goal**: Ensure architecture descriptions, component diagrams, and strategy documents match the actual codebase structure.

**Tasks**:
- [TASK-3-1] Audit `docs/architecture/README.md` — verify claims about contract schema paths (`.egg/schemas/`), contract CLI commands (`egg-contract`), plan parser, check system, and workflow file references. Note: "Known Issues to Investigate" item #1 flagged that `.egg/schemas/` directory does not exist — verify and correct this reference during audit. Acceptance: Every file path, command, and schema reference verified against codebase; non-existent `.egg/schemas/` reference corrected.
- [TASK-3-2] Audit `docs/agentic-feedback-loop.md` — verify workflow descriptions, phase names, and tool references match implementation. **Conceptual document — verified accurate in analysis; no code claims to verify.** Acceptance: Described workflows match actual phase transitions and tools.
- [TASK-3-3] Audit `docs/collaboration-effectiveness.md` — verify claims about system capabilities and constraints. **Conceptual document — verified accurate in analysis; no code claims to verify.** Acceptance: No factual inaccuracies about system behavior.
- [TASK-3-4] Audit `docs/hitl-decisions.md` — verify decision workflow, API endpoints, and queue mechanics match implementation. **Verified accurate in analysis.** Acceptance: API paths and decision flow match `orchestrator/decision_queue.py` and `gateway/phase_api.py`.

**Dependencies**: None

**Exit criteria**: Architecture docs reflect the real system.

### Phase 4: ADR Documents

**Goal**: Ensure all 13 ADRs accurately describe decisions, implementation status, and referenced file paths.

**Tasks**:
- [TASK-4-1] Audit implemented ADRs — address **H10** in `docs/adr/implemented/ADR-Declarative-Setup-Architecture.md` specifically: the ADR claims Phase 5 "✓ COMPLETED" (December 16, 2025) with deliverables including `setup.py` and `config/setup/` module, but neither exists. Actual implementation is a simpler interactive wizard in `sandbox/egg_lib/setup_flow.py`. The `--setup` flag works, but the modular architecture, service management (`--enable-services`), and `host_config.py` were never built. Fix: Update ADR status to reflect actual state — mark completed aspects (interactive setup via `egg --setup`, secrets.env/config.yaml creation) and note deferred aspects. Audit remaining 6 implemented ADRs (excluding ADR-Declarative-Setup-Architecture) for status label accuracy and file path existence. **The following were verified accurate in analysis:** ADR-SDLC-Pipeline, ADR-Gateway-Credential-Injection, ADR-Git-Isolation-Architecture, ADR-Standardized-Logging-Interface. The remaining 3 (ADR-Anthropic-API-Credential-Injection, ADR-Context-Sync-Strategy-Custom-vs-MCP) need standard verification. Acceptance: H10 resolved — ADR-Declarative-Setup-Architecture status updated to reflect partial implementation; all other ADR statuses and file references verified correct.
- [TASK-4-2] Audit in-progress ADRs (3 files) — verify current progress descriptions match implementation state. **Correctly marked as in-progress per analysis.** Acceptance: Status accurately reflects what's been built vs. what's planned.
- [TASK-4-3] Audit not-implemented ADR (1 file) — confirm it remains not-implemented. **Correctly marked per analysis.** Acceptance: Status is accurate.

**Dependencies**: Phase 3 (architecture understanding informs ADR verification)

**Exit criteria**: All ADR statuses and references are accurate.

### Phase 5: Developer Guides

**Goal**: Ensure all guides provide accurate, working instructions.

**Tasks**:
- [TASK-5-1] Audit `docs/guides/local-quickstart.md` — verify setup commands, config file paths (`~/.config/egg/`), environment variables, monitoring endpoints (ports 9848, 9849), and CLI flags. **Verified accurate in analysis.** Acceptance: Following the quickstart produces a working setup.
- [TASK-5-2] Audit `docs/guides/deployment.md` and `docs/guides/deploy-migration.md` — verify deployment instructions, Docker commands, and infrastructure references. **Both verified accurate in analysis.** Acceptance: Deployment paths and commands are valid.
- [TASK-5-3] Audit `docs/guides/agent-development.md` and `docs/guides/agent-mode-design.md` — verify agent constraint levels, tool references, and configuration. **Both verified accurate in analysis.** Acceptance: Agent development instructions match sandbox implementation.
- [TASK-5-4] Audit `docs/guides/sdlc-pipeline.md` — address **H9** (lines 730-731 reference `action/build-refine-review-prompt.sh` and `action/build-plan-review-prompt.sh` which do not exist; they were consolidated into `action/build-unified-review-prompt.sh`). Fix: Replace the two stale prompt builder references with `action/build-unified-review-prompt.sh`. Acceptance: H9 resolved — stale prompt builder references replaced with `build-unified-review-prompt.sh`; all other pipeline documentation matches implementation.
- [TASK-5-5] Audit `docs/guides/github-automation.md` and `docs/guides/reusable-workflows.md` — verify workflow file names, trigger events, and input parameters match `.github/workflows/`. **Both verified accurate in analysis.** Acceptance: All workflow references are correct.

**Dependencies**: None

**Exit criteria**: All guides provide accurate instructions.

### Phase 6: Development Documentation

**Goal**: Ensure STRUCTURE.md and TEST_COVERAGE_PLAN.md reflect the actual project layout and test infrastructure.

**Tasks**:
- [TASK-6-1] Audit `docs/development/STRUCTURE.md` — verify directory tree, file descriptions, and naming conventions against actual filesystem. Note: "Known Issues to Investigate" item #1 (shared with TASK-3-1) flagged that `.egg/schemas/` does not exist — if referenced here, correct it. Also verify item #2: `.github/scripts/checks/` may contain Python files described as shell scripts. **Verified accurate in analysis — confirm these specific items.** Acceptance: Every directory and file mentioned in STRUCTURE.md exists, and missing ones are removed or corrected.
- [TASK-6-2] Audit `docs/development/TEST_COVERAGE_PLAN.md` — verify test framework references, coverage targets, and test file paths against `pytest.ini` and test directories. **Verified accurate in analysis.** Acceptance: Test strategy matches actual infrastructure.

**Dependencies**: None

**Exit criteria**: Development docs match the filesystem.

### Phase 7: Component READMEs

**Goal**: Ensure each component's README accurately describes its purpose, structure, and usage.

**Tasks**:
- [TASK-7-1] Audit `gateway/README.md` — address **H1** (undocumented API endpoints: Worktree Management, Session Management, Repository info, Anthropic Proxy, and Git execute endpoints) and **H2** (undocumented source files: `agent_restrictions.py`, `checkpoint_handler.py`, `transcript_buffer.py`, plus 8 test files). Fix for H1: Add sections for Worktree Operations, Session Management, Repository Operations, and Anthropic Proxy endpoints. Fix for H2: Add missing files to the Files section with descriptions. Also audit `gateway/tests/README-integration.md`. Acceptance: H1 resolved — all endpoint groups documented; H2 resolved — all source files listed; integration test docs verified.
- [TASK-7-2] Audit `sandbox/README.md` and `sandbox/.claude/README.md` — address **H3** (missing 6 egg_lib modules: `checkpoint_cli.py`, `compose.py`, `contract_cli.py`, `orchestration.py`, and `self_improvement/` subdirectory), **H4** (missing 2 bin/ symlinks: `egg-checkpoint` and `egg-contract`), and **H5** (incomplete `.claude/` documentation: `commands/` shows only 1 file but 7 exist; `rules/` shows 5 files but 7 exist). Fix for H3: Add missing modules to egg_lib listing. Fix for H4: Add missing symlinks. Fix for H5: Update directory tree to show all files. `sandbox/.claude/README.md` was **verified accurate in analysis**. Acceptance: H3 resolved — all 18 egg_lib modules listed; H4 resolved — all 5 symlinks listed; H5 resolved — all commands and rules files shown.
- [TASK-7-3] Audit `shared/README.md` and `shared/egg_config/README.md` — address **H6** (missing `egg_container` package documentation), **H7** (15 undocumented `egg_contracts` modules), **C2** (code example uses unimplemented `SlackConfig` — replace with `GitHubConfig` or `GatewayConfig`), **M1** (incomplete constants list — missing orchestrator-related constants), and **M2** (GatewayConfig fields undersold — missing `host` and `rate_limits`). Fix for H6: Add `egg_container` section. Fix for H7: Expand Key modules to cover all modules. Fix for C2: Replace `SlackConfig` example with implemented config class. Fix for M1: Add missing constants or expand reference. Fix for M2: Update Key Fields to include `host` and `rate_limits`. Acceptance: H6 resolved — `egg_container` package documented; H7 resolved — all `egg_contracts` modules listed; C2 resolved — code example uses implemented config class; M1 resolved — constants list complete; M2 resolved — GatewayConfig fields include `host` and `rate_limits`.
- [TASK-7-4] Audit `bin/README.md`, `config/README.md`, and `action/README.md` — address **C1** (config/README.md references non-existent `host_config.py` module, `HostConfig` class, CLI interface, and `host-config.template.yaml` — none exist; actual config/ contains `repo_config.py`, `repositories.yaml.example`, and `secrets.template.env`) and **H8** (action/README.md documents 11 files but 25 exist — missing 14 files including role-specific prompt builders, workloop variants, and convention files). Fix for C1: Remove `host_config.py` documentation section; document what actually exists (`repo_config.py`, `secrets.template.env`); note unified config system is planned but not implemented. Fix for H8: Add all 14 missing files to the listing. `bin/README.md` was **verified accurate in analysis — confirm no changes since audit.** Acceptance: C1 resolved — `host_config.py` section removed, actual modules documented; H8 resolved — all 25 files listed in action/README.md; bin/README.md confirmed accurate.

**Dependencies**: None

**Exit criteria**: All component READMEs are accurate.

### Phase 8: Sandbox Claude Code Configuration

**Goal**: Ensure all Claude Code commands and rules files match actual system behavior.

**Tasks**:
- [TASK-8-1] Audit `sandbox/.claude/commands/*.md` (7 files) — address **M3** (`sandbox/.claude/commands/README.md` lists only 2 commands `/sdlc` and `/show-metrics` but 6 commands exist — missing `/coder-mode`, `/documenter-mode`, `/integrator-mode`, `/tester-mode`). Fix: Add all commands to the listing. Verify remaining 6 command files for accuracy. Acceptance: M3 resolved — all 6 commands listed in README.md; command descriptions match implementation.
- [TASK-8-2] Audit `sandbox/.claude/rules/*.md` (7 files) — address **M4** (`sandbox/.claude/rules/README.md` lists 5 rules but `contract.md` exists and is missing from the listing — documents SDLC contract CLI commands). Fix: Add `contract.md` and `README.md` to the listing. Verify remaining rule files for accuracy. Acceptance: M4 resolved — all 7 rules files listed; rules reflect actual constraints.

**Dependencies**: Phase 7 (sandbox understanding)

**Exit criteria**: All Claude Code config docs are accurate.

### Phase 9: Action Convention Documents & Internal Docs

**Goal**: Ensure action conventions and internal docs are accurate.

**Tasks**:
- [TASK-9-1] Audit `action/autofixer-conventions.md`, `action/conflict-conventions.md`, `action/review-conventions.md` — verify conventions match workflow implementations. Acceptance: Convention docs match workflow behavior.
- [TASK-9-2] Audit `.egg/contract-rules.md` — verify contract rules match `shared/egg_contracts/` implementation. **Verified accurate in analysis.** Acceptance: Rules match validation logic.

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

## Issue-to-Task Mapping

All 24 identified issues are mapped to tasks:

| Issue | Severity | Task | File |
|-------|----------|------|------|
| C1 | Critical | TASK-7-4 | config/README.md |
| C2 | Critical | TASK-7-3 | shared/egg_config/README.md |
| C3 | Critical | TASK-1-4 | CHANGELOG.md |
| C4 | Critical | TASK-1-4 | CHANGELOG.md |
| H1 | High | TASK-7-1 | gateway/README.md |
| H2 | High | TASK-7-1 | gateway/README.md |
| H3 | High | TASK-7-2 | sandbox/README.md |
| H4 | High | TASK-7-2 | sandbox/README.md |
| H5 | High | TASK-7-2 | sandbox/README.md |
| H6 | High | TASK-7-3 | shared/README.md |
| H7 | High | TASK-7-3 | shared/README.md |
| H8 | High | TASK-7-4 | action/README.md |
| H9 | High | TASK-5-4 | docs/guides/sdlc-pipeline.md |
| H10 | High | TASK-4-1 | docs/adr/implemented/ADR-Declarative-Setup-Architecture.md |
| M1 | Moderate | TASK-7-3 | shared/egg_config/README.md |
| M2 | Moderate | TASK-7-3 | shared/egg_config/README.md |
| M3 | Moderate | TASK-8-1 | sandbox/.claude/commands/README.md |
| M4 | Moderate | TASK-8-2 | sandbox/.claude/rules/README.md |
| L1 | Low | TASK-1-1 | README.md |
| L2 | Low | — | docs/adr/README.md (no change needed per analysis) |

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
    documented behavior against the actual codebase. Fixes 24 identified issues
    (C1-C4, H1-H10, M1-M4, L1-L2) inline including file paths, CLI commands,
    port numbers, architecture descriptions, and cross-document links.
phases:
  - id: 1
    name: Core Project Documentation
    goal: Root-level docs accurately reflect current project
    tasks:
      - id: TASK-1-1
        description: "Audit README.md; fix L1 (versioning note — verify v0.1.0 release status)"
        acceptance: "L1 resolved — versioning note reflects release status; all paths, commands, ports correct"
        files:
          - README.md
      - id: TASK-1-2
        description: "Audit CONTRIBUTING.md (verified accurate in analysis — confirm no changes)"
        acceptance: "Developer can follow instructions successfully"
        files:
          - CONTRIBUTING.md
      - id: TASK-1-3
        description: "Audit RELEASING.md (verified accurate in analysis — confirm no changes)"
        acceptance: "Release instructions match actual workflow"
        files:
          - RELEASING.md
      - id: TASK-1-4
        description: "Fix CHANGELOG.md: C3 (CLI commands mismatch — egg start/stop → egg --exec/--setup/--compose) and C4 (test count 43+ → 150+)"
        acceptance: "C3 resolved — CLI description uses actual flags; C4 resolved — test count updated"
        files:
          - CHANGELOG.md
  - id: 2
    name: Documentation Hub & Navigation
    goal: Index and overview docs have valid links and descriptions
    tasks:
      - id: TASK-2-1
        description: "Audit docs/index.md (verified accurate in analysis — confirm no changes)"
        acceptance: "Every link resolves to existing file"
        files:
          - docs/index.md
      - id: TASK-2-2
        description: "Audit docs/README.md for consistency with index"
        acceptance: "No contradictions, all links valid"
        files:
          - docs/README.md
  - id: 3
    name: Architecture & Strategy Documents
    goal: Architecture docs match actual codebase structure
    tasks:
      - id: TASK-3-1
        description: "Audit architecture README; fix .egg/schemas/ reference (directory does not exist)"
        acceptance: "All file paths and commands verified; non-existent paths corrected"
        files:
          - docs/architecture/README.md
      - id: TASK-3-2
        description: "Audit agentic-feedback-loop.md (conceptual — verified accurate)"
        acceptance: "Workflows match implementation"
        files:
          - docs/agentic-feedback-loop.md
      - id: TASK-3-3
        description: "Audit collaboration-effectiveness.md (conceptual — verified accurate)"
        acceptance: "No factual inaccuracies"
        files:
          - docs/collaboration-effectiveness.md
      - id: TASK-3-4
        description: "Audit hitl-decisions.md (verified accurate in analysis)"
        acceptance: "API paths and decision flow match code"
        files:
          - docs/hitl-decisions.md
  - id: 4
    name: ADR Documents
    goal: All ADRs have accurate status and references
    tasks:
      - id: TASK-4-1
        description: "Fix H10 in ADR-Declarative-Setup-Architecture.md (claims Phase 5 complete but setup.py/config/setup module never built; actual implementation is setup_flow.py). Audit remaining 6 implemented ADRs (4 verified accurate: SDLC-Pipeline, Gateway-Credential-Injection, Git-Isolation-Architecture, Standardized-Logging-Interface)"
        acceptance: "H10 resolved — ADR status reflects partial implementation; all other ADR statuses verified"
        files:
          - docs/adr/implemented/ADR-Declarative-Setup-Architecture.md
          - docs/adr/implemented/ADR-Anthropic-API-Credential-Injection.md
          - docs/adr/implemented/ADR-Context-Sync-Strategy-Custom-vs-MCP.md
          - docs/adr/implemented/ADR-SDLC-Pipeline.md
          - docs/adr/implemented/ADR-Gateway-Credential-Injection.md
          - docs/adr/implemented/ADR-Git-Isolation-Architecture.md
          - docs/adr/implemented/ADR-Standardized-Logging-Interface.md
      - id: TASK-4-2
        description: "Audit 3 in-progress ADRs (correctly marked per analysis)"
        acceptance: "Progress descriptions match reality"
        files:
          - docs/adr/in-progress/
      - id: TASK-4-3
        description: "Audit 1 not-implemented ADR (correctly marked per analysis)"
        acceptance: "Status is accurate"
        files:
          - docs/adr/not-implemented/
  - id: 5
    name: Developer Guides
    goal: All guides provide accurate working instructions
    tasks:
      - id: TASK-5-1
        description: "Audit local-quickstart.md (verified accurate in analysis)"
        acceptance: "Setup commands and paths verified"
        files:
          - docs/guides/local-quickstart.md
      - id: TASK-5-2
        description: "Audit deployment and migration guides (both verified accurate)"
        acceptance: "Deployment paths and commands valid"
        files:
          - docs/guides/deployment.md
          - docs/guides/deploy-migration.md
      - id: TASK-5-3
        description: "Audit agent development guides (both verified accurate)"
        acceptance: "Instructions match sandbox implementation"
        files:
          - docs/guides/agent-development.md
          - docs/guides/agent-mode-design.md
      - id: TASK-5-4
        description: "Fix H9 in sdlc-pipeline.md (lines 730-731 reference non-existent build-refine-review-prompt.sh and build-plan-review-prompt.sh; replace with build-unified-review-prompt.sh)"
        acceptance: "H9 resolved — stale prompt builder references replaced with build-unified-review-prompt.sh"
        files:
          - docs/guides/sdlc-pipeline.md
      - id: TASK-5-5
        description: "Audit GitHub automation and reusable workflow guides (both verified accurate)"
        acceptance: "All workflow references correct"
        files:
          - docs/guides/github-automation.md
          - docs/guides/reusable-workflows.md
  - id: 6
    name: Development Documentation
    goal: STRUCTURE.md and test plan reflect actual layout
    tasks:
      - id: TASK-6-1
        description: "Audit STRUCTURE.md (verified accurate; confirm .egg/schemas/ and .github/scripts/checks/ references)"
        acceptance: "Every referenced directory and file exists"
        files:
          - docs/development/STRUCTURE.md
      - id: TASK-6-2
        description: "Audit TEST_COVERAGE_PLAN.md (verified accurate in analysis)"
        acceptance: "Test strategy matches infrastructure"
        files:
          - docs/development/TEST_COVERAGE_PLAN.md
  - id: 7
    name: Component READMEs
    goal: Each component README matches its implementation
    tasks:
      - id: TASK-7-1
        description: "Fix H1 (add undocumented gateway API endpoints: Worktree, Session, Repo, Proxy, Git execute) and H2 (add 3 missing source files + 8 test files) in gateway/README.md"
        acceptance: "H1 resolved — all endpoint groups documented; H2 resolved — all source/test files listed"
        files:
          - gateway/README.md
          - gateway/tests/README-integration.md
      - id: TASK-7-2
        description: "Fix H3 (add 6 missing egg_lib modules), H4 (add 2 missing bin/ symlinks), H5 (list all 7 commands + 7 rules files) in sandbox/README.md"
        acceptance: "H3 resolved — 18 modules listed; H4 resolved — 5 symlinks listed; H5 resolved — all commands/rules shown"
        files:
          - sandbox/README.md
          - sandbox/.claude/README.md
      - id: TASK-7-3
        description: "Fix H6 (add egg_container package), H7 (add 15 egg_contracts modules), C2 (replace SlackConfig example), M1 (add orchestrator constants), M2 (add GatewayConfig host/rate_limits fields) in shared READMEs"
        acceptance: "H6, H7, C2, M1, M2 all resolved per analysis fix descriptions"
        files:
          - shared/README.md
          - shared/egg_config/README.md
      - id: TASK-7-4
        description: "Fix C1 (remove non-existent host_config.py docs, document actual modules) in config/README.md. Fix H8 (add 14 missing files) in action/README.md. Verify bin/README.md (confirmed accurate)"
        acceptance: "C1 resolved — host_config.py section removed, actual modules documented; H8 resolved — all 25 files listed; bin/README.md confirmed"
        files:
          - bin/README.md
          - config/README.md
          - action/README.md
  - id: 8
    name: Sandbox Claude Code Configuration
    goal: Claude Code commands and rules match system behavior
    tasks:
      - id: TASK-8-1
        description: "Fix M3 (add 4 missing commands to commands/README.md: /coder-mode, /documenter-mode, /integrator-mode, /tester-mode). Verify 6 command files"
        acceptance: "M3 resolved — all 6 commands listed; command descriptions match implementation"
        files:
          - sandbox/.claude/commands/
      - id: TASK-8-2
        description: "Fix M4 (add contract.md to rules/README.md listing). Verify 6 rule files"
        acceptance: "M4 resolved — all 7 rules listed; rules reflect actual constraints"
        files:
          - sandbox/.claude/rules/
  - id: 9
    name: Action Conventions & Internal Docs
    goal: Convention docs and internal rules are accurate
    tasks:
      - id: TASK-9-1
        description: "Audit 3 action convention documents"
        acceptance: "Conventions match workflow behavior"
        files:
          - action/autofixer-conventions.md
          - action/conflict-conventions.md
          - action/review-conventions.md
      - id: TASK-9-2
        description: "Audit contract rules (verified accurate in analysis)"
        acceptance: "Rules match validation logic"
        files:
          - .egg/contract-rules.md
  - id: 10
    name: Cross-Document Consistency Check
    goal: No contradictions between documents
    tasks:
      - id: TASK-10-1
        description: "Verify port numbers consistent across all docs"
        acceptance: "No conflicting port references"
        files: []
      - id: TASK-10-2
        description: "Verify branch naming consistency across docs"
        acceptance: "All references agree"
        files: []
      - id: TASK-10-3
        description: "Verify all inter-document links work"
        acceptance: "Zero broken links"
        files: []
```

---

*Authored-by: egg*

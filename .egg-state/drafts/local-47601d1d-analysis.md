# Documentation Onboarding Analysis

> Pipeline: local-47601d1d | Phase: refine | Date: 2026-02-13

## 1. Problem Statement

Generate comprehensive, well-indexed documentation for the `jwbron/egg` repository built from a full codebase survey. The output should match the structure maintained by the incremental doc-updater but produced from scratch.

## 2. Current State Assessment

### Existing Documentation Quality

The repository already has **substantial, well-maintained documentation** (65 markdown files, last updated Feb 7, 2026). The existing docs include:

- **`docs/index.md`** — Navigation hub with task-specific guides (well-structured)
- **`docs/architecture/README.md`** — System design, contracts, checkpoints (7.7 KB, comprehensive)
- **`docs/architecture/orchestrator.md`** — Orchestrator deployment modes
- **`docs/development/STRUCTURE.md`** — Directory layout (15 KB, thorough)
- **`docs/development/TEST_COVERAGE_PLAN.md`** — Testing roadmap
- **`docs/guides/`** — 8 operational guides covering quickstart, deployment, SDLC pipeline, GitHub automation, agent development, agent-mode design, reusable workflows, and deploy migration
- **`docs/adr/`** — 10 ADRs (7 implemented, 3 in-progress) with clear index
- **`docs/hitl-decisions.md`** — Human-in-the-loop workflow
- **`docs/agentic-feedback-loop.md`** — Foundational feedback cycle model (14.6 KB)
- **`docs/collaboration-effectiveness.md`** — Safety and collaboration model
- **Component READMEs** — gateway/, sandbox/, shared/, action/, bin/, config/ all have READMEs

### Documentation Gaps Identified

Despite strong existing coverage, the survey identified specific gaps:

| Gap | Impact | Priority |
|-----|--------|----------|
| **Orchestrator README missing** | No component-level docs for orchestrator/ | High |
| **Integration tests README missing** | No guidance for running/writing integration tests | Medium |
| **API reference missing** | Gateway and orchestrator REST endpoints undocumented | Medium |
| **Scripts README missing** | 10 quality-check scripts undocumented | Medium |
| **Metrics directory undocumented** | Self-improvement metrics unexplained | Low |
| **`docs/index.md` missing orchestrator link** | Orchestrator not referenced in component table | High |
| **SDLC pipeline guide too long** | 45 KB single file; could benefit from topic separation | Low |
| **No testing quick-start guide** | TEST_COVERAGE_PLAN.md is a roadmap, not a how-to | Medium |

### Existing Documentation Accuracy

All existing documentation reviewed appears **current and accurate**:
- Architecture docs match actual code structure
- Component READMEs accurately describe their directories
- ADRs reflect implemented decisions
- Guides reference current CLI flags and configuration options

**Recommendation: Do not rewrite existing docs.** Update and extend them.

## 3. Codebase Survey Summary

### System Overview

egg is a structurally enforced SDLC pipeline for autonomous LLM agents that turns GitHub issues into reviewed pull requests with mandatory human gates. It consists of four core components running in Docker containers.

### Component Map

| Component | Location | Lines | Purpose |
|-----------|----------|-------|---------|
| **Gateway Sidecar** | `gateway/` | ~16,300 Python | Policy enforcement, credential injection, network filtering |
| **Sandbox Container** | `sandbox/` | ~4,500 Python | Agent execution environment with Claude Code integration |
| **Orchestrator** | `orchestrator/` | ~18,000 Python | Pipeline state, container spawning, multi-agent coordination |
| **Shared Libraries** | `shared/` | ~12,500 Python | Config, logging, contracts, container building, git utilities |
| **GitHub Action** | `action/` | ~2,000 Bash | CI/CD composite action with prompt builders |
| **Scripts** | `scripts/` | ~1,500 Python | Quality/security check scripts for CI |
| **Config** | `config/` | ~300 Python | Repository configuration loader |

### Architecture (Gateway → Sandbox → Orchestrator)

```
GitHub Issue → SDLC Pipeline → egg
                                 ├── Gateway Sidecar (:9848)
                                 │   ├── Policy engine (branch/PR/phase/role)
                                 │   ├── Credential injection (GitHub, Anthropic)
                                 │   ├── Squid proxy (:3129, network lockdown)
                                 │   ├── Session management (per-container tokens)
                                 │   └── Worktree isolation (.git shadowed)
                                 │
                                 ├── Orchestrator (:9849)
                                 │   ├── Pipeline state (git-backed persistence)
                                 │   ├── Container spawning (Docker SDK)
                                 │   ├── Multi-agent waves (coder→tester→reviewer→integrator)
                                 │   ├── HITL decision queue
                                 │   └── SSE status streaming
                                 │
                                 └── Sandbox Container(s)
                                     ├── Claude Code (headless agent execution)
                                     ├── git/gh wrappers (route through gateway)
                                     ├── CLAUDE.md rules + slash commands
                                     └── No credentials (zero-trust)
```

### Key Design Principles

1. **Structural enforcement** — Controls are infrastructure-level (gateway blocks), not prompt-level
2. **Zero-credential sandbox** — Agent never sees tokens; gateway injects them
3. **Phase-gated operations** — refine→plan→implement→pr with per-phase permissions
4. **Role-based mutations** — implementer/reviewer/human each own specific contract fields
5. **Immutable merge block** — No merge endpoint exists; humans merge via GitHub UI

### Technology Stack

- **Language:** Python 3.11 (342 .py files), Bash (34 .sh files)
- **Frameworks:** Flask + Waitress (gateway, orchestrator), Pydantic (contracts)
- **Infrastructure:** Docker, Docker Compose, Squid proxy
- **CI/CD:** GitHub Actions (22 workflows), act for local parity
- **Testing:** pytest, hypothesis, bandit (80% coverage threshold)
- **Agent:** Claude Code (headless --print mode with stream-json parsing)
- **Auth:** GitHub App tokens (JWT refresh), OAuth, PATs

### Testing Infrastructure

| Category | Framework | Location | Count |
|----------|-----------|----------|-------|
| Unit tests | pytest | `tests/` | 104 files |
| Gateway tests | pytest | `gateway/tests/` | 26 files |
| Orchestrator tests | pytest | `orchestrator/tests/` | 12 files |
| Integration tests | pytest + Docker | `integration_tests/` | 40 files |
| Security scans | bandit | CI workflow | 1 job |
| Custom checks | Python scripts | `scripts/` | 10 scripts |

## 4. Documentation Plan

### Approach: Update and Extend (Not Rewrite)

The existing documentation is strong. The plan focuses on:
1. **Filling gaps** — New docs where none exist (orchestrator README, scripts README, integration tests README)
2. **Updating the index** — Ensure `docs/index.md` covers all components and guides
3. **Refreshing STRUCTURE.md** — Verify it matches the current directory layout
4. **Enriching architecture docs** — Add orchestrator details to architecture overview
5. **Creating missing component READMEs** — orchestrator/ and scripts/ need READMEs

### Files to Create

| File | Purpose | Priority |
|------|---------|----------|
| `orchestrator/README.md` | Component README for orchestrator (API, config, development) | High |
| `integration_tests/README.md` | How to run/write integration tests | Medium |
| `scripts/README.md` | Index of quality/security check scripts | Medium |

### Files to Update

| File | Changes | Priority |
|------|---------|----------|
| `docs/index.md` | Add orchestrator to component table; add integration tests guide; add scripts reference; verify all links | High |
| `docs/development/STRUCTURE.md` | Verify against current layout; add orchestrator and metrics descriptions | High |
| `docs/architecture/README.md` | Add orchestrator section with deployment modes and multi-agent orchestration | Medium |

### Files to Leave Unchanged

All existing ADRs, guides, component READMEs (gateway, sandbox, shared, action, bin, config), strategy docs, and templates are accurate and should not be modified.

### Detailed Plan by Document

#### 1. `orchestrator/README.md` (New — High Priority)

Content to cover:
- What the orchestrator does (pipeline state, container spawning, multi-agent)
- Architecture (Flask REST API on port 9849, Docker SDK, git-backed state)
- Key modules: models.py, state_store.py, container_spawner.py, multi_agent.py, decision_queue.py
- API endpoints overview (pipelines, containers, phases, decisions, signals)
- Configuration (environment variables, Docker volumes)
- Development (running locally, running tests)
- Dependencies on gateway and sandbox

#### 2. `integration_tests/README.md` (New — Medium Priority)

Content to cover:
- Purpose and scope of integration tests
- Prerequisites (Docker, API keys for E2E)
- Running tests (`make test-integration`, `make test-e2e`, `make test-security`)
- Test categories (local_pipeline/, sdlc/)
- Fixtures (EggStack, gateway_session, test_container)
- Network configuration (172.40.x test subnets)
- Writing new tests (patterns, markers, structured verdicts)

#### 3. `scripts/README.md` (New — Medium Priority)

Content to cover:
- Purpose (CI quality/security checks)
- Script index with descriptions
- Architectural boundary enforcement (check-claude-imports, check-gh-cli-usage, check-container-paths)
- Running scripts locally
- Adding new checks

#### 4. `docs/index.md` (Update — High Priority)

Changes:
- Add orchestrator to Component Documentation table
- Add integration_tests to Component Documentation table
- Add scripts to Component Documentation table
- Add "Running Tests" to Task-Specific Guides table
- Verify all existing links resolve to real files

#### 5. `docs/development/STRUCTURE.md` (Update — High Priority)

Changes:
- Verify orchestrator/ description matches current module set
- Add metrics/ directory description
- Verify integration_tests/ structure is current
- Ensure scripts/ directory listing is complete

#### 6. `docs/architecture/README.md` (Update — Medium Priority)

Changes:
- Add orchestrator section covering multi-agent waves, pipeline state, container spawning
- Reference docs/architecture/orchestrator.md for deployment modes
- Ensure component relationship diagram includes orchestrator

## 5. Constraints and Dependencies

### Constraints

1. **Documentation-only changes** — No source code modifications
2. **Preserve existing docs** — Update in place; do not delete or restructure what works
3. **No placeholder content** — Every document must have real, useful content
4. **Match existing style** — Concise, table-heavy, code-block-rich format used throughout the repo
5. **ADRs are immutable** — Index them but never modify their content

### Dependencies

- All new docs depend on the codebase survey (Phase 1, completed)
- `docs/index.md` updates depend on new component READMEs being created first
- `docs/development/STRUCTURE.md` updates should happen after new READMEs are finalized

### Risks

| Risk | Mitigation |
|------|------------|
| Docs become stale after code changes | doc-updater workflow handles incremental updates |
| Over-documenting stable code | Focus on interfaces, config, and getting-started; skip implementation details |
| Breaking existing link structure | Cross-reference validation in Phase 5 |

## 6. Implementation Approach

### Recommended: Incremental Enhancement

1. Create the three missing component READMEs (orchestrator, integration_tests, scripts)
2. Update `docs/index.md` to reference all components
3. Update `docs/development/STRUCTURE.md` to match current layout
4. Update `docs/architecture/README.md` with orchestrator section
5. Cross-reference validation (verify all links in index.md resolve)

### Alternative Considered: Full Rewrite

Rejected because:
- Existing documentation is high quality and current
- A rewrite would discard accurate content
- Risk of introducing inaccuracies while rewriting known-good docs
- Existing style is consistent and well-suited to the project

### File Count Summary

| Action | Count |
|--------|-------|
| Files to create | 3 |
| Files to update | 3 |
| Files to leave unchanged | 59 |
| Total documentation files after | 68 |

## 7. Open Questions

None. The codebase survey provides sufficient information to proceed with the plan. Existing documentation is clear about conventions, and the gaps are well-defined.

---

*Authored by egg*

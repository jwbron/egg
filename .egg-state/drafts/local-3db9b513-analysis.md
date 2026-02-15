# Analysis: Generate Comprehensive Onboarding Documentation

> Pipeline: local-3db9b513 | Phase: refine

## Problem Statement

The egg repository needs comprehensive onboarding documentation that helps new contributors understand the project. The codebase is a multi-component system (gateway, orchestrator, sandbox, shared libraries, CLI tools, GitHub Actions) with extensive existing documentation spread across 15 README files, 10+ ADRs, and multiple guides. However, there is no unified onboarding path that takes a new contributor from zero context to productive contribution.

**Current state**: Documentation exists in many places but assumes familiarity with the system. A new contributor must navigate between `docs/index.md`, `README.md`, `CONTRIBUTING.md`, individual component READMEs, ADRs, and guides to piece together a mental model. There is no progressive-disclosure path that builds understanding incrementally.

**Desired outcome**: Documentation that provides a clear onboarding journey — from "what is this project?" through "how do I make my first contribution?" — while leveraging and linking to the existing documentation rather than duplicating it.

## Current Behavior

### Existing Documentation Inventory

The repository has significant documentation infrastructure (15 README files, 7+ guides, 10 ADRs), organized as follows:

**Entry points:**
- `README.md` — Comprehensive project overview with architecture diagrams, CLI reference, SDLC pipeline explanation, quick start, and deployment options. At ~437 lines, it covers a lot but is dense for newcomers.
- `docs/index.md` — Master navigation hub with task-specific guide lookup table. Designed primarily for agents, not human contributors.
- `CONTRIBUTING.md` — Development setup, workflow, and PR process. Concise but practical.

**Architecture & design:**
- `docs/architecture/README.md` — System design, security model, component table (`gateway/README.md:1-192`)
- `docs/adr/` — 10 Architecture Decision Records documenting major design choices
- `docs/agentic-feedback-loop.md`, `docs/collaboration-effectiveness.md` — Strategy docs

**Component READMEs:**
- `gateway/README.md` — Gateway sidecar (policy, credentials, API)
- `sandbox/README.md` — Agent environment
- `shared/README.md` — Shared libraries overview
- `config/README.md` — Configuration
- `bin/README.md` — CLI entry points
- `action/README.md` — GitHub Action
- `sandbox/.claude/README.md`, `sandbox/.claude/commands/README.md`, `sandbox/.claude/rules/README.md` — Agent configuration

**Operational guides:**
- `docs/guides/local-quickstart.md` — Getting started with PAT auth
- `docs/guides/deployment.md` — Deployment options
- `docs/guides/sdlc-pipeline.md` — SDLC pipeline operations
- `docs/guides/github-automation.md` — Workflow automation
- `docs/guides/agent-development.md` — Adding new agent roles
- `docs/guides/agent-mode-design.md` — Design principles
- `docs/guides/checkpoint-access.md` — Checkpoint querying
- `docs/guides/reusable-workflows.md` — External repo integration

**Development docs:**
- `docs/development/STRUCTURE.md` — Directory conventions and organization

### Identified Gaps

Despite the breadth of existing docs, several gaps affect new contributor onboarding:

1. **No progressive learning path**: Documentation jumps between high-level architecture and implementation details. A contributor reading `README.md` is immediately presented with gateway diagrams, phase permissions tables, and CLI reference — all important, but overwhelming as an entry point.

2. **Agent-oriented vs. human-oriented**: `docs/index.md` is explicitly designed for agents navigating the codebase ("maps task types to the docs you should read first"). There is no equivalent human-oriented onboarding guide.

3. **Missing conceptual overview**: The system has a rich conceptual model (trusted vs. untrusted containers, structural enforcement vs. behavioral controls, phase-based permissions, multi-agent orchestration). This is documented in ADRs and architecture docs but not synthesized into a "concepts" or "how it works" document for newcomers.

4. **Orchestrator under-documented**: The orchestrator is a major component (30+ Python files, manages the entire SDLC pipeline lifecycle) but has no dedicated README. It's covered in `docs/architecture/orchestrator.md` and `docs/development/STRUCTURE.md` but lacks a component-level README comparable to `gateway/README.md` or `sandbox/README.md`.

5. **No contributor workflow examples**: `CONTRIBUTING.md` covers setup and make targets but doesn't walk through common contribution scenarios (fixing a bug in the gateway, adding a new CLI command, modifying phase permissions, etc.).

6. **Test infrastructure not explained**: The test suite has 111 unit tests, 37 integration tests, and gateway-specific tests, with markers (`integration`, `functional`, `e2e`, `security`, `agent_flaky`), but no guide explaining test architecture, fixture patterns, or how to write tests for different components.

## Constraints

- **No duplication**: The existing 15+ READMEs and guides represent significant maintained documentation. Onboarding docs must link to them, not duplicate them. Duplicated content becomes stale.
- **Multi-audience**: Documentation must serve both human contributors (the primary audience for onboarding) and agent contributors (who use `docs/index.md` as their navigation hub). These audiences have different needs.
- **Existing conventions**: `docs/development/STRUCTURE.md` defines documentation organization rules: "Documentation should live close to code. Only cross-cutting docs belong in the central `docs/` directory."
- **Maintenance burden**: The `on-push-doc-updater.yml` workflow automatically checks if code changes need doc updates. New docs should be structured so this workflow can maintain them.
- **Repository is under heavy development**: The README explicitly states the project is "currently under heavy development" with changing behavior. Onboarding docs should focus on stable concepts (architecture, security model, contribution workflow) rather than volatile implementation details.

## Options Considered

### Option A: Standalone Onboarding Guide

**Approach**: Create a single comprehensive `docs/guides/onboarding.md` document that provides a progressive learning path from zero to contributor. Structure it as a narrative that walks through concepts, architecture, setup, and first contribution, linking to existing docs at each step.

**Pros**:
- Single entry point — new contributors read one document
- Progressive disclosure — concepts build on each other in a logical order
- Minimal structural change — one new file in `docs/guides/`
- Easy to maintain — doc-updater can flag it when referenced components change

**Cons**:
- Risk of becoming a "mega document" that's hard to maintain
- May duplicate content that exists in `README.md` and `CONTRIBUTING.md`
- Doesn't address the orchestrator README gap or test documentation gap

### Option B: Onboarding Guide + Gap-Filling Component Docs

**Approach**: Create `docs/guides/onboarding.md` as the primary onboarding path, AND fill the identified documentation gaps: add `orchestrator/README.md`, enhance test documentation, and add contributor workflow examples. The onboarding guide links to these new docs as part of the learning path.

**Pros**:
- Addresses both the onboarding path AND the structural documentation gaps
- New component READMEs follow the established pattern (every major directory has a README)
- Test documentation helps contributors write tests correctly from day one
- More comprehensive improvement to the documentation surface

**Cons**:
- Larger scope of changes across multiple files
- More effort to maintain — more docs that could go stale
- Risk of scope creep during implementation

### Option C: Restructured Documentation Hub

**Approach**: Restructure `docs/index.md` to serve both humans and agents, adding a "New Contributor" section with a guided path. Add minimal new documents (orchestrator README, test guide) and update existing docs to better support progressive learning.

**Pros**:
- Leverages existing navigation hub rather than creating a parallel one
- Minimal new documents
- Forces improvements to the existing doc structure

**Cons**:
- `docs/index.md` is designed for agent navigation — retrofitting it for human onboarding may compromise its agent-oriented design
- Doesn't create a dedicated narrative onboarding experience
- Mixing audiences in one document creates design tension

## Recommended Approach

**Option B: Onboarding Guide + Gap-Filling Component Docs**

This option is recommended because it addresses both the immediate need (a clear onboarding path) and the underlying gaps that make onboarding difficult (missing orchestrator README, undocumented test patterns). The new documents follow established conventions — every major component directory already has a README, and guides live in `docs/guides/`.

The implementation should:

1. **Create `docs/guides/onboarding.md`** — The primary onboarding document, structured as a progressive learning path:
   - What is egg? (concept, not implementation)
   - Key concepts (trusted/untrusted, structural enforcement, phases, roles)
   - Architecture overview (link to existing docs, don't repeat)
   - Development setup (link to CONTRIBUTING.md + local-quickstart.md)
   - Common contribution scenarios with pointers
   - Where to go next (link to relevant guides per task type)

2. **Create `orchestrator/README.md`** — Component README following the pattern of `gateway/README.md` and `sandbox/README.md`. Covers API, container lifecycle, multi-agent orchestration, state management.

3. **Create `docs/guides/testing.md`** — Test architecture guide covering test types, markers, fixtures, and how to write tests for each component.

4. **Update `docs/index.md`** — Add the onboarding guide to the Quick Navigation section so both humans and agents can discover it.

5. **Update `README.md`** — Add a "New Contributors" callout near the top that points to the onboarding guide.

This approach keeps the scope bounded while providing the highest-impact improvements. The onboarding guide serves as the "glue" document that gives context and links to existing detailed docs, while the gap-filling docs address real holes that affect all contributors.

## Open Questions

1. **Audience prioritization**: The task description says "new contributors." Should this documentation target:
   - External open-source contributors who have never seen the codebase?
   - Internal team members who are familiar with the project's goals but not the codebase?
   - Both equally?

   This affects the level of conceptual explanation vs. practical setup guidance.

2. **Scope of orchestrator README**: The orchestrator has 30+ files and complex internals (container spawning, DAG execution, decision queues, state management). Should the README be comprehensive (like `gateway/README.md` at ~200 lines) or minimal (like `config/README.md` at ~50 lines)?

3. **Test guide depth**: Should the testing documentation cover just the test commands and markers, or should it include detailed fixture documentation, mock patterns, and component-specific test writing guides?

## Complexity Assessment

This is a **medium** complexity task. It involves creating 3-4 new documentation files and updating 2 existing files. The scope is clear (onboarding docs + gap-filling), the patterns are established (existing READMEs and guides provide templates), and the content requires codebase research rather than architectural decisions. However, it spans multiple files and requires synthesizing information from across the entire repository.

---

*Authored-by: egg*

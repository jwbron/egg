# Plan: Build out a guide for adding egg support to another repo

> Issue: #415 | Phase: plan

## Summary

Create a comprehensive adoption guide (`docs/guides/adding-egg-to-your-repo.md`) that walks users through setting up the full SDLC pipeline with egg in their repositories. The guide follows a tiered approach: a quick-start section for essential setup, followed by standard SDLC pipeline configuration, working with egg as a collaborator, and troubleshooting. The guide references existing workflow files rather than duplicating them, with minimal inline examples for clarity.

Based on the approved analysis, this guide will:
- Focus on the full SDLC pipeline (review bots, fixers, responders are part of this)
- Link to existing documentation rather than duplicate details
- Reference workflow files in the repo rather than embedding them (per human feedback)
- Include a placeholder for GitHub authentication when user account auth is complete (#132)

## Implementation Phases

### Phase 1: Document Structure and Prerequisites

**Goal**: Create the document skeleton with prerequisites and initial setup instructions.

**Tasks**:
- [TASK-1-1] Create document skeleton with all major sections — Acceptance: File exists at `docs/guides/adding-egg-to-your-repo.md` with TOC structure
- [TASK-1-2] Write Prerequisites section covering repo requirements, Anthropic API, and GitHub access — Acceptance: Prerequisites clearly list all requirements with links to setup resources
- [TASK-1-3] Add placeholder for GitHub authentication with link to #132 — Acceptance: Section notes that user account auth is coming and links to issue #132

**Dependencies**: None

**Exit criteria**: Document skeleton exists with prerequisites section complete.

### Phase 2: Quick Start and Workflow Setup

**Goal**: Provide essential setup steps to get the SDLC pipeline running.

**Tasks**:
- [TASK-2-1] Write Quick Start section with minimal steps to trigger the pipeline on an issue — Acceptance: Clear 3-5 step quick start that references existing workflow files
- [TASK-2-2] Document SDLC label setup using the setup script — Acceptance: Instructions for running `setup-sdlc-labels.sh` with expected output
- [TASK-2-3] Write caller workflow template for `sdlc-pipeline.yml` — Acceptance: Minimal workflow example that calls reusable workflow with required inputs/secrets
- [TASK-2-4] Document required GitHub Actions secrets — Acceptance: Table listing all secrets with descriptions and where to obtain them

**Dependencies**: Phase 1

**Exit criteria**: Users can copy-paste workflow and configure secrets to get started.

### Phase 3: Working with Egg

**Goal**: Explain how the SDLC pipeline works and how to collaborate with egg.

**Tasks**:
- [TASK-3-1] Write "How the Pipeline Works" overview — Acceptance: Clear explanation of refine → plan → implement → PR flow with diagram reference
- [TASK-3-2] Document phase approvals and HITL decisions — Acceptance: Explains checkbox-based approvals and decision points with examples
- [TASK-3-3] Write "Collaborating with Egg" section — Acceptance: Covers PR reviews, @mention interactions, and feedback loops
- [TASK-3-4] Document useful labels and skip markers — Acceptance: Table of SDLC labels and skip markers with their effects

**Dependencies**: Phase 2

**Exit criteria**: Users understand how to work with egg through the pipeline lifecycle.

### Phase 4: Configuration and Troubleshooting

**Goal**: Cover customization options and common issues.

**Tasks**:
- [TASK-4-1] Document per-repository configuration options — Acceptance: Covers `.egg/` config files (review-rules.md, etc.) with links to detailed docs
- [TASK-4-2] Write secrets reference table — Acceptance: Complete table with all secrets, when required, and descriptions
- [TASK-4-3] Write Troubleshooting section with common issues — Acceptance: 5+ common issues with solutions (auth failures, timeout, label issues, etc.)
- [TASK-4-4] Add "Next Steps" section with links to advanced topics — Acceptance: Links to deployment.md, github-automation.md, and other relevant docs

**Dependencies**: Phase 3

**Exit criteria**: Guide is complete with configuration and troubleshooting coverage.

### Phase 5: Integration and Documentation Updates

**Goal**: Integrate the new guide into the documentation structure.

**Tasks**:
- [TASK-5-1] Add entry to `docs/index.md` in the Guides table — Acceptance: Guide appears in documentation index with correct description
- [TASK-5-2] Update `docs/guides/reusable-workflows.md` to reference new guide — Acceptance: Adds link to adoption guide at top for users new to egg
- [TASK-5-3] Review and polish final document — Acceptance: Consistent formatting, no broken links, follows doc conventions

**Dependencies**: Phase 4

**Exit criteria**: Guide is integrated into documentation structure and cross-referenced.

## Test Strategy

- **Manual verification**: Follow the guide step-by-step in a test repository to verify all instructions work
- **Link validation**: Ensure all internal doc links resolve correctly
- **Workflow validation**: Verify referenced workflow files exist and match described inputs/outputs
- **Review for clarity**: Ensure a new user with no egg context can follow the guide

## Rollback Plan

Since this is a documentation-only change, rollback is straightforward:
```bash
git revert <commit-hash>
```

If partial rollback is needed:
- Remove `docs/guides/adding-egg-to-your-repo.md`
- Revert changes to `docs/index.md`
- Revert changes to `docs/guides/reusable-workflows.md`

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Referenced workflow files change | Medium | Low | Use stable references; note version in guide |
| User account auth not ready (#132) | Medium | Low | Include placeholder with clear note linking to issue |
| Guide becomes outdated | Medium | Medium | Keep examples minimal; link to canonical docs |
| Instructions don't work for edge cases | Low | Medium | Test in clean repository; include troubleshooting |

## Migration Notes

No migrations required. This is a new documentation file that doesn't affect existing functionality.

**Note for future update**: When issue #132 (user account authentication) is complete, update the authentication section in this guide to reflect the new approach.

---

## Structured Task Appendix

The following YAML block is machine-readable and will be extracted into the contract.
It must accurately reflect the tasks described above. The `pr:` section provides the
title and description that will be used when creating the pull request.

```yaml
# yaml-tasks
pr:
  title: "Add guide for adding egg support to external repos"
  description: |
    Creates a comprehensive adoption guide that walks users through setting up
    the full SDLC pipeline with egg in their own repositories. The guide covers
    prerequisites, workflow setup, working with egg as a collaborator, and
    troubleshooting common issues.

    Closes #415
phases:
  - id: 1
    name: Document Structure and Prerequisites
    goal: Create the document skeleton with prerequisites and initial setup instructions
    tasks:
      - id: TASK-1-1
        description: Create document skeleton with all major sections
        acceptance: File exists at docs/guides/adding-egg-to-your-repo.md with TOC structure
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-1-2
        description: Write Prerequisites section covering repo requirements, Anthropic API, and GitHub access
        acceptance: Prerequisites clearly list all requirements with links to setup resources
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-1-3
        description: Add placeholder for GitHub authentication with link to #132
        acceptance: Section notes that user account auth is coming and links to issue #132
        files:
          - docs/guides/adding-egg-to-your-repo.md
  - id: 2
    name: Quick Start and Workflow Setup
    goal: Provide essential setup steps to get the SDLC pipeline running
    tasks:
      - id: TASK-2-1
        description: Write Quick Start section with minimal steps to trigger the pipeline on an issue
        acceptance: Clear 3-5 step quick start that references existing workflow files
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-2-2
        description: Document SDLC label setup using the setup script
        acceptance: Instructions for running setup-sdlc-labels.sh with expected output
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-2-3
        description: Write caller workflow template for sdlc-pipeline.yml
        acceptance: Minimal workflow example that calls reusable workflow with required inputs/secrets
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-2-4
        description: Document required GitHub Actions secrets
        acceptance: Table listing all secrets with descriptions and where to obtain them
        files:
          - docs/guides/adding-egg-to-your-repo.md
  - id: 3
    name: Working with Egg
    goal: Explain how the SDLC pipeline works and how to collaborate with egg
    tasks:
      - id: TASK-3-1
        description: Write How the Pipeline Works overview
        acceptance: Clear explanation of refine to plan to implement to PR flow with diagram reference
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-3-2
        description: Document phase approvals and HITL decisions
        acceptance: Explains checkbox-based approvals and decision points with examples
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-3-3
        description: Write Collaborating with Egg section
        acceptance: Covers PR reviews, @mention interactions, and feedback loops
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-3-4
        description: Document useful labels and skip markers
        acceptance: Table of SDLC labels and skip markers with their effects
        files:
          - docs/guides/adding-egg-to-your-repo.md
  - id: 4
    name: Configuration and Troubleshooting
    goal: Cover customization options and common issues
    tasks:
      - id: TASK-4-1
        description: Document per-repository configuration options
        acceptance: Covers .egg/ config files (review-rules.md, etc.) with links to detailed docs
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-4-2
        description: Write secrets reference table
        acceptance: Complete table with all secrets, when required, and descriptions
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-4-3
        description: Write Troubleshooting section with common issues
        acceptance: 5+ common issues with solutions (auth failures, timeout, label issues, etc.)
        files:
          - docs/guides/adding-egg-to-your-repo.md
      - id: TASK-4-4
        description: Add Next Steps section with links to advanced topics
        acceptance: Links to deployment.md, github-automation.md, and other relevant docs
        files:
          - docs/guides/adding-egg-to-your-repo.md
  - id: 5
    name: Integration and Documentation Updates
    goal: Integrate the new guide into the documentation structure
    tasks:
      - id: TASK-5-1
        description: Add entry to docs/index.md in the Guides table
        acceptance: Guide appears in documentation index with correct description
        files:
          - docs/index.md
      - id: TASK-5-2
        description: Update docs/guides/reusable-workflows.md to reference new guide
        acceptance: Adds link to adoption guide at top for users new to egg
        files:
          - docs/guides/reusable-workflows.md
      - id: TASK-5-3
        description: Review and polish final document
        acceptance: Consistent formatting, no broken links, follows doc conventions
        files:
          - docs/guides/adding-egg-to-your-repo.md
```

---

*Authored-by: egg*

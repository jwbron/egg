# Analysis: Build out a guide for how to add egg support to another repo

> Issue: #415 | Phase: refine

## Problem Statement

Users who want to adopt egg in their own repositories need a comprehensive guide that covers:
1. Setting up GitHub workflows to trigger egg automation
2. Configuring the required GitHub secrets
3. Understanding how to work with egg as a developer/collaborator

Currently, documentation exists but is scattered across multiple files (reusable-workflows.md, deployment.md, github-automation.md, sdlc-pipeline.md). There is no single "getting started for external repos" document that walks through the complete setup process.

## Current Behavior

Existing documentation covers:

| Document | What It Covers |
|----------|----------------|
| `docs/guides/reusable-workflows.md` | How to call egg's workflows from external repos, workflow parameters, secret names |
| `docs/guides/deployment.md` | Docker Compose and CLI deployment (less relevant for external repos using GitHub Actions) |
| `docs/guides/github-automation.md` | All available workflows, customization options, security model |
| `docs/guides/sdlc-pipeline.md` | Deep dive into SDLC phases, contracts, and operational commands |
| `action/README.md` | Brief overview of the GitHub Action |

**Gap**: No unified "adoption guide" that walks a new user through setup end-to-end, with clear checklists and troubleshooting.

## Constraints

- **Technical constraints**:
  - Users must have access to the jwbron/egg repository (workflows reference `jwbron/egg/.github/workflows/*@main`)
  - GitHub App authentication is recommended for automation; PAT works but has limitations
  - The `action_ref` parameter cannot be dynamic (GitHub Actions limitation) — users needing different action versions must fork

- **Scope constraints**:
  - The guide should focus on GitHub Actions deployment (most common use case for external repos)
  - Docker Compose deployment for self-hosted runners is a secondary concern
  - The guide should be practical (copy-paste ready) rather than exhaustive

- **Dependencies**:
  - Requires `ANTHROPIC_OAUTH_TOKEN` (Anthropic API access)
  - Requires GitHub App credentials (`BOT_APP_ID`, `BOT_APP_PRIVATE_KEY`, `BOT_APP_INSTALLATION_ID`) OR a PAT with appropriate scopes

## Options Considered

### Option A: Single comprehensive guide

**Approach**: Create one new document (`docs/guides/adding-egg-to-your-repo.md`) that covers everything: workflow setup, secrets configuration, SDLC labels, working with egg, and troubleshooting.

**Pros**:
- One-stop-shop for new adopters
- Can include step-by-step checklists
- Easy to maintain a single document

**Cons**:
- May become long and overwhelming
- Duplicates some content from existing guides
- Risk of content getting out of sync with source docs

### Option B: Quick-start guide with deep links

**Approach**: Create a shorter "quick-start" guide that provides the essential setup steps and links to existing documentation for details.

**Pros**:
- Minimal duplication
- Keeps detailed documentation in canonical locations
- Faster to write and maintain

**Cons**:
- Users must jump between documents
- May miss context that's only in the linked docs
- Less self-contained

### Option C: Tiered guide with examples

**Approach**: Create a guide with three tiers:
1. **Quick Start** (5 min): Minimal setup to run the review bot
2. **Standard Setup** (15 min): Full SDLC pipeline with all workflows
3. **Advanced Configuration**: Custom prompts, rules, and self-hosted deployment

Include copy-paste workflow files for each tier.

**Pros**:
- Meets users where they are
- Copy-paste ready for common use cases
- Progressive disclosure reduces overwhelm

**Cons**:
- More content to maintain
- Risk of tier examples drifting from actual workflows
- Users might pick wrong tier for their needs

## Recommended Approach

**Option C: Tiered guide with examples** is recommended.

**Justification**:
1. **Different users have different needs** — Some just want PR review automation; others want the full SDLC pipeline. A tiered approach serves both.
2. **Copy-paste ready examples reduce friction** — New users can get started immediately without piecing together workflow files.
3. **Progressive disclosure** — Quick start success builds confidence before diving into advanced features.
4. **Existing docs remain canonical** — The guide links to detailed documentation rather than duplicating it.

**Proposed document structure**:

```
docs/guides/adding-egg-to-your-repo.md

# Adding egg to Your Repository

## Prerequisites
- GitHub repository
- Anthropic API access (ANTHROPIC_OAUTH_TOKEN)
- GitHub App credentials (or PAT for testing)

## Quick Start: AI Code Review (5 minutes)
1. Copy review workflow
2. Configure secrets
3. Open a PR and see it work

## Standard Setup: Full SDLC Pipeline
1. Set up SDLC labels
2. Copy all workflows
3. Configure secrets
4. Trigger pipeline on an issue

## Working with egg
- How the pipeline works
- Approving phases
- HITL decisions
- Useful labels and markers

## Configuration
- Custom review rules (.egg/review-rules.md)
- Skip markers ([skip-review], etc.)
- Bot username and branch prefix

## Secrets Reference
| Secret | Required | Description |
|--------|----------|-------------|
| ... | ... | ... |

## Troubleshooting
- Common issues and solutions

## Advanced Topics
- Custom prompt scripts
- Self-hosted deployment
- Forking workflows for customization
```

## Open Questions

### Question 1: What tier should be the default focus?

The guide could emphasize different starting points:
- **Review bot first**: Lower barrier to entry, but limited value proposition
- **Full SDLC first**: Higher value but more complex setup
- **Equal treatment**: Present both without recommendation

**Context**: The review bot is simpler but the SDLC pipeline is the core value of egg. Most users who want just a review bot might use simpler alternatives.

### Question 2: Should the guide include GitHub App setup instructions?

Creating a GitHub App involves:
- App creation in GitHub settings
- Configuring permissions
- Installing on repositories
- Storing credentials

This is complex but necessary for production use. Options:
- **Include full instructions**: Complete but lengthy
- **Link to GitHub docs + egg-specific requirements**: Shorter, relies on external docs
- **Recommend starting with PAT**: Lower friction but less production-ready

### Question 3: Should example workflow files be embedded or referenced?

- **Embedded in guide**: Easy to copy, but creates duplication with actual workflow files
- **Link to repo files**: Single source of truth, but users must navigate the repo
- **Provide minimal examples + link to full versions**: Balance of convenience and maintenance

---

*Authored-by: egg*

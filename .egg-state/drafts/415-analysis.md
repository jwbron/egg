# Analysis: Guide for Adding egg Support to Another Repo

> Issue: #415 | Phase: refine

## Problem Statement

Users who want to adopt egg's SDLC pipeline in their own repositories lack a comprehensive guide explaining the setup process. Currently, the knowledge is scattered across multiple documentation files:
- `docs/guides/reusable-workflows.md` covers workflow calling
- `docs/guides/sdlc-pipeline.md` explains the pipeline architecture
- `docs/guides/github-automation.md` describes individual workflows
- `action/README.md` documents the GitHub Action

**Current state**: There is no single document that walks a new user through the complete end-to-end setup of egg in an external repository.

**Desired outcome**: A comprehensive "Getting Started with egg in Your Repo" guide that covers:
1. Prerequisites and requirements
2. GitHub App setup and secrets configuration
3. Workflow installation options
4. SDLC label setup
5. Optional customization (review rules, autofixer rules)
6. Working with egg (triggering pipelines, human gates, feedback)

## Current Behavior

### Existing Documentation Structure

The current documentation is organized around component responsibilities rather than user journeys:

| Document | Focus | Gaps for External Setup |
|----------|-------|------------------------|
| `docs/guides/reusable-workflows.md` | How to call workflows | Missing secrets setup, App creation |
| `docs/guides/sdlc-pipeline.md` | Pipeline architecture | Internal focus, assumes egg is already set up |
| `docs/guides/github-automation.md` | Individual workflows | No installation instructions |
| `docs/guides/deployment.md` | Docker Compose/CLI | For running egg locally, not GitHub Actions |

### Setup Components Identified

From the codebase analysis, setting up egg in an external repo requires:

**1. GitHub App Configuration**
- Create a GitHub App with required permissions
- Install the App on the target repository
- Store App credentials as repository secrets

**2. Repository Secrets** (from `docs/guides/reusable-workflows.md:162-168`)
- `BOT_APP_ID` — GitHub App ID
- `BOT_APP_PRIVATE_KEY` — GitHub App private key
- `BOT_APP_INSTALLATION_ID` — GitHub App installation ID
- `ANTHROPIC_OAUTH_TOKEN` — Anthropic API token

**3. Workflow Installation**
Two approaches are documented in `docs/guides/reusable-workflows.md`:
- **Reusable workflow calls**: Reference `jwbron/egg/.github/workflows/*.yml@main`
- **Direct copy**: Copy workflow files into `.github/workflows/`

**4. SDLC Labels** (from `.github/scripts/setup-sdlc-labels.sh`)
- `sdlc:refine` — Refine phase
- `sdlc:plan` — Plan phase
- `sdlc:implement` — Implement phase
- `sdlc:pr` — PR in review
- `sdlc:awaiting-approval` — Waiting for human approval

**5. Optional Customization Files**
- `.egg/review-rules.md` — Custom review focus areas
- `.egg/autofixer-rules.md` — Auto-fix vs report rules
- `.egg/conflict-rules.md` — Conflict resolution rules

**6. Understanding the Human Gates**
- Phase approval via checkboxes
- HITL decisions for multiple-choice questions
- Feedback comments for open-ended input

### Existing Entry Points

The main README.md (lines 139-147) has a "Quick Start" section for the SDLC pipeline but it's too brief for external adoption:

```
### Using the SDLC Pipeline (GitHub Actions)

1. Install the egg GitHub App or configure workflows in your repository
2. Add the `sdlc:refine` label to an issue
3. The pipeline begins: refine → plan → implement → ready for merge
4. Review and merge the PR via GitHub UI
```

This skips all the actual setup steps.

## Constraints

- **GitHub App limitations**: External repos cannot use egg's internal App; users must create their own
- **Secret visibility**: `BOT_APP_PRIVATE_KEY` is multi-line, requires special handling in GitHub Actions
- **Workflow reference stability**: Calling `@main` may break on changes; recommend version tags
- **Action reference limitation**: GitHub's `uses:` field cannot be dynamic; users must hardcode refs
- **Network mode**: External repos will typically use public mode (private mode requires credential access)
- **Permissions**: Workflows need specific GitHub permissions (issues:write, pull-requests:write, contents:write)

## Options Considered

### Option A: Single Comprehensive Guide

**Approach**: Create one new document `docs/guides/setup-external-repo.md` covering all setup steps from start to finish.

**Pros**:
- Single entry point for new users
- Complete narrative from prerequisites to first run
- Can be linked from README.md as "Setup Guide for External Repos"
- Reduces hunting across multiple documents

**Cons**:
- Some duplication with existing docs (reusable-workflows.md, github-automation.md)
- Longer document may be harder to maintain
- May become stale if workflow parameters change

### Option B: Focused Quick Start + References

**Approach**: Create a shorter "quick start" document (`docs/guides/quick-start.md`) that provides essential steps and links to existing docs for details.

**Pros**:
- Minimal duplication
- Leverages existing documentation
- Easier to maintain

**Cons**:
- Users still need to navigate multiple documents
- May miss context needed for decision-making
- Quick start may be too terse for less experienced users

### Option C: Comprehensive Guide with Embedded Tutorials

**Approach**: Create a detailed guide with step-by-step tutorials, including screenshots or command outputs where helpful.

**Pros**:
- Most user-friendly for beginners
- Reduces ambiguity
- Good for onboarding new teams

**Cons**:
- Highest maintenance burden
- Screenshots become stale quickly
- May be overkill for experienced GitHub Actions users

## Recommended Approach

**Option A: Single Comprehensive Guide**

**Justification**:
1. **User journey focus**: New users need a cohesive narrative, not scattered references
2. **Completeness**: All steps in one place prevents missed requirements
3. **Maintainability**: A single document is easier to update than coordinating changes across multiple files
4. **Cross-linking**: Can still reference existing docs for deep dives (e.g., "For details on the pipeline architecture, see `sdlc-pipeline.md`")
5. **Entry point**: Provides a clear "start here" location for external adoption

**Proposed Document Structure**:

```markdown
# Adding egg to Your Repository

## Prerequisites
- GitHub repository with Actions enabled
- Anthropic API access (API key or OAuth token)
- Admin access to create GitHub App

## Step 1: Create a GitHub App
- Required permissions
- Webhook settings (optional)
- Generate private key

## Step 2: Configure Repository Secrets
- BOT_APP_ID
- BOT_APP_PRIVATE_KEY
- BOT_APP_INSTALLATION_ID
- ANTHROPIC_OAUTH_TOKEN

## Step 3: Install Workflows
### Option A: Reusable Workflows (Recommended)
### Option B: Copy Workflows

## Step 4: Set Up SDLC Labels
- Run setup script
- Or create manually

## Step 5: Customize (Optional)
- Review rules
- Autofixer rules
- Conflict rules

## Using egg
### Triggering the Pipeline
### Human Gates and Approvals
### Working with HITL Decisions

## Troubleshooting
- Common issues
- Gateway health checks (N/A for external)
- Permission errors

## Next Steps
- Link to sdlc-pipeline.md for architecture
- Link to github-automation.md for individual workflows
```

**Location**: `docs/guides/setup-external-repo.md`

**Updates Required**:
1. Add link to new guide in `docs/index.md`
2. Add link in main README.md under "Quick Start"
3. Cross-reference from `docs/guides/reusable-workflows.md`

## Open Questions

**Question 1** (multiple-choice):

What level of detail should the GitHub App setup section include?

- **Minimal**: List required permissions only; link to GitHub docs
- **Step-by-step**: Full walkthrough with each configuration option explained
- **Hybrid**: List permissions with brief explanations; link to GitHub docs for screenshots
- **Other (explain in reply)**

---

**Question 2** (multiple-choice):

Should the guide recommend a specific workflow installation approach?

- **Recommend reusable workflows**: Simpler setup, automatic updates, but depends on jwbron/egg stability
- **Recommend copying workflows**: More control, but users miss updates
- **Present both equally**: Let users choose based on their preferences
- **Other (explain in reply)**

---

**Question 3** (open-ended):

Are there specific "working with egg" scenarios (beyond triggering the pipeline and approving phases) that should be included in this guide? For example:
- Handling failed pipelines
- Restarting from a specific phase
- Canceling a pipeline run
- Multiple pipelines on the same issue

---

*Authored-by: egg*

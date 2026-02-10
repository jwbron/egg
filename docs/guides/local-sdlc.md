# Running SDLC Locally

> Run the full SDLC workflow on your local machine with Claude Code managing human interactions.

This guide explains how to use `bin/egg-sdlc` to run the SDLC pipeline locally, using the same workflow structure as CI but with file-based human interaction.

## Overview

The local SDLC workflow provides:

1. **Same phases as CI**: refine → plan → implement → PR
2. **File-based human interaction**: Review and approve via local files
3. **Claude Code integration**: Use Claude Code to execute each phase
4. **Optional act support**: Run the actual GitHub Actions workflows locally

## Quick Start

```bash
# From the repository root
cd /path/to/your-repo

# Start SDLC for an issue
bin/egg-sdlc --issue 123

# The script will:
# 1. Fetch issue details from GitHub
# 2. Set up local state in .egg-state/
# 3. Create a prompt file for the current phase
# 4. Provide instructions for running Claude Code
```

## Prerequisites

### Required

- `gh` (GitHub CLI) - authenticated with access to your repository
- `git` - for repository operations

### Optional (for --use-act mode)

- `docker` - for running containerized workflows
- `act` ([nektos/act](https://github.com/nektos/act)) - for running GitHub Actions locally

Install act:
```bash
# macOS
brew install act

# Linux
curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash

# Windows
choco install act-cli
```

## Usage

### Basic Usage

```bash
# Start from the beginning (refine phase)
bin/egg-sdlc --issue 123

# Start from a specific phase
bin/egg-sdlc --issue 123 --phase plan

# Specify repository explicitly
bin/egg-sdlc --repo owner/repo --issue 123

# Run in fully local mode (no GitHub)
bin/egg-sdlc --issue 123 --local
```

### Using nektos/act

To run the actual GitHub Actions workflows locally:

```bash
# Check dependencies first
bin/egg-sdlc --check-deps

# Run with act
bin/egg-sdlc --issue 123 --use-act
```

**Note**: Running with act requires Docker and may take longer due to container setup.

## Workflow

### Phase 1: Refine

1. Run `bin/egg-sdlc --issue 123`
2. Open Claude Code in the repository
3. Read the prompt file: `.egg-state/local/refine-prompt.md`
4. Analyze the issue and write analysis to `.egg-state/drafts/{issue}-analysis.md`
5. Review file is created at `.egg-state/local/analysis-review.md`
6. Edit the review file to approve or request changes
7. Run `bin/egg-sdlc --issue 123 --phase plan` to continue

### Phase 2: Plan

1. Read the prompt file: `.egg-state/local/plan-prompt.md`
2. Review the analysis
3. Write plan to `.egg-state/drafts/{issue}-plan.md`
4. Include YAML appendix with task definitions
5. Edit `.egg-state/local/plan-review.md` to approve
6. Continue to implement phase

### Phase 3: Implement

1. Read the contract: `egg-contract show`
2. Implement each task from the plan
3. Commit changes with descriptive messages
4. Link commits: `egg-contract add-commit --task task-1 --commit <sha>`
5. Push changes when complete

### Phase 4: PR

1. Create or update the pull request
2. Human reviews and merges via GitHub

## Human Interaction

### Review Files

After each draft phase (refine, plan), a review file is created:

```markdown
# Analysis Review - Issue #123

## Content to Review
[The draft content]

## Your Decision

To approve and advance to the plan phase, uncomment the line below:

<!-- [x] APPROVED - Advance to plan phase -->

If changes are needed, uncomment and fill in below:

<!-- [x] CHANGES REQUESTED
Feedback:
- [Your feedback here]
-->
```

Edit the file to:
- Uncomment `[x] APPROVED` to approve and advance
- Uncomment `[x] CHANGES REQUESTED` and add your feedback to request revisions

### Feedback Files

For open-ended questions:

```markdown
# Feedback Request - Issue #123

## Q1: What is the expected request volume?
> [Your answer here]

<!-- [x] FEEDBACK SUBMITTED -->
```

Answer each question and check `FEEDBACK SUBMITTED` when done.

### Decision Files

For multiple-choice questions:

```markdown
# Decision Required - Issue #123

## Question: Which caching strategy?

- [ ] Option 1: Redis
- [ ] Option 2: In-memory
- [ ] Option 3: File-based

- [ ] Other: [Your explanation]
```

Select one option by changing `[ ]` to `[x]`.

## State Management

All state is stored in `.egg-state/`:

```
.egg-state/
├── contracts/          # Contract JSON (same as CI)
│   └── 123.json
├── drafts/             # Analysis and plan documents
│   ├── 123-analysis.md
│   └── 123-plan.md
├── reviews/            # Internal review verdicts
│   └── 123-refine-review.json
└── local/              # Human interaction files (local only)
    ├── refine-prompt.md
    ├── analysis-review.md
    ├── plan-review.md
    └── feedback.md
```

## Differences from CI

| Aspect | CI (GitHub Actions) | Local (egg-sdlc) |
|--------|---------------------|------------------|
| Human interaction | GitHub issue checkboxes | Local files |
| Phase execution | Separate workflow jobs | Same Claude session |
| Review cycle | Bot comments | Edit local files |
| State isolation | Fresh context per job | Persistent session |
| Policy enforcement | Gateway sidecar | User responsibility |

## Troubleshooting

### "Missing required dependencies"

Install the missing tools:
```bash
# Check what's missing
bin/egg-sdlc --check-deps

# Install gh
brew install gh  # macOS
# or: https://cli.github.com/

# Install act
brew install act  # macOS
# or: https://github.com/nektos/act
```

### "Failed to fetch issue"

Ensure you're authenticated with GitHub:
```bash
gh auth status
gh auth login  # if needed
```

### "No draft file found"

The phase wasn't completed successfully. Check:
1. The prompt file was followed
2. Output was saved to the correct location
3. Changes were committed

## See Also

- [SDLC Pipeline Guide](sdlc-pipeline.md) - Full CI-based SDLC documentation
- [The Agentic Feedback Loop](../agentic-feedback-loop.md) - Conceptual foundation
- [ADR: SDLC Pipeline](../adr/implemented/ADR-SDLC-Pipeline.md) - Architecture decisions

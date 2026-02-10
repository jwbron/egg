# Local SDLC Workflow

When running SDLC locally (via `bin/egg-sdlc`), follow these guidelines.

## State Management

Local SDLC state is stored in `.egg-state/`:

| Directory | Purpose |
|-----------|---------|
| `contracts/` | Contract JSON files (same as CI) |
| `drafts/` | Analysis and plan documents |
| `reviews/` | Internal review verdicts |
| `local/` | Human interaction files (local only) |

## Human Interaction via Files

Instead of GitHub issue comments, local SDLC uses file-based interaction:

### Review Files

After completing a phase, create a review file in `.egg-state/local/`:

```markdown
# Analysis Review - Issue #123

## Content to Review
[Draft content here]

## Your Decision
<!-- [x] APPROVED - Advance to plan phase -->
<!-- [x] CHANGES REQUESTED
Feedback:
- [Feedback here]
-->
```

The human edits the file to approve or request changes.

### Feedback Files

For open-ended questions, create `.egg-state/local/feedback.md`:

```markdown
# Feedback Request - Issue #123

## Q1: What is the expected volume?
> [Human's answer here]

<!-- [x] FEEDBACK SUBMITTED -->
```

### Decision Files

For multiple-choice questions, create `.egg-state/local/decisions.md`:

```markdown
# Decision Required - Issue #123

## Question: Which caching strategy?

<!-- [x] Option 1: Redis -->
<!-- [ ] Option 2: In-memory -->
<!-- [ ] Option 3: File-based -->
```

## Phase Workflow

### 1. Check State

Before starting work:
```bash
# Read contract
cat .egg-state/contracts/{issue}.json | jq '.current_phase'

# Check for pending decisions
ls .egg-state/local/
```

### 2. Execute Phase

Follow the same process as CI:

**Refine Phase:**
1. Analyze the issue
2. Write analysis to `.egg-state/drafts/{issue}-analysis.md`
3. Commit the draft
4. Create review file in `.egg-state/local/analysis-review.md`

**Plan Phase:**
1. Review analysis
2. Write plan to `.egg-state/drafts/{issue}-plan.md`
3. Include YAML appendix for task extraction
4. Commit the draft
5. Create review file in `.egg-state/local/plan-review.md`

**Implement Phase:**
1. Read tasks from contract
2. Implement each task
3. Commit with descriptive messages
4. Link commits: `egg-contract add-commit --task task-1 --commit <sha>`

**PR Phase:**
1. Push all commits
2. Create PR (or update existing)
3. Wait for human review

### 3. Wait for Human

After creating a review/feedback/decision file:
1. Inform the user which file needs attention
2. Provide the file path
3. Explain what decision is needed
4. Wait for the file to be updated

### 4. Process Decision

After the human updates a file:
1. Read the decision from the file
2. Update contract state if needed
3. Advance to next phase or re-run with feedback

## Error Handling

If a phase fails:
1. Report the error clearly
2. Suggest remediation steps
3. Do not auto-retry without human confirmation

## Differences from CI

| Aspect | CI (GitHub Actions) | Local (Claude Code) |
|--------|---------------------|---------------------|
| Human interaction | GitHub issue comments | Local files |
| Phase execution | Separate workflow jobs | Same Claude session |
| Review cycle | Bot comments + checkboxes | Edit files |
| State isolation | Fresh context per job | Persistent session |

## Security Notes

- Local mode bypasses gateway policy enforcement
- Credential handling is the user's responsibility
- All changes are local until explicitly pushed

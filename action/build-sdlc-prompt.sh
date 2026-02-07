#!/bin/bash
# Build SDLC pipeline prompt with phase-specific context
#
# Usage: build-sdlc-prompt.sh <issue_number> <phase> <branch>
#
# Outputs a prompt suitable for the current pipeline phase.

set -euo pipefail

ISSUE_NUMBER="${1:-}"
PHASE="${2:-refine}"
BRANCH="${3:-}"
REPO_ROOT="${GITHUB_WORKSPACE:-$(pwd)}"

if [[ -z "$ISSUE_NUMBER" ]]; then
    echo "Error: Issue number required" >&2
    echo "Usage: $0 <issue_number> [phase] [branch]" >&2
    exit 1
fi

# Get contract state if available
CONTRACT_FILE="$REPO_ROOT/.egg/contracts/${ISSUE_NUMBER}.json"
CONTRACT_STATE=""
if [[ -f "$CONTRACT_FILE" ]]; then
    CONTRACT_STATE=$(cat "$CONTRACT_FILE")
fi

# Template paths
ANALYSIS_TEMPLATE="$REPO_ROOT/docs/templates/analysis.md"
PLAN_TEMPLATE="$REPO_ROOT/docs/templates/plan.md"

# Build orientation context
cat << EOF
# SDLC Pipeline Context

You are working on issue #${ISSUE_NUMBER} in the **${PHASE}** phase.
EOF

if [[ -n "$BRANCH" ]]; then
    echo "Branch: \`${BRANCH}\`"
fi

echo ""

# Phase-specific instructions
case "$PHASE" in
    refine)
        cat << 'EOF'
## Refine Phase Instructions

Your goal is to analyze the issue and produce an analysis document.

1. Read the issue description and any linked resources
2. Explore the codebase to understand the current behavior
3. Identify constraints and options
4. Create the analysis document at `docs/issues/{number}-analysis.md`

### Analysis Document Template

Follow this structure:
- Problem Statement: What is broken or missing
- Current Behavior: How the system works today
- Constraints: Technical, policy, and scope limits
- Options Considered: At least 2 approaches with pros/cons
- Recommended Approach: Which option and why
- Open Questions: Anything needing human input

### Exit Criteria

- Analysis document exists at `docs/issues/{number}-analysis.md`
- Recommended approach is selected
- Open questions resolved or converted to HITL decisions
- Human approval required to proceed to plan phase

### Contract CLI

Use `egg-contract` to track progress:
- `egg-contract show` - View current state
- `egg-contract add-decision --question "..."` - Request human input

EOF
        if [[ -f "$ANALYSIS_TEMPLATE" ]]; then
            echo "### Template Reference"
            echo ""
            echo '```markdown'
            cat "$ANALYSIS_TEMPLATE"
            echo '```'
        fi
        ;;

    plan)
        cat << 'EOF'
## Plan Phase Instructions

Your goal is to create an implementation plan document.

1. Reference the analysis document at `docs/issues/{number}-analysis.md`
2. Break down the recommended approach into phases and tasks
3. Define acceptance criteria for each task
4. Create the plan document at `docs/issues/{number}-plan.md`

### Plan Document Template

Follow this structure:
- Summary: One paragraph of what will be built
- Implementation Phases: With goals, tasks, dependencies, exit criteria
- Test Strategy: What tests will be added
- Rollback / Risk: What could go wrong and how to revert
- Migration: Breaking changes and migration path (if applicable)

### Task Format

Each task should have:
- Unique ID (task-1, task-2, etc.)
- Description
- Acceptance criteria (how reviewer knows it's complete)
- Files affected

### Exit Criteria

- Plan document exists at `docs/issues/{number}-plan.md`
- All phases have tasks with acceptance criteria
- Human approval required to proceed to implement phase
- Tasks will be extracted to contract JSON on approval

### Contract CLI

Use `egg-contract` to track progress:
- `egg-contract show` - View current state
- `egg-contract add-decision --question "..." --options "A,B,C"` - Request choice

EOF
        if [[ -f "$PLAN_TEMPLATE" ]]; then
            echo "### Template Reference"
            echo ""
            echo '```markdown'
            cat "$PLAN_TEMPLATE"
            echo '```'
        fi
        ;;

    implement)
        cat << 'EOF'
## Implement Phase Instructions

Your goal is to execute the tasks defined in the plan.

1. Reference the plan at `docs/issues/{number}-plan.md`
2. Implement each task in order
3. Commit changes and link to tasks via contract CLI
4. Add implementation notes for non-obvious decisions

### Workflow

For each task:
1. Implement the change
2. Commit with descriptive message
3. Link commit to task: `egg-contract add-commit --task task-X --commit $(git rev-parse HEAD)`
4. Add notes if needed: `egg-contract update-notes --task task-X --notes "..."`

### Restrictions

- You CAN: push code, create commits, add to tasks
- You CANNOT: create PRs, mark tasks complete (reviewer does this)
- You CANNOT: advance to PR phase (reviewer must approve)

### Exit Criteria

- All tasks have linked commits
- Code pushed to branch
- Reviewer will evaluate and may kick back incomplete tasks

EOF
        ;;

    pr)
        cat << 'EOF'
## PR Phase Instructions

Your goal is to create and maintain the pull request.

1. Create the PR if not already created
2. Update PR description based on implementation
3. Address any review feedback

### PR Creation

```bash
gh pr create --title "Brief description" --body "$(cat << BODY
## Summary
- Key changes

## Test Plan
- How to verify

Issue: #${ISSUE_NUMBER}

Authored-by: egg
BODY
)"
```

### Restrictions

- You CAN: create/edit PR, push fixes, respond to reviews
- You CANNOT: merge the PR (human must merge via GitHub UI)

### Exit Criteria

- PR created and linked to issue
- Human merges via GitHub UI

EOF
        ;;

    *)
        echo "Unknown phase: $PHASE" >&2
        exit 1
        ;;
esac

# Include contract state if available
if [[ -n "$CONTRACT_STATE" ]]; then
    echo ""
    echo "## Current Contract State"
    echo ""
    echo '```json'
    echo "$CONTRACT_STATE" | jq '.'
    echo '```'
fi

echo ""
echo "---"
echo "Use \`egg-contract show\` to check current state at any time."

#!/usr/bin/env bash
# build-agent-mode-design-review-prompt.sh — Review PR for agent-mode design anti-patterns
#
# This specialized review bot checks whether code follows the agent-mode design
# guidelines from docs/guides/agent-mode-design.md. It specifically looks for:
# - Pre-fetching data the agent could fetch itself
# - Requiring structured output for human-facing content
# - Post-processing pipelines that parse agent output
# - Specifying "how" instead of "what"
# - Constraints beyond what the sandbox enforces
#
# Environment variables:
#   PR_NUMBER          — Pull request number to review
#   GITHUB_REPOSITORY  — owner/repo
#   RUNNER_TEMP        — Temp directory for prompt file
#   LAST_REVIEW_COMMIT — (Optional) Commit SHA of last bot review, for re-reviews
#
# Output:
#   Sets 'prompt-file' and 'model' in $GITHUB_OUTPUT

set -euo pipefail

# Source the base framework
# shellcheck source=review-bot-base.sh
source "$(dirname "$0")/review-bot-base.sh"

# ---------------------------------------------------------------------------
# Bot configuration (used by review-bot-base.sh)
# ---------------------------------------------------------------------------

# shellcheck disable=SC2034  # Variables used by sourced script
BOT_NAME="agent-mode-design"

# shellcheck disable=SC2034  # Used by sourced script
BOT_DEFAULT_RULES='## Agent-Mode Design Review Rules

Check the code against the five guidelines from docs/guides/agent-mode-design.md:

### 1. Pre-fetching is usually wrong
Look for code that fetches data before passing it to an agent. Ask:
- Is this data the agent could fetch itself?
- Is the pre-fetched data being truncated or summarized?
- Could the agent need more context than what was pre-fetched?

Exceptions: Lightweight metadata (PR number, repo name) as orientation context is fine.

### 2. Do not specify output formats unless there is a downstream consumer
Look for:
- JSON schemas required for agent output
- Specific template formats for human-facing output
- Structured output that goes to humans (PR comments, reviews)

Machine-readable output is fine when another system parses it.

### 3. Post-processing pipelines are a code smell
Look for:
- Scripts that parse agent output to take action
- JSON parsing of agent responses
- Code that posts to GitHub/external services based on parsed agent output

Ask: could the agent just take that action directly?

### 4. Instructions should specify what, not how
Look for prompts that:
- Give procedural step-by-step instructions
- Specify output field names or schema
- Tell the agent how to structure its work

Good prompts specify outcomes and domain context, not procedures.

### 5. Let the agent explore and use judgment
Look for:
- Overly prescriptive checklists in general-purpose workflows
- Attempts to anticipate everything the agent might need
- Constraints that are not technically enforced by the sandbox

If a constraint is security-critical, it should be in the sandbox, not the prompt.

### Anti-patterns to flag:
- Building prompts with large embedded data (diffs, file contents)
- JSON.parse() or similar on agent output
- Shell scripts that process agent output and take action
- Long checklists in review prompts
- "Don'\''t do X" instructions for security-sensitive things (should be in sandbox)'

# shellcheck disable=SC2034  # Used by sourced script
BOT_TASK_DESCRIPTION='Review this PR for agent-mode design anti-patterns.

Read docs/guides/agent-mode-design.md to understand the guidelines. Then examine
the changed code to identify:

1. **Pre-fetching anti-patterns**: Code that fetches data for agents instead of
   letting agents fetch what they need.

2. **Structured output anti-patterns**: Requiring JSON or templates for output
   that goes to humans.

3. **Post-processing anti-patterns**: Scripts that parse agent output to take action.

4. **Procedural instruction anti-patterns**: Prompts that specify how instead of what.

5. **Unnecessary constraints**: Prompt-level restrictions for things the sandbox
   should enforce.

Only comment on agent-mode design issues. Skip general code quality, style, or
correctness issues — those are covered by the general review bot.'

# shellcheck disable=SC2034,SC2016  # Used by sourced script; backticks intentional
BOT_DEFAULT_CONVENTIONS='Post your review using `gh pr review`. Use:
- --request-changes for clear violations of agent-mode design guidelines
- --comment for suggestions or borderline cases
- --approve if the code follows agent-mode design principles

Reference docs/guides/agent-mode-design.md when explaining issues. Be specific
about which guideline is violated and why. Sign your review with: — Authored by egg'

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

build_bot_prompt

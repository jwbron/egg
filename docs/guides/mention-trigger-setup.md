# Setting Up @mention Triggers for egg

This guide explains how to configure a GitHub Actions workflow that triggers egg when a bot is mentioned in issues or pull requests.

## Overview

The @mention trigger lets authorized users invoke egg by mentioning a bot username (e.g., `@james-in-a-box`) in:

- Issue comments
- PR conversation comments
- Inline PR review comments (on diffs)
- New issue descriptions

egg receives a context-rich prompt with the issue/PR details, recent conversation, and the user's request, then works autonomously to complete the task.

## Prerequisites

### Required Secrets

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_OAUTH_TOKEN` | Anthropic OAuth token for Claude API access |
| `BOT_GITHUB_TOKEN` | GitHub App token for the bot identity (for posting comments, pushing code) |

### GitHub App Setup

The bot GitHub App needs these permissions:
- **Contents**: Read & Write (push commits)
- **Pull requests**: Read & Write (create/update PRs, post comments)
- **Issues**: Read & Write (post comments, add reactions)

## Files

The implementation consists of two files:

### `.github/workflows/on-mention.yml`

The workflow file that listens for GitHub events and orchestrates the response. It:

1. Filters events to only those mentioning the bot from authorized users
2. Adds a reaction to acknowledge the mention
3. Checks out the correct branch (PR head branch or main)
4. Builds a context-rich prompt using `build-mention-prompt.sh`
5. Runs the egg action
6. Posts a summary comment with a link to the workflow run

### `action/build-mention-prompt.sh`

A reusable bash script that constructs prompts from GitHub event payloads. It:

- Parses `$GITHUB_EVENT_PATH` with `jq` to extract event data
- Uses `gh api` to fetch additional context (comment threads, PR files)
- Outputs a structured prompt to `$GITHUB_OUTPUT`
- Truncates long content to stay within prompt limits (~50K chars)

## Customization

### Changing the Bot Username

1. In `on-mention.yml`, replace `@james-in-a-box` in the `if:` conditions
2. Update the `BOT_USERNAME` env var in the "Build prompt" step
3. Update the `bot-username` input in the "Run egg" step

### Changing Authorized Users

The workflow uses `github.event.sender.login == 'jwbron'` to restrict who can trigger egg.

**Single user:**
```yaml
github.event.sender.login == 'your-username'
```

**Multiple users:**
```yaml
contains(fromJSON('["user1","user2","user3"]'), github.event.sender.login)
```

**Team membership** (requires an API call in a prior step):
```yaml
- name: Check team membership
  id: auth
  run: |
    gh api orgs/YOUR_ORG/teams/YOUR_TEAM/memberships/${{ github.event.sender.login }} \
      && echo "authorized=true" >> "$GITHUB_OUTPUT" \
      || echo "authorized=false" >> "$GITHUB_OUTPUT"
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Adjusting Timeout

The egg action defaults to 30 minutes. Override with:
```yaml
- uses: jwbron/egg@main
  with:
    timeout: "60"  # 60 minutes
```

### Concurrency

The default configuration groups concurrent runs by issue/PR number and does not cancel in-progress runs. To change this:

```yaml
concurrency:
  group: egg-mention-${{ github.event.issue.number || github.event.pull_request.number }}
  cancel-in-progress: true  # Cancel previous run if new mention arrives
```

## Security Considerations

- **Authorization**: Always restrict who can trigger egg. Without the sender check, anyone who can comment could invoke it.
- **Loop prevention**: The workflow filters out the bot's own username to prevent infinite loops where egg's comments trigger itself.
- **Concurrency**: Runs are serialized per issue/PR to prevent race conditions when egg pushes code.
- **Prompt injection**: The prompt includes user-provided content (comment bodies, issue descriptions). egg's system prompt and sandbox provide defense in depth.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Workflow doesn't trigger | Check the `if:` conditions — username, bot mention, and event type must all match |
| Reaction not added | Verify `BOT_GITHUB_TOKEN` secret is set and the app has Issues/PRs write permission |
| egg can't push to PR | Ensure the checkout step uses the PR head ref and the bot has Contents write permission |
| Prompt too large | Adjust `MAX_BODY_CHARS` and `MAX_COMMENT_CHARS` in `build-mention-prompt.sh` |

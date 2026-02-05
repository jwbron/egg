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

Configure these in **Settings > Secrets and variables > Actions**:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_OAUTH_TOKEN` | Anthropic OAuth token for Claude API access |
| `BOT_APP_ID` | GitHub App ID (numeric) for the bot identity |
| `BOT_APP_PRIVATE_KEY` | GitHub App private key (PEM format) |
| `BOT_APP_INSTALLATION_ID` | GitHub App installation ID for this repo/org |

### GitHub App Setup

The bot GitHub App needs these permissions:
- **Contents**: Read & Write (push commits)
- **Pull requests**: Read & Write (create/update PRs, post comments)
- **Issues**: Read & Write (post comments, add reactions)

### Finding the App credentials

1. **App ID**: Go to the App's settings page at `https://github.com/settings/apps/<app-name>` — the App ID is displayed near the top.
2. **Private key**: On the same settings page, scroll to "Private keys" and click "Generate a private key". Save the downloaded `.pem` file contents as the `BOT_APP_PRIVATE_KEY` secret.
3. **Installation ID**: Go to `https://github.com/settings/installations`, click the App installation, and note the numeric ID in the URL (e.g., `https://github.com/settings/installations/12345678` → ID is `12345678`).

## How Authentication Works

The egg gateway generates **short-lived GitHub tokens** (1-hour TTL) from your App credentials at runtime. It:

1. Creates a JWT signed with the App's private key
2. Exchanges the JWT for an installation access token via GitHub's API
3. Caches the token in-memory and auto-refreshes 15 minutes before expiry

This means no long-lived tokens are stored — the gateway generates fresh tokens on demand.

For workflow steps that run outside the egg action (reactions, checkout, prompt building), the `actions/create-github-app-token` action generates a separate short-lived token from the same App credentials.

## Files

The implementation consists of two files:

### `.github/workflows/on-mention.yml`

The workflow file that listens for GitHub events and orchestrates the response. It:

1. Filters events to only those mentioning the bot from authorized users
2. Generates a short-lived bot token via `actions/create-github-app-token`
3. Adds a reaction to acknowledge the mention
4. Checks out the correct branch (PR head branch or main)
5. Builds a context-rich prompt using `build-mention-prompt.sh`
6. Runs the egg action (which generates its own tokens inside the gateway)
7. Posts a summary comment with a link to the workflow run

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
- **Short-lived tokens**: The gateway generates tokens with 1-hour TTL and auto-refreshes them. No long-lived PATs are stored.
- **Prompt injection**: The prompt includes user-provided content (comment bodies, issue descriptions). egg's system prompt and sandbox provide defense in depth.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Workflow doesn't trigger | Check the `if:` conditions — username, bot mention, and event type must all match |
| Reaction not added | Verify `BOT_APP_ID` and `BOT_APP_PRIVATE_KEY` secrets are set and the app has Issues/PRs write permission |
| "Generate bot token" step fails | Verify all three `BOT_APP_*` secrets are correctly set — check App ID, private key PEM format, and installation ID |
| egg can't push to PR | Ensure the checkout step uses the PR head ref and the bot has Contents write permission |
| Gateway logs "Token refresher not configured" | Check that `BOT_APP_ID`, `BOT_APP_PRIVATE_KEY`, and `BOT_APP_INSTALLATION_ID` are all passed to the action |
| Prompt too large | Adjust `MAX_BODY_CHARS` and `MAX_COMMENT_CHARS` in `build-mention-prompt.sh` |

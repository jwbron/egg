# SDLC Pipeline Initialization

Initialize and start an SDLC pipeline. Supports two modes:

- **Issue mode** (`/sdlc <issue_number>`): GitHub-issue-driven pipeline with full remote integration
- **Local mode** (`/sdlc` with no args): Prompt-driven pipeline that runs entirely locally — no GitHub interaction

Both modes create a pipeline in the orchestrator, which then spawns sandbox containers to execute each phase as a DAG.

## Usage

```
/sdlc [<issue_number>] [--repo <owner/repo>]
```

## Workflow

When invoked, determine the mode based on arguments and execute the appropriate flow.

### Step 1: Validate Environment

Check that the orchestrator is accessible:

```bash
curl -s http://egg-orchestrator:9849/api/v1/health | jq -r '.status'
```

If the orchestrator is not running, inform the user:
- "The egg-orchestrator service is not running"
- Suggest: `docker-compose up -d orchestrator`

### Step 2: Determine Mode

- If an `issue_number` argument is provided → **Issue mode** (go to Step 3a)
- If no arguments → **Local mode** (go to Step 3b)

---

### Issue Mode (Step 3a): GitHub-Issue-Driven Pipeline

This is the existing flow for issue-driven development.

#### Parse Arguments

Extract from the user's command:
- `issue_number`: Required - the GitHub issue number (e.g., 496)
- `repo`: Optional - the repository in owner/repo format (defaults to current repo)

#### Get Repository Info

```bash
# Get current repo if not provided
gh repo view --json nameWithOwner -q '.nameWithOwner'

# Get current branch
git branch --show-current
```

#### Create and Start Pipeline

```bash
curl -s -X POST http://egg-orchestrator:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": <issue_number>,
    "repo": "<owner/repo>",
    "branch": "egg/issue-<issue_number>",
    "mode": "issue"
  }' | jq .

curl -s -X POST http://egg-orchestrator:9849/api/v1/pipelines/issue-<issue_number>/start \
  -H "Content-Type: application/json" -d '{}' | jq .
```

#### Report Status

Display the pipeline status and provide monitoring commands.

**Example response:**
```
# SDLC Pipeline Initialized

Pipeline: issue-496
Repository: anthropics/egg
Branch: egg/issue-496
Status: running

The orchestrator is now running the pipeline. Containers will be spawned
for each phase (refine → plan → implement → pr).

Monitor progress:
- `egg-contract show --issue 496`
- `curl http://egg-orchestrator:9849/api/v1/pipelines/issue-496 | jq .`
```

---

### Local Mode (Step 3b): Prompt-Driven Pipeline

This flow creates a pipeline that the orchestrator runs entirely locally — same containers, same DAG execution, but the gateway blocks all GitHub interaction (push, PR, issue comments).

#### Prompt the User

Ask the user what they want to build. Use a conversational approach:

1. **Ask for the task description**: "What would you like to build or change? Describe the feature, bug fix, or task."
2. **Ask 1-2 clarifying questions** based on the response to refine scope and requirements. For example:
   - "What's the expected behavior?"
   - "Are there specific files or areas of the codebase this should touch?"
   - "Should this include tests?"

Combine the user's answers into a single refined prompt.

#### Create and Start Pipeline

```bash
curl -s -X POST http://egg-orchestrator:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "local",
    "prompt": "<refined user prompt>"
  }' | jq .
```

Save the returned `pipeline_id` (e.g., `local-a1b2c3d4`). Then start it:

```bash
curl -s -X POST "http://egg-orchestrator:9849/api/v1/pipelines/<pipeline_id>/start" \
  -H "Content-Type: application/json" -d '{}' | jq .
```

#### Report Status

**Example response:**
```
# Local SDLC Pipeline Started

Pipeline: local-a1b2c3d4
Mode: local (no GitHub interaction)
Status: running

The orchestrator is now running the pipeline. Containers will be spawned
for each phase (refine → plan → implement).

Local pipelines skip the PR phase. When the pipeline completes, you can
push and create a PR manually:

  git push origin <branch>
  gh pr create --title "..." --body "..."

Monitor progress:
  curl http://egg-orchestrator:9849/api/v1/pipelines/<pipeline_id> | jq .
```

## Error Handling

| Error | Action |
|-------|--------|
| Orchestrator unreachable | Check docker-compose status |
| Pipeline already exists | Show existing pipeline status |
| Issue not found (issue mode) | Verify issue number and repo |
| Authentication failed | Check gateway session token |

## Related Commands

- `/coder-mode`: Enter coder agent mode manually
- `/tester-mode`: Enter tester agent mode manually
- `/show-metrics`: View agent activity metrics
- `egg-contract show`: View contract state

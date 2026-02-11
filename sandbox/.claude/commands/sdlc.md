# SDLC Pipeline Initialization

Initialize and start an SDLC pipeline for an issue. This command connects to the egg-orchestrator to create a new pipeline and begin automated orchestration.

## Usage

```
/sdlc <issue_number> [--repo <owner/repo>]
```

## What This Command Does

1. **Check prerequisites**: Verify orchestrator is running and issue exists
2. **Create pipeline**: Initialize a new pipeline in the orchestrator
3. **Start orchestration**: Begin the automated SDLC workflow
4. **Monitor progress**: Show initial status and how to track progress

## Workflow

When invoked, execute these steps:

### Step 1: Validate Environment

Check that the orchestrator is accessible:

```bash
curl -s http://egg-orchestrator:9849/api/v1/health | jq -r '.status'
```

If the orchestrator is not running, inform the user:
- "The egg-orchestrator service is not running"
- Suggest: `docker-compose up -d orchestrator`

### Step 2: Parse Arguments

Extract from the user's command:
- `issue_number`: Required - the GitHub issue number (e.g., 496)
- `repo`: Optional - the repository in owner/repo format (defaults to current repo)

If issue_number is missing, prompt: "Please provide an issue number: `/sdlc 123`"

### Step 3: Get Repository Info

```bash
# Get current repo if not provided
gh repo view --json nameWithOwner -q '.nameWithOwner'

# Get current branch
git branch --show-current
```

### Step 4: Create Pipeline

Make a POST request to create the pipeline:

```bash
curl -s -X POST http://egg-orchestrator:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d '{
    "issue_number": <issue_number>,
    "repo": "<owner/repo>",
    "branch": "egg/issue-<issue_number>"
  }' | jq .
```

### Step 5: Start Pipeline

If creation succeeded, start the pipeline:

```bash
curl -s -X POST http://egg-orchestrator:9849/api/v1/pipelines/issue-<issue_number>/start | jq .
```

### Step 6: Report Status

Display the pipeline status:

```bash
curl -s http://egg-orchestrator:9849/api/v1/pipelines/issue-<issue_number> | jq .
```

Provide the user with:
- Pipeline ID
- Current phase
- Next steps

## Example Interaction

User: `/sdlc 496`

Response:
```
# SDLC Pipeline Initialized

Pipeline: issue-496
Repository: anthropics/egg
Branch: egg/issue-496
Status: pending

## Next Steps

The orchestrator will:
1. Create sandbox containers for each agent role
2. Execute coder agent first
3. Run tester and documenter in parallel
4. Run integrator to create PR

Monitor progress with:
- `egg-contract show --issue 496`
- `curl http://egg-orchestrator:9849/api/v1/pipelines/issue-496`

View decisions requiring input:
- `curl http://egg-orchestrator:9849/api/v1/pipelines/issue-496/decisions`
```

## Error Handling

| Error | Action |
|-------|--------|
| Orchestrator unreachable | Check docker-compose status |
| Pipeline already exists | Show existing pipeline status |
| Issue not found | Verify issue number and repo |
| Authentication failed | Check gateway session token |

## Related Commands

- `/coder-mode`: Enter coder agent mode manually
- `/tester-mode`: Enter tester agent mode manually
- `/show-metrics`: View agent activity metrics
- `egg-contract show`: View contract state

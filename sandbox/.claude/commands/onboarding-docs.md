# Onboarding Documentation Generator

Generate comprehensive documentation for an entire repository by running the
onboarding doc prompt builder through the SDLC pipeline.

## Usage

```
/onboarding-docs [<owner/repo>]
```

## Workflow

### Step 1: Validate Environment

Check that the orchestrator is accessible:

```bash
curl -s http://egg-orchestrator:9849/api/v1/health | jq -r '.status'
```

If the orchestrator is not running, inform the user:
- "The egg-orchestrator service is not running"
- Suggest: `docker-compose up -d orchestrator`

### Step 2: Determine Repository

- If `<owner/repo>` was provided as an argument, use it as `GITHUB_REPOSITORY`.
- If no argument was provided, **ask the user** which repository to document
  (in `owner/repo` format, e.g. `jwbron/egg`).

### Step 3: Ensure Repository Is Cloned

Check if the repository already exists locally under `~/repos/`:

```bash
# Derive the repo directory name from GITHUB_REPOSITORY
repo_name="${GITHUB_REPOSITORY#*/}"
repo_path="$HOME/repos/${repo_name}"

if [ -d "$repo_path/.git" ]; then
    echo "Repository found at $repo_path"
else
    echo "Repository not found locally. Cloning..."
    git clone "https://github.com/${GITHUB_REPOSITORY}.git" "$repo_path"
fi
```

### Step 4: Create and Start SDLC Pipeline

Create a local-mode pipeline via the orchestrator with an onboarding documentation prompt:

```bash
# Create pipeline
curl -s -X POST http://egg-orchestrator:9849/api/v1/pipelines \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg repo "$GITHUB_REPOSITORY" '{
    "mode": "local",
    "repo": $repo,
    "prompt": "Generate comprehensive onboarding documentation for this repository. Survey the codebase structure, identify key components, and create documentation that helps new contributors understand the project."
  }')" | jq .
```

Save the returned `pipeline_id`. Then start it:

```bash
curl -s -X POST "http://egg-orchestrator:9849/api/v1/pipelines/<pipeline_id>/start" \
  -H "Content-Type: application/json" -d '{}' | jq .
```

### Step 6: Stream Live Progress

After starting the pipeline, **immediately** run the pipeline watcher:

```bash
egg-pipeline-watch <pipeline_id>
```

This streams live updates as the pipeline progresses through each phase. The
watcher exits automatically when the pipeline reaches a terminal state.

## Options

You can pass additional environment variables when building the prompt:

| Variable | Description |
|----------|-------------|
| `DRY_RUN=true` | Survey only — describe what docs would be created without making changes |
| `INCLUDE_PATTERN="gateway/**"` | Limit scope to files matching this glob |
| `EXCLUDE_DIRS="legacy,tmp"` | Additional directories to skip (comma-separated) |

Example with scope limiting — include in the prompt:

```
Focus only on files matching the pattern: gateway/**
```

## Error Handling

| Error | Action |
|-------|--------|
| Orchestrator unreachable | Check docker-compose status |
| Repository not found | Verify owner/repo format and access permissions |
| Pipeline already exists | Show existing pipeline status |
| Build script fails | Check GITHUB_REPOSITORY is set correctly |

## Related Commands

- `/sdlc`: General SDLC pipeline initialization
- `/documenter-mode`: Enter documenter agent mode manually

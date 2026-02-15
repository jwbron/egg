# Onboarding Documentation Generator

Generate comprehensive documentation for an entire repository by running the
onboarding doc prompt through the SDLC pipeline via `egg-sdlc`.

## Usage

```
/onboarding-docs [<repo_dir>]
```

## Workflow

### Step 1: Determine Repository

- If `<repo_dir>` was provided as an argument (e.g. `egg`), use it.
- If no argument was provided, **ask the user** which repository to document
  (directory name under `~/repos/`, e.g. `egg`).

### Step 2: Ensure Repository Exists

Check if the repository exists locally under `~/repos/`:

```bash
repo_path="$HOME/repos/${repo_dir}"

if [ -d "$repo_path/.git" ]; then
    echo "Repository found at $repo_path"
else
    echo "Repository not found at $repo_path"
    # Ask the user if they want to clone it
fi
```

### Step 3: Run SDLC Pipeline

Launch the pipeline using `egg-sdlc` with the `--prompt` and `--repo` flags:

```bash
egg-sdlc -r <repo_dir> --prompt "Generate comprehensive onboarding documentation for this repository. Survey the codebase structure, identify key components, and create documentation that helps new contributors understand the project."
```

This handles orchestrator health checks, pipeline creation, DAG visualization,
and HITL checkpoints automatically.

## Customizing the Prompt

You can refine the prompt to narrow scope. Examples:

- Focus on a subdirectory: append `\n\nScope: gateway/**` to the prompt
- Dry run (survey only): prepend `Survey only — describe what docs would be
  created without making changes.` to the prompt
- Exclude directories: append `\n\nExclude directories: legacy, tmp`

## Error Handling

| Error | Action |
|-------|--------|
| Orchestrator unreachable | `egg-sdlc` reports this automatically |
| Repository not found | Verify the directory exists under `~/repos/` |

## Related Commands

- `/sdlc`: General SDLC pipeline initialization
- `/documenter-mode`: Enter documenter agent mode manually

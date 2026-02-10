# bin/

Entry points for egg commands.

## Commands

- `egg` - Start/manage egg sandbox container
- `egg-sdlc` - Run the SDLC pipeline locally with Claude Code
- `setup-gateway` - Install gateway sidecar service

## egg-sdlc

Run the full SDLC workflow locally, using the same workflow structure as CI but with:
- Claude Code managing the agent sessions
- Local files for human interaction (instead of GitHub issue comments)
- Optional nektos/act integration for running GitHub Actions locally

### Usage

```bash
# Start SDLC for an issue
bin/egg-sdlc --issue 123

# Start from a specific phase
bin/egg-sdlc --issue 123 --phase plan

# Use nektos/act to run the actual GitHub Actions workflows
bin/egg-sdlc --issue 123 --use-act

# Check dependencies
bin/egg-sdlc --check-deps
```

### Requirements

- `gh` (GitHub CLI) - for fetching issue data
- `git` - for repository operations
- `docker` - required if using `--use-act`
- `act` (nektos/act) - required if using `--use-act`

### Local File Interaction

Human interaction happens via files in `.egg-state/local/`:

| File | Purpose |
|------|---------|
| `analysis-review.md` | Review/approve analysis (refine phase) |
| `plan-review.md` | Review/approve plan (plan phase) |
| `feedback.md` | Answer open-ended questions |
| `decisions.md` | Make multiple-choice decisions |

Edit these files to provide input, then save. The workflow will detect your changes.

## Note

`egg` and `setup-gateway` are symlinks to files in `gateway/` and `sandbox/`.
`egg-sdlc` is a standalone script in this directory.

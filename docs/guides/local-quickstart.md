# Local Quickstart: egg + SDLC Pipeline

Get egg running locally with the full SDLC pipeline using a GitHub Personal Access Token (PAT).

## Prerequisites

- **Docker** installed and running (Docker Desktop or Docker Engine with Compose v2)
- **GitHub CLI** (`gh`) installed and authenticated: `gh auth login`
- **Anthropic credentials**: either a Claude OAuth token or API key
- **GitHub PAT**: a fine-grained Personal Access Token with Contents (R/W), Pull requests (R/W), Issues (R/W)

## 1. Clone and setup

```bash
git clone https://github.com/jwbron/egg.git
cd egg
pip install -e ./sandbox   # install the egg CLI
egg --setup                # interactive setup wizard
```

The setup wizard will prompt for:
- Anthropic credentials (OAuth token recommended — run `claude auth status --json | jq -r '.oauthToken'`)
- GitHub App credentials — **skip this** if using a PAT only (press Enter through the prompts)
- Your GitHub username and repos to configure

After setup, your config lives at `~/.config/egg/`.

## 2. Configure for PAT authentication

Edit `~/.config/egg/secrets.env`:

```bash
# Anthropic (choose one)
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...   # from claude auth status
# ANTHROPIC_API_KEY=sk-ant-api03-...       # alternative

# GitHub PAT
GITHUB_USER_TOKEN=github_pat_...

# Gateway policy (required)
GATEWAY_BOT_NAME="egg"
GATEWAY_BOT_BRANCH_PREFIX="egg"
GATEWAY_TRUSTED_USERS="your-github-username"
```

Edit `~/.config/egg/repositories.yaml` to set `auth_mode: user` for your repos:

```yaml
github_username: your-github-username
bot_username: egg
writable_repos:
  - your-username/your-repo
default_reviewer: your-github-username

repo_settings:
  your-username/your-repo:
    auth_mode: user    # use your PAT instead of a GitHub App

local_repos:
  paths:
    - /path/to/your-repo   # absolute path on host
```

Setting `auth_mode: user` tells the gateway to use `GITHUB_USER_TOKEN` for git/gh operations, attributing commits and PRs to your personal GitHub account.

## 3. Build and start

```bash
egg --compose --build   # build gateway + orchestrator images (first time only)
egg                     # start sandbox session
```

On subsequent runs, `egg` starts the gateway and orchestrator automatically. Use `--compose --build` again only when the gateway or orchestrator code changes.

## 4. Using the SDLC pipeline

### Option A: Local pipeline (prompt-driven, no GitHub interaction)

Inside the sandbox, run:

```
/sdlc
```

With no arguments, this starts a **local pipeline**. The agent will:

1. Ask what you want to build
2. Ask 1-2 clarifying questions
3. Create a local pipeline in the orchestrator
4. Run through refine → plan → implement phases entirely locally

No code is pushed, no PR is created, and no GitHub issues are touched. The gateway enforces this — `git push` and `gh` commands return 403 in local mode.

When the pipeline completes, push and create a PR manually:

```bash
git push origin egg/my-feature
gh pr create --title "Add feature" --body "..."
```

**Local pipeline phases:**

| Phase | What happens |
|-------|-------------|
| **Refine** | Agent analyzes requirements from your prompt |
| **Plan** | Agent creates an implementation plan |
| **Implement** | Agent writes code locally (terminal phase) |

### Option B: Issue pipeline (GitHub-driven)

```
/sdlc <issue_number>
```

This creates an issue-driven pipeline with full GitHub integration. You can also specify a repo:

```
/sdlc 123 --repo your-username/your-repo
```

**Issue pipeline phases:**

| Phase | What happens | Human action needed |
|-------|-------------|-------------------|
| **Refine** | Agent analyzes the issue, produces a requirements document | Review analysis, approve via checkbox comment on the issue |
| **Plan** | Agent creates an implementation plan with tasks | Review plan, approve via checkbox comment |
| **Implement** | Agent writes code, pushes to `egg/issue-<N>` branch, opens a draft PR | Review code feedback cycles automatically; CI runs |
| **PR** | PR is marked ready for review | You review and merge via GitHub UI |

### Monitor progress

```bash
# From inside the sandbox
egg-contract show                    # view contract state
curl http://egg-orchestrator:9849/api/v1/pipelines/issue-123 | jq .
curl http://egg-orchestrator:9849/api/v1/pipelines/local-a1b2c3d4 | jq .

# From your host (issue pipelines only)
gh issue view 123                    # see comments + phase labels
gh pr list --search "egg/issue-123"  # find the draft PR
```

### Human-in-the-loop decisions

For issue pipelines, at phase boundaries the pipeline posts a GitHub issue comment with a checkbox. Check the box to approve and advance to the next phase. There's a 30-second debounce to prevent accidental approvals.

The agent may also post decision comments (multiple-choice) or feedback requests (open-ended) when it needs human input mid-phase.

## What happens in GitHub (issue pipelines only)

Here's what the issue pipeline creates and when:

| Artifact | When | Details |
|----------|------|---------|
| **Issue comments** | Each phase | Analysis documents, plans, approval checkboxes, decision prompts |
| **Issue labels** | Phase transitions | `sdlc:refine`, `sdlc:plan`, `sdlc:implement`, `sdlc:pr` |
| **Feature branch** | Implement phase | `egg/issue-<N>` — pushed to your repo |
| **Draft PR** | Implement phase | Links to the issue, contains all implementation commits |
| **PR review comments** | Implement phase | Automated code review feedback (line-level) |
| **PR ready for review** | PR phase | Draft flag removed when all checks pass |

**Nothing is merged automatically.** The gateway enforces merge blocking — only humans can merge PRs via the GitHub UI.

Local pipelines do not interact with GitHub at all — the gateway blocks push and gh operations in local mode.

The pipeline stores its internal state in `.egg-state/` on the feature branch (not on main). This includes the contract JSON, draft documents, and review verdicts.

## Common operations

```bash
egg                        # start interactive session
egg --compose --build      # rebuild after code changes to gateway/orchestrator
egg --compose --down       # stop gateway + orchestrator
egg --private              # run in private mode (Anthropic API only, private repos)
egg --public               # run in public mode (full internet, public repos — default)
egg --exec "make test"     # run a command in an ephemeral container
```

## Troubleshooting

**Stale containers/networks**: egg automatically cleans up stale Docker resources from previous runs. If you see network conflicts, run `egg --compose --down` then `egg`.

**Permission denied on repos**: The gateway and orchestrator need to run as your host UID. This is handled automatically via `HOST_UID`/`HOST_GID` in the entrypoint scripts. If issues persist, rebuild with `egg --compose --build`.

**Orchestrator won't start ("must not run as root")**: The orchestrator has a safety check that refuses to run as root, preventing git refs from being created with root ownership. This error means `HOST_UID`/`HOST_GID` are not set in your environment. The `egg` CLI sets these automatically; if running via Docker Compose directly, ensure your `.env` file includes:
```bash
HOST_UID=$(id -u)
HOST_GID=$(id -g)
```

**Token refresher warnings**: If you see "No valid token available from token refresher", this means no GitHub App is configured. This is expected in PAT-only mode — ensure `auth_mode: user` is set for your repos so the gateway uses your PAT.

**Orchestrator git errors**: The orchestrator stores pipeline state in git. `EGG_REPO_PATH` can be either a single git repository or a parent directory containing multiple repositories (the orchestrator will scan subdirectories automatically).

**Empty repository in sandbox containers**: The orchestrator creates isolated git worktrees for each pipeline via the gateway's worktree API. If containers have empty working trees:
1. Ensure `HOST_HOME` in your `.env` file matches your actual home directory: `echo $HOME`
2. Verify the gateway is healthy: `curl http://egg-gateway:9848/api/v1/health`
3. Check orchestrator logs for worktree creation errors: `docker logs egg-orchestrator | grep -i worktree`

**Note**: As of PR #569, worktrees use a bind mount instead of a Docker named volume. If upgrading from an older version, remove the old volume: `docker volume rm egg-worktrees`

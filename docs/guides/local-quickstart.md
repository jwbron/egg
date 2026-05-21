# Local Quickstart: egg + SDLC Pipeline

Get egg running locally with the full SDLC pipeline using a GitHub Personal Access Token (PAT).

## Prerequisites

- **k3s** installed and running: `make k3s-setup` (see [Deployment Guide](deployment.md#k3s-installation) for details)
  - **Linux**: k3s runs natively
  - **macOS**: k3s requires a Linux VM — use [Lima](https://lima-vm.io/) or [Rancher Desktop](https://rancherdesktop.io/)
- **GitHub CLI** (`gh`) installed and authenticated: `gh auth login`
- **Anthropic credentials**: either a Claude OAuth token or API key
- **GitHub PAT**: a fine-grained Personal Access Token with Contents (R/W), Pull requests (R/W), Issues (R/W)

## 1. Clone and setup

```bash
git clone https://github.com/jwbron/egg.git
cd egg
bin/egg-deploy init        # generates ~/.config/egg/launcher-secret
```

`bin/egg-deploy init` is the non-interactive replacement for the
removed `egg --setup` wizard (see
[#1762](https://github.com/jwbron/egg/issues/1762)). It creates
`~/.config/egg/launcher-secret`; the `lifecycle-secret` required for
HITL resolve / pipeline CRUD / phase-control endpoints is generated
the same way (see [Deployment Guide](deployment.md) for the exact
path). Fill in the rest of `~/.config/egg/` by hand.

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

`make deploy` reads `local_repos.paths` and queries each repo's `origin` remote URL to auto-derive `EGG_HOST_REPO_MAP` (the JSON map the orchestrator uses for hostPath mounts). No manual editing of Kubernetes overlays is required. `envsubst` (from GNU gettext: `dnf install gettext` / `brew install gettext`) must be installed for `make deploy` to work.

Setting `auth_mode: user` tells the gateway to use `GITHUB_USER_TOKEN` for git/gh operations, attributing commits and PRs to your personal GitHub account.

## 3. Build and deploy

```bash
make build              # build gateway, orchestrator, and sandbox images
make k3s-import         # import images into k3s containerd store
make k3s-secrets        # create k8s Secrets from ~/.config/egg/
make deploy             # deploy gateway + orchestrator to k3s (idempotent)
kubectl get pods -n egg-system  # verify pods are running
egg --public            # start sandbox session
```

`make build` builds the Docker images. `make k3s-import` imports them into k3s's containerd image store (without this, pods will get `ImagePullBackOff`). `make deploy` applies the Kustomize manifests — it is idempotent and can be re-run after code changes to update the running deployment.

## 4. Using the SDLC pipeline

### Option A: Prompt-driven pipeline (no GitHub interaction)

Inside the sandbox, run:

```
/sdlc
```

With no arguments, this starts a **prompt-driven pipeline**. The agent will:

1. Ask what you want to build
2. Ask 1-2 clarifying questions
3. Create a pipeline in the orchestrator
4. Run through refine → plan → implement → PR phases

During refine and plan phases, the gateway restricts pushes to state files and blocks PR operations. During the PR phase, the orchestrator auto-creates the PR using metadata from the plan, commit log, and diff stats — no agent is spawned.

**Pipeline phases:**

| Phase | What happens |
|-------|-------------|
| **Refine** | Agent analyzes requirements from your prompt |
| **Plan** | Agent creates an implementation plan |
| **Implement** | Agent writes code locally |
| **PR** | Orchestrator auto-creates a draft PR (terminal phase) |

### Option B: Issue pipeline (GitHub-driven)

```
/sdlc -r <repo_dir> -i <issue_number>
```

This creates an issue-driven pipeline with full GitHub integration. The `-r/--repo` flag specifies the repository directory name under `~/repos/` (e.g., `egg`), and `-i/--issue` specifies the GitHub issue number. Example:

```
/sdlc -r your-repo -i 123
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
curl http://egg-orchestrator:9849/api/v1/pipelines/pipeline-85170faf | jq .

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

Prompt-driven pipelines create PRs during the PR phase but do not interact with GitHub issues. The orchestrator auto-creates the PR via `GatewayClient.create_pr()` — no agent is spawned during the PR phase.

The pipeline stores its internal state in `.egg-state/` on the feature branch (not on main). This includes the contract JSON, draft documents, and review verdicts.

## Common operations

```bash
# The interactive `egg` / `egg --public` / `egg --private` CLI was
# removed in #1762. Drive agent work through the MCP server instead:
#   submit_task(issue_number=..., repo="...")

bin/egg-deploy up          # apply k8s manifests, wait for readiness
bin/egg-deploy status      # health + endpoint summary
bin/egg-deploy down        # tear down the deployment
make build && make k3s-import && make deploy  # rebuild images and redeploy after code changes
make k3s-teardown          # remove k3s and all deployed resources
```

## Troubleshooting

**Claude binary not found**: If a sandbox job exits with `Claude Code CLI not found in PATH`, rebuild the sandbox image: `make build && make k3s-import && make deploy`. (The legacy `egg --reset` flag was removed in #1762.)

**Gateway or orchestrator not starting**: Check pod status with `kubectl get pods -n egg-system` and logs with `kubectl logs -n egg-system deploy/gateway` or `kubectl logs -n egg-system deploy/orchestrator`.

**Permission denied on repos**: The gateway and orchestrator need to run as your host UID. Set `host_uid` and `host_gid` in `~/.config/egg/config.yaml` (output of `id -u` and `id -g`), then re-run `make k3s-secrets && make deploy`.

**Orchestrator won't start (root-related error)**: The orchestrator refuses to run as root to prevent git artifacts from being created with root ownership. If you see an error about root, HOST_UID, or HOST_GID, it means `HOST_UID`/`HOST_GID` are not set or are set to 0. The `egg` CLI sets these automatically; if values are wrong, check your `~/.config/egg/config.yaml`:
```yaml
host_uid: 1000  # output of id -u
host_gid: 1000  # output of id -g
```

**Token refresher warnings**: If you see "No valid token available from token refresher", this means no GitHub App is configured. This is expected in PAT-only mode — ensure `auth_mode: user` is set for your repos so the gateway uses your PAT.

**Orchestrator git errors**: The orchestrator stores pipeline state in git. `EGG_REPO_PATH` can be either a single git repository or a parent directory containing multiple repositories (the orchestrator will scan subdirectories automatically).

**Empty repository in sandbox containers**: The orchestrator creates isolated git worktrees for each pipeline via the gateway's worktree API. If containers have empty working trees:
1. Ensure `host_home` in your `~/.config/egg/config.yaml` matches your actual home directory: `echo $HOME`
2. Verify the gateway is healthy: `kubectl exec -n egg-system deploy/orchestrator -- curl http://gateway:9848/api/v1/health`
3. Check orchestrator logs for worktree creation errors: `kubectl logs -n egg-system deploy/orchestrator | grep -i worktree`

# ADR: GitHub Actions Support

**Status:** In Progress (PR #111 — Phase 1 MVP)
**Issue:** #78

## Context

egg currently runs on a local machine with Docker, consisting of a gateway sidecar (policy enforcement, credential injection, worktree management) and a sandbox container (Claude Code CLI). Users want to run egg in GitHub Actions to automate code tasks like fixing issues, responding to PR review comments, or running on a schedule.

GitHub Actions runners provide:
- A checked-out repository at `$GITHUB_WORKSPACE`
- Docker support (build and run containers)
- `GITHUB_TOKEN` for repository access
- Secrets for storing API keys
- Structured output via step outputs and job summaries

The challenge is adapting egg's dual-container model to work inside a GHA runner while maintaining the security boundary between sandbox and credentials.

## Decision

Create a **composite GitHub Action** that orchestrates the full egg stack (gateway + sandbox) within a GHA runner, accepting the checked-out repo as the working repository.

### Architecture

```
┌─ GitHub Actions Runner ────────────────────────────────┐
│                                                         │
│  $GITHUB_WORKSPACE/  ← actions/checkout'd repo          │
│                                                         │
│  ┌─ egg-gateway container ───────────────────────────┐  │
│  │ • Policy enforcement (branch push rules)          │  │
│  │ • GitHub token injection (from GHA secret)        │  │
│  │ • Worktree creation for mounted repo              │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  ┌─ egg-sandbox container ───────────────────────────┐  │
│  │ • Claude Code CLI (non-interactive, --exec mode)  │  │
│  │ • Repo at /home/egg/repos/<repo>                  │  │
│  │ • git/gh wrappers → gateway                       │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  egg-isolated network (172.32.0.0/24)                   │
└─────────────────────────────────────────────────────────┘
```

### User-Facing Interface

```yaml
# Example: user's workflow file
jobs:
  egg-task:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: jwbron/egg@v1
        with:
          prompt: "Fix the failing tests in src/auth/"
          anthropic-oauth-token: ${{ secrets.ANTHROPIC_OAUTH_TOKEN }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          timeout: 30
```

### Action Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `prompt` | Yes | — | Task/prompt to pass to Claude Code |
| `anthropic-oauth-token` | Yes | — | Anthropic OAuth token for Claude API |
| `github-token` | Yes | — | GitHub token for git operations (user PAT or `GITHUB_TOKEN`) |
| `bot-github-token` | No | — | GitHub App token for bot identity (if using bot mode) |
| `bot-username` | No | `egg` | Bot GitHub username (for filtering self-comments) |
| `mode` | No | `auto` | Network mode: `public`, `private`, or `auto` (auto-detects from repo visibility) |
| `timeout` | No | `30` | Timeout in minutes |
| `model` | No | `opus` | Claude model to use |

**Mode auto-detection:** When `mode` is `auto` (the default), the entrypoint queries the GitHub API to determine the repo's visibility. Private/internal repos default to `private` mode; public repos default to `public` mode. This matches the behavior users expect — private repos should not have internet access from the sandbox.

**Bot account support:** The action supports the same dual-identity model as local egg. When `bot-github-token` is provided, the gateway uses the bot identity for git operations (push, PR creation). When omitted, the user's `github-token` is used directly. The `bot-username` input controls self-comment filtering in the gateway.

### Action Outputs

| Output | Description |
|--------|-------------|
| `exit-code` | Container exit code (0 = success) |
| `pr-url` | URL of created PR, if any |
| `log-file` | Path to full Claude output log |

### Entrypoint Flow

The action's entrypoint script (`action/entrypoint.sh`) orchestrates:

1. **Pull images** — Pull pre-built gateway + sandbox images from GHCR (see Phase 1 note on build time)
2. **Create Docker network** — `egg-isolated` with a dynamically allocated subnet (avoids collisions with the runner's existing Docker networks by inspecting `docker network ls` and selecting an unused `172.x.0.0/24` range)
3. **Detect mode** — If `mode=auto`, query `gh api repos/{owner}/{repo}` for visibility; set `private` for private/internal repos, `public` for public repos
4. **Generate ephemeral config** — Write a temporary `repositories.yaml` with the full config format (see Config Generation below), generate a launcher secret
5. **Start gateway** — Mount `$GITHUB_WORKSPACE/.git/` at a known path (e.g., `/repos/{repo-name}/.git`), inject the GitHub token via `GITHUB_USER_TOKEN` (and `BOT_GITHUB_TOKEN` if provided), inject the Anthropic OAuth token via `CLAUDE_CODE_OAUTH_TOKEN`, mount the launcher secret
6. **Wait for health** — Poll `http://egg-gateway:9848/api/v1/health`
7. **Allocate container IP + create session** — Pre-allocate a container IP on the `egg-isolated` network, then call `POST /api/v1/sessions/create` with `{container_id, container_ip, mode, repos, uid, gid}`. This single API call atomically: queries repo visibility, filters repos by mode, creates worktrees, and registers the session. Returns `session_token` and `worktrees` dict.
8. **Start sandbox** — Mount the worktree path returned by the session API, shadow `.git`, inject `EGG_SESSION_TOKEN`, run Claude Code in `--exec` mode with `--print --output-format=stream-json`. The sandbox does **not** receive the Anthropic OAuth token directly — all API calls route through the gateway at `ANTHROPIC_BASE_URL=http://egg-gateway:9848`, where the gateway injects credentials.
9. **Capture output** — Stream container logs, write to `$GITHUB_STEP_SUMMARY`, extract PR URLs for step outputs
10. **Cleanup** — Delete session via gateway API, stop and remove containers, remove the Docker network. A `trap EXIT` handler ensures cleanup runs even if the runner is cancelled mid-execution (though a hard-killed runner may leave orphaned resources — these are ephemeral GHA runner resources and will be discarded when the runner VM is recycled).

### Gateway Token Handling

**GitHub tokens:** The gateway already supports PAT-based auth via `GITHUB_USER_TOKEN`. For GHA, the runner's `GITHUB_TOKEN` (or a PAT from secrets) is passed to the gateway container as `GITHUB_USER_TOKEN`. When `bot-github-token` is provided, it is passed as `BOT_GITHUB_TOKEN` for the bot identity, matching the local egg setup. No gateway code changes are needed.

**Anthropic credentials:** The gateway handles all Anthropic API credential injection. The OAuth token is passed to the **gateway** container as `CLAUDE_CODE_OAUTH_TOKEN` (matching the gateway's credential priority: OAuth token > API key, per `gateway/anthropic_credentials.py`). The sandbox never sees the token — it sets `ANTHROPIC_BASE_URL=http://egg-gateway:9848` and the gateway's `/v1/messages` proxy injects credentials before forwarding to Anthropic's API. This preserves the same credential isolation as local egg.

### Config Generation

Since there's no `~/.config/egg/repositories.yaml` in GHA, the entrypoint generates one with the full config format required by `repo_parser.py`:

```yaml
github_username: <from $GITHUB_ACTOR>
bot_username: <from bot-username input, default "egg">

writable_repos:
  - <owner/repo derived from $GITHUB_REPOSITORY>

repo_settings:
  <owner/repo>:
    auth_mode: <"bot" if bot-github-token provided, else "user">

user_mode:
  github_user: <from $GITHUB_ACTOR>
  git_name: <from $GITHUB_ACTOR>
  git_email: <from $GITHUB_ACTOR>@users.noreply.github.com

local_repos:
  paths:
    - /repos/<repo-name>
```

**Workspace path mapping:** The gateway's worktree manager creates worktrees from a source repo's `.git` directory. In GHA, `$GITHUB_WORKSPACE/.git` is bind-mounted into the gateway container at `/repos/<repo-name>/.git`. The gateway's `create_worktree()` call uses this path as the source, creating worktrees at `~/.egg-worktrees/{container_id}/{repo_name}` as usual. The resulting worktree path is returned by the session create API and mounted into the sandbox container.

### Security Model

The full gateway stack runs in GHA, maintaining the same security boundary as local:

- Sandbox never sees the GitHub token or Anthropic OAuth token directly
- Credentials are injected by the gateway at request time (GitHub tokens for git ops, Anthropic tokens for `/v1/messages` proxy)
- Branch push policies still apply (egg-prefixed branches only)
- Merge operations remain blocked
- `.git` directory is shadowed in the sandbox

The main difference from local: the GHA runner itself has access to secrets (this is inherent to GHA and not a regression — the trust boundary is the GHA workflow file, which is version-controlled).

## Implementation Phases

### Phase 1: Core Action (MVP)

**Deliverables:**
- `action/action.yml` — Composite action definition
- `action/entrypoint.sh` — Container orchestration script
- `action/generate-config.sh` — Dynamic config generation
- `.github/workflows/release-images.yml` — Build + push images to GHCR on release
- `.github/workflows/test-action.yml` — Integration test workflow

**Scope:**
- Pre-built GHCR images (building from source on every run is impractical — the sandbox image includes Python packages, Node.js, Claude Code, and dev tools, resulting in 5-15 minute build times that would dominate the action's run time)
- Single repo support (the checked-out repo)
- Auto mode detection (public/private based on repo visibility)
- Basic output capture (logs + exit code)
- Bot account support via configurable inputs

**Success criteria:**
- Action runs successfully in a GHA workflow
- Image pull completes in under 60 seconds on a standard runner
- Claude Code receives the prompt and can read/write the repo
- Claude Code can push branches and create PRs via the gateway
- Exit code is surfaced correctly

### Phase 2: Optimization

**Deliverables:**
- Docker layer caching via `actions/cache` for faster image pulls
- Output parsing (PR URL extraction, step summary)
- `$GITHUB_STEP_SUMMARY` integration with Claude's output

**Success criteria:**
- PR URLs are available as step outputs
- Job summary contains Claude's output

**Dependencies:** Phase 1

### Phase 3: Advanced Features

**Deliverables:**
- Multi-repo support (additional repos via action input)
- Custom `CLAUDE.md` injection via action input
- Trigger templates (issue-comment trigger, PR-review trigger, schedule trigger)

**Success criteria:**
- Multiple repos can be mounted simultaneously
- Example workflows provided for common trigger patterns

**Dependencies:** Phase 2

## Alternatives Considered

### A. Sandbox Only (No Gateway)

Run only the sandbox container, skip the gateway, and use GHA's `GITHUB_TOKEN` directly in the container.

**Pros:** Simpler, faster startup, fewer moving parts.
**Cons:** No credential isolation — the sandbox sees the GitHub token directly. No policy enforcement on git operations. Diverges from the local security model.

**Rejected because:** The gateway is the core of egg's security model. Removing it for GHA creates a fundamentally different trust model that would need separate testing and reasoning. The added complexity of running the gateway is modest (one extra container) and the security benefits are significant.

### B. Docker Compose

Use `docker-compose.yml` to define the gateway + sandbox stack.

**Pros:** Declarative, familiar, handles networking automatically.
**Cons:** Requires `docker-compose` in the runner (available but adds a dependency). Harder to dynamically configure (compose files are static). Output capture is more complex.

**Deferred:** Could be added as an alternative entrypoint in Phase 3 for users who prefer compose. The shell script approach in Phase 1 gives more control over the lifecycle.

### C. Run on Host (No Containers)

Install Claude Code and egg's git wrappers directly on the GHA runner.

**Pros:** No Docker overhead, fastest startup.
**Cons:** No isolation at all. The runner has full access to everything. Git wrappers would need to be adapted for non-container use. Fundamentally different execution model.

**Rejected because:** This defeats the purpose of egg's sandbox model. Users who want to run Claude Code directly on a runner can do so without egg.

## Consequences

### Positive
- egg becomes usable in CI/CD pipelines, enabling automated code tasks
- Same security model as local (gateway-enforced credential isolation)
- Reuses existing gateway/sandbox infrastructure with minimal changes
- Users get a standard GitHub Action interface

### Negative
- GHCR image pull adds cold-start overhead (mitigated by GitHub's CDN and layer caching)
- GHA runners have limited resources (7GB RAM, 2 vCPU) — see resource analysis below
- Two Docker containers consume more runner resources than a single process
- Action maintenance burden (testing across GHA runner updates)

### Resource Analysis

Standard `ubuntu-latest` runners provide 7GB RAM and 2 vCPU. Expected memory footprint:

| Component | Estimated RSS |
|-----------|---------------|
| Gateway (Flask + Squid proxy) | ~150-250 MB |
| Sandbox (Claude Code / Node.js) | ~200-400 MB |
| OS + Docker overhead | ~500 MB |
| **Total baseline** | **~850 MB - 1.15 GB** |

This leaves ~5.5-6 GB for Claude Code's working memory (file reads, tool results, context), which should be sufficient for typical tasks. The Squid proxy in the gateway could be dropped in GHA mode if memory becomes tight (the GHA runner's network can be constrained via Docker network policies instead), but this is not expected to be necessary.

For memory-intensive tasks or large codebases, users can specify `runs-on: ubuntu-latest-xl` (or equivalent larger runners) in their workflow files. This is outside egg's control and documented as a recommendation.

## Resolved Questions

1. **Issue/PR-triggered prompts:** Yes — tracked separately in #82. Trigger templates will be delivered in Phase 3.
2. **Image registry:** GHCR — integrated with GitHub, free for public repos, and the natural choice for a GitHub Action.
3. **Branch strategy in GHA:** Support both creating new branches and pushing to the current branch. The existing gateway lockdowns (egg-prefixed branches, merge blocking) apply as-is.
4. **Rate limiting:** Not yet. Deferred until usage patterns in GHA are better understood.

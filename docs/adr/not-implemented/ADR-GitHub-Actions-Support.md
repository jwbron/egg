# ADR: GitHub Actions Support

**Status:** Proposed
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
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          github-token: ${{ secrets.GITHUB_TOKEN }}
          mode: public
          timeout: 30
```

### Action Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `prompt` | Yes | — | Task/prompt to pass to Claude Code |
| `anthropic-api-key` | Yes | — | Anthropic API key |
| `github-token` | Yes | — | GitHub token for git operations |
| `mode` | No | `public` | Network mode: `public` or `private` |
| `timeout` | No | `30` | Timeout in minutes |
| `model` | No | `sonnet` | Claude model to use |

### Action Outputs

| Output | Description |
|--------|-------------|
| `exit-code` | Container exit code (0 = success) |
| `pr-url` | URL of created PR, if any |
| `log-file` | Path to full Claude output log |

### Entrypoint Flow

The action's entrypoint script (`action/entrypoint.sh`) orchestrates:

1. **Build images** — Build gateway + sandbox from source (Phase 1), or pull pre-built from GHCR (Phase 2)
2. **Create Docker network** — `egg-isolated` (172.32.0.0/24)
3. **Generate ephemeral config** — Write a temporary `repositories.yaml` pointing at `$GITHUB_WORKSPACE`, generate a launcher secret
4. **Start gateway** — Mount the checked-out repo's `.git/` directory, inject the GitHub token via `GITHUB_USER_TOKEN`, mount the launcher secret
5. **Wait for health** — Poll gateway health endpoint
6. **Create session + worktrees** — Call gateway API to register a session and create a worktree for the repo
7. **Start sandbox** — Mount the worktree, shadow `.git`, inject `EGG_SESSION_TOKEN` and `ANTHROPIC_API_KEY`, run Claude Code in `--exec` mode with `--print --output-format=stream-json`
8. **Capture output** — Stream container logs, write to `$GITHUB_STEP_SUMMARY`, extract PR URLs for step outputs
9. **Cleanup** — Delete session, stop containers, remove network

### Gateway Token Handling

The gateway already supports PAT-based auth via the `GITHUB_USER_TOKEN` environment variable. For GHA, the runner's `GITHUB_TOKEN` (or a PAT from secrets) is passed to the gateway container as `GITHUB_USER_TOKEN`. No gateway code changes are needed for basic auth.

For GitHub App auth (used in some enterprise setups), the gateway's existing App auth path works — the user would pass the App PEM and App ID as action inputs.

### Config Generation

Since there's no `~/.config/egg/repositories.yaml` in GHA, the entrypoint generates one:

```yaml
local_repos:
  paths:
    - /github/workspace
github_username: <from $GITHUB_ACTOR>
```

The gateway's git mounts are derived from this config, pointing at `$GITHUB_WORKSPACE/.git`.

### Security Model

The full gateway stack runs in GHA, maintaining the same security boundary as local:

- Sandbox never sees the GitHub token or Anthropic API key directly
- Credentials are injected by the gateway at request time
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
- `.github/workflows/test-action.yml` — Integration test workflow

**Scope:**
- Build images from source on each run
- Single repo support (the checked-out repo)
- Public mode only
- Basic output capture (logs + exit code)

**Success criteria:**
- Action runs successfully in a GHA workflow
- Claude Code receives the prompt and can read/write the repo
- Claude Code can push branches and create PRs via the gateway
- Exit code is surfaced correctly

### Phase 2: Optimization

**Deliverables:**
- `.github/workflows/release-images.yml` — Build + push images to GHCR on release
- Docker layer caching via `actions/cache`
- Output parsing (PR URL extraction, step summary)

**Success criteria:**
- Cold-start action run completes image pull in under 60 seconds
- PR URLs are available as step outputs
- Job summary contains Claude's output

**Dependencies:** Phase 1

### Phase 3: Advanced Features

**Deliverables:**
- Private mode support
- Multi-repo support (additional repos via action input)
- Custom `CLAUDE.md` injection via action input
- Trigger templates (issue-comment trigger, PR-review trigger, schedule trigger)

**Success criteria:**
- Private mode works with Anthropic API only
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
- Cold-start build time adds overhead (mitigated by GHCR images in Phase 2)
- GHA runners have limited resources (7GB RAM, 2 vCPU) which may constrain Claude Code
- Two Docker containers consume more runner resources than a single process
- Action maintenance burden (testing across GHA runner updates)

## Open Questions

1. **Issue/PR-triggered prompts:** Should the action support commenting `/egg fix this` on an issue to trigger a run with issue context as the prompt? This would require a separate workflow template.
2. **Image registry:** GHCR (free for public repos, integrated with GitHub) vs Docker Hub?
3. **Branch strategy in GHA:** Should the action always create new branches from the checked-out ref, or should it support pushing to the current branch?
4. **Rate limiting:** Should the action enforce any rate limits on Claude API usage to prevent runaway costs?

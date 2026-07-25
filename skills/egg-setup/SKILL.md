---
name: egg-setup
description: Walk the user through initial egg setup or update an existing configuration — checks dependencies, configures secrets and repositories, deploys to k3s, and validates the installation.
disable-model-invocation: true
argument-hint: "[--check | --update secrets | --update repos]"
---

# egg Setup

You are guiding the user through egg setup or a configuration update. `bin/egg-init` owns all of the setup logic; this skill is a conversational front-end that runs it, interprets the output, and helps the user fix anything it flags.

> **Why delegate?** egg's deployment moved to k3s in #1762; every earlier version of this skill validated a Docker Compose architecture that no longer exists. Keeping the logic in `bin/egg-init` (a script, tested in CI) means this skill can never drift from the real setup flow again.

## Argument Parsing

Parse arguments after `/egg-setup`:

| Input | What to run |
|-------|-------------|
| `/egg-setup` | Full guided setup — all `bin/egg-init` stages |
| `/egg-setup --check` | `bin/egg-init --check` — validate, change nothing |
| `/egg-setup --update secrets` | `bin/egg-init --update secrets` |
| `/egg-setup --update repos` | `bin/egg-init --update repos` |

## Flow

### 1. Locate the checkout

`bin/egg-init` lives at the root of the egg repo. If the user is running this skill from an MCP-connected host, the checkout is the one the `egg` MCP server is deployed from. If you can't find it, ask:

> "Where is your egg checkout? (e.g. ~/khan/egg)"

All commands below run from that directory.

### 2. Preflight: dependencies

Run the dependency portion of the check first so failures surface early:

```bash
bin/egg-init --check
```

This verifies, without changing anything:

- **Dependencies**: docker (daemon running), git, `gh` (authenticated), curl, kubectl, `envsubst` (GNU gettext), `claude` CLI
- **Config files**: `config.yaml`, `repositories.yaml`, `secrets.env`, `launcher-secret`, `lifecycle-secret` under `~/.config/egg/`
- **Credentials**: presence and prefix of the Anthropic and GitHub tokens (never prints values)
- **Repositories**: each `local_repos.paths` entry is a real git repo
- **Host integration**: the egg MCP server is registered and the operator skills are installed
- **Cluster** (if reachable): Cilium is present, `egg-system` namespace exists

Present the results as a compact checklist. For anything marked `✗`, offer per-OS install guidance:

- **macOS**: `brew install <tool>` (or `brew install --cask docker` for Docker)
- **Debian/Ubuntu**: `apt install <tool>`
- **Fedora/RHEL**: `dnf install <tool>`
- `gh auth login` for an unauthenticated GitHub CLI

If any required dependency is missing, use `AskUserQuestion`:
- **Question**: "Some required dependencies are missing. How would you like to proceed?"
- **Header**: "Dependencies"
- **Options**:
  - **"Show install instructions"** — display the per-OS commands above
  - **"Skip and continue"** — proceed anyway (later stages may fail)
  - **"Abort"** — stop here

If invoked as `/egg-setup --check`, stop after presenting results and offering fixes. Do not run the setup stages.

### 3. Run the setup

For a full run, or for an `--update` mode, invoke the matching `bin/egg-init` command. These stages are **interactive**: `bin/egg-init` prompts for the two credentials (a Claude OAuth token or API key, and a GitHub PAT) and for repository paths, reading secrets without echoing. Run it in a way the user can answer — do not pipe closed stdin to it.

```bash
bin/egg-init                      # full: preflight → config → cluster → images → deploy → host → smoke
bin/egg-init --update secrets     # credentials only
bin/egg-init --update repos       # repository config only
```

What each stage does, so you can narrate:

1. **preflight** — dependency checks (as in step 2), with per-OS install hints
2. **config** — generates `config.yaml` (auto-detected `host_uid`/`host_gid`/`host_home`), `launcher-secret`, and `lifecycle-secret`; collects the Claude credential (validates `sk-ant-oat` / `sk-ant-api` prefix) and GitHub PAT (validates `github_pat_` / `ghp_` prefix) into `secrets.env` with `chmod 600`; fills gateway policy defaults (`GATEWAY_BOT_NAME=egg`, branch prefix `egg`, `GATEWAY_TRUSTED_USERS` from `gh api user`); derives `repositories.yaml` from repo paths the user provides (`owner/repo` parsed from each `origin` remote, `auth_mode: user`)
3. **cluster** — `make k3s-setup` (k3s + Cilium CNI; refuses a flannel-only cluster) and `make registry-setup` (loopback image registry)
4. **images** — `make build` + `make k3s-push` (layer-aware push to the local registry)
5. **deploy** — `make k3s-secrets` + `make deploy`
6. **host** — registers `http://localhost:9850/mcp` as the `egg` MCP server via `claude mcp add`, and symlinks the operator skills (`sdlc`, `agent-diagnose`, `deployment-diagnose`, `egg-setup`) into `~/.claude/skills/` (never overwrites a real file or a conflicting symlink)
7. **smoke** — `egg-system` pods Ready, orchestrator `/api/v1/health` green on :9849, gateway healthy (checked in-cluster; its Service is ClusterIP-only), MCP endpoint reachable on :9850

### 4. Interpret and fix

`bin/egg-init` is idempotent: re-running skips work that's already done and only prompts for what's missing or what the user asks to replace. Common failures and what to do:

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| preflight: `gh not authenticated` | — | `gh auth login`, re-run |
| `Cilium not detected` | plain k3s with flannel | `make k3s-teardown && make k3s-setup` (in-place CNI swap is unsupported) |
| images stage very slow on first run | expected — the sandbox image is large | reassure; later builds are incremental via the local registry |
| deploy aborts: images not in k3s | tag moved since last build | `make redeploy` |
| smoke: gateway unhealthy | config or secrets issue | `kubectl logs -n egg-system deploy/gateway`; then `/deployment-diagnose` |
| smoke: MCP unreachable | orchestrator down | `kubectl logs -n egg-system deploy/orchestrator`; then `/deployment-diagnose` |
| `Permission denied` on repos | `host_uid`/`host_gid` mismatch | check `~/.config/egg/config.yaml` matches `id -u` / `id -g`, then `make k3s-secrets && make deploy` |

For anything deeper, hand off to the `/deployment-diagnose` skill rather than improvising.

### 5. Done — next steps

On success, show:

```
## egg is up

Drive pipelines from any Claude Code session (the egg MCP server is registered):

  submit_task(issue_number=123, repo="owner/name")
  /sdlc                                   # prompt-driven pipeline
  /sdlc -r <repo> -i <issue_number>       # issue-driven pipeline

Useful later:
  /egg-setup --check           # validate the setup
  /egg-setup --update secrets  # rotate credentials
  /egg-setup --update repos    # add a repository
  make redeploy                # after pulling new egg commits
```

## macOS

`bin/egg-init` detects macOS and refuses the native cluster path: k3s and Cilium are Linux-native and egg's NetworkPolicy isolation is load-bearing, so a degraded local install is not offered. The supported path is a Lima VM running the same `bin/egg-init` inside it — see `docs/guides/onboarding.md#macos`. If the user is on macOS, walk them through that doc rather than attempting a native setup.

## Critical Rules

- **Delegate to `bin/egg-init`** — never hand-edit `~/.config/egg/` files or run individual `make` targets as a substitute; the script sequences them with preflights.
- **Never expose secrets** — the script reads tokens with echo disabled and validates prefixes only. Do not `cat secrets.env`, and mask any token you must reference (first 8 / last 4 chars).
- **Idempotency is the point** — when something fails mid-run, fix the cause and re-run `bin/egg-init`; don't try to reconstruct partial state by hand.
- **Respect existing files** — the script backs up `repositories.yaml` before rewriting and never overwrites a conflicting skills symlink; don't defeat either protection.
- **Keep output concise** — checklists and tables, not paragraphs.

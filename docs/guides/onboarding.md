# Onboarding: From `git clone` to a Running egg

This is the fast path for a new operator on **Linux** (macOS via Lima — see [below](#macos)). It uses `bin/egg-init`, a guided, idempotent setup script that orchestrates the existing Makefile targets with preflight checks, credential capture, and host-side Claude Code integration.

The only two things you supply:

1. A **GitHub fine-grained PAT** with Contents (R/W), Pull requests (R/W), Issues (R/W) — create at <https://github.com/settings/tokens?type=beta>
2. A **Claude credential** — either a Claude Code OAuth token (`claude auth status --json | jq -r .oauthToken`) or an Anthropic API key (<https://console.anthropic.com/settings/keys>)

Everything else (infrastructure secrets, `config.yaml`, `repositories.yaml`, the cluster, images, the registry, MCP registration, skills install) is generated, detected, or interactively gathered by the script.

## Quick Start

```bash
git clone https://github.com/jwbron/egg.git
cd egg
./bin/egg-init
```

That's it. The script walks through seven stages, prompting only for the two credentials and the repos you want egg to work on:

| Stage | What it does |
|-------|--------------|
| **1. preflight** | Checks docker (daemon running), git, `gh` (authenticated), curl, kubectl, `envsubst` (GNU gettext), and the `claude` CLI, with per-OS install guidance for anything missing |
| **2. config** | Generates `~/.config/egg/config.yaml` (auto-detected `host_uid`/`host_gid`/`host_home`), `launcher-secret`, and `lifecycle-secret`; collects your Claude credential and GitHub PAT into `secrets.env` (`chmod 600`, echo disabled, prefix-validated); fills gateway policy defaults (`GATEWAY_BOT_NAME=egg`, branch prefix, `GATEWAY_TRUSTED_USERS` from `gh api user`); derives `repositories.yaml` from the repo paths you give it (`owner/repo` parsed from each `origin` remote, `auth_mode: user`) |
| **3. cluster** | `make k3s-setup` (k3s + Cilium CNI) and `make registry-setup` (loopback image registry). Refuses a flannel-only cluster — NetworkPolicy isolation is load-bearing |
| **4. images** | `make build` + `make k3s-push` (layer-aware push to the local registry; first build is slow, later ones incremental) |
| **5. deploy** | `make k3s-secrets` + `make deploy` |
| **6. host** | Registers `http://localhost:9850/mcp` as the `egg` MCP server via `claude mcp add`, and symlinks the operator skills (`sdlc`, `agent-diagnose`, `deployment-diagnose`, `egg-setup`) into `~/.claude/skills/` |
| **7. smoke** | Verifies `egg-system` pods are Ready, the orchestrator is healthy on `:9849`, the gateway is healthy (checked in-cluster; its Service is ClusterIP-only), and the MCP endpoint is reachable on `:9850` |

Re-running `./bin/egg-init` is safe: every stage detects existing state and skips or validates rather than clobbering.

## Subcommands

```bash
bin/egg-init --check             # Validate an existing setup; change nothing
bin/egg-init --update secrets    # Re-collect credentials (rotate a token)
bin/egg-init --update repos      # Re-collect repository configuration
bin/egg-init --skip-cluster      # Config + host integration only (no cluster)
bin/egg-init --yes               # Accept defaults, skip confirmations
```

`--check` is the one to reach for first on a machine you're not sure about: it reports dependencies, config files, credential presence/prefix, repo validity, host integration, and cluster state without modifying anything.

Environment overrides:

| Variable | Default | Purpose |
|----------|---------|---------|
| `EGG_CONFIG_DIR` | `~/.config/egg` | Config directory |
| `EGG_SKILLS_DIR` | `~/.claude/skills` | Skills install directory |

## What the script does NOT do

- **It does not replace the Makefile.** `bin/egg-init` owns orchestration and detection only; every cluster, image, and deploy action delegates to `make k3s-setup` / `registry-setup` / `build` / `k3s-push` / `k3s-secrets` / `deploy`. There is one implementation of each step.
- **It does not print secrets.** Tokens are read with echo disabled and validated by prefix (`sk-ant-oat` / `sk-ant-api`, `github_pat_` / `ghp_`). `--check` reports presence, never values.
- **It does not overwrite your files blindly.** `repositories.yaml` is backed up before a rewrite; a skills symlink that points elsewhere, or a real file occupying a skills slot, is left untouched with a warning.
- **It does not weaken the security model for convenience.** There is no "simple mode" that skips Cilium or the NetworkPolicy isolation.

## After setup

Drive pipelines from any Claude Code session (the egg MCP server is registered):

```
submit_task(issue_number=123, repo="owner/name")
/sdlc                                   # prompt-driven pipeline
/sdlc -r <repo> -i <issue_number>       # issue-driven pipeline
```

Useful later:

```bash
bin/egg-init --check           # validate the setup
make redeploy                  # rebuild + redeploy after pulling new egg commits
kubectl get pods -n egg-system # inspect the cluster
```

## Troubleshooting

| Symptom | What to do |
|---------|------------|
| `gh not authenticated` | `gh auth login`, re-run `bin/egg-init` |
| `Cilium not detected` | You have a flannel-only k3s. In-place CNI swap is unsupported: `make k3s-teardown && make k3s-setup` |
| First image build is very slow | Expected — the sandbox image is large. Later builds are incremental through the local registry |
| `make deploy` aborts: images not in k3s | The tag moved since the last build (a pull/rebase). Run `make redeploy` |
| Smoke: gateway unhealthy | `kubectl logs -n egg-system deploy/gateway`, then `/deployment-diagnose` |
| Smoke: orchestrator unhealthy on `:9849` | `kubectl logs -n egg-system deploy/orchestrator`, then `/deployment-diagnose` |
| Smoke: MCP unreachable on `:9850` | `kubectl logs -n egg-system deploy/orchestrator`, then `/deployment-diagnose` |
| `Permission denied` on repos | `host_uid`/`host_gid` in `~/.config/egg/config.yaml` must match `id -u`/`id -g`; fix and `make k3s-secrets && make deploy` |

For deeper diagnosis, run the `/deployment-diagnose` skill from Claude Code.

## macOS

`bin/egg-init` detects macOS and refuses the native cluster path. k3s and Cilium are Linux-native, and egg's agent network isolation depends on Cilium's NetworkPolicy enforcement — so a degraded "run it anyway" mode is not offered.

The supported path runs the whole cluster inside a [Lima](https://lima-vm.io/) VM and drives it from the macOS host over the MCP endpoint. The VM provisioning reuses the exact same `bin/egg-init` flow (the same `make k3s-setup` inside the VM) so the two platforms can't drift; the VM layer only pre-seeds the things the host can't provide: `$HOME` mounts (virtiofs), boot persistence for the pod-egress MASQUERADE rule, and `KUBECONFIG` extraction to the host.

> **Status:** the Lima path is tracked in [#3155](https://github.com/jwbron/egg/issues/3155) and is not yet automated in this repo. On macOS today, `bin/egg-init` will offer to run config + host integration only (`--skip-cluster`), leaving the cluster to a Linux VM you provision yourself (Lima's `template://k3s`, plus the Cilium and mount steps above). Full automation is a follow-up.

## Relationship to the manual flow

Everything `bin/egg-init` does can still be done by hand — see [Deployment Guide](deployment.md) and [Local Quickstart](local-quickstart.md) for the individual `make` targets and the config file schemas. The script exists so you don't have to know the ordering, the `openssl` incantation for `lifecycle-secret`, or the `claude mcp add` registration step up front.

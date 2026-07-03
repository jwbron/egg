# egg — Agent-Powered SDLC Platform

Start with **[docs/index.md](docs/index.md)** — it has task-specific lookup tables, architecture docs, and component READMEs.

## Quick Reference

```bash
make help          # List all targets
make deps          # Install all dependencies (installs uv + venv)
make setup         # Install dependencies + pre-commit hooks
make lint          # Run all linters (Python, Shell, YAML, Dockerfile)
make test          # Changeset-aware: tests reachable from the diff (inner-loop default)
make test-all      # Full suite — CI ground truth; updates LKG baseline on green
make lint-fix      # Auto-fix lint issues
make security      # Run security scans (bandit, safety, trivy)
```

**Inner loop: run targeted tests from the venv.** Validate changes with `.venv/bin/pytest <paths>` scoped to the suites your diff touches, plus `make lint`. `make test` (changeset-aware narrowing) is available, but on wide diffs it can take 10+ minutes and pull in known host-environment failures ([#3222](https://github.com/jwbron/egg/issues/3222)); targeted suites usually give the same signal in seconds. Leave full-suite runs (`make test-all`) to CI. Always invoke Python tools from the venv (`.venv/bin/pytest`, never system `pytest`). See [docs/guides/testing.md](docs/guides/testing.md) for the narrowing model.

## Python Environment

This project requires a `.venv` for all Python tooling (pytest, ruff, mypy, etc.). `make` targets resolve to `.venv/bin/<tool>` automatically. When you invoke a Python tool directly, prefix it with `.venv/bin/` (e.g. `.venv/bin/pytest`, `.venv/bin/ruff`); never use the system binary.

If `.venv` is absent, run `make deps` to install everything. This installs `uv` if needed and creates a `.venv` with all dev dependencies.

## Repo Layout

| Directory | What it is |
|-----------|------------|
| `orchestrator/` | Central SDLC pipeline engine — scheduling, health monitoring, multi-agent coordination |
| `gateway/` | Policy-enforcement sidecar — validates git/gh operations, injects credentials |
| `sandbox/` | Untrusted agent container — Claude Code config, tools, entrypoint |
| `config/litellm/` | egg-litellm image: Dockerfile, prompt-cache patches, and cost/cache logger for non-Claude routes |
| `shared/` | Shared Python packages and agent prompt templates |
| `docs/` | All documentation — guides, architecture, references |
| `integration_tests/` | Cross-component integration tests |
| `scripts/` | Build, release, and CI helper scripts |

## Key Entry Points

- **Headless agents** use the Agent SDK (`egg_agent` package)
- **Agent work** goes through the MCP server — see
  [`submit_task`](docs/guides/sdlc-pipeline.md) (full
  refine → plan → implement). The legacy interactive-mode CLI
  (`bin/egg`) was removed in
  [#1762](https://github.com/jwbron/egg/issues/1762).
- See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, branching, and PR workflow

## Conventions

- **Branch names are prefixed `egg/`** (e.g. `egg/fix-1784-overseer-escalate-only`). Never bare `fix/` or `feature/`.
- **This is a public repo.** Issues, PRs, commit messages, and code comments must not carry private references: no internal org or repo names, no private pipeline IDs, no domain-specific examples lifted from private runs. Describe the mechanism generically ("the observed failure", "a real run") and scan text for private terms before publishing.
- **No model snapshot pins in defaults.** Production config defaults use model aliases (`opus`, `sonnet`, `haiku`; canonical mapping in `orchestrator/agent_model_resolution.py` — `_CLAUDE_EXACT_ALIASES`, `DEFAULT_AGENT_MODEL`), resolved at spawn time. Per-agent model choice is a `PipelineConfig` knob, not a code edit. Pinned snapshots are for explicit operator overrides only (e.g. reproducibility in tests).
- **One PR for cohesive work.** Don't split tightly-coupled refactor + feature + tests + docs into a PR chain; split only when a piece stands alone and ships value by itself. Keep each PR scoped to what was asked: a linked issue mentioned as context is not an invitation to fold its fix in.
- **Open follow-up issues as needed**, without asking first (they're cheap and reversible), and frame them accurately: if the work already merged, the issue is "enable / validate / roll out", not "implement".
- **Feeder issues state the problem, not the decomposition.** A parent issue headed for the SDLC pipeline carries the goal, constraints the refiner can't derive from code, related issues, and a definition of done. No slice lists, slice DAGs, phase breakdowns, orderings, or pre-enumerated open questions; decomposition is the pipeline's job, and pre-defining it biases the refiner and hides whether it understood the problem.

## Architecture Invariants

Durable facts that sessions repeatedly get wrong. Verify against code before assuming they've changed.

- **Agents never wait on the bus.** An agent invocation handles one actionable event and exits; the wrapper/event pump owns polling, blocking waits, and heartbeats. `orchestrator/tests/test_wait_instruction_ratchet.py` pins agent-facing prompt sources against reintroduced wait instructions; never write a prompt or role that tells an agent to block, sleep, or poll.
- **The sandbox never writes host mounts.** Only the gateway or orchestrator modify host files; anything durable the sandbox produces is persisted through mediated API egress. The existing worktree mount is a pre-existing exception, not a license for new sandbox-writable host mounts.
- **Images never leave the host.** `push-egg-images.sh` refuses non-loopback registries with no override; keep it that way. The sandbox image bakes repo content from build commands, so any new channel that could move it off-host needs explicit exposure review.
- **`ScriptedProvider` is unit-test only.** Deployed agent pods run the real provider; no ConfigMap, env var, or mount injects a canned LLM trajectory into a running pod. Plans proposing k3s integration tests that drive real pods with deterministic trajectories are structurally wrong; push back at the plan gate.
- **Kubernetes bypasses the image ENTRYPOINT.** Agent pods set the container `command`, so `sandbox/entrypoint/` never runs on the k8s path (Docker Compose only). Boot-time setup belongs in the orchestrator spawner (`kubernetes_spawner.spawn_agent_job`), not the entrypoint.
- **Agents do auto-compact** at roughly 95% of the model's context window; `shared/egg_anchor/` exists for post-compaction recovery, and `orchestrator/agent_model_resolution.py` manages compaction profiles. Don't reason from "the SDK never compacts" or "resume is lossless" past that threshold.
- **Sandbox agents have no Confluence/Jira MCP.** Those MCP tools exist only in host operator sessions. In-pod access goes through the gateway-backed CLI wrappers (e.g. `sandbox/scripts/confluence`), private-network mode only. Task prompts must reference the wrappers; `mcp__confluence__*` names never resolve in a pod, so prompts using them always fall into the low-value "reason it out" fallback.
- **PR topology: one context PR per sliced pipeline.** The context (base) PR is `egg/<id>/work` into main and always exists; root slices base on `/work`, later slices on their parent slice. There is no separate context branch and no "umbrella" PR concept. Program-level metadata (test plan, pre-merge obligations) belongs on the context PR, not the terminal slice PR.

## Pipeline Monitoring and HITL

- **Early BRC silence is normal.** Zero messages in the first several minutes just means agents are reading code and running tests before their first proposal; reviewers don't propose at all. Producer container logs (tool_use activity) are the authoritative liveness signal, not message counts. Silence starts to matter at the 10+ minute silent-agent threshold, and then only for that agent.
- **Heartbeat silence during drafting is normal.** Heartbeats are tool calls, so a producer composing one long output (90-300s+) looks heartbeat-silent. On an agent-stall alert, read the container logs first: a drafting-intent message followed by quiet means it's composing. Don't nudge, cancel, or restart; only treat as a stall if the logs show genuine idleness.
- **Consensus completes in one container cycle.** NACKs resolve via re-proposals within the same lifecycle. A second cycle (new containers) indicates a bug in phase-completion logic; flag it as anomalous rather than normal flow.
- **NACK cycle count is not a model-quality signal.** 3-5 revise cycles per producer is typical regardless of model. Judge cheaper models on convergence and total cost + wall-clock; escalate only on genuine non-convergence or unacceptable latency.
- **Phase-gate resolutions parse strictly.** Only a bare `approve` / `approved` / `lgtm` / `yes` (or JSON `{"action": "approve"}`) advances the phase. Any other free text, including "approve" followed by a note, is treated as request_changes and re-runs the phase; JSON approve-with-feedback silently drops the feedback. To attach direction, deliver it as request_changes (or out-of-band via a message), then approve bare.
- **Never auto-resolve HITL.** Decisions (phase_gate, choice, feedback) and escalations (overseer alerts, stalls, NACK escalations) are always surfaced to the operator; permissive/auto modes license mechanical work, not policy decisions.
- **A failing test is evidence; the NACK is the bug report.** A committed failing test alone is easy to miss in a multi-file diff. The tester pairs it with a NACK on the producer's proposal that explicitly names the failing test. Avoid prompt language like "the test speaks for itself".

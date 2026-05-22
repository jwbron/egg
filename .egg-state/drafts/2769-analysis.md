# Analysis: Support non-Claude models per agent via a LiteLLM proxy

> Issue: #2769 | Phase: refine

## Problem Statement

egg today runs every SDLC agent on Claude via the Claude Agent SDK and Claude Code harness, with the gateway sidecar injecting Anthropic credentials. We want to **run agents on non-Claude models — primarily self-hosted Qwen as the first cost-cutting target — in parallel with Claude**, with **each agent independently selectable**, and **with zero regression for agents that stay on Claude**.

The chosen vehicle is a **LiteLLM proxy** that exposes an Anthropic-compatible `/v1/messages` endpoint and translates to OpenAI-compatible backends (vLLM-hosted Qwen, hosted providers, etc.). The **gateway becomes a per-agent / per-model upstream router**: Anthropic-bound requests continue straight to `api.anthropic.com`; non-Claude-bound requests divert to LiteLLM.

A side benefit is reduced coupling to Claude Code as the harness: routing through a translation layer creates the seam needed to swap harnesses later if we want.

`claude-code-router` was evaluated and rejected for being effectively unmaintained and corrupting streaming tool-call arguments for Qwen thinking-mode models. LiteLLM is actively maintained and exposes a real Anthropic `/v1/messages` translator. (Note: the broader supply-chain risk of any third-party translator is real — see [LiteLLM's March 2026 PyPI incident](https://docs.litellm.ai/docs/) — but does not invalidate the choice.)

## Current Behavior

**Gateway upstream is hard-wired to Anthropic.** In `gateway/gateway.py`:

```python
def get_anthropic_client() -> httpx.Client:
    """Get or create the singleton Anthropic API client."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = httpx.Client(
            base_url="https://api.anthropic.com",  # noqa: EGG200 - gateway proxy client, not direct LLM call
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _anthropic_client
```

Both proxy entry points — `proxy_anthropic_messages()` (`gateway/gateway.py:9752`, the `POST /v1/messages` handler) and `proxy_count_tokens()` (`gateway/gateway.py:10019`) — fetch this singleton client and forward the request body unchanged after credential injection. The streaming-resilience logic, the per-request `_SSEAccumulator` (`gateway/gateway.py:9552`), the private-mode `_filter_blocked_tools()` tool-strip (`gateway/gateway.py:9410`), and the credential injection (`_inject_anthropic_credentials()`, `gateway/gateway.py:9355`) all sit on top of this single upstream.

**Per-agent model config is minimal today.** Three knobs exist:

| Field | File | Default | Notes |
|-------|------|---------|-------|
| `overseer_decision_maker_model` | `orchestrator/models.py:546` | `sonnet` | Overseer Tier-2 model, used in `kubernetes_spawner.py:1582` / `overseer/monitor.py:259` / etc. |
| `overseer_advisor_model` | `orchestrator/models.py:620` | `opus` | Overseer Tier-1 advisor model |
| `model: str = "opus"` arg | `orchestrator/consensus_wrapper.py:622` | `opus` | Hardcoded in `build_consensus_wrapped_command()`; **no caller overrides it today** |

The SDLC roles that actually run the BRC consensus loop (refiner, reviewer_*, planner, coder, tester, etc.) all reach the agent through `build_consensus_wrapped_command()`:

- `orchestrator/concurrent_executor.py:454` calls `build_consensus_wrapped_command(prompt_text)` (no model arg).
- `orchestrator/routes/pipelines.py:2704` does the same on restart.

The wrapper builds the agent command as `python3 -m egg_agent --model opus --max-turns 1000 ...` (`consensus_wrapper.py:653-662`). So **every non-overseer agent today is opus-only, by hardcoding, not by configuration**.

**`[1m]` is Claude-only context-window syntax** and is baked into the in-process defaults at three sites:

- `shared/egg_agent/client.py:62` — `DEFAULT_MODEL = "opus[1m]"`
- `shared/egg_agent/__main__.py:35` — `parser.add_argument("--model", default="opus[1m]", ...)`
- `sandbox/llm/runner.py:49` — `cmd.extend(["--model", "opus[1m]"])` (legacy interactive CLI path)

A non-Claude model name with `[1m]` appended will be rejected upstream.

**The sandbox is zero-credential and already routes through the gateway.** `orchestrator/kubernetes_spawner.py:807` sets `ANTHROPIC_BASE_URL=GATEWAY_K8S_URL` on every agent pod, plus a placeholder OAuth token that the gateway strips and replaces with a real credential server-side. `gateway/allowed_domains.txt:9-15` confirms that `api.anthropic.com` is **intentionally not in the Squid allowlist** — every Anthropic-bound byte is forced through the gateway proxy endpoint. **This is what makes the gateway the right routing point**: the sandbox cannot bypass it even if it wanted to.

**Primary risk surface: Claude Code's model-name assumptions.** Claude Code derives the context window — and therefore auto-compaction timing — from the model name (`/context` shows the components; auto-compact triggers around 95–98% of the model's *known* limit). It also gates features like extended thinking on recognized model names. Compaction in particular is documented as supported only for [recognized Claude model families](https://platform.claude.com/docs/en/build-with-claude/compaction); an unrecognized name falls back to a default window that **will not match a Qwen backend's real limit** and can produce over-length requests that hard-fail and wedge the agent on long sessions. The issue body and community write-ups ([dev.to "Running Claude Code with Local LLMs via vLLM and LiteLLM"](https://dev.to/dcruver/running-claude-code-with-local-llms-via-vllm-and-litellm-599b), [okhlopkov.com on compaction](https://okhlopkov.com/claude-code-compaction-explained/)) converge on one mitigation: **present Claude Code a recognized, conservative model alias** in the request body, and **route on a separate per-agent signal**.

## Constraints

- **No regression on the Claude path.** Existing agents keep running on `api.anthropic.com` with no behavior change. The router must be inert by default — same wire format, same credential injection, same SSE accumulator, same tool-strip, same stream-resilience logic. Routing is additive.
- **Routing point sits below the SSE accumulator, tool-filter, and stream-resilience logic.** All three rely on the Anthropic SSE wire format, which is exactly what LiteLLM emits. The router must choose upstream **after** request validation and credential resolution but **before** `client.send(http_req, stream=True)`.
- **Zero-credential sandbox invariant must hold.** The sandbox today has no Anthropic credentials; same must be true for LiteLLM. Whoever holds the LiteLLM master key lives outside the sandbox (gateway or LiteLLM pod).
- **Gateway-mediated visibility must hold.** All non-Claude traffic must still flow through the gateway so today's audit logging, transcript capture, and tool-strip apply uniformly. LiteLLM must not be directly reachable from sandbox pods.
- **Network policy.** Squid's `allowed_domains.txt` deliberately excludes `api.anthropic.com`; the same hands-off treatment should apply to any new LiteLLM upstream — agents reach it only through the gateway, never directly.
- **Independent selection per agent.** The plumbing must let one agent be on Qwen while another in the same pipeline is on Claude. This is non-trivial: today the model choice is decided where the consensus wrapper is built, not where the request leaves the gateway.
- **No pinned model snapshot versions.** Use model aliases (e.g. `opus`, `sonnet`, `qwen3-coder-30b`) — matches the existing overseer-model defaults.
- **File-size discipline (#2261).** `gateway/gateway.py` is ~10K lines and `orchestrator/routes/pipelines.py` is ~16K. The plan should land new symbols in dedicated sub-modules (per the in-flight decomposition tables in `gateway/CLAUDE.md` and `orchestrator/CLAUDE.md`) rather than accreting more into the barrels.
- **Build-now / validate-later split.** End-to-end validation requires a live non-Claude endpoint that is not yet set up. The integration ships **no-op by default** (Claude path untouched, LiteLLM routing inert until configured), so the no-op slice is buildable, testable, and reviewable independent of having a Qwen endpoint live.

## Options Considered

### Option A: Gateway-side upstream router + per-agent session metadata (recommended)

**Approach**: Decompose `get_anthropic_client()` into a tiny `UpstreamRegistry` keyed by upstream name (`anthropic`, `litellm`). `proxy_anthropic_messages()` and `proxy_count_tokens()` resolve the upstream **per request**, using the same IP-based session lookup that already drives `session_mode` (`gateway/gateway.py:9774`). The orchestrator records the per-agent upstream + model when it creates the gateway session at spawn time (the same place it already records `mode`). `_inject_anthropic_credentials()` becomes upstream-aware so LiteLLM-bound requests get the LiteLLM master key instead of the Anthropic OAuth / API key. The SSE accumulator, tool-strip, and stream-resilience logic are upstream-agnostic and unchanged.

LiteLLM runs as a **separate Deployment in `egg-system`** with a Service the gateway reaches over cluster DNS. The model name *in the request body* stays a recognized Claude alias (per the issue's mitigation), so Claude Code's compaction math stays correct; the **router decides on per-agent session metadata, not on the body**.

**Pros**:
- Claude path is structurally unchanged — same singleton client (now keyed by `"anthropic"`), same credential path, same SSE plumbing.
- Inert by default: with no agent configured for a non-Claude model, no LiteLLM request ever fires.
- Decouples model name from upstream — works around the Claude Code compaction-math limitation the issue flags as the primary risk without lying to the upstream API (LiteLLM still receives the real model name from a header or per-session override; only Claude Code sees the alias).
- Per-agent granularity falls out naturally from the IP-keyed session metadata that already exists.
- LiteLLM blast radius (PyPI supply-chain, version pinning, restart cycles) is contained to one pod separate from the gateway.

**Cons**:
- Two source-of-truth coordination points: orchestrator declares the per-agent upstream when it creates the gateway session; agent's `--model` string must agree with what the gateway will route. A drift here produces a wrong-backend request. (Mitigation: spawner derives both from the same per-role config field.)
- Adds a per-role config field to `PipelineConfig` (plus precedence rules if we layer repo / CLI overrides).
- Adds an LiteLLM deployment to operate (image, version pin / cosign verification, model config YAML, restart policy).

### Option B: LiteLLM-fronts-everything

**Approach**: All `/v1/messages` traffic — Claude and non-Claude — flows through LiteLLM. LiteLLM's model-list does the upstream selection based on model name. Gateway forwards verbatim to LiteLLM and forgets about Anthropic entirely.

**Pros**:
- Single upstream from the gateway's perspective — `get_anthropic_client()` keeps its singleton shape, just with a different `base_url`.
- LiteLLM's `model_list` is purpose-built for the "claude-* → anthropic, qwen-* → vllm" mapping pattern.

**Cons**:
- Adds a hop (and an extra failure mode, supply-chain blast radius, restart-cycle dependency) to **every Claude request**, including the existing production path.
- Issue explicitly says "the existing Claude path must keep working unchanged — no regression for agents that stay on Claude". This option fails that gate.
- Auto-compaction risk gets worse, not better: now even Claude requests transit LiteLLM, so any LiteLLM-side transformation of the model name reaches Claude Code's compaction math.

### Option C: Decide on the model name in the request body

**Approach**: `proxy_anthropic_messages()` parses the request body's `model` field and routes to Anthropic if it starts with `claude-`, otherwise to LiteLLM. No new orchestrator plumbing, no per-agent session metadata.

**Pros**:
- Smallest gateway change (one regex / prefix check inside the existing handler).
- No orchestrator coordination required — the model string in the agent's `--model` flag is the single source of truth.

**Cons**:
- **Directly conflicts with the issue's primary-risk mitigation.** The mitigation requires presenting Claude Code a *recognized Claude alias* even when the actual backend is Qwen, so that Claude Code's compaction math stays sane. Routing on the body means we cannot do that — we have to lie to *both* Claude Code and the gateway.
- Couples upstream selection to model-name conventions: if Anthropic ever ships a non-`claude-*`-prefixed model (or LiteLLM exposes a `claude-*`-aliased Qwen), the router breaks silently.

### Option D: Per-agent `egg_agent` SDK path, bypass Claude Code entirely

**Approach**: For non-Claude agents, swap the consensus-wrapper's `python3 -m egg_agent` invocation away from Claude Code (which the issue identifies as the primary risk surface) and use the egg_agent SDK directly with `--model qwen-...`. The gateway still routes per-agent, but the Claude-Code-harness compatibility surface drops out of the problem.

**Pros**:
- Sidesteps the Claude Code auto-compaction and feature-gating risks entirely (no Claude Code in the loop for non-Claude agents).
- The `egg_agent` SDK path (`shared/egg_agent/client.py`) already takes `--model` as a string and passes it through to the Claude Agent SDK, so the model wiring is small.

**Cons**:
- The SDK path has fewer integrations than the CLI (statusline, `settings.json` rules, the in-process MCP servers registered by `egg_agent.client` are wired but the slash-command / claude-rules surface is not).
- Doubles the test surface for the first cut: now there are two harnesses to validate, not one.
- Adds a "which harness?" config knob to the orchestrator plumbing.

## Recommended Approach

**Option A** is recommended.

It is the only option that satisfies the issue's hard constraints:
1. Claude path remains unchanged in behavior, structure, and risk profile.
2. Per-agent model selection is real (not a global swap).
3. The compaction-math mitigation is supported (route on session metadata, not on the body — so Claude Code can see `opus` while the gateway routes to Qwen via LiteLLM).
4. The router sits below the SSE accumulator, tool-strip, and stream-resilience logic, so they all keep working untouched.

It also matches the existing architecture's grain: the gateway is already the per-request policy point, IP-based session lookup is already the way per-agent state reaches request handlers, and an additive Deployment in `egg-system` is the standard pattern (see `gateway`, `orchestrator`). LiteLLM blast radius is contained to one pod.

Open questions on topology, routing signal, configuration shape, validation target, harness choice, credential handling, failure policy, and slice decomposition are surfaced below and gated on the operator.

## Runtime-primitive assumptions for the downstream plan (#2594)

The plan phase will rely on these primitives existing in their named files/shapes:

| Primitive | Where it lives today | Used by | Execution context |
|-----------|----------------------|---------|--------------------|
| `_anthropic_client` singleton + `get_anthropic_client()` | `gateway/gateway.py:9316-9329` | `proxy_anthropic_messages` (`gateway/gateway.py:9789`), `proxy_count_tokens` (`gateway/gateway.py:10032`) | Gateway pod (`egg-system`) |
| `proxy_anthropic_messages()` route `POST /v1/messages` | `gateway/gateway.py:9752` | Sandbox `ANTHROPIC_BASE_URL` traffic | Gateway pod |
| `proxy_count_tokens()` route `POST /v1/messages/count_tokens` | `gateway/gateway.py:10019` | Same | Gateway pod |
| `_inject_anthropic_credentials()` | `gateway/gateway.py:9355` | Both proxy routes | Gateway pod |
| `get_credentials_manager().get_credential()` (returns `Credential` with `header_name`, `header_value`) | `gateway/anthropic_credentials.py` (imported at `gateway/gateway.py:87` / `:217`) | `_inject_anthropic_credentials` | Gateway pod |
| `_filter_blocked_tools(request_body, session_mode)` | `gateway/gateway.py:9410` | `proxy_anthropic_messages` | Gateway pod |
| `_SSEAccumulator` (incremental SSE parser) | `gateway/gateway.py:9552` | `proxy_anthropic_messages` streaming branch | Gateway pod |
| `get_session_manager().get_session_by_ip(addr)` (returns session with `.mode`, `.container_id`) | imported `gateway/gateway.py:200` from `gateway/session_manager.py` | `proxy_anthropic_messages` (`gateway/gateway.py:9775`) | Gateway pod — this is the routing-signal carrier in the recommended option |
| `build_consensus_wrapped_command(prompt_text, model="opus", ...)` | `orchestrator/consensus_wrapper.py:620` | Spawn (`orchestrator/concurrent_executor.py:454`) + restart (`orchestrator/routes/pipelines.py:2704`) | Trusted CI runner (orchestrator pod) |
| `PipelineConfig.overseer_decision_maker_model` (`str`, default `"sonnet"`) | `orchestrator/models.py:546` | `pipelines.py:536`, `pipelines.py:20509`, `kubernetes_spawner.py:1596`, `overseer/monitor.py:259` | Trusted CI runner |
| `PipelineConfig.overseer_advisor_model` (`str`, default `"opus"`) | `orchestrator/models.py:620` | `pipelines.py:3633` and overseer advisor invocation | Trusted CI runner |
| `KubernetesSpawner._PROTECTED_ENV_KEYS` | `orchestrator/kubernetes_spawner.py:138` | `_resolve_wait_producer_allowlist` and pod env composition (`kubernetes_spawner.py:789-892`) | Trusted CI runner |
| `environment["ANTHROPIC_BASE_URL"] = GATEWAY_K8S_URL` | `orchestrator/kubernetes_spawner.py:807` | Every spawned agent pod | Trusted CI runner (sets in-sandbox env) |
| `environment["EGG_AGENT_ROLE"]`, `environment["EGG_AGENT_*"]` plumbing pattern | `orchestrator/kubernetes_spawner.py:792` | Sandbox env | Trusted CI runner |
| Sandbox `ANTHROPIC_BASE_URL` setup | `sandbox/entrypoint.py:737-738` (`setup_anthropic_api()`) | In-sandbox-agent | In-sandbox agent |
| `DEFAULT_MODEL = "opus[1m]"` | `shared/egg_agent/client.py:62` | `egg_agent.client.run_agent_async` | In-sandbox agent |
| `--model` default `"opus[1m]"` CLI flag | `shared/egg_agent/__main__.py:35` | `python3 -m egg_agent` | In-sandbox agent |
| `["--model", "opus[1m]"]` Claude CLI invocation | `sandbox/llm/runner.py:49` | Legacy interactive runner | In-sandbox agent |
| `allowed_domains.txt` — Anthropic-excluded Squid allowlist (`gateway/allowed_domains.txt:9-15`) | Squid config in gateway pod | Outbound sandbox HTTPS | Gateway pod |
| K8s Service DNS `gateway.egg-system.svc.cluster.local:9848` (`GATEWAY_K8S_URL`) | `orchestrator/kubernetes_spawner.py:124` | All sandbox pods | Cluster-wide DNS |
| `k8s/base/gateway-deployment.yaml` Anthropic/Atlassian secret layout (`secrets.env` mount at `/secrets`) | `k8s/base/gateway-deployment.yaml:71-118` | Where any new LiteLLM master key would land | Cluster |

## Open Questions

### Resolved in Pre-Refine

_(None — the issue body has no `## Additional Context` section. Every question below is genuinely open.)_

### Decisions

<!-- egg-decision id=cq-1 -->

**Where should the LiteLLM proxy run, topologically?**

- [ ] Separate Deployment+Service in egg-system namespace (1 LiteLLM pod, gateway calls it over the cluster Service DNS)
- [ ] Sidecar container in the gateway pod (same pod, localhost call, shares lifecycle)
- [ ] Separate namespace `egg-llm` with its own NetworkPolicy (stronger isolation, more ops surface)
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-2 -->

**How should the gateway decide which upstream (`api.anthropic.com` vs LiteLLM) to use for a given `/v1/messages` request?**

- [ ] Per-agent session metadata: orchestrator declares the model+upstream when it spawns the agent (session lookup by IP, same path used today for `session_mode`) — model name in the body is informational only
- [ ] Custom HTTP header from the sandbox (e.g. `X-Egg-Upstream: litellm`) injected at agent startup — gateway reads the header and routes accordingly
- [ ] Model name in the request body (any non-Claude model name → LiteLLM) — simplest but conflicts with the issue's compaction-mitigation note about presenting a recognized alias to Claude Code
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-3 -->

**How should per-agent model selection be configured (i.e. where does an operator say 'run the refiner on Qwen, leave the coder on Claude')?**

- [ ] New per-role field on `PipelineConfig` (alongside `overseer_decision_maker_model` / `overseer_advisor_model`) — e.g. `agent_models: {refiner: 'qwen3-coder', coder: 'opus'}`
- [ ] Repository-level YAML config (`config/repositories.yaml` or similar) — operator edits once, applies to every pipeline on that repo
- [ ] Per-pipeline override only (CLI flag / API payload on submit_task) — no persistent per-role default, the operator names the override at submission time
- [ ] All of the above stacked, with precedence: CLI > pipeline config > repo config > built-in default 'opus'
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-4 -->

**Which agent role should be the first to be flipped to a non-Claude model for the empirical compatibility validation that the issue calls out as the acceptance test?**

- [ ] A reviewer role (e.g. `reviewer_refine`) — reviewers do less tool-heavy work and have shorter sessions, so this is the lowest-risk first cut
- [ ] The `refiner` (this role) — produces analysis docs, modest tool use, easy to compare output against the Claude baseline
- [ ] The `coder` — most tool-heavy and longest-running role, so it stresses the auto-compaction edge case the issue flags as the primary risk
- [ ] An overseer tier (decision-maker or advisor) — already model-configurable today, so the plumbing is smaller, but it's less representative of the main SDLC loop
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-5 -->

**For the first non-Claude target, should agents continue to run inside the Claude Code harness (relying on the LiteLLM Anthropic-translation seam) or switch to the egg_agent SDK path?**

- [ ] Keep the Claude Code harness for non-Claude models too — use the recognized-alias mitigation so compaction math stays sane; minimum disruption to the spawning + entrypoint code
- [ ] Route non-Claude agents through the `egg_agent` SDK path (which already supports `--model`) and bypass Claude Code entirely — sidesteps the auto-compaction risk, but the SDK path has fewer integrations (statusline, settings.json rules) than the CLI
- [ ] Both: leave the harness choice as a per-role/per-model config knob — maximally flexible, but doubles the test surface for the first cut
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-6 -->

**What should the first acceptance-test backend be for the non-Claude path?**

- [ ] Self-hosted Qwen on vLLM/SGLang (matches the long-term cost goal and is the primary stated target) — requires standing up a vLLM Deployment + model weights as part of validation
- [ ] A hosted Qwen-compatible provider (e.g. Together, Fireworks, DeepInfra, OpenRouter) — fastest path to a live endpoint; defers the self-hosting work but adds a third-party dependency and a new credential to hold
- [ ] An OpenAI/other already-trusted backend behind LiteLLM as the literal first smoke test, with Qwen as the second cut — lowest validation risk; lets us decouple 'gateway routing works' from 'Qwen tool-calling works'
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-7 -->

**How should the gateway handle credentials for the LiteLLM upstream?**

- [ ] Gateway holds a LiteLLM master key in `secrets.env` and injects it on every LiteLLM-bound request (mirrors today's `ANTHROPIC_API_KEY` injection pattern) — LiteLLM holds the real per-backend keys
- [ ] LiteLLM runs with no auth, network-isolated to the gateway (NetworkPolicy or shared pod) and the gateway passes raw upstream credentials per-request — fewer secrets to hold, but pushes per-backend key management into the gateway
- [ ] Gateway holds nothing for LiteLLM; the sandbox sets its own per-agent API key via `extra_env` (e.g. operator-supplied) — inverts today's zero-credential sandbox invariant, so probably a non-starter
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-8 -->

**When the LiteLLM proxy is unreachable / errors for a non-Claude agent, what is the failure policy?**

- [ ] Fail closed (502 to the agent, no fallback) — same policy as today's Claude upstream errors; surfaces the misconfig immediately
- [ ] Fall back to Claude on transient LiteLLM failures only — keeps the pipeline progressing but produces a quietly-mixed transcript and erodes the cost goal
- [ ] Fail closed but auto-escalate to a HITL decision when a non-Claude agent fails to spawn or stalls — best operator UX, most code to write
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-9 -->

**In private mode (PR #686 / #702), the gateway strips `WebSearch` / `WebFetch` tools from outbound requests because those route through Anthropic's infrastructure and bypass container network controls. What should the equivalent policy be when the upstream is LiteLLM → self-hosted Qwen on vLLM (no Anthropic-side tool processing)?**

- [ ] Keep the same tool-strip in private mode regardless of upstream — conservative; the agent simply cannot call these tools whether or not they would exfiltrate
- [ ] Strip only when upstream is Anthropic; allow these tools when upstream is fully self-hosted (no external request hop) — unblocks the tools but adds upstream-aware logic to the filter
- [ ] Keep the strip but document that the rationale only applies to Anthropic upstreams — defer the per-upstream rule to a future issue
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-10 -->

**How should this work be decomposed into slices?**

- [ ] Single slice: gateway router + LiteLLM topology + per-agent model config + acceptance-test agent flip, all together (1 PR)
- [ ] Two slices in parallel: [gateway upstream router + LiteLLM topology, no-op by default] || [per-agent model config + consensus_wrapper plumbing] (2 PRs) — acceptance-test agent flip becomes a follow-up
- [ ] Two slices with dependency: [gateway router + LiteLLM topology, no-op] → [per-agent model config + acceptance-test agent flip on top] (2 PRs)
- [ ] Three slices with dependency: [gateway router + LiteLLM topology, no-op] → [per-agent model config plumbing] → [acceptance-test agent flip + validation] (3 PRs)
- [ ] Other (explain in reply)

<!-- egg-decision id=cq-11 -->

**Should the `[1m]` Claude-only context-window syntax baked into the existing defaults (`shared/egg_agent/client.py:62`, `shared/egg_agent/__main__.py:35`, `sandbox/llm/runner.py:49`) be addressed in this change?**

- [ ] Leave it: Claude defaults keep `opus[1m]`; only non-Claude paths use a different model string. The `[1m]` is harmless on the Claude path — zero risk to existing behavior
- [ ] Refactor: hoist the model string into a single config helper that strips `[1m]` when the resolved upstream is non-Claude — cleaner, but more churn outside the issue's scope
- [ ] Deprecate `opus[1m]` and adopt plain `opus` everywhere — lose the 1M context window for current Claude agents to keep model strings backend-agnostic
- [ ] Other (explain in reply)

### Feedback

<!-- egg-feedback id=feedback-1 -->

## Questions & Feedback

Please **edit this comment** to answer questions or provide feedback.
When you're done, check the box below to submit.

---

### Open Questions

**Q1: For self-hosted Qwen specifically, are there hardware/budget constraints already settled (which GPU, how many, vLLM vs SGLang) that the plan should anchor on, or is the validation expected to use a hosted Qwen provider first regardless of the long-term self-hosted target?**

> _Your answer here_

**Q2: Is there a target list of agent roles you eventually want on non-Claude models (e.g. all reviewers, only refiner+tester, everything except coder), or is this open-ended and the first flip just proves the seam?**

> _Your answer here_

**Q3: The issue notes LiteLLM was chosen over claude-code-router, but does the design need to keep a clean swap-out point (e.g. an `UpstreamRouter` interface) in case LiteLLM hits a similar maintenance/supply-chain problem (the March 2026 PyPI incident is recent), or is hard-wiring LiteLLM acceptable for the first cut?**

> _Your answer here_

**Q4: The existing `max_llm_cost_per_hour` envelope assumes Anthropic-priced tokens. Should the implementation budget include extending cost tracking to LiteLLM/Qwen tokens in this issue, or defer to a follow-up?**

> _Your answer here_

**Q5: Are there any compliance / data-residency constraints (e.g. egg's source code or transcripts cannot transit a third-party Qwen hosting provider) that should rule out option B on the backend question (hosted Qwen) up front?**

> _Your answer here_

---

### Additional Feedback (optional)

> _Add any other feedback or context here_

---

- [ ] Submit feedback (I'm done editing)

---

## Complexity Assessment

**high** — this is a cross-cutting architectural change. It touches:

- a new long-running cluster component (LiteLLM Deployment + Service + secrets)
- the gateway's per-request upstream selection (currently a singleton)
- the gateway's credential injection (currently single-provider)
- new per-agent / per-role configuration in `PipelineConfig` (and possibly repo config and CLI)
- the orchestrator's spawn path (the consensus wrapper's hardcoded model arg) and the spawner's pod-env composition
- an empirical compatibility-validation surface (Claude Code's compaction math against a real non-Claude backend)
- arguably a new harness path if option D wins

It is decomposable into at least two independent slices (gateway router + LiteLLM topology as a no-op shipment, then per-agent model config + acceptance-test flip on top), which is reflected in cq-10.

---

*Authored-by: egg*

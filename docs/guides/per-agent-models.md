# Per-Agent Models — Running a Single Agent on a Non-Claude Backend

This guide walks an operator through the **slice-2** plumbing of
[#2769](https://github.com/jwbron/egg/issues/2769): flipping any single
SDLC phase agent role (refiner, coder, tester, a reviewer, …) to a
non-Claude model via the gateway's LiteLLM proxy, without touching
agent prompts, the sandbox, or source code. By the end you will have
configured a pipeline that runs (e.g.) the refiner on hosted Qwen while
every other role continues to run on Claude.

The seam itself — the gateway-side `UpstreamRegistry`, the LiteLLM
Deployment, the per-session routing decision — is described in
[Upstream Routing](../architecture/upstream-routing.md) and was the
slice-1 deliverable. This guide is the operator-facing complement: the
two configuration fields slice 2 introduces, how they compose, the
**recognized-alias** mitigation that keeps Claude Code's auto-compaction
math sane on non-Claude routes, and the end-to-end smoke test that
validates a live LiteLLM endpoint.

## Mental model in one sentence

> Each agent role independently resolves to a `(claude_code_alias,
> upstream, upstream_model)` triple — the `--model` flag handed to
> Claude Code stays a recognized Claude alias on every route, while
> the gateway rewrites the request body's `model` field to the
> upstream-side name (`"qwen3-coder-30b"`, …) on LiteLLM-bound
> requests so the proxy targets the right backend.

The default path (every agent on Claude, every request to
`api.anthropic.com`) is **byte-identical to today** — no config means
no behavioral change.

## The two configuration knobs

### Per-pipeline override — `PipelineConfig.agent_models`

`agent_models: dict[str, str]` on
[`PipelineConfig`](../../orchestrator/models.py) (the field lives next
to the existing overseer-tier model fields around
`orchestrator/models.py:757`). Keys are
[`AgentRole`](../../shared/egg_contracts/agent_roles.py) values, but
**only the SDLC phase producer and reviewer roles** (`"coder"`,
`"refiner"`, `"tester"`, the `"reviewer_*"` roles, …) — the roles
spawned through the paths that consult the resolver. A Pydantic
validator rejects both typos *and* otherwise-valid roles the resolver
never honors — the utility roles `autofixer` / `conflict_resolver` and
the interface roles `overseer` / `inspector` spawn through dedicated
paths that bypass `resolve_agent_model`, so an override naming one of
them would silently no-op at spawn. The honored set is the constant
`agent_roles.MODEL_OVERRIDE_ROLES`; the validator surfaces a
misconfigured key immediately at construction time. Values are
free-form model strings — interpreted by the resolver (below).

```python
PipelineConfig(
    agent_models={
        "refiner": "qwen3-coder-30b",   # → LiteLLM, "qwen3-coder-30b"
        "coder":   "sonnet",            # → Anthropic, "sonnet"
    },
    # other fields unchanged …
)
```

Default-constructed `PipelineConfig.agent_models` is `{}` — every
existing pipeline continues to spawn every role on the built-in
`"opus"` Claude default with `upstream="anthropic"`.

### Repository-level default — `default_agent_model`

`default_agent_model: str | None` on `repositories.yaml`, read by
[`get_default_agent_model(repo)`](../../config/repo_config.py) in
`config/repo_config.py` (mirroring the existing
`get_repo_setting(repo, key, default)` pattern at
`config/repo_config.py:248`). Documented in
[`config/repositories.yaml.example`](../../config/repositories.yaml.example)
with an inline precedence note.

```yaml
# ~/.config/egg/repositories.yaml
repo_settings:
  acme-corp/widgets:
    # Every role defaults to Sonnet on this repo, unless a pipeline
    # passes its own agent_models entry.
    default_agent_model: sonnet
```

When set, the field applies to **every role** that the per-pipeline
`agent_models` doesn't already pin.

### Precedence

The resolver
[`resolve_agent_model(role, pipeline_config, repo)`](../../orchestrator/agent_model_resolution.py)
in `orchestrator/agent_model_resolution.py` walks the chain:

1. `pipeline_config.agent_models.get(role.value)` — per-pipeline,
   per-role override
2. `get_default_agent_model(repo)` — repository-level default
3. Built-in `"opus"` — the historical default; preserves today's
   behavior unchanged

The result is an `AgentModelDecision` dataclass with fields
`(claude_code_alias: str, upstream: str, upstream_model: str | None)`.

### Classifier — Claude alias vs. LiteLLM upstream

The resolver's classifier divides model strings into two camps:

| Model string pattern | `upstream` | `claude_code_alias` | `upstream_model` |
|----------------------|------------|---------------------|------------------|
| `opus`, `opus[1m]`, `sonnet`, `sonnet[1m]`, `haiku` | `"anthropic"` | the model string verbatim | `None` |
| `claude-*` (e.g. `claude-3-5-sonnet-20241022`) | `"anthropic"` | the model string verbatim | `None` |
| Everything else (e.g. `qwen3-coder-30b`, `mistral-large`) | `"litellm"` | **`"opus"`** (the cq-5 mitigation — see below) | the model string verbatim |

Two consequences:

- The Anthropic path produces no wire change at all when the resolved
  model is a Claude alias — `upstream_model` is `None`, so the gateway
  forwards the body verbatim.
- The LiteLLM path always presents Claude Code the alias `"opus"` —
  see *Recognized-alias mitigation* below for why this matters.

## How the resolved decision threads through spawn

Two sites resolve and thread the decision into the consensus-wrapper
command + gateway session-create call:

| Spawn site | Threads `--model` to | Threads `upstream` / `upstream_model` to |
|------------|----------------------|-------------------------------------------|
| Initial spawn at `orchestrator/concurrent_executor.py:454` | `build_consensus_wrapped_command(model=decision.claude_code_alias, …)` | `GatewayClient.register_session(upstream=…, upstream_model=…)` at `orchestrator/kubernetes_spawner.py:735` (the new kwargs land on the slice-1 wire contract) |
| Restart path at `orchestrator/routes/pipelines.py:2704` | Same `--model` resolution | `restart_agent_job` → `spawn_agent_job` → `register_session(upstream=…, upstream_model=…)` — a restart respawns the Job and registers a **new** gateway session carrying the resolved decision |

When the resolved decision is the default Claude case
(`upstream="anthropic"`, `upstream_model=None`),
`ConcurrentPhaseExecutor._spawn_agent` omits the `upstream` /
`upstream_model` kwargs from its `spawn_fn` call entirely
(`concurrent_executor.py:501-503`), so test mocks and legacy spawn
paths see the pre-#2769 call signature. One layer down,
`spawn_agent_job` still passes both kwargs to
`GatewayClient.register_session` (`kubernetes_spawner.py:770-771`) — as
`None` on the default path — and `register_session` drops `None`
values from the session-create request body
(`gateway_client.py:707-712`), so the wire shape stays byte-identical
to today. This is the slice-2 regression guard exercised by the
existing concurrent-executor tests.

## The gateway-side body rewrite

For LiteLLM-bound requests, the gateway has to make the upstream model
name reach LiteLLM — Claude Code is still sending `"model": "opus"` in
the body it constructs. The rewrite lives in a new helper
`_rewrite_upstream_model(request_body, upstream_model)` colocated with
`_filter_blocked_tools` in `gateway/gateway.py` (slice-1 added
`_filter_blocked_tools` at `gateway/gateway.py:9496`; the new helper
sits adjacent to it).

Called from `proxy_anthropic_messages`
(`gateway/gateway.py:9839`) and `proxy_count_tokens`
(`gateway/gateway.py:10135`) **after** `_filter_blocked_tools` and
**before** building the upstream request:

```
sandbox ──POST /v1/messages──▶ gateway
                                  │
                                  ├─ session_manager.get_session_by_ip(...)
                                  │     → session.upstream = "litellm"
                                  │     → session.upstream_model = "qwen3-coder-30b"
                                  │
                                  ├─ _inject_upstream_credentials(headers, "litellm")
                                  ├─ _filter_blocked_tools(body, session.mode)
                                  ├─ _rewrite_upstream_model(body, session.upstream_model)
                                  │     → body["model"]: "opus" → "qwen3-coder-30b"
                                  │
                                  ├─ client = registry.get("litellm")[0]
                                  └─ stream upstream → accumulate → return SSE downstream
```

Behavior at the edges:

- **`upstream="anthropic"` (or `upstream_model is None`)** — the body
  is returned unchanged. The Anthropic regression guard is enforced
  with a test that sends a non-default incoming `"model"` (e.g.
  `"opus"`) and asserts byte-identical forwarding.
- **Invalid JSON in the request body** — the rewrite is a no-op (the
  original bytes are returned). Parsing failures never crash the
  proxy; they fall through to the existing upstream and let the
  upstream produce whatever error response it would have produced.

## Recognized-alias mitigation (cq-5) — *plausible, not empirically proven*

> The behavior described in this section is the cq-5 *mitigation* for
> a harness compatibility risk — not a proven guarantee. Confirming
> compaction math actually stays sane on long non-Claude sessions is
> the cq-4-deferred operator smoke test below. Read the invariants
> as "what the resolver enforces structurally" rather than "what
> Claude Code is guaranteed to do."

The harness Claude Code runs the agent inside makes **decisions keyed
on the model name** that we cannot ask it to override:

- It derives a model's **context window** — and therefore
  **auto-compaction timing** — from the `--model` flag.
- Some features (e.g. extended thinking) are gated on recognized
  model names.

An unrecognized model name gets a fallback context window that does
not match the real backend. On long sessions this produces
over-length requests that hard-fail and wedge the agent.

The cq-5 mitigation, baked into the resolver's classifier above:

> For every LiteLLM-routed agent, Claude Code is told `--model opus`.
> The gateway separately rewrites the on-the-wire `"model"` field to
> the LiteLLM-side name (`"qwen3-coder-30b"` etc.).

Two structural invariants follow (these are enforced by the resolver
and tested explicitly; *whether* they are sufficient to keep Claude
Code's compaction math sane on a given backend remains the smoke
test's job):

- The Claude-Code-facing alias is **always** a recognized Claude name
  (`opus` by default).
- The model name actually requested upstream is **always** the
  configured `upstream_model`. LiteLLM dispatches to the right
  `model_list` entry.

Tests in `orchestrator/tests/test_agent_model_resolution.py` and
`tests/gateway/test_anthropic_proxy.py` assert both invariants
explicitly — in particular that the Claude-Code-facing alias for a
LiteLLM-routed agent is `"opus"`, never the upstream model name.

## Operator walkthrough — Qwen for the refiner role

This walks through enabling hosted Qwen for a single role end-to-end.
None of these steps modify egg source code.

### 1. Provision LiteLLM credentials

Add the LiteLLM gateway master key (a random secret you generate; the
gateway forwards it on every LiteLLM-bound request as
`x-api-key`):

```bash
# ~/.config/egg/secrets.env
LITELLM_MASTER_KEY=<random_secret>
```

The master key is the credential the gateway itself uses to talk to
LiteLLM. Provider-side keys (the hosted-Qwen API key, the OpenRouter
key, etc.) flow through the same `secrets.env` → `gateway-secrets` →
LiteLLM Deployment env path — see the next step. The gateway pod
rereads `secrets.env` on mtime change.

### 2. Configure LiteLLM `model_list`

Per-operator backend choices live in a host-side overlay, **not** in
the committed `k8s/base/litellm-configmap.yaml`. Copy the template and
edit:

```bash
cp config/litellm-models.template.yaml ~/.config/egg/litellm-models.yaml
$EDITOR ~/.config/egg/litellm-models.yaml
```

The file is a JSON merge patch (RFC 7396, via `kubectl patch
--type=merge`) for the in-cluster `litellm-config` ConfigMap — its
`data.config.yaml` replaces the empty default in full, so include both
`model_list` and `general_settings`:

```yaml
# ~/.config/egg/litellm-models.yaml
data:
  config.yaml: |
    model_list:
      - model_name: qwen3-max
        litellm_params:
          model: openrouter/qwen/qwen3-max
          api_key: os.environ/OPENROUTER_API_KEY
    general_settings:
      master_key: os.environ/LITELLM_MASTER_KEY
```

`OPENROUTER_API_KEY` is wired end-to-end out of the box: set it in
`~/.config/egg/secrets.env` alongside `LITELLM_MASTER_KEY`, and
`make k3s-secrets` extracts it onto the `gateway-secrets` Secret while
`k8s/base/litellm-deployment.yaml` binds it as a `secretKeyRef` env
var on the LiteLLM container (`optional: true` so deployments without
OpenRouter still start).

Adding any other provider key (e.g. `TOGETHER_API_KEY`) is **not**
automatic — it additionally requires (1) extending the `k3s-secrets`
target in `Makefile` to read the new variable from `secrets.env` and
surface it as a literal on `gateway-secrets`, and (2) adding a matching
`secretKeyRef` env entry on the LiteLLM container in
`k8s/base/litellm-deployment.yaml`. Without both edits,
`os.environ/<NEW_KEY>` in the overlay resolves to empty at request
time and the provider returns silent 401s.

Apply by running `make litellm-config` (also invoked automatically by
`make deploy` and `make redeploy`). The target patches the live
ConfigMap and rolls the LiteLLM Deployment to pick up the new config.

> **Hosted-provider choice.** Hosted Qwen is the cq-6 first target;
> any LiteLLM-supported backend works. Self-hosted vLLM / SGLang is
> deferred until the no-op-by-default path is validated against a
> hosted provider first.

### 3a. Per-repository default (recommended for stable rollouts)

If every role on this repo should default to the same model, edit
`~/.config/egg/repositories.yaml`:

```yaml
repo_settings:
  acme-corp/widgets:
    # Every role on this repo defaults to qwen3-max, unless a
    # pipeline passes its own agent_models entry.
    default_agent_model: qwen3-max
```

`default_agent_model` is a *repo-level default for every role* — to
flip exactly one role to a non-Claude backend (the common case for
the cq-4 smoke test), prefer 3b.

### 3b. Per-pipeline override (recommended for the smoke test)

For one-off pipelines (or the smoke test below), pass `agent_models`
in the `submit_task` MCP-tool arguments
([`orchestrator/mcp_tools.py:74`](../../orchestrator/mcp_tools.py) —
`required: ["description", "repo"]`; `issue_number`, `branch`, and
`config` are optional):

```json
{
  "description": "Smoke-test the refiner on hosted Qwen",
  "repo": "acme-corp/widgets",
  "issue_number": 1234,
  "config": {
    "agent_models": {
      "refiner": "qwen3-max"
    }
  }
}
```

The equivalent `POST /pipelines` HTTP body (see
`orchestrator/routes/pipelines.py:1336` where the handler reads
`data.get("issue_number")`) uses the same field names plus an
optional `branch` override:

```json
{
  "issue_number": 1234,
  "repo": "acme-corp/widgets",
  "branch": "egg/issue-1234/work",
  "config": {
    "agent_models": {
      "refiner": "qwen3-max"
    }
  }
}
```

> The orchestrator silently ignores unrecognized top-level keys
> (`data.get("issue_number")` reads `issue_number` specifically), so
> a misspelled `"issue": 1234` would submit a pipeline with **no
> issue binding** without surfacing an error. Use `issue_number`.

Per-pipeline `agent_models` entries **override** the repo-level
`default_agent_model`. Both can be unset — the resolver falls back to
the built-in `"opus"`.

### 4. Run the pipeline and observe routing

Submit a pipeline. The gateway audit log records the per-session
routing decision **once per session** (slice-1's
`audit_log("session_created", …)` extension at
`gateway/gateway.py:8920` includes the resolved `upstream` and
`upstream_model`); every subsequent `/v1/messages` request from that
session inherits the decision implicitly via the session-keyed
lookup, with no per-request routing log line:

- **Refiner session-created line**: `upstream=litellm`,
  `upstream_model=qwen3-max`. Subsequent refiner requests have
  their body forwarded to
  `litellm.egg-system.svc.cluster.local:4000` with the
  `_rewrite_upstream_model` helper substituting the `"model"` field;
  LiteLLM routes to the hosted Qwen backend.
- **Every other session-created line**: `upstream=anthropic`,
  `upstream_model=null`. Subsequent requests have their body
  forwarded byte-identically to `api.anthropic.com`.

If anything is misconfigured (LiteLLM master key absent, LiteLLM pod
unreachable, etc.), the failure policy is **fail closed**: a 502
surfaces to the agent. No silent fallback to Claude — this is cq-8
and intentionally matches today's Anthropic-side failure shape so a
mixed transcript can't quietly erode the cost goal motivating the
work. See [Upstream Routing → Failure
policy](../architecture/upstream-routing.md#failure-policy).

### 5. Exercise the cq-4 smoke test

The two compatibility properties only the live path can prove:

1. **Tool-heavy multi-turn loop.** Pick an issue whose refine phase
   exercises the agent's tool surface (file reads, web fetches, MCP
   tools). Watch the transcript: each tool call should round-trip
   cleanly with no stream corruption (the
   `claude-code-router`-style failure mode for Qwen thinking-mode
   models was the explicit reason LiteLLM was chosen — confirm we are
   not seeing it in practice).
2. **Auto-compaction boundary.** Run a session long enough that
   Claude Code triggers its auto-compaction step. With the
   recognized-alias mitigation, the compaction should fire on
   schedule and the agent should resume cleanly. If the agent wedges
   on an over-length request, the alias mitigation has failed and the
   model needs a custom context-window override (an out-of-scope
   follow-on for #2769).

Capture the transcript via `egg-checkpoint show <ckpt>` and the
gateway audit log via the structured-logging stream
([architecture/logging](../architecture/logging.md)).

This validation step is **operator-driven and out of scope for
slice-2 merge** (per the cq-4 resolution): merging slice 2 ships only
the buildable seam. The smoke test runs once an operator has a live
LiteLLM endpoint to point at.

## No-op-by-default invariant — three independent guards

The combination of slices 1 and 2 keeps the LiteLLM client cold on a
deployment that has not been configured. The guards (described in
detail at [Upstream Routing → No-op-by-default
invariant](../architecture/upstream-routing.md#no-op-by-default-invariant)):

1. **No LiteLLM master key configured.** The LiteLLM credential
   resolver returns `None`; any request that *did* somehow route to
   LiteLLM would fail credential injection with the standard 401.
2. **Session upstream defaults to `"anthropic"`.** Both the
   `Session.upstream` default and the `/api/v1/sessions/create`
   handler default are `"anthropic"`. Slice 2 only sets it when the
   resolver returns a LiteLLM decision.
3. **`agent_models` default is empty.** Both
   `PipelineConfig.agent_models` and repo-level
   `default_agent_model` default to nothing — the resolver returns
   the built-in `"opus"` Anthropic path.

Any single guard suffices. All three are independent; a
misconfiguration on one does not silently activate the LiteLLM path.

## Slice-2 primitives at a glance

| Primitive | Location | Purpose |
|-----------|----------|---------|
| `PipelineConfig.agent_models: dict[str, str]` | `orchestrator/models.py:757` (alongside the existing `PipelineConfig` fields) | Per-pipeline, per-role model override; Pydantic validator rejects keys outside the phase producer / reviewer set (`agent_roles.MODEL_OVERRIDE_ROLES`) |
| `default_agent_model: str \| None` | `config/repositories.yaml.example` (documented schema) | Repository-level default applied when `agent_models` does not pin the role |
| `get_default_agent_model(repo)` | `config/repo_config.py` (mirrors `get_repo_setting` at `config/repo_config.py:248`) | Reader for the repo-level default; returns `None` when absent |
| `resolve_agent_model(role, pipeline_config, repo)` + `AgentModelDecision` | `orchestrator/agent_model_resolution.py` *(new module)* | Walks precedence + classifies into `(claude_code_alias, upstream, upstream_model)` |
| Spawn-side plumbing | `orchestrator/concurrent_executor.py:454`, `orchestrator/routes/pipelines.py:2704`, `orchestrator/kubernetes_spawner.py:735` | Threads `--model` to the consensus wrapper and `upstream` / `upstream_model` to `GatewayClient.register_session` |
| `_rewrite_upstream_model(request_body, upstream_model)` | `gateway/gateway.py` (adjacent to `_filter_blocked_tools` at `gateway/gateway.py:9496`); called from `proxy_anthropic_messages` (`gateway/gateway.py:9839`) and `proxy_count_tokens` (`gateway/gateway.py:10135`) | Rewrites the body's `"model"` field on LiteLLM-bound requests; no-op for `"anthropic"` and for invalid JSON |

The slice-1 primitives this guide builds on (`UpstreamRegistry`,
`Session.upstream` / `upstream_model`, the LiteLLM credential
resolver, the LiteLLM k8s manifests) are catalogued in [Upstream
Routing → Files](../architecture/upstream-routing.md#files).

## HITL decisions that shape this guide

| Decision | Resolution | Where it shows up |
|----------|------------|-------------------|
| `cq-3` | Per-role field on `PipelineConfig` **and** `repositories.yaml` default | Two-knob config above; precedence chain in the resolver |
| `cq-4` | No agent-flip in this pipeline; operator smoke-test deferred | Smoke-test section is operator-driven, not gating merge |
| `cq-5` | Keep Claude Code; recognized-alias mitigation | `claude_code_alias` is always a Claude name; gateway rewrites body's `model` |
| `cq-6` | First validation backend is hosted Qwen | Step 2 example uses hosted Qwen; self-hosted deferred |
| `cq-8` | Fail closed on LiteLLM errors (502, no fallback) | Step 4 error-path note; same behavior as today's Anthropic upstream errors |
| `cq-11` | Leave `[1m]` for Claude | Non-Claude model strings simply do not carry `[1m]`; the resolver routes them via the LiteLLM path |

The full set is at
[`.egg-state/contracts/issue-2769.json`](../../.egg-state/contracts/issue-2769.json).

## Related Documentation

- [Upstream Routing](../architecture/upstream-routing.md) — slice-1
  architecture: gateway router, `UpstreamRegistry`, per-session
  routing, LiteLLM topology, credential layout, failure policy, and
  the no-op-by-default invariant.
- [Credential Injection](../architecture/credential-injection.md) —
  Anthropic credential resolver shape; the LiteLLM resolver follows
  the same mtime-invalidated cache pattern.
- [Orchestrator Architecture](../architecture/orchestrator.md) —
  Spawner / session-creation context that the slice-2 plumbing
  hooks into.
- [Agent Roles](../reference/agent-roles.md) — Canonical role names
  accepted as keys in `agent_models`.
- Issue [#2769](https://github.com/jwbron/egg/issues/2769) — Original
  motivation, refine-phase decisions, plan-phase risk analysis.

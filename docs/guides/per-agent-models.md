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
`ANTHROPIC_CUSTOM_MODEL_OPTION` env-var registration (#2832) that
keeps Claude Code's auto-compaction math sane on non-Claude routes,
and the end-to-end smoke test that validates a live LiteLLM endpoint.

## Mental model in one sentence

> Each agent role independently resolves to a `(claude_code_alias,
> upstream, upstream_model)` triple — on the Anthropic path
> `claude_code_alias` is a Claude alias and `upstream_model` is
> `None`; on the LiteLLM path `claude_code_alias` is the upstream
> name with a `[1m]` context-window-opt-in suffix (e.g.
> `qwen3-coder-30b[1m]`). The orchestrator threads the suffixed name
> into both the agent's `--model` flag and the
> `ANTHROPIC_CUSTOM_MODEL_OPTION` env var so Claude Code registers
> the custom model with 1M-window compaction math; Claude Code
> strips the suffix before the request hits the wire and LiteLLM
> matches the bare name against its `model_list`.

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
        "refiner": "qwen3-max",         # → LiteLLM, "qwen3-max"
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
| Everything else (e.g. `qwen3-coder-30b`, `mistral-large`) | `"litellm"` | `<model>[1m]` (the bare upstream name plus the 1M-context opt-in suffix) | the bare upstream name |

Two consequences:

- The Anthropic path produces no wire change at all when the resolved
  model is a Claude alias — `upstream_model` is `None`, so the gateway
  forwards the body verbatim.
- The LiteLLM path threads `<model>[1m]` into `--model` and the
  `ANTHROPIC_CUSTOM_MODEL_OPTION` env var. Claude Code strips the
  suffix before sending, so LiteLLM keys on the bare upstream name —
  the gateway does not rewrite the body.

## How the resolved decision threads through spawn

Two sites resolve and thread the decision into the consensus-wrapper
command, the agent's env, and the gateway session-create call:

| Spawn site | Threads `--model` + env to | Threads `upstream` / `upstream_model` to |
|------------|----------------------------|-------------------------------------------|
| Initial spawn in `orchestrator/concurrent_executor.py` (`_spawn_agent`) | `build_consensus_wrapped_command(model=decision.claude_code_alias, …)` plus `decision.env_vars()` merged into `extra_env` (sets `ANTHROPIC_CUSTOM_MODEL_OPTION` + `…_OPTION_NAME` on the LiteLLM path) | `GatewayClient.register_session(upstream=…, upstream_model=…)` via the spawner |
| Restart path in `orchestrator/routes/pipelines.py` (`restart_agent`) | Same `--model` resolution; `decision.env_vars()` merged into `extra_env` so the restarted Job re-registers the custom model | `restart_agent_job` → `spawn_agent_job` → `register_session(upstream=…, upstream_model=…)` — a restart respawns the Job and registers a **new** gateway session carrying the resolved decision |

When the resolved decision is the default Claude case
(`upstream="anthropic"`, `upstream_model=None`),
`decision.env_vars()` is empty and `ConcurrentPhaseExecutor._spawn_agent`
omits the `upstream` / `upstream_model` kwargs from its `spawn_fn`
call entirely, so test mocks and legacy spawn paths see the pre-#2769
call signature. One layer down, `spawn_agent_job` still passes both
kwargs to `GatewayClient.register_session` — as `None` on the default
path — and `register_session` drops `None` values from the
session-create request body, so the wire shape stays byte-identical
to today.

## Gateway-side body handling

For LiteLLM-bound requests the gateway forwards the body **unchanged**.
Claude Code already places the bare upstream name in the request's
`"model"` field — it strips the `[1m]` suffix from
`ANTHROPIC_CUSTOM_MODEL_OPTION` before send — so LiteLLM keys the
`model_list` entry off the body directly:

```
sandbox ──POST /v1/messages──▶ gateway
                                  │   body: {"model": "qwen3-coder-30b", …}
                                  ├─ session_manager.get_session_by_ip(...)
                                  │     → session.upstream = "litellm"
                                  │     → session.upstream_model = "qwen3-coder-30b"  (audit only)
                                  │
                                  ├─ _inject_upstream_credentials(headers, "litellm")
                                  ├─ _filter_blocked_tools(body, session.mode)
                                  │
                                  ├─ client = registry.get("litellm")[0]
                                  └─ stream upstream → accumulate → return SSE downstream
```

`Session.upstream_model` is retained as audit metadata on
`session_created` log lines so operators can correlate a transcript
with the resolved backend, but the proxy does not consume it for
routing or rewriting.

> **Startup-probe leak.** A handful of probes Claude Code issues on
> startup leak the `[1m]` suffix onto the wire instead of stripping
> it. `litellm-configmap.yaml` accordingly ships paired
> `model_name: <name>` and `model_name: <name>[1m]` entries pointing
> at the same `litellm_params`, so the suffixed probes resolve to
> the same backend instead of 400ing with `Invalid model name`. See
> the example in [`k8s/base/litellm-configmap.yaml`](../../k8s/base/litellm-configmap.yaml).

## Compaction math + custom model registration (#2832)

> The Claude Code harness derives a model's **context window** —
> and therefore **auto-compaction timing** — from the model name it
> is told to use. Anthropic models map to known windows; an
> unrecognised name silently falls back to a 200k assumption that
> wedges agents on either side of the real upstream window (over-
> compaction for 1M-context Qwen / DeepSeek, no-compaction for
> sub-200k upstreams).

Claude Code documents an opt-in for custom (non-Claude) models via
two env vars:

- `ANTHROPIC_CUSTOM_MODEL_OPTION=<upstream>[1m]` — registers the
  custom model ID and tells Claude Code to use 1M-context
  compaction math.
- `ANTHROPIC_CUSTOM_MODEL_OPTION_NAME=<upstream>` — the bare
  on-the-wire name. Claude Code strips the `[1m]` suffix from the
  registered ID before sending the `"model"` field, so LiteLLM sees
  the bare name and matches its `model_list` entry directly.

The resolver builds both values from the decided upstream string and
the spawn sites merge them into the agent's `extra_env`
(see `AgentModelDecision.env_vars()`). The resolver also pins the
agent's `--model` flag to `<upstream>[1m]` so the harness's
``--model``-keyed pathway also routes through the custom-model
registration.

> **Capability env vars (`MAX_THINKING_TOKENS`,
> `ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES`).** Not set
> by default — clamping them to zero/empty maximises cost savings
> but diverges from the Claude path's runtime behaviour (extended
> thinking, `CLAUDE_EFFORT=xhigh`), making apples-to-apples quality
> comparisons unreliable. Operators who want to minimise cost
> rather than match parity can layer them in via the per-pipeline
> override flow (see [#2832](https://github.com/jwbron/egg/issues/2832)
> for the follow-up).

> **Sub-1M-window upstreams.** The resolver appends `[1m]`
> unconditionally on the LiteLLM path. The current pilot upstreams
> (Qwen3.7-max, DeepSeek-v4-*) are all 1M; smaller upstreams (e.g.
> the original qwen3-coder-30b at 32k) would see Claude Code defer
> compaction past their real window. A per-entry context_window
> declaration is the natural extension when those backends graduate
> past pilot — left out of this change deliberately to keep the
> seam minimal.

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
          drop_params: true
          # Pin the provider to Qwen's first-party host for a stable,
          # cacheable surface (see "Prompt caching" below).
          extra_body:
            provider:
              order: [Alibaba]
              allow_fallbacks: false
      # Paired `[1m]` alias — required to absorb the Claude Code
      # startup-probe suffix leak (#2832). See note below. Keep its
      # litellm_params byte-identical to the bare row.
      - model_name: qwen3-max[1m]
        litellm_params:
          model: openrouter/qwen/qwen3-max
          api_key: os.environ/OPENROUTER_API_KEY
          drop_params: true
          extra_body:
            provider:
              order: [Alibaba]
              allow_fallbacks: false
    # Cost + prompt-cache visibility (see "Prompt caching" below). KEEP
    # this block — your overlay replaces config.yaml in full, so dropping
    # it disables cost/cache logging for routed agents.
    litellm_settings:
      callbacks: cost_callback.cost_logger
    general_settings:
      master_key: os.environ/LITELLM_MASTER_KEY
```

> **Prompt caching.** Claude Code marks a stable prefix with
> `cache_control` on every request; on the Anthropic path that yields a
> ~10× input-cost discount on cache hits. Routing through a stock LiteLLM
> image **silently loses that discount on Qwen/DeepSeek** — its
> Anthropic→OpenAI translation neither honors `cache_control` for those
> providers nor strips the per-request `x-anthropic-billing-header` block
> Claude Code prepends (the block's `cch=` hash invalidates the cache key
> every turn). egg ships a custom **`egg-litellm`** image
> ([`config/litellm/Dockerfile`](../../config/litellm/Dockerfile)) that
> bakes in three patches closing those gaps
> ([`config/litellm/patch_litellm_cache.py`](../../config/litellm/patch_litellm_cache.py));
> the build fails loudly if a LiteLLM bump moves the patched code. Pinning
> the OpenRouter provider (`extra_body.provider.order` + `allow_fallbacks:
> false`) then gives a stable cache surface — without it OpenRouter routes
> across a cheapest-available pool whose cache support varies per turn. The
> bundled `cost_callback` logs the real upstream cost and per-session cache
> hit rate (a JSON line per call) to the LiteLLM pod stream, visible via
> `get_service_logs` / the structured-logging stream.

> **Why the paired `<name>[1m]` row?** Claude Code registers the
> custom model with a `[1m]` context-window-opt-in suffix
> (`ANTHROPIC_CUSTOM_MODEL_OPTION=qwen3-max[1m]`) and strips the
> suffix client-side before request bodies hit the wire — but a
> handful of startup probes leak the suffixed name through. Without
> the paired `qwen3-max[1m]` row in `model_list`, LiteLLM rejects
> those probes with `Invalid model name`. Registering both the bare
> and suffixed aliases pointing at the same `litellm_params`
> absorbs the noise without a gateway-side body rewrite. The
> committed
> [`k8s/base/litellm-configmap.yaml`](../../k8s/base/litellm-configmap.yaml)
> comment block has the full background; ship the paired row for
> every non-Claude `model_name` you register.

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
  their body forwarded **byte-identically** to
  `litellm.egg-system.svc.cluster.local:4000` — Claude Code emits
  the bare upstream name (`qwen3-max`) in the request `"model"`
  field on its own, having stripped the `[1m]` context-window-opt-in
  suffix client-side after registering the custom model via
  `ANTHROPIC_CUSTOM_MODEL_OPTION` at startup (#2832). The gateway
  performs no body rewrite; LiteLLM matches the bare name against
  its `model_list` and routes to the hosted Qwen backend.
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
   custom-model env-var registration (#2832), Claude Code should
   compact against the real upstream window (1M for the current
   pilots) rather than the legacy 200k Claude fallback. Confirm the
   in-session `/context` panel reports the right window size and that
   compaction fires on schedule.

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
| `resolve_agent_model(role, pipeline_config, repo)` + `AgentModelDecision` | `orchestrator/agent_model_resolution.py` | Walks precedence + classifies into `(claude_code_alias, upstream, upstream_model)`; `AgentModelDecision.env_vars()` returns the `ANTHROPIC_CUSTOM_MODEL_OPTION` pair on the LiteLLM path |
| Spawn-side plumbing | `orchestrator/concurrent_executor.py` (`_spawn_agent`), `orchestrator/routes/pipelines.py` (`restart_agent`), `orchestrator/kubernetes_spawner.py` (`spawn_agent_job`) | Threads `--model` to the consensus wrapper, merges `decision.env_vars()` into `extra_env`, and forwards `upstream` / `upstream_model` to `GatewayClient.register_session` |

The slice-1 primitives this guide builds on (`UpstreamRegistry`,
`Session.upstream` / `upstream_model`, the LiteLLM credential
resolver, the LiteLLM k8s manifests) are catalogued in [Upstream
Routing → Files](../architecture/upstream-routing.md#files).

## HITL decisions that shape this guide

| Decision | Resolution | Where it shows up |
|----------|------------|-------------------|
| `cq-3` | Per-role field on `PipelineConfig` **and** `repositories.yaml` default | Two-knob config above; precedence chain in the resolver |
| `cq-4` | No agent-flip in this pipeline; operator smoke-test deferred | Smoke-test section is operator-driven, not gating merge |
| `cq-5` | Keep Claude Code; superseded by env-var registration (#2832) | Resolver pins `claude_code_alias = "<upstream>[1m]"` and threads `ANTHROPIC_CUSTOM_MODEL_OPTION` into the agent env — no gateway body rewrite |
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

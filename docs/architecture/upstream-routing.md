# Upstream Routing — LiteLLM Proxy Seam

This document describes the gateway-side seam that lets per-agent
`/v1/messages` traffic route to either `api.anthropic.com` (default)
or a LiteLLM proxy that fronts non-Claude backends (the first target
is a hosted Qwen provider). It covers the `UpstreamRegistry`
abstraction, the per-session routing decision, the credential layout,
the LiteLLM topology in Kubernetes, and the **no-op-by-default**
invariant that keeps every Claude-bound agent on byte-identical paths
when LiteLLM is not configured.

Status: this seam lands in two stacked changes for [#2769](https://github.com/jwbron/egg/issues/2769). Slice 1 — described here — is the
**gateway router + LiteLLM Deployment**, no-op by default. Slice 2
adds the orchestrator-side resolution (`PipelineConfig.agent_models`,
`default_agent_model`, `resolve_agent_model`); [#2832](https://github.com/jwbron/egg/issues/2832) later replaced the original
cq-5 body-rewrite mitigation with `ANTHROPIC_CUSTOM_MODEL_OPTION`
env-var registration in the agent's sandbox, so the gateway no longer
rewrites request bodies on the LiteLLM path. Operators looking to
actually flip a role to a non-Claude backend should start at the
[Per-Agent Models guide](../guides/per-agent-models.md) — it walks
through the two configuration knobs, the precedence chain, the
custom-model env-var registration, and the cq-4 operator smoke test
end-to-end.

## Why a router, not a hard-wired second client

Today's gateway hard-wires the Anthropic upstream: a singleton
`httpx.Client` is opened against `https://api.anthropic.com`
(`gateway/gateway.py:9320` `get_anthropic_client`) and every
`/v1/messages` and `/v1/messages/count_tokens` request injects an
Anthropic credential before forwarding
(`gateway/gateway.py:9355` `_inject_anthropic_credentials`).

We want any agent role to be independently switchable to a non-Claude
model — first for cost (the primary driver behind #2769), eventually
to mix self-hosted weights with Claude on per-role boundaries — while
every Claude-bound agent stays byte-identically on the existing path
until the operator opts in. We also want a clean swap-out point in
case LiteLLM is unsuitable in the future (refine-phase feedback Q3:
the March 2026 PyPI incident is recent enough that hard-wiring a
specific proxy framework is a known risk).

The shape that satisfies both constraints is a small per-request
registry of `(httpx.Client, credential_resolver)` pairs keyed by
upstream name, with the proxy routes resolving the upstream from the
per-session metadata that already drives `session_mode`. The router
adds no new tool-side surface and no new agent-visible API — agents
keep talking to `http://egg-gateway:9848/v1/messages` as before. The
gateway is the only component that knows there is more than one
upstream.

## Topology — where LiteLLM runs in the cluster

The HITL on `cq-1` selected **a separate Deployment + Service in the
`egg-system` namespace** (1 LiteLLM pod, gateway calls it over the
cluster-internal Service DNS). The alternative shapes — a sidecar in
the gateway pod, or a fully separate namespace with its own
NetworkPolicy — were declined: sidecar couples lifecycle to the
gateway (one restart kills both), and a separate namespace doubles
the ops surface for a defense-in-depth gain we can add later.

```
┌───────────────────────────────────────────────────────────────────┐
│                       UPSTREAM ROUTING                            │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│  egg-agents/ (untrusted)         egg-system/ (trusted)            │
│  ┌─────────────────┐             ┌─────────────────────┐          │
│  │  sandbox  pod   │             │  egg-gateway        │          │
│  │  Claude Code    │  ANTHROPIC_ │                     │          │
│  │  no creds       │  BASE_URL   │  UpstreamRegistry   │          │
│  │                 │ ──────────▶ │   ├── anthropic  ───┼──▶ api.anthropic.com
│  │  /v1/messages   │   :9848     │   └── litellm   ───┼──▶ litellm.egg-system │
│  └─────────────────┘             │                     │          │ .svc.cluster.local
│                                  │  Session lookup     │          │   :4000
│                                  │  by remote_addr →   │          │          │
│                                  │  session.upstream   │          │          ▼
│                                  └─────────────────────┘          │  ┌────────────────┐
│                                                                   │  │ litellm pod    │
│                                                                   │  │ (egg-system)   │
│                                                                   │  │ Anthropic-shape│
│                                                                   │  │ translator →   │
│                                                                   │  │ hosted Qwen    │
│                                                                   │  └────────────────┘
└───────────────────────────────────────────────────────────────────┘
```

Key topology properties:

- **Sandbox sees one URL.** Agents still use
  `ANTHROPIC_BASE_URL=http://egg-gateway:9848`. The router is
  invisible to the sandbox; the gateway picks the upstream based on
  the per-agent session.
- **LiteLLM is gateway-only.** No NetworkPolicy change on the
  `egg-agents` namespace — agent pods continue to be denied
  egress to `litellm.egg-system.svc.cluster.local`. Only the
  gateway pod calls LiteLLM.
- **Operator-owned isolation for the LiteLLM pod itself.** LiteLLM
  in turn talks out to the configured provider (hosted Qwen for
  the first cut, per `cq-6`). `OPENROUTER_API_KEY` follows the same
  path as `LITELLM_MASTER_KEY` (issue #2799): operators set it in
  `~/.config/egg/secrets.env`; `make k3s-secrets` extracts it onto
  `gateway-secrets` as a literal key; the LiteLLM Deployment binds
  it via `secretKeyRef` (`optional: true`). Other provider keys
  (Together AI, etc.) are NOT auto-wired — operators add the
  extraction line in `make k3s-secrets` and the matching
  `secretKeyRef` env entry on the LiteLLM container themselves. The
  gateway pod does not consume any provider key — `secrets.env` is
  the operator-facing entry point and the LiteLLM ConfigMap
  references each with `os.environ/<NAME>`.
- **No-op until configured.** The Service comes up healthy with an
  empty `model_list`. Operators register backends via a host-side
  overlay at `~/.config/egg/litellm-models.yaml` (copy from
  `config/litellm-models.template.yaml`) and apply it with
  `make litellm-config` (also invoked by `make deploy` /
  `make redeploy`). Until the LiteLLM master key is set and at
  least one entry is registered, every `/v1/messages` request still
  routes to `api.anthropic.com` via the default
  `session.upstream = "anthropic"`.

The manifests land at:

| File | Resource |
|------|----------|
| `k8s/base/litellm-deployment.yaml` | Deployment running a pinned LiteLLM image in `egg-system`, mounting the ConfigMap at `/app/config.yaml` |
| `k8s/base/litellm-service.yaml` | ClusterIP Service named `litellm` exposing port `4000` (LiteLLM's default) |
| `k8s/base/litellm-configmap.yaml` | Empty `model_list` so the pod is healthy but serves nothing until operators populate it |
| `k8s/base/kustomization.yaml` | Registers the three new resources |

## The router — `UpstreamRegistry`

The router is a new module, `gateway/upstream_registry.py`, that owns
the (singleton `httpx.Client`, credential resolver) pair for each
known upstream:

| Upstream key | Client base URL | Credential resolver |
|--------------|-----------------|---------------------|
| `"anthropic"` | `https://api.anthropic.com` (preserves the `# noqa: EGG200` annotation at `gateway/gateway.py:9325`) | `AnthropicCredentialsManager` — unchanged |
| `"litellm"` | `LITELLM_BASE_URL` env var, default `http://litellm.egg-system.svc.cluster.local:4000` | LiteLLM resolver in `gateway/anthropic_credentials.py` (see below) |

Public surface:

- `class UpstreamRegistry` — keyed lookup of upstream clients +
  credential resolvers
- `get(upstream: str) -> (httpx.Client, credential_resolver)` —
  returns the pair for a known upstream
- `class UnknownUpstreamError` — raised by `get()` on miss; the
  proxy routes translate this to a 400 with a descriptive body
- `get_upstream_registry()` — module-level accessor mirroring the
  `get_anthropic_client()` lifetime semantics so the registry is
  built once and shared across requests

Both clients share the same `httpx.Timeout(120.0, connect=10.0)`
and `httpx.Limits(max_connections=100, max_keepalive_connections=20)`
shape as today's `_anthropic_client` so latency / pooling behavior
does not silently regress for the Claude path.

## Credentials

### Layout

Anthropic credentials are unchanged: the
`AnthropicCredentialsManager` loads `ANTHROPIC_API_KEY` /
`ANTHROPIC_OAUTH_TOKEN` from `~/.config/egg/secrets.env` with an
mtime-invalidated cache (`gateway/anthropic_credentials.py`).

LiteLLM gets a parallel resolver that loads `LITELLM_MASTER_KEY`
from the same `secrets.env` file via the existing `parse_env_file`
helper at `gateway/anthropic_credentials.py:52` and caches with the
same mtime invalidation. When the key is set, the resolver returns
a credential shaped `header_name="x-api-key"`,
`header_value=<LITELLM_MASTER_KEY>`. When the key is **unset** —
the default for every existing deployment — the resolver returns
`None` and emits no startup warning. This matches the existing
Anthropic-resolver behavior when the API key is absent (the
"no-credential" path is well-trodden) and is the source of the
**no-op-by-default** invariant: without `LITELLM_MASTER_KEY`,
nothing about the Claude flow changes.

The key lands as a documented entry in `config/secrets.template.env`
with an explicit "leave empty to disable LiteLLM routing — no agent
will be routed to LiteLLM with this unset" comment, one block below
the existing `ANTHROPIC_API_KEY` block. This is the choice from
`cq-7`: the gateway holds the LiteLLM master key and injects it on
every LiteLLM-bound request, mirroring today's Anthropic credential
injection. Per-provider backend keys (e.g. `OPENROUTER_API_KEY`) flow
through the same `secrets.env` → `gateway-secrets` path as the master
key (issue #2799); the LiteLLM ConfigMap references them with
`os.environ/<NAME>`. The gateway pod does not consume them. The sandbox
sees neither.

### Injection

`_inject_anthropic_credentials` (`gateway/gateway.py:9355`) is
generalized to `_inject_upstream_credentials(headers, upstream)`.
The old symbol stays as a back-compat alias that calls through with
`upstream="anthropic"`. Dispatch picks the resolver from the
registry and produces a `(headers, error_response_tuple)` pair
identical in shape to today's: a missing credential returns the same
401 / `authentication_error` JSON body the Anthropic path returns,
just sourced from the LiteLLM resolver when the upstream is LiteLLM.

The OAuth-passthrough fallback (when no gateway credential is
configured but the client sent `Authorization` / `x-api-key`
headers itself, used by Claude Code's own OAuth flow) is preserved
verbatim for the Anthropic path. The LiteLLM path does not exercise
that fallback because the sandbox never holds LiteLLM credentials.

## Per-session routing

The routing decision is **per session**, not per request body. The
HITL on `cq-2` settled this: the orchestrator declares the upstream
when it spawns the agent (the same IP-keyed session lookup that
already drives `session_mode`), and the gateway treats the model
name in the request body as informational only. The alternatives —
a custom HTTP header from the sandbox, or model-name sniffing —
were declined: the header shape duplicates session state, and
model-name sniffing would re-derive a per-request decision that the
orchestrator already settled at spawn time.

### Session fields

Two new optional fields land on the `Session` dataclass at
`gateway/session_manager.py:288`:

- `upstream: str = "anthropic"` — the registered upstream name to
  route this session's `/v1/messages` traffic to
- `upstream_model: str | None = None` — the bare upstream-side model
  name, retained as audit metadata on `session_created` log lines.
  Originally fed slice-2's body-rewrite mitigation; replaced by the
  in-sandbox `ANTHROPIC_CUSTOM_MODEL_OPTION` registration in #2832

Both default to the Claude path. Sessions persisted **before** this
change rehydrate cleanly: `Session.from_persistence` tolerates
dicts that omit the two keys, falling back to the defaults. This is
exercised by the back-compat round-trip test in
`gateway/tests/test_session_manager.py`.

### Registration plumbing

`SessionManager.register_session` (`gateway/session_manager.py:548`)
gains two optional parameters (`upstream`, `upstream_model`) and
stores them on the returned `Session`. The
`/api/v1/sessions/create` handler at `gateway/gateway.py:8507`
parses both from the request body (defaulting to `"anthropic"` /
`None`), validates `upstream` against the names registered in
`UpstreamRegistry` (unknown name → HTTP 400 with a descriptive
error), and passes both through to `register_session`. The existing
`audit_log("session_created", ...)` call includes the upstream and
upstream_model so the per-session routing decision is auditable.

The orchestrator-side wire shape catches up in
`GatewayClient.register_session` (`orchestrator/gateway_client.py:602`):
two optional kwargs, included in the POST body only when set
(matches the existing optional-field pattern in the surrounding
calls). No slice-1 caller passes them — that's purely the wire
contract. Slice 2 is where the orchestrator's spawner actually
decides per-agent upstreams and calls
`register_session(upstream=…, upstream_model=…)`.

### Proxy routes

`proxy_anthropic_messages` (`gateway/gateway.py:9753`) and
`proxy_count_tokens` (`gateway/gateway.py:10020`) each replace
`client = get_anthropic_client()` with a registry lookup keyed by
`session.upstream`. When no session is found (e.g. an
unauthenticated probe), the upstream defaults to `"anthropic"` —
this preserves today's behavior verbatim. The `headers, error =
_inject_anthropic_credentials(headers)` call becomes
`_inject_upstream_credentials(headers, session.upstream)`.

Everything **inside** the request loop stays unchanged:

- `_filter_blocked_tools(...)` (the private-mode WebSearch /
  WebFetch strip from `cq-9` — kept identical regardless of
  upstream; the conservative read is that an attacker on a
  compromised sandbox can still exfiltrate through search queries
  even when the upstream is self-hosted, so the strip stays
  defense-in-depth on all routes)
- The pre-stream retry + mid-stream synthetic-error resilience
  loop from [#1907](https://github.com/jwbron/egg/issues/1907) (see [Credential Injection
  → Upstream Stream Resilience](credential-injection.md#upstream-stream-resilience))
- Connection-pool / timeout / limits — both clients share the same
  shape (see "The router" above)

The router is intentionally a **per-request lookup at the top of
the handler**, not new branches inside any of these inner loops.
That keeps the security-critical inner code byte-identical and the
review surface small.

## Request lifecycle — both upstreams

### Anthropic (default — every agent, until configured otherwise)

```
sandbox ──POST /v1/messages──▶ gateway
                                  │
                                  ├─ session_manager.get_session_by_ip(...)
                                  │     → session.upstream = "anthropic"  (default)
                                  │     → session.upstream_model = None    (default)
                                  │
                                  ├─ _inject_upstream_credentials(headers, "anthropic")
                                  │     → registry.get("anthropic")[1].get_credential()
                                  │     → AnthropicCredentialsManager (Anthropic key from secrets.env)
                                  │
                                  ├─ _filter_blocked_tools(body, session.mode)
                                  │
                                  ├─ client = registry.get("anthropic")[0]
                                  │     → existing _anthropic_client, base_url=https://api.anthropic.com
                                  │
                                  └─ stream upstream → accumulate → return SSE downstream
```

Wire shape: byte-identical to today. This is the regression guard:
with `agent_models == {}` everywhere (the slice-2 default) and no
`LITELLM_MASTER_KEY` configured, every gateway request follows the
exact same path it does today.

### LiteLLM (per-agent, once configured)

```
sandbox ──POST /v1/messages──▶ gateway
                                  │
                                  ├─ session_manager.get_session_by_ip(...)
                                  │     → session.upstream = "litellm"
                                  │     → session.upstream_model = "qwen3-coder-30b"  (set by orchestrator at session create)
                                  │
                                  ├─ _inject_upstream_credentials(headers, "litellm")
                                  │     → registry.get("litellm")[1].get_credential()
                                  │     → LiteLLM resolver (LITELLM_MASTER_KEY from secrets.env)
                                  │
                                  ├─ _filter_blocked_tools(body, session.mode)
                                  │     (same strip regardless of upstream — cq-9 conservative choice)
                                  │
                                  │     (body["model"] already carries the upstream name —
                                  │      Claude Code stripped the [1m] suffix from
                                  │      ANTHROPIC_CUSTOM_MODEL_OPTION before send; the
                                  │      gateway does not rewrite the body, see #2832)
                                  │
                                  ├─ client = registry.get("litellm")[0]
                                  │     → LiteLLM client, base_url from LITELLM_BASE_URL
                                  │
                                  └─ stream upstream → accumulate → return SSE downstream
```

In slice 1 the router is in place but no caller sets
`session.upstream = "litellm"`, so the LiteLLM branch is exercised
only by unit tests until slice 2 lands the spawn-side plumbing.
Slice 2 originally set the body's `"model"` field via a gateway-side
`_rewrite_upstream_model` helper; #2832 retired that helper in
favour of the in-sandbox env-var registration described in the
[Per-Agent Models guide](../guides/per-agent-models.md#compaction-math--custom-model-registration-2832).

## Routing policy & reactive fallback (#2987)

[#2987](https://github.com/jwbron/egg/issues/2987) makes the gateway
the **single LLM router** for the cost-driven migration onto open
models. The decision that shapes this section: route everything —
including Anthropic and every open→Anthropic fallback hop — through
the gateway's existing per-upstream registry, and reject the
alternative of fronting Anthropic *through* LiteLLM. Putting Anthropic
inside LiteLLM does not compose: LiteLLM's Anthropic provider wants
`x-api-key`, while egg's Anthropic credential is OAuth-only
(`Authorization: Bearer`), and LiteLLM's `forward_client_headers`
would clobber OpenRouter's own `Authorization: Bearer`. The gateway
sidesteps the collision because `_inject_upstream_credentials` already
keeps Anthropic-OAuth (`Authorization`) and LiteLLM (`x-api-key`) on
**different header names**.

On top of the spawn-time per-agent selection (`session.upstream`), a
**hot-reloadable, model-keyed routing policy** at
`gateway/routing_policy.py` adds two levers:

- **`switchover`** — *proactive* remap. Before the first send, a wire
  model name (the request body's `"model"`) can be remapped to a
  different upstream and/or model. The knob to globally re-route a
  wire model without respawning agents.
- **`fallbacks`** — *reactive* chain. When the primary upstream
  returns a trigger status, the proxy advances through an ordered list
  of fallback hops for that wire model.

**Consolidation principle: fallback is a property of the model, not
the pipeline.** The policy keys on the wire model, so an explicit
per-pipeline `agent_models` override inherits the same fallback chain
for free — no per-pipeline fallback is threaded through the session.
Selection stays spawn-time `agent_models`; only the fallback chain (and
the proactive `switchover`) is hot-reloadable. You cannot hot-swap a
running agent's *primary* model mid-turn — that is baked into its env
at spawn — but `switchover` re-routes a wire model on the next request.

### What this is — and is not

This is the **resilience substrate** for the migration, not the
switchover mechanism itself. Per-role selection is the pre-existing
spawn-time config (`agent_models` / `default_agent_model` → next
spawn). Backend registration (the Fireworks/open `model_list` entries)
still happens in `litellm-models.yaml` via `make litellm-config`. This
change adds the Anthropic-as-fallback-target safety net plus hot
iteration on the fallback/switchover policy.

### The chain walker

`proxy_anthropic_messages` resolves a request into an ordered
`[RouteHop, ...]` chain (`_resolve_route_chain`): hop 0 is the
`switchover` remap or the spawn-time `session.upstream`; hops 1..N are
the `fallbacks` chain for the wire model. It then walks the chain in
the **pre-stream window** (streaming) or per-response (non-streaming):

- **Credentials are rebuilt per hop.** `_prepare_hop` calls
  `_get_forwarded_headers(request.headers)` fresh and injects only that
  hop's credential, every hop. This is load-bearing: because LiteLLM
  uses `x-api-key` and Anthropic-OAuth uses `Authorization` —
  *different* header names — re-injecting into a dict that already
  carries the previous hop's credential would not overwrite it, and a
  litellm→anthropic hop would carry a **stale LiteLLM master key** in
  `x-api-key` alongside the real Anthropic `Authorization` to
  `api.anthropic.com`. Rebuilding from the (already auth-stripped)
  inbound headers makes the bleed structurally impossible.
- **Model rewrite is scoped.** A hop that names a `model` rewrites the
  body's `"model"` field via `_rewrite_upstream_model` — a
  narrowly-scoped reintroduction of the helper [#2832](https://github.com/jwbron/egg/issues/2832)
  retired. It fires **only** when a policy hop names a target, so the
  no-policy path is byte-identical. A hop to `anthropic` MUST set
  `model` to a real Claude id, else Anthropic 404s the open-model name.
- **Triggers.** `_classify_route_status` decides per status code:
  `advance_on` (default `{429}` — **quota only**) advances to the next
  hop; `retry_same_on` (default `{500, 502, 503, 529}`,
  `retry_same_max = 1`) retries the **same** upstream once, then
  surfaces. Same-hop retry precedes advance, so a code in both sets
  retries first. The quota-only default is deliberate: a transient 5xx
  from a cheap open model must not silently escalate to (expensive)
  Opus, and a genuinely buggy 500 must not be masked by a green Opus
  response. Broader 5xx *escalation* is opt-in via `advance_on`.
- **Transport failures** (`ConnectError` / `TimeoutException` /
  `ReadError` / `RemoteProtocolError`) advance to a fallback hop if one
  exists, else surface via the outer handlers — preserving today's 502
  / 504 contract on the last hop. The #1907 same-hop reset retry is
  preserved per hop.

### Pre-stream-only — the quota-surface prerequisite

The trigger fires only on the upstream's **pre-stream HTTP status**.
If a provider surfaces quota as HTTP 200 followed by an in-band SSE
error frame (rather than a pre-stream 429), the gateway streams it
through and never falls back. **Before relying on fallback-on-quota,
verify the real quota response of each provider** (Fireworks /
OpenRouter / LiteLLM). This is the gate the whole "fallback on quota"
promise rides on — see the open question in the [routing-policy
template](../../config/routing-policy.template.yaml).

### Context-window safety (policy-authoring constraint)

The Claude Code compaction profile is fixed at spawn (the `[1m]` /
`ANTHROPIC_CUSTOM_MODEL_OPTION` registration). A fallback/switchover
target with a *smaller* real context window than the source can
overflow mid-conversation: `1M → Opus` is safe; `1M → 256K` (Kimi) is
risky. The gateway cannot know each model's
window, so this is enforced only by policy authoring, not at runtime.

### Hot-reload delivery

The policy file rides the proven
`~/.config/egg/` → `gateway-secrets` → `/secrets` mount. `make
k3s-secrets` bundles `routing-policy.yaml` as a Secret key, and the
gateway mounts the whole Secret at `/secrets` (no `subPath`), so
kubelet propagates an updated file to the running pod **without a
restart and without losing in-flight turns** — the gateway re-reads it
via an mtime-invalidated cache (`RoutingPolicyManager`, mirroring
`AnthropicCredentialsManager`). `make routing-policy` is the thin
standalone wrapper (re-creates the Secret, no gateway rollout). It is
config riding a Secret for the hot-reload mount, not a credential.
This is a deliberately different cadence from adding a *backend*
(`make litellm-config` → ConfigMap patch → LiteLLM pod rollout, which
does bounce that pod): routing/fallback iteration is state-preserving;
backend registration is not.

### Cost-instrumentation consequence

Routing all Anthropic-served traffic through the gateway means it
**bypasses LiteLLM**: the discovery roles/overseer that stay on Opus,
and every open→Anthropic fallback hop. So `config/litellm/cost_callback.py`
only ever covers the **open-model slice** of the pipeline — Phase-0
spend measurement must not assume it covers the whole pipeline. The
gateway is the one universal seam and is the correct eventual home for
whole-pipeline cost instrumentation. Cost instrumentation itself is
out of scope for this change.

## Failure policy

When the upstream is unreachable or errors and **no routing policy
adds a fallback hop for the wire model**, the gateway **fails closed**
— the 502 / 504 (or the upstream's own error status) surfaces to the
agent, no implicit fallback to Claude. This preserves the original
`cq-8` posture as the no-op default: with an empty routing policy the
fail-closed behavior is byte-identical to before #2987. The
alternatives `cq-8` declined (a *blanket* transparent Claude fallback,
HITL escalation) stay declined; #2987 makes fallback an **explicit,
per-wire-model, operator-authored** opt-in rather than an implicit
default, so a quietly-mixed transcript only happens where an operator
asked for it.

The existing `except httpx.ConnectError / TimeoutException /
Exception` handlers in `proxy_anthropic_messages` and
`proxy_count_tokens` cover the last-hop case verbatim — both upstreams
produce the same 502 / 504 error contracts, now attributed to the hop
that actually failed.

## No-op-by-default invariant

The invariant that makes this safe to ship before a live non-Claude
endpoint exists has three independent guards:

1. **No LiteLLM master key configured.** The LiteLLM resolver
   returns `None`, so any request that *did* somehow route to
   LiteLLM would fail credential injection with the standard 401
   — but no request routes there because of guard 2.
2. **Session upstream defaults to `"anthropic"`.** Both
   `Session.upstream` and the `/api/v1/sessions/create` handler's
   default value are `"anthropic"`. Slice 1 has no caller that
   passes `upstream="litellm"`. Slice 2 only sets it when
   `PipelineConfig.agent_models` or repository-level
   `default_agent_model` names a non-Claude model.
3. **`agent_models` default is empty.** Slice 2's
   `PipelineConfig.agent_models` field defaults to `{}`, and the
   repository-level `default_agent_model` defaults to `None`.
   Without operator action, all spawns resolve to the built-in
   `"opus"` Claude path (fable is no longer the default, though still
   opt-in selectable). All use `upstream="anthropic"`.

Any one of the three suffices to keep the LiteLLM client cold on a
given deployment. All three are independent: a misconfiguration on
one does not silently activate the LiteLLM path.

## HITL decisions that shape this design

The `#2769` refine phase resolved eleven `cq-*` decisions. The six
that directly shape the slice-1 architecture:

| Decision | Resolution | Effect on this seam |
|----------|------------|---------------------|
| `cq-1` | Separate Deployment + Service in `egg-system` | Topology section above — LiteLLM is a sibling of the gateway, not a sidecar |
| `cq-2` | Per-agent session metadata (IP-keyed lookup, like `session_mode`) | Per-session routing decision; model name in request body is informational only |
| `cq-5` | Keep Claude Code harness for non-Claude models; compaction-math mitigation | Originally a gateway-side body rewrite in slice 2; superseded by `ANTHROPIC_CUSTOM_MODEL_OPTION` env-var registration in [#2832](https://github.com/jwbron/egg/issues/2832) |
| `cq-7` | Gateway holds `LITELLM_MASTER_KEY` in `secrets.env`; injects per-request | Credentials section above — mirrors today's Anthropic injection |
| `cq-8` | Fail closed on LiteLLM errors (502, no fallback) | Failure policy section above |
| `cq-9` | Keep the private-mode WebSearch / WebFetch strip on every upstream | Proxy-routes section — `_filter_blocked_tools` runs identically regardless of upstream |

The full set is at [`.egg-state/contracts/issue-2769.json`](../../.egg-state/contracts/issue-2769.json).

## Files

| File | Role in this seam |
|------|-------------------|
| `gateway/upstream_registry.py` *(new)* | `UpstreamRegistry`, `UnknownUpstreamError`, `get_upstream_registry()` — keyed (client, credential_resolver) lookup |
| `gateway/routing_policy.py` *(new, #2987)* | `RoutingPolicy`, `RouteHop`, `TriggerConfig`, `RoutingPolicyManager` — mtime-cached, fail-open `switchover`/`fallbacks`/`triggers` policy keyed on the wire model |
| `gateway/gateway.py` `proxy_anthropic_messages` *(#2987)* | Walks the resolved `RouteHop` chain (`_resolve_route_chain`); `_prepare_hop` rebuilds creds per hop (bleed fix); `_rewrite_upstream_model` scoped reintroduction; `_classify_route_status` triggers |
| `config/routing-policy.template.yaml` *(new, #2987)* | Operator template for `~/.config/egg/routing-policy.yaml`; documents schema, trigger defaults, context-window constraint, and hot-reload delivery |
| `Makefile` `routing-policy` *(new, #2987)* | Thin wrapper re-creating `gateway-secrets` to publish `routing-policy.yaml` (no gateway rollout) |
| `gateway/anthropic_credentials.py` | LiteLLM credential resolver alongside `AnthropicCredentialsManager`; shared `parse_env_file` (`anthropic_credentials.py:52`) and mtime cache |
| `gateway/gateway.py:9320` `get_anthropic_client` | Unchanged; registry holds this as the `"anthropic"` entry |
| `gateway/gateway.py:9355` `_inject_anthropic_credentials` | Generalized to `_inject_upstream_credentials(headers, upstream)`; old symbol kept as back-compat alias |
| `gateway/gateway.py:9753` `proxy_anthropic_messages` | Resolves upstream from `session.upstream`; SSE / tool-filter / retry inner loops unchanged |
| `gateway/gateway.py:10020` `proxy_count_tokens` | Same routing change as `proxy_anthropic_messages` |
| `gateway/gateway.py:8507` `/api/v1/sessions/create` | Accepts `upstream` and `upstream_model`; validates `upstream` against `UpstreamRegistry`; audit log includes both |
| `gateway/session_manager.py:288` `Session` | New `upstream: str = "anthropic"` and `upstream_model: str \| None = None` fields with back-compat `from_persistence` |
| `gateway/session_manager.py:548` `register_session` | New optional `upstream` / `upstream_model` kwargs |
| `orchestrator/gateway_client.py:602` `register_session` | New optional `upstream` / `upstream_model` kwargs; included in POST body only when set |
| `k8s/base/litellm-deployment.yaml` *(new)* | LiteLLM pod in `egg-system` |
| `k8s/base/litellm-service.yaml` *(new)* | ClusterIP `litellm:4000` (default `LITELLM_BASE_URL`) |
| `k8s/base/litellm-configmap.yaml` *(new)* | Empty `model_list` — gateway-only callable; operators register backends via the `config/litellm-models.template.yaml` host-side overlay applied by `make litellm-config` |
| `k8s/base/kustomization.yaml` | Registers the three new LiteLLM resources |
| `config/secrets.template.env` | `LITELLM_MASTER_KEY=""` block (disable-when-empty); `OPENROUTER_API_KEY=""` block added by #2799 |

## Related Documentation

- [Credential Injection](credential-injection.md) — Anthropic
  credential resolver shape and the upstream-reset resilience
  pattern that both router branches inherit unchanged
- [Orchestrator Architecture](orchestrator.md) — Spawner and
  session-creation context for slice 2's per-agent model
  resolution
- [Per-Agent Models Guide](../guides/per-agent-models.md) — Operator
  walkthrough for the slice-2 configuration plumbing
  (`PipelineConfig.agent_models`, repo-level `default_agent_model`,
  the `resolve_agent_model` precedence + classifier, the
  `ANTHROPIC_CUSTOM_MODEL_OPTION` env-var registration that replaced
  the original cq-5 body rewrite ([#2832](https://github.com/jwbron/egg/issues/2832)),
  and the cq-4 hosted-Qwen smoke test)
- [Network Isolation](network-isolation.md) — Cluster network
  posture; LiteLLM is gateway-only and NetworkPolicy is unchanged
- Issue [#2769](https://github.com/jwbron/egg/issues/2769) — Original
  motivation, refine-phase decisions, plan-phase risk analysis

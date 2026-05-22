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
`default_agent_model`, `resolve_agent_model`) and the gateway-side
body rewrite (`_rewrite_upstream_model`) that together connect an
agent role to a concrete upstream model. Operators looking to actually
flip a role to a non-Claude backend should start at the
[Per-Agent Models guide](../guides/per-agent-models.md) — it walks
through the two configuration knobs, the precedence chain, the cq-5
recognized-alias mitigation, and the cq-4 operator smoke test
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
  the first cut, per `cq-6`); per-provider credentials live in the
  LiteLLM ConfigMap, not the gateway.
- **No-op until configured.** The Service comes up healthy with an
  empty `model_list`. Until an operator populates the ConfigMap
  and sets the LiteLLM master key, every `/v1/messages` request
  still routes to `api.anthropic.com` via the default
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
injection. LiteLLM itself holds the real per-backend keys in its
ConfigMap (e.g. the hosted Qwen provider's API key). The sandbox
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
model-name sniffing conflicts with the `cq-5` compaction mitigation
that intentionally presents `opus` to Claude Code even when the
upstream is non-Claude.

### Session fields

Two new optional fields land on the `Session` dataclass at
`gateway/session_manager.py:288`:

- `upstream: str = "anthropic"` — the registered upstream name to
  route this session's `/v1/messages` traffic to
- `upstream_model: str | None = None` — the on-the-wire model name
  to forward upstream (only meaningful for LiteLLM; see slice 2's
  body rewrite)

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
- `_SSEAccumulator` parse and the per-session transcript capture
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
                                  ├─ [slice 2] _rewrite_upstream_model(body, session.upstream_model)
                                  │     → body["model"] = "qwen3-coder-30b"
                                  │     (Claude-Code-facing alias stays "opus" so compaction math
                                  │      remains sane — cq-5 mitigation)
                                  │
                                  ├─ client = registry.get("litellm")[0]
                                  │     → LiteLLM client, base_url from LITELLM_BASE_URL
                                  │
                                  └─ stream upstream → accumulate → return SSE downstream
```

The body rewrite is **slice 2**'s addition; in slice 1 the router
is in place but no caller sets `session.upstream = "litellm"`, so
the LiteLLM branch is exercised only by unit tests until slice 2
lands the spawn-side plumbing.

## Failure policy

The HITL on `cq-8` settled this: when the LiteLLM proxy is
unreachable or errors for a non-Claude agent, the gateway **fails
closed** — a 502 surfaces to the agent, no fallback to Claude. The
alternatives (transparent Claude fallback, HITL escalation) were
declined: fallback produces a quietly-mixed transcript that erodes
the cost goal motivating the work, and HITL escalation is a
follow-up that can be layered on top of the fail-closed default if
operators demand it.

This is the same policy the Claude path uses today on upstream
errors. The existing `except httpx.ConnectError / TimeoutException /
Exception` handlers in `proxy_anthropic_messages` and
`proxy_count_tokens` cover the LiteLLM case verbatim — both
upstreams produce the same 502 / 504 error contracts.

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
   Without operator action, every spawn resolves to the built-in
   `"opus"` Claude path with `upstream="anthropic"`.

Any one of the three suffices to keep the LiteLLM client cold on a
given deployment. All three are independent: a misconfiguration on
one does not silently activate the LiteLLM path.

## HITL decisions that shape this design

The `#2769` refine phase resolved eleven `cq-*` decisions. The five
that directly shape the slice-1 architecture:

| Decision | Resolution | Effect on this seam |
|----------|------------|---------------------|
| `cq-1` | Separate Deployment + Service in `egg-system` | Topology section above — LiteLLM is a sibling of the gateway, not a sidecar |
| `cq-2` | Per-agent session metadata (IP-keyed lookup, like `session_mode`) | Per-session routing decision; model name in request body is informational only |
| `cq-5` | Keep Claude Code harness for non-Claude models; present recognized alias | Compaction-math mitigation; body rewrite lives in slice 2 |
| `cq-7` | Gateway holds `LITELLM_MASTER_KEY` in `secrets.env`; injects per-request | Credentials section above — mirrors today's Anthropic injection |
| `cq-8` | Fail closed on LiteLLM errors (502, no fallback) | Failure policy section above |

The full set is at [`.egg-state/contracts/issue-2769.json`](../../.egg-state/contracts/issue-2769.json).

## Files

| File | Role in this seam |
|------|-------------------|
| `gateway/upstream_registry.py` *(new)* | `UpstreamRegistry`, `UnknownUpstreamError`, `get_upstream_registry()` — keyed (client, credential_resolver) lookup |
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
| `k8s/base/litellm-configmap.yaml` *(new)* | Empty `model_list` — gateway-only callable; operators populate post-deploy |
| `k8s/base/kustomization.yaml` | Registers the three new LiteLLM resources |
| `config/secrets.template.env` | New `LITELLM_MASTER_KEY=""` block with the disable-when-empty note |

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
  the `resolve_agent_model` precedence + classifier, the gateway-side
  `_rewrite_upstream_model` helper, the cq-5 recognized-alias
  mitigation, and the cq-4 hosted-Qwen smoke test)
- [Network Isolation](network-isolation.md) — Cluster network
  posture; LiteLLM is gateway-only and NetworkPolicy is unchanged
- Issue [#2769](https://github.com/jwbron/egg/issues/2769) — Original
  motivation, refine-phase decisions, plan-phase risk analysis

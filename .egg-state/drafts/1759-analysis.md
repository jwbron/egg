# Analysis: MCP tooling gaps for the new Kubernetes deployment — auth-gated slice

> Issue: #1759 | Phase: refine | Branch: `egg/issue-1759-auth-gated`

## Problem Statement

Issue #1759 catalogues a broad set of MCP tooling gaps surfaced by the
Docker→k3s migration validation (PR #1692). The issue is being tackled as
multiple parallel slices — this pipeline (branch `egg/issue-1759-auth-gated`)
focuses on the **auth-gated slice**: tooling and diagnostics that would have
detected, prevented, or explained the "role / auth boundary" class of
failures that the issue highlights as a recurring pattern.

The motivating incident (quoted verbatim from PR #1772):

> The HITL auto-approval incident in `issue-1759-v2` was caused by two
> overlapping gaps: `POST /decisions/<id>/resolve` and `/cancel` had no
> auth, and the NetworkPolicy let agent pods reach port 9849 freely. An
> agent pod approved a refine-phase gate 53 seconds after it was created,
> advancing the pipeline into plan without human consent.

PR #1772 is already open and closes half the gap by adding
`require_bearer_auth` to the orchestrator's lifecycle-control routes. **This
analysis is about the complementary tooling half**: the runtime probe that
would have caught the unauthenticated-route + overly-permissive-NetworkPolicy
posture *before* an incident, and the agent/human-facing diagnostics that
would have made this kind of boundary failure self-describing instead of
silent.

Concretely, the issue's "role / auth boundary" pattern covers:

- **#1766** — gateway 403 "Cannot determine agent role" for every
  fine-grained `AgentRole`. Fixed earlier today on `main` via commit
  `caddf4333`; the fine→coarse role mapping now lives in
  `shared/egg_contracts/agent_roles.py` + `gateway/contract_api.py`.
- **#1768** — `egg-contract add-feedback` returns 403 because the top-level
  `feedback` field's default owner is `Role.SYSTEM`
  (`shared/egg_contracts/roles.py:65`). Not yet fixed on `main`; surfaced
  by the refiner in the `issue-1759-v2` session (see the analysis on
  that branch which mentions the limitation explicitly).
- **#1769** — `POST /pipelines/<id>/decisions/<did>/resolve` had no auth
  decorator (`orchestrator/routes/decisions.py:389`). Being fixed by
  PR #1772.

The two proposed artifacts from #1759 that specifically target this
pattern are:

1. **MCP tool: `validate_network_isolation(pipeline_id, role)`** — spawn a
   throwaway probe in `egg-agents` and verify the four claimed invariants
   (can reach gateway; cannot reach internet; cannot reach other agent
   pods; cannot reach orchestrator directly). The issue explicitly calls
   this "the tool that would have caught #1769."
2. **Skill: `agent-diagnose`** — the auth-boundary error-classification
   piece specifically: "403 from gateway: probably fine/coarse role
   mismatch; 404 on container ID: probably Pod UID vs Job UID mismatch;
   short-lived pod deleted before logs readable: persist out-of-band."

Both non-auth items from the issue (`validate_deployment_manifests`,
`get_deployment_context`, `prune_stale_worktrees`, `deployment-diagnose`)
are assumed to belong to a sibling slice (likely
`egg/issue-1759-k8s-validation` / `egg/issue-1759-v2`). **Whether this
scoping is correct is the first and most important open question**
(decision-2 below).

The desired outcome is that the next auth-boundary bug — on any surface —
is flagged by a probe before it reaches production, and when one does reach
production, `agent-diagnose` explains the failure class rather than leaving
a human to pattern-match three incidents before noticing.

## Current Behavior

### Auth posture, end-to-end

**Gateway side (bearer-token model, enforced):**
- `gateway/auth.py:95-148` — `require_session_auth()` decorator validates a
  session Bearer token and stores `g.session` for downstream handlers.
  Constant-time compare; source IP logged but not rejected (pod IPs are
  ephemeral). Applied to every gateway endpoint.
- `gateway/session_manager.py:273-384` — `Session` dataclass carries
  `agent_role`, `phase`, `pipeline_id`, `container_id`, `mode`. Raw token
  in-memory only; SHA-256 hash persisted at
  `/home/egg/.egg-state/sessions/sessions.json`.
- `gateway/contract_api.py:146-198` — role resolution priority: session
  metadata (`g.session.agent_role`) → `X-Egg-Role` header (disabled by
  default) → `EGG_AGENT_ROLE` env var. `_resolve_role()` collapses the
  fine-grained `AgentRole` to a coarse `Role` via `get_contract_role()`
  in `shared/egg_contracts/agent_roles.py` (the #1766 fix).

**Orchestrator side (unauthenticated on `main`, bearer-token in PR #1772):**
- `orchestrator/routes/decisions.py:389` — `resolve_decision()` currently
  has no auth decorator at `fd78b342f`. This is the #1769 gap.
- PR #1772 (`b70d1e76c`) introduces `orchestrator/auth.py` with a
  `require_bearer_auth` decorator validating
  `Authorization: Bearer $EGG_LIFECYCLE_SECRET`, applied to **16 routes**
  spanning decisions, pipelines, phases, containers. Agents never receive
  the secret (it is added to `kubernetes_spawner._PROTECTED_ENV_KEYS` and
  the spawner constructs pod env from scratch).

**Contract-level field ownership (authorization model):**
- `shared/egg_contracts/roles.py:11-21` — `Role` enum: IMPLEMENTER,
  REVIEWER, HUMAN, SYSTEM.
- `shared/egg_contracts/roles.py:35-61` — `FIELD_OWNERSHIP` dict maps JSON
  paths (`decisions.*.resolved`, `phases.*.status`, etc.) to owners.
- `shared/egg_contracts/roles.py:65` — `DEFAULT_OWNER = Role.SYSTEM`.
  **This is the #1768 root cause**: top-level `feedback` has no entry in
  `FIELD_OWNERSHIP`, so it inherits SYSTEM, and a refiner session gets
  403 when calling `add-feedback` (the refiner role resolves to
  `implementer`, not `system`).

### Network isolation, as enforced today

`k8s/base/network-policies.yaml` encodes six Calico NetworkPolicies in
`egg-agents`:

| Policy | Direction | Effect |
|---|---|---|
| `default-deny-ingress` (lines 1-12) | Ingress | Deny all ingress to `egg-agents` pods |
| `default-deny-egress` (lines 14-25) | Egress | Deny all egress from `egg-agents` pods |
| `allow-agent-to-gateway` (lines 27-54) | Egress | Agent → `egg-system/gateway` on 9848 (API), 3129 (proxy) |
| `allow-agent-to-orchestrator` (lines 56-81) | Egress | **Agent → `egg-system/orchestrator` on 9849** |
| `allow-orchestrator-to-agent` (lines 83-105) | Ingress | `egg-system/orchestrator` → agent pods (health, logs) |
| `allow-agent-dns` (lines 107-130) | Egress | Agent → `kube-system/kube-dns` on 53 (UDP/TCP) |

**The `allow-agent-to-orchestrator` policy is the network half of the
#1769 incident.** The issue's `validate_network_isolation` checklist says
"✅ Cannot reach orchestrator directly (only allowed via gateway
session)" — but the current NetworkPolicy permits exactly that, on port
9849, to any pod with `app.kubernetes.io/component: agent`
(`kubernetes_spawner.py:286-295`). Agents legitimately need that path for
heartbeats, signals, and progress-reporting — but until PR #1772 lands, the
same path exposed the unauthenticated `/decisions/.../resolve` route.

Therefore the invariant `validate_network_isolation` can realistically
verify on the **orchestrator** axis is:

> Agents can reach the *agent-facing* subset of orchestrator routes
> (messages, signals, progress, anchors, reads) but **cannot** successfully
> mutate lifecycle routes (decisions/resolve, pipelines DELETE, phase
> advance, container DELETE, etc.).

Post-PR-#1772 that is reducible to: "lifecycle routes return 401 without
the lifecycle secret; the agent never has the secret; therefore 401."
The NetworkPolicy itself stays as-is; the probe validates the combined
invariant rather than a NetworkPolicy-only invariant.

This reframing is important and is **decision-3** below.

### MCP tool surface today

`orchestrator/mcp_tools.py` exposes 23 tools (see `PIPELINE_TOOLS`, lines
63-671). None of them talk to Kubernetes directly at a cluster level;
`list_containers` / `get_container_logs` call into
`KubernetesSpawner`/`KubernetesClient` but are scoped to pipeline
containers. **No MCP tool today spawns a probe pod, reads k8s Events,
or performs `pods/exec`.** The `kubernetes_client.py` wrapper does not
import `connect_post_namespaced_pod_exec` or `list_namespaced_event`.

### Skill surface today

`skills/` contains three skills: `babysit-pr`, `egg-setup`, `sdlc`. Each
is a single directory with a `SKILL.md` (YAML front-matter + markdown
body). There is no k8s- or diagnosis-shaped skill today. Skills invoke
MCP tools through the normal Claude tool-calling mechanism; no registry,
no schema enforcement beyond what the harness parses.

### Related work that changes the shape of this analysis

- **PR #1772** (open, 515 insertions) closes #1769 at the route layer.
  `validate_network_isolation` does *not* need to reproduce this — it
  validates that the defense-in-depth layering holds end-to-end.
- **Commit `caddf4333`** (already on `main`) fixes #1766.
  `validate_network_isolation` must not regress on this fix; the probe
  should run as a realistic fine-grained `AgentRole`, not a synthetic
  role.
- **#1768** is not on `main` at all. The `issue-1759-v2` branch's
  analysis notes that `egg-contract add-feedback` still returns 403
  from the refiner role for exactly this reason, and the authors there
  reformulated their open-ended questions as decisions to work around
  it. **This same limitation applies to this analysis.** (Decision-17
  below asks whether we pull #1768's fix into this slice, or keep it
  separate.)
- **#1760** (k8s monitor false positive) is mitigated on `main` via
  `POD_STARTUP_GRACE_SECONDS = 60` (`kubernetes_monitor.py:52`). Not in
  scope here, but affects how short-lived probe pods behave.
- **#1763** (SHA image tagging) is **not** on `main` —
  `k8s/base/gateway-deployment.yaml:33` and
  `k8s/base/orchestrator-deployment.yaml:39` still read
  `image: egg-*:latest`. Not directly in scope for this auth slice, but
  touches probe-image reproducibility (decision-8).

## Constraints

### Technical

- **RBAC: probe spawning requires additional permissions.** The orchestrator
  runs as `egg-orchestrator` ServiceAccount. To spawn a probe Job in
  `egg-agents` it already has `jobs.create` (used for agent spawning).
  To stream the probe's logs back it already has `pods.log`. What it does
  *not* have and may need depending on implementation:
  - `events.list` (to capture NetworkPolicy denial events — Calico may
    emit these on ingress/egress drop).
  - `pods/exec` (only if we pick an exec-based probe; see decision-4).
- **NetworkPolicy enforcement depends on Calico.** `make k3s-setup` sets
  `--flannel-backend=none --disable-network-policy` and assumes Calico
  is installed. On clusters where Calico is missing, the policies above
  silently do nothing. `validate_network_isolation` must **detect this
  and warn**, not silently return pass. (Decision-6.)
- **Ephemeral pod IPs.** Gateway sessions are token-bound, not IP-bound
  (`kubernetes_spawner.py:341-363`). A probe pod gets its own session
  token; any attempt to reuse an existing agent's token from a different
  pod would be rejected by session-IP-agnostic logic only if the probe
  presents the correct token. The probe must therefore carry its own
  legitimate session to make a credible test.
- **Short-lived probe pods risk log loss.** If the probe exits in <10s
  and the Job's pod is reaped, we lose evidence. Relevant mitigations:
  `ttlSecondsAfterFinished`, capturing stdout via the k8s API before
  the Job is deleted, or projecting logs to the gateway. (Decision-5.)
- **Agents cannot exec into each other.** `pods/exec` on an agent pod
  from an agent pod would fail — by design. A probe that uses exec
  must be initiated by the orchestrator (which *does* have the RBAC),
  not by another agent.
- **MCP output size cap.** `validate_network_isolation` returns
  structured per-claim results (pass, evidence). The per-claim evidence
  must be bounded (status code + first-line response body + selected
  events) to avoid exceeding the MCP response limit; full detail should
  be available via a separate `get_container_logs` call on the probe
  container-id if the caller wants to drill down.
- **`feedback` field 403 for refiner role (#1768).** Open-ended
  questions registered via `egg-contract add-feedback` fail from this
  role. The v2 branch worked around it by reformulating as multi-choice
  decisions. This analysis takes the same approach. (Decision-17.)

### Business / scope

- **Non-goals carry over from the issue**: no full k8s operator, no
  replacement for `kubectl`, read-mostly tooling. `validate_network_isolation`
  is the one proposed item that *creates* cluster resources (a probe
  Job); its lifecycle must be strictly bounded (single Job, single Pod,
  `ttlSecondsAfterFinished <= 60`, cleaned up on completion).
- **Complementary to PR #1772, not overlapping.** Any route-level auth
  work belongs to PR #1772; this slice is the probe + diagnostics layer.
- **Complementary to the v2 / k8s-validation slices.** If the human
  prefers those slices to own `validate_network_isolation`, this slice
  can narrow to the skill-level auth-boundary classifier only.
  (Decision-1.)

### Dependencies

- **PR #1772 merging** changes the assertion the probe validates. Before
  merge: the probe should fail (the unauthenticated route responds
  200/204). After merge: the probe should pass (the route returns 401).
  A version of the probe that runs *before* PR #1772 merges is useful
  as a regression witness. (Decision-2.)
- **#1768 fix** (or workaround) determines whether the refiner can
  register open-ended feedback on this very analysis. We've already
  chosen the workaround (reformulate as decisions); a real fix is not
  in scope.
- **Calico presence**: `validate_network_isolation`'s value proposition
  is zero without NetworkPolicy enforcement. A prerequisite check
  (decision-6) should fail-loud if Calico is absent.

## Options Considered

### Option A: Tool + skill together, auth-scoped

**Approach**: Ship `validate_network_isolation` MCP tool *and* the
auth-boundary-error classification section of `agent-diagnose` skill in
this slice. Leave every other #1759 item to the sibling slices.

**Pros**:
- Matches the "auth-gated" branch name most literally.
- Produces a reviewable, small surface: one tool, one scoped skill
  addition.
- The tool generates the data (four per-claim results + evidence) that
  the skill knows how to interpret ("403 from lifecycle route =
  expected post-#1772", "403 from contract API = fine/coarse
  mismatch"). Skill and tool reinforce each other.

**Cons**:
- Two artifacts in one cycle; slightly larger than a pure tool cycle.
- Ties the skill ship to the tool's schema being stable.
- Duplicates effort if the sibling slice also wants to ship a broader
  `agent-diagnose` skill (scope overlap needs to be coordinated).

### Option B: Tool only, skill deferred

**Approach**: Ship `validate_network_isolation` MCP tool only. Document
the auth-boundary error classes as a note that the sibling slice's
`agent-diagnose` skill can absorb when it lands.

**Pros**:
- Smallest reviewable surface. Pure MCP tool work.
- Cleanly complements PR #1772: "merge #1772; run this tool to prove
  the end-to-end posture; file the probe's output as the regression
  witness for #1769."
- No overlap with v2/k8s-validation slice regardless of what they do
  for skills.

**Cons**:
- Leaves the diagnostic half of the auth-boundary story on the table.
- The issue explicitly motivates `agent-diagnose` on the pattern
  "three debug cycles collapsed into one" — the tool alone doesn't
  achieve this.

### Option C: Skill only, tool deferred

**Approach**: Ship the auth-boundary error classification as a skill
addition (a new `agent-diagnose` skill scoped to auth/role errors, or
a section added to an existing skill). Defer
`validate_network_isolation` — let the sibling k8s-validation slice
own it if it wants it.

**Pros**:
- Fastest to the "collapse three debug cycles into one" win.
- Needs no RBAC change, no new k8s API wiring.
- Makes the #1766/#1768/#1769 postmortem actionable from the moment
  a human reads it.

**Cons**:
- Leaves the preventive layer unshipped — the posture verification is
  the primary motivation for this slice per the issue's explicit call-out.
- A skill with no backing tool reduces to "document these
  failure modes" — useful, but the issue's skill-shape design
  presumes composable tool primitives exist.

### Option D: Tool + skill + #1768 fix

**Approach**: Same as Option A, plus carve out the `feedback` field
ownership fix (#1768) because it blocks the refiner's ability to
register feedback cleanly on analyses like this one and is the same
"auth boundary" failure class.

**Pros**:
- Closes the full role/auth boundary cluster referenced in #1759
  (#1766 already merged, #1769 by PR #1772, #1768 by this slice).
- Immediate dogfooding benefit: this very analysis would not need to
  reformulate feedback as decisions.

**Cons**:
- #1768's fix touches `shared/egg_contracts/roles.py` — a
  contracts-level change with blast radius beyond this slice. A
  minimal fix (add `feedback` with owner `Role.IMPLEMENTER`) is
  one line, but the decision about who *should* own `feedback`
  across all agent roles is not trivial.
- Adds scope to the "auth-gated" slice that the issue itself does not
  explicitly scope in.
- Better as a separate issue triaged independently from the tool+skill
  deliverables.

### Option E: Narrow to regression witness only

**Approach**: Ship a one-shot integration test — not an MCP tool, not a
skill — that spawns a throwaway pod and asserts that the four invariants
hold after PR #1772 merges. No long-term MCP surface, no new tooling
primitive. Document the auth-boundary error taxonomy as an ADR under
`docs/`.

**Pros**:
- Minimum possible scope. Fastest to merge.
- Directly validates PR #1772's claim without adding an MCP surface to
  maintain.

**Cons**:
- Discards the reusable-primitive design the issue pushes for.
- Future auth-boundary bugs will need a new one-shot each time — the
  issue's whole framing is "stop doing one-shots, build the primitive."
- The skill value (error classification) is lost.

## Recommended Approach

**Option A (tool + skill, auth-scoped)** — with the explicit understanding
that:

1. **The tool validates a combined invariant, not a NetworkPolicy-only
   invariant.** Post-PR-#1772 the orchestrator lifecycle routes are
   authenticated, so the probe's assertion is "agent session cannot
   successfully mutate lifecycle state" — which is
   NetworkPolicy-reachability ∧ route-auth ∧ session-scope, all three
   evaluated end-to-end. Pre-#1772, the probe still reports the *actual*
   behavior (200/204 on unauthenticated mutate = FAIL), which serves as
   the regression witness for #1769.

2. **The skill contribution is narrowly scoped to auth-boundary error
   classes.** Specifically the three patterns in the issue (403 from
   gateway → fine/coarse role mismatch; 404 on container ID → Pod UID
   vs Job UID; short-lived pod → persist out-of-band) plus the new
   401 from orchestrator → missing/wrong lifecycle secret. The
   broader `agent-diagnose` skill (env resolution, scheduling events,
   OOMKill vs NetworkPolicy denial, etc.) belongs to the sibling slice
   — this slice contributes only the auth-error dispatch table.

3. **`validate_network_isolation` reports per-claim structured results**
   (decision-12 from the v2 analysis; we keep the same recommendation).
   Each claim has `{name, expected, actual, pass, evidence, container_id}`.
   Top-level overall pass = AND of all claim passes. Evidence is bounded
   (status code + first 512 bytes of body + up to 10 recent k8s Events
   for the probe pod).

4. **Probe mechanism: spawn throwaway Job, read logs.** Not pods/exec.
   This matches the issue's explicit language ("spawn a throwaway
   probe in `egg-agents`") and keeps RBAC narrow (`jobs.create` +
   `pods.log` already held). The probe Job uses a minimal image with
   `curl`, `sh`, and a `probe.sh` script that runs the four attempts
   and writes structured JSON to stdout.

5. **The probe runs as a realistic `AgentRole` session** (recommend
   `refiner`) so it exercises the post-#1766 fine→coarse role
   resolution path as well. The orchestrator creates a scoped
   single-use session for the probe (TTL = Job's active deadline)
   and destroys it on completion.

6. **Defer #1768 to a separate issue.** It surfaced on the v2 branch,
   it surfaces again here. It is a contracts-level change that deserves
   its own triage. The immediate coping mechanism — register
   open-ended questions as multi-choice decisions with a leading
   "Other (explain in reply)" — stays in force for this slice.

7. **Prerequisite check**: if Calico / NetworkPolicy enforcement is
   absent, the tool returns a single structured "no enforcement, skipping
   probes" result with `pass: null` rather than claiming the invariants
   hold.

What the plan phase must still decide:

- Image / script for the probe (decision-7).
- Exact RBAC delta (probably nil beyond what the orchestrator already
  holds; confirm in decision-9).
- Where in `orchestrator/mcp_tools.py` the tool lives and how it
  composes `orchestrator/kubernetes_client.py` (decision-10).
- How the skill contribution is packaged: a new `agent-diagnose`
  skill scoped to auth errors that the v2 slice can extend, or a
  section in a shared skill that the v2 slice owns (decision-11).

## Open Questions

**Every question below is registered as a contract decision via
`egg-contract add-decision`** (verify with `egg-contract show`). Due to
#1768 (refiner role cannot write the top-level `feedback` field), every
open-ended question has been reformulated as a multi-choice decision
with a trailing "Other (explain in reply)" option so the human can
provide free-form answers where needed.

### Decision index (decisions 2-17, 19-21 in the contract)

- **decision-2** — Is `auth-gated` the right scope for this slice?
- **decision-3** — What PR #1772 merge state does this slice assume?
- **decision-4** — Which invariant does `validate_network_isolation`
  actually verify given the existing `allow-agent-to-orchestrator`
  NetworkPolicy?
- **decision-5** — Probe mechanism: spawn Job vs `pods/exec` vs long-lived
  probe Deployment.
- **decision-6** — Probe log persistence strategy.
- **decision-7** — Behavior when Calico / NetworkPolicy enforcement is
  absent.
- **decision-8** — Probe container image.
- **decision-9** — Probe image reproducibility given `:latest` tags.
- **decision-10** — RBAC posture for the probe.
- **decision-11** — Where the tool lives in `mcp_tools.py`.
- **decision-12** — Skill packaging: standalone or section.
- **decision-13** — Auth-boundary error taxonomy: which classes ship in
  this slice?
- **decision-14** — Tool naming convention (prefix vs not).
- **decision-15** — Pass/fail reporting shape.
- **decision-16** — `feedback` field ownership fix (#1768): in/out of
  scope.
- **decision-17** — Probe's session lifecycle.
- **decision-19** — Test coverage bar for this slice.
- **decision-20** — Documentation surface.
- **decision-21** — Dogfooding: do we run the probe against the current
  cluster as part of this slice's acceptance?

The decisions are created via `egg-contract add-decision` and appear in
`egg-contract show`. The human resolves each by checking a box in the
contract view.

### Rendered decisions (registered in contract)

<!-- egg-hitl-decision id=decision-2 -->

**Is 'auth-gated' the right scope for this slice of issue #1759?**

- [ ] Yes — ship validate_network_isolation MCP tool plus the auth-boundary-error classification piece of agent-diagnose; other #1759 items belong to sibling slices on egg/issue-1759-k8s-validation and egg/issue-1759-v2
- [ ] Narrower — validate_network_isolation MCP tool only; skill contribution deferred to the sibling slice
- [ ] Broader — include the #1768 feedback-field-ownership fix because it is the same role/auth boundary failure class and blocks this very analysis
- [ ] Split differently — auth-gated = skill only (auth-error classification); let the sibling slice own validate_network_isolation
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**What PR #1772 merge state should validate_network_isolation assume when it ships?**

- [ ] Assume PR #1772 has merged — probe asserts agent session gets 401 on lifecycle routes; failure = regression witness
- [ ] Assume nothing — probe reports actual status codes; caller interprets pass/fail based on expected state from manifest (lets the tool work both before and after #1772 lands)
- [ ] Block this slice on PR #1772 merging — do not ship the probe until its assertion is the post-fix assertion
- [ ] Ship the probe first, deliberately with the pre-fix assertion, as the test that blocks merging PR #1772 until it passes
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-4 -->

**The issue says 'Cannot reach orchestrator directly'. The current allow-agent-to-orchestrator NetworkPolicy explicitly allows agent→orchestrator:9849 for heartbeats/signals. Which invariant should validate_network_isolation actually verify?**

- [ ] Combined invariant: agent session cannot successfully mutate lifecycle state (NetworkPolicy-reachability + route-auth + session-scope evaluated end-to-end). Lifecycle routes return 401; reads/messages/signals/progress return 2xx. Post-#1772 this is the correct invariant.
- [ ] Network-only invariant as issue literally states: tighten the NetworkPolicy to deny agent→orchestrator:9849 entirely; introduce a per-agent-session sidecar or gateway-proxied path for heartbeats. Bigger redesign.
- [ ] Route-class invariant: agent can only reach the agent-facing subset of orchestrator routes (messages, signals, progress, reads, anchors); the probe enumerates every lifecycle route explicitly and asserts 401 on each.
- [ ] Per-claim invariant matching the issue's four bullets verbatim, treating the orchestrator claim as aspirational and flagging 'partial' when today's NetworkPolicy allows reachability.
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-5 -->

**How should validate_network_isolation run its probes?**

- [ ] Spawn a throwaway probe Job in egg-agents and read its logs via the existing KubernetesClient.get_pod_logs path — matches the issue's language; narrow RBAC (already held)
- [ ] pods/exec into an existing agent pod when present, else spawn a probe Job — cheaper while pipelines are live; requires new RBAC (pods/exec)
- [ ] Always pods/exec into a long-lived probe Deployment added to the overlay — fastest per call; persistent cluster overhead
- [ ] Spawn probe via the gateway's container-spawn pipeline (same path as agents) so it inherits every NetworkPolicy label correctly — maximum realism, most code reused
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-6 -->

**How should the probe's logs be persisted if the Job finishes before its logs can be retrieved (the short-lived-pod problem #1759 flags)?**

- [ ] Block on Job completion then read logs before allowing k8s GC: set ttlSecondsAfterFinished=60 so the Pod lingers for a minute post-finish; retrieve logs synchronously in the MCP handler
- [ ] Stream probe stdout to a ConfigMap/Secret the orchestrator reads after completion — survives Pod deletion; extra RBAC (configmaps.create in egg-agents)
- [ ] Write probe results to a shared emptyDir+sidecar that uploads to the gateway via session session — matches gateway-mediated agent I/O but more moving parts
- [ ] Have the probe POST its structured result back to the orchestrator over the same agent-to-orchestrator path it's testing — self-referential but zero extra infra
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-7 -->

**validate_network_isolation is only meaningful when NetworkPolicy enforcement is active (Calico). k3s by default ships without it. What should the tool do when enforcement is absent?**

- [ ] Run a preflight: check that Calico's felix pod exists in kube-system and NetworkPolicy enforcement is actually active; if absent, return a single 'no-enforcement' result with pass: null and skip the probes
- [ ] Run the probes anyway and always report actual behavior — if default-deny works, report pass; if not, report fail; let the caller notice enforcement is off by the shape of the results
- [ ] Hard-fail with a clear 'NetworkPolicy enforcement is not active; refusing to produce misleading results' error; require the caller to fix the cluster first
- [ ] Inspect the Calico setup via kube-system labels, emit a structured warning in the response, and then run the probes — warning surfaces in tool output but doesn't block
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-8 -->

**What container image should the probe run?**

- [ ] curlimages/curl:8.5.0 (pinned upstream image) — tiny, widely-mirrored, single binary; no shell loops, each claim a separate curl invocation parsed by the orchestrator
- [ ] alpine:3.19 with curl and jq installed via entrypoint.sh — flexible probe script lives inline in the Job spec; slightly larger
- [ ] Reuse the existing egg-agent base image — identical environment to real agents; largest image, slowest startup
- [ ] A new minimal egg-probe image built alongside egg-gateway/egg-orchestrator — consistent provenance with the rest of the control plane
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-9 -->

**Until #1763 (SHA image tagging) lands the egg-gateway/egg-orchestrator images are :latest — how does that affect the probe image choice?**

- [ ] Not relevant: the probe uses an external image (curlimages/curl or alpine) with a pinned tag, so #1763 does not affect probe image reproducibility
- [ ] If the probe reuses an egg-* image, gate the tool on #1763 landing first so the probe image is SHA-pinned too — otherwise the probe can silently drift
- [ ] Pin the probe image by digest (sha256:...) in the Job spec, regardless of which base image is chosen — eliminates tag-drift independent of #1763
- [ ] Acceptable risk: probe image pinned to a tag now; migrate to digest pinning in a follow-up when #1763 normalises image handling across the control plane
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-10 -->

**What RBAC changes does the probe require?**

- [ ] Probably none beyond what the orchestrator already holds: jobs.create + pods.get + pods.log on egg-agents are already used for agent spawning. Confirm via audit and proceed
- [ ] Add events.list to egg-orchestrator SA so the tool can include NetworkPolicy-denial Events as evidence in failed claims
- [ ] Add events.list + pods/exec if decision-5 picks exec-based probing; otherwise only events.list
- [ ] Create a dedicated egg-probe ServiceAccount with the narrowest possible RBAC (jobs.create in egg-agents only) and have the orchestrator bind the Job to it — defence-in-depth; more YAML
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-11 -->

**Where should validate_network_isolation live in the codebase?**

- [ ] orchestrator/mcp_tools.py (existing location) with a new _handle_validate_network_isolation method that delegates to a new module orchestrator/k8s_isolation_probe.py for the actual Job-spawn-and-read logic
- [ ] New top-level module orchestrator/k8s_tools.py that groups all future cluster-scoped MCP tools; mcp_tools.py imports tool schemas from there
- [ ] Gateway-side new module gateway/isolation_probe.py because the gateway already brokers agent sessions and has the session_manager for scoped probe sessions — tool exposed via an orchestrator route that proxies to gateway
- [ ] Shared lib under shared/egg_k8s/ because the probe is reusable from CI (outside orchestrator) too
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-12 -->

**How should the skill contribution (auth-boundary error classification) be packaged?**

- [ ] New skill skills/agent-diagnose/SKILL.md scoped to auth/role errors only; the sibling slice (v2 or k8s-validation) extends it with env/events/OOM/probe sections when those tools land
- [ ] New skill skills/auth-diagnose/SKILL.md that is strictly auth-boundary-error scoped; separate skill name signals narrow scope; sibling slice ships a separate skills/agent-diagnose/ that does NOT overlap
- [ ] Add an 'Auth boundary errors' section to an existing skill (e.g., skills/sdlc/SKILL.md) — minimum new surface; risk of bloat
- [ ] Documentation-only: write docs/auth-boundary-error-taxonomy.md; do not ship a skill in this slice; sibling slice's agent-diagnose can cite the doc
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-13 -->

**Which auth-boundary error classes should the skill's dispatch table cover in this slice?**

- [ ] Minimal four-class taxonomy: (a) gateway 403 'Cannot determine agent role' → fine/coarse role mismatch (#1766 — should not recur, but historical); (b) orchestrator 401 on lifecycle mutate → missing EGG_LIFECYCLE_SECRET (post-#1772); (c) contract-write 403 on feedback or similar → field ownership / #1768-class; (d) agent-reaching-route-it-shouldn't → pattern of 200 where 401/403 expected (#1769-class)
- [ ] Four-class above plus: (e) 404 on container ID → Pod UID vs Job UID (#1760-class, non-auth but flagged in the issue's pattern list); (f) short-lived-pod-log-loss — include classification + mitigation pointer even though the fix isn't in this slice
- [ ] Full taxonomy (classes a-f) plus a template entry for 'unknown 4xx/5xx with auth-like stack trace'; overreach but future-proof
- [ ] Only class (d) — 'agent reaching a route it shouldn't be able to reach' — because the probe is the primary deliverable and the skill classification is secondary
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-14 -->

**Should the new k8s-aware MCP tools use a domain-prefixed naming convention?**

- [ ] Keep the issue's unprefixed name validate_network_isolation — matches the issue's proposed tool names for consistency across sibling slices
- [ ] Prefix with k8s_: k8s_validate_network_isolation — signals cluster-scoped tools; requires updating the issue's other proposed names too
- [ ] Prefix with probe_: probe_network_isolation — describes the action shape rather than the domain
- [ ] Prefix only cluster-scoped (k8s_) tools; leave gateway-scoped tools unprefixed
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-15 -->

**validate_network_isolation pass/fail reporting shape:**

- [ ] Structured per-claim: [{claim, expected, actual, pass, evidence, container_id}, ...] with evidence bounded (status code + first 512 bytes of body + up to 10 recent Events). No single top-level pass/fail.
- [ ] Structured per-claim plus top-level overall_pass = AND of all claim passes — machine-friendly for auto-recovery; slight schema redundancy
- [ ] Single top-level pass/fail only; evidence dumped as narrative text for the skill to parse — smallest schema; weakest contract
- [ ] Structured per-claim with pass = one of {true, false, null}; null signals 'could not evaluate' (e.g., Calico absent, probe pod failed to start) — three-valued logic, callers must handle null
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-16 -->

**Is the #1768 fix (register 'feedback' field ownership so refiner/implementer can add-feedback) in scope for this slice?**

- [ ] Out of scope. Separate issue/PR. This analysis continues to use the reformulate-as-decisions workaround (same as v2 slice)
- [ ] In scope as a trivial one-liner: add 'feedback' -> Role.IMPLEMENTER to FIELD_OWNERSHIP; ship in this slice since it is the same auth-boundary class
- [ ] In scope as a comprehensive fix: register ownership for 'feedback', 'feedback.*.answers.*', 'feedback.*.submitted', etc.; requires deciding who owns submitted vs answers vs questions — bigger change
- [ ] Split: register 'feedback' ownership as part of this slice (one-liner), but leave sub-field ownership (answers, submitted, etc.) to a separate issue
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-17 -->

**How should the probe's gateway session be provisioned and scoped?**

- [ ] Create a single-use session via session_manager.register_session() bound to the probe's Job container_id, with TTL = Job's active deadline (~60s); inject as GATEWAY_SESSION_TOKEN; revoke on completion. Matches agent session pattern.
- [ ] Reuse the orchestrator's own long-lived service credentials — no gateway session; the probe talks to orchestrator routes only. Means the probe cannot test gateway-reachability end-to-end.
- [ ] Create a sessions bound to a realistic AgentRole (e.g., refiner) so the probe exercises the post-#1766 fine→coarse resolution path as well as the #1769 auth-gate — maximum realism
- [ ] Create TWO sessions: one role=coder session for positive-case claims (should be allowed), one role=refiner session for reviewer-like claims — matrix testing across roles
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-19 -->

**What is the test coverage bar for this slice?**

- [ ] Unit tests for the MCP tool handler (mock KubernetesClient; assert Job spec shape, session creation, result parsing) + integration test that creates a real probe Job against the kind/k3s test cluster + skill tests exercise the classification dispatch table against canned error strings. Happy path + each failure class covered.
- [ ] Unit + integration as above, plus a post-deploy acceptance test run as part of make k3s-deploy that asserts current cluster posture — turns the probe into a deployment gate
- [ ] Unit tests only; integration tests land in a follow-up once CI's k3s-in-kind rig is in place
- [ ] Unit + integration + a lived test that runs the probe in the production cluster weekly (cron) and opens an issue on regression
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-20 -->

**Documentation surface for this slice:**

- [ ] Tool docstring (MCP schema) + skill SKILL.md + one-page reference under docs/mcp-tools/validate-network-isolation.md + an ADR (architecture decision record) for the probe-Job pattern — comprehensive, easier for reviewers and future adopters
- [ ] Tool docstring + skill SKILL.md only — rely on inline docstrings; no standalone doc files
- [ ] Tool docstring + skill SKILL.md + a runbook docs/runbooks/auth-boundary-incident.md that cross-references #1769 postmortem — operational focus
- [ ] Minimal: tool docstring only; SKILL.md deferred to decision-12 outcome
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-21 -->

**Dogfooding: do we run validate_network_isolation against the current cluster as part of this slice's acceptance?**

- [ ] Yes, mandatory: PR check runs the probe against a CI k3s cluster seeded with the current NetworkPolicy manifests + PR #1772 applied; merge blocked on all claims passing post-#1772
- [ ] Yes, advisory: run the probe as a dev-loop command (make validate-isolation) and publish results as a comment on the PR, but do not block merge on it until CI infra matures
- [ ] No: the probe is a tool; tests verify its behavior on a fixture cluster; running it against the production cluster is an operator activity, not a CI activity
- [ ] Partial: run probe in CI against a kind cluster with the manifests applied; do NOT run it against production or staging clusters from PR CI
- [ ] Other (explain in reply)

## Complexity Assessment

**medium**

Rationale:

- **Two new artifacts** (one MCP tool, one narrowly-scoped skill
  contribution) touching two subsystems (`orchestrator/mcp_tools.py` +
  `kubernetes_client.py`; `skills/`).
- **One new cluster resource pattern** (throwaway probe Job with a
  self-destructing session) but it reuses the existing
  `KubernetesSpawner` and `register_session` plumbing — no new RBAC
  needed if we avoid `pods/exec`.
- **No broad refactors**. No cross-cutting schema changes. No
  contracts-level changes (if decision-15 says no).
- **Tightly dependent on PR #1772** in its assertion semantics but not
  in its code path — the probe works regardless of merge state; the
  assertion outcome shifts.
- **Not high** because the parallelism is shallow (tool and skill are
  sequenceable in a single plan phase; no need to split work across
  phases) and the blast radius is scoped to `egg-agents` + a couple of
  orchestrator files.
- **Not low** because of the cluster-probe semantics, the #1768
  workaround that needs surfacing in the plan, the Calico-absent
  degraded mode, and the skill-packaging question.

---

*Authored-by: egg*

# Analysis: MCP tooling gaps for the new Kubernetes deployment

> Issue: #1759 | Phase: refine

## Problem Statement

PR #1692 migrated the egg control plane from Docker Compose to k3s. Validating
that migration on a fresh machine exposed a long tail of deployment bugs
(bad hostPath mounts, missing Secret references, health-endpoint path
mismatches, `ORCHESTRATOR_PORT` env collisions, `imagePullPolicy: IfNotPresent`
against absent tags, a silent pipeline FAILED caused by k8s monitor
false-positives, orphaned worktrees from a Compose-era state dir) that the
current MCP tool surface could not help diagnose. Every debug loop fell back
to `kubectl get pods`, `kubectl describe pod`, `kubectl logs deploy/…`,
`docker build && sudo k3s ctr images import -`, and `kubectl rollout
restart …`.

The MCP surface today is pipeline-centric — it assumes orchestrator+gateway
are already up and talking to each other, which is exactly the state that
breaks first on a k8s-native deployment. The issue proposes closing the gap
along two axes:

1. **New MCP tools** for deterministic, structured actions/fetches that
   agents and humans both benefit from (validation, isolation probes,
   worktree cleanup, deployment context).
2. **New skills** for diagnostic workflows where the value is judgment —
   knowing which evidence to collect, how to triage it, and how to narrate
   findings. Skills compose the underlying MCP tools.

The desired outcome is that the next end-to-end k3s validation pass — and
every routine diagnose-and-redeploy loop after it — can be driven entirely
from egg's MCP interface without dropping to raw `kubectl`, except for the
long-tail cases explicitly scoped out.

## Current Behavior

### MCP surface

MCP tools live in `orchestrator/mcp_tools.py` and are served by
`orchestrator/mcp_server.py` via FastMCP over HTTP. Handlers are dispatched
from `PipelineToolHandler.handle_tool_call`. The current tool catalogue:

- **Orchestrator-backed**: `submit_task`, `babysit_pr`, `get_status`,
  `cancel_task`, `list_tasks`, `provide_input`, `check_health`,
  `list_containers`, `get_container_logs`, `send_message`,
  `get_consensus_status`, `get_phase`, `get_pipeline_snapshot`,
  `validate_config`, `restart_agent`, `restart_phase`, `advance_phase`,
  `start_phase`, `complete_phase`, `populate_contract`.
- **Gateway-backed**: `list_checkpoints`, `search_checkpoints`,
  `get_contract`.

None of these currently speak to Kubernetes directly. `list_containers`
and `get_container_logs` call into `KubernetesSpawner`/`KubernetesClient`
but are scoped to *pipeline* containers — they do not expose cluster-level
state (pod Events, control-plane image tags, NetworkPolicy enforcement,
manifest drift) that the issue targets.

### Kubernetes client layer

`orchestrator/kubernetes_client.py` (≈945 lines) uses the synchronous
`kubernetes` Python client. It exposes:

- `create_container()` → Job via `BatchV1Api.create_namespaced_job`.
- `get_pod_logs()` → `read_namespaced_pod_log(tail_lines, since_seconds)`.
- `get_pod_status()` → `read_namespaced_pod()` and maps pod phase to
  `ContainerStatus`, inspecting `container_statuses[0].state.waiting.reason`
  for `ErrImagePull`.
- `get_pod_for_job()`, `list_containers()`, `wait_for_container()`,
  `is_connected()`.

What is **not** present today:

- Any use of the Events API (`CoreV1Api.list_namespaced_event`). Pod- and
  Job-level events (scheduling failures, ImagePullBackOff reasons,
  NetworkPolicy denials from CNI) are invisible to the orchestrator.
- Any dry-run / manifest-validation path — no use of
  `kubectl apply --dry-run=server`, `kubeconform`, `kube-score`, or hand-rolled
  semantic checks. `grep -rn dry-run` hits only git flags.
- Any async k8s client usage.

### Kubernetes deployment

The k3s deployment is a kustomize base + overlay
(`k8s/base/`, overlays under `k8s/overlays/local/`). Relevant facts for
this analysis:

- Health endpoints standardised on `/api/v1/health` (gateway port 9851,
  orchestrator on its `api` port) — `k8s/base/gateway-deployment.yaml:74,82`
  and `k8s/base/orchestrator-deployment.yaml`. The issue's `/healthz` vs
  `/api/v1/health` complaint is one a `validate_deployment_manifests`
  tool would catch on a fresh overlay.
- Secrets are created imperatively (`make k3s-secrets`) into a
  `gateway-secrets` Secret in `egg-system`; manifests reference that name
  but nothing verifies it exists before apply.
- Six Calico NetworkPolicies in `k8s/base/network-policies.yaml` encode
  the fail-closed agent isolation story (`default-deny-ingress/egress`
  plus four allow-lists: agent→gateway, agent→orchestrator,
  orchestrator→agent, agent→kube-dns). Nothing currently probes these
  policies at runtime.
- `make k3s-setup` passes `--flannel-backend=none --disable-network-policy`
  to k3s and relies on Calico being installed separately. Clusters that
  skip the Calico step silently lose NetworkPolicy enforcement.

### Agent spawner & session model

`orchestrator/kubernetes_spawner.py` (≈1081 lines) creates per-pipeline Jobs
in `egg-agents`, wires per-agent worktrees, and registers token-only
gateway sessions (IP-bound sessions don't work because Pod IPs are
ephemeral). Host-path volumes come from `EGG_HOST_REPO_MAP` and
`EGG_HOST_WORKTREES_PATH` env vars (currently hardcoded to `/home/jwies/...`
in the local overlay — flagged as follow-up in #1760).

### Worktree lifecycle

Two worktree locations matter:

- **Shared pipeline worktree dir**:
  `/home/egg/.egg-worktrees/<container_id>/<repo_name>`
  (`WORKTREE_BASE_DIR` in `gateway/worktree_manager.py:47`). Orphaned when a
  container is destroyed without running `remove_worktree()`.
- **Per-repo admin dirs**: `/home/egg/repos/<repo>/.git/worktrees/`. Stale
  when a worktree dir is removed by something other than `git worktree
  remove` (the Compose-era state dir the fresh k3s deploy inherited is the
  classic example from the issue).

`WorktreeManager.prune_stale_worktrees()`
(`gateway/worktree_manager.py:1270-1366`) already runs `git worktree prune`
per repo, respects locks, and is invoked on gateway startup by
`startup_cleanup()`. `cleanup_orphaned_worktrees()`
(worktree_manager.py:1182) handles the shared worktree base dir with a
manual `shutil.rmtree` fallback.

So the plumbing for both cleanup paths exists — what is missing is an
**on-demand MCP tool** to run them outside of gateway startup, with a
`dry_run` mode that reports findings without mutating state.

### `submit_task` / `cancel_task` reuse semantics

- `submit_task` (`orchestrator/mcp_tools.py:66-126`) derives `pipeline_id`
  from `issue_number` (or `jira_ticket`). The orchestrator routes now
  return HTTP 409 when a *live* pipeline exists — but allow branch reuse
  when the prior pipeline is `CANCELLED`/`FAILED`/`COMPLETE`
  (`orchestrator/routes/pipelines.py:820-880`). The MCP handler forwards
  these details back to the caller — no silent reuse of running state.
- `cancel_task(cleanup=True)` (mcp_tools.py) sends `PATCH status=cancelled`
  synchronously, then fires `DELETE /pipelines/{id}` **in a daemon
  thread**. The DELETE endpoint cleans up "containers, remote branches,
  Redis messages, and the state file" — but worktree removal is listed in
  `cleanup=True`'s argument description, so the effective contract is
  ambiguous in code vs. docstring.

The issue's observation ("pulled pods from the cancelled run's state
(pre-fix image)") is therefore most consistent with either (a) a race
between the background DELETE and the immediate next `submit_task`, or
(b) a stale `:latest` image cache in k3s containerd that survived cleanup.
#1763 fixes (b). (a) is a separate race worth flagging.

### Skills surface

`skills/` contains three skills today: `babysit-pr`, `egg-setup`, `sdlc`.
Each is a single directory with a `SKILL.md` file that has YAML front-matter
(`name`, `description`, `disable-model-invocation`, `argument-hint`) and a
markdown body of phased instructions. Skills invoke MCP tools through the
normal Claude tool-calling mechanism. There is no registry, no schema
enforcement on SKILL.md beyond what the harness parses, and no skill-level
test harness — behavior is tested by exercising the underlying MCP tools.
There are currently **no** k8s or diagnosis skills.

### Related work in flight

- **#1760** — k8s runtime reconciliation marks healthy agent pods FAILED
  during the `ContainerCreating → Running` transition.
  `kubernetes_monitor` needs to key off
  `containerStatuses[0].state.terminated.finishedAt`, not pod phase. This
  is the kind of failure `agent-diagnose` would make obvious.
- **#1763** — images pinned to `:latest` mean `kubectl apply` sees no
  spec change after a rebuild; the stale pod keeps running. Once
  SHA-tagged images land, the image-tag bullet in `get_deployment_context`
  becomes cheap, and `rebuild_and_rollout` collapses to `make redeploy` —
  so the issue explicitly *defers* that tool.
- **#3bfec3505** (already landed on main) fixes k8s container-ID
  resolution from Pod UIDs vs Job UIDs, and **#caddf4333** (already
  landed) fixes a gateway 403 for fine-grained AgentRole sessions. These
  are upstream of any new MCP tool that touches agent identities.

## Constraints

### Technical

- **k8s API auth**: the orchestrator runs with the `egg-orchestrator`
  ServiceAccount + RBAC (`k8s/base/rbac.yaml`). Any new tools that query
  the cluster (events, manifest dry-run, network-probe Job creation) may
  require RBAC additions (`events.list`, possibly `pods/exec` for probes,
  `jobs.create` in `egg-agents`). The issue implicitly assumes this
  without calling it out.
- **`kubernetes_client` is synchronous**. New tools should follow that
  convention; introducing async would require a broader refactor.
- **MCP output size cap and stiff schema**. The issue itself splits on
  this axis: structured, bounded output → tool; "dump me everything
  useful" → skill. We should not try to make `deployment-diagnose` a
  single MCP tool.
- **Calico vs. Flannel**: NetworkPolicies only bite on Calico (`make
  k3s-setup` forces `--flannel-backend=none --disable-network-policy` and
  assumes Calico is installed). `validate_network_isolation` must warn,
  not fail, when running on a cluster without NetworkPolicy enforcement.
- **Short-lived agent pods**: agent pods that crash in <10s are deleted
  before `kubectl logs` can fetch anything. The issue flags this as a
  **separate concern** but makes `agent-diagnose` "only as useful as the
  log data it can still reach." Any diagnose skill that depends on
  post-hoc log retrieval will be unreliable until log capture is
  addressed (enable `restartPolicy: OnFailure` with `--previous`, project
  logs to gateway, or set `ttlSecondsAfterFinished` high enough for a
  follow-up fetch).
- **Tool outputs should be safe by default**: `prune_stale_worktrees` is
  the only mutating tool proposed. Default should be `dry_run=true`.
- **Context discovery**: `get_deployment_context` needs to discriminate
  Docker vs. Kubernetes runtime. `KubernetesClient.is_connected()` is
  the obvious probe; the orchestrator already knows which spawner it
  loaded.
- **Validation lint choice**: `kubeconform` (schema only, fast, no
  cluster) vs. `kubectl apply --dry-run=server` (full admission, needs
  cluster) vs. hand-rolled Python checks. The issue's checklist is
  semantic ("Secret exists", "hostPath exists", "Service selector matches
  pod labels") which `kubeconform` alone will **not** catch — semantic
  checks belong in the tool, structural checks can be delegated.

### Business / scope

- **Non-goals (explicit in issue)**: no full k8s operator; no replacement
  for `kubectl`; read-mostly with a single narrow write action
  (`prune_stale_worktrees`). Anything more destructive stays explicit.
- **`rebuild_and_rollout` is deferred** pending #1763 land.
- **Scope sprawl risk**: the issue proposes four tools, two skills, and
  flags two separate concerns (submit_task reuse docs, short-lived pod
  logs). Shipping in one PR would be very large; staging the work is a
  **plan**-phase decision, not a refine-phase one, so I am not carving
  phases here — but I am surfacing the slicing question (decision-2).

### Dependencies

- **#1763 (image SHA tagging)**: `get_deployment_context` is more useful
  once images carry SHA tags; `rebuild_and_rollout` is only worth
  considering after it lands.
- **#1760 (monitor false-positive fix)**: `agent-diagnose` is most useful
  after the monitor stops pre-emptively marking pods FAILED — but it can
  ship before; in fact it would have surfaced #1760 faster.
- **Short-lived pod log capture** (flagged, not tracked): blocks
  `agent-diagnose` reliability in its worst case.

## Options Considered

### Option A: Build everything the issue asks for, in one feature set

**Approach**: Deliver all four tools (`validate_deployment_manifests`,
`validate_network_isolation`, `prune_stale_worktrees`,
`get_deployment_context`), both skills (`deployment-diagnose`,
`agent-diagnose`), and the `submit_task` reuse behaviour fix in a single
plan→implement cycle. Defer only `rebuild_and_rollout` per the issue.

**Pros**:
- Matches the issue's framing exactly.
- Each item has a concrete, validated use case from the session.
- Shipping skills alongside the tools means agents see the full
  diagnosis workflow on day one.

**Cons**:
- Large scope for a single cycle. Reviewer cost is high; regressions
  are harder to isolate.
- Mixes tool work, skill work, manifest/RBAC work, and
  orchestrator-route work — touches four subsystems in one PR.
- Ties `agent-diagnose` reliability to a fix for short-lived pod log
  capture that is not in scope.
- Ties `get_deployment_context` image-tag UX to #1763 landing first.

### Option B: Sequence by dependency — tools first, skills second

**Approach**: Land the four tools (and the `submit_task` doc/flag
clarification) as the first unit of work. Skills come after, once the
tools they compose are stable. Keep `rebuild_and_rollout` explicitly
deferred. Separately, acknowledge in this analysis that short-lived pod
log capture is a prerequisite for `agent-diagnose` reliability and tee
it up as follow-up work (don't solve it here).

**Pros**:
- Tools are independent units — each can be reviewed and tested on its
  own. MCP output schemas are the right forcing function for clean APIs.
- Skills become straightforward once primitives exist (the issue itself
  notes "most of which are existing Kubernetes API calls, some of which
  are the MCP tools above"). Skill work becomes a
  documentation+orchestration task rather than a plumbing task.
- Decouples the pod-log-capture dependency from shippable wins.
- Natural first milestone: "the next `make deploy` validation pass can
  be driven from MCP".

**Cons**:
- Skills ship later, so initial value is narrower.
- Two reviewable artefacts instead of one.
- Risk of the skill phase sliding if other priorities intervene.

### Option C: Skills-first, thin tools

**Approach**: Ship `deployment-diagnose` and `agent-diagnose` as skills
that shell out to `kubectl` / existing MCP tools today, adding new MCP
tools only where a raw-`kubectl` approach won't work (the network-isolation
probe, the worktree pruner). Validation of manifests gets pushed into CI
or a Makefile target rather than an MCP tool.

**Pros**:
- Fastest to first diagnosis-user-value.
- Minimises MCP schema surface area.
- Matches the issue's warning that "dump everything" belongs in skill
  shape.

**Cons**:
- Skills that shell out to `kubectl` are harder to run from agent pods
  (by design, agents don't have `kubectl` inside the sandbox). The tools
  the issue proposes exist specifically so agent-side auto-recovery
  becomes possible — dropping that reduces the issue to a human-only
  debug aid.
- Punts `validate_deployment_manifests` to a different lifecycle
  (CI/Makefile), which doesn't help the "agent-initiated redeploy"
  path the issue anticipates.
- Doesn't address the agent-accessibility goal that motivated the
  tool/skill split in the first place.

### Option D: Minimal subset — only what the last validation session required, plus doc fixes

**Approach**: Ship `prune_stale_worktrees` (worktree orphans broke the
first submission), `validate_deployment_manifests` (catches most of the
static bugs from the session), and add documentation to
`submit_task`/`cancel_task` about the reuse race. Everything else becomes
separate issues.

**Pros**:
- Smallest possible surface. Low risk.
- Directly maps to the "saved hours in that session" subset the issue
  calls out.

**Cons**:
- Leaves `validate_network_isolation` unshipped, so the Calico
  NetworkPolicy posture remains unverified at runtime — one of the PR's
  least-tested claims.
- No diagnose skills, so every future k8s bug will still fall back to
  `kubectl describe`.
- Fragments the issue into follow-ups for unclear reason — all four
  tools have concrete, validated use cases.

## Recommended Approach

**Option B (sequenced: tools → skills, `rebuild_and_rollout` deferred).**

Rationale:

1. The tool-vs-skill split in the issue is a *good* split and should be
   honoured: the tools become the primitives the skills compose. Landing
   them first gives the skills a stable foundation and gives `make deploy`
   validation a bounded, reviewable win.
2. Tools are independent of each other and can be planned as parallelizable
   work in the plan phase. Skills cannot start until the tools they
   compose exist, so ordering is forced by the call graph.
3. #1763 and #1760 are in flight. Deferring `rebuild_and_rollout` (per the
   issue) and letting #1760 ship first for `agent-diagnose`'s usefulness
   are concrete sequencing hooks that make Option B cleaner than
   Option A.
4. The pod-log-capture concern is **flagged** but kept out of scope —
   the analysis treats it as a prerequisite for `agent-diagnose`
   reliability rather than part of this issue's deliverables. That
   should be a separate issue (decision-6).
5. The `submit_task` reuse behaviour is a doc/flag question with low
   implementation cost but unclear desired behaviour (add `fresh=true`?
   make cleanup synchronous? document only?). It should ride with the
   tools phase but be sized explicitly in plan (decisions 5 and 15).

Key decisions the plan phase needs to make but this analysis does not:

- Where each new tool lives in `mcp_tools.py` (orchestrator-backed vs. a
  new `k8s-backed` domain section).
- Whether validation uses `kubeconform`, `kubectl --dry-run`, or pure
  Python (decision-3).
- Exact RBAC additions to `k8s/base/rbac.yaml` (decision-11).
- Whether `validate_network_isolation` spawns its own probe Job or
  `pods/exec`'s into an existing pod (decision-4).

## Open Questions

All questions below are registered as contract decisions via
`egg-contract add-decision`. Run `egg-contract --issue 1759 show` to see
them in the contract. Humans resolve each by checking the box for their
preferred option.

> **Note on decision-1**: `decision-1` in the contract is a neutralized
> test artifact (`__test_reset__`, pre-resolved) generated while
> smoke-testing the CLI workflow. Real open questions are decisions 2-16.
>
> **Note on feedback vs. decisions**: The top-level `feedback` field on
> the contract is owned by the `system` role
> (`shared/egg_contracts/roles.py`), and the current gateway role
> resolution maps the `refiner` session role to `implementer`. That
> means `egg-contract add-feedback` fails with a 403 from this role.
> To keep all open questions discoverable via `egg-contract show`, I
> reformulated every open-ended prompt as a multi-choice decision with
> natural options plus the auto-appended "Other (explain in reply)" that
> preserves free-form answers. Decisions 10-16 are the former open-ended
> feedback items. The role-resolution gap is worth raising as a separate
> issue; it is not in scope for #1759.

### Decisions

<!-- egg-hitl-decision id=decision-2 -->

**How should we slice issue #1759 into shippable units?**

- [ ] Option B: Sequenced — tools land first (validate_deployment_manifests, validate_network_isolation, prune_stale_worktrees, get_deployment_context) then skills (deployment-diagnose, agent-diagnose); rebuild_and_rollout deferred per issue (Recommended)
- [ ] Option A: One feature set — all four tools plus both skills plus submit_task reuse clarification shipped in one plan→implement cycle
- [ ] Option C: Skills-first — ship diagnose skills that shell out to kubectl and existing MCP tools; add new MCP tools only where kubectl will not work
- [ ] Option D: Minimal subset — only prune_stale_worktrees plus validate_deployment_manifests plus a submit_task documentation fix; all other items become follow-up issues
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-3 -->

**What should back validate_deployment_manifests?**

- [ ] Pure Python on parsed kustomize output — no external binary deps, covers the issue's semantic checklist; more code to maintain (Recommended)
- [ ] External kubeconform binary for schema validation plus Python for semantic checks — structural coverage offloaded but adds a runtime dep
- [ ] kubectl apply --dry-run=server for structural validation plus Python for semantic — catches admission-webhook errors but requires a live cluster at validation time
- [ ] Delegate structural validation to kubectl --dry-run=client and do all semantic checks in Python — no server needed but admission rules not exercised
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-4 -->

**How should validate_network_isolation run its probes?**

- [ ] Spawn a throwaway probe Job in egg-agents and read its logs — isolated; costs a Job lifecycle per call; narrow RBAC (Recommended)
- [ ] pods/exec into an existing agent pod when present, else spawn a probe Job — cheaper while pipelines are live; wider RBAC (pods/exec)
- [ ] Always pods/exec into a long-lived probe Deployment added to the overlay — fastest per call; persistent cluster overhead
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-5 -->

**What should happen when submit_task is called with a pipeline_id whose previous run was just cancelled with cleanup=true?**

- [ ] Add a fresh=true flag on submit_task — without it keep the current behavior; with it, wait for in-flight cleanup to finish before creating the new pipeline (Recommended)
- [ ] Make cancel_task(cleanup=true) block synchronously until cleanup finishes; no submit_task change required
- [ ] Document only — call out that cancel_task(cleanup=true) plus immediate submit_task is non-idempotent and the caller must wait
- [ ] Have submit_task detect a cancelled+cleanup-in-progress state and queue the new run behind it automatically
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-6 -->

**Short-lived agent pods (<10s lifespan) lose their logs before kubectl logs can fetch them. Where should fixing that land?**

- [ ] Separate follow-up issue; agent-diagnose ships with documented best-effort log fetch (Recommended)
- [ ] Pull it into scope of #1759 — needed for agent-diagnose to be reliable
- [ ] Fold it into #1760 — the reconciliation false-positive is the primary producer of short-lived crashed pods
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-7 -->

**What should the default behavior be for prune_stale_worktrees?**

- [ ] dry_run=true by default; caller must pass dry_run=false to actually remove — matches the issue's proposed signature (Recommended)
- [ ] dry_run=false by default but always include a preview section in the response — matches cancel_task(cleanup=true) ergonomics
- [ ] Two separate tools — list_stale_worktrees (read-only) and prune_stale_worktrees (write) — stricter API surface
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-8 -->

**get_deployment_context exposes the image tag currently running per component. Until #1763 lands (SHA-tagged images), the tag is :latest which is ambiguous. What should the tool do about this?**

- [ ] Ship now; return :latest with a warning; stop warning once #1763 SHA tags land (Recommended)
- [ ] Block this tool on #1763 landing so its image-tag field is always meaningful
- [ ] Ship now and also surface the image digest (containerStatuses[*].imageID) which is unique even for :latest
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-9 -->

**Should the new k8s-aware MCP tools use a domain-prefixed naming convention?**

- [ ] Keep the issue's unprefixed names — validate_deployment_manifests, validate_network_isolation, prune_stale_worktrees, get_deployment_context (Recommended)
- [ ] Prefix with k8s_ — k8s_validate_manifests, k8s_probe_isolation, k8s_get_context, plus gateway_prune_worktrees for the worktree pruner
- [ ] Prefix only the cluster-scoped tools (k8s_*) and leave gateway-scoped worktree tool unprefixed
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-10 -->

**Beyond the six semantic checks the issue lists, which additional manifest anti-patterns should validate_deployment_manifests also catch?**

- [ ] Add security-posture checks: missing runAsNonRoot, missing readOnlyRootFilesystem, missing seccompProfile, capabilities.add of SYS_ADMIN/NET_ADMIN, hostNetwork/hostPID true (Recommended)
- [ ] Add reliability checks: missing resource requests/limits, missing liveness/readiness probes, restartPolicy mismatched to Job kind
- [ ] Add both security-posture and reliability checks
- [ ] Keep strictly to the six checks the issue lists; add more only if a concrete bug surfaces
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-11 -->

**What RBAC posture should the new orchestrator-facing tools hold? events.list is required in all options; the variable is where pods/exec and Job-create sit.**

- [ ] Extend the existing egg-orchestrator ServiceAccount with events.list plus pods/exec and jobs.create (batch/v1) in egg-agents namespace only — simplest; wide but namespace-scoped (Recommended)
- [ ] Create a dedicated probe ServiceAccount with pods/exec and jobs.create only, used only by validate_network_isolation; orchestrator SA gets events.list only — stricter separation
- [ ] Do not grant pods/exec; have the probe Job create its own short-lived SA via a downward-API pattern — most defense-in-depth; highest implementation cost
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-12 -->

**validate_network_isolation pass/fail semantics: the tool verifies four distinct claims. How should it report?**

- [ ] Always return structured per-claim results ({claim, pass, evidence}) and let the caller decide — matches existing MCP conventions; caller-friendly (Recommended)
- [ ] Return a single pass/fail for the whole tool call; fail if any claim fails — machine-friendly for auto-recovery agents; less diagnosable
- [ ] Return structured per-claim results plus a top-level overall pass/fail that is false when any claim fails
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-13 -->

**deployment-diagnose evidence ordering. The issue says 'lead with whichever piece of evidence made the failure obvious.' How should the skill choose its lede?**

- [ ] Infer ordering from the symptom: ErrImagePull → image tag + pull events; CrashLoopBackOff → last exit reason + logs; Pending → scheduling Events; Running-but-unhealthy → health endpoint body (Recommended)
- [ ] Use a fixed ordering for every run: Events → container statuses → image drift → env resolved → health body — predictable but doesn't match human intuition
- [ ] Let the skill try both — output a structured report with all sections always present, plus an at-top 'Most likely cause' paragraph the LLM synthesises from the evidence
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-14 -->

**get_deployment_context cross-runtime portability. How should the tool behave on a Docker/Compose deployment?**

- [ ] Degrade cleanly: return runtime=docker with k8s-specific fields omitted/null; works identically in both worlds (Recommended)
- [ ] Hard-fail with a clear 'use the compose-specific tool' error — preserves k8s-only invariants, simpler schema
- [ ] Return runtime=docker with a docker-specific payload (image, container id, network) parallel to the k8s payload — richer but doubles the schema
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-15 -->

**Scope boundary for the submit_task-after-cancel reuse race (the issue's 'silently reuses existing task_id' note). Is this in scope for #1759 or should it split out?**

- [ ] Keep in scope for #1759; resolve via decision-5 (fresh=true flag or equivalent) in the same cycle (Recommended)
- [ ] Split into its own follow-up issue; #1759 only adds documentation to submit_task/cancel_task surfaces; the behavior change ships separately
- [ ] Fold into the k8s monitor fix (#1760) because the stale-state symptom is amplified on k3s by cleanup races
- [ ] Other (explain in reply)

<!-- egg-hitl-decision id=decision-16 -->

**Manifest source for validate_deployment_manifests. Should it accept an overlay path parameter, or only validate the currently-applied overlay?**

- [ ] Accept an optional overlay_path parameter, defaulting to k8s/overlays/local/ — enables CI and future GKE overlay validation without modification (Recommended)
- [ ] Hardcode to the currently-applied overlay (read from orchestrator's known-good path) — simplest surface; blocks CI dry-runs
- [ ] Accept overlay_path as required; caller must always specify — maximally explicit; more ceremony on the happy path
- [ ] Other (explain in reply)

## Complexity Assessment

**high**

Rationale:

- Four new MCP tools, each touching a distinct subsystem (manifest parsing;
  k8s Events + probe Jobs; gateway WorktreeManager; k8s client + runtime
  discovery).
- Two new skills that compose the tools and require SKILL.md design and
  ranking heuristics.
- A doc/flag change on `submit_task`/`cancel_task` reuse semantics.
- Cross-cutting concerns: RBAC updates to `k8s/base/rbac.yaml`, possible
  new NetworkPolicy entries for a probe Job, and a latent prerequisite
  (short-lived pod log capture) that affects skill reliability.
- Multiple independent phases exist (each tool independent of the others;
  each skill independent once its backing tools exist), which is the
  textbook profile for high-complexity, parallelizable work in the plan
  phase.

---

*Authored-by: egg*

# metadata
complexity_tier: high
parallel_phases: true

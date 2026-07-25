# Review Quality Reference

This document is a current-state snapshot of egg's review-quality machinery as
shipped for [issue #3523](https://github.com/jwbron/egg/issues/3523) — "structured
findings, verification ladder, risk-routed review graph, shared-evidence prompt
prefix." It describes five changes that harden the BRC (Broadcast-Review-Converge)
review graph against three costs: wasted revision rounds, shallow findings, and
uniform cost regardless of risk.

The design deliberately mirrors the structure and vocabulary of Claude Code's
built-in `/review` / `/code-review` skill (four-level effort ladder, finder angles,
three-state verify, finding shape) so egg's review machinery stays interoperable
with it. Treat that skill as the reference design; this doc uses its vocabulary
rather than a bespoke one.

## Staged rollout convention (`off → log → on`)

Every behavior-shifting piece below ships behind the established staged-flag
convention (originally mirrored in `orchestrator/slice_green_gate.py`, which has
since diverged — that gate now defaults to `on` and degrades an unknown value to
`on` with a warning, since over-verifying is its safe direction. The flags below
keep the off-default shape, since under-reviewing is never safe for them):

- **`off`** (default) — inert. Behavior is byte-identical to the pre-#3523 pipeline.
  Unknown / mistyped flag values resolve to `off` (fail-safe): an operator typo
  must never silently activate a behavior shift.
- **`log`** — compute the would-be behavior and record it (to logs / existing BRC
  artifacts), but do **not** apply it. This is the measurement/observation stage.
- **`on`** — apply the behavior.

The flags are read in code, not in prompts, so the gate is deterministic. The two
runtime flags governing this rollout are `EGG_RISK_ROUTER` (§4) and
`EGG_REVIEW_EVIDENCE_PREFIX` (§5); the per-finding tool-call cap has its own
`…_MODE` flag (§2).

## 1. Structured findings and the server-side computed verdict

A reviewer no longer emits a prose-only NACK. It emits a **versioned finding
schema**, and orchestrator-side code computes the edge verdict from the findings.
Models own judgment (what to flag, severity, confidence, prose); code owns
mechanics (dedup, verdict, rendering).

### The finding schema

Defined in [`shared/egg_contracts/review_findings.py`](../../shared/egg_contracts/review_findings.py),
next to the other verdict contracts and validated at the message boundary the same
way `orchestrator/attestation_schemas.py` validates attestations. The wire schema
is versioned (`FINDINGS_SCHEMA_VERSION = 1`) and evolves additively.

A `Finding` carries:

- `schema_version` — wire version (defaulted, bumped additively).
- `id` — stable identifier within the review batch (required, non-empty).
- `role` — which lens produced it, e.g. `reviewer_security` (required, non-empty).
- `anchor` — a `FindingAnchor`: `path` + line range, or slice-level for
  cross-cutting findings.
- `summary` — one sentence (required, non-empty).
- `failure_scenario` — concrete inputs/state, then the resulting wrong output,
  crash, or data loss. **Required to block:** a finding with no failure scenario is
  representable but is flagged non-blocking-eligible; it can never be a valid
  blocking finding (see `Finding.is_blocking_eligible()`).
- `severity` — `FindingSeverity`: `blocking` | `advisory` (defaults to advisory).
- `confidence` — `FindingConfidence`: `high` | `medium` | `low`.
- `evidence` — the quoted triggering line, or what was checked.
- `suggested_patch` — optional fix referencing real symbols.
- `pre_merge_obligation` — optional human-only merge-time action.
- `converged_roles` — the producing lenses when ≥2 lenses merge on one mechanism
  (see convergence, below).

`validate_findings_payload(data)` parses an untrusted payload at the wire boundary,
raising `ValueError` on a structurally malformed payload (missing required
`id`/`role`/`summary`, bad enum, wrong types). It deliberately does **not** raise
when a `blocking` finding lacks a `failure_scenario` — that condition is surfaced
(not errored) via `non_blocking_eligible_warnings(payload)`, and such a finding
simply cannot block.

### The computed edge verdict

[`orchestrator/review_findings_verdict.py`](../../orchestrator/review_findings_verdict.py)
`compute_verdict(payload)` is the determinism boundary. It:

1. runs mechanism-level dedup first (`merge_findings_by_mechanism`),
2. partitions findings into blocking-eligible vs advisory, and
3. returns a `ComputedVerdict`.

The three outcomes:

| Findings | Verdict |
|----------|---------|
| Any blocking-eligible finding | **NACK** (`VERDICT_NACK`) |
| Advisory-only (with `pre_merge_obligation` text) | **ACK** with obligations → the existing [conditional-ACK](conditional-ack.md) path |
| Empty | **ACK** (`VERDICT_ACK`) |

`ApprovalMatrix.record_findings_verdict()`
([`orchestrator/approval_matrix.py`](../../orchestrator/approval_matrix.py)) applies
the computed verdict through the same `record_ack` / `record_nack` primitives the
legacy path uses, so advisory obligations flow into the existing conditional-ACK
machinery unchanged.

### Mechanism dedup and convergence-as-signal

`merge_findings_by_mechanism()` collapses findings that name the same causal
mechanism, keyed by `_mechanism_key()` — an explicit `mechanism` tag if a reviewer
set one, else a concrete file anchor (`path` + line range), else the normalized
`summary`. Code derives the key only from concrete fields; it never makes a semantic
guess.

When ≥2 distinct lenses converge on one mechanism, the merged finding records all
producing lenses in `converged_roles` and its confidence is raised one rung
(`_raise_confidence`). That convergence — previously discarded — is surfaced to the
producer in the NACK reason and is available via `ComputedVerdict.converged_findings`
for escalation.

### The producer-facing NACK reason

`render_findings_nack_reason(computed)`
([`orchestrator/consensus_wrapper.py`](../../orchestrator/consensus_wrapper.py)) is
the rendering half of the boundary split (verdict logic lives in
`review_findings_verdict.py`; this turns the resulting findings into the prose the
producer sees in `ApprovalEntry.reason`). Only blocking findings drive the reason;
convergence (`converged across N lenses: …`) is surfaced inline, and advisory
obligations are appended as a non-blocking footer. The output is deterministic
(unit-test-golden-friendly).

## 2. The CONFIRMED / PLAUSIBLE / REFUTED verification ladder

Every specialist criteria file carries a shared verification ladder, so all lenses
share one verify discipline. It lives under the heading **"Verification Ladder —
CONFIRMED / PLAUSIBLE / REFUTED"** in:

- [`shared/prompts/code-review-criteria.md`](../../shared/prompts/code-review-criteria.md)
- [`shared/prompts/code-review-holistic-criteria.md`](../../shared/prompts/code-review-holistic-criteria.md)
- [`shared/prompts/security-review-criteria.md`](../../shared/prompts/security-review-criteria.md)
- [`shared/prompts/concurrency-review-criteria.md`](../../shared/prompts/concurrency-review-criteria.md)
- [`shared/prompts/agent-design-criteria.md`](../../shared/prompts/agent-design-criteria.md)
- [`shared/prompts/contract-review-criteria.md`](../../shared/prompts/contract-review-criteria.md)

The three states, with symmetric evidence duties:

- **CONFIRMED** — can name the inputs/state that trigger it and the wrong output;
  quote the triggering line.
- **PLAUSIBLE** — mechanism is real, trigger uncertain (timing, env, config); state
  what would confirm it.
- **REFUTED** — factually wrong or guarded elsewhere; quote the guard that breaks
  the causal chain.

Mapping onto verdicts: a CONFIRMED blocking finding NACKs; PLAUSIBLE demotes to
advisory / a pre-merge obligation and never blocks; only REFUTED drops silently.
Two companion rules encode this:

- **Blocking must reproduce** — mark a finding blocking only if you can state a
  concrete failing scenario; if you cannot, it is advisory. (This is enforced
  structurally too: see `Finding.is_blocking_eligible()` in §1.)
- **Drop only the refuted; downgrade the unconfirmed** — drop a claim only when you
  can show it wrong against the code; if it is plausibly real but unconfirmed, keep
  it as advisory rather than dropping it.

One nuance is encoded explicitly: a **pre-existing defect still CONFIRMS when the
diff materially amplifies its consequence**, and the finding must say so. This
sharpens the older "do not dismiss issues as not-a-regression" rule.

### Empirical scratch checks and the per-finding tool-call cap

Reviewers run in sandboxes with full checkouts, so the criteria grant a **read-only
scratch-check permission**: cheap experiments in a scratch directory (never mutating
the checkout, never the network) to confirm or refute a claim — e.g. actually
running a disputed command, or reading a pinned dependency's real source instead of
trusting memory.

Their cost is bounded by a **per-finding tool-call cap**, enforced in the wrapper
(not the prompt) in
[`orchestrator/consensus_wrapper.py`](../../orchestrator/consensus_wrapper.py)
(`review_finding_tool_call_cap()`, `evaluate_finding_tool_call_cap()`). It is
governed by two env vars:

- `EGG_REVIEW_FINDING_TOOL_CALL_CAP` — the integer cap.
- `EGG_REVIEW_FINDING_TOOL_CALL_CAP_MODE` — the staged flag (`off` / `log` / `on`);
  `log` records would-have-been-capped outcomes without enforcing.

## 3. Method-angle procedures (the four finder angles)

The domain lenses say *what* to care about; the code reviewer prompt adds named
procedures that say *how to look*. They live under the heading **"Finder Method —
four angles"** in
[`shared/prompts/code-review-criteria.md`](../../shared/prompts/code-review-criteria.md)
and [`shared/prompts/code-review-holistic-criteria.md`](../../shared/prompts/code-review-holistic-criteria.md),
mirroring the `/review` skill's finder angles A–E:

1. **Line-by-line scan** — for every changed line, ask what input, state, timing, or
   platform makes it wrong (inverted conditions, off-by-one, null deref, missing
   await, falsy-zero, wrong-variable copy-paste, swallowed errors).
2. **Removed-behavior audit** — for every line the diff deletes or replaces, name
   the invariant it enforced and find where the new code re-establishes it; if it
   does not, that is a finding. (Deletion-shaped regressions — dropped guards,
   narrowed validation, deleted tests — are exactly what a coder under revision
   pressure produces.)
3. **Cross-file tracer** — for each changed function, grep callers for broken call
   sites (new precondition, changed return shape, new exception); check callees for
   in-slice parallel changes that make a call unsafe.
4. **Quote-the-rule discipline** — flag a convention violation only when both the
   exact written rule and the exact violating line can be quoted. No
   spirit-of-the-doc inference.

The specialist lenses (security, concurrency, agent-design, contract) share the
verification ladder and companion rules of §2 but keep their own domain content;
the four method angles are specific to the code-review and holistic lenses.

## 4. The deterministic risk router

The review graph was static — every slice got all critical lenses at full depth.
A deterministic router (plain code, never a model) now sits in front of it and, per
slice, gates lenses, sets a risk tier that scales reasoning effort, and optionally
scales stance.

### The router

[`orchestrator/risk_router.py`](../../orchestrator/risk_router.py) `route_slice(changed_files, config)`
is pure: the same changed-file set and config always yield the same
`RiskRouteDecision` (`lenses`, `tier`, `stance`, `unrouted`, `forced_security`,
`warnings`). `RiskTier` is an ordered `LOW < MEDIUM < HIGH < XHIGH`.

### The per-repo config: `.egg/review-risk.yaml`

[`.egg/review-risk.yaml`](../../.egg/review-risk.yaml) is the policy input; the
router reads it via `load_risk_config()`. Format:

```yaml
schema_version: 1          # currently 1; evolve additively
rules:                     # ordered list of glob -> {lenses, tier}
  - match: "**/*.md"       # canonical key (alias: `glob`); shared match_pattern grammar
    lenses: [reviewer_code]              # non-empty; from the FULL lens set
    tier: low                            # low|medium|high|xhigh
  - match: "gateway/"
    lenses: [reviewer_code, reviewer_code_holistic, reviewer_contract, reviewer_security]
    tier: xhigh
```

The valid lenses are the members of `FULL_IMPLEMENT_LENSES`: `reviewer_code`,
`reviewer_code_holistic`, `reviewer_contract`, `reviewer_security`,
`reviewer_concurrency`.

**Resolution.** Each changed file resolves to the most-specific matching rule (the
glob with the most literal, non-wildcard characters wins; ties break by declaration
order). Across the slice, lenses **union** and the tier is the **max** any file
demands.

### Lens gating, effort tiers, and stance

- **Lens gating** — a docs-only slice gets a minimal graph (e.g. a single
  `reviewer_code` lens at `low`); the concurrency lens runs only on the paths a rule
  assigns it; and security is force-added on protected paths (below).
- **Effort tiers** — each tier maps to a reasoning effort (`_TIER_EFFORT`) and the
  `/review` finding caps (`_TIER_REVIEW_CAP` = 4 / 8 / 10 / 15 for
  low / medium / high / xhigh). The effort is injected through the existing seam,
  `resolve_review_effort()` in
  [`orchestrator/agent_model_resolution.py`](../../orchestrator/agent_model_resolution.py),
  which threads `--effort` into the consensus-wrapped command.
- **Stance** — `stance_for_tier()` returns `PRECISION_FIRST` on `LOW` (trivial ⇒
  fewer, high-confidence findings), `RECALL_FIRST` on `HIGH`/`XHIGH` (risky ⇒
  coverage), and no override on `MEDIUM`.

### HARD floors (never less review than is safe)

Enforced in `route_slice()`, not in the config:

1. **Unrouted files** — any changed file matching no rule ⇒ the FULL lens set + a
   loud warning ("missing config never means less review") + tier floored to
   `MISROUTE_FLOOR_TIER` (`HIGH`).
2. **Security-sensitive paths** — a slice touching an auth/session/input-boundary
   path (`is_security_sensitive()` / `SECURITY_SENSITIVE_GLOBS`) always runs
   `reviewer_security`, even if the matched rule omits it, with the tier floored to
   `HIGH`. The protected-path set is in code and cannot be overridden by config.
3. **Floor tier** — every slice is guaranteed at least `FLOOR_TIER` (`LOW`).

### The flag and fail-open

The router rides one staged flag, `EGG_RISK_ROUTER` (`off` / `log` / `on`), resolved
by `risk_router_mode()` in
[`orchestrator/review_graph.py`](../../orchestrator/review_graph.py). In `log` mode
it computes the would-be gated graph / tier / effort and logs it while returning the
unchanged full graph. If `review-risk.yaml` fails to load, `resolve_risk_decision()`
**fails open** to the FULL review graph + legacy effort with a warning — a broken
config never silently reduces review.

## 5. The shared-evidence prompt prefix (the cost bet)

Every reviewer in a wave used to cold-start, each re-reading the same diff and files.
An unprivileged evidence gatherer now assembles the shared context once, so parallel
same-model reviewers can share a byte-identical cached prefix (prompt caching prices
cached reads at roughly a tenth of standard input).

### The evidence gatherer

[`orchestrator/evidence_gatherer.py`](../../orchestrator/evidence_gatherer.py)
`gather_evidence(...)` is read-only, has no GitHub access, casts no verdict, and
posts nothing. It assembles an `EvidencePack`: the diff, changed files with enclosing
context, caller/callee lists for changed symbols, and verified environment facts.

**Evidence, never conclusions.** The pack carries evidence only — no hypotheses, no
"areas of concern," no ordering by importance. Ordering is mechanical (path order),
because emphasis is covert anchoring: if the gatherer editorializes, every lens
anchors on one framing and convergence-as-signal (§1) stops meaning anything. This is
enforced structurally: `assert_pack_carries_no_conclusions()` runs at import and
fails loudly if the pack schema ever grows an editorializing field.

### Prompt assembly

Reviewer prompts are assembled as `[identical system prefix][identical evidence
pack][one lens instruction at the tail]` in
[`orchestrator/routes/pipelines/_criteria.py`](../../orchestrator/routes/pipelines/_criteria.py):
`_SHARED_EVIDENCE_SYSTEM_PREFIX` + `build_shared_evidence_prefix(pack)` +
`apply_shared_evidence_prefix(lens_instruction, role, pack)`. The system prefix
frames the pack as **material under review, never instructions**, so adversarial text
in the diff — which now flows through one gatherer into every reviewer's prefix — is
treated as data. Each reviewer still runs its own greps, traces, and scratch checks
from the shared base: the pack eliminates redundant ramp-up, not investigation.

### Independence guardrails

- **Cold-start roles.** `shares_evidence_prefix(role)` returns `True` only for the
  specialist lenses (`EVIDENCE_PREFIX_SHARING_ROLES`). The `tester` and any
  `finding_verifier` are in `COLD_START_ROLES = {"tester", "finding_verifier"}` and
  never inherit the prefix — a verdict that comes from executing the proposal, or a
  verification of a finding, must not inherit the context that produced the claim.
- **Delphi redaction unchanged.** The pack carries repo facts only, never a
  producer's self-assessment, so Delphi redaction of proposal payloads is unaffected.

### The flag and the cost-measurement gate

The prefix rides `EGG_REVIEW_EVIDENCE_PREFIX` (`off` / `log` / `on`), resolved by
`evidence_prefix_mode()`. Cost is a first-class acceptance criterion here: the flip
to `on` requires a **measured net per-wave cost reduction** observed in `log` mode
first. In `log` mode the machinery gathers the pack and records the would-have-been
cache-hit rate and per-wave cost without changing the prompt
(`aggregate_wave_cache_stats()`, `evidence_prefix_log_record()` in
`consensus_wrapper.py`). The underlying per-session cache stats come from the
LiteLLM cost logger
([`config/litellm/cost_callback.py`](../../config/litellm/cost_callback.py),
`LiteLLMCostLogger.async_log_success_event`, which emits `cache_hit_rate_pct`).

## Sequencing and scope notes

The pieces shipped in the issue's stated order: method angles + the prompt-level
rules of the ladder first (prompt-only, zero-regret); then the finding schema +
computed verdict + dedup/convergence; then the risk router (config + gating + effort,
`log` first); then the evidence prefix last, behind its own flag and cost gate.

Out of scope by operator directive: an eval/benchmark harness (this is a
single-operator deployment; the operator judges quality directly) and
human-feedback learning loops (no human PR review exists in this deployment).

## Pointers

- Finding schema: [`shared/egg_contracts/review_findings.py`](../../shared/egg_contracts/review_findings.py)
  — `Finding`, `FindingAnchor`, `FindingSeverity`, `FindingConfidence`,
  `FINDINGS_SCHEMA_VERSION`, `is_blocking_eligible()`, `validate_findings_payload()`,
  `non_blocking_eligible_warnings()`.
- Verdict + convergence: [`orchestrator/review_findings_verdict.py`](../../orchestrator/review_findings_verdict.py)
  — `compute_verdict()`, `merge_findings_by_mechanism()`, `_mechanism_key()`,
  `ComputedVerdict.converged_findings`.
- Matrix integration + NACK render + tool-call cap: [`orchestrator/consensus_wrapper.py`](../../orchestrator/consensus_wrapper.py)
  (`render_findings_nack_reason()`, `review_finding_tool_call_cap()`),
  [`orchestrator/approval_matrix.py`](../../orchestrator/approval_matrix.py)
  (`record_findings_verdict()`).
- Reviewer criteria (ladder, companion rules, method angles): [`shared/prompts/`](../../shared/prompts/)
  `*-criteria.md`.
- Risk router: [`orchestrator/risk_router.py`](../../orchestrator/risk_router.py),
  config [`.egg/review-risk.yaml`](../../.egg/review-risk.yaml), flag +
  fail-open in [`orchestrator/review_graph.py`](../../orchestrator/review_graph.py),
  effort seam in [`orchestrator/agent_model_resolution.py`](../../orchestrator/agent_model_resolution.py).
- Evidence prefix: [`orchestrator/evidence_gatherer.py`](../../orchestrator/evidence_gatherer.py),
  assembly in [`orchestrator/routes/pipelines/_criteria.py`](../../orchestrator/routes/pipelines/_criteria.py),
  cost logging in [`config/litellm/cost_callback.py`](../../config/litellm/cost_callback.py).
- Reference design: Claude Code's `/review` / `/code-review` skill (see issue #3523
  comments for the verbatim prompt bodies).
- Related: [Conditional ACK](conditional-ack.md) (advisory-only findings route
  through the conditional-ACK obligation path),
  [Concurrent Execution](../guides/concurrent-execution.md),
  [Reviewer Sync](../../shared/prompts/REVIEWER-SYNC.md).

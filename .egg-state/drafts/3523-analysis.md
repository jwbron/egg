# Analysis: Review quality — structured findings, verification ladder, risk router, shared-evidence prefix (#3523)

## Goal

Raise the signal and lower the cost of egg's multi-agent review machinery through five
changes to how reviewers investigate, how their verdicts are represented, and how the
review graph is provisioned per slice. The issue is fully specified and prescriptive; this
analysis grounds each item in the live codebase, fixes scope boundaries, and hands the plan
phase concrete seams, sequencing, and acceptance criteria. **No eval/benchmark harness and
no human-feedback loop are in scope** (single-operator deployment; operator judges quality
directly).

## Existing machinery (verified on `egg/issue-3523-refiner/work` @ 871f16785)

The issue's premise — "the right skeleton already exists" — checks out. Every referenced
seam is present:

| Seam | File | Shape |
|---|---|---|
| Asymmetric review graph | `orchestrator/review_graph.py` (428 L) | `ReviewEdge(reviewer_role, producer_role, criticality)`, `ReviewGraph.reviewers_for()/producers_for()`; `ReviewCriticality.CRITICAL` blocks consensus |
| Deterministic verdict | `orchestrator/approval_matrix.py` (650 L) | `ApprovalMatrix` seeds entries per edge; `ApprovalEntry.reason` is today a free-form NACK string; conditional-ACK obligation fields already exist (`#1998`) |
| Specialist lenses | `shared/prompts/*-criteria.md` | `code-review-criteria.md` (110 L), `security-review-criteria.md` (254 L), `concurrency-review-criteria.md` (151 L), `agent-design-criteria.md`, `contract-review-criteria.md` |
| Verdict wrapper | `orchestrator/consensus_wrapper.py` | injects `EGG_EVENT_ACTION ∈ propose\|ack\|nack`; `build_event_pump_wrapped_command(effort=…)` already threads `--effort` per role |
| Effort plumbing | `orchestrator/agent_model_resolution.py` (705 L) | resolves per-role model/effort; no risk taxonomy drives it today |
| Message-boundary schema precedent | `orchestrator/attestation_schemas.py` (522 L), `shared/egg_contracts/` | validated contracts at the wire boundary — the home item 1 names for the finding schema |
| Staged-flag precedent | `orchestrator/slice_green_gate.py` (830 L) | `green_gate_mode() -> Literal["off","log","on"]`; env-resolved, **unknown value fails to `off`**, `log` records would-have-been behavior. This is the exact convention every behavior-shifting piece here must copy |
| Cacheable-prefix precedent | `orchestrator/routes/event_prompt/` | protected root, tail-position memory, prefix-budget caps — item 5 extends this discipline across sibling reviewers |
| Conditional ACK | `docs/reference/conditional-ack.md` (129 L) | advisory-obligation path item 1 reuses for advisory-only findings |

## Reference design: Claude Code's built-in `/review` skill (operator directive)

Items 2, 3, and 4 must **mirror the structure of Claude Code's built-in `/review` skill**
(extracted verbatim from the Claude Code 2.1.201 binary, validated byte-identical against a
live session) rather than reinvent it, so egg's review machinery stays generically useful and
interoperable. Treat this skill as the shared design vocabulary — angles, tiers, stances, caps,
3-state verify. Full verbatim text of all four levels will be supplied to the plan/implement
phases; the mapping the plan phase must honor:

- **Effort-level ladder ⇒ item 4 risk tiers.** Four self-contained prompt bodies selected by
  the invoker (here: the deterministic router). Per tier: **low** — 1 diff pass, no verify,
  hunk-only, no subagents, findings cap 4, test files skipped, explicitly forbidden from
  flagging outside the hunk (this *is* the issue's "docs-only ⇒ minimal graph"; a genuinely
  different design, not a smaller fan-out); **medium** — 3+5 angles × 6 candidates, 1-vote
  verify, precision stance ("every finding one a maintainer would act on"), cap 8, test files
  in scope; **high** — same fan-out, recall-biased verify ("catching real bugs matters more
  than avoiding false positives"), cap 10; **xhigh/max** — 5+5 angles × 8, recall ("a missed
  bug ships"), cap 15, plus a gap sweep.
- **Finder angles ⇒ item 3 method-angle procedures.** Correctness: (A) line-by-line diff scan
  reading each hunk's enclosing function, asking per line "what input/state/timing/platform
  makes this wrong"; (B) removed-behavior auditor; (C) cross-file tracer (grep callers + check
  callees); (D, xhigh) language-pitfall specialist; (E, xhigh) wrapper/proxy correctness.
  Cleanup: reuse, simplification, efficiency, altitude (special-casing shared infra ⇒ the fix
  isn't deep enough), conventions (quote-the-rule). Load-bearing rule: **"pass every candidate
  with a nameable failure scenario through; finders that silently drop half-believed candidates
  bypass verify and are the dominant cause of misses."**
- **Verify ladder ⇒ item 2.** Exactly CONFIRMED / PLAUSIBLE / REFUTED with the same evidence
  duties; verify **stance scales with tier**: medium = precision; high = "PLAUSIBLE by default,
  REFUTED only when constructible from the code"; xhigh = "a single non-REFUTED vote carries
  the finding; do NOT drop on uncertainty."
- **Finding schema ⇒ item 1.** file, line, one-sentence summary, required concrete
  `failure_scenario` ("concrete inputs/state → wrong output/crash"). Cleanup/altitude/convention
  findings use the same shape with the concrete cost in `failure_scenario` instead of a crash.
  **Correctness always outranks cleanup when a cap forces a cut.** Ranked most-severe first;
  empty result is an explicit `[]`.
- **xhigh gap sweep.** One extra finder receiving the verified list, hunting ONLY for defects
  not already on it (moved/extracted code that dropped a guard, second-tier footguns,
  setup/teardown asymmetry, flipped config defaults). "If nothing new, return an empty sweep;
  do not pad."

## The five changes — scope, seams, acceptance direction

### Item 1 — Structured findings in BRC verdicts; verdict computed from findings
- **What.** Replace the prose-only NACK with a versioned finding schema carried in the
  verdict payload. Per-finding fields (all from the issue, verbatim): `id`, `role`, anchor
  (path + line range, or slice-level), `summary`, **`failure_scenario` (required — a finding
  without one cannot block)**, `severity ∈ {blocking, advisory}`, `confidence ∈ {high,
  medium, low}`, quoted evidence, optional suggested patch, optional pre-merge obligation.
- **Determinism boundary moves.** Reviewer emits findings; **orchestrator-side code computes
  the edge verdict**: any blocking finding ⇒ NACK; advisory-only ⇒ ACK-with-obligations (the
  existing conditional-ACK path); empty ⇒ ACK. Models own judgment (what to flag, severity,
  confidence, all prose); code owns mechanics (dedup, verdict, rendering the NACK reason the
  producer sees).
- **Convergence as signal.** Findings from different lenses naming the same causal mechanism
  merge into one finding carrying N independent producers; this raises confidence and is
  surfaced to the producer and to HITL on escalation. Today that information is discarded.
- **Seams.** Schema in `shared/egg_contracts/` (validated at the boundary like
  `attestation_schemas.py`); versioned, evolved additively. Verdict computation replaces the
  free-form `ApprovalEntry.reason` assignment path in `approval_matrix.py` /
  `consensus_wrapper.py`. Advisory-only routes through the existing conditional-ACK obligation
  fields.
- **Acceptance.** A blocking finding without a `failure_scenario` cannot produce a NACK (it
  degrades to advisory). Verdict is a pure function of findings, unit-tested. Merged findings
  record ≥2 producers. Existing prose NACK path remains until item 1's schema is on.

### Item 2 — Three-state verification ladder in reviewer prompts
- **What.** Add a CONFIRMED / PLAUSIBLE / REFUTED ladder with symmetric evidence duties to the
  shared criteria. CONFIRMED ⇒ may block; PLAUSIBLE ⇒ advisory / pre-merge obligation, never
  blocks; only REFUTED drops silently (must quote the guard that breaks the causal chain).
  Companion rules: **"blocking must reproduce"** and **"drop only the refuted; downgrade the
  unconfirmed."**
- **Empirical scratch checks.** Explicitly permit cheap, read-only experiments in a scratch
  directory (never mutate the checkout, never the network) to confirm/refute a claim. Bound
  with a **per-finding tool-call cap enforced in the wrapper, not the prompt, where feasible**
  (`consensus_wrapper.py`); no such cap exists today.
- **Pre-existing-defect nuance.** A pre-existing defect still CONFIRMS when the diff materially
  amplifies its consequence, and the finding must say so — sharpening the existing
  "do not dismiss as not-a-regression" rule in `code-review-criteria.md`.
- **Split.** The *prompt-level* rules are prose-only (zero-regret, sequenced first). The
  *tool-call cap* is a wrapper change (couples to item 1's finding boundary).
- **Acceptance.** Criteria files carry the ladder + both companion rules + the amplified-defect
  nuance. Wrapper enforces a configurable per-finding tool-call cap (or documents why a given
  reviewer cannot be capped).

### Item 3 — Method-angle procedures in the code reviewer prompt
- **What.** Add named *how-to-look* procedures to `shared/prompts/code-review-criteria.md`
  (one file, zero new agents): **line-by-line scan** (per changed line, what input/state/
  timing/platform makes it wrong), **removed-behavior audit** (per deleted/replaced line, name
  the invariant it enforced, find where the new code re-establishes it — else a finding),
  **cross-file tracer** (grep callers for broken call sites; check callees for unsafe parallel
  changes in-slice), **quote-the-rule discipline** (flag a convention violation only when the
  exact written rule and the exact violating line can both be quoted).
- **Sequenced first** with item 2's prompt rules: prompt-only, no orchestrator change.
- **Acceptance.** All four procedures present and named in the criteria file; removed-behavior
  audit explicitly covers deletions (the file says nothing about deletions today).

### Item 4 — Deterministic risk router in front of the review graph
- **What.** A per-repo, path/glob risk config plus a **deterministic (plain-code, non-model)**
  router that, per slice: (a) **gates lenses** — concurrency lens only on async/queue-handler
  paths, security lens always on auth/session/input-boundary paths, docs-only ⇒ minimal graph;
  (b) **sets a risk tier scaling reasoning effort** via the existing `--effort` plumbing
  (`agent_model_resolution.py` → `consensus_wrapper.build_event_pump_wrapped_command`); (c)
  **optionally scales stance** (precision-first on trivial tiers, recall-first on high tiers) —
  one prompt conditional, `log` mode first.
- **Cost is a first-class output.** Per-wave token cost is a primary success criterion
  alongside review quality (operator directive). The router should **default low-risk slices
  to lower tiers aggressively** — mirroring the `low`/`medium` designs above rather than always
  running the full graph — so cost tracks risk. This is the dominant cost lever in the issue and
  the precondition for affording item 2's deeper investigation on the slices that deserve it.
- **Floor rules (hard).** A slice matching no config runs the **full graph with a loud
  warning** (missing config must never mean *less* review). A floor tier guarantees a misrouted
  risky slice still gets a real review. Cost-cutting never overrides these floors.
- **Seams.** New config + router module feeding `review_graph.py` edge selection and the effort
  arg already threaded through the wrapper. Ship behind the `off→log→on` flag; `log` records the
  would-have-been graph/tier into BRC artifacts.
- **Acceptance.** Router is pure/deterministic and unit-tested; no-match ⇒ full graph + warning;
  security lens is never gated off auth/input-boundary paths; `log` mode changes no real
  behavior, only records.

### Item 5 — Shared-evidence prompt prefix (the cost bet)
- **What.** An unprivileged **evidence gatherer** (read-only, no GitHub access, casts no
  verdict, posts nothing) assembles a per-slice evidence pack: the diff, changed files with
  enclosing context, caller/callee lists for changed symbols, verified environment facts.
  Reviewer prompts are assembled as `[identical system prefix][identical evidence pack][one
  lens instruction at tail]`, fanned out same-turn/same-model so the prompt cache stays warm.
- **Hard rule: evidence, never conclusions.** No hypotheses, no "areas of concern," no ordering
  by importance; the pack is ordered mechanically (path order) because emphasis is covert
  anchoring — which would destroy item 1's convergence-as-signal.
- **Each reviewer still dives independently** from the shared base (its own greps, traces,
  scratch checks). The pack removes redundant ramp-up, not investigation.
- **Independence guardrails (non-negotiable).** The tester and any *verifier of findings* stay
  cold-start (a verifier must not inherit the context that produced the claim). Delphi
  redaction is unchanged: the pack carries repo facts, **never the producer's self-assessment**.
- **Named risk carried forward.** Adversarial diff content now flows through one gatherer into
  every reviewer's prefix; the existing untrusted-input posture applies — pack content is
  material under review, never instructions.
- **Sequenced last**, behind its own flag. This is *the cost bet*: prompt caching prices cached
  reads at ~1/10 of standard input, so measured **cache-hit rate and per-wave cost in `log` mode
  are an explicit acceptance criterion, not an afterthought** (operator directive). Measure before
  enabling; gateway/LiteLLM cost logging already captures per-session cache stats. Extends the
  `orchestrator/routes/event_prompt/` cacheable-prefix discipline across sibling reviewers.
- **Acceptance.** Gatherer has no verdict/post/GitHub capability; pack is byte-identical across
  a wave and mechanically ordered; tester + finding-verifier remain cold-start; Delphi redaction
  unchanged; enable-gated behind a flag; **`log` mode records cache-hit rate and per-wave cost,
  and a measured net cost reduction is required before flipping to `on`.**

## Sequencing & slice structure (from the issue, binding)

1. **Item 3 + item 2's prompt-level rules** — prompt-only, zero-regret, no orchestrator change.
2. **Item 1** — the structural investment the rest hangs off (schema, computed edge verdict,
   dedup/convergence). Item 2's wrapper-level tool-call cap lands here (it couples to the finding
   boundary).
3. **Item 4** — router config + lens gating + effort tiers, `log` mode first.
4. **Item 5** — behind its own flag, last: the router decides which waves are large enough for the
   prefix to pay; item 1's schema makes convergence measurable so anchoring regressions are visible.

This yields a natural linear-ish slice chain 1→2→3→4 (item 3 and 2-prompt independent of item 1;
item 4 and 5 depend on item 1). The plan phase (architect) owns exact slice boundaries.

## Cross-cutting requirements

- **Staged-flag discipline.** Every behavior-shifting piece (item 1 verdict computation, item 4
  router, item 5 prefix; item 2's tool-call cap) ships behind the `off→log→on` convention modeled
  on `slice_green_gate.green_gate_mode()`: env-resolved, unknown value fails safe to `off`, `log`
  records would-have-been behavior into existing BRC artifacts. Pure prompt-text additions (item 3,
  item 2 ladder prose) are additive and need no flag.
- **Additive, versioned contracts.** The finding schema is versioned and evolved additively; the
  legacy prose-NACK path stays available until the schema is `on`.
- **Fail-safe defaults.** No-match router ⇒ full graph + loud warning; a finding missing a
  `failure_scenario` degrades to advisory rather than erroring; a flag typo degrades to `off`.

## Risks

- **Determinism-boundary regression (item 1).** Moving verdict computation server-side must not
  change today's ACK/NACK outcomes while the flag is `off`/`log`. Mitigation: verdict is a pure,
  unit-tested function of findings; `log` mode diffs computed-vs-legacy verdict without acting.
- **Router silently under-reviewing (item 4).** A glob gap could drop a critical lens. Mitigation:
  floor tier + no-match-runs-full-graph + loud warning + `log`-first rollout; security lens is
  structurally un-gatable on auth/input-boundary paths.
- **Anchoring via the shared pack (item 5).** An editorializing gatherer collapses convergence-as-
  signal. Mitigation: hard "evidence, never conclusions" rule, mechanical path ordering, cold-start
  tester/verifier, unchanged Delphi redaction.
- **Prompt-cache fragility (item 5).** Any non-identical byte in the prefix voids the cache win.
  Mitigation: assemble prefix deterministically; measure real hit-rate in `log` mode before `on`.
- **Untrusted-input surface concentration (item 5).** One gatherer funnels adversarial diff text to
  all lenses. Mitigation: pack is data-not-instructions per the existing untrusted-input posture.

## Out of scope (binding)

- Eval / benchmark harness (operator judges quality directly).
- Human-feedback learning loops (no human PR review in this deployment).

## Acceptance criteria (direction for the plan phase)

1. Versioned finding schema in `shared/egg_contracts/`, boundary-validated; `failure_scenario`
   required for a blocking finding; verdict computed deterministically server-side (blocking⇒NACK,
   advisory-only⇒conditional-ACK, empty⇒ACK); mechanism dedup attaches N producers.
2. Shared criteria carry the CONFIRMED/PLAUSIBLE/REFUTED ladder, "blocking must reproduce",
   "drop only refuted / downgrade unconfirmed", the amplified-pre-existing-defect nuance, and a
   scratch-check permission; a per-finding tool-call cap is enforced in the wrapper where feasible.
3. `code-review-criteria.md` names the four method-angle procedures, including deletion audit.
4. Deterministic risk router + per-repo config gates lenses and sets effort tiers via the existing
   `--effort` plumbing; no-match⇒full graph+warning; floor tier guaranteed; `log`-first.
5. Read-only evidence gatherer + byte-identical, mechanically-ordered shared prefix; tester and
   finding-verifier stay cold-start; Delphi redaction unchanged; flag-gated; `log` mode records
   cache-hit rate and per-wave cost, and a measured net cost reduction gates the flip to `on`.
6. Every behavior-shifting piece rides the `off→log→on` convention and fails safe to `off`.
7. **Cost is a first-class success criterion:** the router defaults low-risk slices to lower tiers
   (low/medium designs) rather than always running the full graph, and per-wave token cost is
   measured and reported for items 4 and 5 in `log` mode.
8. Reviewer prompts (items 2–4) mirror the Claude Code `/review` skill vocabulary (tier ladder,
   finder angles, 3-state verify, finding schema, xhigh gap sweep) so egg's machinery stays
   interoperable with it rather than bespoke.

## Open decisions

None registered. The issue is a complete, prescriptive operator directive: it fixes the five
changes, their per-field schema, the sequencing, the staged-flag rollout, the floor/independence
guardrails, and the out-of-scope boundary. The remaining open choices (exact slice cut-points,
schema field encodings, router config format, gatherer packaging) are implementation-level and
belong to the architect in the plan phase, not to operator HITL. If the plan phase surfaces a
genuine scope fork (e.g. the shared-prefix fork point proving too shallow), it will be raised as a
plan-phase decision then.

# BRC Context Discipline

> A context discipline for the stateless **event-pump** BRC agents: a small,
> stable, deterministic **protected root** that stays resident; the bulk
> history moved to a **queryable environment** pulled on demand; and the
> session **bounded by a proactive deterministic reseed** at a token threshold
> that pre-empts Claude Code's lossy auto-compaction. Introduced by
> [#3200](https://github.com/jwbron/egg/issues/3200).

## What problem this solves

Each BRC event is handled by a one-shot `python3 -m egg_agent` process
(`orchestrator/consensus_wrapper.py`): the wrapper polls
`egg-orch brc next-action`, spawns the agent to handle a single event
(propose / ACK / NACK / confirm), and re-invokes it for the next event. To
preserve continuity *across* those invocations the agent's Claude Code session
is resumed by `session_id` rather than cold-started each time.

That resume is where context accumulates. Every re-invocation re-sends the
bulk a review needs — the per-producer `git log A..HEAD --not origin/base -p`
delta and the distilled BRC memory excerpt — and the resumed session retains
it. As the window fills, two things go wrong:

- **The lossy compaction wall.** Claude Code auto-compacts at ~95% of the
  window. That compaction is a *silent, lossy self-summary* that drops exactly
  the anchors BRC depends on — the per-producer last-reviewed SHA, the open
  NACK obligations, conditional-ACK conditions. `shared/egg_anchor/` exists as
  a post-compaction recovery band-aid; the discipline here removes the need to
  rely on it.
- **Context rot.** *Below* the wall, a near-full window degrades judgment
  before any compaction fires.

### Corrected premise

The original framing — "the Agent SDK does not auto-compact, so the window is a
hard failure" — is **false**. `orchestrator/agent_model_resolution.py` is a
Claude Code compaction-profile system: CC auto-compacts at ~95% of the window,
`DISABLE_COMPACT` is never set, and sub-1M models withhold the `[1m]` alias so
CC's 200K default compacts *safely below* their real backend limit. The real
hazard is therefore not a crash but the silent, lossy compaction described
above. The discipline pre-empts that compaction with a *deterministic* reseed
the harness controls.

## The three parts

### 1. Protected root — small, deterministic, resident

`shared/egg_anchor/protected_root.py` renders a byte-stable root in a fixed
section order:

| Section | Source |
|---------|--------|
| (a) Role contract | the role's non-negotiable behavioural spec |
| (b) Task anchor | `compose_task_description` ([#3163](https://github.com/jwbron/egg/issues/3163)) — the operator's submit-time task statement + binding directives |
| (c) BRC anchors ([#3189](https://github.com/jwbron/egg/issues/3189)) | mechanically derived: last-reviewed SHA per producer, latest verdict per reviewer→producer edge, open NACKs, conditional-ACK obligations |
| (d) Queryable-environment pointers | the JIT-pull recipe + served-read handles that *replace* the inlined bulk (optional; omitted when not wired) |
| (e) Non-negotiable directives | |

**Byte stability is the load-bearing property.** Every keyed collection is
sorted, list counts are bounded by `RootCaps`, each section is hard-capped, and
no timestamps or sequence numbers enter the output. Identical input → identical
bytes, which makes the root (1) a cacheable prompt prefix for warm resume and
(2) a deterministic reseed source — re-rendering the root after a reseed yields
the same bytes.

**Section (c) is authoritative and never agent-authored.** The
`BRCDerivedAnchors` are derived deterministically from the BRC message record
(`shared/egg_anchor/brc_derive.py`), so the anchor layer cannot drift from what
actually happened on the bus. Directive salience becomes a *structural*
property of low utilization rather than something prompt-engineered.

### 2. Queryable environment — the bulk, pulled just-in-time

`shared/egg_agent/queryable_env.py` renders **pointers**, not bulk. Instead of
inlining the diff and the memory excerpt, the root carries:

- the exact `git log <last_reviewed_sha>..<proposal_sha> --not origin/<base> -p`
  recipe per producer (scoped by the #3189 anchors), rendered verbatim so the
  agent can audit the scope before pulling; and
- the served-read handles — `mcp__brc__read_peer_artifact` and the live
  `GET /<pipeline_id>/brc-transcript` route
  ([#3076](https://github.com/jwbron/egg/issues/3076) /
  [#3077](https://github.com/jwbron/egg/issues/3077)).

The agent runs the recipe / calls the tools only for the producer(s) the
current event actually names, so the bulk bytes never enter the resident prompt
at compose time.

> **The honest limit.** JIT pull does **not** bound the context window. A slice
> the agent pulls stays resident until the next reseed/compaction, exactly like
> the inlined bulk would have. What pull buys is a *lower resident root cost*
> (pointers, not diffs) and *re-pull-ability*: when the reseed discards
> accumulated history and re-seeds from the protected root, the pointers
> survive and the bulk can be pulled again. **The reseed bounds the window; the
> pull makes the reseed re-pull-able.** This invariant is recorded verbatim in
> `QUERYABLE_ENV_HONEST_LIMIT` and rendered into the prompt so neither the agent
> nor a maintainer can miss it.

**Enrichment is a claim, not ground truth.** The #3188 agent-authored
enrichment (BRC-memory prose, per-producer assessment) moves into the queryable
environment SHA-stamped with the proposal commit it was authored against. When
the producer re-proposes, the current proposal SHA advances past the stamp and
`enrichment_is_stale()` marks the claim stale so it is re-verified against the
fresh `git log` delta rather than trusted. The check is fail-safe: an unstamped
claim, or one with no current SHA to compare against, is treated as stale. The
deterministic #3189 layer + the git-log delta stay authoritative.

### 3. The bound — proactive deterministic reseed

At each re-invocation the wrapper compares the resumed session's cumulative
**window occupancy** against a threshold and chooses (`shared/egg_agent/reseed.py`):

- **occupancy known and `< threshold` → RESUME** the cached session, reusing the
  warm context and its >90% root cache.
- **occupancy `>= threshold` → RESEED**: do *not* resume; start a fresh session
  seeded only from the protected root and let the bulk be re-pulled
  just-in-time. This discards accumulated history *before* CC's ~95% lossy
  compaction would fire.

```
threshold = min(400_000, 0.80 × real_backend_window)
```

(`orchestrator/agent_model_resolution.reseed_threshold`). The `400_000` floor
is a context-rot/cost ceiling — an initial knob to tune, not derived. The
`0.80` margin pre-empts CC's ~95% compaction.

**The margin is computed against the REAL backend window, never the `[1m]`
alias.** Computing 80% of the `[1m]`-implied 1M for a model whose real backend
is, e.g., a 128K-class route is the *mis-trigger bug*; resolving against the
real window avoids it. Worked examples: `opus[1m]` → 400k (the floor caps
0.80×1M); the 200K profile → 160k; a 128K-class backend → ~102k.

**Bias to reseed on any uncertainty.** A reseed is cheap and safe — it only
forfeits recency, never the anchors in the protected root — whereas a wrong
*resume* can carry a near-full window into a lossy compaction that drops the BRC
anchors. So every ambiguous case collapses to a reseed, never a "resume below
threshold":

- no warm session (first event, expired session, consensus reset, pod death);
- unknown / `None` occupancy (non-Claude / sub-200K LiteLLM routes whose SDK
  usage may be partial or absent);
- no resolvable threshold;
- resume disabled (the staged-rollout default).

Reseed is assumed to fire *rarely* — an assumption the measurement surfaces
(below) exist to confirm. Within-a-single-event growth is out of scope for this
gate; it is bounded by the tool-output caps (`tool_output_cap.py`) and the gated
recursion escalation, not by the re-invocation threshold.

## Prerequisite: token-occupancy capture

The reseed trigger needs a number the SDK previously discarded.
`AgentResult` (`shared/egg_agent/result.py`) historically exposed
`num_turns` / `cost_usd` / `duration_ms` / `session_id` but no token counts;
`client.py` dropped the `ResultMessage.usage` block. The discipline captures
cumulative session **window occupancy** into `AgentResult`:

```
occupancy = cache_read + cache_creation + input
```

It must be window *occupancy*, **not** billed/effective input. Under resume the
cache-read tokens are the bulk and are exactly what counts toward the window;
capturing only uncached input would make the trigger fire far too late. The
capture is defensive — absent/partial `usage` yields `None`, never an exception,
and `None` biases the gate to a safe reseed.

## Session-resume substrate

Because each event is a fresh process, the `session_id` (and the occupancy the
gate reads) must survive *between* processes. `shared/egg_agent/session.py`
persists a small `SessionState` record (`session_id` + `window_occupancy`) to a
JSON file and reads it back:

- **Substrate only — no decision.** Reading a record never implies "resume";
  the occupancy-vs-threshold gate in `reseed.py` owns that decision, kept out of
  the substrate so the substrate can ship dark.
- **Cold-start fallback — never a hard failure.** A missing/empty/corrupt state
  file, an unset path, or a record without a usable `session_id` all resolve to
  `None` (cold-start from the protected root). Writes are atomic
  (temp file + `os.replace`) and equally defensive: a persistence failure
  returns `False` and never crashes the run it is bookkeeping for.

Mid-phase restarts additionally need the BRC *message record* to survive so a
reseeded session can re-pull it and re-derive the #3189 anchors
([#3200](https://github.com/jwbron/egg/issues/3200) slice-7); `_write_brc_history`
historically persisted only at phase transitions.

## Feature flags

The discipline ships dark and is rolled out behind explicit, default-OFF
switches, each read in exactly one place:

| Env var | Read in | Effect |
|---------|---------|--------|
| `EGG_SESSION_RESUME` | `egg_agent.session.session_resume_enabled` | Master opt-in for warm resume. OFF (default) → a passed-in `session_id` is ignored and the agent cold-starts; the substrate is inert and the agent path is byte-for-byte the legacy cold-start. |
| `EGG_SESSION_STATE_FILE` | `egg_agent.session.resolve_session_state_path` | Location of the cross-invocation session-state file. Unset → the round-trip is a no-op (substrate stays inert). |
| `EGG_RESEED_THRESHOLD` | `egg_agent.reseed.resolve_reseed_threshold` | Cross-boundary integer override of the reseed threshold. The sandbox runs with `orchestrator` off `PYTHONPATH`, so the orchestrator side may compute `reseed_threshold(model)` and export the integer here; otherwise the gate imports the orchestrator helper when available, and falls back to `None` (safe reseed) when neither yields a value. |

**Slice-9 master flag.** The terminal slice
([#3200](https://github.com/jwbron/egg/issues/3200) slice-9) introduces a
single master context-discipline flag that gates the whole discipline
(protected-root / queryable-environment split + threshold reseed + JIT pull) for
**every** event-pump role — producers and reviewers alike — each inlining only
its own contract and its own anchors via the role-parameterized
`render_protected_root`. The flag is a **kill-switch**, not the preserved
fallback build:

- **ON** → every role takes the new path; the mechanism is uniform and only the
  *content* of the root differs by role.
- **OFF (and default during rollout)** → today's full-context inlining path,
  byte-for-byte unchanged; the OFF path retains no dependency on the new code.

The flag drives the existing `compose_event_prompt(..., jit_pull=...)` toggle in
`orchestrator/routes/event_prompt.py`, whose `jit_pull=False` default already
renders the legacy inline path byte-for-byte. It is read in one place; no role
hard-codes the new path.

## Measurement (emit-only)

The discipline is "build + measure": slice-10 emits per-event measurement
surfaces from the occupancy field and the reseed decisions — window occupancy
per event, peak utilization under resume (the **primary** metric), single-event
working set vs the real window (the recursion-escalation signal), reseed
frequency per phase (the cost case rests on this being low), root-cache hit
rate, and tokens/event. These are **emit-only**: no control flow branches on the
measured values, no A/B harness, nothing gated. The go/no-go is a later
human-read pass against the central hypothesis: *resident-root + JIT-pull keeps
peak context utilization low under resume, while review quality matches
full-inline.*

## Gated escalation: sub-agent recursion (not default)

The only mechanism that delivers a true "window never fills" guarantee is
recursion — a reviewer spawns sub-agent calls over diff/transcript slices, the
bulk lands in throwaway sub-contexts, and only distilled findings return. It is
**deliberately not the default**: the per-event working set fits the window
today (~50–130k/call), the accumulation problem is cross-event drift (which the
threshold reseed solves), and recursion forfeits the >90% root cache, adds
latency, and imports decomposition-error risk. It is retained as a *gated
escalation* for the within-event-too-big tail — adopt it when a single event's
working set routinely approaches the real backend window (e.g. sub-200K models
become the default route, or per-event review scope grows). The context
discipline described here is a strict prerequisite of recursion.

## Constraints carried from the design

- **Provider stickiness (LiteLLM route).** A provider bounce is amplified under
  resume; the route is single-pinned.
- **Deterministic rendering.** The root must render to stable bytes (sorted,
  bounded, hard per-section caps) for a stable cacheable prefix.
- **Agent-authored = claims, not ground truth.** SHA-stamp enrichment so the
  delta can invalidate stale claims; the deterministic layer + git-log delta
  stay authoritative.
- **Persistence timing.** Mid-phase restarts need the message record to survive
  (live Redis stream across restart, or a history-persist step on the restart
  route).

## Source map

| Concern | Module |
|---------|--------|
| Protected-root renderer | `shared/egg_anchor/protected_root.py` |
| #3189 anchor derivation | `shared/egg_anchor/brc_derive.py`, `shared/egg_anchor/models.py` |
| Queryable-environment pointers + enrichment staleness | `shared/egg_agent/queryable_env.py` |
| Resume-vs-reseed decision gate | `shared/egg_agent/reseed.py` |
| Session-resume substrate | `shared/egg_agent/session.py` |
| Token-occupancy capture | `shared/egg_agent/result.py`, `shared/egg_agent/client.py` |
| Real-window + threshold resolution | `orchestrator/agent_model_resolution.py` |
| Event-prompt inline-vs-pointer toggle | `orchestrator/routes/event_prompt.py` |

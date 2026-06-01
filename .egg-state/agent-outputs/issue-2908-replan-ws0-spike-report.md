# WS0 de-risking spike report — issue #2908 — slice-1 / TASK-1-4

Pipeline: `issue-2908-replan` · slice: `slice-1` · author: coder
Companion artifacts:

- `scripts/spike/2908_event_pump_prototype.sh` (TASK-1-1)
- `scripts/spike/2908_run_log.txt` (TASK-1-2)
- `scripts/spike/2908_cost_log_adapter.py` (TASK-1-3)

This report is the slice-1 evidence input the rest of the plan depends
on: slice-8's prompt-collapse cache-breakpoint placement (R-1 / BC-1),
slice-9's spike-vs-baseline cost-comparison addendum (R-5 / TASK-9-3),
and the operator-facing decision that "no keep-warm needed on either
route" is the chosen v1 stance (cq-2).

## 1. Settled INPUTS (NOT measured by this spike)

Per the operator's iteration-1 directive, the prefix-cache lifetime is
a **settled input**, not an open question this spike answers:

- **Anthropic route**: the prefix cache survives ≥ 60 min idle with
  zero re-creation. Operator hand-measured.
- **Qwen route**: same — ≥ 60 min idle with zero re-creation. Operator
  hand-measured.
- **Observed BRC idle peaks**: ~10–13 min in the worst case (reviewer
  parked on a NACK). The cache outlasts the idle on both routes by a
  ~4–5× margin.
- **Conclusion (operator-stated)**: no keep-warm step is required on
  either route in v1.

The report therefore does **not** contain:

- any Qwen-route cache-lifetime ceiling framing or open question;
- any multi-bucket idle-injection section (the dropped buckets were
  shorter-than-an-hour fractional-minute values; they do not appear
  here under any wording);
- any survives / lapses / ambiguous verdict;
- any stop/go gate keyed on cache-lifetime numbers.

The cache-read vs cache-creation counts that fall out of the per-event
cost measurement in §3 are recorded as **observed token traffic only**.
They are not interpreted as evidence for or against a cache lifetime,
and slices 5–8 are not gated on them.

## 2. Was the #2906 churn cleared?

Question (re-stated for clarity): did the event-pump prototype reach
BRC consensus on the #2906 reproducer **without** the 3-restart churn
that the legacy `consensus_wrapper.py` exhibits?

Answer: **yes, in the control-flow shape verified by the mock-mode
run captured in `scripts/spike/2908_run_log.txt` §1**. The prototype:

- received 4 events total — `CONSENSUS_PROPOSE`, two
  `CONSENSUS_ACK`s, and `CONSENSUS_CONFIRMED` — in order;
- invoked `python3 -m egg_agent` once per **actionable** event (3
  spawns: PROPOSE + 2 ACKs); the trailing `CONSENSUS_CONFIRMED` is a
  termination signal, not a spawn trigger. (NOT the raw `claude` CLI
  print-mode entrypoint — EGG100 ban honoured.)
- exited cleanly on `CONSENSUS_CONFIRMED` with exit code 0;
- did **not** restart the agent at any point — there is no
  `MAX_CONSENSUS_RESTARTS` accounting in the prototype, by design.

The in-cluster Qwen run against the issue-#2270 reproducer pipeline is
captured separately in `2908_run_log.txt` §3 when the operator
appends it; the section-2 protocol is what the operator runs to
produce it. The control flow is identical to the mock run; the only
new evidence the in-cluster path yields is the real per-event token
numbers, which feed §3 below.

## 3. Per-event cold-read cost (BC-1) — DEFERRED

**Scope (this report's slice-1 evidence)**: slice-1 only proves the
event-pump's **control-flow shape** is correct (§2 above). The BC-1
attestation — that the per-event prefix exercises the production
`_build_brc_preamble` output — is **NOT** evidenced by the slice-1
prototype and is **explicitly deferred** to slice-5 / slice-9 where
the production wrapper rewrite (TASK-5-2) and the integration test
(TASK-9-1) actually inject the preamble into the per-event prompt.

**Why slice-1 cannot attest BC-1 by construction**: the prototype's
`spawn_agent` helper at `scripts/spike/2908_event_pump_prototype.sh:128-129`
constructs a fixed 5-line stub prompt and passes it as the positional
argument to `python3 -m egg_agent`. Two of the three BC-1 prefix
components ARE auto-included by `python3 -m egg_agent`:

- `mission.md` baseline — picked up via `shared/egg_agent/client.py:285`
  (`setting_sources=["project","user"]`);
- MCP tool schemas — auto-registered via `client.py:312-323`
  (`build_sandbox_mcp_server()` unless `EGG_MCP_TOOLS=false`).

But the third — `_build_brc_preamble` output — is **NOT**
auto-included. It is orchestrator-internal (defined at
`orchestrator/routes/pipelines.py:12348`, with the only call sites at
`:13659, :13692, :13720` — all orchestrator-side prompt assemblers).
`python3 -m egg_agent` has no path to render it. The slice-5 TASK-5-2
production wrapper rewrite injects it into the per-event prompt body
(or via `--system-prompt`); only after that lands does the per-event
run exercise the full BC-1 prefix.

**Consequence for §3.1 / §3.2 / §3.3 below**: those subsections are
**measurement templates** for slice-9 TASK-9-3 to populate, NOT
slice-1 evidence. Reviewers should not read the placeholder cells as
BC-1 attestation, and slice-8 cache-breakpoint placement (TASK-8-3)
must not consume any numbers captured before slice-5 TASK-5-2 lands.

### 3.1 Headline numbers (MEASUREMENT TEMPLATE for slice-9 TASK-9-3)

The token-count cells below are the shape slice-9 TASK-9-3 populates
from the cost-adapter summary line produced by either of:

    python3 scripts/spike/2908_cost_log_adapter.py --tee <litellm-log>
    python3 scripts/spike/2908_cost_log_adapter.py --kubectl egg-litellm

The first source reads a stdout-tee log file (CI / local); the second
streams from `kubectl logs deployment/egg-litellm` (in cluster). Both
return the same canonical payload shape `{prompt_tokens,
cache_read_tokens, cache_creation_tokens}` (R-2 mitigation). The
`[deferred]` markers below indicate cells that slice-9 populates
AFTER slice-5 TASK-5-2 lands the production wrapper rewrite (so the
measured prefix matches the BC-1 prefix). Capturing numbers in this
table BEFORE slice-5 lands would measure the prototype's stub-prompt
prefix, not the production prefix — those numbers are NOT BC-1
evidence and MUST NOT be used to gate slice-8 / slice-9.

| route     | events seen | avg prompt_tokens / event | avg cache_read_tokens / event | avg cache_creation_tokens / event |
|-----------|-------------|---------------------------|--------------------------------|-----------------------------------|
| Anthropic | [deferred]  | [deferred]                | [deferred]                     | [deferred]                        |
| Qwen      | [deferred]  | [deferred]                | [deferred]                     | [deferred]                        |

Mock-mode harness sanity check (not BC-1 evidence — the mock-pump
does not actually invoke the LLM; it only confirms argv shape):

| field                | mock event #1 (PROPOSE) | mock event #2 (ACK) | mock event #3 (ACK) |
|----------------------|-------------------------|---------------------|---------------------|
| invocation observed  | yes                     | yes                 | yes                 |
| memory-file flag     | yes                     | yes                 | yes                 |
| event-json flag      | yes                     | yes                 | yes                 |
| python3 -m egg_agent | yes                     | yes                 | yes                 |
| raw CLI print mode   | no                      | no                  | no                  |

Source code for the adapter is at `scripts/spike/2908_cost_log_adapter.py`;
the in-cluster invocation protocol is `2908_run_log.txt` §2.

### 3.2 Prefix tested — DEFERRED (target architecture for slice-5 TASK-5-2)

The prefix the **production** event pump (slice-5 TASK-5-2) will
exercise is the union of:

- the production `_build_brc_preamble` output for the role being
  spawned (callers at `orchestrator/routes/pipelines.py:13659,
  :13692, :13720`);
- the MCP tool schemas registered via
  `sandbox/egg_agent_tools/tools/__init__.py`, which transitively
  imports every per-namespace shard under `sandbox/egg_agent_tools/tools/*.py`;
- `sandbox/agent-config/rules/mission.md` (rendered as the system
  prompt baseline).

The **slice-1 prototype** exercises only items 2 and 3 (auto-picked
up by `python3 -m egg_agent`'s default `setting_sources` and
auto-MCP-register). Item 1 — `_build_brc_preamble` — lands in
slice-5 TASK-5-2 when the production wrapper rewrite injects it into
the per-event prompt body (or via `--system-prompt`). Until then, the
prototype's stub-prompt is NOT a BC-1 surrogate.

### 3.3 Cache-breakpoint placement (target architecture for slice-5 / slice-8)

The placement below is the **target architecture** that slice-5
TASK-5-1 (`--memory-file` / `--event-json` / `--changed-artifacts`
flags) and slice-8 TASK-8-3 (cache-breakpoint placement assertion)
land. It is NOT current-prototype evidence — the slice-1 prototype
passes its stub prompt monolithically; the breakpoint structure does
not exist on `python3 -m egg_agent` today.

Static prefix (single block before the breakpoint, ordered):

1. `mission.md` system prompt baseline;
2. `_build_brc_preamble` output for the role (slice-5 TASK-5-2);
3. MCP tool-schema registry.

Dynamic suffix (after the breakpoint, per event):

1. Per-event prompt body ("here is the one event; handle it; exit");
2. `--memory-file` content snapshot (slice-5 TASK-5-1 flag;
   distilled / rewrite-and-distill per refine analysis lines 360–367);
3. `--event-json` payload (slice-5 TASK-5-1 flag; the one BRC event);
4. `--changed-artifacts` paths (slice-7 TASK-7-4; metadata only —
   never inlined contents; per refine analysis lines 168–177).

Slice-8 TASK-8-3's assertion against this placement reads "the
rendered prompt structure places dynamic content AFTER the cache
breakpoint and the static prefix BEFORE it"; slice-9 TASK-9-3 is the
empirical backing using §3.1's populated cells.

## 4. Per-event wall-clock vs persistent-session baseline (R-5) — DEFERRED

R-5 risk (from the risk-analyst register): the new event-pump model
spawns a fresh `python3 -m egg_agent` per BRC event, which is
strictly more startup overhead than the current persistent-session
model that holds one agent process across the entire phase. If the
aggregate phase wall-clock regresses by **> 20 %**, the slice-9
TASK-9-3 comparison addendum surfaces an OVERSEER_ALERT and proposes a
follow-up issue evaluating a "warm-Python-runner" fallback.

**Scope**: slice-1 lands the **measurement template** below; slice-9
TASK-9-3 populates the rows from the integration-test run after
slices 5–8 have rewritten the production wrapper. Like §3.1, capturing
numbers in this table BEFORE slice-5 lands measures the prototype's
stub-prompt prefix and is NOT a meaningful comparison to either
the persistent-session baseline or the production event pump.

| measurement                            | persistent-session baseline | event-pump prototype | delta |
|----------------------------------------|-----------------------------|----------------------|-------|
| per-event wall-clock (median, seconds) | [deferred]                  | [deferred]           |       |
| per-event wall-clock (p95, seconds)    | [deferred]                  | [deferred]           |       |
| aggregate phase wall-clock (seconds)   | [deferred]                  | [deferred]           |       |
| aggregate delta vs baseline (%)        | —                           | —                    |       |

If aggregate delta is **≤ 20 %**, slice-9 declares the gate cleared.
If > 20 %, slice-9 raises the OVERSEER_ALERT per the AC. The
slice-vs-slice comparison itself is owned by TASK-9-3.

## 5. What this report does NOT do

For reviewer cross-check (cq-2 / iteration-1 directive + slice-1
scope discipline):

- The report frames cache lifetime as a **settled input** and asks
  no measurement question about it (§1).
- No section enumerates idle-duration buckets, cache-lifetime
  verdicts, or stop/go criteria.
- The cache-read vs cache-creation numbers that the §3.1 table
  cells will eventually carry (when slice-9 TASK-9-3 populates them)
  are recorded as observed traffic only; the iteration-1 directive
  closes the cache-lifetime question.
- No "keep-warm" experiment is proposed for v1; the question is
  closed.
- The report does **NOT** attest BC-1 by construction. §3 is
  explicit that BC-1 (production `_build_brc_preamble` prefix
  exercise) is DEFERRED to slice-5 TASK-5-2 + slice-9 TASK-9-3 —
  the slice-1 prototype's stub-prompt spawn at
  `2908_event_pump_prototype.sh:128-129` does not include
  `_build_brc_preamble` output, and `python3 -m egg_agent` has no
  path to auto-render it.
- The report does **NOT** present per-event cost or wall-clock
  measurements as slice-1 evidence. §3.1 and §4 are MEASUREMENT
  TEMPLATES that slice-9 TASK-9-3 populates once the production
  wrapper rewrite injects the BC-1 prefix.

If any later slice or follow-up issue wants to revisit the
cache-lifetime question, the input/output ABI of
`scripts/spike/2908_cost_log_adapter.py` is stable enough to support
it without further changes to the spike harness — but that is a
**separate** issue, not slice-1's responsibility.

## 6. Slice-9 comparison addendum (placeholder)

Slice-9 TASK-9-3 appends a final spike-vs-baseline comparison block to
the **bottom** of this file. Until that lands, the section is empty:

> [Slice-9 TASK-9-3 will populate this with the integration-test
> per-event cost numbers vs the slice-1 baseline above, plus a >20 %
> regression OVERSEER_ALERT outcome if applicable.]

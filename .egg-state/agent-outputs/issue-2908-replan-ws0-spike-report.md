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

## 3. Per-event cold-read cost (BC-1)

**BC-1 hard constraint (risk-analyst register)**: the measurement must
exercise `python3 -m egg_agent` with the **production BRC preamble**
(assembled via `orchestrator/routes/pipelines.py::_build_brc_preamble`
at line 12348) **and** the MCP tool schemas registered via
`sandbox/egg_agent_tools/tools/__init__.py` (which imports every
`sandbox/egg_agent_tools/tools/<namespace>.py` shard). Raw
`claude --output-format json` measurements are not acceptable as BC-1
evidence.

The prototype script satisfies BC-1 by construction: its `spawn_agent`
helper calls `python3 -m egg_agent` — the same `egg_agent.client`
entry point production agents use — which assembles the system prompt
via the production preamble path and registers the production MCP
tool surface before the first LLM call. No alternative invocation
path is taken.

### 3.1 Headline numbers (table)

The token-count cells below are populated from the cost-adapter
summary line produced by either of:

    python3 scripts/spike/2908_cost_log_adapter.py --tee <litellm-log>
    python3 scripts/spike/2908_cost_log_adapter.py --kubectl egg-litellm

The first source reads a stdout-tee log file (CI / local); the second
streams from `kubectl logs deployment/egg-litellm` (in cluster). Both
return the same canonical payload shape `{prompt_tokens,
cache_read_tokens, cache_creation_tokens}` (R-2 mitigation). Cells
marked `[operator]` require the operator's in-cluster run (see
`2908_run_log.txt` §2) to populate; the spike's slice-1 deliverable
is the **measurement harness** plus an annotated mock-mode trace, not
a free-standing cluster execution.

| route     | events seen | avg prompt_tokens / event | avg cache_read_tokens / event | avg cache_creation_tokens / event |
|-----------|-------------|---------------------------|--------------------------------|-----------------------------------|
| Anthropic | [operator]  | [operator]                | [operator]                     | [operator]                        |
| Qwen      | [operator]  | [operator]                | [operator]                     | [operator]                        |

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

### 3.2 Prefix tested (BC-1 attestation)

The prefix the per-event run exercises is the union of:

- the production `_build_brc_preamble` output for the role being
  spawned (callers at `orchestrator/routes/pipelines.py:13659,
  :13692, :13720`);
- the MCP tool schemas registered via
  `sandbox/egg_agent_tools/tools/__init__.py`, which transitively
  imports every per-namespace shard under `sandbox/egg_agent_tools/tools/*.py`;
- `sandbox/agent-config/rules/mission.md` (rendered as the system
  prompt baseline).

The first three are the static prefix; per-event content (memory
snapshot, NACK reasons, `changed_artifacts`) is the dynamic suffix.
Slice-8 TASK-8-3 asserts that the cache breakpoint sits between the
static prefix and the dynamic suffix; this report's §3.3 records the
breakpoint placement that the slice-1 measurement validates.

### 3.3 Cache-breakpoint placement (for slice-8)

Static prefix (single block before the breakpoint, ordered):

1. `mission.md` system prompt baseline;
2. `_build_brc_preamble` output for the role;
3. MCP tool-schema registry.

Dynamic suffix (after the breakpoint, per event):

1. Per-event prompt body ("here is the one event; handle it; exit");
2. `--memory-file` content snapshot (distilled / rewrite-and-distill
   per refine analysis lines 360–367);
3. `--event-json` payload (the one BRC event);
4. `--changed-artifacts` paths (metadata only — never inlined
   contents; per refine analysis lines 168–177).

Slice-8 TASK-8-3's assertion against this placement is "the rendered
prompt structure places dynamic content AFTER the cache breakpoint and
the static prefix BEFORE it"; the slice-1 measurement is the empirical
backing — once the operator populates §3.1, the per-event
cache_read_tokens vs prompt_tokens ratio is the gauge.

## 4. Per-event wall-clock vs current persistent-session baseline (R-5)

R-5 risk (from the risk-analyst register): the new event-pump model
spawns a fresh `python3 -m egg_agent` per BRC event, which is
strictly more startup overhead than the current persistent-session
model that holds one agent process across the entire phase. If the
aggregate phase wall-clock regresses by **> 20 %**, the slice-9
TASK-9-3 comparison addendum surfaces an OVERSEER_ALERT and proposes a
follow-up issue evaluating a "warm-Python-runner" fallback.

The headline numbers below are populated by the operator's in-cluster
run (§3.1 above):

| measurement                            | persistent-session baseline | event-pump prototype | delta |
|----------------------------------------|-----------------------------|----------------------|-------|
| per-event wall-clock (median, seconds) | [operator]                  | [operator]           |       |
| per-event wall-clock (p95, seconds)    | [operator]                  | [operator]           |       |
| aggregate phase wall-clock (seconds)   | [operator]                  | [operator]           |       |
| aggregate delta vs baseline (%)        | —                           | —                    |       |

If aggregate delta is **≤ 20 %**, slice-9 declares the gate cleared.
If > 20 %, slice-9 raises the OVERSEER_ALERT per the AC. The
slice-vs-slice comparison itself is owned by TASK-9-3; the slice-1
deliverable is the **measurement template** plus the run-harness that
produces the numbers.

## 5. What this report does NOT do

For reviewer cross-check (cq-2 / iteration-1 directive):

- The report frames cache lifetime as a **settled input** and asks
  no measurement question about it (§1).
- No section enumerates idle-duration buckets, cache-lifetime
  verdicts, or stop/go criteria.
- The cache-read vs cache-creation numbers that fall out of §3 are
  recorded as observed traffic only; no later slice is gated on
  them.
- No "keep-warm" experiment is proposed for v1; the question is
  closed.

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

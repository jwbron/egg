# Overseer Calibration Corpus & Detector Test Harness

> **Status:** Contract definition for the overseer overhaul ([#2270](https://github.com/jwbron/egg/issues/2270), slice 1).
> This is **deliverable #1** and the regression bedrock every downstream detector plugs into.
> It satisfies acceptance criterion **AC-3**. Slices 4, 7, and 8 have all landed — the corpus xfails for those slices have flipped to strict assertions.

The overseer overhaul replaces a noisy, respawning watcher pod with an
orchestrator-side **detection plane** of deterministic detectors (slices 4, 7, 8 — all delivered).
The single biggest risk in that work is *recalibration regressions* — a detector
that goes quiet on a real fault, or one that floods operators with false alarms.
The **calibration corpus** is how we make that risk testable: a fixed set of
labelled event-stream snapshots that every detector is asserted against, so a
miscalibration shows up as a failing test instead of a 3 a.m. page.

No production detector code changes in slice 1. Slice 1 ships **only** the corpus
fixtures, the harness, and this contract.

---

## 1. The record shape

The corpus is a package of **rows**. Each row is a recorded or synthesized
`EventStreamSnapshot` paired with the verdict a correct detector must produce
over it.

### `EventStreamSnapshot` (the detector input)

A snapshot is the immutable slice of pipeline state a detector sees. It is a pure
data value — no I/O, no clock, no live cluster — so detectors are pure functions
and tests are deterministic. It carries:

| Field group | Contents |
|-------------|----------|
| Running-agent set | The agents currently spawned, **each annotated with its lifecycle owner** (see #3230 note below) |
| BRC consensus matrix | Per-role propose/ACK/NACK/confirm state for the active slice |
| Phase & decision state | Current phase, pending HITL decisions, auto-advance state |
| Container transitions | Recent pod/container state changes (Running → OOMKilled/Evicted, etc.) |
| Gateway counters | Error-rate and repeated-denial counters |
| Cost counters | Token/cost accumulators for budget detectors |

> **Lifecycle-owner annotation (#3230).** The running-agent set records *who
> spawned each agent*. This is the structural fix for the #3230 false stall: a
> producer drafting under **orchestrator-owned** spawning is **not** "a phase with
> 0 running agents." A stall detector that ignores the owner cries wolf; one that
> reads it stays quiet. The corpus encodes both the normal and the bad version of
> this case so the distinction is regression-locked.

### The row (snapshot + label + expectation)

Each row (`CorpusRow`) carries:

| Field | Meaning |
|-------|---------|
| `row_id` | Stable row identifier (e.g. `false_stall_3230__normal`) |
| `snapshot` | The `EventStreamSnapshot` described above |
| `label` | `known_normal` **or** `known_bad` — the row's ground truth |
| `incident` | Human-readable incident name |
| `pins` | The issue / defect ids this row pins |
| `expected` | The expected detector verdict: `None` for known-normal, or the expected `ExpectedFinding` for known-bad |
| `detector_key` | Which detector this row exercises (lets later slices flip their own rows; see §5) |
| `delivered_in_slice` | For known-bad rows, the slice (one of 4/7/8) that delivers the detector; `None` for known-normal |
| `notes` | Why this row exists / what it pins |

The fault class is **not** a standalone row field — it lives on the row's
`expected` finding (`expected.finding_class`) for known-bad rows; known-normal
rows carry no `expected` at all.

### The `Finding` shape (the detector output)

A detector is a pure function `EventStreamSnapshot -> Optional[Finding]`. The
`Finding` it may return is a structural (duck-typed) protocol with these
attributes:

```
Finding{
    finding_class,          # the fault class detected
    severity,               # info | low | medium | high
    evidence,               # the snapshot facts that triggered it
    recommended_action,     # from the bounded corrective vocabulary (slice 6)
    requires_adjudication,  # bool — escalate to the on-demand adjudicator agent?
}
```

The corpus's `expected` label is the narrower `ExpectedFinding`
(`finding_class`, `severity`, `requires_adjudication`); the harness matches a
detector's `Finding` against it structurally via `match_finding`.

`requires_adjudication=True` is the seam to the adjudication plane (slice 4): the
orchestrator spawns a **normal** on-demand OVERSEER agent with the `Finding` +
evidence + snapshot, and consumes its structured verdict. The corpus does not
test the agent; it tests that detectors set this flag correctly.

---

## 2. The AC-3 contract: None-on-normal / Finding-on-bad

This is the whole point of the corpus, and it is exactly two rules:

1. **Known-normal inputs MUST yield `None`.** A detector that returns *any*
   `Finding` on a `known-normal` row has a false positive — it fails the harness.
2. **Known-bad inputs MUST yield the expected `Finding`.** A detector that returns
   `None` (or a `Finding` of the wrong class) on a `known-bad{class}` row has a
   false negative — it fails the harness.

> **Contract for every detector under test:**
> *yield `None` on every `known-normal` row, and the expected `Finding` on every
> `known-bad` row tagged for that detector.*

A "calibration test" for a detector is simply: run it over its corpus rows and
assert rule 1 and rule 2. **Corpus-tested == shippable.** Every §5 detector
(slice 8) and every calibrated fix (slice 7) ships *only* if it passes this
contract — that is the structural guard against a new false-positive flood.

---

## 3. The seed corpus rows

Slice 1 lands these rows. They are the concrete failure modes that motivated the
overhaul; each is encoded in **both** a `known-normal` and a `known-bad` variant
where the distinction is the whole point.

| Row | `finding_class` | Why it exists |
|-----|-----------------|---------------|
| Self-injection loop | `overseer_self_injection` | Overseer mis-reads its own bootstrap as a prompt-injection attack, refuses, respawns, alerts `[high]` each cycle |
| Alert-reflection | `alert_reflection` | An overseer/orchestrator informational alert reflected back as a **binding** operator HITL directive |
| #3230 false stall | `phase_stall` | Producer drafting under **orchestrator-owned** spawn — normal, must NOT fire (this is the lifecycle-owner row) |
| #2242 heartbeat-stall | `heartbeat_stall` | Agent emitting tool calls every 2–3s is alive — must NOT be flagged as stalled |
| #2222/#2224 branch-divergence | `branch_divergence` | Detector must use **ancestor-of-`origin/main` OR patch-id** match, not `(#NNNN)` subject-regex matching |
| #2948 transient kubelet eviction | `container_death` | A transient evict/reschedule must NOT cascade to a producer-permanent-death `FAILED` |

Downstream slices add rows for their own detectors (the full §5 survey in slice 8
was the largest addition — 25 coverage-gap detectors: 24 across 8 modules in
``health_checks/tier1/`` plus ``detect_overseer_self_health`` in
``overseer/self_monitor.py``). The seed set establishes the shape and the contract;
later slices extend, never reshape.

---

## 4. The scoreboard

The harness emits a **scoreboard** (`Scoreboard`): a precision/recall tally over
the whole corpus. `evaluate` runs each row through its resolved detector (falling
back to the null detector for unregistered detectors) and counts each row into
one bucket:

| Bucket | Meaning |
|--------|---------|
| `true_positive` | Known-bad row, detector fired with the correct class |
| `false_positive` | Known-normal row, detector fired (over-fire) |
| `false_negative` | Known-bad row, detector returned `None` / wrong class |
| `true_negative` | Known-normal row, detector correctly silent |
| `undelivered` | Rows whose detector is not yet registered (counted for visibility) |

From these the scoreboard derives `precision` (TP / (TP + FP)) and `recall`
(TP / (TP + FN)). The slice-1 acceptance signal is `false_positive == 0` and
`precision == 1.0` (the baseline null detector never over-fires); with all
slices (4/7/8) delivered, recall is at 1.0 and precision must remain there.
This precision/recall tally is distinct from the per-row pytest pass/`xfail`
bookkeeping in the harness (see §5).

---

## 5. The xfail → strict flip convention (red → green workflow)

Slice 1 defined corpus rows for detectors that slices 4, 7, and 8 had not built
yet. Those rows cannot pass against code that does not exist, so the harness
marks each known-bad row whose detector is unregistered as **`xfail`**
(expected-to-fail). This is deliberate: the corpus is the *spec*, written before
the detector.

The marker is applied **automatically and conditionally** in `_row_param`: a row
is xfailed iff `resolve_detector(row.detector_key) is None` and the row is
known-bad. The xfail is registered with `strict=False` — intentional for slice-1
build-greenness, so the baseline run is green (`10 passed, 6 xfailed`) without
any `xpass` failing the build. Nobody hand-marks individual rows.

The workflow each downstream slice follows is **red → green**:

1. **Red (slice 1).** The row for a future detector exists; because no detector
   resolves for its `detector_key`, the harness marks it `xfail`. It documents the
   exact `known_normal`/`known_bad` behaviour the future detector must satisfy. The
   build stays green because `xfail` is tolerated.
2. **Implement (slice 4/7/8).** The owning slice builds the detector and registers
   it via `register_detector`. The moment it resolves, `_row_param` no longer
   applies the xfail marker — the row becomes a strict assertion automatically (the
   marker "evaporates" because the detector resolves).
3. **Green.** From then on the row is a hard regression gate: any later change that
   breaks the detector fails the build.

> **Convention for downstream slices:** when you land a detector, register it so
> the harness resolves it for the row's `detector_key`; the rows then flip to
> strict on their own in the *same* change. A detector merged without registering
> leaves its rows stuck at `xfail` — treat that as an incomplete slice.

This keeps the corpus honest: every detector the design promises has a row from
day one (red), and every detector that ships is permanently regression-locked
(green). Nothing is "covered" without a strict assertion behind it.

---

## 6. Where it lives & how detectors plug in

- **Fixtures:** the labelled corpus rows live under
  `orchestrator/tests/overseer_calibration/` (`__init__.py`, `corpus.py`,
  `fixtures.json`) as a corpus/test-fixtures package. Snapshots are data — no I/O,
  no clock.
- **Harness:** `orchestrator/tests/test_overseer_calibration.py` — the
  assert-over-rows driver that runs each detector-under-test across its corpus rows
  and emits the §4 scoreboard.
- **Detectors:** pure `EventStreamSnapshot -> Optional[Finding]` functions in the
  slice-4 framework extending `health_checks/tier1/`. Each registers the rows it
  owns and is run against them by the harness.
- **Adjudication seam:** detectors only set `requires_adjudication`; the
  orchestrator (slice 4) owns spawning the on-demand OVERSEER agent and consuming
  its verdict. The corpus tests detectors, not the agent.

## Related

- Issue [#2270](https://github.com/jwbron/egg/issues/2270) — overseer overhaul umbrella (AC-3, deliverable #1)
- [Health Check Framework](../../orchestrator/health_checks/README.md) — the Tier-1 framework the detection plane extends
- `docs/architecture/overseer.md` — overseer architecture (delivered shape; written in slice 9)
- #3230 (false stall), #2242 (heartbeat-stall), #2222/#2224 (branch-divergence), #2948 (transient eviction) — the calibration motivating cases

# Overseer Calibration Corpus & Detector Test Harness

> The regression bedrock every overseer detector plugs into
> ([#2270](https://github.com/jwbron/egg/issues/2270)).

The overseer architecture uses an orchestrator-side **detection plane** of
deterministic detectors instead of a noisy, respawning watcher pod.
The single biggest risk in that work is *recalibration regressions* — a detector
that goes quiet on a real fault, or one that floods operators with false alarms.
The **calibration corpus** is how that risk is made testable: a fixed set of
labelled event-stream snapshots that every detector is asserted against, so a
miscalibration shows up as a failing test instead of a 3 a.m. page.

The corpus is a test-fixtures package — it holds the corpus fixtures, the
harness, and the contract detectors are asserted against; it contains no
production detector code.

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
| `detector_key` | Which detector this row exercises (resolves the row to its detector; see §5) |
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
    recommended_action,     # from the bounded corrective vocabulary
    requires_adjudication,  # bool — escalate to the on-demand adjudicator agent?
}
```

The corpus's `expected` label is the narrower `ExpectedFinding`
(`finding_class`, `severity`, `requires_adjudication`); the harness matches a
detector's `Finding` against it structurally via `match_finding`.

`requires_adjudication=True` is the seam to the adjudication plane: the
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
assert rule 1 and rule 2. **Corpus-tested == shippable.** A detector ships *only*
if it passes this contract — that is the structural guard against a new
false-positive flood.

---

## 3. The seed corpus rows

These rows are the concrete failure modes that motivated the detection plane;
each is encoded in **both** a `known-normal` and a `known-bad` variant where the
distinction is the whole point.

| Row | `finding_class` | Why it exists |
|-----|-----------------|---------------|
| Self-injection loop | `overseer_self_injection` | Overseer mis-reads its own bootstrap as a prompt-injection attack, refuses, respawns, alerts `[high]` each cycle |
| Alert-reflection | `alert_reflection` | An overseer/orchestrator informational alert reflected back as a **binding** operator HITL directive |
| #3230 false stall | `phase_stall` | Producer drafting under **orchestrator-owned** spawn — normal, must NOT fire (this is the lifecycle-owner row) |
| #2242 heartbeat-stall | `heartbeat_stall` | Agent emitting tool calls every 2–3s is alive — must NOT be flagged as stalled |
| #2222/#2224 branch-divergence | `branch_divergence` | Detector must use **ancestor-of-`origin/main` OR patch-id** match, not `(#NNNN)` subject-regex matching |
| #2948 transient kubelet eviction | `container_death` | A transient evict/reschedule must NOT cascade to a producer-permanent-death `FAILED` |

Beyond this seed set, each detector contributes the rows it owns. The §5 survey
is the largest contributor — 25 coverage-gap detectors: 24 across 8 modules in
``health_checks/tier1/`` plus ``detect_overseer_self_health`` in
``overseer/self_monitor.py``. New rows extend the corpus without reshaping the
record shape or the contract.

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
(TP / (TP + FN)). The acceptance signal is `false_positive == 0` and
`precision == 1.0` (a registered detector never over-fires), with `recall == 1.0`
once every detector the corpus pins is registered. This precision/recall tally is
distinct from the per-row pytest pass/`xfail` bookkeeping in the harness (see §5).

---

## 5. The xfail → strict flip convention (red → green workflow)

A corpus row can exist for a detector that is not yet built — the corpus is the
*spec*, and a row can be written before its detector. Such a row cannot pass
against code that does not exist, so the harness marks each known-bad row whose
detector is unregistered as **`xfail`** (expected-to-fail).

The marker is applied **automatically and conditionally** in `_row_param`: a row
is xfailed iff `resolve_detector(row.detector_key) is None` and the row is
known-bad. The xfail is registered with `strict=False`, so a build with
unimplemented detectors stays green (e.g. `10 passed, 6 xfailed`) without any
`xpass` failing the build. Nobody hand-marks individual rows.

A row therefore moves **red → green** as its detector is built:

1. **Red.** The row exists but no detector resolves for its `detector_key`, so the
   harness marks it `xfail`. It documents the exact `known_normal`/`known_bad`
   behaviour the detector must satisfy, and the build stays green because `xfail`
   is tolerated.
2. **Implement.** The detector is built and registered via `register_detector`.
   The moment it resolves, `_row_param` no longer applies the xfail marker — the
   row becomes a strict assertion automatically (the marker "evaporates" because
   the detector resolves).
3. **Green.** From then on the row is a hard regression gate: any later change that
   breaks the detector fails the build.

> **Convention:** when you land a detector, register it so the harness resolves it
> for the row's `detector_key`; the rows then flip to strict on their own in the
> *same* change. A detector merged without registering leaves its rows stuck at
> `xfail` — treat that as incomplete.

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
  detection-plane framework extending `health_checks/tier1/`. Each registers the
  rows it owns and is run against them by the harness.
- **Adjudication seam:** detectors only set `requires_adjudication`; the
  orchestrator owns spawning the on-demand OVERSEER agent and consuming its
  verdict. The corpus tests detectors, not the agent.

## Related

- Issue [#2270](https://github.com/jwbron/egg/issues/2270) — overseer overhaul umbrella
- [Health Check Framework](../../orchestrator/health_checks/README.md) — the Tier-1 framework the detection plane extends
- `docs/architecture/overseer.md` — overseer architecture
- #3230 (false stall), #2242 (heartbeat-stall), #2222/#2224 (branch-divergence), #2948 (transient eviction) — the calibration motivating cases

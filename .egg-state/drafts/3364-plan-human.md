# The plan in plain terms — issue #3364

**Do the three remaining pieces of this work as three separate, independent
pull requests — one that adds monitoring tools, one that hardens how the system
rides out provider throttling, and one that trims the `/sdlc` skill — and gate
the trimming on first proving nothing goes dark.**

## How the work is split

Three pull requests, each landing on its own. They touch **completely separate
files**, so there's no collision between them and they can land in **any
order** (doing the small one first is preferred, just for quick value — it
isn't required). Only the third one has an internal ordering rule inside
itself.

| PR | What it does | Risk |
|----|--------------|------|
| **PR B** | Monitoring tools: new filter options + a "unit finished" event | Low — purely additive |
| **PR C** | Supervision hardening: ride out provider throttling instead of giving up | Higher — changes live behavior |
| **PR D** | Trim the skill file, after proving the overseer covers what's removed | Medium — deletion, gated |

## PR B — Better long-run monitoring (low risk)

Four steps:

1. Add two options to the status-watching tool (`wait-status`): one to **hide
   chosen event types** and one for **quieter output**. The filtering happens
   on the reader's side, and the tool stays simple and dependency-free. The
   existing options and the normal (unfiltered) output are left exactly as they
   are.
2. Add a new **"a unit of work finished" event** and fire it at both places a
   unit closes — success and failure — carrying enough detail to say **which**
   unit and **whether it succeeded or failed**, so a watcher needs no second
   lookup. (Technically: the event is fired outside the scheduler's internal
   lock, so it can't cause a deadlock.)
3. Add that new event to the **approved list** so the status stream actually
   forwards it to watchers.
4. Tests for both new options and for both firing points (one success event,
   one failure event, each tellable apart).

## PR C — Riding out provider throttling (the highest-risk piece)

Today, when the AI provider throttles the whole run, the system misreads it as
a generic failure and gives up within minutes. Five steps fix that:

1. **Recognize throttling for what it is.** Detect the signature of *every*
   worker failing on throttle wording ("429", "rate limit", "overloaded") and
   classify it as a **temporary rate-limit** — separate from the generic
   "something's wrong" path that halts. It reuses the existing throttle-wording
   list rather than duplicating it, and does **not** change which errors count
   as fatal.
2. **Wait it out at the right pace.** Instead of the 30-second cap, retry
   patiently across the real throttle window (which can last hours) — reading
   the reset time from the error when it's given, otherwise backing off
   gently — without hammering the provider. Per the operator's decision
   (below): **no hard time limit**, but **raise an alert once the wait crosses
   a threshold**, so an attended operator is told while auto-recovery keeps
   going.
3. **Keep finished work.** Recovery reuses a restart that **preserves
   already-completed units** — it only re-arms the failed/in-flight part, never
   throwing away work that already landed.
4. **Don't loop forever on a real bug.** If a restart hits the *identical*
   failure at the *same point* (no progress), **stop and escalate** instead of
   looping; if the restart makes progress, keep going with the patient-retry
   policy. (This is a separate trigger from the threshold alert in step 2.)
5. Heavy tests for all of it: throttle → rate-limit (not the generic halt);
   the pacing/window and the threshold alert; finished work preserved across a
   restart; the loop-guard escalating on a repeat and continuing when there's
   progress; and a **regression check** that genuinely-fatal errors and
   ordinary failures still halt exactly as before.

## PR D — Trimming the skill (medium risk, and gated)

This is a deletion, but it's **gated**: the first step must prove coverage
before anything is removed.

1. **The gate (must come first).** For each of the five monitoring blocks being
   removed, name the concrete overseer source that produces an alert the skill
   already shows the user. This is **real verification, not a rubber stamp**:
   four of the five map only to the overseer's **AI-classifier** path (there is
   no fixed-rule emitter for them), and the migration hook the old text points
   to **doesn't exist in the code at all**. If any block turns out to have **no
   real overseer coverage**, that block is **raised to the operator as a
   decision** — with the specific block and evidence — rather than quietly
   deleted into a gap. **Nothing is removed without a confirmed replacement.**
2. **Then delete** (only after the gate): the old migration section, the
   "what-if-the-overseer-is-absent" fallback, the five monitoring blocks, the
   auto-trigger of the stuck-pipeline rescue, and a stray short stall block —
   plus the leftover on/off-switch conditionals. **Preserve** the skill's
   *reactions* to incoming alerts (the long-running-phase prompt and the
   unresolved-disagreement prompt must still fire) and the **user-initiated**
   rescue workflow itself (only its automatic trigger goes).
3. **Remove the old on/off switch entirely** (`overseer_owns_host_detection`) —
   concluding the trial period rather than flipping its default — leaving no
   trace of it anywhere in the code.
4. **Confirm what stays is intact** (reading the request, triage, submitting,
   the status-watching loop, brokering your decisions, showing you alerts, and
   the user-initiated rescue), and add a short **last-resort troubleshooting**
   note with two guardrails: never auto-perform a destructive suggestion
   (always ask first), and always stop the watcher before restarting it.
5. Verification: prove the removed switch and the non-existent hook are gone
   everywhere, update any tests that referenced the removed switch, and confirm
   the skill still reads correctly with its alert-reaction prompts intact.

## The one operator decision (already settled)

**cq-1 — when a throttle wall persists, how long should the patient retry keep
waiting?** The operator has decided: **retry until the cap lifts with no hard
limit, but raise an alert once the wait crosses a threshold** so an attended
operator is informed while auto-recovery continues. The plan adopts this
word-for-word in PR C. No new decision is needed at planning time. The only
other possible decision — a PR-D block that turns out to have no overseer
coverage — is deliberately **not** pre-registered; it's raised at the moment
it's discovered, if it happens at all.

## Verifying and landing

- Each PR is tested on its own; the fast test command is used during work and
  the **full suite** is run before the phase finishes, with the style checks
  green throughout.
- **No pre-merge coordination** is needed — the three PRs are independent and
  may merge in any order (small one first preferred).
- **After PR C lands**, watch the first real throttle-wall recovery to confirm
  the threshold alert fires and finished work is preserved — since this one
  changes live behavior.

## What this plan deliberately does *not* do

Re-do the already-landed PR A; build the deferred context-measurement surface
(tracked separately); tackle the thirteen visibility follow-ups (tracked in
sibling issues); re-implement the safety-gating that's already done (only a thin
backstop rule is kept); or add **new** overseer detectors to fill a coverage gap
— that last one is an operator decision raised only if the PR-D gate finds a
gap, not something scoped in advance.

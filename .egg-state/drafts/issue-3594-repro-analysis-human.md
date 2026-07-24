# In plain terms — issue #3364

**Trim the `/sdlc` skill down to three jobs — run pipelines, keep you posted,
and hand you decisions — and move all the "watch for trouble and recover"
machinery out of the skill and into the always-on orchestrator and overseer,
where it can protect every pipeline instead of only an attended session.**

## The problem

A long, failure-heavy run earlier (19 units of work, issue #3312) survived
because of a pile of monitoring-and-recovery behavior. The first instinct was
to write that playbook *into* the skill file (`skills/sdlc/SKILL.md`). That is
the wrong home. The skill's job is narrow: start pipelines, keep the user
updated, and broker decisions and alerts. Detecting problems, classifying
them, and recovering from them belong in the **orchestrator and overseer** —
the always-running parts that can see inside the pod and own the pipeline's
state. Put there, that protection covers *every* pipeline, not just the ones a
person happens to be watching.

Meanwhile the skill file has grown to 1,600 lines and still carries five
"detector" blocks that duplicate what the overseer should own. Those blocks
sit behind an old on/off switch from a migration effort (issue #1962) that is
now **closed** — so they are dead, duplicated logic to **delete**, not extend.

This work is **three independent pull requests**, each with its own risk level.
An earlier fourth piece (PR A) already landed separately and is not touched
here.

## PR B — Better tools for watching a long run (low risk, purely additive)

Two small, self-contained additions that the skill then consumes:

- The status-watching helper (`skills/sdlc/bin/wait-status`) gains two new
  options: one to **hide chosen event types** and one for a **quieter output**.
  The filtering happens on the reader's side, and the helper stays simple and
  dependency-free, exactly as it is today.
- A new **"a unit of work finished" event** is added and fired at both the
  success and failure points of the unit-of-work scheduler, carrying enough
  detail to tell success from failure and to identify which unit closed. It's
  also added to the approved list of events the status stream is allowed to
  forward, so watchers actually receive it.

Both are new-only changes — nothing existing changes behavior.

## PR C — Making supervision tougher (the highest-risk piece)

Today, when the AI provider throttles the whole run (a rate-limit or usage-cap
wall — "429", "rate limit", "overloaded"), the system mishandles it. Those
throttling signals are *deliberately* not treated as fatal, so they fall
through to the generic "something's wrong" path, trip a failure-streak counter
after about ten tries, and — because the retry wait is capped at 30 seconds —
the whole run gives up within minutes. That's wrong: a throttle is temporary
and should be waited out, not treated as a crash. Three fixes:

- **Recognize a throttle for what it is.** Detect the tell-tale pattern of
  *every* worker failing on throttling signals, and classify it as a
  temporary rate-limit — separate from the generic-failure path that halts.
- **Wait it out at the right pace.** Instead of the 30-second cap, retry
  patiently across the real throttle window (which can last hours), without
  hammering the provider — and do it in a way that **keeps already-finished
  units of work**, rather than throwing that progress away on recovery.
- **Don't loop forever on a real bug.** Tell a temporary throttle apart from a
  genuine, repeatable failure: if a restart hits the *exact same* failure at
  the *exact same* point, **stop and escalate** instead of retrying endlessly.

## PR D — Slim the skill (medium risk, and gated — see below)

- **Delete** from the skill file: the old migration section, the
  "what to do if the overseer isn't running" fallback, and all five host-side
  detector blocks (stall detection, silent-agent detection, escalation on
  unresolved disagreements, long-running-phase detection, and the
  *auto-trigger* of the "stuck pipeline rescue" — roughly lines 553–793, plus
  a short stall-detection block near line 1517).
- **Remove the old on/off switch entirely** (`overseer_owns_host_detection`),
  concluding the trial period rather than just flipping its default, and leave
  no dangling references to it.
- **Keep** everything that makes the skill useful: reading the request and
  seeding a run, the pre-work triage, submitting, the status-watching loop,
  brokering your decisions (approvals, choices, free-text feedback), showing
  you the overseer's alerts, and the **user-initiated** stuck-pipeline rescue
  workflow — only its *automatic trigger* goes, the workflow itself stays.
- **Keep a short last-resort troubleshooting note** with two guardrails: never
  auto-perform a destructive suggestion (always ask you first), and always
  stop the watcher before restarting it.
- **Critically, keep the "show the user this alert" reactions.** Removing the
  *detection* must not remove the skill's responses when an overseer alert
  *does* arrive — the long-running-phase prompt and the unresolved-disagreement
  prompt must still fire on the matching alert.

## The one genuine catch in PR D — the deletion is gated

It's tempting to assume "the overseer already does all this, so deleting is
safe." The refiner checked, and that assumption **does not fully hold**. The
overseer's always-on, deterministic checks emit a *different* set of alerts
than the five host blocks. The specific vocabulary those host blocks use
(stall, silent, unresolved-disagreement, long-running) shows up in production
only as bookkeeping, not as live emitters — and the part that actually
classifies problems into that vocabulary runs through an **AI-based
classifier**, not a fixed rule. On top of that, the exact migration hook the
old section points to (`run_migrated_detectors`) **exists nowhere in the real
code** — only in the skill's own prose.

So before **any** deletion, PR D must produce a **coverage map**: for each
host block being removed, name the concrete overseer source (either a
deterministic structural check or the classifier path) that produces an alert
the skill already surfaces. **No block gets deleted without a confirmed
replacement.** If the map turns up a block with no real overseer coverage,
that's a genuine gap — and the choice of "delete anyway," "keep the block," or
"add a new overseer detector" is **not** the refiner's to make. It gets raised
to you as a decision **at the moment it's discovered** (during planning or
implementation), with the specific block and evidence attached. It's
deliberately not pre-registered now, because whether it happens at all depends
on what the mapping finds.

## What's left for a human to decide

One real decision, in PR C:

- **How long should the patient throttle-retry keep waiting before it gives
  up?** Waiting out a throttle fixes the *pacing*, but a subscription or
  weekly cap can stay shut for hours or even days. Without a ceiling, an
  unattended pipeline could sit paused for a very long time — and that pause
  is something you'd see. So the question of the retry's upper bound is put to
  you as an explicit decision (tracked as **cq-1**).

Everything else in the issue is already settled — either by the original
instructions or as routine planning-and-implementation detail.

## Operator decision already resolved

The one decision raised above (how long to keep waiting when a cap wall
persists) has been answered by a human reviewer at this phase gate:

- **Retry until the cap lifts (no hard ceiling)** — keep trying across the
  real throttle window, don't give up at a fixed retry count.
- **But emit an OVERSEER_ALERT once the wait crosses a threshold** — so that an
  attended operator is informed while auto-recovery continues.

This means an unattended pipeline won't silently sit paused forever without
anyone knowing; the alert surfaces the situation to someone who can act,
while the system keeps trying to recover on its own.

## A few groundwork notes

- The three PRs are independent and should be planned as three separate units
  of work. PR C is the riskiest (it changes live supervision behavior), so it
  should carry heavy test coverage on the new throttle-classification and the
  loop guard.
- Several exact file locations named in the original issue have since moved
  (the code was reorganized into folders); the refiner re-checked and recorded
  the corrected, current locations, which supersede the issue's older ones.
- The PR D plan must schedule the coverage-map **before** any deletion, and
  must explicitly protect the "react to an incoming alert" paths.

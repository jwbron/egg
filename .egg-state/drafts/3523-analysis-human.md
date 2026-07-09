# In plain terms — issue #3523

**Make egg's automated code review both sharper and cheaper: force every
objection to come with a concrete "here's how it breaks" example, tell
reviewers *how* to hunt (not just what to care about), send the heavyweight
reviewers only to the changes that actually warrant them, and stop every
reviewer from re-reading the same code from scratch.**

## Background: how egg reviews work today

When the pipeline proposes a change, several specialist AI reviewers each look
at it through their own lens — one for security, one for concurrency, one for
general correctness, and so on. Each votes to approve or block, and a simple
rule combines the votes: any blocking vote from a critical reviewer stops the
change until it's fixed.

The refiner checked the issue's central claim — "the right machinery already
exists" — against the live code and confirmed it: every piece the issue builds
on is really there. The problem isn't missing machinery; it's four rough edges
that cost time and money:

1. **Wasted rework.** A "block" today is just free-form prose. A vague or
   unreproducible objection still forces a full revision round, and can even
   trip a safety lockout that escalates a perfectly healthy change to a human.
2. **Shallow reviews.** The reviewer instructions say *what* to worry about by
   topic, but never *how* to look — nothing tells a reviewer to trace who calls
   a changed function, or to check what a deleted line used to protect.
3. **One-size-fits-all cost.** Every change gets every heavyweight reviewer at
   full depth — whether it touches delicate concurrency code or just a README.
   And each reviewer starts cold: five or more of them separately re-read the
   same diff and the same files, each paying full price to get up to speed.

The issue proposes five changes to fix these. Below, each in plain terms.

## The five changes

### 1. Make every objection structured — and let the system, not the reviewer, decide "block or approve"

Instead of a free-text block reason, each reviewer fills in a small standard
form for every concern: where it is, a one-sentence summary, how confident they
are, the exact line they're worried about, and — **required** — a concrete
**failure scenario**: what inputs or situation trigger it, and the wrong result,
crash, or data loss that follows. **A concern with no failure scenario is not
allowed to block** — it's automatically downgraded to advisory (a note, not a
stopper).

The reviewers still own all the judgment (what to flag, how serious, how
confident, every word of explanation). But the *decision* to approve or block
is now computed mechanically from the forms: any genuine blocker means "block,"
notes-only means "approve, with the notes attached," nothing at all means
"approve." A nice side effect: when two different reviewers flag the same
underlying cause, the system merges them into one concern backed by several
reviewers — which sensibly raises confidence. Today that agreement is thrown
away.

Why it helps: a blocker now always comes with a reproducible example, so any
rework the pipeline is forced into is spent on something real — and because the
one doing the rework is itself an AI, a concrete example plus a suggested fix
turns straight into a faster, more reliable correction.

### 2. A three-step "how sure are you?" ladder for reviewers

Add an explicit rating every reviewer must apply to each concern:

- **Confirmed** — they can name exactly what triggers it and the wrong outcome,
  and quote the offending line.
- **Plausible** — the mechanism is real but they can't be sure it actually
  triggers (depends on timing, environment, configuration). They say what would
  settle it.
- **Refuted** — it's actually fine (something elsewhere already guards against
  it); they must quote the thing that rules it out.

Only a **confirmed** concern may block. A **plausible** one becomes an advisory
note, never a blocker. Only a **refuted** one is dropped silently — so
"I'm not sure" downgrades a concern rather than deleting it. Reviewers are also
explicitly allowed to run small, safe, read-only experiments in a scratch area
(never touching the real code, never the network) to actually check a claim
rather than guess — with a cap on how much effort any single concern may
consume. One subtlety: a bug that was already there still counts as confirmed if
the new change makes its consequences materially worse — and the write-up has to
say so.

### 3. Tell the code reviewer *how* to look, with named search routines

Add four concrete search procedures to the general code-review instructions (one
file, no new reviewers):

- **Line-by-line scan** — for each changed line, ask what input, state, timing,
  or platform would make it wrong.
- **Removed-behavior audit** — for each deleted or replaced line, name what it
  used to guarantee, then find where the new code re-establishes it. If nothing
  does, that's a finding. (The instructions say nothing about deletions today,
  and dropped safety checks are exactly the kind of mistake that creeps in under
  revision pressure.)
- **Cross-file tracer** — for each changed function, check everything that calls
  it (did the change break a caller?) and everything it calls.
- **Quote-the-rule discipline** — only flag a "you broke a convention" issue if
  you can quote both the written rule and the line that violates it. No vibes.

### 4. A simple, rules-based dispatcher in front of the reviewers

Right now every change is sent to every heavyweight reviewer at full depth. Add
a small **rules-based** dispatcher (plain code, deliberately *not* an AI) that
looks at which files a change touches and decides:

- **Which reviewers to send.** The concurrency specialist only for
  concurrency-sensitive code; the security specialist always for anything
  touching logins, sessions, or untrusted input; a docs-only change gets a
  minimal review.
- **How hard to look.** It sets a risk level that dials each reviewer's effort
  up or down, reusing effort controls that already exist.
- **Optionally, how to frame the review** — lean toward "only flag things worth
  acting on" for trivial changes, "cast a wide net" for risky ones.

Cost here is treated as a genuine goal, not an afterthought: low-risk changes
should be routed to lighter reviews aggressively, so spending tracks real risk.
But there are firm safety floors that cost-cutting can never override: **a change
that matches no rule gets the full review plus a loud warning** (a gap in the
rules must never quietly mean *less* review), a guaranteed minimum level catches
anything misrouted, and the security specialist can never be switched off on
sensitive paths.

### 5. One shared "evidence pack" so reviewers stop re-reading everything (the cost bet)

Because each reviewer currently starts from scratch, most of their cost is
rebuilding the same context. The fix: one **unprivileged gatherer** (read-only,
can't post anything, casts no vote) assembles a single evidence pack for the
change — the diff, the changed files with enough surrounding code, lists of what
calls and is called by the changed parts, and confirmed facts about the
environment. Every reviewer then receives that identical pack as the opening of
their prompt, which lets the AI
service bill that shared portion at a large discount when the reviewers run
together.

The **hard rule: evidence only, never conclusions.** No hunches, no "areas of
concern," no ordering by importance — the pack is arranged mechanically (by file
path), because any emphasis would quietly steer every reviewer the same way and
destroy the independent-agreement signal from change #1. Each reviewer still
does its own digging from that shared starting point. Two independence
safeguards are kept firm: the adversarial tester and anyone *double-checking* a
concern still start cold (a fact-checker must not inherit the context that
produced the claim), and the existing rule that hides reviewers' self-assessments
from each other is untouched. One risk is called out plainly: funneling the
change's text through a single gatherer means any hostile content in a diff now
reaches every reviewer through one channel — so, as today, the pack is always
treated as material to inspect, never as instructions to obey.

## Rolled out carefully, in order

Every change that alters behavior ships through the project's established
three-step switch: **off**, then **log** (it runs silently and records what it
*would* have done, without acting), then **on**. A typo in the setting fails
safely to *off*. Pure wording additions (changes 3 and the checklists in
change 2) need no switch at all. The order is deliberate:

1. The wording-only pieces first (change 3 and change 2's checklists) —
   zero-risk, no plumbing.
2. Then change 1 (the structured objection forms) — the foundation the rest
   leans on.
3. Then change 4 (the dispatcher), in "log" mode first.
4. Then change 5 (the shared pack), behind its own switch, last — the
   dispatcher decides which reviews are big enough for the shared pack to pay
   off, and change 1's forms make it measurable whether the shared pack is
   quietly biasing anyone.

Notably, changes 2–4 are deliberately modeled on an existing, proven review
checklist that ships inside the tooling (Claude Code's built-in review feature),
rather than invented from scratch — so egg's reviewer stays compatible with it
instead of drifting into something bespoke. This is an explicit instruction from
the person who requested the work.

## What's deliberately left out

- No benchmark or scoring harness to grade review quality — in this
  single-operator setup, the operator judges quality directly.
- No "learn from human review feedback" loop — there's no human PR review in
  this setup to learn from.

## Is there anything for a human to decide?

**No open questions.** The refiner judged the issue to be a complete, firm
instruction: it already fixes the five changes, the exact fields on the
objection form, the rollout order, the three-step switch, and the safety floors
and independence rules. The choices that remain — precisely where to cut the
work into stages, the exact encodings, the dispatcher's config format, how the
gatherer is packaged — are implementation details for the planning stage, not
judgment calls for the operator. If planning later turns up a genuine fork in
the road (for example, the shared pack proving too thin to be useful), that will
be raised as a decision then.

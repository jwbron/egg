# In plain terms — the plan for issue #3523

**The five review-quality improvements will be built as eight self-contained
steps: one stands alone (adding wording to the reviewer instructions), and the
other seven form a single chain because they all touch the same core file.
Every step that changes how reviews actually behave ships switched *off*, with a
"watch it silently first" middle setting, so nothing changes until someone
deliberately turns it on.**

## What's being built (recap)

Issue #3523 asks for five improvements to egg's automated code review, worked
out in detail during the earlier analysis stage (see the companion
"In plain terms" note for the analysis). In short:

1. Reviewer instructions that say *how* to hunt for problems, plus a
   three-step "how sure are you?" scale (Confirmed / Plausible / Refuted).
2. A standard structured form for every objection — including a **required**
   concrete "here's how it breaks" example.
3. Letting the system, not the reviewer, decide "block or approve" from those
   forms, and merging duplicate objections from different reviewers into one.
4. A simple rules-based dispatcher that sends the right reviewers, at the right
   depth, to each change based on risk.
5. A shared "evidence pack" so reviewers stop re-reading the same code from
   scratch.

This plan turns those into a concrete build order.

## How the work is broken into eight steps

The planner took the architect's blueprint and finalized it into eight steps.
Seven of them have to happen in a strict order because they all edit the same
central file (the piece that wraps every reviewer), and two things editing one
file at the same time would collide — so they line up single file. One step
stands completely apart because it only touches the reviewer instruction
documents, which nothing else edits.

- **Step 1 — Reviewer instructions (stands alone).** Add the four "how to look"
  routines (including the new "what did this deleted line used to protect?"
  check) and the three-step confidence scale to the shared reviewer
  instructions. Pure wording; changes no behavior; needs no switch.
- **Step 2 — The objection form (stands alone).** Define the standard
  structured form for a single objection, with its required "how it breaks"
  example. Purely an addition — nothing uses it yet, so nothing changes.
- **Step 3 — Let the system decide block-or-approve.** Have the system read the
  forms and compute the verdict (any real blocker → block; notes-only → approve
  with the notes attached; nothing → approve), and merge duplicate objections
  that name the same underlying cause. Ships behind a switch; the old free-text
  path stays in place until the switch is turned fully on.
- **Step 4 — A cap on checking effort.** Put a configurable limit on how many
  investigative steps a reviewer may spend per objection (so the newly-allowed
  "run a small experiment to check" can't run away). Enforced in the wrapper,
  not the instructions. Shares step 3's switch.
- **Step 5 — Build the risk dispatcher (not yet plugged in).** Write the
  rules-based dispatcher and its per-project risk settings file as a standalone,
  fully-tested piece — but don't connect it to anything yet, so nothing changes.
  It bakes in the safety floors (a change matching no rule gets the *full*
  review plus a loud warning; the security specialist can never be switched off
  sensitive code).
- **Step 6 — Plug the dispatcher in.** Connect step 5's dispatcher to the live
  review so it actually picks the reviewers, dials their effort, and optionally
  sets the framing (strict on trivial changes, wide net on risky ones). Behind
  its own switch, "watch silently" first.
- **Step 7 — The shared evidence pack.** Add a new **read-only** gatherer role
  that can't vote, can't post, and can't touch GitHub — it just assembles the
  shared pack (the diff, the changed files with surrounding code, and the
  caller/callee lists) and hands every reviewer the identical opening so the
  shared part is billed cheaply. Behind its own switch.
- **Step 8 — Documentation.** Write it all up for operators and future
  maintainers, and cross-link the existing approve-with-obligations reference.

(Two small planner refinements worth noting: the risk dispatcher was split into
"build the tool" and "plug it in" so each step stays small and reviewable; and
the chain was arranged so each step has exactly one predecessor, which the
platform requires.)

## How it's kept safe

Every step that changes real behavior (steps 3, 4, 6, and 7) ships behind the
project's standard three-position switch: **off** (the default — completely
inert), **watch silently** (it runs alongside the current behavior and records
what it *would* have done, without acting), and **on**. A typo in the setting
falls back to *off*. The pure-wording steps (1, and the checklists in 2) need no
switch at all. Crucially, the old behavior stays fully in place until a switch
is deliberately turned on — so merging this work changes nothing on its own.

## Cost is treated as a real goal

Two of the steps are meant to *save* money, and that's measured, not assumed:
the dispatcher (steps 5–6) is required to route low-risk changes to lighter,
cheaper reviews rather than always running everything; and the shared evidence
pack (step 7) must actually record its measured savings while in "watch
silently" mode — and a genuine, measured cost reduction is required before its
switch is allowed to be turned on.

## Built to match an existing checklist

The new reviewer wording and risk levels are modeled on an existing, proven
review checklist that ships inside the tooling (Claude Code's built-in review
feature) rather than invented from scratch — an explicit instruction from the
person who requested the work. The implementer is told to fetch that reference
(from two specific comments on the GitHub issue) before writing any of the
reviewer wording, so the vocabulary stays consistent instead of bespoke.

## How it's tested

- Step 1 gets a "guard" test that fails if any of the required routines,
  confidence levels, or rules is ever removed from the instructions.
- The standalone logic pieces (the objection form, the block-or-approve
  decision, the dispatcher) get thorough unit tests — that logic is exactly what
  must be pinned down.
- Every behavior-changing step gets two specific safety tests: one proving that
  "watch silently" mode changes no real outcome versus today, and one proving a
  typo'd switch falls back to *off*. The shared-pack step also proves every
  reviewer really gets an identical opening and that the fact-checkers stay
  independent.
- Each step must leave the whole build green on its own.

## What a human needs to do

- **Before merging: nothing.** Everything defaults to *off* and is inert until
  someone opts in.
- **After merging: a gradual, per-feature rollout.** For each of the three
  switches in turn, set it to "watch silently," check the recorded
  would-have-been behavior across several review rounds, then turn it on. The
  shared-evidence switch additionally requires that measured cost saving before
  being turned on.

## Is there anything for a human to decide right now?

**No.** The planner raised no questions for the operator. The four choices that
had been left open earlier (exactly how to cut the steps, how to encode the
objection form, the format of the risk-settings file, and how to package the
evidence gatherer) were all settled as ordinary engineering decisions at the
architect and planning stages — consistent with the earlier finding that this
issue is a complete, firm instruction. The only thing flagged as a *possible*
future question is the one the issue itself named — if the shared evidence pack
turns out to be too thin to be useful, that would come back as a decision then,
not now.

## What's deliberately left out

- No benchmark or scoring system to grade review quality — the operator judges
  it directly.
- No "learn from human review feedback" loop — there's no human PR review in
  this setup to learn from.

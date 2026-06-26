# Plan in plain language: make egg's docs describe how things work now

**Issue #3288.** This is a jargon-free companion to the engineering plan. It
explains *what* we're doing and *why*, for a reader who doesn't need the
internal project mechanics. It adds no new scope — it just restates the plan.

## The problem

egg's documentation has slowly turned into a *running history of changes*
instead of a *description of how the system works today*. Pages and code
comments are peppered with internal bookkeeping — work-item numbers, internal
step counters, and notes like "what was removed in change #NNNN" — mixed in with
the actual explanation of how things behave.

That bookkeeping helps the person making a change, but once the change is
shipped it just gets in the reader's way: it doesn't describe the current code,
it takes effort to read past, and it slowly goes stale and misleading as later
changes pile up.

## The goal

Make the documentation a clean snapshot of the current state. There are two
connected pieces of work:

1. **Fix the tool that writes the docs.** egg has an automated documentation
   writer. Its instructions currently tell it to "document the changes that were
   just made," which is exactly what produces history-style writing. We rewrite
   those instructions so it instead describes how the code works *now*, as if the
   internal change-tracking machinery didn't exist — and never bakes that
   internal bookkeeping into anything it writes.

2. **Clean up the docs we already have.** A focused pass over the existing
   documentation and code comments to strip out the bookkeeping and rewrite
   "it used to do X, now it does Y" passages into plain "it does Y" descriptions.

Important boundary: this is **not** "remove every issue reference." A link that
explains *why the system is built the way it is* is genuinely useful and stays —
we just reframe it as a reason rather than a timeline. The rule of thumb is:
keep the *why*, drop the *when*.

## The order of work

One foundational change comes first: fixing the documentation writer's
instructions, so the standard for "good docs" is settled before we clean the
existing docs against it.

Everything else is a set of independent cleanup passes that each build on that
foundation. They don't overlap — each one owns a separate area of the codebase —
so they can proceed in parallel without stepping on each other.

## The cleanup passes

- **The documentation writer's instructions** (the foundation). Reword the
  instructions and role description so the writer produces current-state
  snapshots, never embeds internal bookkeeping, prefers explaining *why* over
  *when*, and — when updating an existing page — folds the new state in and
  removes the now-stale history rather than stacking another layer on top.
  Two things are deliberately left untouched: the writer's existing limits on
  which files it may edit, and its ability to correctly say "no documentation
  change is needed" when a change has no doc impact.

- **Architecture pages — light edits.** Several architecture pages need only
  sentence-level fixes: swap history-style narration for present-tense
  descriptions and remove the internal bookkeeping, keeping useful reasoning.

- **Architecture pages — full rewrites.** A few pages are built *around* a
  historical narrative (inventories of "what was removed," "this landed in
  several steps," tables of retired features). These need rewriting from the
  ground up into a description of how things work today, preserving only the
  genuinely useful reasoning.

- **The gateway component.** Clean the history-style tags out of the gateway's
  code comments and docstrings (text only — no behavior changes), and
  reorganize the gateway's component guide so its tables describe today's
  structure instead of the order pieces were built in.

- **The orchestrator component.** Same treatment for the orchestrator: clean the
  dense history tags out of its code comments (notably in one heavily-tagged
  file), and reorganize its component guide around today's structure.

- **Shared code plus a bounded sweep.** Clean the history framing out of a set
  of shared-code docstrings, and sweep the handful of remaining
  highest-density reference and guide pages. This pass also closes the loop by
  writing down — in the change's own notes, not as a new permanent doc — an
  explicit list of the lower-priority files we're intentionally leaving for a
  later follow-up, so the coverage is honest and auditable.

## What we're deliberately not doing now

Two things are scoped out and handed to follow-up work, by explicit decision:

- **The long tail of low-density files.** Many files carry just one or two
  bookkeeping references. We thoroughly clean the named, high-density targets
  and explicitly list the rest as deferred — we never pretend the corpus is
  100% clean.

- **An automated guard.** We are not building an automatic check that blocks
  future bookkeeping from creeping back into the docs. It's a reasonable idea,
  but it needs its own tuning to avoid false alarms, so it's noted as a separate
  follow-up.

## How we'll know it worked

- The documentation writer's instructions clearly call for current-state
  writing, forbid embedding internal bookkeeping, and still correctly handle the
  "no doc change needed" case. Its file-editing limits are unchanged.
- The cleaned-up files read as descriptions of how the system behaves today, not
  as change logs. Any issue links that remain are there to explain a reason, not
  a timeline.
- Code-comment edits change wording only — the affected components' tests stay
  green, proving no behavior changed.
- The automated checks and linters stay green throughout.

## After it ships

Nothing needs to be done by hand. Everything here is documentation and
instruction text, so it's a no-op for anything currently running. The updated
instructions for the documentation writer take effect for future work once the
system is rebuilt on its normal release schedule. Two follow-up items are
expected: cleaning the deferred long tail of files, and the optional automated
guard mentioned above.

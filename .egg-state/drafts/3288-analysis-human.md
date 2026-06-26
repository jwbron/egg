# In plain terms — issue #3288

**Make the documentation describe how the system works *today*, instead of
reading like a running diary of every change that was ever made.**

## The problem

Over time, egg's documentation has drifted. Instead of telling a reader how the
software currently behaves, many pages and code comments now read like a
change-log: "this was added," "that used to work differently," "step 3 landed
this piece," and so on. They are littered with internal process labels — things
like work-batch numbers and task IDs that only mean something inside egg's own
development machinery, not to someone trying to understand the code.

There are two reasons for the drift, and the fix has two matching parts:

1. **The automated documentation writer is pointed in the wrong direction.** The
   instructions it follows tell it to "document the *changes*" that were just
   made. That framing naturally produces change-log prose and pulls those
   internal process labels into permanent documentation.
2. **The existing documentation has accumulated years of that style.** Roughly
   260 files across the docs and source trees already carry these process labels
   and change-log narration.

The goal: documentation should read as if the internal development process never
existed. History is fine *only* when it genuinely helps a present-day reader —
for example, explaining *why* something is built the way it is — and even then,
the reasoning matters more than the chronology.

## The two pieces of work

**Piece 1 — Retrain the automated documentation writer (small, low risk).**
Update the instructions it follows so that it:
- describes the *current* state of the code, not the change that produced it;
- never sprinkles internal process labels into documentation, comments, or code
  docstrings;
- includes background only when it's genuinely useful, favoring the reasoning
  over a timeline;
- when editing an existing page, *merges* the new reality into the description
  and *deletes* the now-stale change-log lines, rather than tacking on another
  entry;
- still correctly does nothing when a change has no documentation impact (that
  existing "nothing to document here" path must keep working).

This touches just two source files and is essentially text-only. It's worth
doing first, because it sets the standard that the cleanup then follows.

**Piece 2 — Clean up the existing documentation (large).** A pass over the docs,
the README and guidance files, and code comments to strip the internal process
labels, rewrite change-log-style passages into plain "here's how it works now"
descriptions, and remove "historical record" sections that no longer match the
live code. A few high-value pages are written so heavily around the change-log
framing that they need a full rewrite, not a line edit. This piece is large
enough that it should be broken into independent chunks (by area of the docs) so
the work is reviewable and pieces don't collide.

## What *not* to do

- This is **not** "delete every issue reference." A link that explains *why* the
  current design exists is useful and stays — just reframed as reasoning rather
  than as a dated event.
- The automated writer's existing limits on *which files it may edit* (only
  documentation and a couple of scratch locations — never source code or tests)
  stay exactly as they are. Only the wording of its instructions changes.
- The "nothing to document" shortcut stays working.

## Two decisions left for a human

1. **How thorough should the cleanup be?** Around 260 files are affected.
   Recommended: clean the specific, high-impact files the issue calls out plus
   the densest offenders now, and leave the long tail to follow-up work — rather
   than attempting all 260 at once (which would be a merge headache) or doing
   none of the cleanup now.
2. **Should we add an automated check** that catches these internal process
   labels before they land in documentation again? Recommended: not in this
   round — it's separable, has its own tuning cost, and the issue asks for an
   instruction change plus a cleanup, not a new automated guard.

# The plan, in plain terms — issue #3312

**Split the 19 oversized files one at a time, each as its own self-contained
pull request, starting with the easiest and saving the two giants for last —
but every one of the 19 still gets done.**

(This is the companion to the plain-language problem write-up; that one explained
*what* and *why*. This one explains *how the work is organized*.)

## One file, one slice, one pull request

The work is cut into **19 independent pieces ("slices") — exactly one per
oversized file**. Each slice splits a single file and ships as its own pull
request. Nothing is bundled; nothing is half-done across slices.

The important property is that **the slices don't depend on each other**. An
earlier attempt needed a shared "set up the pattern first" step that everything
else waited on — but that groundwork already shipped (in merged PR #2335), so
there's no longer a bottleneck slice. Each of the 19 files can be split on its
own, in any order, even in parallel.

There's exactly **one file all 19 slices touch**: the allow-list (the little
"these files are too big, allow them for now" list), because each slice deletes
its own line from it. When two slices are in flight at once they'll both try to
edit that list — but that's a trivial, mechanical merge (each just removes a
different line), not a real dependency. There's a standard recipe for resolving
it, and the slices stay independent.

## The order: easiest first, giants last

The slices are lined up **smallest file to largest**:

- Slices 1–17 are the seventeen more manageable files (from ~1,500 lines up to
  ~5,000).
- Slice 18 is `gateway.py` (~10,400 lines).
- Slice 19 is `pipelines.py` (~27,200 lines) — the one containing the tricky
  `_run_pipeline` state machine.

Why this order? Two previous attempts at this work **never managed to land even
a single file**. Doing the easy ones first means real progress gets banked early
and the splitting recipe is battle-tested on seventeen simpler files before the
two hardest are attempted. Critically, **"last" does not mean "optional"** — the
two giants are fully in scope and must be finished; they're simply scheduled
where their longer review time won't hold up the seventeen quicker wins.

The plan recommends doing as many slices in parallel as is practical; the
architect may choose to run a few one-after-another for safety, but that's a
sequencing choice, not a change to the work.

## What happens inside each slice (the same routine every time)

Every slice follows the identical, proven recipe:

1. **Audit first** — search the whole codebase for everything that refers to the
   file's contents, so nothing gets left dangling when things move.
2. **Move, then split** — first turn the file into a folder in one clean "move
   only" step (so it's easy to verify nothing changed yet), then split its
   contents into smaller internal files.
3. **Keep the front door stable** — the folder's entry point re-publishes every
   name the old file offered, so all existing code and tests keep working
   untouched.
4. **Tidy the bookkeeping** — remove the file's allow-list line and add an
   accurate "here's the new layout" entry to the relevant map.
5. **Prove it's green** — the style check and the full test suite must both pass
   before the slice is done.

A couple of files need a little extra care, all called out in the plan:

- Files that are **mostly one big class** (several of the larger ones) are split
  by moving the *method bodies* into helper files while the class itself stays
  put — so anything referring to the class by its original name still works.
- The web-route files keep their route declarations in the front door and move
  only the logic behind them, so the list of URLs the server answers is provably
  unchanged.
- Two files that double as **runnable programs** (`entrypoint.py`, `orch_cli.py`)
  get extra checks that they can still be launched after becoming folders.
- The biggest file's `_run_pipeline` — the part that walks a job through its
  stages — is **deliberately split into one handler per stage plus a thin
  coordinating loop**, with the stage order preserved exactly. This is the one
  piece the issue insisted be tackled head-on rather than worked around, and the
  plan does exactly that.

## What "finished" means

- All 19 files are under the size limit.
- The allow-list is **empty** — and by design that only becomes true when the
  very last slice (`pipelines.py`) lands, so an empty allow-list is the single
  unambiguous "we're done" signal.
- The four "how it fits together" maps are accurate (entries added for every
  split file, a new section in the sandbox map, and a brand-new `shared` map).
- Nothing that used the old files broke, and no test lost track of an internal
  hook it relies on.
- Every slice passed the style check and the full test suite.
- No behavior changed anywhere; any bug noticed along the way is written up
  separately rather than fixed in passing.

## The main things reviewers will watch for

The plan hands reviewers a short watch-list — the realistic ways a *pure
reorganization* like this could still go wrong:

- **A forgotten re-publish** — if the front door fails to re-offer a moved name,
  a test that reaches for it fails. This is the single biggest risk, which is
  exactly why the most-referenced file (`pipelines.py`, with dozens of such
  hooks) is saved for last, after the recipe is proven.
- **The `_run_pipeline` split** subtly changing the order things happen — guarded
  by keeping the behavior identical and leaning on the existing thorough tests.
- **A split that leaves a piece still too big** — handled by splitting that piece
  further within the same slice, never by adding a new allow-list exception.
- **Hidden circular references** surfacing once a file becomes a folder — handled
  with one consistent import style per folder.
- **The main branch drifting** under a long-running giant slice — handled by the
  easy-first ordering and by re-checking against the latest code at the start of
  each slice.

## Anything for a human to decide?

No open scope question remains: the person who requested the work locked it to
all 19 files with no trimming, and that's reflected throughout the plan. The only
judgment left is sequencing and how aggressively to parallelize — and that's the
architect's call in the next stage, not a decision that needs a human ruling now.

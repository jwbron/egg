# The plan, in plain terms — issue #3312

**Split the 19 oversized files one at a time, each as its own self-contained
pull request, starting with the easiest and saving the two giants for last —
but every one of the 19 still gets done.**

(This is the companion to the plain-language problem write-up; that one explained
*what* and *why*. This one explains *how the work is organized*.)

## One file, one slice, one pull request

The work is cut into **19 pieces ("slices") — exactly one per oversized file**.
Each slice splits a single file and ships as its own pull request. Nothing is
bundled; nothing is half-done across slices.

The slices are **run strictly one after another, in a single line**
(slice 1, then slice 2, …, through slice 19) — not in parallel. The reason is a
single shared file: the allow-list (the little "these files are too big, allow
them for now" list), which **every** slice has to edit, because each one deletes
its own line from it. If the slices were worked on as separate parallel branches,
they'd all be editing that same list at once and would **collide when it came
time to merge them**. Sequencing them into one chain side-steps that collision
entirely: each slice starts from the finished result of the one before it, so the
allow-list is only ever changed by one slice at a time.

(A handful of slices also touch a second shared file — the container "build
recipe" that lists which code goes into the shipped image, see below — which
reinforces the same need to go one at a time. The one-time "set up the splitting
pattern" groundwork that an earlier attempt depended on already shipped in merged
PR #2335, so the only things forcing a sequence are those shared files.)

## The order: easiest first, giants last

The slices are lined up **smallest file to largest**:

- Slices 1–17 are the seventeen more manageable files (from ~1,500 lines up to
  ~5,000).
- Slice 18 is `gateway.py` (~10,400 lines).
- Slice 19 is `pipelines.py` (~27,200 lines) — the one containing the tricky
  `_run_pipeline` state machine.

Why this order along the chain? Two previous attempts at this work **never
managed to land even a single file**. Doing the easy ones first means real
progress gets banked early and the splitting recipe is battle-tested on seventeen
simpler files before the two hardest are attempted. Because the chain is
sequential, the seventeen simpler files all land *before* the two giants are
reached. Critically, **"last" does not mean "optional"** — the two giants are
fully in scope and must be finished; they're simply placed at the tail of the
chain so that if one of them stalls, it doesn't hold up the seventeen quicker
wins that have already landed ahead of it.

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
5. **Update the container build, for the files that need it** — some files are
   copied into the shipped container image *by name* (or by a pattern that only
   grabs loose files, not folders). When such a file becomes a folder, it would
   silently vanish from the image unless the build recipe is updated in the same
   slice. The plan lists exactly which slices need this (eight of them) and which
   don't, and those slices additionally build the image and check the code still
   loads inside it.
6. **Prove it's green** — the style check and the full test suite must both pass
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
- Every slice passed the style check and the full test suite — and the slices
  that change a by-name container copy also built the image and confirmed the
  code still loads inside it.
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
- **A file silently disappearing from the shipped container image** when it turns
  into a folder — because a file copied into the image by a filename pattern (or
  as a single named file) is no longer picked up once it becomes a folder, and the
  normal style check and tests pass anyway since they run against the source code,
  not the built image. Caught by updating the image's copy step in the same slice
  and building the image to confirm the code still loads.

## Anything for a human to decide?

No open question remains for a human. The person who requested the work locked
the scope to all 19 files with no trimming, and the ordering question is settled
too: because the shared allow-list forces the slices into one sequential chain,
there's no parallelism trade-off left to weigh. The plan is fully determined —
nothing here needs a human ruling before the work proceeds.

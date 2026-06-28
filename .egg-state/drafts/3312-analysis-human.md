# In plain terms — issue #3312

**Break up 19 oversized Python files into tidy, smaller pieces — without
changing how anything behaves — until none of them trips the project's
"this file is too big" guard anymore.**

## The problem

The project has a rule: no single source file should be longer than about
1,500 lines (or 100 KB). It's a guardrail against files growing so large that
nobody can hold them in their head. Today 19 files break that rule. They're
parked on an *allow-list* — a small file that says "we know these are too big,
let them through for now." That allow-list was meant to be temporary. The job
is to shrink every one of those 19 files until the allow-list can be emptied
completely.

Two of the files are in a league of their own:

- `orchestrator/routes/pipelines.py` — roughly **27,000 lines**.
- `gateway/gateway.py` — roughly **10,000 lines**.

The rest are smaller but still over the limit. The big two are the hard part,
and — this matters — they are **not** allowed to be skipped or pushed to a
"later" project. Every one of the 19 is in scope. (For the record, a few of the
files have quietly grown a little *larger* than the snapshot in the issue, so
the live measurements are what count, not the issue's table.)

## How each file gets broken up (the established recipe)

This isn't a free-for-all rewrite. The project already has a proven, written-down
way to split a large file, and we follow it exactly:

- Turn the one big file into a **small folder** (a "sub-package") of several
  smaller files.
- The folder gets a single front-door file (called `__init__.py`, the "barrel")
  whose only job is to re-export every public name the old file used to offer.
  Anything that imported the old file keeps working unchanged, because the
  front door still hands out all the same names.
- The actual code moves into smaller private files inside the folder (their
  names start with an underscore to signal "internal").
- Crucially, the existing tests keep working untouched. Many tests reach into
  the old file by name — for instance, to swap in a fake version of an internal
  helper. (There are a lot of these: well over fifty such named hooks point at
  the big pipelines file alone, and around a dozen at the gateway file.) The
  front door re-exports those names so every one of those tests still finds what
  it's looking for in the same place.

There are three concrete reference points to copy from, and they should be read
*before* starting:

1. The written recipe: `docs/guides/decomposition-pattern.md`.
2. A finished worked example already in the codebase: the `scripts/select_tests/`
   folder.
3. A merged example change to model the diff on: PR #2335.

## Why it's done in many small steps, not one big one

Each file is split on its own and lands as its own self-contained change. As each
file is finished:

- its entry is removed from the allow-list (so progress is visible and the guard
  tightens one notch at a time),
- the project's "maps" of how the code fits together are kept accurate. For each
  file that's split, a short entry describing its new internal layout is **added**
  to the relevant map (`orchestrator/CLAUDE.md`, `gateway/CLAUDE.md`, and a
  freshly-added section in `sandbox/CLAUDE.md`), and a brand-new map file —
  `shared/CLAUDE.md` — is **created** to cover the one in-scope file in that area.
- and the full build must be **green** — both the style checks (`make lint`) and
  the whole test suite (`make test-all`) — before that step is considered done.

> A small correction worth flagging: the issue assumes those two main maps still
> contain stale, leftover placeholder rows from an earlier attempt that need
> "retagging." A check of the current code shows that's no longer the case —
> there are no such leftover rows in those files. So the real work is *adding*
> fresh, accurate layout entries as each file lands, not cleaning up old ones.
> (Any genuinely stale references that do turn up while editing get tidied in
> passing, but they aren't the point.)

The independent files are ordered so they can land and deliver value on their own,
even if the two giant files turn out to be slow going. But "slow going" is the
only allowance — the giants still have to be finished, not dropped.

The single trickiest piece inside the 27,000-line file is a chunk called the
`_run_pipeline` phase-transition state machine — the part that walks a job through
its stages. The issue specifically requires this to be tackled head-on rather than
worked around or deferred.

## The golden rule: nothing changes behavior

This is a **pure reorganization**. Code moves between files; it does not change
what it does. If, while splitting a file, someone notices a genuine bug, they do
**not** fix it as part of this work — they write it up as a separate follow-up.
Mixing a behavior fix into a "we only moved things" change is exactly what makes
such changes risky and hard to review, so it's off-limits here.

All the work goes on branches whose names start with `egg/`.

## What "done" looks like

- All 19 files are below the size limit.
- The allow-list's list of exempted files is **empty**.
- The "how it fits together" maps are accurate — new layout entries added for
  every split file, plus the new `shared/CLAUDE.md`.
- No production code that imports these files breaks, and no test loses track of
  the internal hook it relies on.
- Every landing step passed lint and the full test suite.
- No behavior changed anywhere.

## Is there anything left for a human to decide?

For this particular task, the usual "how much should we bite off?" question has
**already been answered by the person who requested the work**: all 19 files,
including the two giant ones, with no trimming of the list. So there is no
scope-size decision to make here — the instruction is deliberately firm about
that, and the refiner confirmed there's no genuine open question to put to a
human at this stage. The room for judgment is entirely in the *ordering and
packaging* of the work (which file goes first, how the changes are grouped into
reviewable chunks), which is a planning detail handled in the next stage.

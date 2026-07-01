# In plain terms — issue #3393

**Let one pipeline make coordinated changes in several GitHub repositories at
once — opening one pull request per repo, in the right order — instead of
forcing the "other repo" half of a change to be done by hand.**

## The problem

Today a pipeline works on exactly one repository. But plenty of real changes
span two or more repos: a schema lives in repo A, the service that consumes it
lives in repo B. The pipeline can only handle repo B, so someone has to edit
repo A manually, merge it first, and coordinate the timing — one coherent
change gets split across an automated pipeline plus a human side-task.

## What changes

- A pipeline can be **submitted with a list of repos** (any number, each with
  its own base branch). Single-repo pipelines keep working exactly as before.
- The plan's units of work ("slices") each **belong to exactly one repo**.
  Cross-repo changes become two ordered slices — "add the new schema in
  repo A" then "migrate the consumer in repo B" — using the dependency
  mechanism that already exists.
- The pipeline opens **one PR per slice, in that slice's repo**, and the PRs
  reference each other.
- Every repo that ends up with work also gets its own **working branch and
  umbrella PR**, exactly like today's single repo does — so each repo keeps
  the same audit trail it has now. Repos submitted but left without work get
  neither.
- Work on a slice follows **that repo's own house rules** — its own
  instructions file, linters, and test commands — not the rules of whichever
  repo happens to be first in the list.
- Two safety rules are enforced at submission: all repos in a run must be
  **uniformly private or uniformly public** (no leaking private context into
  public surfaces), and all must use the **same auth mode** (mixing bot and
  user credentials is deferred).

## The good news (verified against the code)

Most of the plumbing is already multi-repo-ready: the machinery that checks
out code already accepts a list of repos, credentials already resolve per
repo, PR creation already takes a repo argument. The real gap is narrow: the
plan/contract has **no notion of which repo a slice belongs to**, and three
spots in the code quietly throw away everything after the *first* repo in the
list (a sweep at implementation time will catch any stragglers). Fixing that
gap — plus the new submission checks — is the heart of the work.

## The genuinely hard bits

1. **You cannot merge two PRs in two repos atomically.** GitHub doesn't work
   that way. The operator has now decided how v1 handles this: plain merge
   ordering is **automated** — the dependent slice is developed in parallel,
   its PR opened as a draft, and the pipeline automatically marks it ready
   the moment the upstream PR merges (a mechanical, observable signal; no
   human latency). A human steps in only when the wait is about **more than
   merge order** — e.g. waiting for repo A to publish a release, or choosing
   which released version repo B should pin to. Those judgment calls are
   released by an explicit human decision instead of fragile automation, and
   the same goes for the rare case where development itself genuinely cannot
   continue without the upstream artifact.
2. **The contract format is saved as JSON** and read by every phase, so
   adding the new "repo" field needs a careful, backward-compatible
   migration — fortunately the codebase has done this four times before.
3. One subtle trap found during grounding: the map from repo to checked-out
   folder is keyed by the repo's short name, so two repos that share a name
   (different owners) would collide. The operator has ruled on the fix: the
   map is **re-keyed by the full owner-plus-name** ("owner/repo"). Simply
   rejecting same-name combinations at submission was considered and ruled
   out — it would quietly break the "any number of repos" promise. If
   re-keying turns out to be far more work than expected, that comes back
   to the operator as a new decision, never as a silent fallback to
   rejection.

## Where decisions stand

Nothing above is provisional anymore. The operator has settled all the open
design questions, with the same binding force as the merge-ordering decision:

- **Merge ordering** (hard bit 1): automated draft-hold, human decisions
  only for beyond-merge-order conditions.
- **Branches and umbrella PRs**: one per repo that has work, none for repos
  that end up without any — as described above.
- **Folder-map collision** (hard bit 3): re-key by "owner/repo"; rejection
  shortcut ruled out.
- **Testing and review scope**: each slice is tested and reviewed inside its
  own repo only — no cross-repo diff in v1.
- **Naming and house rules**: the pipeline is named after the first repo in
  the list; branch naming is uniform; status displays list PRs per repo; and
  each slice follows its own repo's conventions.

Only a new operator decision can reopen any of these.

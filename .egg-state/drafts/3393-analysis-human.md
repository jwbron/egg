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
   that way. The pipeline can order the work and *hold* the dependent PR
   until the upstream one merges — the exact holding mechanism is an open
   decision for the operator (see the registered decision).
2. **The contract format is saved as JSON** and read by every phase, so
   adding the new "repo" field needs a careful, backward-compatible
   migration — fortunately the codebase has done this four times before.
3. One subtle trap found during grounding: the map from repo to checked-out
   folder is keyed by the repo's short name, so two repos that share a name
   (different owners) would collide. v1 must re-key it or reject that
   combination.

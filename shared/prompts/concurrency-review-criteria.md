<!-- Lens-specific review criteria for `reviewer_concurrency`.
     Consumed by the SDLC orchestrator's `_get_concurrency_review_criteria()` loader.
     Keep this file output-format-agnostic (no gh commands, no verdict JSON references). -->

Inherits from `code-review-criteria.md`; only lens-specific rules below override or extend it.

## Concurrency Lens — Scope

The concurrency reviewer is one of three lenses on the implement-phase change
set (`reviewer_code`, `reviewer_security`, `reviewer_concurrency`). Focus
**only on the concurrency lens** and defer code quality, security findings
(other than security-relevant races), and general correctness to
`reviewer_code` / `reviewer_security`.

The concurrency lens is **CRITICAL** — your NACK blocks consensus until the
producer re-proposes ([#2139](https://github.com/jwbron/egg/issues/2139),
closing [#1997](https://github.com/jwbron/egg/issues/1997)).

## What to Flag (in priority order)

The lens-specific rules below are **additive** to the base file. They name
the patterns most likely to slip past a single-pass review when the bug is a
multi-actor or temporal interaction rather than a defect in any one line.

### 1. Race conditions

Two concurrent code paths read/write shared state without an explicit
ordering. Common shapes:

- Producer pushes a commit and immediately calls `consensus propose` — a
  reviewer that polls in between may observe a propose for a SHA the
  branch has not yet seen ([#1925](https://github.com/jwbron/egg/issues/1925)).
- Two BRC reviewers race to ACK; the second writer overwrites a stale
  `proposal_version`.
- An agent reads `phase_configs` at start and another agent mutates it
  mid-phase; the reader keeps using the stale value.
- A test fixture creates a temp file in `setUp` while a parallel test
  worker overwrites it (xdist worker collisions).

Verification: identify each shared-state read and write the diff
introduces, then ask "what happens if these interleave in any order?"

### 2. Deadlocks

Mutual or cyclic waits where every participant is blocked on another:

- Two locks acquired in opposite orders by two code paths.
- An async `await` inside a held threading lock.
- A subprocess that the parent waits on synchronously while the
  subprocess waits on a pipe the parent never drains.
- BRC: producer A waits for ACK from reviewer B; reviewer B's
  `wait_for_event` is blocked on a message A is itself blocked on
  producing — see "BRC-protocol invariants" below.
### 3. Shared-state mutation without synchronization

Mutable global / module-level state read or written from multiple
threads, async tasks, or processes without a lock, channel, or
copy-on-write discipline (e.g. module-level `_CACHE = {}` written from
FastAPI request handlers; a `dataclass` mutated inside one of several
`Task` subagents; Pydantic models reused across requests without
`model_copy()`).

### 4. Async-context leakage

`asyncio` / `anyio` contexts mishandled in ways that surface as silent
message loss or resource exhaustion:

- A `Task` is created via `asyncio.create_task(...)` but the reference
  is dropped, so the GC cancels it before it completes.
- An `async with` context exits before its inner task finishes,
  cancelling background work the caller assumed had committed.
- Mixing sync (`requests`) and async (`httpx.AsyncClient`) calls in the
  same handler — the sync call blocks the event loop.
- An `asyncio.Lock` created at module import time inside a multi-loop
  test runner — bound to the wrong loop, silently no-ops.
- `time.sleep()` in async code paths.

### 5. Retry-storm patterns

New code that retries an external call without exponential backoff,
jitter, or a global ceiling:

- A `for _ in range(N)` retry loop with `sleep(1)` on a 503 response.
- Multiple agents whose polling cadences align on `:00` / `:30` minute
  marks (cross-fleet thundering herd — schedule jitter is the
  canonical mitigation).
- A reviewer that re-reviews on every push without debounce, causing
  N-reviewers × M-pushes of work.
- BRC: a producer that re-proposes on every NACK without honouring
  the `max_flip_flops=3` cap.

### 6. Resource-cleanup ordering

Resources released in the wrong order, conditionally, or not at all:

- A file handle opened inside `try` and closed only on the happy path.
- A subprocess spawned without a corresponding `terminate()` /
  `wait()` on the cancellation path.
- A pooled connection returned while a coroutine still holds a
  reference and uses it for the next request.
- A `tempfile.TemporaryDirectory` cleaned up while a child still has
  its CWD inside it.
### 7. BRC-protocol invariants

The BRC consensus protocol has temporal invariants that, when
violated, present as deadlocks or silent message loss. Flag any diff
that touches:

- **send→wait ordering and `--since` cursor threading.** Events that
  predate a `wait_for_event` call (including the caller's own
  just-sent message) are intentionally skipped. Zero-drop semantics
  across a send→wait boundary require threading the send's message ID
  through `--since <id>` ([#1925](https://github.com/jwbron/egg/issues/1925));
  flag any new call site that drops the cursor.
- **Heartbeat cadence and stall windows.** A handler that holds the
  event loop (or consensus mutex) past the stall window will be
  declared dead even though the work is still progressing
  ([#2012](https://github.com/jwbron/egg/issues/2012)). Flag any new
  long-running operation inside a heartbeat-bearing path.
- **`stale_reviewers` invalidation on re-propose.** A re-propose must
  invalidate prior ACKs at the older version; any path that skips
  this is a critical bug regardless of test coverage.
- **Flip-flop bound enforcement.** The `max_flip_flops=3` cap bounds
  producer/reviewer ping-pong; flag any change that weakens it.

## How to Review

1. Read the diff with explicit attention to **multi-actor** paths —
   FastAPI handlers, anything spawned via `subprocess` / `asyncio` /
   `Task`, anything that touches the BRC message bus.
2. For each shared-state location the diff reads or writes, articulate
   the synchronization mechanism (lock, channel, copy, idempotence).
   "Single-threaded by convention" is not a synchronization mechanism
   if any code path runs inside FastAPI, `pytest-xdist`, or a `Task`
   subagent.
3. For each new external call, articulate the retry policy, the
   timeout, and the failure mode. Missing timeouts on third-party
   calls are a blocking finding.
4. Cross-reference [`code-review-criteria.md`](./code-review-criteria.md)
   for the base review rules — verdict format, severity classification,
   and BRC ACK/NACK lifecycle inherit from there.

## What to Skip

- Pure single-threaded logic / data-shape bugs — defer to `reviewer_code`.
- Security vulnerabilities that are not race / ordering issues — defer
  to `reviewer_security`.
- Style / readability concerns around concurrency primitives — defer
  to `reviewer_code`.
- Issues already explicitly flagged by `reviewer_code` (acknowledge
  rather than duplicate).

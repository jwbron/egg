"""JIT-pull *queryable environment* renderers for the BRC event-pump (#3200, slice-5).

The slice-3 event-pump prompt (``orchestrator/routes/event_prompt.py``)
INLINES the bulk a review needs: the full per-producer ``git log
A..HEAD --not origin/base -p`` delta (``_render_producer_delta_section``)
and the 2 KB distilled BRC memory excerpt (``_render_memory_section``).
That bulk is the dominant, recency-driven driver of the resident
context: it is re-sent on every one-shot invocation and accumulates in
the resumed session until Claude Code's lossy ~95% auto-compaction
fires.

This module renders the *queryable environment* half of the #3200
context discipline: instead of inlining the bulk, the protected root
carries small, stable **POINTERS** to it — the exact ``git log`` recipe
(scoped by the #3189 deterministic anchors' last-reviewed + proposal
SHAs) and the served-read handles (``read_peer_artifact`` /
``GET /<pipeline_id>/brc-transcript``) — and the agent pulls the bulk
just-in-time only for the producer(s) the current event actually names.

**The honest limit (architect slice-5 goal; AC-2 part 2).** JIT pull
does NOT bound the context window. A slice the agent pulls stays
resident in the session until the next reseed/compaction, exactly like
the inlined bulk would have. What pull buys is a *lower resident root
cost* (pointers, not diffs) and *re-pull-ability* — when the slice-6
threshold reseed discards accumulated history and re-seeds a fresh
session from the protected root, the pointers survive and the bulk can
be pulled again on demand. **The reseed bounds the window; the pull
makes the reseed re-pull-able.** Do not mistake "moved to a pull" for
"bounded" — that conflation is the central tension the prototype must
keep honest.

Purity: this module renders POINTERS (recipe text + served-read
handles); it never runs ``git log`` and never reads the memory file.
Running the recipe is the agent's just-in-time action, so the bulk
bytes never enter the resident prompt at compose time. The renderers
are deterministic (sorted producers, stable wording, no timestamps) so
the rendered block is a cacheable prompt-prefix fragment, consistent
with the protected root it slots into.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "QUERYABLE_ENV_HONEST_LIMIT",
    "ProducerPullPointer",
    "enrichment_is_stale",
    "render_pull_handles",
    "render_queryable_env_section",
    "render_review_pull_recipe",
]


# The honest-limit notice, rendered verbatim into the pointer block AND
# kept as a module constant so the "pull does not bound the window,
# reseed does" invariant is recorded in one authoritative place (the
# task-5-1 acceptance: "comment records 'pull does not bound the window,
# reseed does'"). Both the rendered prose and this constant carry it so
# neither the agent reading the prompt nor a maintainer reading the code
# can miss it.
QUERYABLE_ENV_HONEST_LIMIT: str = (
    "Honest limit: pulling the delta/transcript does NOT bound your "
    "context window — a pulled slice stays resident until the next "
    "reseed (the #3200 threshold reseed re-seeds a fresh session from "
    "this protected root). The reseed bounds the window; the pull only "
    "lowers the resident root cost and makes the reseed re-pull-able. "
    "Pull only what THIS event needs."
)


@dataclass(frozen=True)
class ProducerPullPointer:
    """A single producer's JIT-pull pointer (no bulk bytes).

    Sourced from the #3189 deterministic anchors (slice-3): the
    ``last_reviewed_sha`` is this reviewer's last-reviewed commit for
    the producer (the delta's start ref) and ``proposal_sha`` is the
    producer's current pushed proposal (the delta's end ref — the
    reviewer's own worktree HEAD does NOT contain the producer's
    commits under per-role worktrees, so the end ref must be the
    proposal SHA, not ``HEAD``; #3076).
    """

    producer: str
    last_reviewed_sha: str = ""
    proposal_sha: str = ""


def render_review_pull_recipe(
    pointers: Sequence[ProducerPullPointer],
    base_branch: str,
) -> str:
    """Render the per-producer ``git log`` recipe POINTERS (not the diff).

    For each producer the block emits the exact command the agent runs
    to pull the full adversarial-re-review delta just-in-time::

        git log <last_reviewed_sha>..<proposal_sha> --not origin/<base> -p

    The command is rendered verbatim so the agent can audit the scope
    before pulling; the diff bytes themselves stay OUT of the resident
    prompt until the agent chooses to run it. Producers are sorted for
    byte-stability. When a producer has no recorded last-reviewed SHA
    (first review) the start ref falls back to the proposal's full
    branch history with an explicit note.
    """
    base_branch = (base_branch or "main").strip() or "main"
    ordered = sorted(pointers, key=lambda p: p.producer)
    if not ordered:
        return "(no producers in scope for this event — nothing to pull)"

    lines: list[str] = [
        "Pull the FULL re-review delta per producer with the exact "
        "recipe below (run it only for the producer(s) THIS event "
        "names). The delta is NOT inlined — running the recipe is your "
        "just-in-time pull:",
        "",
    ]
    for p in ordered:
        producer = (p.producer or "(unknown)").strip() or "(unknown)"
        start = (p.last_reviewed_sha or "").strip()
        end = (p.proposal_sha or "").strip() or "<proposal_commit_sha>"
        if start:
            recipe = f"git log {start}..{end} --not origin/{base_branch} -p"
            note = ""
        else:
            # No prior review: there is no start SHA to diff from, so the
            # recipe scopes to the proposal's full in-branch history.
            recipe = f"git log {end} --not origin/{base_branch} -p"
            note = "  (first review — no prior last-reviewed SHA; full branch history)"
        lines.append(f"- {producer}:")
        lines.append(f"    {recipe}")
        if note:
            lines.append(note)
    return "\n".join(lines)


def render_pull_handles(pipeline_id: str, *, phase: str = "implement") -> str:
    """Render the served-read handles for bulk BRC history / peer artifacts.

    Two handles, both already shipped, both pulled just-in-time:

    * ``read_peer_artifact`` (MCP ``mcp__brc__read_peer_artifact``) — the
      BRC message-record transcript + peer-artifact content for a phase.
    * ``GET /<pipeline_id>/brc-transcript`` (#3076/#3077) — the live
      served read of the in-flight phase's BRC transcript straight from
      the message store, for the phase currently in flight (the on-disk
      ``.egg-state/brc-history/`` files exist only after phase
      completion).

    ``pipeline_id`` is interpolated into the route so the agent has the
    concrete URL; ``phase`` defaults to ``implement`` (the only phase the
    event-pump runs review cycles in today) and is surfaced as the
    required query param.
    """
    pid = (pipeline_id or "<pipeline_id>").strip() or "<pipeline_id>"
    ph = (phase or "implement").strip() or "implement"
    lines = [
        "Bulk BRC history and peer-artifact content are NOT inlined — pull them on demand:",
        "",
        "- Peer artifacts + message transcript: "
        "``mcp__brc__read_peer_artifact`` (filter by ``peer_role`` / "
        "``message_type`` to pull only what you need).",
        f"- Live in-flight transcript: ``GET /{pid}/brc-transcript?"
        f"phase={ph}&role=<your-role>`` (served from the message store "
        "for the phase currently in flight).",
    ]
    return "\n".join(lines)


def render_queryable_env_section(
    *,
    pipeline_id: str,
    base_branch: str,
    pointers: Sequence[ProducerPullPointer],
    phase: str = "implement",
) -> str:
    """Assemble the full queryable-environment POINTER block.

    This is the JIT-pull replacement for the inlined
    ``_render_producer_delta_section`` + ``_render_memory_section``: the
    recipe pointers, the served-read handles, and the honest-limit
    notice — deterministic, small, and resident in the protected root.
    The bulk it points at is pulled just-in-time and is never inlined
    here.
    """
    recipe = render_review_pull_recipe(pointers, base_branch)
    handles = render_pull_handles(pipeline_id, phase=phase)
    return "\n\n".join(
        [
            "### Re-review delta (pull on demand)",
            recipe,
            "### Bulk history / peer artifacts (pull on demand)",
            handles,
            QUERYABLE_ENV_HONEST_LIMIT,
        ]
    )


def enrichment_is_stale(enrichment_sha: str, current_proposal_sha: str) -> bool:
    """True iff a SHA-stamped enrichment claim is stale vs the live delta.

    The #3188 agent-authored enrichment (BRC-memory ``codebase /change
    model`` prose and per-producer ``summary_of_assessment``) is treated
    as CLAIMS, not ground truth: each record is SHA-stamped with the
    proposal commit it was authored against (slice-5 task-5-2). When the
    producer re-proposes, the current proposal SHA advances past the
    enrichment's stamp and the claim must be re-verified against the
    fresh ``git log`` delta rather than trusted — a stale ``verified``
    claim must NOT suppress re-checking.

    A claim is stale when both SHAs are present and differ. A missing
    stamp (``""``) is treated as stale (fail-safe: an unstamped claim
    cannot be proven current, so re-verify). A missing current SHA is
    treated as NOT stale only when the enrichment has a stamp but we
    have nothing to compare against — but to stay fail-safe we bias to
    stale whenever we cannot positively confirm the stamp matches the
    live delta. The deterministic #3189 layer + the git-log delta remain
    authoritative either way; this helper only decides whether to TRUST
    the enrichment or re-derive.
    """
    stamp = (enrichment_sha or "").strip()
    current = (current_proposal_sha or "").strip()
    if not stamp:
        # Unstamped enrichment can never be proven current → re-verify.
        return True
    if not current:
        # No live delta to compare against → cannot confirm currency →
        # re-verify (fail-safe; never trust an unconfirmed claim).
        return True
    return stamp != current

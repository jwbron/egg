"""Durable BRC memory section — inline excerpt and JIT-pull pointer.

:func:`_render_memory_section` INLINES the 2 KB memory excerpt at the
user-prompt tail (legacy full-context path, architect od-6 Option B);
:func:`_render_memory_pointer_section` emits the on-disk path as a
JIT-pull POINTER (#3200 queryable-environment path). The inline renderer
is kept byte-for-byte so slice-9's feature flag preserves the OFF path.
AST-identical to the pre-split definitions — pure refactor (#3312
slice-6).
"""

from __future__ import annotations

from ._caps import MEMORY_EXCERPT_MAX_CHARS, _truncate


def _render_memory_section(memory_excerpt: str) -> str:
    """Render the durable BRC memory at the user-prompt tail position.

    Architect od-6 Option B: the memory excerpt is appended to the user
    prompt rather than injected via ``--append-context`` (which the
    illustrative pseudocode referenced but which does not exist on
    ``build_agent_command`` at ``shared/egg_agent/command.py:11-46``).
    Tail position keeps the surrounding event framing in the cacheable
    prefix while letting the memory bytes turn over without
    invalidating earlier turns.
    """
    truncated = _truncate(memory_excerpt or "", MEMORY_EXCERPT_MAX_CHARS)
    if not truncated.strip():
        return ""
    return "\n".join(
        [
            "## Durable BRC memory (tail-position context)",
            "",
            "This is your distilled state across prior BRC events for "
            "this slice — reuse the codebase / change-model section, "
            "the per-producer assessment, and the decision log to keep "
            "your verdict consistent across one-shot invocations. The "
            "writer caps the file at the cacheable-prefix budget; the "
            "tail-position delivery here keeps the rest of this prompt "
            "stable across re-entries (architect od-6 Option B).",
            "",
            "```markdown",
            truncated,
            "```",
            "",
        ]
    )


def _render_memory_pointer_section(memory_rel_path: str) -> str:
    """Render the durable BRC memory as a JIT-pull POINTER (#3200 slice-5).

    The new-discipline counterpart of :func:`_render_memory_section`,
    which INLINES a 2 KB memory excerpt into every one-shot prompt. Under
    the #3200 context discipline the memory file is #3188 agent-authored
    enrichment — CLAIMS, not ground truth — so it moves into the
    *queryable environment*: the prompt carries only a small pointer to
    the on-disk path and the agent reads it just-in-time, instead of the
    excerpt riding resident in every invocation.

    The pointer states the honest limit explicitly (a pulled excerpt
    stays resident until the slice-6 reseed — the pull does not bound the
    window) and the claims-not-ground-truth caveat (the file's summaries
    are SHA-stamped; a summary whose ``enrichment_sha`` predates the
    producer's current proposal SHA is stale and must be re-verified
    against the live ``git log`` delta, per
    ``egg_agent.queryable_env.enrichment_is_stale``).

    This is ADDITIVE: the legacy ``_render_memory_section`` is left
    byte-for-byte unchanged so slice-9's feature flag can preserve the
    OFF (full-context inline) path exactly. ``memory_rel_path`` empty ->
    section omitted.
    """
    rel = (memory_rel_path or "").strip()
    if not rel:
        return ""
    return "\n".join(
        [
            "## Durable BRC memory (pull on demand)",
            "",
            "Your distilled state across prior BRC events for this slice "
            "is NOT inlined — read it just-in-time only if you need it:",
            "",
            f"- Path: `{rel}`",
            "",
            "It is #3188 agent-authored enrichment: treat the "
            "``codebase / change model`` prose and each producer's "
            "``summary_of_assessment`` as CLAIMS, not ground truth. Each "
            "summary is SHA-stamped (``enrichment_sha``); when it predates "
            "the producer's current proposal SHA the claim is stale — "
            "re-verify against the live ``git log`` delta. The "
            "deterministic #3189 anchors are authoritative. Honest limit: "
            "reading this file makes its bytes resident until the next "
            "reseed; the pull does not bound the window, the reseed does.",
            "",
        ]
    )

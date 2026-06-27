"""``compose_event_prompt`` — assemble the per-event one-shot prompt.

Ties together the section renderers (``_render_*``) and enforces the
``PROMPT_ENVELOPE_MAX_BYTES`` envelope cap (excluding the delta) by
hard-truncating the largest variable-size section (NACKs / iteration
feedback) in priority order. AST-identical to the pre-split definition —
pure refactor (#3312 slice-6). The positional signature is fixed by the
slice-3 plan and the wrapper-bash call site; do not reorder it.
"""

from __future__ import annotations

from typing import Any

from ._caps import (
    _ENVELOPE_TRUNCATION_SENTINEL,
    _ITERATION_FEEDBACK_TRUNCATION_SENTINEL,
    PROMPT_ENVELOPE_MAX_BYTES,
)
from ._render_delta import _render_delta_pointer_section, _render_producer_delta_section
from ._render_event import _render_event_section
from ._render_memory import _render_memory_pointer_section, _render_memory_section
from ._render_nacks import _render_nacks_section
from ._render_task import _render_iteration_feedback_section, _render_task_section


def compose_event_prompt(
    role: str,
    event_payload: dict[str, Any] | None,
    memory_excerpt: str,
    nacks: list[dict[str, Any]] | None,
    git_log_delta: list[dict[str, Any]] | None,
    base_branch: str,
    *,
    task_description: str = "",
    iteration_feedback: dict[str, Any] | None = None,
    jit_pull: bool = False,
    memory_rel_path: str = "",
    pipeline_id: str = "",
) -> str:
    """Compose the per-event one-shot prompt the wrapper hands the agent.

    Positional signature is fixed by the slice-3 plan
    (TASK-3-1): ``(role, event_payload, memory_excerpt, nacks,
    git_log_delta, base_branch) -> str``. The wrapper bash invokes this
    via ``python3 -c`` so changing the positional order would silently
    break the call site; keep the order stable. New inputs go after the
    ``*`` as keyword-only with a safe default (see ``task_description``).

    Args:
        role: Agent role token (e.g. ``"coder"``, ``"reviewer_code"``).
            Surfaces in the role banner and in the "act per your role
            contract" framing.
        event_payload: The ``event_payload`` field returned by the
            orchestrator's ``brc next-action`` route. ``None`` is
            treated as an empty payload; ``action`` / ``type`` keys
            populate the event banner.
        memory_excerpt: Rendered markdown content of
            ``.egg-state/agent-outputs/<role>/brc-memory-<pipeline-id>.md``
            as read by the wrapper. Pass ``""`` (or anything that
            strips empty) when ``EGG_BRC_MEMORY!=full`` so the section
            is omitted.
        nacks: List of dicts in the shape of
            ``peer_consensus.py:_open_nacks_barrier_response`` (keys
            ``reviewer``, ``version``, ``reason``, ``artifact_refs``).
            Pass ``None`` or ``[]`` when no open-NACK barrier is in
            effect.
        git_log_delta: Per-producer rendered re-review deltas. Each
            entry is a dict with ``producer``,
            ``last_reviewed_commit_sha``, ``delta``. Pass ``None`` or
            ``[]`` for a producer event (no per-producer delta to
            surface).
        base_branch: Base branch the delta excludes (renders as
            ``--not origin/<base_branch>``). Usually ``main``.
        task_description: The contract's ``task_description`` (#3123) —
            the operator's submit-time task statement including any
            binding directives. Rendered (capped at
            ``TASK_DESCRIPTION_MAX_CHARS``) right after the event
            section so every one-shot invocation carries the operator's
            framing; pass ``""`` to omit (GitHub-issue pipelines, or a
            worktree without the contract file).
        iteration_feedback: The current phase's operator kickback
            (#3231) — the per-iteration ``request_changes`` /
            ``change_approach`` feedback the re-spawned agent must act on,
            threaded in by the orchestrator's ``next-action`` route from
            ``PhaseExecution.operator_directives`` / ``iteration_history``
            (#2795). A dict with an ``audience`` tag (``"producer"`` /
            ``"reviewer"``), ``directives`` (list of
            ``{feedback_text, iteration_n}``, oldest→newest) and — for the
            producer arm — an optional ``prior_iteration`` summary
            (``{iteration_n, verdict_matrix, nack_reasons,
            final_proposal_commit}``). Rendered right after the task
            section. The producer is told to address-or-rebut every
            directive before re-proposing; the reviewer (re-reviewing the
            producer's directive-driven change) is told to evaluate the
            draft against the directive rather than NACK it back toward
            the pre-directive rubric. Pass ``None`` / empty to omit (no
            kickback yet — the no-op golden-stable path).
        jit_pull: #3200 slice-5 queryable-environment toggle. ``False``
            (default) renders the legacy full-context INLINE path
            byte-for-byte unchanged — the per-producer ``git log`` diff
            and the 2 KB memory excerpt are inlined. ``True`` renders the
            bulk as JIT-pull POINTERS instead (the ``git log`` recipe +
            ``read_peer_artifact`` / ``brc-transcript`` handles for the
            delta; the memory file as an on-demand path), so only small
            pointers stay resident. The bulk stays reachable via the
            existing pull tools; the pull does NOT bound the window — the
            slice-6 reseed does. slice-9 sets this from its feature flag;
            until then the live CLI keeps the default so production
            behaviour is unchanged.
        memory_rel_path: Repo-relative path of the durable BRC memory
            file, rendered as the on-demand pointer when ``jit_pull`` is
            ``True``. Ignored on the legacy path (which inlines
            ``memory_excerpt`` instead). Empty omits the memory pointer.
        pipeline_id: Pipeline id interpolated into the ``brc-transcript``
            pull handle when ``jit_pull`` is ``True``. Empty renders a
            ``<pipeline_id>`` placeholder. Ignored on the legacy path.

    Returns:
        Rendered prompt string suitable for passing as the positional
        argument to ``python3 -m egg_agent``. The envelope (everything
        EXCLUDING the rendered delta) is bounded to ``PROMPT_ENVELOPE_MAX_BYTES``
        bytes — when the rendered envelope would exceed the cap the
        NACKs section (the variable-size driver per the reviewer's
        worked example: 6 reviewers × multi-KB reasons) is hard-truncated
        at the byte boundary with an explicit sentinel appended. The
        delta itself scales with the actual change and is emitted
        untruncated.
    """
    role = (role or "unknown").strip() or "unknown"
    base_branch = (base_branch or "main").strip() or "main"

    event_section = _render_event_section(role, event_payload)
    task_section = _render_task_section(task_description)
    iteration_section = _render_iteration_feedback_section(iteration_feedback)
    nacks_section = _render_nacks_section(nacks)
    # #3200 slice-5: when the queryable-environment discipline is enabled
    # (``jit_pull``), render the delta + memory as JIT-pull POINTERS
    # instead of inlining the bulk. The default (``False``) renders the
    # legacy inline path byte-for-byte unchanged so slice-9's feature
    # flag can preserve the OFF path exactly; slice-9 sets ``jit_pull``
    # from that flag. The live CLI keeps the default until then.
    if jit_pull:
        delta_section = _render_delta_pointer_section(git_log_delta, base_branch, pipeline_id)
        memory_section = _render_memory_pointer_section(memory_rel_path)
    else:
        delta_section, _delta_bytes = _render_producer_delta_section(git_log_delta, base_branch)
        memory_section = _render_memory_section(memory_excerpt)

    contract = "\n".join(
        [
            "## What to do",
            "",
            "Handle THIS single event per your role contract. Reuse the "
            "durable BRC memory below to keep your verdict consistent "
            "across one-shot invocations. When you have acted (proposed, "
            "ACKed, NACKed, or confirmed), exit naturally — the "
            "orchestrator derives ``egg-orch brc next-action`` in-process "
            "and re-spawns you one-shot with the next actionable event. "
            "Do NOT block on ``egg-orch message wait-loop`` yourself: the "
            "orchestrator owns the wait and spawns you one-shot per event "
            "(#3164).",
            "",
        ]
    )

    # Enforce the envelope cap (architect plan acceptance: "per-event
    # prompt envelope (excluding delta) ≤ 10 KB"). The envelope is the
    # sum of all sections EXCLUDING the rendered delta. We truncate the
    # variable-size sections in priority order: the NACKs section first
    # (the largest driver), then the iteration-feedback section if the
    # prompt still overshoots — a maximal task + maximal iteration
    # feedback with no/minimal NACKs can exceed the cap with nothing left
    # in NACKs to cut (#3231 review item 3). event/contract are bounded,
    # memory is already 2 KB capped and tail-positioned (od-6 contract),
    # and the task section carries the operator's submit-time framing we
    # keep intact. The truncation is byte-exact with ``errors="replace"``
    # so a UTF-8 multibyte sequence split at the boundary doesn't crash;
    # the sentinel's own byte length is subtracted from the per-section
    # budget so the post-truncation envelope honours the cap.
    def _envelope_bytes() -> int:
        present = [
            s
            for s in (
                event_section,
                task_section,
                iteration_section,
                nacks_section,
                contract,
                memory_section,
            )
            if s
        ]
        return sum(len(s.encode("utf-8")) for s in present) + max(0, len(present) - 1)

    def _shrink_to_fit(section: str, sentinel: str) -> str:
        """Byte-exact trim of ``section`` so the envelope honours the cap.

        ``sentinel`` is appended after the cut and its own byte length is
        reserved from the budget, so the post-truncation envelope still
        honours the cap. Each truncation candidate passes its own
        section-appropriate sentinel wording.
        """
        sentinel_bytes = len(sentinel.encode("utf-8"))
        others_bytes = _envelope_bytes() - len(section.encode("utf-8"))
        budget = max(0, PROMPT_ENVELOPE_MAX_BYTES - others_bytes - sentinel_bytes)
        raw = section.encode("utf-8")
        if len(raw) <= budget:
            return section
        return raw[:budget].decode("utf-8", errors="replace") + sentinel

    # Shrink the *largest* present truncation candidate first, re-measuring
    # after each cut. Cutting NACKs before iteration unconditionally (the
    # earlier two-`if` form) collapsed a small NACKs section to a bare
    # sentinel — losing the reviewer's actual NACK reasons — while the real
    # bloat (a ~4 KB iteration section) went untrimmed until the second pass
    # (#3231 re-review note 1). Picking the larger section each round cuts
    # the actual driver and only touches the smaller section if trimming the
    # larger one alone isn't enough.
    def _largest_candidate() -> str | None:
        candidates: list[tuple[str, int]] = []
        if nacks_section:
            candidates.append(("nacks", len(nacks_section.encode("utf-8"))))
        if iteration_section:
            candidates.append(("iteration", len(iteration_section.encode("utf-8"))))
        if not candidates:
            return None
        return max(candidates, key=lambda c: c[1])[0]

    while _envelope_bytes() > PROMPT_ENVELOPE_MAX_BYTES:
        which = _largest_candidate()
        if which == "nacks":
            shrunk = _shrink_to_fit(nacks_section, _ENVELOPE_TRUNCATION_SENTINEL)
            if shrunk == nacks_section:
                break  # already at its sentinel floor — nothing left to cut
            nacks_section = shrunk
        elif which == "iteration":
            shrunk = _shrink_to_fit(iteration_section, _ITERATION_FEEDBACK_TRUNCATION_SENTINEL)
            if shrunk == iteration_section:
                break
            iteration_section = shrunk
        else:
            break  # no truncation candidates left; fixed sections alone overshoot

    parts: list[str] = [event_section]
    if task_section:
        parts.append(task_section)
    if iteration_section:
        parts.append(iteration_section)
    if delta_section:
        parts.append(delta_section)
    if nacks_section:
        parts.append(nacks_section)
    parts.append(contract)
    if memory_section:
        parts.append(memory_section)

    return "\n".join(parts)

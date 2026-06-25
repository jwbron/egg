"""Per-event prompt composer for the BRC event-pump (#2908 slice-3 task-3-1).

The slice-2 event-pump wrapper (``orchestrator/consensus_wrapper.py``)
invokes the agent one-shot per actionable BRC event. Slice-2 shipped a
minimal stub prompt; this module is the slice-3 replacement.

The composer assembles the single user prompt the wrapper hands to
``python3 -m egg_agent`` for a given event. Memory continuity rides on
the durable per-role memory artifact written by slice-1
(``sandbox/egg_agent_tools/handlers/brc_memory.py``); for review events
the prompt also includes the FULL ``git log
{last_reviewed_commit_sha}..HEAD --not origin/{base_branch} -p`` delta
per producer so the re-review audits the actual delta — not just the
orchestrator-side ``changed_artifacts`` summary, which would
systematically weaken adversarial re-review (see
``shared/prompts/REVIEWER-SYNC.md`` + risk_analyst R6 from
the replan2 architect output).

Design choices encoded here:

* **Memory at tail position (architect od-6 Option B).** The memory
  excerpt is appended at the very end of the user prompt rather than
  passed through ``--append-context`` (which does not exist on
  ``build_agent_command`` at
  ``shared/egg_agent/command.py:11-46``). Tail position keeps the
  surrounding event-specific framing in the cacheable prefix while
  letting the memory bytes change without invalidating the cache for
  the prior turns.

* **Envelope budget (≤ 10 KB) excludes the git-log delta.** The delta
  scales with the actual change size; capping it would defeat the
  whole point of full-delta re-review. The 10 KB cap bounds only the
  surrounding prose so a chatty NACK reason can't push the cacheable
  prefix past a healthy size.

* **Memory excerpt cap at 2 KB.** Matches the slice-1 writer's caps on
  the codebase prose (2 KB) and per-producer summary (1 KB each) so a
  well-distilled file lands inside the budget; an over-stuffed file is
  truncated rather than rejected.

* **No ``changed_artifacts``-only shortcut.** The git-log delta MUST be
  the full ``git log {sha}..HEAD --not origin/{base_branch} -p`` output
  per producer. The composer renders the command verbatim alongside the
  rendered diff so the agent can audit the scope without re-deriving it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Cap on the inline memory excerpt — keep the per-event prompt within
# the cacheable prefix budget. Matches the slice-1 writer's
# ``_CODEBASE_PROSE_MAX_CHARS`` so a well-distilled file passes through
# unchanged. The architect's plan acceptance: "composer correctly
# truncates memory excerpts that exceed 2 KB".
MEMORY_EXCERPT_MAX_CHARS: int = 2000

# Cap on the prompt envelope EXCLUDING the git-log delta. The 10 KB
# bound is the architect's plan acceptance: "per-event prompt envelope
# (excluding delta) ≤ 10 KB". The delta itself scales with the
# change and is not counted. The cap is enforced in
# ``compose_event_prompt`` by hard-truncating the NACKs section (the
# variable-size driver — event/contract are bounded and memory is
# already 2 KB capped) when the rendered envelope would otherwise
# overflow. A pathological NACK payload (e.g. 6 reviewers each with a
# multi-KB ``reason``) would otherwise silently bloat the cacheable
# prefix.
PROMPT_ENVELOPE_MAX_BYTES: int = 10240

# Sentinel appended when the NACKs section is byte-truncated to keep
# the envelope under ``PROMPT_ENVELOPE_MAX_BYTES``. Mirrors the shape
# of ``_GIT_LOG_DELTA_MAX_BYTES``'s truncation marker so the agent
# sees the cut explicitly rather than reviewing a silently-clipped
# blocker list.
_ENVELOPE_TRUNCATION_SENTINEL: str = (
    "\n…(NACK list truncated — surrounding envelope exceeded "
    f"{PROMPT_ENVELOPE_MAX_BYTES} bytes; pull the full open-NACK "
    "barrier with ``egg-orch brc get-state`` if you need every "
    "blocker before re-proposing)\n"
)

# Cap on the inline copy of the contract's ``task_description`` (#3123).
# The full text stays one tool call away (``mcp__sdlc__show_contract``);
# this inline excerpt exists so the operator's task framing — including
# binding directives like "adopt prior branch X, do not reimplement" —
# is PUSHED into every one-shot invocation instead of relying on the
# agent pulling it per the rules file. 4 KB inside the 10 KB envelope
# leaves room for the event payload and the (separately truncatable)
# NACKs section.
TASK_DESCRIPTION_MAX_CHARS: int = 4000

_TASK_TRUNCATION_SENTINEL: str = (
    "\n…(task description truncated — read the full text with "
    "``mcp__sdlc__show_contract`` before making scope or adopt-vs-"
    "reimplement decisions)\n"
)

# Cap on the inline copy of the per-iteration operator feedback (#3231).
# The orchestrator-owned event-loop respawn path threads the operator's
# ``request_changes`` / ``change_approach`` kickback — recorded on
# ``PhaseExecution.operator_directives`` (#2795) — into the re-spawned
# producer's prompt via the ``next-action`` event_payload so the
# producer addresses (or explicitly rebuts) it before re-proposing,
# rather than re-reading its own prior draft and re-proposing it
# unchanged (the #1283 / #1915 fake-cycle class). The full directive
# history stays in ``PhaseExecution.operator_directives``; this inline
# excerpt carries the most recent directive (the one the producer must
# answer THIS round) plus a frozen summary of the prior iteration's
# verdicts/NACKs. 4 KB inside the 10 KB envelope leaves room for the
# event payload, the task section, and the (separately truncatable)
# NACKs section.
ITERATION_FEEDBACK_MAX_CHARS: int = 4000

_ITERATION_FEEDBACK_TRUNCATION_SENTINEL: str = (
    "\n…(operator feedback truncated — pull the full directive history "
    "with ``egg-orch brc get-state`` if you need every prior round before "
    "re-proposing)\n"
)


def _truncate(text: str, max_chars: int) -> str:
    """Trim ``text`` to ``max_chars`` characters with an ellipsis sentinel.

    The ellipsis character (``…``) is one Unicode code point but encodes
    to 3 bytes in UTF-8; we measure by ``len(str)`` (code points) here
    because the upstream cap is also expressed in code points. The 10 KB
    envelope assertion later uses bytes, so the truncation is
    conservative against the byte cap.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


# Keys carrying the variable-size NACK payload that ``_render_nacks_section``
# already renders. We strip these from the JSON copy in
# ``_render_event_section`` so the envelope-cap pass over ``nacks_section``
# is the single source of truth for the rendered NACK bytes.
_NACK_PAYLOAD_KEYS: tuple[str, ...] = ("nacks", "unresolved_nacks", "aggregated_nacks")


def _strip_nacks_for_json(event_payload: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``event_payload`` with NACK lists replaced.

    Each NACK key present in the original is replaced with a small
    cross-reference marker (``"<rendered in '## Open NACKs ...' section>"``
    plus the entry count) so the agent still sees that NACKs are
    attached and that the full payload lives in the dedicated section
    that the envelope-cap pass governs. The structural shape of the
    payload (keys, ordering, non-NACK values) is preserved so the agent
    can still inspect the rest of the JSON for context.
    """
    out: dict[str, Any] = {}
    for key, value in event_payload.items():
        if key in _NACK_PAYLOAD_KEYS and isinstance(value, list):
            out[key] = (
                f"<{len(value)} entr{'y' if len(value) == 1 else 'ies'} "
                "rendered in the '## Open NACKs against the current proposal "
                "version' section below; truncated under the envelope budget "
                "when oversized>"
            )
        else:
            out[key] = value
    return out


def _render_event_section(role: str, event_payload: dict[str, Any] | None) -> str:
    """Render the role banner + event description.

    The event payload is serialised as JSON (sorted keys, 2-space
    indent) so the rendering is deterministic — two callers with the
    same payload produce byte-identical output, which lets snapshot
    tests pin the shape without sensitivity to dict-iteration order.

    Variable-size NACK lists (``nacks`` / ``unresolved_nacks`` /
    ``aggregated_nacks``) are stripped from the JSON before rendering
    so the same data is not also embedded here — ``_render_nacks_section``
    is the single source of truth for the rendered NACK list, and it
    honours the ``PROMPT_ENVELOPE_MAX_BYTES`` truncation budget.
    Without this strip the NACK payload appears twice in the envelope
    (once as JSON here, once as markdown in nacks_section), defeating
    the envelope cap because the truncation pass only touches the
    nacks_section copy. The stripped keys are replaced with a
    cross-reference marker so the agent still sees that NACKs are
    attached and where to find them.
    """
    if event_payload is None:
        event_payload = {}
    action = ""
    if isinstance(event_payload, dict):
        # ``next-action`` puts the chosen verb under ``action`` (see
        # ``orchestrator/routes/consensus.py``'s ``_VALID_ACTIONS``).
        action = str(event_payload.get("action") or "")
        payload_for_json = _strip_nacks_for_json(event_payload)
    else:
        payload_for_json = event_payload
    payload_json = json.dumps(payload_for_json, indent=2, sort_keys=True)

    lines = [
        f"# BRC Event-Pump Handler — Role: {role}",
        "",
        f"You are the **{role}** agent. The wrapper has invoked you to "
        "handle ONE BRC event. Act on it according to your role contract, "
        "update durable BRC memory if you reach a verdict, then exit "
        "naturally. The wrapper will invoke you again with the next event.",
        "",
        "## Event",
        "",
        f"Action: **{action or '(unspecified)'}**",
        "",
        "Payload (JSON):",
        "```json",
        payload_json,
        "```",
        "",
    ]
    return "\n".join(lines)


def _render_producer_delta_section(
    git_log_delta: list[dict[str, Any]] | None,
    base_branch: str,
) -> tuple[str, int]:
    """Render the per-producer ``git log`` re-review-scope block.

    Each entry is a dict with keys ``producer``, ``last_reviewed_commit_sha``,
    ``proposal_commit_sha``, and ``delta``. The command form is rendered
    verbatim so the agent sees the exact scope (architect plan
    acceptance: "git-log delta command is emitted verbatim with the
    per-producer ``last_reviewed_commit_sha`` substituted in"); the
    rendered diff follows so the agent can audit the full change as a
    fresh review. ``proposal_commit_sha`` is used as the range end-ref
    (#3076) so the delta is scoped to the producer's pushed work
    instead of the reviewer's own HEAD; legacy payloads without it fall
    back to ``HEAD`` and the rendered caution.

    Returns ``(section_markdown, total_delta_bytes)`` so the caller can
    measure the delta separately from the envelope budget.
    """
    if not git_log_delta:
        return "", 0

    lines: list[str] = [
        "## Per-producer re-review delta",
        "",
        "For each assigned producer below, the wrapper has run the FULL "
        "delta-scoping command from the producer's "
        "``last_reviewed_commit_sha`` (stored in your durable BRC memory) "
        "to ``HEAD`` of the producer's branch, EXCLUDING commits already "
        "on the base branch. Audit the diff as a fresh review per "
        "``shared/prompts/REVIEWER-SYNC.md``: the named-blockers from "
        "your prior NACK MUST be addressed, AND any new findings the "
        "delta introduces are in scope. Both passes must succeed to ACK.",
        "",
    ]

    total_delta_bytes = 0
    for entry in git_log_delta:
        producer = str(entry.get("producer") or "(unknown)").strip()
        sha = str(entry.get("last_reviewed_commit_sha") or "").strip()
        proposal_sha = str(entry.get("proposal_commit_sha") or "").strip()
        delta = entry.get("delta") or ""
        if not isinstance(delta, str):
            delta = str(delta)
        # Command is emitted verbatim — the per-producer
        # ``last_reviewed_commit_sha`` and the proposal endpoint
        # substituted in so the agent can cross-check the scope against
        # the orchestrator's stored values. ``end_ref`` is the
        # producer's proposed commit when the payload carries one
        # (#3076); ``HEAD`` only on legacy payloads.
        cmd_sha = sha if sha else "<no prior review — full branch history>"
        end_ref = proposal_sha or "HEAD"
        cmd = f"git log {cmd_sha}..{end_ref} --not origin/{base_branch} -p"
        if delta.strip():
            delta_rendered = delta
        elif proposal_sha:
            delta_rendered = "(no commits in range — re-review is a no-op)"
        else:
            # Empty delta against the reviewer's own HEAD is NOT
            # evidence the producer didn't revise: per-role worktrees
            # mean the reviewer's HEAD never contains the producer's
            # commits (#3076 — the "re-review delta is empty" phantom
            # NACK). Only trust an empty range when it was scoped to
            # the producer's proposal SHA. When the wrapper's
            # ``sync_to_proposals`` could not merge the producer's
            # commit into your worktree (``unresolvable`` or
            # ``merge-failed``), #3077 slice-1 task-1-1 prepends a
            # ``worktree NOT synced to <sha>`` banner at the very top
            # of this prompt — check for it and re-run the
            # ``git log`` command rendered above against the producer's
            # branch directly rather than trusting your local diff.
            delta_rendered = (
                "(no commits in range — CAUTION: this range ended at YOUR "
                "worktree's HEAD, which does not contain the producer's "
                "commits. An empty delta here is NOT evidence the producer "
                "didn't revise. Check the TOP of this prompt for a "
                "``worktree NOT synced to <sha>`` banner (#3077 slice-1): "
                "when present, the wrapper could not sync your worktree to "
                "the producer's commit — re-run the ``git log`` command "
                "shown in the ``Re-review scope`` line above against the "
                "producer's branch (e.g. "
                "`git log <producer-branch-or-sha> --not "
                f"origin/{base_branch} -p`) rather than trusting your "
                "local diff. If no banner is present, the same fallback "
                "applies before issuing a verdict.)"
            )
        lines.extend(
            [
                f"### Producer: ``{producer}``",
                "",
                f"- last_reviewed_commit_sha: `{sha or '-'}`",
                f"- proposal_commit_sha: `{proposal_sha or '-'}`",
                "- Re-review scope (executed by the wrapper):",
                f"  `{cmd}`",
                "",
                "Delta:",
                "```diff",
                delta_rendered,
                "```",
                "",
            ]
        )
        total_delta_bytes += len(delta.encode("utf-8"))

    return "\n".join(lines), total_delta_bytes


def _render_nacks_section(nacks: list[dict[str, Any]] | None) -> str:
    """Render the open-NACK barrier payload (#2142).

    Mirrors the shape of
    ``orchestrator/peer_consensus.py:_open_nacks_barrier_response``:
    one dict per reviewer with ``reviewer``, ``version``, ``reason``,
    ``artifact_refs``. Each NACK's full ``reason`` is rendered verbatim
    so the producer's re-propose addresses every blocker, not just the
    most recent one.
    """
    if not nacks:
        return ""

    lines: list[str] = [
        "## Open NACKs against the current proposal version",
        "",
        "Two or more reviewers have NACKed the current proposal version; "
        "the orchestrator has surfaced them all here so the re-propose "
        "addresses every blocker in a single round-trip (#2142). A "
        "re-propose that resolves only one NACK is rejected with HTTP "
        "409 until all are addressed.",
        "",
    ]
    for nack in nacks:
        reviewer = str(nack.get("reviewer") or "?")
        version = nack.get("version", "?")
        reason = str(nack.get("reason") or "").rstrip()
        artifact_refs = nack.get("artifact_refs") or []
        if not isinstance(artifact_refs, list):
            artifact_refs = [artifact_refs]
        refs_rendered = ", ".join(str(r) for r in artifact_refs) if artifact_refs else "—"
        lines.append(f"### Reviewer: ``{reviewer}`` (v{version})")
        lines.append("")
        lines.append(f"- artifact_refs: {refs_rendered}")
        if reason:
            lines.append("- reason:")
            lines.append("")
            lines.append("  ```")
            for raw_line in reason.splitlines():
                lines.append(f"  {raw_line}")
            lines.append("  ```")
        else:
            lines.append("- reason: (none recorded)")
        lines.append("")
    return "\n".join(lines)


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


def _render_delta_pointer_section(
    git_log_delta: list[dict[str, Any]] | None,
    base_branch: str,
    pipeline_id: str = "",
) -> str:
    """Render the re-review delta as JIT-pull POINTERS (#3200 slice-5).

    The new-discipline counterpart of :func:`_render_producer_delta_section`,
    which INLINES the full per-producer ``git log`` diff (potentially
    hundreds of KB) into every one-shot prompt. Under the #3200 context
    discipline that bulk moves into the *queryable environment*: the
    prompt carries only the exact ``git log
    <last_reviewed>..<proposal> --not origin/<base> -p`` recipe (scoped
    by the #3189 anchors already in the payload) plus the served-read
    handles (``mcp__brc__read_peer_artifact`` /
    ``GET /<pipeline_id>/brc-transcript``), and the agent pulls the diff
    just-in-time only for the producer THIS event names.

    Honest limit (recorded here and in the rendered prose): JIT pull does
    NOT bound the context window — a pulled slice stays resident until the
    next reseed; the slice-6 reseed bounds the window, the pull only
    lowers the resident root cost and makes the reseed re-pull-able.

    Self-contained (no ``egg_agent`` import) because this module runs
    standalone via the wrapper bash — the same constraint that forces
    :func:`_issue_anchor_fallback` to duplicate
    ``compose_task_description``. The canonical renderer lives in
    ``egg_agent.queryable_env``; the wording is kept in sync deliberately.

    ADDITIVE: :func:`_render_producer_delta_section` is left byte-for-byte
    unchanged so slice-9's feature flag preserves the OFF (inline) path.
    """
    if not git_log_delta:
        return ""
    pid = (pipeline_id or "<pipeline_id>").strip() or "<pipeline_id>"
    base_branch = (base_branch or "main").strip() or "main"
    lines: list[str] = [
        "## Per-producer re-review delta (pull on demand)",
        "",
        "The full diff is NOT inlined. Pull it just-in-time with the "
        "exact recipe below, only for the producer(s) THIS event names:",
        "",
    ]
    for entry in sorted(git_log_delta, key=lambda e: str(e.get("producer") or "")):
        producer = str(entry.get("producer") or "(unknown)").strip() or "(unknown)"
        sha = str(entry.get("last_reviewed_commit_sha") or "").strip()
        proposal_sha = (
            str(entry.get("proposal_commit_sha") or "").strip() or "<proposal_commit_sha>"
        )
        if sha:
            recipe = f"git log {sha}..{proposal_sha} --not origin/{base_branch} -p"
        else:
            recipe = f"git log {proposal_sha} --not origin/{base_branch} -p"
        lines.append(f"### Producer: ``{producer}``")
        lines.append(f"- Pull the delta: `{recipe}`")
        lines.append("")
    lines.extend(
        [
            "Bulk BRC history and peer-artifact content are also NOT inlined — pull on demand:",
            "",
            "- Peer artifacts + message transcript: ``mcp__brc__read_peer_artifact``.",
            f"- Live in-flight transcript: ``GET /{pid}/brc-transcript?"
            "phase=implement&role=<your-role>``.",
            "",
            "Honest limit: pulling the delta/transcript does NOT bound "
            "your context window — a pulled slice stays resident until the "
            "next reseed. The reseed bounds the window; the pull only "
            "lowers the resident root cost and makes the reseed "
            "re-pull-able. Pull only what THIS event needs.",
            "",
        ]
    )
    return "\n".join(lines)


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


def _render_task_section(task_description: str) -> str:
    """Render the contract's ``task_description`` as a pushed section (#3123).

    The #3033/#3042 channel made the submit description reliably land in
    ``contract.task_description``, but delivery stayed pull-based: nothing
    in the per-event prompt or the role-scoped task views surfaced it, so
    an agent could complete a whole slice without ever reading the
    operator's directives (observed live: a slice coder reimplemented 12
    completed tasks from scratch past a prominent "ADOPT, DO NOT
    REIMPLEMENT" directive). This section closes the last hop by pushing
    the text into every one-shot invocation.

    Truncated at ``TASK_DESCRIPTION_MAX_CHARS`` with an explicit sentinel
    — the full text is one ``mcp__sdlc__show_contract`` call away.
    """
    if not (task_description or "").strip():
        return ""
    body = (task_description or "").strip()
    if len(body) > TASK_DESCRIPTION_MAX_CHARS:
        body = body[:TASK_DESCRIPTION_MAX_CHARS] + _TASK_TRUNCATION_SENTINEL
    return "\n".join(
        [
            "## Task & operator directives (contract ``task_description``)",
            "",
            "This is the operator's authoritative, submit-time task "
            "statement for the whole pipeline. It is BINDING for every "
            "event you handle: re-read it before structural decisions "
            "(what to adopt vs. implement from scratch, scope "
            "boundaries, hard requirements). If it conflicts with what "
            "you were about to do, the directive wins — course-correct "
            "or raise a HITL decision rather than proceeding.",
            "",
            body,
            "",
        ]
    )


def _directive_meta_tag(directive: dict[str, Any]) -> str:
    """Format a directive's iteration + timestamp as a parenthetical tag.

    Surfaces both the ``iteration_n`` ordering signal and the
    ``created_at`` wall-clock timestamp the route collects (#3231
    re-review note 1 — the timestamp was packed into the payload but
    never rendered). Returns ``""`` when neither is present so callers
    can append unconditionally without a dangling ``()``.
    """
    parts: list[str] = []
    it = directive.get("iteration_n")
    if it is not None:
        parts.append(f"iteration {it}")
    created = str(directive.get("created_at") or "").strip()
    if created:
        parts.append(created)
    return f" ({', '.join(parts)})" if parts else ""


def _render_iteration_feedback_section(iteration_feedback: dict[str, Any] | None) -> str:
    """Render the per-iteration operator kickback as a pushed section (#3231).

    Under ``EGG_EVENT_LOOP_OWNER=orchestrator`` the re-spawned producer's
    prompt is composed here (not via the in-pod ``_build_phase_iteration_context``
    path that already carries #2795's iteration context). Without this
    section the producer re-reads its own prior on-disk draft and
    re-proposes it byte-for-byte — the operator's ``request_changes`` /
    ``change_approach`` silently no-ops (the #1283 / #1915 fake-cycle
    class, regressed for the orchestrator-owned event loop).

    The orchestrator's ``next-action`` route attaches the current phase
    execution's ``operator_directives`` (chronological) + — for the
    producer ``propose`` arm — the latest ``iteration_history`` summary
    onto the event_payload as a serializable ``iteration_feedback`` dict;
    this renderer turns it into the markdown the agent sees. The
    ``audience`` key (``"producer"`` / ``"reviewer"``) selects the
    framing: the producer is told to address-or-rebut every directive
    before re-proposing (an unchanged re-propose is a defect); the
    reviewer (re-reviewing the producer's directive-driven change, #2795)
    is told to evaluate the draft *against* the directive rather than
    NACK it back toward the pre-directive default rubric. Only the most
    recent directive is rendered in full; earlier directives are
    summarised one line each so the precedence chain is visible without
    re-reading the whole history.

    Truncated at ``ITERATION_FEEDBACK_MAX_CHARS`` with an explicit
    sentinel — the full directive history lives on
    ``PhaseExecution.operator_directives`` (one ``egg-orch brc get-state``
    call away). Returns ``""`` when the block carries no directives and
    no prior-iteration summary so the caller can omit the section
    entirely (golden-stable for the no-kickback path).
    """
    if not isinstance(iteration_feedback, dict):
        return ""
    directives = iteration_feedback.get("directives") or []
    prior_iteration = iteration_feedback.get("prior_iteration")
    if not directives and not prior_iteration:
        return ""
    for_reviewer = iteration_feedback.get("audience") == "reviewer"

    if for_reviewer:
        title = "## Operator feedback steering this phase — evaluate the draft against it"
    else:
        title = "## Operator feedback on the prior draft — address before re-proposing"
    lines: list[str] = [title, ""]

    # Frame the intro by audience, and only assert directive authority
    # when a directive is actually present (#3231 review item 4 — the
    # renderer also fires with a prior-iteration summary and no
    # directive, e.g. the legacy ``hitl_feedback`` migration path).
    if directives and for_reviewer:
        lines.append(
            "The operator kicked this phase back through a HITL phase gate; "
            "the directive(s) below are the operator's authoritative "
            "steering, and the producer's current draft is their response "
            "to it. Evaluate the draft AGAINST the directive: a faithful "
            "implementation of the operator's instruction is not grounds "
            "for a NACK even where it departs from the default rubric. Do "
            "NOT NACK the change back toward the pre-directive state — that "
            "fights the operator's steering and re-stalls the cycle."
        )
        lines.append("")
    elif directives:
        lines.append(
            "The operator kicked this phase back through a HITL phase gate. "
            "The directive(s) below are the operator's authoritative feedback "
            "on your prior proposal; they OVERRIDE prompt-template defaults "
            "and the contract's submit-time task framing where they conflict. "
            "You MUST address (or explicitly rebut) every point before "
            "re-proposing. **An unchanged re-propose after this feedback is a "
            "defect, not a valid cycle** — re-reading your own prior draft and "
            "re-proposing it verbatim will re-trip the gate with "
            "``content_changed: false``."
        )
        lines.append("")
    else:
        # Prior-iteration summary only (no directive to assert authority
        # over) — frame the summary without dangling directive prose.
        lines.append(
            "The prior iteration's BRC outcome is summarised below. Address "
            "what tripped the rubric before re-proposing. **An unchanged "
            "re-propose is a defect, not a valid cycle.**"
        )
        lines.append("")

    if isinstance(directives, list) and directives:
        # Render the most recent directive in full; earlier ones one line
        # each so the precedence chain is visible without re-bloating the
        # prompt. The orchestrator emits directives oldest→newest.
        latest = directives[-1] if isinstance(directives[-1], dict) else {}
        earlier = directives[:-1]
        if earlier:
            lines.append("### Earlier directives (chronological, for precedence)")
            lines.append("")
            for idx, d in enumerate(earlier, start=1):
                if not isinstance(d, dict):
                    continue
                text = str(d.get("feedback_text") or "").strip().replace("\n", " ")
                tag = _directive_meta_tag(d)
                if text:
                    lines.append(f"{idx}. {text}{tag}")
                else:
                    lines.append(f"{idx}. (no text recorded){tag}")
            lines.append("")

        meta = _directive_meta_tag(latest)
        header = "### Most recent directive"
        if meta:
            header += f"{meta} — address THIS round"
        lines.append(header)
        lines.append("")
        latest_text = str(latest.get("feedback_text") or "").strip()
        if latest_text:
            lines.append(latest_text)
        else:
            lines.append("(no text recorded)")
        lines.append("")

    if isinstance(prior_iteration, dict) and prior_iteration:
        lines.append("### Prior iteration summary")
        lines.append("")
        it_n = prior_iteration.get("iteration_n")
        if it_n is not None:
            lines.append(f"Frozen snapshot of iteration {it_n}'s BRC outcome:")
            lines.append("")
        verdict_matrix = prior_iteration.get("verdict_matrix") or {}
        if isinstance(verdict_matrix, dict) and verdict_matrix:
            verdicts = "; ".join(
                f"{edge}: {state}" for edge, state in sorted(verdict_matrix.items())
            )
            lines.append(f"- Verdict matrix: {verdicts}")
        nack_reasons = prior_iteration.get("nack_reasons") or []
        if isinstance(nack_reasons, list) and nack_reasons:
            lines.append(f"- NACK reasons ({len(nack_reasons)}):")
            for reason in nack_reasons:
                lines.append(f"  - {reason}")
        # Surface the prior iteration's final proposal commit(s) for parity
        # with the in-pod renderer (#3231 review item 2) — the producer can
        # diff against this SHA to see exactly what it last proposed.
        final_commits = prior_iteration.get("final_proposal_commit") or {}
        if isinstance(final_commits, dict) and final_commits:
            commits = "; ".join(
                f"{producer}: {sha}" for producer, sha in sorted(final_commits.items())
            )
            lines.append(f"- Final proposal commit(s): {commits}")
        if not verdict_matrix and not nack_reasons and not final_commits:
            lines.append("- (no verdict/NACK detail recorded for the prior iteration)")
        lines.append("")

    rendered = "\n".join(lines)
    if len(rendered) > ITERATION_FEEDBACK_MAX_CHARS:
        rendered = rendered[:ITERATION_FEEDBACK_MAX_CHARS] + _ITERATION_FEEDBACK_TRUNCATION_SENTINEL
    return rendered


def _issue_anchor_fallback(contract_data: dict[str, Any]) -> str:
    """Build a minimal task anchor from the contract's ``issue`` info (#3163).

    Contracts written before #3163 carry ``task_description: null`` on
    GitHub-issue pipelines (the #3042 exclusion), which left the binding
    task section silently absent for the most common pipeline type —
    observed live as a refiner adopting the *previous* pipeline's stale
    draft as its task. New contracts get a composed statement at
    creation; this fallback covers the contracts already committed to
    live branches, which no creation-time fix reaches.

    Keep the wording in sync with
    :func:`egg_contracts.loader.compose_task_description`'s GitHub-issue
    branch — the two are deliberately near-identical (issue identity +
    ``gh issue view`` directive + "NOT your task" worktree disclaimer).
    They cannot share a helper because this module is invoked standalone
    by the wrapper bash (``python3 .../event_prompt.py``) with no package
    context, so it cannot import ``egg_contracts``. The only intentional
    divergences are the ``(title)`` clause and the "no operator task
    statement was recorded" note, both specific to the fallback path.
    """
    issue = contract_data.get("issue")
    if not isinstance(issue, dict):
        return ""
    number = issue.get("number")
    if not isinstance(number, int):
        return ""
    anchor = f"This pipeline's task is GitHub issue #{number}"
    url = issue.get("url")
    if isinstance(url, str) and url.strip():
        anchor += f" — {url.strip()}"
    title = issue.get("title")
    if isinstance(title, str) and title.strip() and title.strip() != f"Issue #{number}":
        anchor += f" ({title.strip()})"
    anchor += (
        f". No operator task statement was recorded on this contract; "
        f"fetch the live issue body (`gh issue view {number}`) before "
        "structural decisions. Worktree artifacts (drafts, agent outputs) "
        "that reference any other issue or pipeline are leftovers from "
        "previous runs — they are NOT your task."
    )
    return anchor


def _read_task_description(repo_path: Path) -> str:
    """Read ``task_description`` from the worktree contract file (#3123).

    Resolves the contract identifier from ``EGG_PIPELINE_ID`` /
    ``EGG_ISSUE_NUMBER`` (both exported into agent pods; the composer
    subprocess inherits them) and reads
    ``.egg-state/contracts/<key>.json`` directly — this script runs
    standalone via the wrapper bash, so it cannot import
    ``egg_contracts``. The contract file is committed and pushed to the
    assigned branch before any agent spawns, so a fresh worktree always
    carries it; ``task_description`` is written at contract creation and
    never mutated mid-phase, so the worktree copy cannot go stale.

    When the contract carries no ``task_description`` (pre-#3163
    GitHub-issue contracts) but does carry ``issue`` identity, a minimal
    anchor is synthesized so the task section is never silently empty
    for an issue-backed pipeline.

    Fail-soft like the memory reader above: a missing file, malformed
    JSON, or absent field yields ``""`` (section omitted) rather than
    failing the composer — the wrapper would otherwise fall back to the
    stub prompt and lose the delta/NACK sections too.
    """
    candidates: list[str] = []
    pipeline_id = (os.environ.get("EGG_PIPELINE_ID") or "").strip()
    if pipeline_id:
        candidates.append(f"{pipeline_id}.json")
    issue_number = (os.environ.get("EGG_ISSUE_NUMBER") or "").strip()
    if issue_number:
        # Mirrors egg_contracts.loader._canonical_key for int identifiers.
        candidates.append(f"issue-{issue_number}.json")

    contracts_dir = repo_path / ".egg-state" / "contracts"
    fallback = ""
    for name in candidates:
        try:
            raw = (contracts_dir / name).read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        task_description = data.get("task_description")
        if isinstance(task_description, str) and task_description.strip():
            return task_description.strip()
        if not fallback:
            fallback = _issue_anchor_fallback(data)
    return fallback


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
            "ACKed, NACKed, or confirmed), exit naturally — the wrapper "
            "polls ``egg-orch brc next-action`` and re-invokes you with "
            "the next actionable event. Do NOT block on "
            "``egg-orch message wait-loop`` yourself: the wrapper owns "
            "the wait and the heartbeat (#2908 slice-2).",
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


# ---------------------------------------------------------------------------
# Wrapper-bash CLI: ``python3 event_prompt.py <action>`` (slice-3 TASK-3-2)
# ---------------------------------------------------------------------------

# Sentinel value for the memory excerpt when ``EGG_BRC_MEMORY!=full``.
# The wrapper still writes through ``write-only`` (slice-1 default in
# slice-2), so the file may exist on disk; the reader path is gated
# separately so slice-4's default-on flip flips reads in one step.
_MEMORY_MODE_FULL = "full"

# Cap on a single ``git log`` subprocess in seconds. Long enough that a
# multi-megabyte delta against a slow filesystem still completes, short
# enough that a hung gateway doesn't deadlock the wrapper loop.
_GIT_LOG_TIMEOUT_SECS = 60

# Cap on the rendered ``git log`` output per producer (bytes). A
# pathologically large refactor could push a single delta past
# Claude's context budget regardless of the cacheable-prefix bound;
# we truncate with an explicit sentinel so the agent can detect the
# truncation rather than silently reviewing half a diff.
_GIT_LOG_DELTA_MAX_BYTES = 256 * 1024  # 256 KiB per producer

# Regex matching a slice-1 ``last_reviewed_commit_sha`` bullet. The
# slice-1 writer renders the value as either a 7-40 char SHA or the
# ``-`` sentinel for "no prior review" (see
# ``sandbox/egg_agent_tools/handlers/brc_memory.py::_render_assessment``).
_LAST_REVIEWED_SHA_RE = re.compile(r"^\s*-\s*last_reviewed_commit_sha\s*:\s*(\S+)\s*$")
_PRODUCER_HEADING_RE = re.compile(r"^\s*###\s+(.+?)\s*$")


def _parse_per_producer_sha(memory_text: str) -> dict[str, str]:
    """Extract ``{producer: last_reviewed_commit_sha}`` from memory text.

    Crude but stable parse against the slice-1 writer's rendered
    format. The writer guarantees a ``### <role>`` heading followed by
    a ``- last_reviewed_commit_sha: <value>`` bullet inside the
    ``## Per-producer assessment`` section; this scan walks the file
    once and pairs each heading with the first matching bullet that
    follows it.
    """
    out: dict[str, str] = {}
    current_producer: str | None = None
    for raw_line in memory_text.splitlines():
        heading = _PRODUCER_HEADING_RE.match(raw_line)
        if heading:
            current_producer = heading.group(1).strip().strip("`") or None
            continue
        sha_match = _LAST_REVIEWED_SHA_RE.match(raw_line)
        if sha_match and current_producer:
            value = sha_match.group(1).strip()
            # ``-`` sentinel = "no prior review"; skip so the wrapper
            # doesn't render ``git log -..HEAD``.
            if value and value != "-":
                out[current_producer] = value
            # First match wins per producer — same shape the slice-1
            # writer produces (one bullet per heading).
            current_producer = None
    return out


def _pipeline_id_token() -> str:
    """Resolve the validated pipeline-id token from env, or ``""`` when unusable.

    ``EGG_PIPELINE_ID`` with an ``issue-<EGG_ISSUE_NUMBER>`` fallback (the
    pod-inherited identifiers; see ``_read_task_description`` /
    ``_memory_path``). Returns ``""`` when neither is set or the token carries
    a character outside ``[A-Za-z0-9_-]`` — the same fail-soft validation the
    #3163 memory filename uses, so a malformed id never leaks into a path or a
    rendered pull handle. Shared by :func:`_memory_path` and the slice-9
    ``jit_pull`` wiring (the ``pipeline_id`` interpolated into the
    ``brc-transcript`` handle) so both resolve the id identically.
    """
    pipeline_id = (os.environ.get("EGG_PIPELINE_ID") or "").strip()
    if not pipeline_id:
        issue_number = (os.environ.get("EGG_ISSUE_NUMBER") or "").strip()
        if issue_number:
            pipeline_id = f"issue-{issue_number}"
    if not pipeline_id or not all(ch.isalnum() or ch in "_-" for ch in pipeline_id):
        return ""
    return pipeline_id


def _memory_path(repo_path: Path, role: str) -> Path | None:
    """Resolve the pipeline-scoped BRC memory file path (#3163).

    Mirrors ``sandbox/egg_agent_tools/handlers/brc_memory.py::
    memory_path_for_role``: the filename carries the pipeline id
    (``brc-memory-<pipeline-id>.md``) so a fresh pipeline never inlines
    a previous pipeline's memory — role-only keying let memory files
    merged to main via context PRs seed later pipelines' prompts with
    the wrong pipeline's distilled state. The id resolves via
    :func:`_pipeline_id_token` (``EGG_PIPELINE_ID`` with an
    ``issue-<EGG_ISSUE_NUMBER>`` fallback), matching the contract-file
    resolution above.

    Fail-soft (unlike the sandbox writer, which raises): no resolvable
    pipeline id or a malformed token yields ``None`` — the composer
    omits the memory section rather than reading a shared file.
    """
    pipeline_id = _pipeline_id_token()
    if not pipeline_id:
        return None
    return repo_path / ".egg-state" / "agent-outputs" / role / f"brc-memory-{pipeline_id}.md"


def _read_memory_excerpt(memory_path: Path, mode: str) -> str:
    """Read ``memory_path`` iff ``EGG_BRC_MEMORY`` is ``full``.

    Slice-1 ships with the writer in ``write-only`` mode by default;
    the reader path is gated separately so slice-4 can flip the default
    in one place without touching slice-3's wiring. ``mode`` is the
    normalised env value (lowercased, stripped).
    """
    if mode != _MEMORY_MODE_FULL:
        return ""
    try:
        return memory_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def _run_git_log(
    sha: str,
    base_branch: str,
    repo_path: Path,
    end_ref: str = "HEAD",
) -> str:
    """Render ``git log {sha}..{end_ref} --not origin/{base_branch} -p``.

    Runs the subprocess in ``repo_path``. The gateway allows
    ``git log`` with ``-p`` / ``--patch`` and ``--not`` flags (see
    ``gateway`` allow-list; #2905). On non-zero rc or timeout we
    return a sentinel string so the agent can audit the failure
    explicitly rather than silently reviewing an empty diff.

    ``end_ref`` defaults to ``HEAD`` for legacy payloads, but callers
    should pass the producer's ``proposal_commit_sha`` when the event
    payload carries one (#3076): the reviewer's own HEAD does not
    contain the producer's commits (per-role worktrees), so a
    ``{sha}..HEAD`` range in the reviewer's worktree is empty even
    when the producer revised — the "re-review delta is empty"
    phantom-NACK. The proposal SHA resolves from any agent worktree
    because all per-role worktrees share the host repo's object store.
    """
    cmd = [
        "git",
        "log",
        f"{sha}..{end_ref}",
        "--not",
        f"origin/{base_branch}",
        "-p",
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"(git log timed out after {_GIT_LOG_TIMEOUT_SECS}s for {sha}..{end_ref})"
    except OSError as exc:  # pragma: no cover — defensive
        return f"(git log failed: {exc})"

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return f"(git log returned rc={result.returncode}: {stderr or 'no stderr'})"

    payload = result.stdout or ""
    encoded = payload.encode("utf-8")
    if len(encoded) > _GIT_LOG_DELTA_MAX_BYTES:
        # Truncate at the byte cap and re-decode with replacement so a
        # UTF-8 multibyte sequence split at the boundary doesn't crash
        # the rendering. The truncation sentinel is on its own line so
        # the agent sees the cut explicitly.
        truncated = encoded[:_GIT_LOG_DELTA_MAX_BYTES].decode("utf-8", errors="replace")
        return (
            truncated
            + "\n…(truncated — delta exceeded "
            + f"{_GIT_LOG_DELTA_MAX_BYTES} bytes; the agent should pull "
            + "the full delta with the command above if a thorough audit is required)\n"
        )
    return payload


def _build_delta_entries(
    *,
    action: str,
    role: str,
    base_branch: str,
    repo_path: Path,
    memory_text: str,
    event_payload: Any = None,
) -> list[dict[str, Any]]:
    """Render per-producer deltas for the current action.

    For review actions (``ack`` / ``nack``) we render one delta per
    producer **scoped to the current event** (the producer(s) named
    in ``event_payload.pending_reviews``). For producer actions
    (``propose`` / ``confirm``) there is no per-producer delta — the
    producer just looks at HEAD.

    **Scoping invariant (reviewer_code_holistic v2 finding #3).** The
    memory file accumulates a per-producer ``last_reviewed_commit_sha``
    for every producer the reviewer has ever ACK/NACKed. The current
    event names *one* producer (or a small set in
    ``pending_reviews``) — the agent's job this invocation is to
    review THAT producer's latest delta, not to be handed stale
    deltas for unrelated prior producers. Treat the memory file as a
    per-producer LOOKUP keyed by the current event's producers, not
    as an ENUMERATION source. When the event payload doesn't name
    producers (legacy / synthetic / test paths) we fall back to
    rendering all stored SHAs — this preserves backward compatibility
    for callers that bypass the next-action route.

    **``changed_artifacts`` fallback (plan TASK-3-2 acceptance).**
    For each scoped producer with no stored SHA (first-ever ACK,
    parse failure, file missing) the renderer falls back to the
    orchestrator's signal-level artifact list. The fallback is sourced
    in priority order from (a) ``event_payload.pending_reviews[i].
    artifact_refs`` (the next-action route enriches this from
    ``PeerConsensusTracker.get_current_proposal_snapshot``), and (b)
    the top-level ``event_payload.changed_artifacts`` key (legacy /
    test path). The fallback is explicitly labelled as a degraded
    baseline so the agent does not mistake it for an
    adversarial-re-review-grade diff. The documenter's docs at
    ``docs/architecture/orchestrator.md`` and
    ``docs/reference/agent-wait-patterns.md`` describe the same
    fallback: "strictly a degraded baseline, not the adversarial
    re-review path".

    ``role`` is currently unused inside the function body; the
    parameter is retained for the call-site symmetry (architect
    plan: the composer signature passes role through alongside
    action). Once the next-action route is the only producer of
    event_payload, ``role`` can be re-purposed as the reviewer-role
    half of the (reviewer, producer) relationship; for now the
    producer derivation is event-payload-driven.
    """
    del role  # see docstring — kept for call-site symmetry
    if action not in ("ack", "nack"):
        return []

    per_producer_sha = _parse_per_producer_sha(memory_text)

    # Scope the producer set to the current event. Falls back to ALL
    # stored producers in memory when the event payload doesn't name
    # any (legacy callers, synthetic test payloads).
    scoped_producers = _extract_current_producers(event_payload)
    if not scoped_producers:
        scoped_producers = sorted(per_producer_sha.keys())

    out: list[dict[str, Any]] = []
    for producer in scoped_producers:
        sha = per_producer_sha.get(producer, "")
        # The producer's proposed commit SHA from pending_reviews
        # (#3076). When present it is BOTH the delta endpoint (the
        # reviewer's own HEAD never contains the producer's commits —
        # per-role worktrees — so ``{sha}..HEAD`` was empty even after
        # a revision) and the anchor for ``git show <sha>:<path>``
        # artifact reads, which resolve from any agent worktree via
        # the shared host object store.
        proposal_sha = _extract_proposal_sha_for_producer(event_payload, producer)
        if sha:
            delta = _run_git_log(
                sha,
                base_branch,
                repo_path,
                end_ref=proposal_sha or "HEAD",
            )
            out.append(
                {
                    "producer": producer,
                    "last_reviewed_commit_sha": sha,
                    "proposal_commit_sha": proposal_sha,
                    "delta": delta,
                }
            )
            continue

        # Per-producer fallback — no recorded SHA for this producer.
        # Prefer per-producer artifact_refs from pending_reviews; fall
        # back to the legacy top-level changed_artifacts key.
        artifacts = _extract_artifacts_for_producer(event_payload, producer)
        if not artifacts and not proposal_sha:
            continue
        # Strip backticks from artifact paths before interpolation. The
        # paths are producer-supplied through ``snapshot["artifacts"]``;
        # ``proposal_sha`` is hex-validated upstream, but ``a`` is not.
        # The agent (not bash) is the consumer here so this is not a
        # shell-injection vector, but a stray backtick in a path would
        # break the markdown code span the agent renders. Defensive
        # belt-and-braces rather than trusting producer payloads.
        artifacts = [a.replace("`", "") for a in artifacts]
        refs_text = "\n".join(f"- `{a}`" for a in artifacts)
        if proposal_sha:
            # First review with a known proposal SHA. The producer's work
            # is NOT in this reviewer's worktree — per-role worktrees are
            # isolated, and #3216 (WS1 of #3209) stops syncing peer trees
            # into read-only reviewers — so render served reads keyed by
            # artifact NAME rather than path-bearing `git show <sha>:<path>`
            # commands. `egg-artifact` resolves the repo path server-side
            # from the spec registry and streams the committed bytes at
            # <sha>, regardless of whether the commit resolves in this
            # worktree's object store (the #3002 split-store case the old
            # `git show` channel breaks on). Registered coordination
            # artifacts get a per-name read; anything unregistered is
            # covered by the full proposed-change delta below.
            from egg_contracts.artifact_spec import name_for_path

            read_names: list[str] = []
            for a in artifacts:
                name = name_for_path(a)
                if name and name not in read_names:
                    read_names.append(name)
            read_cmds = "\n".join(
                f"- `egg-artifact get {name} --ref {proposal_sha}`" for name in read_names
            )
            fallback_delta = (
                "(No `last_reviewed_commit_sha` recorded yet for this "
                "producer — this is your FIRST review of this proposal. "
                "The producer's work is NOT in your working tree; per-role "
                "worktrees are isolated. Read each registered coordination "
                f"artifact at the proposed commit `{proposal_sha}` via the "
                "served read — it resolves the artifact server-side from "
                "its spec-registered name, no local checkout required:)\n\n"
                + (f"Proposed artifacts:\n{read_cmds}\n\n" if read_cmds else "")
                + "Full proposed change:\n"
                f"- `git log {proposal_sha} --not origin/{base_branch} -p`\n\n"
                "Do NOT NACK for a missing file before reading it via "
                "these commands — a plain `Read` of the path in your own "
                "worktree is expected to fail and is not evidence the "
                "artifact does not exist.\n"
            )
        else:
            # No proposal SHA in the payload AND no recorded
            # ``last_reviewed_commit_sha`` for this producer — degraded
            # baseline. #3077 slice-5 task-5-1 deletes the prior
            # "fetch and read the actual file diffs yourself" prose:
            # per-role worktrees mean the agent CANNOT recover the
            # producer's work via its own ``git fetch`` (the producer's
            # commits live in the host object store, which the wrapper
            # syncs into the agent worktree before this prompt is
            # rendered when a proposal SHA is known). With no SHA there
            # is nothing to render a ``git show`` against, so we surface
            # the orchestrator's signal-level artifact list and stop —
            # the agent must NOT self-fetch.
            fallback_delta = (
                "(No `last_reviewed_commit_sha` recorded yet for this "
                "producer and no proposal SHA in the event payload — "
                "falling back to the orchestrator's signal-level "
                "`changed_artifacts` list as a degraded baseline. This is "
                "NOT the adversarial-re-review path.)\n\n"
                f"Artifacts the orchestrator flagged as changed:\n{refs_text}\n"
            )
        out.append(
            {
                "producer": producer,
                "last_reviewed_commit_sha": "",
                "proposal_commit_sha": proposal_sha,
                "delta": fallback_delta,
            }
        )
    return out


def _extract_changed_artifacts(event_payload: Any) -> list[str]:
    """Pull a top-level ``changed_artifacts`` list out of the event payload.

    Defensive against schema drift — non-list shapes coerce to empty
    rather than raising. Entries are stringified so a future schema
    surfaces structured artifact refs (e.g. dicts with role / path)
    without crashing the renderer.

    NB: the production payload from
    ``orchestrator/routes/consensus.py::_derive_next_action`` does NOT
    emit a top-level ``changed_artifacts`` key — reviewer-side
    fallback should prefer ``_extract_artifacts_for_producer`` which
    walks the ``pending_reviews[i].artifact_refs`` enrichment surfaced
    by the next-action route. This helper remains as the legacy /
    test-payload entry point.
    """
    if not isinstance(event_payload, dict):
        return []
    raw = event_payload.get("changed_artifacts")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item is not None and str(item).strip()]


def _extract_current_producers(event_payload: Any) -> list[str]:
    """Return the producer roles named by the *current* event.

    Walks the next-action route's payload shapes in priority order:

    * ``pending_reviews`` — the reviewer-side payload key emitted when
      one or more producers have proposals awaiting this reviewer's
      verdict. Each entry is ``{producer, current_version,
      prior_version, prior_verdict, artifact_refs?}``.
    * ``producer`` / ``producer_role`` — the producer-side payload key
      naming the agent's own producer slot (e.g. on a re-propose with
      ``unresolved_nacks``).

    Returns an empty list when no producer can be identified — the
    caller treats that as "legacy / synthetic payload, fall back to
    enumerating all stored memory SHAs" (backward-compat for callers
    that bypass the next-action route).

    De-dupes while preserving first-seen order so the rendered prompt
    sections are stable across invocations.
    """
    if not isinstance(event_payload, dict):
        return []

    seen: list[str] = []
    pending = event_payload.get("pending_reviews")
    if isinstance(pending, list):
        for entry in pending:
            if not isinstance(entry, dict):
                continue
            producer = entry.get("producer") or entry.get("producer_role")
            if not isinstance(producer, str):
                continue
            producer = producer.strip()
            if producer and producer not in seen:
                seen.append(producer)

    if not seen:
        raw = event_payload.get("producer") or event_payload.get("producer_role")
        if isinstance(raw, str) and raw.strip():
            seen.append(raw.strip())

    return seen


def _extract_proposal_sha_for_producer(event_payload: Any, producer: str) -> str:
    """Pull the producer's proposed commit SHA from the event payload.

    Reads ``pending_reviews[i].proposal_commit_sha`` for the entry whose
    ``producer`` matches (#3076) — the enrichment added by the
    next-action route from
    ``PeerConsensusTracker.get_current_proposal_snapshot``. Returns
    ``""`` when the payload carries no SHA for the named producer
    (legacy payloads, synthetic test paths), in which case callers fall
    back to the pre-#3076 behaviour (``HEAD`` delta endpoint / the
    degraded artifact-list fallback).

    The value is sanitised to a hex-ish token before being embedded in
    rendered shell commands: anything that is not a 7-64 char hex
    string is discarded rather than interpolated.

    Asymmetric regex with
    ``orchestrator/attestation_schemas.py::ProposalPayload.validate_commit_sha_format``
    is intentional: that writer-side validator uses a loose
    ``[A-Za-z0-9_]{7,64}`` so reconstruction sentinels (e.g.
    ``RECONSTRUCTED_NO_SHA``) round-trip through
    ``_proposal_commit_shas`` to non-shell consumers; this reader-side
    check is the strict hex-only shell-interpolation boundary that
    rejects those sentinels before any rendered ``git`` command sees
    them. Do not unify — tightening the writer breaks the sentinel
    round-trip; loosening this reader re-opens the shell-injection gap.
    """
    if not isinstance(event_payload, dict) or not isinstance(producer, str):
        return ""
    producer = producer.strip()
    if not producer:
        return ""
    pending = event_payload.get("pending_reviews")
    if not isinstance(pending, list):
        return ""
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        entry_producer = entry.get("producer") or entry.get("producer_role")
        if not isinstance(entry_producer, str) or entry_producer.strip() != producer:
            continue
        raw = entry.get("proposal_commit_sha")
        if isinstance(raw, str):
            candidate = raw.strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,64}", candidate):
                return candidate
        return ""
    return ""


def _extract_artifacts_for_producer(event_payload: Any, producer: str) -> list[str]:
    """Pull the artifact list for a specific producer from the payload.

    Priority order (reviewer_code_holistic v2 finding #1 — wire the
    per-producer artifact_refs the next-action route now emits, not
    the never-emitted top-level ``changed_artifacts`` key):

    1. ``pending_reviews[i].artifact_refs`` where ``entry.producer ==
       producer`` — the production reviewer-side payload, enriched by
       ``_has_pending_peer_proposals`` from
       ``PeerConsensusTracker.get_current_proposal_snapshot``.
    2. Top-level ``event_payload.changed_artifacts`` if the producer
       in the payload's top-level ``producer`` / ``producer_role`` key
       matches — preserved for legacy / synthetic test paths.

    Returns ``[]`` when no artifacts can be associated with the named
    producer.
    """
    if not isinstance(event_payload, dict) or not isinstance(producer, str):
        return []
    producer = producer.strip()
    if not producer:
        return []

    pending = event_payload.get("pending_reviews")
    if isinstance(pending, list):
        for entry in pending:
            if not isinstance(entry, dict):
                continue
            entry_producer = entry.get("producer") or entry.get("producer_role")
            if not isinstance(entry_producer, str) or entry_producer.strip() != producer:
                continue
            raw = entry.get("artifact_refs")
            if isinstance(raw, list):
                refs = [str(item) for item in raw if item is not None and str(item).strip()]
                if refs:
                    return refs

    # Legacy / synthetic-test fallback: only honour the top-level
    # ``changed_artifacts`` when the payload's top-level producer
    # matches the requested producer.
    top_producer = event_payload.get("producer") or event_payload.get("producer_role")
    if isinstance(top_producer, str) and top_producer.strip() == producer:
        return _extract_changed_artifacts(event_payload)
    return []


def _extract_producer_role(event_payload: Any) -> str:
    """Pull the producer role from the event payload, defaulting to
    ``(unknown producer)`` so the fallback entry still renders a
    label rather than a blank string.
    """
    if not isinstance(event_payload, dict):
        return "(unknown producer)"
    raw = event_payload.get("producer") or event_payload.get("producer_role")
    if not isinstance(raw, str) or not raw.strip():
        return "(unknown producer)"
    return raw.strip()


def _extract_nacks(event_payload: Any) -> list[dict[str, Any]]:
    """Pull a structured open-NACK list out of the event payload.

    The orchestrator's ``next-action`` route surfaces the open-NACK
    barrier (``orchestrator/peer_consensus.py:_open_nacks_barrier_response``)
    inside the event_payload when the action verb is ``propose`` on a
    re-propose. Three keys are accepted so the surface naming can evolve
    without breaking the wrapper:

    * ``nacks`` — the canonical key from ``_open_nacks_barrier_response``
      used for the 2+-reviewer barrier shape.
    * ``unresolved_nacks`` — the key emitted by ``next-action``'s
      single-reviewer NACK path (``orchestrator/routes/consensus.py``
      ``_derive_next_action`` lines 329-348). This is the common case
      for producer re-propose events: a single reviewer NACK does not
      trigger the open-NACK barrier (which requires 2+ distinct
      reviewers) but still carries reviewer ``reason`` /
      ``artifact_refs`` the producer needs to address. Omitting this
      key silently dropped single-reviewer NACK feedback from the
      per-event prompt (reviewer_code_holistic v2 finding).
    * ``aggregated_nacks`` — accepted for forward-compat in case the
      next-action route synthesises its own barrier-equivalent payload.

    Non-list values are coerced to an empty list rather than raised so
    a schema drift surfaces as "no NACKs rendered" rather than a hard
    crash in the wrapper.
    """
    if not isinstance(event_payload, dict):
        return []
    raw = event_payload.get("nacks")
    if not isinstance(raw, list):
        raw = event_payload.get("unresolved_nacks")
    if not isinstance(raw, list):
        raw = event_payload.get("aggregated_nacks")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _extract_iteration_feedback(event_payload: Any) -> dict[str, Any] | None:
    """Pull the per-iteration operator kickback off the event payload (#3231).

    The orchestrator's ``next-action`` route attaches the current phase
    execution's ``operator_directives`` (chronological) + the latest
    ``iteration_history`` summary onto the ``propose`` event_payload as a
    serializable ``iteration_feedback`` dict. This reader is the hop the
    ``_cli`` composer subprocess uses to pull it back out — defensive
    against schema drift so a missing/malformed block yields ``None``
    (section omitted) rather than crashing the wrapper's fallback to the
    slice-2 stub prompt.
    """
    if not isinstance(event_payload, dict):
        return None
    raw = event_payload.get("iteration_feedback")
    if not isinstance(raw, dict):
        return None
    return raw


def _context_discipline_enabled() -> bool:
    """Return whether the #3200 context discipline (master flag) is enabled.

    The single switch (``EGG_CONTEXT_DISCIPLINE``, default OFF) that flips THIS
    composer from the legacy full-context INLINE path to the
    queryable-environment JIT-pull path (``compose_event_prompt(jit_pull=…)``).
    Read once here in ``_cli`` and applied uniformly: the composer is already
    role-parameterized, so this one read covers every event-pump role —
    producers and reviewers alike — with no role hard-coding the new path.

    Canonical home: :func:`egg_agent.context_discipline.context_discipline_enabled`
    — the SAME function the sandbox-side warm-resume gate
    (``egg_agent.session.session_resume_enabled``) reads, so the whole discipline
    has one authoritative on/off. This module, however, also runs standalone
    under the wrapper bash where ``egg_agent`` may be off ``PYTHONPATH`` (the same
    constraint that forces :func:`_render_delta_pointer_section` to avoid the
    ``egg_agent`` import). So when the import is unavailable we fall back to
    reading the same env var inline with identical truthy semantics — a
    deliberate cross-boundary mirror, exactly the pattern
    ``egg_agent.reseed.resolve_reseed_threshold`` uses to import
    ``orchestrator.agent_model_resolution`` under ``try``/``except``.
    """
    try:
        from egg_agent.context_discipline import context_discipline_enabled
    except Exception:  # pragma: no cover - wrapper-bash standalone (egg_agent off path)
        return os.environ.get("EGG_CONTEXT_DISCIPLINE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    return context_discipline_enabled()


def _cli(argv: list[str] | None = None) -> int:
    """Render the per-event prompt for the wrapper bash (TASK-3-2).

    Reads the action from argv, the event payload from stdin, and the
    role / base branch / repo path / memory mode from env. Prints the
    rendered prompt to stdout. The wrapper bash invokes this as::

        prompt=$(printf '%s' "$event_payload" \\
            | python3 /opt/egg-runtime/orchestrator/routes/event_prompt.py \\
                "$action")

    Env vars consumed:

    * ``EGG_AGENT_ROLE`` (required) — agent role token; surfaces in
      role banner and gates the memory-file path.
    * ``EGG_BASE_BRANCH`` (default ``main``) — substituted into the
      ``--not origin/<base>`` term of the git-log delta.
    * ``EGG_REPO_PATH`` (default cwd) — working directory for the
      git-log subprocess + base for the memory-file path resolution.
    * ``EGG_BRC_MEMORY`` (default ``full`` since slice-4 task-4-1) —
      slice-1 reader gate; ``full`` enables the read path. Set
      ``write-only`` to keep the writer warm without reading the
      excerpt, or ``off`` for the one-release rollback escape hatch
      (no writes, no reads).
    * ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` (pod env, inherited —
      not re-exported by the wrapper) — contract identifier for the
      ``task_description`` section (#3123) and the pipeline-scoped
      memory filename (#3163). Unset → both sections omitted.
    * ``EGG_CONTEXT_DISCIPLINE`` (pod env, inherited — #3200 slice-9
      master flag, default OFF) — when truthy, renders the per-producer
      delta + durable memory as JIT-pull POINTERS instead of inlining
      the bulk (``jit_pull=True``); OFF keeps the legacy full-context
      inline path byte-for-byte. Read via ``_context_discipline_enabled``.
    """
    parser = argparse.ArgumentParser(
        description="Render the per-event BRC event-pump prompt (slice-3).",
    )
    parser.add_argument(
        "action",
        help="next-action verb: 'propose' | 'ack' | 'nack' | 'confirm' | 'complete'",
    )
    parser.add_argument(
        "--event-payload-file",
        default="",
        help="Path to a file containing the event_payload JSON (alternative to stdin).",
    )
    args = parser.parse_args(argv)

    role = (os.environ.get("EGG_AGENT_ROLE") or "").strip() or "unknown"
    base_branch = (os.environ.get("EGG_BASE_BRANCH") or "").strip() or "main"
    repo_path = Path(os.environ.get("EGG_REPO_PATH") or os.getcwd())
    # Slice-4 task-4-1 flipped the unset-env default from ``off`` to
    # ``full`` so the event-pump composer reads the memory file by
    # default. Operators can opt back into the slice-1 inert default
    # for a one-release rollback window by setting ``EGG_BRC_MEMORY=off``.
    memory_mode = (os.environ.get("EGG_BRC_MEMORY") or "full").strip().lower()

    # Event payload — JSON on stdin (preferred) or from --event-payload-file.
    if args.event_payload_file:
        try:
            event_raw = Path(args.event_payload_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"event_prompt: cannot read --event-payload-file: {exc}", file=sys.stderr)
            return 2
    else:
        event_raw = sys.stdin.read()
    event_raw = (event_raw or "").strip()
    if event_raw:
        try:
            event_payload = json.loads(event_raw)
        except json.JSONDecodeError:
            # Surface the raw payload so the agent can still see the
            # source string — better than silently treating it as
            # empty when the wrapper passed something we couldn't parse.
            event_payload = {"action": args.action, "raw": event_raw}
    else:
        event_payload = {"action": args.action}

    memory_path = _memory_path(repo_path, role)
    memory_text = _read_memory_excerpt(memory_path, memory_mode) if memory_path else ""
    # Even in ``write-only`` mode we still parse per-producer SHAs from
    # the on-disk file so the slice-3 wrapper renders the delta — the
    # mode gates only whether the markdown excerpt itself flows into
    # the prompt, not whether the per-producer SHAs flow into the
    # delta command. This matches the slice-3 plan TASK-3-2 wording:
    # "with ``EGG_BRC_MEMORY=write-only`` (slice-1 default), the prompt
    # omits memory but still emits the git-log delta against … a
    # fallback baseline".
    sha_lookup_text = memory_text
    if not sha_lookup_text and memory_path is not None and memory_path.exists():
        try:
            sha_lookup_text = memory_path.read_text(encoding="utf-8")
        except OSError:
            sha_lookup_text = ""

    delta_entries = _build_delta_entries(
        action=args.action,
        role=role,
        base_branch=base_branch,
        repo_path=repo_path,
        memory_text=sha_lookup_text,
        event_payload=event_payload,
    )

    nacks = _extract_nacks(event_payload)

    # Contract task statement (#3123) — pushed into every per-event
    # prompt so operator directives don't depend on the agent pulling
    # ``task_description`` per the rules file. Identifier comes from
    # ``EGG_PIPELINE_ID`` / ``EGG_ISSUE_NUMBER`` (pod env, inherited by
    # this subprocess); pre-#3163 issue contracts without a recorded
    # statement get a synthesized issue anchor; empty only when the
    # worktree lacks the contract file entirely (fail-soft).
    task_description = _read_task_description(repo_path)

    # Per-iteration operator kickback (#3231) — the orchestrator's
    # ``next-action`` route attaches ``iteration_feedback`` onto the
    # ``propose`` event_payload (sourced from
    # ``PhaseExecution.operator_directives`` / ``iteration_history``,
    # #2795) so the re-spawned producer under
    # ``EGG_EVENT_LOOP_OWNER=orchestrator`` addresses the operator's
    # ``request_changes`` / ``change_approach`` before re-proposing
    # instead of re-reading its own prior draft and re-proposing it
    # unchanged. Read straight off the event payload the wrapper pipes
    # on stdin — the composer subprocess has no orchestrator-package
    # context (see ``_read_task_description``) so the payload is the
    # only hop, mirroring how NACKs already flow.
    iteration_feedback = _extract_iteration_feedback(event_payload)

    # #3200 slice-9 (task-9-1): the single master flag gates the whole context
    # discipline. ON -> render the bulk (per-producer delta + memory) as
    # JIT-pull POINTERS so only the small role-parameterized protected root
    # stays resident; OFF (default) -> the legacy full-context INLINE path,
    # byte-for-byte unchanged. The composer is role-parameterized, so flipping
    # ``jit_pull`` here drives every event-pump role (producers AND reviewers)
    # through the new path without any role hard-coding it. ``memory_rel_path``
    # / ``pipeline_id`` are consumed only on the ``jit_pull=True`` arm (the
    # memory pointer + the ``brc-transcript`` pull handle); on the OFF arm they
    # are ignored, so the legacy path keeps no dependency on the new code.
    context_discipline = _context_discipline_enabled()
    memory_rel_path = ""
    if context_discipline and memory_path is not None:
        try:
            memory_rel_path = memory_path.relative_to(repo_path).as_posix()
        except ValueError:
            memory_rel_path = memory_path.as_posix()

    prompt = compose_event_prompt(
        role,
        event_payload if isinstance(event_payload, dict) else {"raw": event_payload},
        memory_text,
        nacks,
        delta_entries,
        base_branch,
        task_description=task_description,
        iteration_feedback=iteration_feedback,
        jit_pull=context_discipline,
        memory_rel_path=memory_rel_path,
        pipeline_id=_pipeline_id_token(),
    )
    sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":  # pragma: no cover — wrapper-bash entry-point
    sys.exit(_cli())

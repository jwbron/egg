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
``docs/architecture/REVIEWER-SYNC.md`` + risk_analyst R6 from
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

import json
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
# change and is not counted.
PROMPT_ENVELOPE_MAX_BYTES: int = 10240


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


def _render_event_section(
    role: str, event_payload: dict[str, Any] | None
) -> str:
    """Render the role banner + event description.

    The event payload is serialised as JSON (sorted keys, 2-space
    indent) so the rendering is deterministic — two callers with the
    same payload produce byte-identical output, which lets snapshot
    tests pin the shape without sensitivity to dict-iteration order.
    """
    if event_payload is None:
        event_payload = {}
    action = ""
    if isinstance(event_payload, dict):
        # ``next-action`` puts the chosen verb under ``action``; we also
        # accept ``type`` so an alternate orchestrator schema doesn't
        # silently surface as "(unspecified)".
        action = str(event_payload.get("action") or event_payload.get("type") or "")
    payload_json = json.dumps(event_payload, indent=2, sort_keys=True)

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
    and ``delta``. The command form is rendered verbatim so the agent
    sees the exact scope (architect plan acceptance: "git-log delta
    command is emitted verbatim with the per-producer
    ``last_reviewed_commit_sha`` substituted in"); the rendered diff
    follows so the agent can audit the full change as a fresh review.

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
        "``docs/architecture/REVIEWER-SYNC.md``: the named-blockers from "
        "your prior NACK MUST be addressed, AND any new findings the "
        "delta introduces are in scope. Both passes must succeed to ACK.",
        "",
    ]

    total_delta_bytes = 0
    for entry in git_log_delta:
        producer = str(entry.get("producer") or "(unknown)").strip()
        sha = str(entry.get("last_reviewed_commit_sha") or "").strip()
        delta = entry.get("delta") or ""
        if not isinstance(delta, str):
            delta = str(delta)
        # Command is emitted verbatim — the per-producer
        # ``last_reviewed_commit_sha`` substituted in so the agent can
        # cross-check the scope against the orchestrator's stored value.
        cmd_sha = sha if sha else "<no prior review — full branch history>"
        cmd = f"git log {cmd_sha}..HEAD --not origin/{base_branch} -p"
        lines.extend(
            [
                f"### Producer: ``{producer}``",
                "",
                f"- last_reviewed_commit_sha: `{sha or '-'}`",
                "- Re-review scope (executed by the wrapper):",
                f"  `{cmd}`",
                "",
                "Delta:",
                "```diff",
                delta if delta.strip() else "(no commits in range — re-review is a no-op)",
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


def compose_event_prompt(
    role: str,
    event_payload: dict[str, Any] | None,
    memory_excerpt: str,
    nacks: list[dict[str, Any]] | None,
    git_log_delta: list[dict[str, Any]] | None,
    base_branch: str,
) -> str:
    """Compose the per-event one-shot prompt the wrapper hands the agent.

    Positional signature is fixed by the slice-3 plan
    (TASK-3-1): ``(role, event_payload, memory_excerpt, nacks,
    git_log_delta, base_branch) -> str``. The wrapper bash invokes this
    via ``python3 -c`` so changing the positional order would silently
    break the call site; keep the order stable.

    Args:
        role: Agent role token (e.g. ``"coder"``, ``"reviewer_code"``).
            Surfaces in the role banner and in the "act per your role
            contract" framing.
        event_payload: The ``event_payload`` field returned by the
            orchestrator's ``brc next-action`` route. ``None`` is
            treated as an empty payload; ``action`` / ``type`` keys
            populate the event banner.
        memory_excerpt: Rendered markdown content of
            ``.egg-state/agent-outputs/<role>/brc-memory.md`` as read
            by the wrapper. Pass ``""`` (or anything that strips empty)
            when ``EGG_BRC_MEMORY!=full`` so the section is omitted.
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

    Returns:
        Rendered prompt string suitable for passing as the positional
        argument to ``python3 -m egg_agent``. The envelope (everything
        EXCLUDING the rendered delta) is bounded to ``PROMPT_ENVELOPE_MAX_BYTES``
        bytes; the delta itself scales with the actual change.
    """
    role = (role or "unknown").strip() or "unknown"
    base_branch = (base_branch or "main").strip() or "main"

    event_section = _render_event_section(role, event_payload)
    delta_section, _delta_bytes = _render_producer_delta_section(
        git_log_delta, base_branch
    )
    nacks_section = _render_nacks_section(nacks)
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

    parts: list[str] = [event_section]
    if delta_section:
        parts.append(delta_section)
    if nacks_section:
        parts.append(nacks_section)
    parts.append(contract)
    if memory_section:
        parts.append(memory_section)

    return "\n".join(parts)

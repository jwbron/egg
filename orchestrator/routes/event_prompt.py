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
            # the producer's proposal SHA.
            delta_rendered = (
                "(no commits in range — CAUTION: this range ended at YOUR "
                "worktree's HEAD, which does not contain the producer's "
                "commits. An empty delta here is NOT evidence the producer "
                "didn't revise. Read the producer's branch directly, e.g. "
                "`git log <producer-branch-or-sha> --not "
                f"origin/{base_branch} -p`, before issuing a verdict.)"
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
    delta_section, _delta_bytes = _render_producer_delta_section(git_log_delta, base_branch)
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

    # Enforce the envelope cap (architect plan acceptance: "per-event
    # prompt envelope (excluding delta) ≤ 10 KB"). The envelope is the
    # sum of all sections EXCLUDING the rendered delta; if it would
    # overflow we truncate the NACKs section (the variable-size driver
    # — event/contract are bounded and memory is already 2 KB capped)
    # while preserving the architect's od-6 tail-position contract for
    # memory. The truncation is byte-exact with ``errors="replace"`` so
    # a UTF-8 multibyte sequence split at the boundary doesn't crash;
    # the sentinel's own byte length is subtracted from the per-section
    # budget so the post-truncation envelope honours the cap.
    envelope_sections = [s for s in (event_section, nacks_section, contract, memory_section) if s]
    envelope_bytes = sum(len(s.encode("utf-8")) for s in envelope_sections) + max(
        0, len(envelope_sections) - 1
    )
    if envelope_bytes > PROMPT_ENVELOPE_MAX_BYTES and nacks_section:
        non_nacks_bytes = envelope_bytes - len(nacks_section.encode("utf-8"))
        sentinel_bytes = len(_ENVELOPE_TRUNCATION_SENTINEL.encode("utf-8"))
        nacks_budget = max(0, PROMPT_ENVELOPE_MAX_BYTES - non_nacks_bytes - sentinel_bytes)
        nacks_raw = nacks_section.encode("utf-8")
        if len(nacks_raw) > nacks_budget:
            nacks_section = (
                nacks_raw[:nacks_budget].decode("utf-8", errors="replace")
                + _ENVELOPE_TRUNCATION_SENTINEL
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
        refs_text = "\n".join(f"- `{a}`" for a in artifacts)
        if proposal_sha:
            # First review with a known proposal SHA: render concrete,
            # working read commands instead of the directionless "fetch
            # and read the diffs yourself" (#3076 — reviewers NACKed
            # plans they could not find because nothing said WHERE the
            # producer's work lives; their worktree does not contain
            # it, but the shared object store resolves the SHA).
            show_cmds = "\n".join(f"- `git show {proposal_sha}:{a}`" for a in artifacts)
            fallback_delta = (
                "(No `last_reviewed_commit_sha` recorded yet for this "
                "producer — this is your FIRST review of this proposal. "
                "The producer's work is NOT in your working tree; per-"
                "role worktrees are isolated. Read it via the proposed "
                f"commit `{proposal_sha}`, which resolves from your "
                "worktree through the shared object store:)\n\n"
                + (f"Proposed artifacts:\n{show_cmds}\n\n" if artifacts else "")
                + "Full proposed change:\n"
                f"- `git log {proposal_sha} --not origin/{base_branch} -p`\n\n"
                "Do NOT NACK for a missing file before reading it via "
                "these commands — a plain `Read` of the path in your own "
                "worktree is expected to fail and is not evidence the "
                "artifact does not exist.\n"
            )
        else:
            fallback_delta = (
                "(No `last_reviewed_commit_sha` recorded yet for this "
                "producer — falling back to the orchestrator's signal-level "
                "`changed_artifacts` list as a degraded baseline. This is "
                "NOT the adversarial-re-review path; if your role demands a "
                "full audit, fetch and read the actual file diffs yourself "
                "before issuing a verdict.)\n\n"
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

    memory_path = repo_path / ".egg-state" / "agent-outputs" / role / "brc-memory.md"
    memory_text = _read_memory_excerpt(memory_path, memory_mode)
    # Even in ``write-only`` mode we still parse per-producer SHAs from
    # the on-disk file so the slice-3 wrapper renders the delta — the
    # mode gates only whether the markdown excerpt itself flows into
    # the prompt, not whether the per-producer SHAs flow into the
    # delta command. This matches the slice-3 plan TASK-3-2 wording:
    # "with ``EGG_BRC_MEMORY=write-only`` (slice-1 default), the prompt
    # omits memory but still emits the git-log delta against … a
    # fallback baseline".
    sha_lookup_text = memory_text
    if not sha_lookup_text and memory_path.exists():
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

    prompt = compose_event_prompt(
        role,
        event_payload if isinstance(event_payload, dict) else {"raw": event_payload},
        memory_text,
        nacks,
        delta_entries,
        base_branch,
    )
    sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":  # pragma: no cover — wrapper-bash entry-point
    sys.exit(_cli())

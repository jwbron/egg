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


def _render_event_section(role: str, event_payload: dict[str, Any] | None) -> str:
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
        # ``next-action`` puts the chosen verb under ``action`` (see
        # ``orchestrator/routes/consensus.py``'s ``_VALID_ACTIONS``).
        action = str(event_payload.get("action") or "")
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
) -> str:
    """Render ``git log {sha}..HEAD --not origin/{base_branch} -p``.

    Runs the subprocess in ``repo_path``. The gateway allows
    ``git log`` with ``-p`` / ``--patch`` and ``--not`` flags (see
    ``gateway`` allow-list; #2905). On non-zero rc or timeout we
    return a sentinel string so the agent can audit the failure
    explicitly rather than silently reviewing an empty diff.
    """
    cmd = [
        "git",
        "log",
        f"{sha}..HEAD",
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
        return f"(git log timed out after {_GIT_LOG_TIMEOUT_SECS}s for {sha}..HEAD)"
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
    producer with a stored ``last_reviewed_commit_sha`` in the memory
    file. For producer actions (``propose`` / ``confirm``) there is
    no per-producer delta — the producer just looks at HEAD.

    **``changed_artifacts`` fallback (plan TASK-3-2 acceptance).**
    When no per-producer SHA is available (off mode, first-ever ACK
    before any memory write, parse failure, file missing) and the
    event payload carries a ``changed_artifacts`` list, render a
    single fallback entry naming the producer and the artifact list
    — explicitly labelled as a degraded baseline so the agent does
    not mistake it for an adversarial-re-review-grade diff. The
    documenter's docs at
    ``docs/architecture/orchestrator.md`` and
    ``docs/reference/agent-wait-patterns.md`` describe the same
    fallback: "strictly a degraded baseline, not the adversarial
    re-review path".

    ``role`` is currently unused inside the function body; the
    parameter is retained for the call-site symmetry (architect
    plan: the composer signature passes role through alongside
    action so a future revision can scope the producer set by the
    reviewer/producer relationship).
    """
    del role  # see docstring
    if action not in ("ack", "nack"):
        return []

    per_producer = _parse_per_producer_sha(memory_text)
    if per_producer:
        out: list[dict[str, Any]] = []
        for producer in sorted(per_producer.keys()):
            sha = per_producer[producer]
            delta = _run_git_log(sha, base_branch, repo_path)
            out.append(
                {
                    "producer": producer,
                    "last_reviewed_commit_sha": sha,
                    "delta": delta,
                }
            )
        return out

    # ``changed_artifacts`` fallback — when there is no stored SHA we
    # still surface the orchestrator's signal-level artifact list so
    # the agent has SOMETHING to anchor the re-review on. This is
    # explicitly a degraded baseline (the agent must hand-resolve any
    # diff from it); the adversarial-re-review path is only honoured
    # when a real ``last_reviewed_commit_sha`` is available.
    changed_artifacts = _extract_changed_artifacts(event_payload)
    if not changed_artifacts:
        return []

    producer = _extract_producer_role(event_payload)
    refs_text = "\n".join(f"- `{a}`" for a in changed_artifacts)
    fallback_delta = (
        "(No `last_reviewed_commit_sha` recorded yet for this "
        "producer — falling back to the orchestrator's signal-level "
        "`changed_artifacts` list as a degraded baseline. This is "
        "NOT the adversarial-re-review path; if your role demands a "
        "full audit, fetch and read the actual file diffs yourself "
        "before issuing a verdict.)\n\n"
        f"Artifacts the orchestrator flagged as changed:\n{refs_text}\n"
    )
    return [
        {
            "producer": producer,
            "last_reviewed_commit_sha": "",
            "delta": fallback_delta,
        }
    ]


def _extract_changed_artifacts(event_payload: Any) -> list[str]:
    """Pull a ``changed_artifacts`` list out of the event payload.

    Defensive against schema drift — non-list shapes coerce to empty
    rather than raising. Entries are stringified so a future schema
    surfaces structured artifact refs (e.g. dicts with role / path)
    without crashing the renderer.
    """
    if not isinstance(event_payload, dict):
        return []
    raw = event_payload.get("changed_artifacts")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item is not None and str(item).strip()]


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
    re-propose. Two keys are accepted so the surface naming can evolve
    without breaking the wrapper:

    * ``nacks`` — the canonical key from ``_open_nacks_barrier_response``.
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
    * ``EGG_BRC_MEMORY`` (default ``off``) — slice-1 reader gate;
      ``full`` enables the read path, anything else skips it.
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
    memory_mode = (os.environ.get("EGG_BRC_MEMORY") or "off").strip().lower()

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

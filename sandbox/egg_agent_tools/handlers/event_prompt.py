"""Per-event prompt composer for the BRC event-pump (#2908 slice-3).

Sandbox-side mirror of ``orchestrator.routes.pipelines.compose_event_prompt``.
The orchestrator builds the wrapper bash template that runs in every
agent pod (see ``orchestrator/consensus_wrapper.py``
``_EVENT_PUMP_WRAPPER_TEMPLATE``); the bash then needs to compose the
per-event prompt at runtime, reading the durable BRC memory written by
:mod:`egg_agent_tools.handlers.brc_memory` (slice-1).

The orchestrator pod and the sandbox/agent pod have disjoint
``PYTHONPATH``\\ s — the orchestrator imports
``orchestrator.routes.*`` from the repo root, the sandbox imports
``egg_agent_tools.*`` from ``/opt/egg-runtime/sandbox`` — so a single
shared module is not importable from both sides without inventing a
third top-level package. Slice-3 keeps the canonical *testable*
implementation in ``orchestrator/routes/pipelines.py`` (where the
TASK-3-6 unit tests live) and *mirrors* the same logic here so the
wrapper bash can call it directly via
``python3 -m egg_agent_tools.handlers.event_prompt``. The two
implementations MUST stay in lock step; the snapshot test for
``compose_event_prompt`` (TASK-3-6) pins the orchestrator-side
behaviour and any divergence here would surface as a wrapper-vs-test
mismatch on first integration.

Composition shape (architect v2 ``design.per_event_prompt`` +
risk_analyst R6):

1. Role banner.
2. One-line event description (action + slice + role).
3. Event payload (JSON, orchestrator's next-action body).
4. Optional per-reviewer NACK delta.
5. Per-producer ``git log {sha}..HEAD --not origin/{base} -p`` command
   lines — shell commands the agent runs in its sandbox to read the
   full delta per REVIEWER-SYNC.md. We do NOT inline the diff bytes
   here; the wrapper bounds the *envelope* to 10 KB.
6. Action contract (single event, exit naturally).
7. Memory excerpt at the tail (architect od-6 Option B) — keeps the
   cacheable prefix above stable across re-entries.

The CLI entry point reads JSON from ``--input-file`` (path to a JSON
document with the inputs) and writes the composed prompt to stdout.
The wrapper bash invokes it with a JSON document materialised in
``$XDG_RUNTIME_DIR`` (or ``/tmp`` fallback) to avoid the bash
metachar / quoting hazard documented in #2741.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from egg_agent_tools.handlers import brc_memory

_logger = logging.getLogger(__name__)

# Envelope caps. Mirror
# ``orchestrator/routes/pipelines.py:_COMPOSE_EVENT_PROMPT_*``.
_ENVELOPE_MAX_BYTES = 10 * 1024  # 10 KB — sections 1..6 (the prose)
_MEMORY_MAX_BYTES = 2 * 1024  # 2 KB — tail memory excerpt
_REASON_TRUNCATE_CHARS = 800  # Per-reviewer NACK reason cap


def _truncate_memory_excerpt(memory_excerpt: str | None) -> str:
    """Return ``memory_excerpt`` trimmed to the 2 KB per-event cap.

    Tail-preserving: when the rendered memory exceeds the cap we keep
    the most recent decision-log entries (the tail) and prefix a
    ``…(truncated)…`` marker so the agent can tell the excerpt is
    partial. UTF-8 bytes measured via ``.encode("utf-8")`` so multi-
    byte characters don't blow the cap.
    """
    if not memory_excerpt:
        return ""
    encoded = memory_excerpt.encode("utf-8")
    if len(encoded) <= _MEMORY_MAX_BYTES:
        return memory_excerpt
    marker = (
        "…(truncated for per-event prompt; see brc-memory.md on disk for full)…\n\n"
    )
    marker_bytes = marker.encode("utf-8")
    tail_budget = _MEMORY_MAX_BYTES - len(marker_bytes)
    if tail_budget <= 0:
        return marker.rstrip()
    tail_bytes = encoded[-tail_budget:]
    return marker + tail_bytes.decode("utf-8", errors="ignore")


def _render_nacks_block(nacks: list[dict[str, Any]] | None) -> str:
    """Render the per-reviewer NACK delta for the per-event prompt."""
    if not nacks:
        return ""
    lines: list[str] = ["## Open NACKs you must address", ""]
    for nack in nacks:
        reviewer = str(nack.get("reviewer", "unknown"))
        version = nack.get("version")
        version_tag = f"v{version}" if version is not None else "v?"
        artifact_refs = nack.get("artifact_refs") or []
        if isinstance(artifact_refs, list):
            refs_str = ", ".join(str(r) for r in artifact_refs)
        else:
            refs_str = str(artifact_refs)
        reason_raw = str(nack.get("reason") or "")
        if len(reason_raw) > _REASON_TRUNCATE_CHARS:
            reason = reason_raw[:_REASON_TRUNCATE_CHARS] + "…(truncated)"
        else:
            reason = reason_raw
        lines.append(f"### {reviewer} ({version_tag})")
        if refs_str:
            lines.append(f"- artifact_refs: {refs_str}")
        lines.append("")
        lines.append(reason if reason else "_(no reason text)_")
        lines.append("")
    return "\n".join(lines)


def _render_git_log_delta_commands(
    per_producer_last_reviewed_sha: dict[str, str] | None,
    base_branch: str | None,
) -> str:
    """Render per-producer ``git log {sha}..HEAD --not origin/{base} -p``.

    Empty / missing SHA falls back to ``git log origin/{base}..HEAD -p``
    (first review for this producer in the current slice).
    """
    if not per_producer_last_reviewed_sha:
        return ""
    base = base_branch or "main"
    base_ref = f"origin/{base}"
    lines: list[str] = [
        "## Full delta per producer (REVIEWER-SYNC.md — required reading)",
        "",
        "Run these in your sandbox before forming a verdict — the "
        "summary above is only the orchestrator's signal-level view of "
        "what changed. The full diff is authoritative for adversarial "
        "re-review:",
        "",
    ]
    for producer in sorted(per_producer_last_reviewed_sha.keys()):
        sha = (per_producer_last_reviewed_sha.get(producer) or "").strip()
        if sha:
            lines.append(f"# producer={producer} (since your last review)")
            lines.append(f"git log {sha}..HEAD --not {base_ref} -p")
        else:
            lines.append(f"# producer={producer} (first review this slice)")
            lines.append(f"git log {base_ref}..HEAD -p")
        lines.append("")
    return "\n".join(lines)


def compose_event_prompt(
    role: str,
    event_payload: dict[str, Any] | str | None,
    *,
    memory_excerpt: str | None = None,
    nacks: list[dict[str, Any]] | None = None,
    per_producer_last_reviewed_sha: dict[str, str] | None = None,
    base_branch: str | None = None,
    action: str = "review",
    slice_id: str | None = None,
) -> str:
    """Compose the single-event prompt for the wrapper to pass the agent.

    Mirror of ``orchestrator.routes.pipelines.compose_event_prompt`` —
    see that function's docstring for the full contract. Keeping the
    implementations in lock step is a TASK-3-6 requirement; the
    orchestrator-side snapshot test bounds drift.
    """
    if event_payload is None:
        payload_str = "{}"
    elif isinstance(event_payload, str):
        payload_str = event_payload or "{}"
    else:
        try:
            payload_str = json.dumps(event_payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            payload_str = "{}"

    slice_tag = slice_id or "none"

    sections: list[str] = []
    sections.append(f"# BRC event-pump handler — role={role}\n")
    sections.append(
        f"Action requested: **{action}** (slice={slice_tag}). Handle this "
        "single event, update durable BRC memory, then exit naturally. "
        "The wrapper will invoke you again with the next event.\n"
    )
    sections.append("## Event payload (orchestrator next-action body)\n")
    sections.append("```json")
    sections.append(payload_str)
    sections.append("```")
    sections.append("")
    nacks_block = _render_nacks_block(nacks)
    if nacks_block:
        sections.append(nacks_block)
    git_log_block = _render_git_log_delta_commands(
        per_producer_last_reviewed_sha, base_branch
    )
    if git_log_block:
        sections.append(git_log_block)
    sections.append(
        "## Action contract\n\n"
        "- You are invoked one-shot per actionable BRC event by the "
        "wrapper. **Do not** call `egg-orch message wait-loop` — the "
        "wrapper owns the blocking wait between events. Just handle "
        "the single event above and exit.\n"
        "- Update your durable BRC memory at "
        "`.egg-state/agent-outputs/<role>/brc-memory.md` via the same "
        "`egg-orch consensus ack` / `consensus nack` / `consensus "
        "propose` paths — the slice-1 writer records the per-producer "
        "`last_reviewed_commit_sha` automatically.\n"
        "- Exit when the action is done; the wrapper invokes you "
        "again as soon as the next event lands.\n"
    )

    head = "\n".join(sections)
    head_encoded = head.encode("utf-8")
    if len(head_encoded) > _ENVELOPE_MAX_BYTES:
        truncated = head_encoded[:_ENVELOPE_MAX_BYTES].decode(
            "utf-8", errors="ignore"
        )
        head = (
            truncated
            + "\n\n…(per-event prompt envelope truncated at "
            f"{_ENVELOPE_MAX_BYTES} bytes)…\n"
        )

    memory_tail = _truncate_memory_excerpt(memory_excerpt)
    if memory_tail:
        return (
            head
            + "\n## Durable BRC memory (your distilled prior assessments)\n\n"
            + memory_tail
            + "\n"
        )
    return head


# ---------------------------------------------------------------------------
# Sandbox-side helpers — read memory + derive per-producer SHA map
# ---------------------------------------------------------------------------


def load_memory_for_role(role: str | None = None) -> brc_memory.BRCMemory | None:
    """Return the parsed :class:`BRCMemory` for ``role`` or ``None``.

    Honours ``EGG_BRC_MEMORY``: returns ``None`` unless mode is ``full``
    so the per-event prompt only includes memory when the read path is
    enabled. ``write-only`` keeps writes side-effecting (slice-1
    default) but treats the reader as a no-op (slice-3 design
    intent — slice-4 flips the default to ``full``).

    Returns ``None`` when:
      * mode is anything other than ``full``;
      * the memory file does not exist (first event for this role);
      * the role resolver raises (``EGG_AGENT_ROLE`` unset — caller
        should not have invoked the reader in that case, but we fail
        soft so the wrapper does not crash mid-event-loop).
    """
    if not brc_memory.is_reads_enabled():
        return None
    try:
        resolved_role = brc_memory._resolve_role(role)
    except brc_memory.HandlerError:
        _logger.warning(
            "event_prompt: EGG_AGENT_ROLE unresolved; skipping memory read"
        )
        return None
    try:
        path = brc_memory.memory_path_for_role(resolved_role)
    except brc_memory.HandlerError:
        _logger.warning(
            "event_prompt: memory path resolution failed for role=%s",
            resolved_role,
        )
        return None
    try:
        return brc_memory.load_memory(path)
    except FileNotFoundError:
        return None
    except Exception:  # pragma: no cover — defensive; renderer must not crash wrapper
        _logger.exception(
            "event_prompt: load_memory failed unexpectedly for role=%s", resolved_role
        )
        return None


def render_memory_excerpt(memory: brc_memory.BRCMemory | None) -> str:
    """Render a :class:`BRCMemory` into the per-event-prompt excerpt.

    Returns an empty string when ``memory`` is ``None`` OR when every
    field on the loaded memory is empty/defaulted — the slice-1 writer
    returns ``BRCMemory()`` (all fields default) when the memory file
    does not exist yet (first event for this role), and rendering an
    empty memory would embed a useless ``## Codebase / change model\\n\\n-\\n…``
    skeleton in the per-event prompt that the agent has to read past.
    The composer skips the tail section entirely when this returns
    empty.
    """
    if memory is None:
        return ""
    if (
        not memory.codebase_change_model
        and not memory.per_producer
        and not memory.decision_log
    ):
        return ""
    return brc_memory.render_memory(memory)


def per_producer_sha_map(
    memory: brc_memory.BRCMemory | None,
) -> dict[str, str]:
    """Extract ``{producer -> last_reviewed_commit_sha}`` from memory.

    Empty dict when memory is ``None`` or carries no per-producer
    entries; the renderer then emits an empty git-log-delta section
    so the agent sees only the orchestrator's signal-level view of
    what changed.
    """
    if memory is None:
        return {}
    return {
        producer: assessment.last_reviewed_commit_sha
        for producer, assessment in memory.per_producer.items()
    }


# ---------------------------------------------------------------------------
# CLI entry point — invoked by the wrapper bash
# ---------------------------------------------------------------------------


def _parse_input_document(path: Path) -> dict[str, Any]:
    """Read the wrapper-supplied JSON input file.

    Schema (all keys optional):
      {
        "role": "coder",
        "action": "ack",
        "slice_id": "slice-3",
        "base_branch": "main",
        "event_payload": { ... } | "<json string>",
        "nacks": [ {reviewer, version, reason, artifact_refs}, ... ]
      }

    The memory excerpt + per-producer SHA map are NOT in the input
    document; they are read from disk by the CLI so the wrapper bash
    does not have to thread them through the JSON envelope (cleaner
    separation, and avoids shipping multi-KB memory through bash
    argv).
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"event_prompt: --input-file not found: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"event_prompt: --input-file is not valid JSON: {exc}"
        ) from exc
    if not isinstance(doc, dict):
        raise SystemExit(
            "event_prompt: --input-file must contain a JSON object at the top level"
        )
    return doc


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="event_prompt",
        description=(
            "Compose the per-event BRC prompt the event-pump wrapper "
            "invokes the agent with. Reads inputs from a JSON file "
            "(--input-file) and writes the composed prompt to stdout."
        ),
    )
    parser.add_argument(
        "--input-file",
        required=True,
        type=Path,
        help="Path to a JSON document with the composer inputs.",
    )
    args = parser.parse_args(argv)

    doc = _parse_input_document(args.input_file)
    role = doc.get("role") or os.environ.get("EGG_AGENT_ROLE", "unknown")
    action = doc.get("action") or "review"
    slice_id = doc.get("slice_id") or os.environ.get("EGG_SLICE_ID")
    base_branch = doc.get("base_branch")
    event_payload = doc.get("event_payload")
    nacks = doc.get("nacks") or []
    if not isinstance(nacks, list):
        nacks = []

    memory = load_memory_for_role(role)
    memory_excerpt = render_memory_excerpt(memory)
    sha_map = per_producer_sha_map(memory)

    prompt = compose_event_prompt(
        role=role,
        event_payload=event_payload,
        memory_excerpt=memory_excerpt,
        nacks=nacks,
        per_producer_last_reviewed_sha=sha_map,
        base_branch=base_branch,
        action=action,
        slice_id=slice_id,
    )
    # Write to stdout. The wrapper bash captures via $(…) and passes the
    # captured string to ``python3 -m egg_agent``. Using stdout (not a
    # tempfile) keeps the wrapper bash simpler — the prompt is small
    # (~2-10 KB) and the bash $(…) substitution handles UTF-8 cleanly.
    sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised by integration tests
    raise SystemExit(_cli_main(sys.argv[1:]))

"""Durable BRC memory writer (#2908 slice-1, task-1-6).

Reviewers in the BRC consensus protocol keep a per-role markdown memory
file that the slice-3 event-pump will consult on re-entry to a review
cycle so a stateless one-shot agent invocation carries the same
distilled context a long-lived session would have built up.

Slice-1 lands the **writer** in ``brc_ack`` / ``brc_nack`` (this
module) and gates everything behind ``EGG_BRC_MEMORY``. Slice-3 lands
the **reader** in ``compose_event_prompt``.

**Path layout** — ``.egg-state/agent-outputs/<role>/brc-memory.md``
(subdirectory layout per architect od-1). Resolved against
``EGG_REPO_PATH``. The path constructor raises BEFORE creating any
file or directory when ``EGG_AGENT_ROLE`` is unset/empty (architect
od-1 + risk_analyst R14 — never fall through to a degenerate
``.egg-state/agent-outputs//brc-memory.md`` that two roles could
collide on).

**Modes** — ``EGG_BRC_MEMORY``:

  - ``off`` (default) — writes are no-ops. Slice-1 ships inert in
    production; slice-4 flips this on.
  - ``write-only`` — writes happen but reads are no-ops. The slice-3
    reader respects this mode by passing an empty memory excerpt.
  - ``full`` — writes happen and the slice-3 reader includes the
    memory excerpt in the per-event prompt.

**Atomic write** — the rendered markdown is written via a temp file
in the same directory followed by ``os.replace`` (POSIX-atomic rename
on the same filesystem). The pattern is local to this module rather
than promoted to ``shared/`` because the body is short (~20 lines)
and lifting it through ``shared/`` would expand the slice's review
surface for negligible reuse. The contract codified by slice-1
task-1-9 is testable independent of helper choice: back-to-back
handler invocations must never observe a partial file.

**Decision-log cap (architect od-2)** — the decision log is capped at
the **last 20 entries** on every write (distill-on-write). An
unbounded log eventually pushes the memory file past the cacheable
prefix of a per-event handler invocation — the very cost the artifact
exists to avoid.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

_logger = logging.getLogger(__name__)

# Cap on the per-producer ``prior_nack_reasons`` list. NACK reasons are
# typically 1-3 bullets per reviewer; 10 is a generous ceiling that
# keeps the per-producer section bounded even if a chatty reviewer
# pastes a list of findings.
_MAX_NACK_REASONS: Final[int] = 10

# Cap on the decision log. Architect od-2 distill-on-write rationale —
# the file must stay inside the cacheable prefix.
_DECISION_LOG_CAP: Final[int] = 20

# Cap on the codebase/change-model prose. The slice-3 reader uses this
# field to seed the agent's understanding without re-reading the
# repository; capping at 2 KB keeps it bounded in the per-event prompt
# envelope.
_CODEBASE_PROSE_MAX_CHARS: Final[int] = 2000

# Cap on the per-producer ``summary_of_assessment`` field. Kept short
# enough that all reviewer entries together can still fit alongside the
# git-log delta in the event prompt envelope.
_ASSESSMENT_PROSE_MAX_CHARS: Final[int] = 1000

# Environment variables.
ENV_MEMORY_MODE: Final[str] = "EGG_BRC_MEMORY"
ENV_AGENT_ROLE: Final[str] = "EGG_AGENT_ROLE"
ENV_REPO_PATH: Final[str] = "EGG_REPO_PATH"

# Valid mode values. The default (``full``) is the production setting
# post-slice-4 task-4-1: the event-pump wrapper reads the memory file,
# and the writer keeps the per-producer state current. ``off`` is the
# one-release rollback escape hatch. The string set is small so the
# parser is plain ``raw in MODE_*`` rather than an enum — keeps the
# public surface to raw strings the tests and the slice-3 reader can
# compare directly.
MODE_OFF: Final[str] = "off"
MODE_WRITE_ONLY: Final[str] = "write-only"
MODE_FULL: Final[str] = "full"
_VALID_MODES: Final[frozenset[str]] = frozenset({MODE_OFF, MODE_WRITE_ONLY, MODE_FULL})
# Default mode when ``EGG_BRC_MEMORY`` is unset / empty.
# Slice-4 task-4-1 flipped this from ``off`` to ``full`` so the
# event-pump wrapper (now the production path) reads the memory
# excerpt by default.
MODE_DEFAULT: Final[str] = MODE_FULL


# Set of EGG_BRC_MEMORY values for which we've already emitted the
# "unknown value" warning. The handler boundary is hot, so the warning
# fires once per distinct typo per process — enough for the operator to
# notice a misconfiguration (e.g. ``writeonly`` missing the hyphen) but
# not enough to spam every ACK/NACK on a deployed pod.
_warned_unknown_modes: set[str] = set()


def get_memory_mode() -> str:
    """Return the configured memory mode, defaulting to ``full``.

    Reads ``EGG_BRC_MEMORY`` with the canonical tri-state values
    (``off`` / ``write-only`` / ``full``). Slice-4 task-4-1 flipped
    the unset-env default from ``off`` to ``full`` so the production
    event-pump wrapper reads the memory file by default; setting
    ``EGG_BRC_MEMORY=off`` is the one-release rollback escape hatch.

    Unknown values fall back to ``off`` (fail-safe — an undocumented
    value should not silently flip a production pipeline into a
    write-bearing mode) and log a one-shot warning per distinct value
    so a typo like ``writeonly`` (missing hyphen) doesn't sit silently
    inert in production.
    """
    raw = os.environ.get(ENV_MEMORY_MODE, "").strip().lower()
    if not raw:
        return MODE_DEFAULT
    if raw in _VALID_MODES:
        return raw
    # Unknown value → fail-safe to off. Log once per distinct value so
    # the operator catches typos without per-call spam on the handler
    # boundary. The slice-1 plan and ``docs/architecture/brc-memory.md``
    # canonicalise the three accepted values, so this branch is reachable
    # only via typo or legacy/unsupported value. Note: the fail-safe
    # target is ``off`` (NOT the new ``full`` default) — an explicit
    # but unrecognised value is a misconfiguration signal, and a
    # write-bearing default would mask it.
    if raw not in _warned_unknown_modes:
        _warned_unknown_modes.add(raw)
        _logger.warning(
            "Unknown %s value %r; falling back to %r. Accepted values: %s.",
            ENV_MEMORY_MODE,
            raw,
            MODE_OFF,
            ", ".join(sorted(_VALID_MODES)),
        )
    return MODE_OFF


def is_writes_enabled(mode: str | None = None) -> bool:
    """True iff the writer should persist changes for ``mode``."""
    actual = mode if mode is not None else get_memory_mode()
    return actual in (MODE_WRITE_ONLY, MODE_FULL)


def is_reads_enabled(mode: str | None = None) -> bool:
    """True iff the slice-3 reader path should consume the memory file.

    Exposed here so slice-3's ``compose_event_prompt`` shares the
    mode-gating logic with the writer. Slice-1 ships the writer in
    ``write-only`` mode (reads disabled); slice-3 + slice-4 land the
    reader and flip the default to ``full``.
    """
    actual = mode if mode is not None else get_memory_mode()
    return actual == MODE_FULL


def _resolve_role(role: str | None) -> str:
    """Resolve the writer's role with the fail-closed contract.

    Per architect od-1 + risk_analyst R14: a missing ``EGG_AGENT_ROLE``
    must raise BEFORE the path constructor touches the filesystem.
    Falling through to a degenerate ``.egg-state/agent-outputs//`` path
    is unacceptable for a primitive that other reviewers will eventually
    consult to make veto-bearing decisions — two roles could collide on
    a single shared file.
    """
    candidate = role if role is not None else os.environ.get(ENV_AGENT_ROLE)
    if candidate is None:
        candidate = ""
    candidate = candidate.strip()
    if not candidate:
        raise ValueError(
            f"BRC memory writer requires {ENV_AGENT_ROLE} to be set "
            f"(got empty/unset); fail-closed to avoid a degenerate "
            f"shared-file path"
        )
    # The role token lands as a directory segment; reject anything that
    # could smuggle path separators or shell metacharacters. The
    # existing role set is ``[a-z][a-z0-9_]*`` (e.g. ``reviewer_code``);
    # we accept that shape plus hyphens for future-proofing.
    for ch in candidate:
        if not (ch.isalnum() or ch in "_-"):
            raise ValueError(
                f"BRC memory writer rejects role token {candidate!r}: "
                f"only [a-zA-Z0-9_-] are accepted to keep the path "
                f"a single directory segment"
            )
    return candidate


def _resolve_repo_root() -> Path:
    """Return the repo root for memory path resolution.

    Defaults to the CWD when ``EGG_REPO_PATH`` is unset — matches the
    semantics of ``handlers._gateway.get_repo_path``.
    """
    raw = os.environ.get(ENV_REPO_PATH)
    if raw:
        return Path(raw)
    return Path.cwd()


def memory_path_for_role(role: str | None = None) -> Path:
    """Return the absolute memory file path for ``role`` (or env role).

    Path layout (per ``docs/architecture/brc-memory.md``)::

        <EGG_REPO_PATH>/.egg-state/agent-outputs/<role>/brc-memory.md

    Raises ``ValueError`` BEFORE touching the filesystem when the role
    cannot be resolved (architect od-1 + risk_analyst R14).
    """
    resolved_role = _resolve_role(role)
    root = _resolve_repo_root()
    return root / ".egg-state" / "agent-outputs" / resolved_role / "brc-memory.md"


# ---------------------------------------------------------------------------
# In-memory representation
# ---------------------------------------------------------------------------


@dataclass
class ProducerAssessment:
    """Per-producer assessment block (architect v2 design.memory_schema).

    All six required fields are stored verbatim. ``prior_nack_reasons``
    is a list so a re-review can audit each individual reason; the
    rest are single-line strings.
    """

    producer: str
    last_reviewed_commit_sha: str = ""
    prior_verdict: str = ""
    prior_nack_reasons: list[str] = field(default_factory=list)
    prior_conditional_obligation: str = ""
    summary_of_assessment: str = ""


@dataclass
class BRCMemory:
    """In-memory representation of the rendered ``brc-memory.md``.

    Read by :func:`load_memory` and written by :func:`render_memory`
    via :func:`write_memory_atomic`. Slice-3's reader consumes the
    same dataclass so the writer and reader share one schema.
    """

    codebase_change_model: str = ""
    per_producer: dict[str, ProducerAssessment] = field(default_factory=dict)
    decision_log: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Render / parse
# ---------------------------------------------------------------------------


def _bullet_value(field_name: str, value: str) -> str:
    """Render a single ``- <field>: <value>`` line."""
    return f"- {field_name}: {value}"


def _normalize_single_line(raw: str, max_chars: int) -> str:
    """Collapse whitespace and trim to ``max_chars`` characters.

    The schema stores multi-paragraph prose on a single line so a
    naive markdown reader can parse the per-producer block by
    splitting on ``- <field>:`` boundaries. Distilling on write also
    caps the per-event prompt size — the file must stay inside the
    cacheable prefix.
    """
    if not raw:
        return ""
    collapsed = " ".join(raw.split())
    if len(collapsed) > max_chars:
        collapsed = collapsed[: max_chars - 1] + "…"
    return collapsed


def _render_assessment(assessment: ProducerAssessment) -> list[str]:
    """Render one ``### <producer>`` block."""
    nacks = "; ".join(assessment.prior_nack_reasons[:_MAX_NACK_REASONS])
    nacks_line = nacks if nacks else "-"
    obligation = assessment.prior_conditional_obligation or "-"
    summary = _normalize_single_line(assessment.summary_of_assessment, _ASSESSMENT_PROSE_MAX_CHARS)
    lines = [
        f"### {assessment.producer}",
        "",
        _bullet_value("producer", assessment.producer),
        _bullet_value("last_reviewed_commit_sha", assessment.last_reviewed_commit_sha or "-"),
        _bullet_value("prior_verdict", assessment.prior_verdict or "-"),
        _bullet_value("prior_nack_reasons", nacks_line),
        _bullet_value("prior_conditional_obligation", obligation),
        _bullet_value("summary_of_assessment", summary or "-"),
    ]
    return lines


def render_memory(memory: BRCMemory) -> str:
    """Render the in-memory ``BRCMemory`` to the on-disk markdown form.

    Matches the schema documented in ``docs/architecture/brc-memory.md``
    so the slice-3 reader parses what the slice-1 writer produces.
    """
    codebase = _normalize_single_line(memory.codebase_change_model, _CODEBASE_PROSE_MAX_CHARS)
    lines: list[str] = [
        "## Codebase / change model",
        "",
        codebase if codebase else "-",
        "",
        "## Per-producer assessment",
        "",
    ]
    # Deterministic ordering — sorted by producer role so two writers
    # that converge on the same data render the same bytes. This keeps
    # the per-event prompt's cacheable prefix stable across re-entries.
    for producer in sorted(memory.per_producer.keys()):
        lines.extend(_render_assessment(memory.per_producer[producer]))
        lines.append("")

    lines.append("## Decision log")
    lines.append("")
    if memory.decision_log:
        for entry in memory.decision_log[-_DECISION_LOG_CAP:]:
            lines.append(f"- {entry}")
    else:
        lines.append("-")
    lines.append("")
    return "\n".join(lines)


def _parse_assessment_block(role: str, block_lines: list[str]) -> ProducerAssessment:
    """Parse the body of a ``### <producer>`` block.

    Each non-empty line is expected to be of the form
    ``- <field>: <value>``. Unknown fields are ignored (forward-compat).
    Missing fields default to the dataclass default.
    """
    assessment = ProducerAssessment(producer=role)
    for raw_line in block_lines:
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:]
        if ":" not in body:
            continue
        key, _, value = body.partition(":")
        key = key.strip()
        value = value.strip()
        # ``-`` sentinel means "no value populated" — restore the
        # dataclass default rather than carrying the literal dash
        # forward.
        if value == "-":
            value = ""
        if key == "producer":
            # producer name is also the section header; keep them in sync.
            if value:
                assessment.producer = value
        elif key == "last_reviewed_commit_sha":
            assessment.last_reviewed_commit_sha = value
        elif key == "prior_verdict":
            assessment.prior_verdict = value
        elif key == "prior_nack_reasons":
            if value:
                # Stored as ``; ``-joined; un-join on read.
                assessment.prior_nack_reasons = [
                    item.strip() for item in value.split(";") if item.strip()
                ]
            else:
                assessment.prior_nack_reasons = []
        elif key == "prior_conditional_obligation":
            assessment.prior_conditional_obligation = value
        elif key == "summary_of_assessment":
            assessment.summary_of_assessment = value
    return assessment


def parse_memory(content: str) -> BRCMemory:
    """Parse a rendered memory file back into a ``BRCMemory``.

    Tolerant of missing sections so a partially-populated file (e.g.
    one written by a future schema variant) still loads with sensible
    defaults. Caller is responsible for handling ``FileNotFoundError``.
    """
    memory = BRCMemory()
    if not content.strip():
        return memory

    # Find the three top-level sections.
    section_starts: dict[str, int] = {}
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "## Codebase / change model":
            section_starts["codebase"] = idx
        elif stripped == "## Per-producer assessment":
            section_starts["producers"] = idx
        elif stripped == "## Decision log":
            section_starts["decisions"] = idx

    def _section_body(start_key: str, end_keys: list[str]) -> list[str]:
        start = section_starts.get(start_key)
        if start is None:
            return []
        # Find the earliest following section start.
        end = len(lines)
        for k in end_keys:
            if k in section_starts and section_starts[k] > start:
                end = min(end, section_starts[k])
        return lines[start + 1 : end]

    # Codebase / change model — collect non-blank lines until the next
    # section.
    codebase_lines = [
        ln for ln in _section_body("codebase", ["producers", "decisions"]) if ln.strip()
    ]
    if codebase_lines:
        # The renderer writes a single line; tolerate multi-line files
        # from an earlier hand-edit by joining with spaces.
        codebase = " ".join(ln.strip() for ln in codebase_lines)
        if codebase == "-":
            codebase = ""
        memory.codebase_change_model = codebase

    # Per-producer assessments.
    producer_block = _section_body("producers", ["decisions"])
    current_role: str | None = None
    current_lines: list[str] = []
    for ln in producer_block:
        stripped = ln.strip()
        if stripped.startswith("### "):
            # Flush the prior block.
            if current_role is not None:
                memory.per_producer[current_role] = _parse_assessment_block(
                    current_role, current_lines
                )
            current_role = stripped[4:].strip() or None
            current_lines = []
        else:
            current_lines.append(ln)
    if current_role is not None:
        memory.per_producer[current_role] = _parse_assessment_block(current_role, current_lines)

    # Decision log.
    decision_lines = _section_body("decisions", [])
    for ln in decision_lines:
        stripped = ln.strip()
        if not stripped.startswith("- "):
            continue
        entry = stripped[2:].strip()
        if entry and entry != "-":
            memory.decision_log.append(entry)
    return memory


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def write_memory_atomic(memory: BRCMemory, path: Path) -> None:
    """Persist ``memory`` to ``path`` atomically via tempfile + os.replace.

    Pattern (mirrors ``shared/egg_contracts/usage_loader.py:_atomic_write``):

      1. Ensure the parent directory exists (created with parents=True).
      2. ``tempfile.mkstemp`` in the same directory so ``os.replace`` is
         a same-filesystem POSIX-atomic rename.
      3. Write rendered content, ``flush()`` + ``os.fsync()``.
      4. ``os.chmod(0o644)`` so the file is readable by sibling tools.
      5. ``os.replace`` the temp file into place.
      6. Best-effort cleanup of the temp file on any exception.

    Slice-1 task-1-9 codifies the "back-to-back invocations never see a
    partial state" contract via fault injection in tests.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = render_memory(memory)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".tmp",
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup; the next write will overwrite via
        # ``os.replace`` regardless.
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_memory(path: Path) -> BRCMemory:
    """Read the memory file from ``path``; return an empty memory if absent."""
    if not path.exists():
        return BRCMemory()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return BRCMemory()
    return parse_memory(content)


# ---------------------------------------------------------------------------
# Public entry-point: record_review
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with second precision (``Z`` suffix)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_review(
    *,
    role: str | None = None,
    verdict: str,
    producer_role: str,
    reason: str,
    files_reviewed: list[str] | None = None,
    commit_sha: str = "",
    pre_merge_condition: str = "",
) -> None:
    """Record a review event in the writer's per-role memory file.

    No-op when ``EGG_BRC_MEMORY`` is ``off``. The default since slice-4
    task-4-1 is ``full``, so production agents write by default; setting
    ``EGG_BRC_MEMORY=off`` is the one-release rollback escape hatch.

    Args:
        role: Writer's role override. Defaults to ``$EGG_AGENT_ROLE``.
            Resolution is fail-closed: raises ``ValueError`` before
            touching the filesystem when neither is set.
        verdict: One of ``"ACK"``, ``"NACK"``, ``"conditional-ACK"``.
            Free-form: callers can pass other strings (e.g. for a
            future hardening pass) and the writer stores them verbatim.
        producer_role: The producer being reviewed (the role whose
            proposal the reviewer is verdicting).
        reason: Free-form reason text. For NACKs the text seeds
            ``prior_nack_reasons``; for ACKs it lands in
            ``summary_of_assessment``.
        files_reviewed: Artifact references that were reviewed.
            Currently informational only — recorded in the decision log
            entry so a future re-review can see what was looked at.
        commit_sha: Commit SHA reviewed (typically the producer's
            current proposal head). Lands in
            ``last_reviewed_commit_sha`` — slice-3 reads this to scope
            the adversarial re-review git delta.
        pre_merge_condition: Pre-merge obligation text (#1998). When
            non-empty, recorded in ``prior_conditional_obligation`` and
            the verdict is upgraded to ``"conditional-ACK"`` for
            display purposes if the caller passed plain ``"ACK"``.

    The handler boundary callers (``brc_ack`` / ``brc_nack``) call this
    AFTER the orchestrator returns success — a stale-version rejection
    or gateway error short-circuits before reaching this writer so the
    memory file never records verdicts that the orchestrator didn't
    accept.
    """
    if not is_writes_enabled():
        return

    path = memory_path_for_role(role)

    # Load existing memory (or seed an empty one).
    memory = load_memory(path)

    # Upgrade ACK to conditional-ACK when an obligation is attached;
    # makes the on-disk verdict self-describing without a separate
    # boolean column.
    final_verdict = verdict
    if pre_merge_condition and verdict.upper() == "ACK":
        final_verdict = "conditional-ACK"

    # Update or create the per-producer assessment block.
    existing = memory.per_producer.get(producer_role)
    if existing is None:
        existing = ProducerAssessment(producer=producer_role)
        memory.per_producer[producer_role] = existing
    existing.producer = producer_role
    if commit_sha:
        existing.last_reviewed_commit_sha = commit_sha
    existing.prior_verdict = final_verdict
    existing.prior_conditional_obligation = pre_merge_condition or ""

    verdict_normalized = final_verdict.upper()
    if verdict_normalized == "NACK":
        existing.prior_nack_reasons = [reason] if reason else []
        existing.summary_of_assessment = reason or existing.summary_of_assessment
    elif verdict_normalized in ("ACK", "CONDITIONAL-ACK"):
        # ACK clears the NACK history: the producer's prior NACKs no
        # longer apply once the reviewer has approved this version.
        existing.prior_nack_reasons = []
        if reason:
            existing.summary_of_assessment = reason

    # Append to the decision log; cap to last 20 (distill-on-write).
    files_label = f" [{', '.join(files_reviewed)}]" if files_reviewed else ""
    # Splitlines guards against multi-line reasons; the explicit empty
    # default guards against a whitespace-only reason whose ``splitlines``
    # returns ``[]`` (``reason or ''`` is truthy, so a naive ``[0]`` lookup
    # would IndexError — reachable via ``brc_ack`` which has no
    # 50-char NACK-reason minimum).
    reason_lines = (reason or "").strip().splitlines()
    reason_first_line = reason_lines[0] if reason_lines else ""
    entry = (
        f"{_utc_now_iso()} {verdict_normalized.lower()} {producer_role}: "
        f"{reason_first_line}{files_label}"
    )
    memory.decision_log.append(entry)
    memory.decision_log = memory.decision_log[-_DECISION_LOG_CAP:]

    write_memory_atomic(memory, path)

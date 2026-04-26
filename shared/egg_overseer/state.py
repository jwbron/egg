"""On-disk schemas + helpers for overseer state files (issue #1962).

This module defines the Pydantic models and read/write helpers for the
two state files the overseer owns under ``.egg-state/oversight/``:

* ``filed-issues.jsonl`` — append-only JSON Lines log of issues the
  overseer has filed (or has been instructed by the human to skip);
  used for intra-phase dedup. Cross-phase dedup falls back to
  ``gh issue list --search "<8-char-signature>"`` because the local
  filesystem does not persist across phase containers.
* ``agent-timing.json`` — single-object JSON file the migrated detectors
  (TASK-6-1) read/write to track per-agent timing for stall /
  silent-agent / NACK / long-running-phase detection.

The helpers are deliberately lightweight: no orchestrator dependency, no
DB driver. The advisor-strategy pipeline runs fast enough that file I/O
on bounded JSON is not a bottleneck. Concurrent writers (overseer
respawns at phase boundaries) are protected by an ``fcntl.LOCK_EX``
flock on a sentinel ``agent-timing.lock`` file per the risk_analyst's
R-PERF-02 / R-SEC-04 mitigation.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class FiledIssueRecord(BaseModel):
    """One filed-issue record in ``filed-issues.jsonl``.

    ``issue_number`` may be None when ``hitl_outcome == "skipped"`` —
    the human declined to file the issue. Carrying the skip in the
    JSONL prevents the advisor from re-prompting on the same anomaly
    after an overseer respawn (per risk_analyst R-OP-03).
    """

    issue_number: int | None = None
    anomaly_type: str
    anomaly_signature: str  # 8-char prefix is embedded in the title.
    agent_role: str
    repo: str  # owner/repo (matches EGG_PIPELINE_REPO).
    pipeline_id: str
    phase: str
    filed_at: datetime  # decision time (filed OR skipped).
    parent_alert_message_id: str | None = None
    hitl_outcome: Literal["filed", "skipped", "modified_and_filed"] | None = None


class AgentTimingEntry(BaseModel):
    """Per-role timing snapshot consumed by the migrated detectors.

    Mirrors the fields ``/sdlc`` previously kept in its in-memory
    ``{role: {phase, phase_entered_at, …}}`` map (SKILL.md:568-570 in
    the pre-migration codebase).
    """

    role: str
    phase: str
    phase_entered_at: datetime
    first_seen_at: datetime
    nudged_at: datetime | None = None
    has_any_messages: bool = False
    last_alerted_at: datetime | None = None
    # Maps anomaly-type (e.g. "agent-stall") → last alert datetime.
    # Detectors check this map before emitting another alert for the
    # same anomaly+role+phase combo.
    alerted_anomalies: dict[str, datetime] = Field(default_factory=dict)


class AgentTimingState(BaseModel):
    """Top-level ``agent-timing.json`` envelope."""

    schema_version: int = 1
    pipeline_id: str
    entries: dict[str, AgentTimingEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSONL helpers (filed-issues)
# ---------------------------------------------------------------------------

# Header line written verbatim on first append. Hand-written dict literal
# (NOT a Pydantic dump) because Pydantic v2 strips underscore-prefixed
# names from JSON output by default — the leading underscore on
# ``_kind`` is preserved deliberately to avoid colliding with any
# future field on FiledIssueRecord.
_HEADER_DICT: dict[str, object] = {"_kind": "header", "schema_version": 1}
_HEADER_LINE = json.dumps(_HEADER_DICT, sort_keys=True)


def _ensure_parent(path: str | os.PathLike[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)


def append_filed_issue(path: str | os.PathLike[str], record: FiledIssueRecord) -> None:
    """Append one ``FiledIssueRecord`` to ``path``.

    Writes the header line on first creation. Each record line is
    ``record.model_dump_json()``. Calls ``fsync`` so the record is
    durable before the function returns.

    Concurrency: acquires ``fcntl.LOCK_EX`` via the same
    ``agent-timing.lock`` sentinel used by ``save_agent_timing``
    so concurrent overseer respawns don't interleave records (POSIX
    only guarantees atomic writes ≤ ``PIPE_BUF`` bytes; record JSON
    can exceed that).

    Args:
        path: Filesystem path to ``filed-issues.jsonl``.
        record: Record to append.
    """
    p = Path(path)
    _ensure_parent(p)
    with _file_lock(p):
        write_header = not p.exists()
        with open(p, "a", encoding="utf-8") as f:
            if write_header:
                f.write(_HEADER_LINE + "\n")
            f.write(record.model_dump_json() + "\n")
            f.flush()
            os.fsync(f.fileno())


def load_filed_issues(
    path: str | os.PathLike[str],
) -> list[FiledIssueRecord]:
    """Stream ``filed-issues.jsonl`` and return all records.

    Validates the header on line 1. Skips malformed data lines with a
    warning rather than aborting (so a single corrupt record doesn't
    block the dedup path entirely). Returns an empty list when the file
    does not exist.

    Args:
        path: Filesystem path to ``filed-issues.jsonl``.

    Returns:
        List of ``FiledIssueRecord`` in append order.

    Raises:
        ValueError: if the header line is missing, malformed, or carries
            an unknown ``schema_version``.
    """
    p = Path(path)
    if not p.exists():
        return []
    records: list[FiledIssueRecord] = []
    with open(p, encoding="utf-8") as f:
        first = f.readline()
        if not first.strip():
            return records
        try:
            header = json.loads(first)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"load_filed_issues: header line is not valid JSON: {first!r}"
            ) from exc
        if header.get("_kind") != "header":
            raise ValueError(f"load_filed_issues: header line missing _kind=header: {header!r}")
        if header.get("schema_version") != 1:
            raise ValueError(
                f"load_filed_issues: unknown schema_version "
                f"{header.get('schema_version')!r}; this overseer only "
                f"understands schema_version=1"
            )
        for line_no, line in enumerate(f, start=2):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                records.append(FiledIssueRecord.model_validate(payload))
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "load_filed_issues: skipping malformed line %d: %s",
                    line_no,
                    exc,
                )
    return records


# ---------------------------------------------------------------------------
# Agent-timing helpers (single-object JSON, atomic write, flock-protected)
# ---------------------------------------------------------------------------


def _lock_path_for(path: Path) -> Path:
    return path.parent / f"{path.name}.lock"


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Acquire ``fcntl.LOCK_EX`` for the read-modify-write critical section.

    Per risk_analyst R-PERF-02 / R-SEC-04 mitigation: concurrent overseer
    respawns at phase boundaries can race on the read step, so the
    tmp+rename atomicity (which only protects the rename) is not
    sufficient. The lock sentinel is created on first access; cleanup
    happens via worktree teardown.
    """
    _ensure_parent(path)
    lock_path = _lock_path_for(path)
    # Create on first access, mode 0644 — the lock file is empty.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def load_agent_timing(path: str | os.PathLike[str], *, pipeline_id: str = "") -> AgentTimingState:
    """Load ``agent-timing.json`` (returns a fresh state if missing).

    Acquires the flock for the read step so concurrent writers cannot
    truncate the file mid-read.

    Args:
        path: Filesystem path to ``agent-timing.json``.
        pipeline_id: Pipeline id to embed when the file is missing —
            callers that load before any writes should pass the current
            pipeline id so the returned state is well-formed.

    Returns:
        Parsed ``AgentTimingState``. Returns a default state with the
        passed pipeline_id when the file does not exist.
    """
    p = Path(path)
    with _file_lock(p):
        if not p.exists():
            return AgentTimingState(pipeline_id=pipeline_id)
        with open(p, encoding="utf-8") as f:
            payload = json.load(f)
    return AgentTimingState.model_validate(payload)


def save_agent_timing(state: AgentTimingState, path: str | os.PathLike[str]) -> None:
    """Persist ``state`` to ``path`` atomically via tempfile + rename.

    Acquires the flock for the read-modify-write critical section even
    though only the write side is guarded here, because callers that
    follow load → mutate → save expect both halves to be flock-protected.
    """
    p = Path(path)
    with _file_lock(p):
        _ensure_parent(p)
        # NamedTemporaryFile with delete=False so we can os.replace.
        tmp_dir = str(p.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=tmp_dir,
            delete=False,
            prefix=f".{p.name}.",
            suffix=".tmp",
        ) as tmp:
            tmp.write(state.model_dump_json())
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, p)


# ---------------------------------------------------------------------------
# Anomaly signature
# ---------------------------------------------------------------------------


def compute_anomaly_signature(
    anomaly_type: str,
    agent_role: str,
    repo: str,
    tier1_alert_types: tuple[str, ...] = (),
) -> str:
    """Deterministic 16-hex signature used for filed-issue dedup.

    Field set is ``(anomaly_type, agent_role, repo, sorted(tier1_alert_types))``
    per ``decision-5`` (per-repo dedup) + risk_analyst HR-06. Sorting
    the Tier-1 list makes the signature order-independent.

    The first 8 hex characters are embedded in the issue title so
    ``gh issue list --search "<sig8>"`` can find the existing record
    after the local JSONL cache is gone (cross-phase dedup fallback).

    Args:
        anomaly_type: Stable kebab-case anomaly identifier (e.g.
            ``"agent-loop"``).
        agent_role: Role of the affected agent (e.g. ``"coder"``).
        repo: ``owner/repo`` string from ``EGG_PIPELINE_REPO``.
        tier1_alert_types: Tuple of Tier-1 health-alert type names that
            were active at signature time. Sorted internally so the
            order callers pass them in does not matter. Default empty
            tuple preserves today's behavior when the Tier-1 list is
            empty (the most common case in practice).

    Returns:
        16-character hex string (the leading 16 hex digits of SHA-1).
    """
    sorted_tier1 = sorted(tier1_alert_types)
    payload = "|".join([anomaly_type, agent_role, repo, ",".join(sorted_tier1)])
    digest = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
    return digest[:16]


__all__ = [
    "FiledIssueRecord",
    "AgentTimingEntry",
    "AgentTimingState",
    "append_filed_issue",
    "load_filed_issues",
    "load_agent_timing",
    "save_agent_timing",
    "compute_anomaly_signature",
]

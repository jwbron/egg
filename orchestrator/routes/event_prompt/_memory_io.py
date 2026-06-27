"""Worktree IO + memory parsing for the standalone wrapper-bash CLI.

Reads the durable BRC memory file and the worktree contract file, and
resolves the pipeline-scoped paths/identifiers — all fail-soft because
this module runs standalone via the wrapper bash (no orchestrator
package context). AST-identical to the pre-split definitions — pure
refactor (#3312 slice-6).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ._caps import _LAST_REVIEWED_SHA_RE, _MEMORY_MODE_FULL, _PRODUCER_HEADING_RE
from ._render_task import _issue_anchor_fallback


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

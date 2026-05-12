"""
Orchestrator-side post-apply hook for the apply_epic agent (#1557 N1).

The apply_epic agent runs in the sandbox and writes its
:class:`EpicApplyArtifact` to
``.egg-state/agent-outputs/<prefix>-epic-apply.json``.  This module is
the **consumer half** of that handoff:
:func:`merge_epic_apply_from_agent_outputs` reads the file (atomically
via ``Path.read_text`` against a file written through ``os.replace``),
validates it against the :class:`EpicApplyArtifact` schema, and merges
into ``Pipeline.phases["plan"].artifacts["epic_apply"]`` via the
existing :meth:`Pipeline.set_epic_apply` helper.

This closes reviewer_code v4 BLOCKER N1: previously the producer side
was wired but no consumer ever called ``set_epic_apply()``, so re-runs
saw no prior state and re-issued every ``createJiraIssue``.

Merge semantics:

* Idempotent — the orchestrator can call the merger multiple times
  on the same artifact (e.g. cycle through BRC re-reviews) without
  drift.  The artifact's ``applied_edits[]`` entries with
  ``status="applied"`` carry their own idempotency seed.
* Validation-first — malformed artifacts log a structured error and
  refuse to merge rather than silently corrupting the pipeline state.
* Existing-artifact merge — when the pipeline already has an
  ``epic_apply`` artifact (e.g. from a prior apply cycle), the new
  artifact's ``applied_edits`` / ``wont_do_batch`` / ``in_flight_gates``
  are unioned by stable keys (target / child_key) so re-spawned
  agents don't lose earlier work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Add shared directory to path for egg_logging.
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str, **kwargs: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.epic_apply_merge")


def _agent_outputs_path(repo_path: Path, *, issue_number: int | None, pipeline_id: str) -> Path:
    """Return the path the apply_epic agent writes to.

    Mirrors the prefix-derivation pattern from
    ``orchestrator.jira_epic_inputs.write_inputs_to_agent_outputs``
    so both producer (agent) and consumer (this module) agree on the
    same filename without a separate config knob.
    """
    prefix = str(issue_number) if issue_number is not None else pipeline_id
    return repo_path / ".egg-state" / "agent-outputs" / f"{prefix}-epic-apply.json"


def _merge_applied_edits(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union ``applied_edits`` lists by (kind, target, summary_hash).

    Preserves the existing entries' status; incoming entries with the
    same key replace only when the existing status is not ``applied``
    (so a previously-applied mutation can't regress to ``pending``).
    """

    def key(entry: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(entry.get("kind", "")),
            str(entry.get("target", "")),
            str(entry.get("summary_hash", "")),
        )

    out: dict[tuple[str, str, str], dict[str, Any]] = {key(e): e for e in existing}
    for entry in incoming:
        k = key(entry)
        prior = out.get(k)
        if prior is None:
            out[k] = entry
        elif prior.get("status") != "applied":
            out[k] = entry
    return list(out.values())


def _merge_by_child_key(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Union lists keyed by ``child_key`` (used for wont_do + in_flight gates)."""
    out: dict[str, dict[str, Any]] = {
        str(e.get("child_key", "")): e for e in existing if e.get("child_key")
    }
    for entry in incoming:
        ck = str(entry.get("child_key", ""))
        if not ck:
            continue
        out[ck] = entry
    return list(out.values())


def merge_epic_apply_from_agent_outputs(
    pipeline: Any,
    *,
    repo_path: Path | str,
    issue_number: int | None,
    pipeline_id: str,
) -> bool:
    """Read the apply_epic artifact file and merge it into ``pipeline``.

    Returns:
        ``True`` when an artifact was found, validated, and merged.
        ``False`` when no file exists (the apply_epic agent didn't run
        or ran with no work).  Validation failures return ``False`` and
        log a structured error — the caller (orchestrator post-apply
        hook) leaves the pipeline state untouched and surfaces a HITL
        gate so the operator can intervene.
    """
    try:
        from models import EpicApplyArtifact
    except ImportError:  # pragma: no cover
        from orchestrator.models import EpicApplyArtifact  # type: ignore[no-redef]

    repo = Path(repo_path)
    target = _agent_outputs_path(repo, issue_number=issue_number, pipeline_id=pipeline_id)
    if not target.exists():
        return False

    try:
        raw = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        logger.error(
            "epic_apply_artifact_unreadable",
            path=str(target),
            error=str(exc),
        )
        return False

    if not isinstance(raw, dict):
        logger.error(
            "epic_apply_artifact_malformed",
            path=str(target),
            reason="top-level value is not a mapping",
        )
        return False

    # Validate the incoming payload BEFORE merging — corrupted payloads
    # must not overwrite known-good state.
    try:
        incoming = EpicApplyArtifact.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "epic_apply_artifact_invalid",
            path=str(target),
            error=str(exc),
        )
        return False

    # Merge with any existing artifact on the pipeline so partial-batch
    # re-runs don't lose earlier work.
    existing = pipeline.get_epic_apply()
    if existing is None:
        merged = incoming
    else:
        existing_dict = existing.model_dump()
        incoming_dict = incoming.model_dump()
        merged_dict = {
            "version": incoming_dict.get("version") or existing_dict.get("version") or 1,
            # Idempotency seed: prefer the existing one so re-runs
            # converge against the same Atlassian-idempotency-key
            # namespace.
            "idempotency_seed": existing_dict.get("idempotency_seed")
            or incoming_dict.get("idempotency_seed"),
            # SHA: the incoming write replaces the prior (the apply
            # step just stamped a fresh value).
            "refine_description_sha256": incoming_dict.get("refine_description_sha256")
            or existing_dict.get("refine_description_sha256"),
            "plan_node_to_jira_key": {
                **(existing_dict.get("plan_node_to_jira_key") or {}),
                **(incoming_dict.get("plan_node_to_jira_key") or {}),
            },
            "applied_edits": _merge_applied_edits(
                existing_dict.get("applied_edits") or [],
                incoming_dict.get("applied_edits") or [],
            ),
            "wont_do_batch": _merge_by_child_key(
                existing_dict.get("wont_do_batch") or [],
                incoming_dict.get("wont_do_batch") or [],
            ),
            "in_flight_gates": _merge_by_child_key(
                existing_dict.get("in_flight_gates") or [],
                incoming_dict.get("in_flight_gates") or [],
            ),
        }
        merged = EpicApplyArtifact.model_validate(merged_dict)

    pipeline.set_epic_apply(merged)
    logger.info(
        "epic_apply_artifact_merged",
        pipeline_id=pipeline_id,
        applied_edits=len(merged.applied_edits),
        wont_do_batch=len(merged.wont_do_batch),
        in_flight_gates=len(merged.in_flight_gates),
    )
    return True


__all__ = [
    "merge_epic_apply_from_agent_outputs",
]

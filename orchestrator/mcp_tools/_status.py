"""PipelineToolHandler status snapshot + pending-decision/reviewer-feedback enrichment handlers (#3312 slice-13).

Method bodies extracted verbatim from the pre-split
``orchestrator/mcp_tools.py`` and bound onto ``PipelineToolHandler``
in the package barrel (``orchestrator/mcp_tools/__init__.py``). They
take ``self`` explicitly and are AST-identical to the originals.
Barrel globals (``logger`` etc.) are imported from the package so
they stay a single binding.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp_tools import logger


def _handle_get_status(self, args: dict[str, Any]) -> dict[str, Any]:
    """Get enriched pipeline status.

    Fetches pipeline state, agent executions, decisions, and recent messages.
    For phase_gate decisions, includes draft document content so the
    caller can present it to the user without needing filesystem access.
    Falls back gracefully if messages fail.

    The optional ``wait`` parameter is handled in the async tool wrapper
    (``mcp_server._make_tool_fn``) before this sync handler runs, so no
    worker thread is held during the delay.
    """
    return self._build_status_snapshot(args["task_id"])


def _live_running_agents_fallback(self, task_id: str) -> list[dict[str, Any]]:
    """Live running-agent view from the ``/status`` concurrent block (#3230).

    Used to backfill ``get_status.running_agents`` when the persisted
    phase-agent list is empty under the orchestrator-owned event loop
    (#3164). The ``/status`` endpoint's ``concurrent.agents`` is
    server-side reconstructed from live Job labels (see
    ``routes.pipelines._live_event_agents``), so it reflects role pods
    that are actually ``Running``. ``task_id`` must already be URL-quoted
    (the caller quotes it once for both requests). Best-effort: any
    failure or a non-concurrent phase yields ``[]``.
    """
    try:
        status_result = self._make_request(f"/api/v1/pipelines/{task_id}/status")
        concurrent = status_result.get("data", {}).get("concurrent", {}) or {}
    except Exception as e:
        # Symmetric with routes.pipelines._live_event_agents: a repeatedly
        # failing /status round-trip is otherwise invisible (#3230).
        logger.debug(
            "Live running-agent backfill query failed (#3230)",
            task_id=task_id,
            error=str(e),
        )
        return []
    agents = concurrent.get("agents", []) or []
    return [a for a in agents if a.get("status") == "running"]


def _build_status_snapshot(self, raw_task_id: str) -> dict[str, Any]:
    """Build the full enriched status snapshot for a pipeline.

    Args:
        raw_task_id: Pipeline/task ID (unquoted).

    Returns:
        The enriched status dict: ``pipeline``, ``current_phase``,
        ``status``, ``running_agents``, ``completed_agents``,
        ``phase_started_at`` / ``phase_elapsed_seconds``,
        ``pending_decisions`` (with draft content enrichment),
        ``recent_messages``. When the pipeline is wedged between
        phases (#2166), also includes ``wedged_no_successor`` with
        ``phase`` / ``completed_at`` / ``since_seconds``.
    """
    task_id = quote(raw_task_id, safe="")

    # Primary: pipeline state
    pipeline_result = self._make_request(f"/api/v1/pipelines/{task_id}")
    pipeline_data = pipeline_result.get("data", {}).get("pipeline", {})

    # Extract PR info from the pipeline's top-level ``pr_url`` /
    # ``pr_number`` fields (#1625, #2777 cq-4). The PR phase was
    # removed; the context PR opens up-front via
    # ``_open_context_pr_at_implement_start`` which persists the URL
    # and number directly on the pipeline record so monitoring
    # clients can pick them up without a separate ``gh pr list``.
    pr_url = pipeline_data.get("pr_url")
    raw_pr_number = pipeline_data.get("pr_number")
    pr_number: int | None = int(raw_pr_number) if isinstance(raw_pr_number, int) else None
    if pr_url and pr_number is None:
        match = re.search(r"/pull/(\d+)", pr_url)
        if match:
            pr_number = int(match.group(1))

    # Build status from pipeline data
    pipeline_info: dict[str, Any] = {
        "id": pipeline_data.get("id", ""),
        "repo": pipeline_data.get("repo", ""),
        "issue_number": pipeline_data.get("issue_number"),
        "created_at": pipeline_data.get("created_at", ""),
    }
    if pr_url:
        pipeline_info["pr_url"] = pr_url
        if pr_number is not None:
            pipeline_info["pr_number"] = pr_number

    status: dict[str, Any] = {
        "current_phase": pipeline_data.get("current_phase", ""),
        "status": pipeline_data.get("status", ""),
        "pipeline": pipeline_info,
    }

    # Extract agent info from phases. ``phases`` was previously
    # also used for PR-info extraction above, but that lookup was
    # rewired in #2777 to read ``pipeline_data["pr_url"]`` /
    # ``pipeline_data["pr_number"]`` directly. The per-phase agent
    # iteration below still needs the phases map, so bind it here.
    phases = pipeline_data.get("phases") or {}
    current_phase_key = pipeline_data.get("current_phase", "")
    phase_data = phases.get(current_phase_key, {})
    agents = phase_data.get("agents", [])
    status["running_agents"] = [a for a in agents if a.get("status") == "running"]
    status["completed_agents"] = [a for a in agents if a.get("status") == "complete"]

    # Under the orchestrator-owned BRC event loop (#3164, unconditional)
    # role pods are on-demand one-shots that are never persisted into the
    # phase's agent list, so the persisted view above is empty even while
    # role pods are Running — a blind dashboard (#3230). Backfill
    # ``running_agents`` from the ``/status`` endpoint's live
    # ``concurrent.agents`` view (server-side reconstructed from live Job
    # labels) so the dashboard reflects the live cohort during
    # event-loop-owned phases. Stays empty when no pod is live, so
    # between-spawn quiescence is not misreported as running.
    if not status["running_agents"]:
        status["running_agents"] = self._live_running_agents_fallback(task_id)

    # Server-computed timing (#1702)
    now = datetime.now(UTC)

    phase_started_at = phase_data.get("started_at")
    if phase_started_at:
        try:
            started_dt = datetime.fromisoformat(phase_started_at)
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=UTC)
            status["phase_started_at"] = started_dt.isoformat()
            status["phase_elapsed_seconds"] = max(0, int((now - started_dt).total_seconds()))
        except ValueError, TypeError:
            pass

    for agent in status["running_agents"]:
        agent_started_at = agent.get("started_at")
        if agent_started_at:
            try:
                agent_dt = datetime.fromisoformat(agent_started_at)
                if agent_dt.tzinfo is None:
                    agent_dt = agent_dt.replace(tzinfo=UTC)
                agent["elapsed_seconds"] = max(0, int((now - agent_dt).total_seconds()))
            except ValueError, TypeError:
                pass

    # Extract decisions
    decisions = pipeline_data.get("decisions", [])
    status["pending_decisions"] = [d for d in decisions if d.get("status") == "pending"]

    # Watchdog: flag a pipeline that is nominally RUNNING but stalled
    # between phases — the current phase reports COMPLETE, no HITL gate
    # is pending, yet no successor has been scheduled within the
    # threshold (#2166). Lets operators fail loudly within a minute
    # instead of polling for 10+ min hoping to spot the absence of
    # progress.
    if (
        pipeline_data.get("status") == "running"
        and not status["pending_decisions"]
        and current_phase_key
        and phase_data.get("status") == "complete"
    ):
        phase_completed_at = phase_data.get("completed_at")
        if phase_completed_at:
            try:
                completed_dt = datetime.fromisoformat(phase_completed_at)
                if completed_dt.tzinfo is None:
                    completed_dt = completed_dt.replace(tzinfo=UTC)
                since_seconds = int((now - completed_dt).total_seconds())
                if since_seconds > 60:
                    status["wedged_no_successor"] = {
                        "phase": current_phase_key,
                        "completed_at": completed_dt.isoformat(),
                        "since_seconds": since_seconds,
                    }
            except ValueError, TypeError:
                pass

    # Enrichment: recent messages (optional)
    try:
        messages_result = self._make_request(f"/api/v1/pipelines/{task_id}/messages?limit=10")
        raw_messages = messages_result.get("data", {}).get("messages", [])
        status["recent_messages"] = [
            {
                "from_role": m.get("from_role", ""),
                "type": m.get("message_type", ""),
                "subject": m.get("subject", ""),
                "timestamp": m.get("timestamp", ""),
            }
            for m in raw_messages
        ]
    except Exception:
        logger.debug("Failed to fetch messages", task_id=task_id)

    # Enrichment: attach draft content to pending decisions (optional)
    self._enrich_pending_decisions(status, raw_task_id, pipeline_data)

    return status


def _enrich_pending_decisions(
    self,
    status: dict[str, Any],
    pipeline_id: str,
    pipeline_data: dict[str, Any],
) -> None:
    """Attach draft content and agent summaries to pending decisions.

    For all decision types (phase_gate, choice, feedback), reads the
    phase's draft document from the pipeline worktree and attaches it
    as ``draft_content`` so the caller can present context to the user.
    Agent summaries and reviewer feedback are attached only to
    phase_gate decisions.

    Mutates ``status["pending_decisions"]`` in place.
    """
    pending = status.get("pending_decisions", [])
    if not pending:
        return

    # Build completed agents summary (phase_gate only)
    completed_agents = status.get("completed_agents", [])
    agents_summary = [
        {
            "role": a.get("role", ""),
            "status": a.get("status", ""),
        }
        for a in completed_agents
    ]

    # Resolve repo path to read drafts from the worktree
    repo = pipeline_data.get("repo", "")
    issue_number = pipeline_data.get("issue_number")
    current_phase = status.get("current_phase", "")

    # Resolve worktree path once (invariant across decisions)
    worktree_path = None
    _read_phase_draft = None
    if repo:
        try:
            from orchestrator.routes import resolve_worktree_path, resolve_worktree_repo_path
            from orchestrator.routes.pipelines import _read_phase_draft

            env_path = os.environ.get("EGG_REPO_PATH", "/home/egg/repos")
            base_path = Path(env_path)
            repo_name = repo.split("/")[-1]
            repo_path = resolve_worktree_repo_path(base_path, repo_name)
            worktree_path = resolve_worktree_path(pipeline_id, repo_path)
        except Exception:
            logger.debug(
                "Failed to resolve worktree for decision enrichment",
                pipeline_id=pipeline_id,
            )

    # Attach draft_content to all pending decisions from draft-producing phases
    for decision in pending:
        decision_phase = decision.get("phase") or current_phase
        draft_content = None
        if worktree_path is not None and _read_phase_draft is not None:
            try:
                draft_content = _read_phase_draft(
                    worktree_path,
                    decision_phase,
                    issue_number=issue_number,
                    pipeline_id=pipeline_id,
                    max_chars=16_000,
                    branch=pipeline_data.get("branch"),
                )
            except Exception:
                logger.debug(
                    "Failed to read draft for decision enrichment",
                    pipeline_id=pipeline_id,
                )

        if draft_content is not None:
            decision["draft_content"] = draft_content

        # Phase-gate-specific enrichments
        if decision.get("decision_type") == "phase_gate":
            if agents_summary:
                decision["completed_agents_summary"] = agents_summary

            reviewer_feedback = self._read_reviewer_feedback(
                worktree_path,
                decision_phase,
                issue_number,
                pipeline_id,
            )
            if reviewer_feedback:
                decision["reviewer_feedback"] = reviewer_feedback


def _read_reviewer_feedback(
    self,
    worktree_path: Path | None,
    phase: str,
    issue_number: int | None,
    pipeline_id: str,
    max_chars: int = 16_000,
) -> list[dict[str, str]]:
    """Read reviewer feedback from .egg-state/reviews/ for a given phase.

    Returns a list of dicts with reviewer, verdict, summary, analysis, suggestions,
    and feedback fields. Caps total content at max_chars.
    """
    if worktree_path is None:
        return []

    reviews_dir = worktree_path / ".egg-state" / "reviews"
    if not reviews_dir.is_dir():
        return []

    try:
        from orchestrator.routes.pipelines import _pipeline_identifier
    except ImportError:
        return []

    identifier = _pipeline_identifier(issue_number, pipeline_id)
    prefix = f"{identifier}-{phase}-"

    feedback: list[dict[str, str]] = []
    total_chars = 0

    try:
        review_files = sorted(reviews_dir.glob(f"{prefix}*-review.json"))
    except Exception:
        return []

    import json

    for i, review_file in enumerate(review_files):
        try:
            data = json.loads(review_file.read_text(encoding="utf-8"))
            # Extract reviewer type from filename:
            # e.g. "42-refine-refiner-review.json" -> "refiner"
            stem = review_file.stem  # "42-refine-refiner-review"
            stem = stem.removesuffix("-review")
            reviewer_type = stem.removeprefix(f"{identifier}-{phase}-")

            entry = {
                "reviewer": reviewer_type,
                "verdict": data.get("verdict", "unknown"),
                "summary": data.get("summary", ""),
                "analysis": data.get("analysis", ""),
                "suggestions": data.get("suggestions", ""),
                "feedback": data.get("feedback", ""),
            }

            entry_chars = sum(len(v) for v in entry.values())
            if total_chars + entry_chars > max_chars:
                remaining = len(review_files) - i
                feedback.append(
                    {
                        "reviewer": f"({remaining} more reviewer(s) omitted)",
                        "verdict": "truncated",
                        "summary": "Content limit reached. Review files directly.",
                        "analysis": "",
                        "suggestions": "",
                        "feedback": "",
                    }
                )
                break
            total_chars += entry_chars
            feedback.append(entry)
        except Exception:
            logger.debug(
                "Failed to read review file",
                path=str(review_file),
            )
            continue

    return feedback


def _handle_get_phase(self, args: dict[str, Any]) -> dict[str, Any]:
    """Get current phase details for a pipeline."""
    task_id = quote(args["task_id"], safe="")
    return self._make_request(f"/api/v1/pipelines/{task_id}/phase")

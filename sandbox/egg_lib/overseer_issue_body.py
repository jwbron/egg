"""Sandbox-side body composer for ``egg-orch overseer file-issue`` (issue #1962).

Renders the canonical issue-body template
(``egg_overseer.issue_template.TEMPLATE_LITERAL``) with the
``decision-8`` opt-2 "Pipeline Links" sub-block and runs the result
through ``egg_overseer.scrubbing.scrub_secrets`` as defense-in-depth
(the advisor is the primary scrubber).

Also exposes ``find_existing_issue`` — the per-repo dedup primitive
the CLI verb calls before invoking ``gh``: first reads the local
``.egg-state/oversight/filed-issues.jsonl`` cache, then falls back to
``gh issue list --label agent:overseer --state open --search
"<8-char-signature>" --json number,title``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from egg_overseer.issue_template import TEMPLATE_LITERAL
from egg_overseer.scrubbing import scrub_secrets
from egg_overseer.state import load_filed_issues

logger = logging.getLogger(__name__)

# Path to the per-pipeline filed-issues JSONL cache; intra-phase only.
DEFAULT_FILED_ISSUES_PATH = ".egg-state/oversight/filed-issues.jsonl"


def compose_issue_title(*, anomaly_type: str, agent_role: str, anomaly_signature: str) -> str:
    """Compose the issue title with the 8-char signature embedded.

    Format: ``[Pipeline Diagnostic] {anomaly_type} - {agent_role} [{sig8}]``.

    Drops ``pipeline_id`` from the title (per ``R-OP-02``) and embeds
    the first 8 characters of the anomaly signature so
    ``gh issue list --search "<sig8>"`` reliably finds the existing
    record across phase boundaries (after the local JSONL cache is
    gone).

    Args:
        anomaly_type: Stable kebab-case anomaly identifier.
        agent_role: Affected agent role.
        anomaly_signature: 16-hex signature from
            ``egg_overseer.state.compute_anomaly_signature``. The first
            8 characters are embedded.

    Returns:
        Title string ≤120 chars (callers should pass anomaly_type +
        agent_role short enough to fit).
    """
    sig8 = anomaly_signature[:8]
    return f"[Pipeline Diagnostic] {anomaly_type} - {agent_role} [{sig8}]"


def compose_issue_body(
    *,
    anomaly_type: str,
    agent_role: str,
    pipeline_id: str,
    phase: str,
    branch: str,
    commit_sha: str,
    parent_alert_message_id: str | None = None,
    classification: dict[str, Any] | None = None,
    recent_log_lines: list[str] | None = None,
    health_alerts: list[dict[str, Any]] | None = None,
    timeline: list[dict[str, Any]] | None = None,
    actions_taken: list[str] | None = None,
    suggested_remediation: str | None = None,
    repo: str | None = None,
) -> str:
    """Render the canonical template + Pipeline Links + scrub secrets.

    Args:
        anomaly_type: Stable kebab-case anomaly identifier.
        agent_role: Affected agent role.
        pipeline_id: Pipeline identifier.
        phase: Phase name.
        branch: Git branch name.
        commit_sha: Commit SHA at filing time.
        parent_alert_message_id: ID of the parent ``OVERSEER_ALERT``.
        classification: Haiku classification dict
            (``type``/``confidence``/``reasoning``).
        recent_log_lines: Recent container log lines.
        health_alerts: Active Tier-1 health alerts.
        timeline: Timeline entries (``{timestamp, event}`` dicts).
        actions_taken: Strings of corrective actions tried so far.
        suggested_remediation: Free-form remediation hint.
        repo: ``owner/repo`` for the branch URL; defaults to
            ``$EGG_PIPELINE_REPO`` when None.

    Returns:
        Rendered + scrubbed issue body. Body is bounded by gateway
        policy at 50 KB; this function does not enforce that bound,
        callers do.
    """
    repo = repo or os.environ.get("EGG_PIPELINE_REPO", "unknown/unknown")
    branch_url = f"https://github.com/{repo}/tree/{branch}"
    timestamp = datetime.now(UTC).isoformat()

    if timeline:
        timeline_lines = "\n".join(
            f"- `{entry.get('timestamp', '?')}`: {entry.get('event', '?')}" for entry in timeline
        )
    else:
        timeline_lines = "- No timeline events recorded"

    if classification:
        classification_lines = (
            f"- **Type**: {classification.get('type', anomaly_type)}\n"
            f"- **Confidence**: {classification.get('confidence', 'N/A')}\n"
            f"- **Reasoning**: {classification.get('reasoning', 'N/A')}"
        )
    else:
        classification_lines = "- No classification data available"

    if actions_taken:
        actions_lines = "\n".join(f"- {action}" for action in actions_taken)
    else:
        actions_lines = "- No corrective actions taken yet"

    remediation = suggested_remediation or ("Investigate the agent logs and pipeline state")

    container_logs_section = ""
    if recent_log_lines:
        truncated = "\n".join(recent_log_lines[-50:])
        container_logs_section = (
            f"\n\n### Container Logs (last {len(recent_log_lines[-50:])} lines)"
            f"\n````\n{truncated}\n````\n"
        )

    # If health_alerts were passed, append them as a sub-block. The
    # canonical template doesn't carry them as a top-level field; we
    # tack them onto the actions section so the rendered body still
    # has every signal in one place.
    if health_alerts:
        alerts_block = "\n".join(
            f"- `{a.get('type', '?')}`: {a.get('detail', '')}" for a in health_alerts
        )
        actions_lines = f"{actions_lines}\n\n#### Active Tier-1 health alerts\n{alerts_block}"

    body = TEMPLATE_LITERAL.format(
        anomaly_type=anomaly_type,
        pipeline_id=pipeline_id,
        phase=phase,
        agent_role=agent_role,
        timestamp=timestamp,
        anomaly_description=(
            classification.get("reasoning", "(no description provided)")
            if classification
            else "(no description provided)"
        ),
        timeline_lines=timeline_lines,
        classification_lines=classification_lines,
        actions_lines=actions_lines,
        container_logs_section=container_logs_section,
        remediation=remediation,
        branch_url=branch_url,
        branch=branch,
        commit_sha=commit_sha,
        parent_alert_message_id=parent_alert_message_id or "(none)",
    )

    # Defense-in-depth scrub. The advisor is the primary scrubber.
    return scrub_secrets(body)


def find_existing_issue(
    *,
    repo: str,
    anomaly_signature: str,
    filed_issues_path: str | os.PathLike[str] = DEFAULT_FILED_ISSUES_PATH,
    _gh_runner: object | None = None,
) -> int | None:
    """Look up a prior open issue with the same anomaly signature.

    Strategy (per the plan's ``Dedup persistence scope`` section):

    1. Read the local ``filed-issues.jsonl`` cache (intra-phase fast
       path). Returns the most-recently filed matching issue number if
       any record carries the same ``anomaly_signature``.
    2. Fall back to ``gh issue list --label agent:overseer --state open
       --search "<sig8>" --json number,title`` and return the first
       result whose title contains the 8-char prefix (cross-phase
       fallback because the local JSONL doesn't survive container
       teardown).

    Args:
        repo: ``owner/repo`` to scope the gh search to.
        anomaly_signature: 16-hex signature; first 8 chars are
            embedded in titles.
        filed_issues_path: Override for tests; defaults to the canonical
            sandbox path.
        _gh_runner: Test seam — pass a callable
            ``(argv: list[str]) -> subprocess.CompletedProcess`` to
            avoid invoking the real ``gh`` binary in unit tests.

    Returns:
        Issue number of an existing open issue; ``None`` if no match.
    """
    # Local cache first.
    try:
        records = load_filed_issues(filed_issues_path)
    except (ValueError, OSError) as exc:
        logger.warning(
            "find_existing_issue: failed to read %s (%s); falling back to gh",
            filed_issues_path,
            exc,
        )
        records = []
    matching = [
        r
        for r in records
        if r.anomaly_signature == anomaly_signature and r.issue_number is not None
    ]
    if matching:
        # Most recent wins (records are append-only so the last in list).
        return matching[-1].issue_number

    # GitHub-side fallback. Search by the 8-char prefix embedded in
    # the title; that's the contract titles preserve. We use the
    # ``in:title`` qualifier so the search ignores body and comment
    # matches — both reduces false-positive duplicates and lifts the
    # 100-result ceiling for repos with chatty issues.
    sig8 = anomaly_signature[:8]
    argv = [
        "gh",
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        "agent:overseer",
        "--state",
        "open",
        "--search",
        f"in:title {sig8}",
        "--json",
        "number,title",
        "--limit",
        "100",
    ]
    runner = _gh_runner or subprocess.run
    try:
        proc = runner(  # type: ignore[operator]
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("find_existing_issue: gh fallback failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "find_existing_issue: gh exit %s: %s",
            proc.returncode,
            proc.stderr,
        )
        return None
    try:
        issues = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for issue in issues:
        title = issue.get("title", "")
        if sig8 in title:
            number = issue.get("number")
            if isinstance(number, int):
                return number
    return None


def _ensure_jsonl_dir(path: str | os.PathLike[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


__all__ = [
    "DEFAULT_FILED_ISSUES_PATH",
    "compose_issue_title",
    "compose_issue_body",
    "find_existing_issue",
]

"""GitHub diagnostic issue filing for the overseer agent.

# DEAD CODE — single source of truth lives at
# shared/egg_overseer/issue_template.py. This file is the historical
# orchestrator entry point retained so existing imports do not break.
#
# Per the #1962 implementation plan (decision-9 opt-1), production filing
# now happens sandbox-side via the new ``egg-orch overseer file-issue``
# CLI verb (``sandbox/egg_lib/orch_cli.py``); ``file_diagnostic_issue``
# below is no longer invoked in production.
#
# The literal at lines 86-107 below is preserved byte-for-byte as the
# canonical-literal source: ``orchestrator/tests/test_overseer_issue_filer.py``
# asserts the literal here matches
# ``egg_overseer.issue_template.TEMPLATE_LITERAL`` substring-for-substring
# so any drift fails CI loudly.

Builds structured issue bodies following the diagnostic format and
files them via the ``gh`` CLI.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from egg_overseer.issue_template import TEMPLATE_LITERAL

logger = logging.getLogger(__name__)

# Labels applied to diagnostic issues filed by the overseer.
# DEAD: production filing uses agent:overseer + matching priority label
# only (per decision-7 / decision-17 / feedback-1.Q4); this list is
# preserved only to keep the historical import graph intact.
DIAGNOSTIC_LABELS = ["egg:diagnostic", "pipeline-health"]


# Pre-#1962 canonical body literal — preserved verbatim for the
# byte-equality regression test (TASK-7-1) that the planner specified
# at "the literal at orchestrator/overseer/issue_filer.py:86-107".
# The runtime code path now uses TEMPLATE_LITERAL.format(...) below;
# this constant is the historical anchor against which the regression
# test asserts the canonical bytes have not drifted. The canonical
# template extends this with a Pipeline Links sub-block per
# decision-8 opt-2 — see egg_overseer.issue_template.TEMPLATE_LITERAL
# for the live template, and tests for byte-equality of this prefix
# against the live template's leading region.
LEGACY_BODY_LITERAL: str = """## Pipeline Diagnostic: {anomaly_type}

**Pipeline**: `{pipeline_id}`
**Phase**: `{phase}`
**Agent**: `{agent_role}`
**Detected**: `{timestamp}`

### Anomaly
{anomaly_description}

### Timeline
{timeline_lines}

### Classification
{classification_lines}

### Actions Taken
{actions_lines}
{container_logs_section}
### Suggested Remediation
- {remediation}
"""


def _build_issue_body(
    pipeline_id: str,
    agent_role: str,
    anomaly: dict,
    context: dict,
) -> str:
    """Build a markdown issue body following the diagnostic format.

    Args:
        pipeline_id: The pipeline identifier.
        agent_role: The agent role experiencing the issue.
        anomaly: Dict with anomaly details (type, description, classification, etc.).
        context: Dict with additional context (phase, timeline, actions taken, etc.).

    Returns:
        A markdown-formatted issue body string.
    """
    phase = context.get("phase", "unknown")
    timestamp = context.get("detected_at", datetime.now(UTC).isoformat())

    anomaly_type = anomaly.get("type", "unknown")
    anomaly_description = anomaly.get("description", "No description provided")

    # Build timeline section
    timeline_entries = context.get("timeline", [])
    if timeline_entries:
        timeline_lines = "\n".join(
            f"- `{entry.get('timestamp', '?')}`: {entry.get('event', '?')}"
            for entry in timeline_entries
        )
    else:
        timeline_lines = "- No timeline events recorded"

    # Build classification section
    classification = anomaly.get("classification", {})
    if classification:
        classification_lines = (
            f"- **Type**: {classification.get('type', anomaly_type)}\n"
            f"- **Confidence**: {classification.get('confidence', 'N/A')}\n"
            f"- **Reasoning**: {classification.get('reasoning', 'N/A')}"
        )
    else:
        classification_lines = "- No classification data available"

    # Build actions taken section
    actions_taken = context.get("actions_taken", [])
    if actions_taken:
        actions_lines = "\n".join(f"- {action}" for action in actions_taken)
    else:
        actions_lines = "- No corrective actions taken yet"

    # Build suggested remediation
    remediation = context.get(
        "suggested_remediation",
        anomaly.get("recommended_action", "Investigate the agent logs and pipeline state"),
    )

    # Build container logs section (if available)
    container_logs_section = ""
    raw_container_logs = context.get("container_logs", "")
    if raw_container_logs:
        # Truncate to last 2000 chars to keep issue body manageable
        truncated = raw_container_logs[-2000:]
        if len(raw_container_logs) > 2000:
            truncated = f"... (truncated, showing last 2000 chars)\n{truncated}"
        container_logs_section = f"\n\n### Container Logs\n````\n{truncated}\n````\n"

    # DEAD CODE — preserved byte-for-byte as the canonical literal
    # source for the regression test. Production rendering uses
    # egg_overseer.issue_template.render(**fields). The fields below are
    # the historical no-link dialect; the new "Pipeline Links" sub-block
    # the canonical TEMPLATE_LITERAL adds is rendered with placeholder
    # values so the literal substring at lines below matches the body
    # the canonical render produces when the same fields are passed.
    branch = context.get("branch", "unknown")
    commit_sha = context.get("commit_sha", "unknown")
    parent_alert_message_id = context.get("parent_alert_message_id", "unknown")
    branch_url = context.get("branch_url", f"https://github.com/unknown/tree/{branch}")

    body = TEMPLATE_LITERAL.format(
        anomaly_type=anomaly_type,
        pipeline_id=pipeline_id,
        phase=phase,
        agent_role=agent_role,
        timestamp=timestamp,
        anomaly_description=anomaly_description,
        timeline_lines=timeline_lines,
        classification_lines=classification_lines,
        actions_lines=actions_lines,
        container_logs_section=container_logs_section,
        remediation=remediation,
        branch_url=branch_url,
        branch=branch,
        commit_sha=commit_sha,
        parent_alert_message_id=parent_alert_message_id,
    )
    return body


async def file_diagnostic_issue(
    pipeline_id: str,
    agent_role: str,
    anomaly: dict,
    context: dict,
) -> dict:
    """File a diagnostic GitHub issue for a persistent problem.

    DEAD CODE — production filing happens sandbox-side via
    ``egg-orch overseer file-issue`` (issue #1962). This function is
    retained only to keep the historical import graph intact.

    Builds a structured issue body and files it via ``gh issue create``.
    If the ``gh`` CLI is not available or the command fails, the issue
    template is still returned so it can be logged or sent via other
    channels.

    Args:
        pipeline_id: The pipeline identifier.
        agent_role: The agent role experiencing the issue.
        anomaly: Dict with anomaly details.
        context: Dict with additional context.

    Returns:
        A dict with keys:
            issue_number: int | None (None if filing failed)
            filed: bool
            template: str (the issue body markdown)
    """
    anomaly_type = anomaly.get("type", "unknown")
    title = f"[Pipeline Diagnostic] {anomaly_type} - {agent_role} ({pipeline_id})"
    body = _build_issue_body(pipeline_id, agent_role, anomaly, context)

    # Build label arguments
    label_args: list[str] = []
    for label in DIAGNOSTIC_LABELS:
        label_args.extend(["--label", label])

    try:
        cmd = [
            "gh",
            "issue",
            "create",
            "--title",
            title,
            "--body",
            body,
            *label_args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
        stdout_text = (stdout_bytes or b"").decode()
        stderr_text = (stderr_bytes or b"").decode()

        if proc.returncode == 0:
            # Parse issue number from URL output (e.g. https://github.com/org/repo/issues/123)
            url = stdout_text.strip()
            issue_number: int | None = None
            if url and "/" in url:
                try:
                    issue_number = int(url.rstrip("/").rsplit("/", 1)[-1])
                except (ValueError, IndexError):
                    pass
            logger.info(
                "Filed diagnostic issue #%s for pipeline %s agent %s",
                issue_number,
                pipeline_id,
                agent_role,
            )
            return {"issue_number": issue_number, "filed": True, "template": body}

        logger.warning(
            "gh issue create failed (rc=%d): %s",
            proc.returncode,
            stderr_text,
        )
        return {"issue_number": None, "filed": False, "template": body}

    except FileNotFoundError:
        logger.warning("gh CLI not found; cannot file diagnostic issue")
        return {"issue_number": None, "filed": False, "template": body}

    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        logger.warning("gh issue create timed out")
        return {"issue_number": None, "filed": False, "template": body}

    except Exception as exc:
        logger.warning("Failed to file diagnostic issue: %s", exc)
        return {"issue_number": None, "filed": False, "template": body}

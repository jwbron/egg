"""GitHub diagnostic issue filing for the overseer agent.

Builds structured issue bodies following the diagnostic format and
files them via the ``gh`` CLI.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Labels applied to diagnostic issues filed by the overseer.
DIAGNOSTIC_LABELS = ["egg:diagnostic", "pipeline-health"]


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

    body = f"""## Pipeline Diagnostic: {anomaly_type}

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

### Suggested Remediation
- {remediation}
"""
    return body


async def file_diagnostic_issue(
    pipeline_id: str,
    agent_role: str,
    anomaly: dict,
    context: dict,
) -> dict:
    """File a diagnostic GitHub issue for a persistent problem.

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
        logger.warning("gh issue create timed out")
        return {"issue_number": None, "filed": False, "template": body}

    except Exception as exc:
        logger.warning("Failed to file diagnostic issue: %s", exc)
        return {"issue_number": None, "filed": False, "template": body}

"""Canonical issue-body template for overseer-filed issues (issue #1962).

Single source of truth for the markdown template the overseer renders
when filing a diagnostic GitHub issue. Both the dead orchestrator-side
``orchestrator/overseer/issue_filer.py`` (preserved as the historical
literal source) and the live sandbox-side
``sandbox/egg_lib/overseer_issue_body.py`` import ``TEMPLATE_LITERAL``
from this module so a single edit covers both call sites — no parallel
copies that can drift.

The template literal is byte-for-byte identical to the body-format
that ``_build_issue_body`` returned at
``orchestrator/overseer/issue_filer.py:86-107`` before #1962, extended
with a "Pipeline Links" sub-block per ``decision-8`` opt-2.
"""

from __future__ import annotations

# The canonical issue-body template. Field placeholders use named
# ``{}`` substitutions so callers pass keyword arguments to ``render``.
#
# IMPORTANT: This literal is the byte-for-byte canonical source. The
# orchestrator-side test in ``test_overseer_issue_filer.py`` asserts
# the literal at ``orchestrator/overseer/issue_filer.py:86-107`` matches
# this constant character-for-character so a future drift fails CI
# loudly instead of silently shipping two copies of the template.
TEMPLATE_LITERAL: str = """## Pipeline Diagnostic: {anomaly_type}

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

### Pipeline Links
- **Pipeline ID**: `{pipeline_id}` ([branch on GitHub]({branch_url}))
- **Phase**: `{phase}`
- **Branch**: `{branch}`
- **Commit SHA at filing**: `{commit_sha}`
- **Parent OVERSEER_ALERT message**: `{parent_alert_message_id}`
"""


def render(**fields: object) -> str:
    """Substitute the named field placeholders into the template.

    Args:
        **fields: Keyword arguments matching the placeholder names in
            ``TEMPLATE_LITERAL``. Required:
            ``anomaly_type, pipeline_id, phase, agent_role, timestamp,
            anomaly_description, timeline_lines, classification_lines,
            actions_lines, container_logs_section, remediation,
            branch_url, branch, commit_sha, parent_alert_message_id``.

    Returns:
        The rendered body string.
    """
    return TEMPLATE_LITERAL.format(**fields)


__all__ = ["TEMPLATE_LITERAL", "render"]

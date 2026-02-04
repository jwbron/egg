"""
Workflow signatures for GitHub operations.

This module provides utilities to automatically add workflow context
signatures to PRs, comments, and Slack messages so users can identify
which job/workflow generated each output.
"""

from egg_logging.context import get_current_context


def get_workflow_signature(include_trace_id: bool = False) -> str:
    """Get workflow signature from current logging context.

    Args:
        include_trace_id: Whether to include the trace ID in the signature

    Returns:
        Markdown signature with workflow context, or empty string if no context

    Example:
        "_(Workflow: check_failure | ID: `gw-check_failure-20251130-102305-a1b2c3d4`)_"
    """
    ctx = get_current_context()
    if not ctx:
        return ""

    parts = []

    if ctx.workflow_type:
        # Make workflow type more human-readable
        workflow_display = ctx.workflow_type.replace("_", " ").title()
        parts.append(f"Workflow: {workflow_display}")

    if ctx.workflow_id:
        parts.append(f"ID: `{ctx.workflow_id}`")

    if include_trace_id and ctx.trace_id:
        parts.append(f"Trace: `{ctx.trace_id[:8]}...`")

    if not parts:
        return ""

    return f"_({' | '.join(parts)})_"


def add_signature_to_pr_body(body: str, include_trace_id: bool = False) -> str:
    """Add workflow signature to PR description.

    Args:
        body: Original PR body
        include_trace_id: Whether to include trace ID

    Returns:
        PR body with signature appended
    """
    signature = get_workflow_signature(include_trace_id)
    if not signature:
        return body

    # Add signature at the end, separated by a horizontal rule
    return f"{body}\n\n---\n\n{signature}"


def add_signature_to_comment(comment: str, include_trace_id: bool = False) -> str:
    """Add workflow signature to GitHub comment.

    Args:
        comment: Original comment text
        include_trace_id: Whether to include trace ID

    Returns:
        Comment with signature appended
    """
    signature = get_workflow_signature(include_trace_id)
    if not signature:
        return comment

    # Add signature at the end with some spacing
    return f"{comment}\n\n{signature}"


def get_workflow_context_dict() -> dict:
    """Get workflow context as a dictionary for structured outputs.

    Returns:
        Dict with workflow_id, workflow_type, and trace_id (if available)
    """
    ctx = get_current_context()
    if not ctx:
        return {}

    result = {}
    if ctx.workflow_id:
        result["workflow_id"] = ctx.workflow_id
    if ctx.workflow_type:
        result["workflow_type"] = ctx.workflow_type
    if ctx.trace_id:
        result["trace_id"] = ctx.trace_id

    return result

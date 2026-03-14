"""
Webhook interface for external triggers.

Provides webhook endpoints for:
- GitHub webhooks (issue events, PR events)
- External system triggers
- Custom webhook handlers
"""

import hashlib
import hmac
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Add shared directory to path
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


logger = get_logger("orchestrator.webhooks")

webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/api/v1/webhooks")

# Webhook handler type
WebhookHandler = Callable[[str, dict[str, Any]], dict[str, Any] | None]

# Registered webhook handlers
_webhook_handlers: dict[str, list[WebhookHandler]] = {}


def register_webhook_handler(event_type: str, handler: WebhookHandler) -> None:
    """Register a handler for a webhook event type.

    Args:
        event_type: GitHub event type (e.g., "issues", "pull_request")
        handler: Handler function
    """
    if event_type not in _webhook_handlers:
        _webhook_handlers[event_type] = []
    _webhook_handlers[event_type].append(handler)


def verify_github_signature(
    payload: bytes,
    signature: str | None,
    secret: str | None,
) -> bool:
    """Verify GitHub webhook signature.

    Args:
        payload: Raw request body
        signature: X-Hub-Signature-256 header value
        secret: Webhook secret

    Returns:
        True if signature is valid
    """
    if not signature or not secret:
        return False

    # GitHub sends signature as "sha256=<hash>"
    if not signature.startswith("sha256="):
        return False

    expected_sig = signature[7:]  # Remove "sha256=" prefix

    # Compute HMAC
    mac = hmac.new(
        secret.encode(),
        msg=payload,
        digestmod=hashlib.sha256,
    )
    computed_sig = mac.hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)


@webhooks_bp.route("/github", methods=["POST"])
def github_webhook() -> tuple[Response, int]:
    """
    Handle GitHub webhooks.

    Processes GitHub webhook events and triggers appropriate actions.
    Supports issues, pull_request, and check_run events.

    Headers:
        X-GitHub-Event: Event type
        X-GitHub-Delivery: Unique delivery ID
        X-Hub-Signature-256: HMAC signature

    Response:
        {"success": true, "message": "Webhook processed"}
    """
    import os

    # Get headers
    event_type = request.headers.get("X-GitHub-Event")
    delivery_id = request.headers.get("X-GitHub-Delivery")
    signature = request.headers.get("X-Hub-Signature-256")

    if not event_type:
        return jsonify(
            {
                "success": False,
                "message": "Missing X-GitHub-Event header",
            }
        ), 400

    # Verify signature if secret is configured
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if webhook_secret:
        if not verify_github_signature(request.data, signature, webhook_secret):
            logger.warning(
                "Invalid webhook signature",
                event_type=event_type,
                delivery_id=delivery_id,
            )
            return jsonify(
                {
                    "success": False,
                    "message": "Invalid signature",
                }
            ), 401

    # Parse payload
    try:
        payload = request.get_json()
    except Exception as e:
        return jsonify(
            {
                "success": False,
                "message": f"Invalid JSON: {e}",
            }
        ), 400

    logger.info(
        "GitHub webhook received",
        event_type=event_type,
        delivery_id=delivery_id,
        action=payload.get("action"),
    )

    # Handle event
    try:
        result = handle_github_event(event_type, payload)

        return jsonify(
            {
                "success": True,
                "message": "Webhook processed",
                "data": result,
            }
        ), 200

    except Exception as e:
        logger.error(
            "Webhook processing error",
            event_type=event_type,
            error=str(e),
        )
        return jsonify(
            {
                "success": False,
                "message": f"Processing error: {e}",
            }
        ), 500


def handle_github_event(
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Handle a GitHub event.

    Args:
        event_type: GitHub event type
        payload: Event payload

    Returns:
        Result dictionary
    """
    action = payload.get("action", "unknown")
    result: dict[str, Any] = {
        "event_type": event_type,
        "action": action,
        "handled": False,
    }

    # Call registered handlers
    handlers = _webhook_handlers.get(event_type, [])
    for handler in handlers:
        try:
            handler_result = handler(event_type, payload)
            if handler_result:
                result.update(handler_result)
                result["handled"] = True
        except Exception as e:
            logger.error(
                "Webhook handler error",
                event_type=event_type,
                error=str(e),
            )

    # Built-in handlers
    if event_type == "issues":
        result.update(_handle_issue_event(payload))
    elif event_type == "pull_request":
        result.update(_handle_pr_event(payload))
    elif event_type == "issue_comment":
        result.update(_handle_comment_event(payload))

    return result


def _handle_issue_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle issues event."""
    action = payload.get("action")
    issue = payload.get("issue", {})
    issue_number = issue.get("number")
    repo = payload.get("repository", {}).get("full_name")

    result: dict[str, Any] = {
        "issue_number": issue_number,
        "repo": repo,
    }

    if action == "labeled":
        label = payload.get("label", {}).get("name")
        if label == "egg" or label == "egg-pipeline":
            # Trigger pipeline for labeled issue
            result["trigger"] = "pipeline_create"
            result["handled"] = True

            # Import here to avoid circular imports
            from events import EventType, emit_event

            emit_event(
                EventType.PIPELINE_CREATED,
                pipeline_id=f"issue-{issue_number}",
                data={
                    "issue_number": issue_number,
                    "repo": repo,
                    "trigger": "webhook",
                },
            )

    return result


def _handle_pr_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle pull_request event."""
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    repo = payload.get("repository", {}).get("full_name")

    result: dict[str, Any] = {
        "pr_number": pr_number,
        "repo": repo,
    }

    # Check if this is an egg-created PR
    head_ref = pr.get("head", {}).get("ref", "")
    if head_ref.startswith("egg/"):
        if action == "closed" and pr.get("merged"):
            result["trigger"] = "pipeline_complete"
            result["handled"] = True
        elif action == "review_requested":
            result["trigger"] = "review_requested"
            result["handled"] = True

    return result


def _handle_comment_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle issue_comment event."""
    action = payload.get("action")
    comment = payload.get("comment", {})
    issue = payload.get("issue", {})
    issue_number = issue.get("number")

    result: dict[str, Any] = {
        "issue_number": issue_number,
    }

    if action == "created":
        body = comment.get("body", "")

        # Check for HITL decision markers
        if "<!-- egg-hitl-decision" in body:
            # Check for checked checkboxes
            if "- [x]" in body:
                result["trigger"] = "decision_resolved"
                result["handled"] = True

    return result


@webhooks_bp.route("/trigger", methods=["POST"])
def manual_trigger() -> tuple[Response, int]:
    """
    Manual trigger endpoint for testing and external integrations.

    Request body:
        {
            "event": "pipeline.start",
            "issue_number": 123,
            "repo": "owner/repo",
            "data": {...}
        }

    Response:
        {"success": true, "message": "Trigger processed"}
    """
    data = request.get_json()
    if not data:
        return jsonify(
            {
                "success": False,
                "message": "Missing request body",
            }
        ), 400

    event = data.get("event")
    issue_number = data.get("issue_number")
    repo = data.get("repo")

    if not event:
        return jsonify(
            {
                "success": False,
                "message": "Missing event type",
            }
        ), 400

    logger.info(
        "Manual trigger received",
        event=event,
        issue_number=issue_number,
        repo=repo,
    )

    result: dict[str, Any] = {
        "event": event,
        "issue_number": issue_number,
        "handled": False,
    }

    if event == "pipeline.start" and issue_number:
        # Create or start a pipeline
        from events import EventType, emit_event

        emit_event(
            EventType.PIPELINE_STARTED,
            pipeline_id=f"issue-{issue_number}",
            data={
                "issue_number": issue_number,
                "repo": repo,
                "trigger": "manual",
            },
        )
        result["handled"] = True

    return jsonify(
        {
            "success": True,
            "message": "Trigger processed",
            "data": result,
        }
    ), 200


@webhooks_bp.route("/health", methods=["GET"])
def webhook_health() -> tuple[Response, int]:
    """Webhook endpoint health check."""
    return jsonify(
        {
            "status": "healthy",
            "registered_handlers": list(_webhook_handlers.keys()),
        }
    ), 200

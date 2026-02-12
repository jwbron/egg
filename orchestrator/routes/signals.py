"""
Signal endpoints for sandbox callbacks.

Provides REST endpoints for sandboxes to report completion,
progress updates, and errors back to the orchestrator.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Add parent directory to path for imports
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path for logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from dispatch import create_dispatcher
from handoffs import AgentOutput, save_agent_output
from models import AgentRole
from state_store import InvalidPipelineIdError, PipelineNotFoundError, get_state_store

logger = get_logger("orchestrator.signals")

signals_bp = Blueprint("signals", __name__, url_prefix="/api/v1/pipelines")


def make_error_response(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create an error response."""
    response: dict[str, Any] = {"success": False, "message": message}
    if details:
        response["details"] = details
    return jsonify(response), status_code


def make_success_response(
    message: str,
    data: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


from routes import get_repo_path  # noqa: E402 — shared helper


@signals_bp.route("/<pipeline_id>/signal", methods=["POST"])
def handle_signal(pipeline_id: str) -> tuple[Response, int]:
    """
    Handle a signal from a sandbox container.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "signal_type": "complete" | "progress" | "error" | "heartbeat",
            "agent_role": "coder",  // required for agent signals
            "container_id": "abc123...",  // optional
            "data": {...}  // signal-specific data
        }

    Signal types:
        - complete: Agent finished execution
        - progress: Progress update
        - error: Error occurred
        - heartbeat: Keep-alive signal

    Response:
        {
            "success": true,
            "message": "Signal received"
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    signal_type = data.get("signal_type")
    if not signal_type:
        return make_error_response("Missing signal_type")

    repo_path = get_repo_path()

    # Route to appropriate handler
    handlers = {
        "complete": handle_complete_signal,
        "progress": handle_progress_signal,
        "error": handle_error_signal,
        "heartbeat": handle_heartbeat_signal,
    }

    handler = handlers.get(signal_type)
    if not handler:
        return make_error_response(
            f"Unknown signal type: {signal_type}. Valid types: {list(handlers.keys())}"
        )

    return handler(pipeline_id, data, repo_path)


def handle_complete_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle agent completion signal.

    Request body data:
        {
            "agent_role": "coder",
            "commit": "abc1234",  // optional
            "files_changed": ["src/main.py"],  // optional
            "handoff_data": {...},  // optional
            "metrics": {...}  // optional
        }
    """
    agent_role_str = data.get("agent_role")
    if not agent_role_str:
        return make_error_response("Missing agent_role")

    try:
        agent_role = AgentRole(agent_role_str)
    except ValueError:
        return make_error_response(f"Invalid agent_role: {agent_role_str}")

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        # Create dispatcher and record completion
        dispatcher = create_dispatcher(pipeline, repo_path)

        commit = data.get("commit")
        outputs = data.get("handoff_data", {})

        dispatcher.complete_agent(agent_role, commit=commit, outputs=outputs)
        dispatcher.save_contract()

        # Save agent output
        if data.get("handoff_data") or data.get("files_changed"):
            output = AgentOutput(
                role=agent_role,
                commit=commit,
                files_changed=data.get("files_changed", []),
                handoff_data=outputs,
                metrics=data.get("metrics", {}),
            )
            save_agent_output(repo_path, output)

        logger.info(
            "Agent completed",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            commit=commit,
        )

        # Check if all agents are complete
        is_complete = dispatcher.is_complete()

        return make_success_response(
            "Completion recorded",
            data={
                "agent_role": agent_role.value,
                "commit": commit,
                "all_complete": is_complete,
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except Exception as e:
        logger.error(
            "Failed to record completion",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return make_error_response(f"Failed to record completion: {e}", status_code=500)


def handle_progress_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle progress update signal.

    Request body data:
        {
            "agent_role": "coder",
            "progress_percent": 50,
            "current_task": "Writing tests",
            "message": "..."
        }
    """
    agent_role_str = data.get("agent_role")

    logger.info(
        "Progress update",
        pipeline_id=pipeline_id,
        role=agent_role_str,
        progress=data.get("progress_percent"),
        task=data.get("current_task"),
    )

    return make_success_response(
        "Progress recorded",
        data={
            "agent_role": agent_role_str,
            "progress_percent": data.get("progress_percent"),
        },
    )


def handle_error_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle error signal.

    Request body data:
        {
            "agent_role": "coder",
            "error": "Error message",
            "recoverable": false
        }
    """
    agent_role_str = data.get("agent_role")
    if not agent_role_str:
        return make_error_response("Missing agent_role")

    try:
        agent_role = AgentRole(agent_role_str)
    except ValueError:
        return make_error_response(f"Invalid agent_role: {agent_role_str}")

    error_message = data.get("error", "Unknown error")
    recoverable = data.get("recoverable", False)

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        # Mark agent as failed
        dispatcher = create_dispatcher(pipeline, repo_path)
        dispatcher.fail_agent(agent_role, error_message)
        dispatcher.save_contract()

        logger.error(
            "Agent failed",
            pipeline_id=pipeline_id,
            role=agent_role.value,
            error=error_message,
            recoverable=recoverable,
        )

        return make_success_response(
            "Error recorded",
            data={
                "agent_role": agent_role.value,
                "error": error_message,
                "recoverable": recoverable,
            },
        )

    except InvalidPipelineIdError:
        return make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except PipelineNotFoundError:
        return make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )
    except Exception as e:
        logger.error(
            "Failed to record error",
            pipeline_id=pipeline_id,
            error=str(e),
        )
        return make_error_response(f"Failed to record error: {e}", status_code=500)


def handle_heartbeat_signal(
    pipeline_id: str,
    data: dict[str, Any],
    repo_path: Path,
) -> tuple[Response, int]:
    """
    Handle heartbeat signal.

    Request body data:
        {
            "agent_role": "coder",
            "container_id": "abc123..."
        }
    """
    agent_role_str = data.get("agent_role")
    container_id = data.get("container_id")

    logger.debug(
        "Heartbeat",
        pipeline_id=pipeline_id,
        role=agent_role_str,
        container_id=container_id[:12] if container_id else None,
    )

    return make_success_response(
        "Heartbeat acknowledged",
        data={
            "timestamp": datetime.utcnow().isoformat(),
        },
    )


@signals_bp.route("/<pipeline_id>/signal/batch", methods=["POST"])
def handle_batch_signals(pipeline_id: str) -> tuple[Response, int]:
    """
    Handle multiple signals in a batch.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "signals": [
                {"signal_type": "complete", "agent_role": "coder", ...},
                {"signal_type": "complete", "agent_role": "tester", ...}
            ]
        }

    Response:
        {
            "success": true,
            "data": {
                "processed": 2,
                "results": [...]
            }
        }
    """
    data = request.get_json()
    if not data or "signals" not in data:
        return make_error_response("Missing signals array")

    signals = data["signals"]
    if not isinstance(signals, list):
        return make_error_response("signals must be an array")

    repo_path = get_repo_path()
    results = []

    for signal in signals:
        signal_type = signal.get("signal_type", "unknown")

        try:
            # Re-use single signal handling
            handlers = {
                "complete": handle_complete_signal,
                "progress": handle_progress_signal,
                "error": handle_error_signal,
                "heartbeat": handle_heartbeat_signal,
            }

            handler = handlers.get(signal_type)
            if handler:
                response, status = handler(pipeline_id, signal, repo_path)
                results.append(
                    {
                        "signal_type": signal_type,
                        "success": status == 200,
                        "response": response.get_json(),
                    }
                )
            else:
                results.append(
                    {
                        "signal_type": signal_type,
                        "success": False,
                        "error": f"Unknown signal type: {signal_type}",
                    }
                )

        except Exception as e:
            results.append(
                {
                    "signal_type": signal_type,
                    "success": False,
                    "error": str(e),
                }
            )

    return make_success_response(
        f"Processed {len(results)} signal(s)",
        data={
            "processed": len(results),
            "results": results,
        },
    )

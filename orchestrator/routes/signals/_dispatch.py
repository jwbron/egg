"""Signal dispatch + batch endpoint bodies; the @signals_bp.route decorators stay on thin wrappers in the barrel (decision-8) (#3312)."""

import routes.signals as _pkg
from flask import Response, request

from ._responses import make_error_response, make_success_response


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
    if data is None:
        return make_error_response("Missing request body")
    if not isinstance(data, dict):
        return make_error_response("Request body must be a JSON object")

    signal_type = data.get("signal_type")
    if not signal_type:
        return make_error_response("Missing signal_type")

    repo_path = _pkg.get_repo_path()
    # Signal requests don't include a repo field, so get_repo_path() may
    # return the bare parent directory.  Resolve using the pipeline's
    # stored repo field.
    repo_path = _pkg.resolve_repo_path_for_pipeline(pipeline_id, repo_path)

    # Route to appropriate handler
    handlers = {
        "complete": _pkg.handle_complete_signal,
        "progress": _pkg.handle_progress_signal,
        "error": _pkg.handle_error_signal,
        "heartbeat": _pkg.handle_heartbeat_signal,
        "readiness": _pkg.handle_readiness_signal,
        "consensus_propose": _pkg.handle_consensus_propose_signal,
        "consensus_ack": _pkg.handle_consensus_ack_signal,
        "consensus_nack": _pkg.handle_consensus_nack_signal,
        "consensus_withdraw": _pkg.handle_consensus_withdraw_signal,
        "consensus_confirmed": _pkg.handle_consensus_confirmed_signal,
        "consensus_producer_push": _pkg.handle_consensus_producer_push_signal,
        "consensus_excuse_producer": _pkg.handle_consensus_excuse_producer_signal,
        "consensus_resolve_obligation": _pkg.handle_consensus_resolve_obligation_signal,
    }

    handler = handlers.get(signal_type)
    if not handler:
        return make_error_response(
            f"Unknown signal type: {signal_type}. Valid types: {list(handlers.keys())}"
        )

    return handler(pipeline_id, data, repo_path)


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
    if data is None:
        return make_error_response("Missing request body")
    if not isinstance(data, dict):
        return make_error_response("Request body must be a JSON object")
    if "signals" not in data:
        return make_error_response("Missing signals array")

    signals = data["signals"]
    if not isinstance(signals, list):
        return make_error_response("signals must be an array")

    repo_path = _pkg.get_repo_path()
    repo_path = _pkg.resolve_repo_path_for_pipeline(pipeline_id, repo_path)
    results = []

    for signal in signals:
        signal_type = signal.get("signal_type", "unknown")

        try:
            # Re-use single signal handling
            handlers = {
                "complete": _pkg.handle_complete_signal,
                "progress": _pkg.handle_progress_signal,
                "error": _pkg.handle_error_signal,
                "heartbeat": _pkg.handle_heartbeat_signal,
                "readiness": _pkg.handle_readiness_signal,
                "consensus_propose": _pkg.handle_consensus_propose_signal,
                "consensus_ack": _pkg.handle_consensus_ack_signal,
                "consensus_nack": _pkg.handle_consensus_nack_signal,
                "consensus_withdraw": _pkg.handle_consensus_withdraw_signal,
                "consensus_confirmed": _pkg.handle_consensus_confirmed_signal,
                "consensus_producer_push": _pkg.handle_consensus_producer_push_signal,
                "consensus_excuse_producer": _pkg.handle_consensus_excuse_producer_signal,
                "consensus_resolve_obligation": _pkg.handle_consensus_resolve_obligation_signal,
            }

            handler = handlers.get(signal_type)
            if handler:
                response, status = handler(pipeline_id, signal, repo_path)
                results.append(
                    {
                        "signal_type": signal_type,
                        "success": status in (200, 202),
                        "pending": status == 202,
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

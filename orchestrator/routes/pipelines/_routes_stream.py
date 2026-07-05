"""stream-route bodies helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _stream_all_pipelines_body() -> _pkg.Response:
    """
    Stream unified events for all pipelines via Server-Sent Events (SSE).

    Provides real-time updates for ALL pipeline state changes in a single
    SSE connection. Unlike the per-pipeline stream, terminal events for
    individual pipelines do not end the stream.

    Query params:
        ascii: Use ASCII-only characters (default: false)
        active_only: Only include active pipelines (default: true)
        full_dag: Include full DAG visualization (default: false)

    Response:
        text/event-stream with the following event types:
        - snapshot: Initial state of all active pipelines
        - pipeline.*: Pipeline lifecycle events
        - phase.*: Phase transition events
        - agent.*: Agent lifecycle events
        - decision.*: HITL decision events
        - done: Stream is ending (timeout)
    """
    if not _pkg._UNIFIED_SSE_AVAILABLE:
        return _pkg.make_error_response(
            "Unified SSE streaming module not available",
            status_code=500,
        )

    use_ascii = _pkg.request.args.get("ascii", "false").lower() == "true"
    active_only = _pkg.request.args.get("active_only", "true").lower() == "true"
    full_dag = _pkg.request.args.get("full_dag", "false").lower() == "true"

    repo_path = _pkg.get_repo_path()

    return _pkg.Response(
        _pkg.stream_with_context(
            _pkg.create_unified_sse_stream(
                repo_path=repo_path,
                use_ascii=use_ascii,
                active_only=active_only,
                full_dag=full_dag,
            )
        ),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _stream_pipeline_body(pipeline_id: str) -> _pkg.Response:
    """
    Stream pipeline events via Server-Sent Events (SSE).

    Provides real-time updates for pipeline state changes including
    phase transitions, agent lifecycle, and DAG visualization.

    URL params:
        pipeline_id: Pipeline ID

    Query params:
        ascii: Use ASCII-only characters (default: false)

    Response:
        text/event-stream with the following event types:
        - snapshot: Initial pipeline state
        - pipeline.*: Pipeline lifecycle events
        - phase.*: Phase transition events
        - agent.*: Agent lifecycle events
        - decision.*: HITL decision events
        - done: Stream is ending (terminal state or timeout)
        - error: An error occurred

    The stream automatically closes when the pipeline reaches a
    terminal state (completed, failed, cancelled) or after the
    maximum connection time (1 hour).
    """
    if not _pkg._SSE_AVAILABLE:
        return _pkg.make_error_response(
            "SSE streaming module not available",
            status_code=500,
        )

    use_ascii = _pkg.request.args.get("ascii", "false").lower() == "true"

    # Validate pipeline exists before starting stream
    repo_path = _pkg.get_repo_path()
    try:
        _pkg._resolve_pipeline(pipeline_id, repo_path)
    except _pkg.InvalidPipelineIdError:
        return _pkg.make_error_response(
            f"Invalid pipeline ID format: {pipeline_id}",
            status_code=400,
        )
    except _pkg.PipelineNotFoundError:
        return _pkg.make_error_response(
            f"Pipeline {pipeline_id} not found",
            status_code=404,
        )

    return _pkg.Response(
        _pkg.stream_with_context(
            _pkg.create_sse_stream(pipeline_id, repo_path=repo_path, use_ascii=use_ascii)
        ),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

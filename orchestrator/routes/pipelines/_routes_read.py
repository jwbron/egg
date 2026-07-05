"""read-route bodies helpers for routes/pipelines (#3312 slice-4).

Extracted verbatim from the pipelines barrel; barrel-resident and
test-patched globals are reached via ``_pkg`` so
``patch("routes.pipelines.<name>")`` keeps intercepting.
"""

from __future__ import annotations

import routes.pipelines as _pkg  # noqa: E402,F401


def _list_pipelines_body() -> tuple[_pkg.Response, int]:
    """
    List all pipelines.

    Query params:
        repo_path: Path to repository (optional)
        active_only: Only return active pipelines (default: false)

    Response:
        {
            "success": true,
            "data": {
                "pipelines": [
                    {"id": "issue-123", "status": "running", ...},
                    ...
                ]
            }
        }
    """
    repo_path = _pkg.get_repo_path()
    active_only = _pkg.request.args.get("active_only", "false").lower() == "true"

    try:
        all_pipelines = _pkg._collect_all_pipelines(repo_path)

        if active_only:
            pipelines = [
                p
                for p in all_pipelines
                if p.status
                not in (
                    _pkg.PipelineStatus.COMPLETE,
                    _pkg.PipelineStatus.FAILED,
                    _pkg.PipelineStatus.CANCELLED,
                )
            ]
        else:
            pipelines = all_pipelines

        # Convert to response format
        pipeline_data = [
            {
                "id": p.id,
                "issue_number": p.issue_number,
                "repo": p.repo,
                "branch": p.branch,
                "status": p.status.value,
                "current_phase": p.current_phase.value,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
            }
            for p in pipelines
        ]

        return _pkg.make_success_response(
            f"Found {len(pipelines)} pipeline(s)",
            data={"pipelines": pipeline_data},
        )

    except _pkg.StateStoreError as e:
        _pkg.logger.error("Failed to list pipelines", error=str(e))
        return _pkg.make_error_response(f"Failed to list pipelines: {e}", status_code=500)


def _get_pipeline_body(pipeline_id: str) -> tuple[_pkg.Response, int]:
    """
    Get a pipeline by ID.

    URL params:
        pipeline_id: Pipeline ID (e.g., "issue-123")

    Query params:
        repo_path: Path to repository (optional)

    Response:
        {
            "success": true,
            "data": {
                "pipeline": {...}
            }
        }
    """
    repo_path = _pkg.get_repo_path()

    try:
        _store, pipeline = _pkg._resolve_pipeline(pipeline_id, repo_path)

        return _pkg.make_success_response(
            "Pipeline retrieved",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

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
    except _pkg.StateValidationError as e:
        _pkg.logger.error("Pipeline validation failed", pipeline_id=pipeline_id, error=str(e))
        return _pkg.make_error_response(
            f"Pipeline state is invalid: {e}",
            status_code=500,
        )

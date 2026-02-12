"""
Pipeline CRUD endpoints for egg-orchestrator.
"""

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


# Import orchestrator modules - try relative import first
try:
    from ..container_spawner import ContainerSpawnError, get_container_spawner
    from ..decision_queue import DecisionTimeoutError, get_decision_queue
    from ..models import AgentRole, PipelineStatus, ReviewVerdict
    from ..state_store import (
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStoreError,
        StateValidationError,
        get_state_store,
    )
except ImportError:
    from container_spawner import ContainerSpawnError, get_container_spawner  # type: ignore
    from decision_queue import DecisionTimeoutError, get_decision_queue  # type: ignore
    from models import AgentRole, PipelineStatus, ReviewVerdict  # type: ignore
    from state_store import (  # type: ignore
        InvalidPipelineIdError,
        PipelineNotFoundError,
        StateStoreError,
        StateValidationError,
        get_state_store,
    )

logger = get_logger("orchestrator.pipelines")

pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/api/v1/pipelines")


from routes import get_repo_path  # noqa: E402 — shared helper


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


@pipelines_bp.route("", methods=["GET"])
def list_pipelines() -> tuple[Response, int]:
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
    repo_path = get_repo_path()
    active_only = request.args.get("active_only", "false").lower() == "true"

    try:
        store = get_state_store(repo_path)

        if active_only:
            pipelines = store.get_active_pipelines()
        else:
            pipeline_ids = store.list_pipelines()
            pipelines = []
            for pid in pipeline_ids:
                try:
                    pipelines.append(store.load_pipeline(pid))
                except StateStoreError:
                    continue

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

        return make_success_response(
            f"Found {len(pipelines)} pipeline(s)",
            data={"pipelines": pipeline_data},
        )

    except StateStoreError as e:
        logger.error("Failed to list pipelines", error=str(e))
        return make_error_response(f"Failed to list pipelines: {e}", status_code=500)


@pipelines_bp.route("/<pipeline_id>", methods=["GET"])
def get_pipeline(pipeline_id: str) -> tuple[Response, int]:
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
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        return make_success_response(
            "Pipeline retrieved",
            data={"pipeline": pipeline.model_dump(mode="json")},
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
    except StateValidationError as e:
        logger.error("Pipeline validation failed", pipeline_id=pipeline_id, error=str(e))
        return make_error_response(
            f"Pipeline state is invalid: {e}",
            status_code=500,
        )


@pipelines_bp.route("", methods=["POST"])
def create_pipeline() -> tuple[Response, int]:
    """
    Create a new pipeline.

    Request body:
        {
            "issue_number": 123,
            "repo": "owner/name",
            "branch": "egg/issue-123",
            "config": {...}  // optional
        }

    Response:
        {
            "success": true,
            "message": "Pipeline created",
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    mode = data.get("mode", "issue")

    if mode == "local":
        # Local mode: prompt required, issue_number/repo/branch optional
        prompt = data.get("prompt")
        if not prompt:
            return make_error_response("Missing prompt (required for local mode)")

        # Local pipelines always use the base EGG_REPO_PATH — not a repo-specific
        # subdirectory — so that list/get/start resolve to the same path.
        repo_path = Path(os.environ.get("EGG_REPO_PATH", "."))
        if not repo_path.is_absolute():
            repo_path = Path.cwd() / repo_path

        try:
            store = get_state_store(repo_path)
            pipeline = store.create_pipeline(
                issue_number=data.get("issue_number"),
                repo=data.get("repo"),
                branch=data.get("branch"),
                config=data.get("config"),
                mode="local",
                prompt=prompt,
            )

            # Create companion contract for the local pipeline
            try:
                from egg_contracts.loader import create_local_contract

                create_local_contract(
                    pipeline_id=pipeline.id,
                    title=prompt[:100],
                    repo_root=repo_path,
                )
                logger.info(
                    "Local pipeline contract created",
                    pipeline_id=pipeline.id,
                )
            except Exception as contract_err:
                # Contract creation is best-effort — don't block pipeline
                logger.warning(
                    "Failed to create contract for local pipeline",
                    pipeline_id=pipeline.id,
                    error=str(contract_err),
                )

            logger.info(
                "Local pipeline created",
                pipeline_id=pipeline.id,
                prompt=prompt[:100],
            )

            return make_success_response(
                "Pipeline created",
                data={"pipeline": pipeline.model_dump(mode="json")},
            )

        except StateStoreError as e:
            if "already exists" in str(e):
                return make_error_response(str(e), status_code=409)
            logger.error("Failed to create local pipeline", error=str(e))
            return make_error_response(f"Failed to create pipeline: {e}", status_code=500)

    # Issue mode: existing behavior
    issue_number = data.get("issue_number")
    repo = data.get("repo")
    branch = data.get("branch")

    if not issue_number:
        return make_error_response("Missing issue_number")
    if not repo:
        return make_error_response("Missing repo")
    if not branch:
        return make_error_response("Missing branch")

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.create_pipeline(
            issue_number=issue_number,
            repo=repo,
            branch=branch,
            config=data.get("config"),
            mode="issue",
        )

        logger.info(
            "Pipeline created",
            pipeline_id=pipeline.id,
            issue_number=issue_number,
        )

        return make_success_response(
            "Pipeline created",
            data={"pipeline": pipeline.model_dump(mode="json")},
        )

    except StateStoreError as e:
        if "already exists" in str(e):
            return make_error_response(str(e), status_code=409)
        logger.error("Failed to create pipeline", error=str(e))
        return make_error_response(f"Failed to create pipeline: {e}", status_code=500)


@pipelines_bp.route("/<pipeline_id>", methods=["PATCH"])
def update_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Update a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Request body:
        {
            "status": "running",
            "current_phase": "plan",
            ...
        }

    Response:
        {
            "success": true,
            "data": {
                "pipeline": {...}
            }
        }
    """
    data = request.get_json()
    if not data:
        return make_error_response("Missing request body")

    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.update_pipeline(pipeline_id, data)

        logger.info("Pipeline updated", pipeline_id=pipeline_id)

        return make_success_response(
            "Pipeline updated",
            data={"pipeline": pipeline.model_dump(mode="json")},
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
    except StateValidationError as e:
        return make_error_response(
            f"Invalid update: {e}",
            status_code=400,
        )


@pipelines_bp.route("/<pipeline_id>", methods=["DELETE"])
def delete_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Delete a pipeline.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline deleted"
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        store.delete_pipeline(pipeline_id)

        logger.info("Pipeline deleted", pipeline_id=pipeline_id)

        return make_success_response("Pipeline deleted")

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


@pipelines_bp.route("/<pipeline_id>/status", methods=["GET"])
def get_pipeline_status(pipeline_id: str) -> tuple[Response, int]:
    """
    Get pipeline status summary.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "data": {
                "id": "issue-123",
                "status": "running",
                "current_phase": "implement",
                "pending_decisions": 0
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        pending = pipeline.get_pending_decisions()

        data = {
            "id": pipeline.id,
            "status": pipeline.status.value,
            "current_phase": pipeline.current_phase.value,
            "pending_decisions": len(pending),
            "updated_at": pipeline.updated_at.isoformat(),
        }

        # Include first pending decision details so the collaborator
        # doesn't need a second round-trip to fetch it
        if pending:
            d = pending[0]
            data["pending_decision"] = {
                "id": d.id,
                "question": d.question,
                "context": d.context,
                "options": d.options,
                "created_at": d.created_at.isoformat(),
            }

        return make_success_response("Status retrieved", data=data)

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


def _get_unified_criteria(phase: str) -> str:
    """Return unified review criteria ported from build-unified-review-prompt.sh."""
    if phase == "refine":
        return (
            "### 1. Problem Understanding\n"
            "- Does the analysis correctly identify the core problem or feature request?\n"
            "- Is the current behavior (if applicable) accurately described?\n"
            "- Are the goals and desired outcomes clear?\n\n"
            "### 2. Research Quality\n"
            "- Has the agent explored the relevant parts of the codebase?\n"
            "- Are existing patterns and conventions identified?\n"
            "- Is the technical context accurate?\n\n"
            "### 3. Options Analysis\n"
            "- Are the options meaningfully different?\n"
            "- Are trade-offs clearly articulated for each option?\n"
            "- Is the reasoning logical and well-founded?\n\n"
            "### 4. Constraints and Dependencies\n"
            "- Are technical constraints identified (performance, compatibility, etc.)?\n"
            "- Are dependencies on other code or systems noted?\n"
            "- Are potential risks or complications surfaced?\n\n"
            "### 5. Open Questions\n"
            "- Are open questions specific enough for a human to answer?\n"
            "- Do questions address genuine ambiguities?\n"
            "- Are questions actionable?\n\n"
            "### 6. Recommendation Quality\n"
            "- Is there a clear recommended approach?\n"
            "- Is the recommendation justified with specific reasons?\n"
            "- Does the recommendation align with the analysis findings?\n"
        )
    elif phase == "plan":
        return (
            "### 1. Alignment with Analysis\n"
            "- Does the plan implement the recommended approach from the analysis?\n"
            "- Are all requirements addressed?\n"
            "- If the plan deviates from the analysis, is the reason explained?\n\n"
            "### 2. Task Breakdown\n"
            "- Are tasks specific and actionable?\n"
            "- Are tasks appropriately sized (not too large, not too granular)?\n"
            "- Is the order of tasks logical?\n\n"
            "### 3. Acceptance Criteria\n"
            "- Does each task have clear, verifiable acceptance criteria?\n"
            "- Are acceptance criteria specific (not vague)?\n"
            "- Can the criteria be objectively verified?\n\n"
            "### 4. Dependencies\n"
            "- Are dependencies between tasks identified?\n"
            "- Is the phase structure logical?\n"
            "- Are external dependencies (libraries, APIs, etc.) noted?\n\n"
            "### 5. Test Strategy\n"
            "- Is there a test strategy section?\n"
            "- Does it cover unit tests, integration tests as appropriate?\n"
            "- Are edge cases and error scenarios considered?\n\n"
            "### 6. Risk Assessment\n"
            "- Are potential risks identified?\n"
            "- Is there a rollback plan or mitigation strategy?\n"
            "- Are technical challenges acknowledged?\n"
        )
    else:
        return (
            "### 1. Task Completion\n"
            "- Are all tasks from the plan implemented?\n"
            "- Does each implementation match its acceptance criteria?\n"
            "- Are all files listed in the plan modified or created?\n\n"
            "### 2. Code Quality\n"
            "- Does the code follow existing patterns in the codebase?\n"
            "- Is the code readable and maintainable?\n\n"
            "### 3. Security\n"
            "- Are there any injection vulnerabilities (SQL, command, XSS)?\n"
            "- Is input validation present at trust boundaries?\n"
            "- Are credentials properly handled (not hardcoded)?\n\n"
            "### 4. Error Handling\n"
            "- Are errors handled gracefully?\n"
            "- Are failure paths considered?\n\n"
            "### 5. Testing\n"
            "- Are there tests for new functionality?\n"
            "- Do existing tests still pass?\n"
            "- Are edge cases covered?\n\n"
            "### 6. Documentation\n"
            "- Are significant changes documented?\n"
        )


def _get_agent_design_criteria() -> str:
    """Return agent-mode design review criteria from build-agent-mode-design-review-prompt-workloop.sh."""
    return (
        "Flag these **clear** anti-patterns:\n\n"
        "1. **Excessive pre-fetching** — Baking large diffs (10KB+) or full file contents "
        "into prompts instead of letting the agent fetch what it needs\n"
        "2. **Structured output for humans** — Requiring JSON when output goes directly "
        "to humans rather than machines\n"
        "3. **Post-processing pipelines** — Scripts that parse agent output to take actions "
        "the agent could take directly\n"
        "4. **Rigid procedures** — Micromanaging step-by-step procedures when objectives "
        "would suffice\n"
        "5. **Prompt-level security** — Using instructions for constraints that should be "
        "sandbox-enforced\n"
    )


def _get_code_review_criteria() -> str:
    """Return code review criteria from build-code-review-prompt-workloop.sh."""
    return (
        "### Security (highest priority)\n"
        "- Injection vulnerabilities (SQL, command, XSS, LDAP, path traversal)\n"
        "- Authentication/authorization flaws\n"
        "- Credential exposure, hardcoded secrets\n"
        "- SSRF, open redirects, unsafe deserialization\n\n"
        "### Correctness\n"
        "- Logic errors, off-by-one, boundary conditions\n"
        "- Race conditions, deadlocks, concurrency bugs\n"
        "- Null/undefined handling, missing error paths\n"
        "- Resource leaks (connections, file handles, memory)\n\n"
        "### Robustness\n"
        "- Missing input validation at trust boundaries\n"
        "- Unhandled exceptions that could crash the system\n"
        "- Missing retry logic for transient failures\n"
        "- Inadequate timeouts for external calls\n\n"
        "### Design\n"
        "- Violations of existing codebase patterns\n"
        "- Breaking changes to public interfaces\n"
        "- Tight coupling that will hinder future changes\n"
    )


def _get_contract_review_criteria() -> str:
    """Return contract verification criteria from build-contract-verification-prompt-workloop.sh."""
    return (
        "### Task Verification\n"
        "For each task in the contract, verify:\n"
        "1. The described functionality is present in the code\n"
        "2. The acceptance criteria for the task is satisfied\n"
        "3. If a commit is linked, verify it relates to the task\n"
        "4. Where applicable, tests cover the new functionality\n\n"
        "### Phase Consistency\n"
        "- All tasks in completed phases are actually implemented\n"
        "- Phase status matches task completion state\n"
        "- No orphaned code exists that isn't covered by any task\n\n"
        "### Acceptance Criteria Verification\n"
        "For each acceptance criterion:\n"
        "1. Examine the implementation to verify it meets the criterion\n"
        "2. Note any gaps in your review\n\n"
        "### Contract Integrity\n"
        "- No implementation changes violate previously verified criteria\n"
        "- New changes don't break existing contract compliance\n"
        "- All required files listed in tasks are present\n"
    )


# Per-phase reviewer matrix matching GHA sdlc-work-loop.yml
_PHASE_REVIEWERS: dict[str, list[str]] = {
    "refine": ["unified", "agent-design"],
    "plan": ["unified", "agent-design"],
    "implement": ["unified", "agent-design", "code", "contract"],
}


def _get_review_criteria_for_type(reviewer_type: str, phase: str) -> str:
    """Dispatch to the correct criteria function based on reviewer type."""
    if reviewer_type == "unified":
        return _get_unified_criteria(phase)
    elif reviewer_type == "agent-design":
        return _get_agent_design_criteria()
    elif reviewer_type == "code":
        return _get_code_review_criteria()
    elif reviewer_type == "contract":
        return _get_contract_review_criteria()
    else:
        return _get_unified_criteria(phase)


def _get_reviewer_scope_preamble(reviewer_type: str, phase: str) -> str:
    """Return a scope preamble that tells the reviewer what to focus on."""
    if reviewer_type == "unified":
        return f"This is a **unified review** of the {phase} phase output."
    elif reviewer_type == "agent-design":
        return (
            "This is a specialized **agent-mode design review**. Focus ONLY on "
            "agent-mode design principles. Do NOT review general code quality, "
            "security, or correctness — other reviewers handle those.\n\n"
            "**Only flag issues if you find clear agent-mode design anti-patterns.** "
            "If the output has no agent-mode concerns, approve it."
        )
    elif reviewer_type == "code":
        return (
            "This is a **comprehensive code review**. Focus on security, correctness, "
            "and robustness. Agent-mode design alignment is handled by another reviewer."
        )
    elif reviewer_type == "contract":
        return (
            "This is a **contract verification review**. Verify that the implementation "
            "matches the contract and all acceptance criteria are met. Do NOT review "
            "general code quality or security — other reviewers handle those."
        )
    return ""


def _verdict_path_for_type(
    phase: str,
    reviewer_type: str,
    pipeline_mode: str,
    issue_number: int | None = None,
) -> str:
    """Return the relative verdict file path for a given reviewer type."""
    if pipeline_mode == "local":
        return f".egg-state/reviews/{phase}-{reviewer_type}-review.json"
    else:
        return f".egg-state/reviews/{issue_number}-{phase}-{reviewer_type}-review.json"


def _get_draft_path(phase: str, pipeline_mode: str, issue_number: int | None = None) -> str | None:
    """Return relative path to the draft file for a phase."""
    is_local = pipeline_mode == "local"
    if is_local:
        if phase == "refine":
            return ".egg-state/drafts/analysis.md"
        elif phase == "implement":
            return None
        else:
            return f".egg-state/drafts/{phase}.md"
    else:
        if phase == "refine":
            return f".egg-state/drafts/{issue_number}-analysis.md"
        elif phase == "implement":
            return None
        else:
            return f".egg-state/drafts/{issue_number}-{phase}.md"


def _read_phase_draft(
    repo_path: Path,
    phase: str,
    pipeline_mode: str,
    issue_number: int | None = None,
    max_chars: int = 8000,
) -> str:
    """Read draft file contents. Truncates at max_chars."""
    draft_rel = _get_draft_path(phase, pipeline_mode, issue_number)
    if not draft_rel:
        return f"(No draft file for {phase} phase)"
    draft_path = repo_path / draft_rel
    if not draft_path.exists():
        return f"(Draft file not found: {draft_rel})"
    content = draft_path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        return content[:max_chars] + f"\n\n... (truncated, {len(content)} chars total)"
    return content


def _build_review_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    reviewer_type: str = "unified",
    issue_number: int | None = None,
    review_cycle: int = 1,
    prior_feedback: str | None = None,
) -> str:
    """Build a review prompt for the reviewer agent.

    Tells the reviewer to evaluate the draft for the given phase and write
    a typed verdict JSON file to .egg-state/reviews/.
    """
    draft_path = _get_draft_path(phase, pipeline_mode, issue_number)

    verdict_path = _verdict_path_for_type(phase, reviewer_type, pipeline_mode, issue_number)

    lines = [
        f"You are reviewing the **{phase}** phase output of the SDLC pipeline "
        f"({reviewer_type} reviewer).\n",
        "## Scope\n",
        _get_reviewer_scope_preamble(reviewer_type, phase),
        "",
        "## Context\n",
        f"Pipeline ID: {pipeline_id}",
        f"Phase: {phase}",
        f"Reviewer: {reviewer_type}",
        f"Review cycle: {review_cycle}",
        "",
        "## Your Task\n",
    ]

    if draft_path:
        lines.append(f"1. Read the draft at `{draft_path}`")
    else:
        lines.append(
            "1. Review the implementation using `git log --oneline -10` "
            "and `git diff HEAD~10..HEAD`"
        )
    lines.append("2. Evaluate it against the criteria below")
    lines.append(f"3. Write your verdict to `{verdict_path}` as JSON")
    lines.append("4. Commit the verdict file")
    lines.append("")

    # Review criteria
    lines.append("## Review Criteria\n")
    lines.append(_get_review_criteria_for_type(reviewer_type, phase))
    lines.append("")

    # Prior feedback for re-reviews
    if review_cycle > 1 and prior_feedback:
        lines.append("## Prior Review Feedback\n")
        lines.append(
            "This is a re-review. The previous review found issues. "
            "Verify that the following feedback was addressed:\n"
        )
        lines.append(prior_feedback)
        lines.append("")

    # Verdict format
    lines.append("## Verdict Format\n")
    lines.append(f"Write the following JSON to `{verdict_path}`:\n")
    lines.append("```json")
    lines.append("{")
    lines.append(f'  "reviewer": "{reviewer_type}",')
    lines.append('  "verdict": "approved" or "needs_revision",')
    lines.append('  "summary": "Brief summary of findings",')
    lines.append('  "feedback": "Detailed feedback if needs_revision, empty if approved",')
    lines.append('  "timestamp": "ISO 8601 timestamp"')
    lines.append("}")
    lines.append("```\n")
    lines.append(
        "If the work meets all criteria, set verdict to `approved`. "
        "If significant issues remain, set verdict to `needs_revision` "
        "and provide actionable feedback."
    )

    return "\n".join(lines)


def _read_review_verdict(
    repo_path: Path,
    phase: str,
    reviewer_type: str = "unified",
    pipeline_mode: str = "local",
    issue_number: int | None = None,
) -> ReviewVerdict | None:
    """Read a typed review verdict JSON from the repo.

    Returns None if the file is missing or malformed (treated as approved
    for graceful degradation).
    """
    verdict_rel = _verdict_path_for_type(phase, reviewer_type, pipeline_mode, issue_number)
    verdict_file = repo_path / verdict_rel

    if not verdict_file.exists():
        logger.warning(
            "Verdict file not found, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
        )
        return None

    try:
        raw = verdict_file.read_text()
        data = json.loads(raw)
        return ReviewVerdict(**data)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(
            "Failed to parse verdict file, treating as approved",
            path=str(verdict_file),
            reviewer_type=reviewer_type,
            error=str(e),
        )
        return None


def _aggregate_review_verdicts(
    verdicts: dict[str, ReviewVerdict | None],
) -> tuple[str, str]:
    """Aggregate multiple typed review verdicts into an overall result.

    Returns:
        (overall_verdict, combined_feedback) where overall_verdict is
        "approved" or "needs_revision". Any needs_revision → overall
        needs_revision. Missing/None verdicts are treated as approved.
    """
    overall = "approved"
    feedback_sections: list[str] = []

    for reviewer_type, verdict in verdicts.items():
        if verdict is None:
            continue
        if verdict.verdict == "needs_revision":
            overall = "needs_revision"
            section = f"### {reviewer_type} reviewer\n"
            if verdict.feedback:
                section += verdict.feedback
            elif verdict.summary:
                section += verdict.summary
            feedback_sections.append(section)

    combined = "\n\n".join(feedback_sections) if feedback_sections else ""
    return overall, combined


def _build_phase_prompt(
    phase: str,
    pipeline_id: str,
    pipeline_mode: str,
    prompt: str | None = None,
    issue_number: int | None = None,
    repo: str | None = None,
    branch: str | None = None,
    review_feedback: str | None = None,
    review_cycle: int = 0,
) -> str:
    """Build a phase-specific prompt for the sandbox Claude invocation.

    Follows the same structure as action/build-sdlc-prompt.sh:
    Context → Task → Restrictions → Completion.  Adapted for the
    orchestrator (local mode has no GitHub issue, contract, or PR).
    """
    is_local = pipeline_mode == "local"

    # --- Context header ---
    lines = [f"You are in the **{phase}** phase of the SDLC pipeline.\n"]
    lines.append("## Context\n")
    lines.append(f"Pipeline ID: {pipeline_id}")
    lines.append(f"Phase: {phase}")
    lines.append(f"Mode: {pipeline_mode}")
    if repo:
        lines.append(f"Repository: {repo}")
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_number is not None:
        lines.append(f"Issue: #{issue_number}")
    lines.append("")

    # --- Prior review feedback (revision cycles) ---
    if review_cycle > 0 and review_feedback:
        lines.append(f"## Prior Review Feedback (Cycle {review_cycle})\n")
        lines.append(
            "The reviewer found issues with your previous draft. "
            "Address the feedback below and revise your draft **in-place** "
            "(overwrite the same file).\n"
        )
        lines.append(review_feedback)
        lines.append("")

    # --- Task description ---
    if prompt:
        lines.append("## Task Description\n")
        lines.append(prompt)
        lines.append("")

    # --- Phase-specific instructions ---
    lines.append("## Your Task\n")

    if phase == "refine":
        lines.extend(
            [
                "Analyze the task and produce a structured analysis:",
                "",
                "1. Understand the problem or feature request",
                "2. Research the current codebase to understand existing patterns",
                "3. Identify constraints and dependencies",
                "4. Consider multiple implementation approaches",
                "5. Recommend an approach with justification",
                "",
            ]
        )
        if is_local:
            lines.extend(
                [
                    "Write your analysis to `.egg-state/drafts/analysis.md`.",
                    "Commit the draft when done.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"Write your analysis to `.egg-state/drafts/{issue_number}-analysis.md`.",
                    "Commit and push the draft when done.",
                    "",
                ]
            )

    elif phase == "plan":
        lines.extend(
            [
                "Create a detailed implementation plan:",
                "",
                "1. Review any prior analysis",
                "2. Break down the work into phases with discrete tasks",
                "3. Define clear acceptance criteria for each task",
                "4. Identify test strategy",
                "5. Consider rollback and risks",
                "",
            ]
        )
        if is_local:
            lines.extend(
                [
                    "Write your plan to `.egg-state/drafts/plan.md`.",
                    "Commit the draft when done.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"Write your plan to `.egg-state/drafts/{issue_number}-plan.md`.",
                    "Commit and push the draft when done.",
                    "",
                ]
            )

    elif phase == "implement":
        lines.extend(
            [
                "Implement the changes described in the task and plan:",
                "",
                "1. Review the plan (check `.egg-state/drafts/`)",
                "2. Implement the required changes",
                "3. Run tests to verify correctness",
                "4. Commit with descriptive messages",
                "",
            ]
        )
        if not is_local:
            lines.extend(
                [
                    "Use the contract CLI to track progress:",
                    "- `egg-contract show` — View current contract state",
                    "- `egg-contract add-commit --task <id> --commit <sha>` — Link commit to task",
                    "",
                ]
            )

    elif phase == "pr":
        lines.extend(
            [
                "Create a pull request for this implementation:",
                "",
                "1. Ensure all commits are pushed",
                "2. Create the PR with a descriptive title and body",
                f"3. Reference the issue (#{issue_number}) in the PR description"
                if issue_number
                else "3. Create the PR with a clear summary",
                "4. Wait for human review and approval",
                "",
            ]
        )

    else:
        lines.append(f"Execute the {phase} phase.\n")

    # --- Phase restrictions ---
    lines.append("## Phase Restrictions\n")
    if is_local and phase != "pr":
        lines.extend(
            [
                "This is a **local** pipeline — no GitHub operations in this phase:",
                "- You CANNOT push code (git push)",
                "- You CANNOT create PRs (gh pr create)",
                "- You CANNOT post issue comments",
                "- You CAN read and modify local files",
                "- You CAN run tests",
                "- You CAN commit locally",
                "",
            ]
        )
    elif is_local and phase == "pr":
        lines.extend(
            [
                "This is a **local** pipeline entering the PR phase.",
                "Push access is enabled for this phase only.",
                "- You CAN push code (git push)",
                "- You CAN create and edit PRs (gh pr create, gh pr edit)",
                "- You CANNOT merge PRs (human must merge)",
                "",
            ]
        )
    else:
        if phase in ("refine", "plan"):
            lines.extend(
                [
                    "- You CAN write drafts to `.egg-state/drafts/`",
                    "- You CAN push draft files (git push)",
                    "- You CANNOT create PRs (gh pr create)",
                    "",
                ]
            )
        elif phase == "implement":
            lines.extend(
                [
                    "- You CAN push code (git push)",
                    "- You CAN link commits to tasks (egg-contract add-commit)",
                    "- You CANNOT create PRs (the pipeline manages the PR)",
                    "",
                ]
            )
        elif phase == "pr":
            lines.extend(
                [
                    "- You CAN create and edit PRs (gh pr create, gh pr edit)",
                    "- You CAN push additional commits",
                    "- You CANNOT merge PRs (human must merge)",
                    "",
                ]
            )

    # --- Completion ---
    lines.append("## Phase Completion\n")
    lines.append(
        "When you have completed your work for this phase, "
        "ensure everything is committed and exit successfully."
    )

    return "\n".join(lines)


def _spawn_and_wait(
    spawner,
    pipeline_id: str,
    agent_role: AgentRole,
    issue_number: int | None,
    host_repos_dir: str | None,
    gateway_mode: str,
    repos: list[str],
    phase: str,
    sandbox_env: dict[str, str],
    sandbox_command: list[str],
    timeout: int = 3600,
    store=None,
) -> tuple[int, str]:
    """Spawn a container, wait for it to exit, clean up, return (exit_code, logs).

    If ``store`` is provided, the container is recorded in the phase execution
    state so that the status endpoint can report it while it runs.

    Returns:
        (exit_code, container_logs) — logs are captured before cleanup on failure.
    """
    from models import ContainerInfo, ContainerStatus, PipelinePhase

    spawned = spawner.spawn_agent_container(
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        issue_number=issue_number,
        repo_mount=host_repos_dir,
        mode=gateway_mode,
        wait_for_gateway=False,
        repos=repos,
        phase=phase,
        extra_env=sandbox_env,
        command=sandbox_command,
    )

    # Record container in phase execution state
    if store is not None:
        try:
            pipeline = store.load_pipeline(pipeline_id)
            phase_execution = pipeline.get_phase_execution(PipelinePhase(phase))
            container_info = ContainerInfo(
                container_id=spawned.container_info.container_id,
                container_name=spawned.container_info.container_name,
                status=ContainerStatus.RUNNING,
                started_at=datetime.utcnow(),
                agent_role=agent_role,
            )
            phase_execution.containers.append(container_info)
            store.save_pipeline(pipeline)
        except Exception as track_err:
            logger.warning(
                "Failed to record container in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    docker_client = spawner.docker
    final_info = docker_client.wait_for_container(
        spawned.container_info.container_id,
        timeout=timeout,
    )

    container_logs = ""
    if final_info.exit_code != 0:
        try:
            container_logs = spawner.docker.get_container_logs(
                spawned.container_info.container_id,
                tail=50,
            )
        except Exception:
            pass

    # Update container status in phase execution
    if store is not None:
        try:
            pipeline = store.load_pipeline(pipeline_id)
            phase_execution = pipeline.get_phase_execution(PipelinePhase(phase))
            for ci in phase_execution.containers:
                if ci.container_id == spawned.container_info.container_id:
                    ci.status = ContainerStatus.EXITED
                    ci.exited_at = datetime.utcnow()
                    ci.exit_code = final_info.exit_code
                    break
            store.save_pipeline(pipeline)
        except Exception as track_err:
            logger.warning(
                "Failed to update container status in pipeline state",
                container_id=spawned.container_info.container_id[:12],
                error=str(track_err),
            )

    # Always clean up the container
    try:
        spawner.remove_agent_container(
            spawned.container_info.container_id,
            force=True,
            cleanup_session=True,
        )
    except Exception as cleanup_err:
        logger.warning(
            "Failed to clean up container",
            container_id=spawned.container_info.container_id[:12],
            error=str(cleanup_err),
        )

    return final_info.exit_code, container_logs


# Phases that get an agentic review cycle before advancing
_REVIEWED_PHASES = {"refine", "plan", "implement"}

# Phases that pause for human approval before advancing (HITL gates)
_HITL_GATE_PHASES = {"refine", "plan"}


def _build_checker_prompt(pipeline_id: str, pipeline_mode: str) -> str:
    """Build a prompt for the checker agent that runs tests/lint.

    The checker discovers and runs project test/lint commands, then
    writes structured results to .egg-state/checks/implement-results.json.
    """
    return (
        "You are the **checker** for the SDLC pipeline implement phase.\n\n"
        f"Pipeline ID: {pipeline_id}\n"
        f"Mode: {pipeline_mode}\n\n"
        "## Your Task\n\n"
        "Discover and run all project test and lint commands, then write results.\n\n"
        "1. **Discover commands**: Look for Makefile, pyproject.toml, package.json, "
        "setup.cfg, tox.ini, or similar build/test configuration files\n"
        "2. **Run tests**: Execute the project's test suite (pytest, jest, go test, etc.)\n"
        "3. **Run linting**: Execute linters (ruff, eslint, golangci-lint, etc.)\n"
        "4. **Write results**: Create `.egg-state/checks/implement-results.json` with:\n\n"
        "```json\n"
        "{\n"
        '  "all_passed": true/false,\n'
        '  "checks": [\n'
        '    {"name": "pytest", "passed": true/false, "output": "summary of output"},\n'
        '    {"name": "lint", "passed": true/false, "output": "summary of output"}\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "5. Commit the results file\n\n"
        "## Important\n\n"
        "- Always exit 0 regardless of check results (results are informational)\n"
        "- Write the results file even if all checks pass\n"
        "- If you cannot find any test/lint commands, write all_passed: true\n"
    )


def _build_autofix_prompt(
    pipeline_id: str,
    pipeline_mode: str,
    check_results: dict,
) -> str:
    """Build a prompt for the autofixer agent.

    Modeled on action/build-autofixer-prompt.sh. Tells the agent to read
    check failures, fix auto-fixable issues, and commit fixes.
    """
    failures = []
    for check in check_results.get("checks", []):
        if not check.get("passed", True):
            failures.append(
                f"- **{check.get('name', 'unknown')}**: {check.get('output', 'failed')}"
            )

    failure_summary = "\n".join(failures) if failures else "No specific failures recorded."

    return (
        "You are the **autofixer** for the SDLC pipeline implement phase.\n\n"
        f"Pipeline ID: {pipeline_id}\n"
        f"Mode: {pipeline_mode}\n\n"
        "## Check Failures\n\n"
        f"{failure_summary}\n\n"
        "## Your Task\n\n"
        "**Fix ALL auto-fixable issues in a single pass.**\n\n"
        "1. **Read the check results** at `.egg-state/checks/implement-results.json`\n"
        "2. **Investigate all failures**: Examine test output, lint errors, etc.\n"
        "3. **Fix without committing yet**: For each auto-fixable issue "
        "(lint errors, formatting, simple type errors, obvious test fixes), make the fix\n"
        "4. **Verify locally**: Run the same checks again to confirm fixes work\n"
        "5. **Commit all fixes together** with a descriptive message\n\n"
        "## Auto-fixable vs Report-only\n\n"
        "**Auto-fixable (commit fixes directly):**\n"
        "- Lint errors (formatting, import order, code style)\n"
        "- Type errors with clear fixes\n"
        "- Simple test failures with obvious fixes\n\n"
        "**Report only (note in commit message):**\n"
        "- Complex logic errors requiring design decisions\n"
        "- Security issues requiring architectural changes\n"
        "- Test failures from unclear requirements\n"
    )


def _read_check_results(repo_path: Path) -> dict | None:
    """Read checker output from .egg-state/checks/implement-results.json.

    Returns None if the file is missing or malformed.
    """
    check_file = repo_path / ".egg-state/checks/implement-results.json"
    if not check_file.exists():
        logger.warning("Check results file not found", path=str(check_file))
        return None
    try:
        raw = check_file.read_text()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Failed to parse check results", path=str(check_file), error=str(e))
        return None


def _populate_contract_from_plan(repo_path: Path, pipeline_id: str) -> None:
    """Read the plan draft and populate the contract with tasks.

    Lightweight version of action/populate-contract-tasks.py.
    Reads .egg-state/drafts/plan.md, extracts task structure from
    markdown headers, and writes tasks + acceptance criteria to the contract.
    """
    try:
        from egg_contracts.loader import load_contract, save_contract
    except ImportError:
        logger.warning("egg_contracts not available, skipping contract population")
        return

    plan_path = repo_path / ".egg-state/drafts/plan.md"
    if not plan_path.exists():
        logger.warning("Plan draft not found, skipping contract population", path=str(plan_path))
        return

    try:
        contract = load_contract(pipeline_id, repo_path)
    except Exception:
        logger.warning(
            "Contract not found for pipeline, skipping population", pipeline_id=pipeline_id
        )
        return

    try:
        from egg_contracts.models import Phase as ContractPhase
        from egg_contracts.models import PhaseStatus
        from egg_contracts.models import Task as ContractTask

        plan_text = plan_path.read_text()

        # Extract tasks from markdown — look for ## or ### headers with task-like content
        import re

        tasks: list[ContractTask] = []
        task_idx = 1

        # Look for numbered items or headers that look like tasks
        for match in re.finditer(
            r"^#{2,3}\s+(?:Task\s+)?(\d+[\.\):]?\s*)?(.+)$",
            plan_text,
            re.MULTILINE,
        ):
            title = match.group(2).strip()
            if title and len(title) > 5:  # Skip very short headers
                tasks.append(
                    ContractTask(
                        id=f"task-{task_idx}",
                        description=title,
                    )
                )
                task_idx += 1

        if tasks:
            # Create a single phase containing all tasks
            phase = ContractPhase(
                id="phase-1",
                name="Implementation",
                status=PhaseStatus.PENDING,
                tasks=tasks,
            )
            contract.phases = [phase]
            save_contract(contract, repo_path)
            logger.info(
                "Contract populated from plan",
                pipeline_id=pipeline_id,
                task_count=len(tasks),
            )

    except Exception as e:
        logger.warning(
            "Failed to populate contract from plan",
            pipeline_id=pipeline_id,
            error=str(e),
        )


def _run_pipeline(pipeline_id: str, repo_path: Path) -> None:
    """Run a pipeline by spawning containers for each phase.

    This runs in a background thread. For each phase it:
    1. Spawns a worker (CODER) container
    2. For reviewed phases (refine, plan): spawns a reviewer, reads the
       verdict, and loops back to the worker if revision is needed
    3. Advances to the next phase once approved (or circuit-breaker hit)

    Args:
        pipeline_id: Pipeline ID
        repo_path: Path to repository
    """
    from routes.phases import get_phase_transitions

    try:
        store = get_state_store(repo_path)
        spawner = get_container_spawner()
        pipeline = store.load_pipeline(pipeline_id)
        pipeline_mode = getattr(pipeline, "mode", "issue")
        transitions = get_phase_transitions(pipeline_mode)

        # Map pipeline mode to gateway session mode
        gateway_mode = "local" if pipeline_mode == "local" else "public"

        # Determine host repos path for volume mount.  When the
        # orchestrator runs inside Docker, EGG_REPO_PATH is the
        # *container* path but volume mounts need the *host* path
        # (since the Docker socket operates on the host daemon).
        # EGG_HOST_REPOS_DIR provides that; fall back to EGG_REPO_PATH
        # when running natively (not in Docker).
        host_repos_dir = os.environ.get(
            "EGG_HOST_REPOS_DIR",
            os.environ.get("EGG_REPO_PATH"),
        )

        while True:
            pipeline = store.load_pipeline(pipeline_id)

            if pipeline.status in (PipelineStatus.FAILED, PipelineStatus.CANCELLED):
                logger.info(
                    "Pipeline stopped", pipeline_id=pipeline_id, status=pipeline.status.value
                )
                break

            current_phase = pipeline.current_phase

            # Start the current phase
            phase_execution = pipeline.get_phase_execution(current_phase)
            if phase_execution.status == PipelineStatus.PENDING:
                phase_execution.status = PipelineStatus.RUNNING
                phase_execution.started_at = datetime.utcnow()
                pipeline.status = PipelineStatus.RUNNING
                store.save_pipeline(pipeline)

            # Common sandbox environment for all containers in this phase
            gateway_url = os.environ.get("GATEWAY_URL", "http://172.32.0.2:9848")
            sandbox_env: dict[str, str] = {
                "EGG_PIPELINE_ID": pipeline_id,
                "EGG_PIPELINE_PHASE": current_phase.value,
                "EGG_PIPELINE_MODE": pipeline_mode,
                "GATEWAY_URL": gateway_url,
                "EGG_GATEWAY_URL": gateway_url,
                "RUNTIME_UID": os.environ.get("HOST_UID", "1000"),
                "RUNTIME_GID": os.environ.get("HOST_GID", "1000"),
            }
            if pipeline.prompt:
                sandbox_env["EGG_PIPELINE_PROMPT"] = pipeline.prompt

            repos = [pipeline.repo] if pipeline.repo else []

            # PR phase gets push access even for local pipelines.
            # Override the gateway session mode to "public" so the
            # gateway allows git push and PR creation.
            phase_gateway_mode = gateway_mode
            if current_phase.value == "pr" and pipeline_mode == "local":
                phase_gateway_mode = "public"

            phase_failed = False
            review_feedback: str | None = None

            # --- Inner review cycle ---
            while True:
                # Reload to get latest review_cycles count
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                review_cycle = phase_execution.review_cycles

                # 1. Spawn worker (CODER)
                logger.info(
                    "Spawning worker for phase",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                    review_cycle=review_cycle,
                    mode=gateway_mode,
                )

                phase_prompt = _build_phase_prompt(
                    phase=current_phase.value,
                    pipeline_id=pipeline_id,
                    pipeline_mode=pipeline_mode,
                    prompt=pipeline.prompt,
                    issue_number=pipeline.issue_number,
                    repo=pipeline.repo,
                    branch=pipeline.branch,
                    review_feedback=review_feedback,
                    review_cycle=review_cycle,
                )

                sandbox_command = [
                    "claude",
                    "--dangerously-skip-permissions",
                    "--print",
                    "--verbose",
                    "--output-format",
                    "stream-json",
                    "--model",
                    "opus",
                    "--max-turns",
                    "200",
                    phase_prompt,
                ]

                try:
                    exit_code, container_logs = _spawn_and_wait(
                        spawner=spawner,
                        pipeline_id=pipeline_id,
                        agent_role=AgentRole.CODER,
                        issue_number=pipeline.issue_number,
                        host_repos_dir=host_repos_dir,
                        gateway_mode=phase_gateway_mode,
                        repos=repos,
                        phase=current_phase.value,
                        sandbox_env=sandbox_env,
                        sandbox_command=sandbox_command,
                        store=store,
                    )
                except ContainerSpawnError as e:
                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.FAILED
                    phase_execution.error = str(e)
                    phase_execution.completed_at = datetime.utcnow()
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = str(e)
                    store.save_pipeline(pipeline)
                    logger.error("Failed to spawn container", pipeline_id=pipeline_id, error=str(e))
                    phase_failed = True
                    break

                if exit_code != 0:
                    error_msg = f"Container exited with code {exit_code}"
                    if container_logs:
                        log_lines = container_logs.strip().splitlines()
                        tail = "\n".join(log_lines[-10:])
                        error_msg += f"\n--- container logs (last 10 lines) ---\n{tail}"

                    pipeline = store.load_pipeline(pipeline_id)
                    phase_execution = pipeline.get_phase_execution(current_phase)
                    phase_execution.status = PipelineStatus.FAILED
                    phase_execution.error = error_msg
                    phase_execution.completed_at = datetime.utcnow()
                    pipeline.status = PipelineStatus.FAILED
                    pipeline.error = error_msg
                    store.save_pipeline(pipeline)
                    logger.error(
                        "Phase failed",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        exit_code=exit_code,
                        container_logs=container_logs[-2000:] if container_logs else "",
                    )
                    phase_failed = True
                    break

                # 2. Checker + autofix loop (implement phase only)
                if current_phase.value == "implement":
                    max_autofix = 3
                    for autofix_attempt in range(max_autofix):
                        logger.info(
                            "Spawning checker",
                            pipeline_id=pipeline_id,
                            autofix_attempt=autofix_attempt + 1,
                        )

                        checker_prompt = _build_checker_prompt(pipeline_id, pipeline_mode)
                        checker_command = [
                            "claude",
                            "--dangerously-skip-permissions",
                            "--print",
                            "--verbose",
                            "--output-format",
                            "stream-json",
                            "--model",
                            "opus",
                            "--max-turns",
                            "50",
                            checker_prompt,
                        ]
                        checker_env = {**sandbox_env, "EGG_AGENT_ROLE": "checker"}

                        try:
                            checker_exit, _ = _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=AgentRole.CHECKER,
                                issue_number=pipeline.issue_number,
                                host_repos_dir=host_repos_dir,
                                gateway_mode=phase_gateway_mode,
                                repos=repos,
                                phase=current_phase.value,
                                sandbox_env=checker_env,
                                sandbox_command=checker_command,
                                timeout=1800,
                                store=store,
                            )
                        except ContainerSpawnError as e:
                            logger.warning(
                                "Checker failed to spawn, skipping checks",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            break

                        check_results = _read_check_results(repo_path)
                        if check_results is None or check_results.get("all_passed"):
                            logger.info(
                                "All checks passed",
                                pipeline_id=pipeline_id,
                                attempt=autofix_attempt + 1,
                            )
                            break  # Checks pass — proceed to review

                        # Last attempt — don't autofix, just proceed
                        if autofix_attempt >= max_autofix - 1:
                            logger.warning(
                                "Max autofix attempts reached, proceeding to review",
                                pipeline_id=pipeline_id,
                                attempts=max_autofix,
                            )
                            break

                        # Spawn autofix worker
                        logger.info(
                            "Spawning autofixer",
                            pipeline_id=pipeline_id,
                            autofix_attempt=autofix_attempt + 1,
                        )

                        autofix_prompt = _build_autofix_prompt(
                            pipeline_id, pipeline_mode, check_results
                        )
                        autofix_command = [
                            "claude",
                            "--dangerously-skip-permissions",
                            "--print",
                            "--verbose",
                            "--output-format",
                            "stream-json",
                            "--model",
                            "opus",
                            "--max-turns",
                            "100",
                            autofix_prompt,
                        ]

                        try:
                            _spawn_and_wait(
                                spawner=spawner,
                                pipeline_id=pipeline_id,
                                agent_role=AgentRole.CODER,
                                issue_number=pipeline.issue_number,
                                host_repos_dir=host_repos_dir,
                                gateway_mode=phase_gateway_mode,
                                repos=repos,
                                phase=current_phase.value,
                                sandbox_env=sandbox_env,
                                sandbox_command=autofix_command,
                                store=store,
                            )
                        except ContainerSpawnError as e:
                            logger.warning(
                                "Autofixer failed to spawn, proceeding to review",
                                pipeline_id=pipeline_id,
                                error=str(e),
                            )
                            break

                # 3. Multi-reviewer loop (all reviewed phases)
                if current_phase.value not in _REVIEWED_PHASES:
                    break  # No review needed — advance

                reviewer_types = _PHASE_REVIEWERS.get(current_phase.value, ["unified"])

                # Delete stale verdict files before spawning reviewers
                for rtype in reviewer_types:
                    verdict_rel = _verdict_path_for_type(
                        current_phase.value, rtype, pipeline_mode, pipeline.issue_number
                    )
                    verdict_path = repo_path / verdict_rel
                    if verdict_path.exists():
                        try:
                            verdict_path.unlink()
                        except OSError:
                            pass

                # Run reviewers sequentially
                all_verdicts: dict[str, ReviewVerdict | None] = {}
                for reviewer_type in reviewer_types:
                    logger.info(
                        "Spawning reviewer",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        reviewer_type=reviewer_type,
                        review_cycle=review_cycle + 1,
                    )

                    review_prompt = _build_review_prompt(
                        phase=current_phase.value,
                        pipeline_id=pipeline_id,
                        pipeline_mode=pipeline_mode,
                        reviewer_type=reviewer_type,
                        issue_number=pipeline.issue_number,
                        review_cycle=review_cycle + 1,
                        prior_feedback=review_feedback,
                    )

                    reviewer_command = [
                        "claude",
                        "--dangerously-skip-permissions",
                        "--print",
                        "--verbose",
                        "--output-format",
                        "stream-json",
                        "--model",
                        "opus",
                        "--max-turns",
                        "50",
                        review_prompt,
                    ]

                    reviewer_env = {
                        **sandbox_env,
                        "EGG_REVIEWER_TYPE": reviewer_type,
                    }

                    try:
                        rev_exit, rev_logs = _spawn_and_wait(
                            spawner=spawner,
                            pipeline_id=pipeline_id,
                            agent_role=AgentRole.REVIEWER,
                            issue_number=pipeline.issue_number,
                            host_repos_dir=host_repos_dir,
                            gateway_mode=phase_gateway_mode,
                            repos=repos,
                            phase=current_phase.value,
                            sandbox_env=reviewer_env,
                            sandbox_command=reviewer_command,
                            timeout=1800,
                            store=store,
                        )
                    except ContainerSpawnError as e:
                        logger.warning(
                            "Reviewer failed to spawn, treating as approved",
                            pipeline_id=pipeline_id,
                            reviewer_type=reviewer_type,
                            error=str(e),
                        )
                        all_verdicts[reviewer_type] = None
                        continue

                    if rev_exit != 0:
                        logger.warning(
                            "Reviewer exited non-zero, treating as approved",
                            pipeline_id=pipeline_id,
                            reviewer_type=reviewer_type,
                            exit_code=rev_exit,
                        )
                        all_verdicts[reviewer_type] = None
                        continue

                    # Read this reviewer's verdict
                    all_verdicts[reviewer_type] = _read_review_verdict(
                        repo_path,
                        current_phase.value,
                        reviewer_type=reviewer_type,
                        pipeline_mode=pipeline_mode,
                        issue_number=pipeline.issue_number,
                    )

                # Aggregate all verdicts
                overall_verdict, combined_feedback = _aggregate_review_verdicts(all_verdicts)

                if overall_verdict == "approved":
                    logger.info(
                        "All reviewers approved",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        review_cycle=review_cycle + 1,
                    )
                    break  # Advance to next phase

                # needs_revision — check circuit breaker
                max_cycles = pipeline.config.max_review_cycles
                if review_cycle + 1 >= max_cycles:
                    logger.warning(
                        "Review circuit breaker — advancing despite needs_revision",
                        pipeline_id=pipeline_id,
                        phase=current_phase.value,
                        review_cycles=review_cycle + 1,
                        max_review_cycles=max_cycles,
                    )
                    break

                # Store feedback and loop
                review_feedback = combined_feedback
                pipeline = store.load_pipeline(pipeline_id)
                phase_execution = pipeline.get_phase_execution(current_phase)
                phase_execution.review_cycles = review_cycle + 1
                store.save_pipeline(pipeline)

                logger.info(
                    "Review needs revision — looping",
                    pipeline_id=pipeline_id,
                    phase=current_phase.value,
                    review_cycle=review_cycle + 1,
                    feedback_preview=review_feedback[:200] if review_feedback else "",
                )
                # Continue inner while loop → re-spawn worker with feedback

            # If the phase failed, the outer loop should also break
            if phase_failed:
                break

            # Phase succeeded — mark complete and advance
            pipeline = store.load_pipeline(pipeline_id)
            phase_execution = pipeline.get_phase_execution(current_phase)
            phase_execution.status = PipelineStatus.COMPLETE
            phase_execution.completed_at = datetime.utcnow()

            # After plan phase: populate contract with task structure
            if current_phase.value == "plan" and pipeline_mode == "local":
                _populate_contract_from_plan(repo_path, pipeline_id)

            # --- HITL gate: pause for human approval ---
            if pipeline.config.hitl_gates and current_phase.value in _HITL_GATE_PHASES:
                draft_content = _read_phase_draft(
                    repo_path, current_phase.value, pipeline_mode, pipeline.issue_number
                )
                phase_label = "analysis" if current_phase.value == "refine" else current_phase.value
                question = (
                    f"The {current_phase.value} phase has completed. "
                    f"Please review the {phase_label} and approve to continue."
                )

                dq = get_decision_queue(pipeline_id, repo_path)
                decision = dq.queue_decision(
                    question=question,
                    context=draft_content,
                    options=["approve"],
                    timeout_seconds=pipeline.config.decision_timeout,
                )

                pipeline.status = PipelineStatus.AWAITING_HUMAN
                store.save_pipeline(pipeline)

                try:
                    dq.wait_for_decision(decision.id)
                except DecisionTimeoutError:
                    logger.warning(
                        "HITL gate timed out, advancing",
                        pipeline_id=pipeline_id,
                        decision_id=decision.id,
                    )

                # Resume — reload since decision resolution may have modified state
                pipeline = store.load_pipeline(pipeline_id)
                pipeline.status = PipelineStatus.RUNNING
                store.save_pipeline(pipeline)

            # Determine next phase
            next_phases = transitions.get(current_phase, [])
            if not next_phases:
                # Terminal phase — pipeline complete
                pipeline.status = PipelineStatus.COMPLETE
                store.save_pipeline(pipeline)
                logger.info("Pipeline complete", pipeline_id=pipeline_id)
                break

            # Advance to next phase
            next_phase = next_phases[0]
            pipeline.current_phase = next_phase
            store.save_pipeline(pipeline)

            logger.info(
                "Phase advanced",
                pipeline_id=pipeline_id,
                from_phase=current_phase.value,
                to_phase=next_phase.value,
            )

    except Exception as e:
        logger.error(
            "Pipeline execution failed", pipeline_id=pipeline_id, error=str(e), exc_info=True
        )
        try:
            store = get_state_store(repo_path)
            pipeline = store.load_pipeline(pipeline_id)
            pipeline.status = PipelineStatus.FAILED
            pipeline.error = str(e)
            store.save_pipeline(pipeline)
        except Exception:
            pass


@pipelines_bp.route("/<pipeline_id>/start", methods=["POST"])
def start_pipeline(pipeline_id: str) -> tuple[Response, int]:
    """
    Start pipeline execution.

    Spawns containers for each phase in sequence, advancing through
    the phase DAG until completion or failure. Runs in a background thread.

    URL params:
        pipeline_id: Pipeline ID

    Response:
        {
            "success": true,
            "message": "Pipeline started",
            "data": {
                "pipeline_id": "local-a1b2c3d4",
                "status": "running"
            }
        }
    """
    repo_path = get_repo_path()

    try:
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)

        if pipeline.status == PipelineStatus.RUNNING:
            return make_error_response(
                f"Pipeline {pipeline_id} is already running",
                status_code=409,
            )

        if pipeline.status == PipelineStatus.AWAITING_HUMAN:
            return make_error_response(
                f"Pipeline {pipeline_id} is awaiting human approval",
                status_code=409,
            )

        if pipeline.status in (PipelineStatus.COMPLETE, PipelineStatus.FAILED):
            return make_error_response(
                f"Pipeline {pipeline_id} is already {pipeline.status.value}",
                status_code=409,
            )

        # Mark pipeline as running
        pipeline.status = PipelineStatus.RUNNING
        store.save_pipeline(pipeline)

        # Run the pipeline in a background thread
        thread = threading.Thread(
            target=_run_pipeline,
            args=(pipeline_id, repo_path),
            daemon=True,
            name=f"pipeline-{pipeline_id}",
        )
        thread.start()

        logger.info("Pipeline started", pipeline_id=pipeline_id)

        return make_success_response(
            "Pipeline started",
            data={
                "pipeline_id": pipeline_id,
                "status": "running",
                "current_phase": pipeline.current_phase.value,
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

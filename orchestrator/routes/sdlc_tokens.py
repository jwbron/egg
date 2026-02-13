"""
SDLC token-gated approval endpoints.

Provides token generation and validation for SDLC pipeline phase approvals.
Tokens are stored in-memory (ephemeral, single-session lifetime).
"""

import functools
import hashlib
import os
import secrets
import sys
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


from sdlc_wordlist import WORD_LIST

logger = get_logger("orchestrator.sdlc_tokens")

sdlc_tokens_bp = Blueprint("sdlc_tokens", __name__, url_prefix="/api/v1/sdlc-tokens")


def _check_launcher_auth() -> tuple[bool, str]:
    """Validate the launcher secret from the Authorization header.

    The /generate and /reset endpoints are privileged — only the entrypoint
    (running as root before Claude starts) should call them. The launcher
    secret is unavailable to the sandbox egg user, preventing Claude from
    calling these endpoints directly.
    """
    expected = os.environ.get("EGG_LAUNCHER_SECRET", "")
    if not expected:
        # If no secret is configured, deny all requests to privileged endpoints.
        # This prevents accidental exposure in misconfigured deployments.
        return False, "Launcher secret not configured"

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False, "Missing or invalid Authorization header"

    provided = auth_header[7:]
    if secrets.compare_digest(provided, expected):
        return True, ""

    return False, "Invalid launcher authorization token"


def _require_launcher_auth(f: Any) -> Any:
    """Decorator requiring launcher secret auth on privileged endpoints."""

    @functools.wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        is_valid, error = _check_launcher_auth()
        if not is_valid:
            logger.warning(
                "SDLC endpoint auth failed",
                endpoint=request.path,
                error=error,
                source_ip=request.remote_addr,
            )
            return _make_error(error, status_code=401)
        return f(*args, **kwargs)

    return decorated


# In-memory token store: pipeline_id -> token data
# Tokens are ephemeral (one session lifetime); orchestrator is a singleton.
# CAVEAT: If the orchestrator restarts while a pipeline is token-gated,
# the in-memory tokens are lost but Pipeline.sdlc_token_gated remains True
# in persistent storage. Use the /reset endpoint to recover from this state.
_token_store: dict[str, dict[str, Any]] = {}

# Only the first two PipelinePhase values (refine, plan) are token-gated.
# The implement and pr phases proceed automatically after plan approval.
# NOTE: If this set changes, also update the regex in sdlc-approve.sh.
VALID_PHASES = {"refine", "plan"}


def _generate_token() -> str:
    """Generate a 3-word token like APPLE-HORSE-RIVER."""
    words = [secrets.choice(WORD_LIST) for _ in range(3)]
    return "-".join(words)


def _hash_token(token: str) -> str:
    """SHA-256 hash a token for storage."""
    return hashlib.sha256(token.upper().encode()).hexdigest()


def has_tokens_for_pipeline(pipeline_id: str) -> bool:
    """Check if tokens have been generated for a pipeline."""
    return pipeline_id in _token_store


def _make_error(message: str, status_code: int = 400) -> tuple[Response, int]:
    """Create an error response."""
    return jsonify({"success": False, "message": message}), status_code


def _make_success(message: str, data: dict[str, Any] | None = None) -> tuple[Response, int]:
    """Create a success response."""
    response: dict[str, Any] = {"success": True, "message": message}
    if data:
        response["data"] = data
    return jsonify(response), 200


@sdlc_tokens_bp.route("/generate", methods=["POST"])
@_require_launcher_auth
def generate_tokens() -> tuple[Response, int]:
    """Generate approval tokens for an SDLC pipeline.

    Requires launcher secret authentication. Only callable by the entrypoint
    (running as root) — not by Claude in the sandbox.

    Request body:
        {"pipeline_id": "issue-596"}

    Returns plaintext tokens (one-time display to human).
    """
    data = request.get_json() or {}
    pipeline_id = data.get("pipeline_id")

    if not pipeline_id:
        return _make_error("Missing pipeline_id")

    if pipeline_id in _token_store:
        return _make_error(f"Tokens already generated for pipeline {pipeline_id}", 409)

    refine_token = _generate_token()
    plan_token = _generate_token()

    # Ensure tokens are different
    while plan_token == refine_token:
        plan_token = _generate_token()

    _token_store[pipeline_id] = {
        "refine_hash": _hash_token(refine_token),
        "plan_hash": _hash_token(plan_token),
        "refine_used": False,
        "plan_used": False,
    }

    logger.info("SDLC tokens generated", pipeline_id=pipeline_id)

    return _make_success(
        "Tokens generated",
        data={
            "pipeline_id": pipeline_id,
            "refine_token": refine_token,
            "plan_token": plan_token,
        },
    )


@sdlc_tokens_bp.route("/approve", methods=["POST"])
def approve_phase() -> tuple[Response, int]:
    """Validate a token and approve an SDLC phase.

    Request body:
        {
            "pipeline_id": "issue-596",
            "phase": "refine",
            "token": "APPLE-HORSE-RIVER"
        }

    Returns:
        200: Phase approved
        400: Bad input
        403: Wrong token
        404: No tokens for pipeline
        409: Token already used
    """
    data = request.get_json() or {}
    pipeline_id = data.get("pipeline_id")
    phase = data.get("phase")
    token = data.get("token")

    if not pipeline_id:
        return _make_error("Missing pipeline_id")
    if not phase:
        return _make_error("Missing phase")
    if not token:
        return _make_error("Missing token")

    if phase not in VALID_PHASES:
        return _make_error(
            f"Invalid phase: {phase}. Must be one of: {', '.join(sorted(VALID_PHASES))}"
        )

    if pipeline_id not in _token_store:
        return _make_error(f"No tokens found for pipeline {pipeline_id}", 404)

    store = _token_store[pipeline_id]
    hash_key = f"{phase}_hash"
    used_key = f"{phase}_used"

    if store[used_key]:
        return _make_error(f"Token for phase '{phase}' has already been used", 409)

    # Timing-safe comparison of token hashes
    provided_hash = _hash_token(token)
    if not secrets.compare_digest(provided_hash, store[hash_key]):
        logger.warning(
            "SDLC token validation failed",
            pipeline_id=pipeline_id,
            phase=phase,
        )
        return _make_error("Invalid token", 403)

    # Mark token as used
    store[used_key] = True

    # Resolve any pending decisions for this pipeline/phase
    _resolve_phase_decisions(pipeline_id, phase)

    logger.info("SDLC phase approved", pipeline_id=pipeline_id, phase=phase)

    return _make_success(
        f"Phase '{phase}' approved",
        data={"pipeline_id": pipeline_id, "phase": phase},
    )


@sdlc_tokens_bp.route("/reset", methods=["POST"])
@_require_launcher_auth
def reset_token_gate() -> tuple[Response, int]:
    """Clear token-gated state for a pipeline.

    Requires launcher secret authentication. Only callable by privileged
    callers — not by Claude in the sandbox.

    Recovery endpoint for when the orchestrator restarts and in-memory tokens
    are lost while Pipeline.sdlc_token_gated is still True in persistent storage.

    Request body:
        {"pipeline_id": "issue-596"}
    """
    data = request.get_json() or {}
    pipeline_id = data.get("pipeline_id")

    if not pipeline_id:
        return _make_error("Missing pipeline_id")

    # Clear persistent flag first — if this fails, don't clear in-memory
    # tokens, since the pipeline would remain gated in persistent storage
    # while appearing cleared in memory.
    try:
        from routes import get_repo_path
        from state_store import PipelineNotFoundError, get_state_store

        repo_path = get_repo_path()
        store = get_state_store(repo_path)
        pipeline = store.load_pipeline(pipeline_id)
        if pipeline.sdlc_token_gated:
            pipeline.sdlc_token_gated = False
            store.save_pipeline(pipeline)
            logger.info("Token gate cleared", pipeline_id=pipeline_id)
    except PipelineNotFoundError:
        pass  # Pipeline doesn't exist in store — just clear in-memory tokens
    except Exception as e:
        logger.error(
            "Failed to clear persistent token gate",
            pipeline_id=pipeline_id,
            error=str(e),
            exc_info=True,
        )
        return _make_error("Failed to clear token gate in persistent storage", 503)

    # Only clear in-memory tokens after persistent flag is successfully cleared
    _token_store.pop(pipeline_id, None)

    return _make_success("Token gate cleared", data={"pipeline_id": pipeline_id})


def _is_phase_transition_decision(decision: Any, phase: str) -> bool:
    """Check if a decision is a phase-transition gate for the given phase.

    Matches the specific question format produced by the HITL gate in
    pipelines.py: "The {phase} phase has completed. ..."
    """
    expected_prefix = f"the {phase} phase has completed"
    return decision.question.lower().startswith(expected_prefix)


def _resolve_phase_decisions(pipeline_id: str, phase: str) -> None:
    """Resolve pending HITL decisions for a pipeline phase."""
    from routes import get_repo_path

    try:
        from decision_queue import get_decision_queue

        repo_path = get_repo_path()
        queue = get_decision_queue(pipeline_id, repo_path)
        pending = queue.get_pending_decisions()

        for decision in pending:
            if _is_phase_transition_decision(decision, phase):
                queue.resolve_decision(decision.id, f"Approved via SDLC token ({phase})")
                logger.info(
                    "Auto-resolved decision via SDLC token",
                    pipeline_id=pipeline_id,
                    decision_id=decision.id,
                    phase=phase,
                )
    except Exception as e:
        # Don't fail the approval if decision resolution fails
        logger.warning(
            "Failed to auto-resolve decisions",
            pipeline_id=pipeline_id,
            phase=phase,
            error=str(e),
        )

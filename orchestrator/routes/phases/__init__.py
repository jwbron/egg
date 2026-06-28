"""
Phase transition endpoints for egg-orchestrator.

Provides REST endpoints for advancing pipeline phases with validation.

This package is the stable public API surface for the ``phases`` blueprint
(file-decomposition pattern, #3312). Per decision-8 the ``@phases_bp.route``
decorators stay here on thin wrappers that delegate to private submodules:

- ``_responses``    — make_error_response / make_success_response
- ``_transitions``  — PHASE_TRANSITIONS, validate_phase_transition,
  _clear_concurrent_state
- ``_gates``        — conditional-ACK HITL gate + unresolved decision/gap
  collectors
- ``_status``       — get_current_phase / start_phase / fail_phase
- ``_advance``      — advance_phase (the plan-exit phase-transition state
  machine)
- ``_complete``     — complete_phase
- ``_populate``     — populate_contract

The barrel re-exports every externally-referenced or test-patched symbol so
``from routes.phases import _foo`` and ``patch("routes.phases._foo")`` keep
resolving. Submodules invoke the barrel-patched dependencies through this
package module (``import routes.phases as _pkg``) so the existing
``patch("routes.phases.<name>")`` seams stay effective unchanged — the names
were module globals of the pre-split file, and package-attribute access
reproduces that lookup exactly.
"""

import sys
from pathlib import Path

from flask import Blueprint, Response

# Add parent directory (orchestrator/) to path for imports. The sub-package
# lives one level deeper than the original module, so the walk-up gains a
# ``.parent`` versus the pre-split file.
_parent_path = Path(__file__).parent.parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

# Add shared directory to path for logging (egg-root/shared).
_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


# Barrel-patched dependencies + re-exported domain surface. Imported here so
# ``routes.phases.<name>`` is a single patch point; the private submodules call
# the patched seams via ``_pkg`` so
# ``patch("routes.phases.get_pipeline_state_lock")`` (etc.) keeps working.
from decision_queue import get_decision_queue  # noqa: E402,F401
from lifecycle_auth import require_lifecycle_secret  # noqa: E402
from models import (  # noqa: E402,F401 — re-export of pre-split module globals
    DecisionStatus,
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)
from peer_consensus import get_peer_consensus_tracker  # noqa: E402,F401
from routes import get_state_store_for_pipeline  # noqa: E402,F401
from state_store import (  # noqa: E402,F401
    InvalidPipelineIdError,
    PipelineNotFoundError,
    VersionConflictError,
    get_pipeline_state_lock,
)

logger = get_logger("orchestrator.phases")

phases_bp = Blueprint("phases", __name__, url_prefix="/api/v1/pipelines")

# Private submodules. Imported after the blueprint + shared deps + logger exist
# so the ``import routes.phases as _pkg`` barrel access inside them resolves
# against a populated package module.
from . import (  # noqa: E402,F401
    _advance,
    _complete,
    _gates,
    _populate,
    _responses,
    _status,
    _transitions,
)

# Re-export the stable public / test-patched surface (pattern §a/§d).
from ._gates import (  # noqa: E402,F401
    CONDITIONAL_ACK_ADDRESS,
    CONDITIONAL_ACK_APPROVE,
    CONDITIONAL_ACK_GATE_MARKER,
    CONDITIONAL_ACK_OPTIONS,
    CONDITIONAL_ACK_REJECT,
    _collect_unresolved_contract_gaps,
    _collect_unresolved_phase_decisions,
    _ensure_conditional_ack_gate,
    _existing_conditional_ack_gate,
)
from ._responses import (  # noqa: E402,F401
    make_error_response,
    make_success_response,
)
from ._transitions import (  # noqa: E402,F401
    PHASE_TRANSITIONS,
    _clear_concurrent_state,
    validate_phase_transition,
)

# ---- Route registrations -------------------------------------------------
# Decision-8: decorators stay in __init__.py on thin wrappers; the bodies live
# in the private submodules above.


@phases_bp.route("/<pipeline_id>/phase", methods=["GET"])
def get_current_phase(pipeline_id: str) -> tuple[Response, int]:
    return _status.get_current_phase(pipeline_id)


@phases_bp.route("/<pipeline_id>/phase", methods=["POST"])
@require_lifecycle_secret
def advance_phase(pipeline_id: str) -> tuple[Response, int]:
    return _advance.advance_phase(pipeline_id)


@phases_bp.route("/<pipeline_id>/phase/start", methods=["POST"])
@require_lifecycle_secret
def start_phase(pipeline_id: str) -> tuple[Response, int]:
    return _status.start_phase(pipeline_id)


@phases_bp.route("/<pipeline_id>/phase/complete", methods=["POST"])
@require_lifecycle_secret
def complete_phase(pipeline_id: str) -> tuple[Response, int]:
    return _complete.complete_phase(pipeline_id)


@phases_bp.route("/<pipeline_id>/phase/populate-contract", methods=["POST"])
@require_lifecycle_secret
def populate_contract(pipeline_id: str) -> tuple[Response, int]:
    return _populate.populate_contract(pipeline_id)


@phases_bp.route("/<pipeline_id>/phase/fail", methods=["POST"])
@require_lifecycle_secret
def fail_phase(pipeline_id: str) -> tuple[Response, int]:
    return _status.fail_phase(pipeline_id)

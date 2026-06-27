"""Decision endpoints for HITL integration.

Provides REST endpoints for queuing, polling, and resolving
human-in-the-loop decisions.

This package is the stable public API surface for the ``decisions`` blueprint
(file-decomposition pattern, #3312). Per decision-8 the ``@decisions_bp.route``
decorators stay here on thin wrappers that delegate to private submodules:

- ``_responses``       — make_error_response / make_success_response
- ``_query``           — list / get / queue decision read+create endpoints
- ``_resolve``         — resolve_decision + _resolve_contract_decision
- ``_handlers``        — HITL resolution-dispatch hooks
- ``_graph_mutations`` — conditional-ACK consensus-graph mutations
- ``_lifecycle``       — cancel / feedback-answer / queue-status endpoints

The barrel re-exports every externally-referenced or test-patched symbol so
``from routes.decisions import _foo`` and ``patch("routes.decisions._foo")``
keep resolving. Submodules invoke the barrel-patched dependencies and dispatch
hooks through this package module (``import routes.decisions as _pkg``) so the
existing ``patch("routes.decisions.<name>")`` seams stay effective unchanged —
the names were module globals of the pre-split file, and package-attribute
access reproduces that lookup exactly.
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


# Barrel-patched dependencies. Imported here so ``routes.decisions.<name>``
# is a single patch point; the private submodules call them via ``_pkg`` so
# ``patch("routes.decisions.get_decision_queue")`` (etc.) keeps working.
from decision_queue import (  # noqa: E402
    DecisionAlreadyResolvedError,  # noqa: F401 — re-export
    DecisionNotFoundError,  # noqa: F401 — re-export
    get_decision_queue,  # noqa: F401 — re-export / _pkg seam
)
from events import EventType, emit_event  # noqa: E402,F401
from lifecycle_auth import require_lifecycle_secret  # noqa: E402
from peer_consensus import get_peer_consensus_tracker  # noqa: E402,F401
from routes import get_state_store_for_pipeline  # noqa: E402,F401

logger = get_logger("orchestrator.decisions")

decisions_bp = Blueprint("decisions", __name__, url_prefix="/api/v1/pipelines")

# Private submodules. Imported after the blueprint + shared deps + logger exist
# so the ``import routes.decisions as _pkg`` barrel access inside them resolves
# against a populated package module.
from . import (  # noqa: E402,F401
    _graph_mutations,
    _handlers,
    _lifecycle,
    _query,
    _resolve,
)

# Re-export the stable public / test-patched surface (pattern §a/§d).
from ._graph_mutations import (  # noqa: E402,F401
    _force_nack_conditional_edges,
    _invalidate_conditional_acks,
    _persist_deferred_actions,
)
from ._handlers import (  # noqa: E402,F401
    _COMPLETE_TASK_RESOLUTION_RE,
    _handle_conditional_ack_gate,
    _handle_restart_agent,
    _maybe_complete_task_from_resolution,
    _normalize_choice_resolution,
)
from ._resolve import _resolve_contract_decision  # noqa: E402,F401
from ._responses import (  # noqa: E402,F401
    make_error_response,
    make_success_response,
)

# ---- Route registrations -------------------------------------------------
# Decision-8: decorators stay in __init__.py on thin wrappers; the bodies live
# in the private submodules above.


@decisions_bp.route("/<pipeline_id>/decisions", methods=["GET"])
def list_decisions(pipeline_id: str) -> tuple[Response, int]:
    return _query.list_decisions(pipeline_id)


@decisions_bp.route("/<pipeline_id>/decisions", methods=["POST"])
def queue_decision(pipeline_id: str) -> tuple[Response, int]:
    return _query.queue_decision(pipeline_id)


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>", methods=["GET"])
def get_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    return _query.get_decision(pipeline_id, decision_id)


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>/resolve", methods=["POST"])
@require_lifecycle_secret
def resolve_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    return _resolve.resolve_decision(pipeline_id, decision_id)


@decisions_bp.route("/<pipeline_id>/decisions/<decision_id>/cancel", methods=["POST"])
@require_lifecycle_secret
def cancel_decision(pipeline_id: str, decision_id: str) -> tuple[Response, int]:
    return _lifecycle.cancel_decision(pipeline_id, decision_id)


@decisions_bp.route("/<pipeline_id>/feedback/answer", methods=["POST"])
@require_lifecycle_secret
def answer_feedback(pipeline_id: str) -> tuple[Response, int]:
    return _lifecycle.answer_feedback(pipeline_id)


@decisions_bp.route("/<pipeline_id>/decisions/status", methods=["GET"])
def get_queue_status(pipeline_id: str) -> tuple[Response, int]:
    return _lifecycle.get_queue_status(pipeline_id)

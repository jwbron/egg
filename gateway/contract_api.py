"""Contract API endpoints on the gateway.

These endpoints are the sandbox's entry point for contract reads and
writes.  The gateway does not own contract state: it forwards each
request to the orchestrator (``EGG_ORCHESTRATOR_URL``), which is the
single source of truth during pipeline execution (see #1781).  This
preserves the sandbox's "only talk to the gateway" policy while
eliminating the per-agent worktree divergence that caused spurious
BRC NACKs.

The gateway still owns:
  - session authentication (``require_session_auth``)
  - role resolution from session metadata (prevents header-based
    privilege escalation)

It sends the verified role to the orchestrator via ``X-Egg-Role``.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import Blueprint, Response, g, jsonify, request

try:
    from .auth import require_session_auth
except ImportError:
    from auth import require_session_auth  # type: ignore[no-redef, import-untyped]

# Add shared directory to path for egg_contracts
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from egg_contracts import Role, get_contract_role

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(  # type: ignore[misc]
        name: str,
        level: int | str = logging.INFO,
        component: str | None = None,
    ) -> logging.Logger:
        return logging.getLogger(name)


logger = get_logger("gateway.contract")

contract_bp = Blueprint("contract", __name__, url_prefix="/api/v1/contract")

# Upstream orchestrator that owns contract state. The default matches the
# docker-compose service name used elsewhere in the gateway
# (``checkpoint_handler.py``).
_DEFAULT_ORCHESTRATOR_URL = "http://egg-orchestrator:9849"

# Default timeout for orchestrator calls. Kept tight (mutations are
# quick) so a stuck orchestrator surfaces as an error instead of a
# silent stall.
_ORCHESTRATOR_TIMEOUT_SECONDS = 30

_VALID_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _orchestrator_url() -> str:
    return os.environ.get("EGG_ORCHESTRATOR_URL", _DEFAULT_ORCHESTRATOR_URL).rstrip("/")


def _resolve_role(value: str) -> Role | None:
    """Resolve a role string to the coarse contract :class:`Role`."""
    normalized = value.lower()
    try:
        return Role(normalized)
    except ValueError:
        return get_contract_role(normalized)


def get_role_from_context() -> Role | None:
    """Resolve the caller's contract role from the request context.

    Priority (highest to lowest):
      1. Session metadata — production path, set by the launcher.
      2. ``X-Egg-Role`` header — only honored when
         ``EGG_ENABLE_TEST_ROLE_HEADER=1``.
      3. ``EGG_AGENT_ROLE`` environment variable — development only.
    """
    if hasattr(g, "session") and g.session:
        session_role = getattr(g.session, "agent_role", None)
        if session_role:
            return _resolve_role(session_role)

    if os.environ.get("EGG_ENABLE_TEST_ROLE_HEADER") == "1":
        header_role = request.headers.get("X-Egg-Role")
        if header_role:
            return _resolve_role(header_role)

    env_role = os.environ.get("EGG_AGENT_ROLE")
    if env_role:
        return _resolve_role(env_role)

    return None


def _session_pipeline_id() -> str | None:
    if hasattr(g, "session") and g.session:
        return getattr(g.session, "pipeline_id", None)
    return None


def _validate_identifier_value(identifier: int | str) -> tuple[Response, int] | None:
    if isinstance(identifier, int):
        return None
    if not _VALID_IDENTIFIER_RE.match(identifier):
        return _error(
            "Invalid identifier: must contain only alphanumeric characters, hyphens, and underscores",
            status_code=400,
        )
    return None


def _error(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    payload: dict[str, Any] = {"success": False, "message": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _forward_response(payload: dict[str, Any], status_code: int) -> tuple[Response, int]:
    """Return the orchestrator's JSON payload unchanged."""
    return jsonify(payload), status_code


def _proxy(
    method: str,
    path: str,
    *,
    role: Role | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Forward a request to the orchestrator and relay its response.

    The orchestrator response is relayed verbatim.  Connection errors
    surface as 502 so callers can distinguish "orchestrator unreachable"
    from "contract not found".
    """
    url = f"{_orchestrator_url()}{path}"
    if params:
        from urllib.parse import urlencode

        url = f"{url}?{urlencode(params)}"

    headers = {"Accept": "application/json"}
    if role is not None:
        headers["X-Egg-Role"] = role.value

    data: bytes | None = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body).encode("utf-8")

    try:
        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, timeout=_ORCHESTRATOR_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                logger.error(
                    "Non-JSON response from orchestrator contract API",
                    path=path,
                    status=response.status,
                )
                return _error("Malformed response from orchestrator", status_code=502)
            return _forward_response(payload, response.status)
    except HTTPError as exc:
        raw = exc.read().decode("utf-8") if hasattr(exc, "read") else ""
        try:
            payload = json.loads(raw) if raw else {"success": False, "message": str(exc)}
        except json.JSONDecodeError:
            payload = {"success": False, "message": raw or str(exc)}
        return _forward_response(payload, exc.code)
    except (URLError, TimeoutError) as exc:
        logger.error(
            "Orchestrator unreachable for contract request",
            path=path,
            error=str(exc),
        )
        return _error(
            "Orchestrator unreachable — try again",
            status_code=502,
        )


def _context_params(body: dict[str, Any] | None = None) -> dict[str, str]:
    """Collect bootstrap hints (pipeline_id, repo) to forward upstream."""
    params: dict[str, str] = {}
    pipeline_id = None
    if body:
        pipeline_id = body.get("pipeline_id")
    if not pipeline_id:
        pipeline_id = request.args.get("pipeline_id") or _session_pipeline_id()
    if pipeline_id:
        params["pipeline_id"] = pipeline_id

    repo = (body or {}).get("repo") or request.args.get("repo")
    if repo:
        params["repo"] = repo
    return params


@contract_bp.route("/<int:issue_number>", methods=["GET"])
@require_session_auth
def get_contract(issue_number: int) -> tuple[Response, int]:
    """Fetch the contract for ``issue_number`` (integer form)."""
    return _get_contract_impl(issue_number)


@contract_bp.route("/<identifier>", methods=["GET"])
@require_session_auth
def get_contract_by_pipeline_id(identifier: str) -> tuple[Response, int]:
    """Fetch the contract by string identifier (pipeline ID)."""
    return _get_contract_impl(identifier)


def _get_contract_impl(identifier: int | str) -> tuple[Response, int]:
    validation_error = _validate_identifier_value(identifier)
    if validation_error:
        return validation_error

    params = _context_params()
    include_audit = request.args.get("include_audit_log", "false").lower() == "true"
    if include_audit:
        params["include_audit_log"] = "true"

    return _proxy(
        "GET",
        f"/api/v1/contracts/{identifier}",
        params=params,
    )


@contract_bp.route("/exists/<int:issue_number>", methods=["GET"])
@require_session_auth
def check_contract_exists(issue_number: int) -> tuple[Response, int]:
    return _check_contract_exists_impl(issue_number)


@contract_bp.route("/exists/<identifier>", methods=["GET"])
@require_session_auth
def check_contract_exists_by_pipeline_id(identifier: str) -> tuple[Response, int]:
    return _check_contract_exists_impl(identifier)


def _check_contract_exists_impl(identifier: int | str) -> tuple[Response, int]:
    validation_error = _validate_identifier_value(identifier)
    if validation_error:
        return validation_error
    return _proxy(
        "GET",
        f"/api/v1/contracts/{identifier}/exists",
        params=_context_params(),
    )


@contract_bp.route("/mutate", methods=["POST"])
@require_session_auth
def mutate_contract() -> tuple[Response, int]:
    """Apply a role-validated mutation to the contract.

    Identifier is accepted in the request body for backwards
    compatibility with the CLI (``egg-contract``) and older callers.
    """
    body = request.get_json()
    if not body:
        return _error("Missing request body")

    identifier = body.get("identifier")
    if identifier is None:
        identifier = body.get("issue_number")
    if identifier is None:
        return _error("Missing identifier or issue_number")

    validation_error = _validate_identifier_value(identifier)
    if validation_error:
        return validation_error

    field_path = body.get("field_path")
    new_value = body.get("new_value", ...)
    if not field_path:
        return _error("Missing field_path")
    if new_value is ...:
        return _error("Missing new_value")

    role = get_role_from_context()
    if role is None:
        return _error(
            "Cannot determine agent role. Role must be set via workflow context.",
            status_code=403,
            details={"hint": "Set EGG_AGENT_ROLE via workflow inputs, not agent env vars"},
        )

    forwarded = {
        "field_path": field_path,
        "new_value": new_value,
        "actor": body.get("actor", "agent"),
    }
    if "reason" in body:
        forwarded["reason"] = body["reason"]
    # Forward pipeline_id in the body so the orchestrator can bootstrap
    # from the shared worktree on first access.
    params = _context_params(body)
    if "pipeline_id" in params:
        forwarded["pipeline_id"] = params["pipeline_id"]
    if "repo" in params:
        forwarded["repo"] = params["repo"]

    return _proxy(
        "POST",
        f"/api/v1/contracts/{identifier}/mutate",
        role=role,
        json_body=forwarded,
    )


@contract_bp.route("/validate", methods=["POST"])
@require_session_auth
def validate_contract_mutation() -> tuple[Response, int]:
    """Dry-run a mutation without applying it."""
    body = request.get_json()
    if not body:
        return _error("Missing request body")

    field_path = body.get("field_path")
    new_value = body.get("new_value", ...)
    if not field_path:
        return _error("Missing field_path")
    if new_value is ...:
        return _error("Missing new_value")

    role = get_role_from_context()
    if role is None:
        return _error("Cannot determine agent role", status_code=403)

    return _proxy(
        "POST",
        "/api/v1/contract-mutations/validate",
        role=role,
        json_body={"field_path": field_path, "new_value": new_value},
    )

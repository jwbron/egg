"""Artifact API endpoints on the gateway.

Sandbox entry point for served-read access to coordination artifacts
(``analysis-draft``, ``plan-draft``, ``architect-output``, ...).  The
gateway does **not** own artifact bytes: it forwards each request to the
orchestrator's ``/api/v1/artifacts/get`` route (``EGG_ORCHESTRATOR_URL``)
which is the single source of truth.  Same shape as ``contract_api.py``:
the gateway handles session authentication and role resolution, the
orchestrator owns the data.

Why this endpoint exists (cross-link #3002): the historical "read peer
artifact" channel for reviewers relied on the shared host object store
between agent worktrees — a producer's commit was *implicitly*
available in the reviewer's worktree because the two shared a ``.git``
directory.  When that assumption breaks (deployments without a shared
store, or even an isolated container model), the silent failure mode is
"reviewer reviews an empty diff".  This endpoint replaces the implicit
channel with a served read: any sandbox session can fetch a registered
artifact by name + commit SHA, regardless of whether the commit
resolves locally.

STRICT (HITL Q2 of #3077): the request schema has no ``path`` field;
agents pass a spec-registered ``name`` (``plan-draft``,
``analysis-draft``, ...) and the gateway / orchestrator resolve the
repo-relative path through :mod:`egg_contracts.artifact_spec`.  An
unknown name returns 400 listing the registered names; a non-hex ref
returns 400; an unresolvable ref or path-absent-at-ref returns a
structured 4xx (propagated from the orchestrator verbatim, *not*
wrapped to 500).

URL scheme::

    POST /api/v1/artifact/get   — read content of a registered artifact

Request body::

    {
        "name": "plan-draft",          # required, spec-registered
        "ref":  "abcdef0123..",        # required, 7-40 hex commit SHA
        "pipeline_id": "issue-3077",   # optional; defaults to session
        "identifier":  "issue-3077",   # optional; orchestrator derives
        "repo":        "owner/name"    # optional; multi-repo hint
    }

The orchestrator response (``data: {name, ref, path, content,
truncated}``) is returned verbatim.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Blueprint, Response, g, jsonify, request

try:
    from .auth import require_session_auth
except ImportError:
    from auth import require_session_auth  # type: ignore[no-redef, import-untyped]

# Shared packages live under ../shared relative to this file.
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


logger = get_logger("gateway.artifact")

artifact_bp = Blueprint("artifact", __name__, url_prefix="/api/v1/artifact")

# Upstream orchestrator — mirrors contract_api's default so a single
# ``EGG_ORCHESTRATOR_URL`` env var covers both endpoints.
_DEFAULT_ORCHESTRATOR_URL = "http://egg-orchestrator:9849"
_ORCHESTRATOR_TIMEOUT_SECONDS = 30

# Mirrors the orchestrator-side regex (``routes/artifacts.py``) so the
# gateway can reject a malformed ref before the orchestrator round-trip.
_HEX_REF_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _orchestrator_url() -> str:
    return os.environ.get("EGG_ORCHESTRATOR_URL", _DEFAULT_ORCHESTRATOR_URL).rstrip("/")


def _resolve_role(value: str) -> Role | None:
    normalized = value.lower()
    try:
        return Role(normalized)
    except ValueError:
        return get_contract_role(normalized)


def _role_from_context() -> Role | None:
    """Resolve the caller's contract role.

    Priority matches :mod:`gateway.contract_api`: session metadata
    first, then ``X-Egg-Role`` only when the test-role header is
    explicitly enabled, finally ``EGG_AGENT_ROLE`` for dev.  The
    request body's ``role`` field is *never* consulted — this is the
    "role from session, never from the request body" property the
    task description names.
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


def _error(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    payload: dict[str, Any] = {"success": False, "message": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _forward(
    payload: dict[str, Any],
    status_code: int,
) -> tuple[Response, int]:
    """Relay an orchestrator response untouched (no 500-wrapping)."""
    return jsonify(payload), status_code


def _proxy_post(
    path: str,
    *,
    role: Role,
    json_body: dict[str, Any],
    params: dict[str, str] | None = None,
) -> tuple[Response, int]:
    """Forward a POST to the orchestrator and relay its JSON response.

    The orchestrator's structured 4xx bodies pass through unchanged so
    callers see the original ``message`` / ``details``.  Connection
    failures map to 502 so the gateway is visibly distinguishable
    from a true orchestrator-side error.
    """
    url = f"{_orchestrator_url()}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Egg-Role": role.value,
    }
    data = json.dumps(json_body).encode("utf-8")

    try:
        req = Request(url, data=data, headers=headers, method="POST")
        with urlopen(req, timeout=_ORCHESTRATOR_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                logger.error(
                    "Non-JSON response from orchestrator artifact API",
                    path=path,
                    status=response.status,
                )
                return _error("Malformed response from orchestrator", status_code=502)
            return _forward(payload, response.status)
    except HTTPError as exc:
        # Orchestrator returned a structured error (4xx / 5xx).  Pass
        # the body through verbatim so the caller sees the registered-
        # names list / details the orchestrator built.
        raw = exc.read().decode("utf-8") if hasattr(exc, "read") else ""
        try:
            payload = json.loads(raw) if raw else {"success": False, "message": str(exc)}
        except json.JSONDecodeError:
            payload = {"success": False, "message": raw or str(exc)}
        return _forward(payload, exc.code)
    except (URLError, TimeoutError) as exc:
        logger.error(
            "Orchestrator unreachable for artifact request",
            path=path,
            error=str(exc),
        )
        return _error(
            "Orchestrator unreachable — try again",
            status_code=502,
        )


@artifact_bp.route("/get", methods=["POST"])
@require_session_auth
def get_artifact() -> tuple[Response, int]:
    """Fetch a registered artifact by name + commit SHA.

    Required body fields: ``name`` (spec-registered) and ``ref`` (7-40
    hex characters).  ``pipeline_id`` falls back to the session's id
    when not supplied; ``identifier`` and ``repo`` are optional hints
    the orchestrator side may consume.  The ``path`` field is
    **rejected at the gateway** so a misbehaving client can't even
    reach the orchestrator's schema check.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("Request body must be a JSON object")

    # Strip the forbidden ``path`` field at the wire boundary.  The
    # orchestrator also enforces this (defense in depth) but rejecting
    # here means the caller's error message names the offending field
    # rather than a generic forwarded error.
    if "path" in body:
        return _error(
            "The 'path' field is not accepted by this endpoint. "
            "Pass a spec-registered 'name' instead.",
            status_code=400,
        )

    name = body.get("name")
    ref = body.get("ref")
    if not isinstance(name, str) or not name:
        return _error("Missing or invalid 'name' (string required)")
    if not isinstance(ref, str) or not ref:
        return _error("Missing or invalid 'ref' (string required)")
    if not _HEX_REF_RE.match(ref):
        return _error(
            f"Invalid 'ref' {ref!r}: must be 7-40 hex characters",
            status_code=400,
        )

    role = _role_from_context()
    if role is None:
        return _error(
            "Cannot determine agent role. Role must be set via workflow context.",
            status_code=403,
            details={"hint": "Set EGG_AGENT_ROLE via workflow inputs, not agent env vars"},
        )

    pipeline_id = body.get("pipeline_id") or _session_pipeline_id()
    if not isinstance(pipeline_id, str) or not pipeline_id:
        return _error(
            "Missing 'pipeline_id' and no session pipeline available",
            status_code=400,
        )

    forwarded: dict[str, Any] = {
        "name": name,
        "ref": ref,
        "pipeline_id": pipeline_id,
    }
    identifier = body.get("identifier")
    if identifier is not None:
        # Pass through both shapes (str | int) — the orchestrator
        # coerces all-digit strings to int per the identifier spec.
        if not isinstance(identifier, str | int):
            return _error("'identifier' must be a string or integer when provided")
        forwarded["identifier"] = identifier

    repo = body.get("repo")
    if isinstance(repo, str) and repo:
        forwarded["repo"] = repo

    return _proxy_post(
        "/api/v1/artifacts/get",
        role=role,
        json_body=forwarded,
    )

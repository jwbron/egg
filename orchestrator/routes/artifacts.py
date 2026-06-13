"""Orchestrator artifact-read endpoint (#3077 slice-4).

Serves the committed content of a registered coordination artifact at a
hex-validated commit SHA via ``git show`` against the authoritative repo
on the orchestrator side.  The name-resolving design — agents never name
a repo path — is the slice-2 :mod:`egg_contracts.artifact_spec` registry
applied at the wire boundary: an unknown ``name`` is a 400 with the
registered names listed, never a 500.

Cross-links:

* #3002 — this endpoint is the blocking prerequisite for replacing the
  shared-object-store / per-agent-worktree coordination channel with a
  served read.  After this lands, sandbox agents fetch peer artifacts
  via ``egg-artifact`` (sandbox helper, task-4-3) instead of inheriting
  them through a shared ``.git`` directory.
* #3077 slice-2 (:mod:`egg_contracts.artifact_spec`) — the single
  source of truth for ``name → path template`` resolution.  Anything
  added there is automatically reachable through this endpoint.
* #3077 slice-3 (:func:`routes.signals._validate_producer_artifacts`)
  — propose-time validator that runs the same ``git show`` presence
  check on the producer side.  This endpoint is the *read* surface
  layered on the same registry.

STRICT per HITL Q2 (issue #3077): the request schema has **no** path
field.  An unregistered ``name`` is rejected at the wire with a 400
listing the registered names; a non-hex ``ref`` is a 400; an
unresolvable ref or a path-absent-at-ref is a structured 4xx — never a
500.  Server-side ``git show`` is the only read mechanism: agents do not
need the commit object locally.

URL scheme:
  POST   /api/v1/artifacts/get                          — read content

Request body (JSON)::

    {
        "name": "plan-draft",         # spec-registered name (required)
        "ref":  "abcdef0123..",       # 7-40 hex commit SHA (required)
        "pipeline_id": "issue-3077",  # required (worktree + identifier)
        "identifier":  "issue-3077",  # optional, derived from pipeline
        "repo":        "owner/name"   # optional, multi-repo hint
    }

Successful response::

    {
        "success": true,
        "message": "Artifact retrieved",
        "data": {
            "name": "plan-draft",
            "ref":  "<sha>",
            "path": ".egg-state/drafts/3077-plan.md",
            "content":   "<bytes>",
            "truncated": false
        }
    }

The ``content`` payload is the raw blob, decoded as UTF-8 with
replacement so a non-UTF-8 byte sequence cannot 500 the route.  When
the blob exceeds :data:`_ARTIFACT_MAX_BYTES`, ``content`` is the head
slice (re-decoded with replacement at the byte boundary) and
``truncated`` is ``True``.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, jsonify, request

# Shared packages live under ../../shared relative to this file.
_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# The orchestrator package lives one level up.  Mirrors the bootstrap
# in ``routes/contracts.py`` so this module is importable either as
# ``routes.artifacts`` (production) or as the bare module path that the
# orchestrator's script-mode startup uses.
_parent_path = Path(__file__).parent.parent
if str(_parent_path) not in sys.path:
    sys.path.insert(0, str(_parent_path))

from egg_contracts.artifact_spec import (  # noqa: E402
    all_specs,
    resolve_artifact_path,
    spec_by_name,
)

logger = logging.getLogger("orchestrator.artifacts")

artifacts_bp = Blueprint("artifacts", __name__, url_prefix="/api/v1/artifacts")


# 7-40 hex chars: the same shape ``git rev-parse`` accepts for an
# abbreviated or full SHA.  The wire schema rejects non-hex refs at
# 400 — there is no way for a malformed ``ref`` to reach ``git show``.
_HEX_REF_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

# Safe shape for an explicitly-passed ``identifier``.  The identifier is
# interpolated into the spec path template (``resolve_artifact_path``),
# so constrain it to a slug — letters, digits, ``.``, ``_``, ``-`` — that
# starts with an alphanumeric and contains no ``..`` substring (the
# negative lookahead) and no ``/`` (not in the charset).  ``git show``
# does not normalize ``..``, so this is defense-in-depth rather than a
# fix for a live traversal; it makes the strict no-path guarantee hold at
# the wire instead of relying on pathspec semantics.  All-digit issue
# numbers and ``issue-3077``-style pipeline ids both match.
_SAFE_IDENTIFIER_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]*$")

# Match the propose-time validator's git-show budget
# (orchestrator/routes/signals.py).  Plan/analysis drafts and architect
# outputs are well under 100 KiB today; the conservative ceiling protects
# the request budget while still serving any realistic artifact whole.
_ARTIFACT_MAX_BYTES = 256 * 1024  # 256 KiB

# Bound the subprocess so an unresponsive git can't pin a Flask worker
# indefinitely.  Mirrors the cap in ``_validate_producer_artifacts``
# (15s) — long enough for a slow filesystem and short enough to fail
# fast on a wedged worktree.
_GIT_SHOW_TIMEOUT_SECS = 15


def _error(
    message: str,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Return a structured error response (never raises)."""
    payload: dict[str, Any] = {"success": False, "message": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _success(
    message: str,
    data: dict[str, Any],
) -> tuple[Response, int]:
    return jsonify({"success": True, "message": message, "data": data}), 200


def _resolve_identifier(
    *,
    pipeline_id: str,
    explicit_identifier: str | int | None,
) -> tuple[str | int | None, tuple[Response, int] | None]:
    """Determine the identifier the spec path template renders against.

    The caller may pass ``identifier`` explicitly; otherwise we look up
    the pipeline (lazy import — keeps ``routes.artifacts`` importable
    from contexts that don't have the state-store dependency, e.g.
    unit tests) and derive it via the same ``_pipeline_identifier``
    helper :mod:`routes.signals` uses at propose-time.  This keeps the
    identifier convention single-sourced through
    :func:`routes.pipelines._pipeline_identifier`.

    Returns ``(identifier, error)`` where exactly one is ``None``.
    Lookup failures surface as structured 404s — the caller's
    ``pipeline_id`` is for a pipeline this orchestrator can't see, so
    "absent" is the truthful answer.
    """
    if explicit_identifier is not None:
        if isinstance(explicit_identifier, str):
            # Defense-in-depth: reject any identifier that could influence
            # the resolved path beyond the spec's intent before it reaches
            # the path template.  See ``_SAFE_IDENTIFIER_RE`` for why this
            # is belt-and-suspenders rather than a traversal fix.
            if not _SAFE_IDENTIFIER_RE.fullmatch(explicit_identifier):
                return None, _error(
                    f"Invalid 'identifier' {explicit_identifier!r}: must be digits "
                    "or a safe slug (letters, digits, '.', '_', '-'; no '/' or '..')",
                    status_code=400,
                    details={"identifier": explicit_identifier},
                )
            # Coerce all-digit strings to ``int`` so callers can pass
            # either ``3077`` (an issue number) or ``"issue-3077"`` (a
            # qualified pipeline id) without thinking about the slice-2
            # registry's ``str | int`` accepted shape.
            if explicit_identifier.isdigit():
                return int(explicit_identifier), None
        return explicit_identifier, None

    # Lazy imports — same rationale as ``routes.signals``: keep the
    # ~24k-line ``routes.pipelines`` and the state-store dependency
    # out of ``routes.artifacts`` import time so test contexts that
    # never call this code path don't pay the import cost.
    try:
        from routes import get_state_store_for_pipeline
    except ImportError:  # pragma: no cover — defensive
        from . import get_state_store_for_pipeline  # type: ignore[no-redef]

    try:
        from routes.pipelines import _pipeline_identifier
    except ImportError:  # pragma: no cover — defensive
        from .pipelines import _pipeline_identifier  # type: ignore[no-redef]

    try:
        from state_store import (
            InvalidPipelineIdError,
            PipelineNotFoundError,
            StateStoreError,
        )
    except ImportError:  # pragma: no cover — defensive
        return None, _error(
            "Orchestrator state-store unavailable",
            status_code=503,
        )

    try:
        _store, pipeline = get_state_store_for_pipeline(pipeline_id)
    except PipelineNotFoundError:
        return None, _error(
            f"Pipeline {pipeline_id!r} not found",
            status_code=404,
        )
    except InvalidPipelineIdError:
        return None, _error(
            f"Invalid pipeline_id: {pipeline_id!r}",
            status_code=400,
        )
    except StateStoreError as exc:  # pragma: no cover — defensive
        return None, _error(
            f"Failed to resolve pipeline {pipeline_id!r}: {exc}",
            status_code=503,
        )

    return _pipeline_identifier(pipeline.issue_number, pipeline_id), None


def _resolve_worktree(
    *,
    pipeline_id: str,
    repo_hint: str | None,
) -> tuple[Path | None, tuple[Response, int] | None]:
    """Locate the worktree the ``git show`` should run in.

    Prefers the shared pipeline worktree (the same one the propose-time
    validators read from).  Falls back to the orchestrator's repo path
    so post-pruning reads still work via the branch the producer
    pushed to.
    """
    try:
        import contract_store
    except ImportError:  # pragma: no cover — defensive
        return None, _error(
            "Orchestrator worktree resolver unavailable",
            status_code=503,
        )

    worktree = contract_store.resolve_pipeline_worktree(pipeline_id, repo_hint)
    if worktree is not None:
        return worktree, None

    # Worktree pruned (post-PR / archived): fall back to the main repo
    # so reads still resolve through the branch the producer pushed to.
    # ``get_repo_path`` honours the multi-repo layout and the request's
    # ``repo`` hint via ``request.get_json``.
    try:
        from routes import get_repo_path
    except ImportError:  # pragma: no cover — defensive
        from . import get_repo_path  # type: ignore[no-redef]

    repo_path = get_repo_path()
    if (repo_path / ".git").exists():
        return repo_path, None

    return None, _error(
        f"No repository available for pipeline {pipeline_id!r}",
        status_code=404,
    )


def _run_git_show(
    worktree: Path,
    ref: str,
    rel_path: str,
) -> tuple[bytes | None, tuple[Response, int] | None]:
    """Run ``git show ref:rel_path`` and classify the outcome.

    Returns ``(payload, error)`` where exactly one is ``None``.  Path-
    absent / ref-unresolvable failures map to structured 4xx so the
    sandbox helper can surface them as clean stderr messages; an
    infrastructure failure (timeout, OSError) is a 503 — explicitly
    *not* a 500.  Output is captured as bytes so a non-UTF-8 blob
    cannot kill the subprocess decoder.
    """
    cmd = ["git", "-C", str(worktree), "show", f"{ref}:{rel_path}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_GIT_SHOW_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, _error(
            f"git show timed out after {_GIT_SHOW_TIMEOUT_SECS}s for {ref[:8]}:{rel_path}",
            status_code=503,
        )
    except OSError as exc:  # pragma: no cover — defensive
        return None, _error(
            f"git show failed for {ref[:8]}:{rel_path}: {exc}",
            status_code=503,
        )

    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        # Distinguish the two structurally meaningful 4xx classes:
        #   * 404: the (ref, path) pair simply doesn't exist (most
        #     common — caller used a stale SHA or asked for an
        #     artifact that wasn't committed at that commit).
        #   * 422: the ref itself is unresolvable to git (typically
        #     "bad object" / "unknown revision").
        # Both are 4xx and both pass through the gateway verbatim.
        lowered = stderr.lower()
        # "invalid object name" is git's canonical phrasing when the ref
        # itself doesn't resolve (e.g. an abbreviated SHA that doesn't
        # match any commit object).  "unknown / bad revision" and "bad
        # object" cover the other shapes ``git show`` produces for the
        # same condition across versions.  Anything else (most commonly
        # "exists on disk, but not in <ref>") is a genuine path-absent
        # outcome and lands as 404.
        if (
            "invalid object name" in lowered
            or "unknown revision" in lowered
            or "bad object" in lowered
            or "bad revision" in lowered
        ):
            return None, _error(
                f"ref {ref!r} is not resolvable in this repository ({stderr or 'no stderr'})",
                status_code=422,
                details={"ref": ref, "path": rel_path},
            )
        return None, _error(
            f"artifact {rel_path!r} not found at {ref[:8]} ({stderr or 'no stderr'})",
            status_code=404,
            details={"ref": ref, "path": rel_path},
        )

    return result.stdout or b"", None


def _decode_with_cap(
    payload: bytes,
) -> tuple[str, bool]:
    """Decode the git-show payload, capping at :data:`_ARTIFACT_MAX_BYTES`.

    Mirrors the truncation contract used by
    ``routes.event_prompt._run_git_log``: re-decode the head slice with
    UTF-8 replacement so a multibyte sequence split at the byte boundary
    can't crash rendering.  Returns ``(content, truncated)``.
    """
    if len(payload) <= _ARTIFACT_MAX_BYTES:
        return payload.decode("utf-8", errors="replace"), False
    head = payload[:_ARTIFACT_MAX_BYTES]
    return head.decode("utf-8", errors="replace"), True


@artifacts_bp.route("/get", methods=["POST"])
def get_artifact() -> tuple[Response, int]:
    """Return the committed content of an artifact at ``ref``.

    Schema-level invariant: the request body has **no** ``path`` field.
    Path is always resolved server-side from ``name`` through the
    slice-2 spec registry — there is no way for an agent to read an
    arbitrary repo path through this endpoint.
    """
    # Reads are intentionally NOT role-gated: the gateway forwards the
    # session role as ``X-Egg-Role`` for audit / future use, but this
    # route never consults it (unlike contract mutations in
    # ``routes.contracts``, which role-validate writers).  Any
    # authenticated session may read any spec-registered artifact; the
    # strict no-path design — not a per-role allow-list — is the access
    # boundary here.
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("Request body must be a JSON object")

    # Schema-level rejection of the forbidden ``path`` field.  This is
    # the wire-level enforcement of HITL Q2: even if a future bug let
    # the gateway forward an extra field, the orchestrator side
    # refuses it.
    if "path" in body:
        return _error(
            "The 'path' field is not accepted by this endpoint. "
            "Pass a spec-registered 'name' instead.",
            status_code=400,
            details={"registered_names": sorted(spec.name for spec in all_specs())},
        )

    name = body.get("name")
    ref = body.get("ref")
    pipeline_id = body.get("pipeline_id")

    if not isinstance(name, str) or not name:
        return _error("Missing or invalid 'name' (string required)")
    if not isinstance(ref, str) or not ref:
        return _error("Missing or invalid 'ref' (string required)")
    if not isinstance(pipeline_id, str) or not pipeline_id:
        return _error("Missing or invalid 'pipeline_id' (string required)")

    if not _HEX_REF_RE.match(ref):
        return _error(
            f"Invalid 'ref' {ref!r}: must be 7-40 hex characters",
            status_code=400,
            details={"ref": ref},
        )

    try:
        spec = spec_by_name(name)
    except KeyError:
        return _error(
            f"Unknown artifact name {name!r}",
            status_code=400,
            details={"registered_names": sorted(s.name for s in all_specs())},
        )

    explicit_identifier = body.get("identifier")
    if explicit_identifier is not None and not isinstance(explicit_identifier, str | int):
        return _error("'identifier' must be a string or integer when provided")

    identifier, ident_error = _resolve_identifier(
        pipeline_id=pipeline_id,
        explicit_identifier=explicit_identifier,
    )
    if ident_error is not None:
        return ident_error
    assert identifier is not None  # mypy: above branch returns on None

    rel_path = resolve_artifact_path(name, identifier)

    repo_hint = body.get("repo") if isinstance(body.get("repo"), str) else None
    worktree, worktree_error = _resolve_worktree(
        pipeline_id=pipeline_id,
        repo_hint=repo_hint,
    )
    if worktree_error is not None:
        return worktree_error
    assert worktree is not None  # mypy

    payload, git_error = _run_git_show(worktree, ref, rel_path)
    if git_error is not None:
        return git_error
    assert payload is not None  # mypy

    content, truncated = _decode_with_cap(payload)

    logger.info(
        "artifact served",
        extra={
            "pipeline_id": pipeline_id,
            "artifact_name": name,
            "ref": ref,
            "path": rel_path,
            "bytes": len(payload),
            "truncated": truncated,
            "spec_phase": spec.phase,
            "spec_producer": spec.producer_role,
        },
    )

    return _success(
        "Artifact retrieved",
        data={
            "name": name,
            "ref": ref,
            "path": rel_path,
            "content": content,
            "truncated": truncated,
        },
    )

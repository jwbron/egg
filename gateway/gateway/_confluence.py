"""Gateway confluence cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import re
from typing import Any

from flask import Response, g, request

try:
    from ..confluence_client import (
        DEFAULT_LIMIT as CONFLUENCE_DEFAULT_LIMIT,
    )
    from ..confluence_client import (
        HARD_MAX_LIMIT as CONFLUENCE_HARD_MAX_LIMIT,
    )
    from ..confluence_client import (
        ConfluenceCredentialsUnavailable,
        ConfluenceResponseTooLarge,
        ConfluenceUpstreamError,
        ConfluenceUpstreamForbidden,
        redact_response,
        validate_confluence_api_path,
    )
    from ..confluence_search import (
        extract_search_spaces,
    )
except ImportError:  # flat/container import mode
    from confluence_client import (  # type: ignore[no-redef, import-untyped]
        DEFAULT_LIMIT as CONFLUENCE_DEFAULT_LIMIT,
    )
    from confluence_client import (  # type: ignore[no-redef, import-untyped]
        HARD_MAX_LIMIT as CONFLUENCE_HARD_MAX_LIMIT,
    )
    from confluence_client import (  # type: ignore[no-redef, import-untyped]
        ConfluenceCredentialsUnavailable,
        ConfluenceResponseTooLarge,
        ConfluenceUpstreamError,
        ConfluenceUpstreamForbidden,
        redact_response,
        validate_confluence_api_path,
    )
    from confluence_search import (  # type: ignore[no-redef, import-untyped]
        extract_search_spaces,
    )

from ._helpers import make_error, make_success


def _b() -> Any:
    """Return the gateway barrel for call-time lookup of patched symbols.

    Seam getters/validators and gateway-local helpers are patched by tests at
    ``gateway.gateway.<name>``; resolving them on the barrel at call time keeps
    those patches effective after the split.
    """
    import sys

    return sys.modules.get("gateway.gateway") or sys.modules["gateway"]


class _BarrelLogger:
    """Proxy to the barrel ``logger`` so tests patching ``gateway.logger``
    observe log calls emitted from this submodule."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_b().logger, name)


logger: Any = _BarrelLogger()

_CONFLUENCE_PAGE_ID_RE = re.compile(r"^\d+$")


_CONFLUENCE_SPACE_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _session_confluence_context() -> dict[str, Any]:
    """Return session-scoped fields to include in Confluence audit records.

    Per refine decision 13 there is no per-session ``session.confluence_*``
    field — pageId / spaceKey are recovered from the request body or
    response per call.
    """
    ctx: dict[str, Any] = {
        "session_mode": getattr(g, "session_mode", None),
    }
    session = getattr(g, "session", None)
    if session is not None:
        ctx["pipeline_id"] = getattr(session, "pipeline_id", None)
        ctx["agent_role"] = getattr(session, "agent_role", None)
    return ctx


def _confluence_error_from_upstream(exc: ConfluenceUpstreamError) -> tuple[Response, int]:
    """Translate a ``ConfluenceUpstreamError`` to an HTTP response.

    Atlassian error envelopes occasionally include user-identifying strings
    (e.g. account ids embedded in messages) and space-enumeration leaks
    (e.g. ``"valid keys are: ENG, DOCS, SECRET"``).  The success-path
    redactor only runs on 2xx bodies, so we apply it here too before the
    upstream body crosses the gateway/sandbox boundary.
    """
    if 300 <= exc.status_code < 400:
        # A 3xx is never a valid read response from the Atlassian REST API —
        # it's the signature of an unauthenticated/misrouted request being
        # bounced to the login page. The usual cause is a missing/invalid
        # gateway Atlassian token or a wrong base URL (e.g. ATLASSIAN_BASE_URL
        # set to a page browser URL, or CONFLUENCE_BASE_URL missing the
        # ``/wiki`` suffix). Surface that pointedly instead of an opaque 502 so
        # operators don't have to reverse-engineer the redirect. Still 502
        # (bad upstream response), distinct from the 503 "creds absent" path.
        message = (
            f"Confluence upstream returned {exc.status_code} (redirect) — the "
            "gateway received a login redirect instead of a REST response. "
            "This usually means the gateway's Atlassian credentials are "
            "missing/invalid or the base URL is wrong (e.g. ATLASSIAN_BASE_URL "
            "must be the bare tenant origin, or CONFLUENCE_BASE_URL must include "
            "the /wiki suffix)."
        )
        details: dict[str, Any] = {
            "upstream_status": exc.status_code,
            "upstream_body": _redact_upstream_error_body(exc.body),
            "path": exc.path,
            "likely_cause": "missing_or_invalid_atlassian_credentials_or_base_url",
        }
        # Surface the upstream ``Location`` when present so the operator can
        # confirm the bounce target (typically ``/login`` or the tenant root)
        # without reproducing.
        if exc.location:
            message += f" Upstream redirected to: {exc.location}"
            details["upstream_location"] = exc.location
        return make_error(message, status_code=502, details=details)
    if 400 <= exc.status_code < 500:
        status = exc.status_code
    else:
        status = 502
    return make_error(
        f"Confluence upstream error {exc.status_code}",
        status_code=status,
        details={
            "upstream_status": exc.status_code,
            "upstream_body": _redact_upstream_error_body(exc.body),
            "path": exc.path,
        },
    )


def _redact_upstream_error_body(body: Any) -> Any:
    """Run ``redact_response`` over an Atlassian error envelope.

    Atlassian returns errors as JSON dicts (and very occasionally as plain
    text); the redactor mutates dicts/lists in place.  Non-container shapes
    pass through unchanged.
    """
    if isinstance(body, (dict, list)):
        return redact_response(body)
    return body


def _confluence_not_configured_error(
    exc: ConfluenceCredentialsUnavailable,
) -> tuple[Response, int]:
    """Translate missing credentials to an HTTP 503 response."""
    return make_error(
        "Confluence credentials not configured on the gateway",
        status_code=503,
        details={"reason": str(exc)},
    )


def _confluence_response_too_large(
    exc: ConfluenceResponseTooLarge,
    *,
    page_id: str | None = None,
    space_key: str | None = None,
) -> tuple[Response, int]:
    """Translate an oversized response to HTTP 413."""
    details: dict[str, Any] = {"size_bytes": exc.size_bytes, "path": exc.path}
    if page_id is not None:
        details["pageId"] = page_id
    if space_key is not None:
        details["spaceKey"] = space_key
    return make_error(
        "Confluence response too large",
        status_code=413,
        details=details,
    )


def _confluence_forbidden_response(
    exc: ConfluenceUpstreamForbidden,
    *,
    event: str,
    page_id: str | None = None,
    space_key: str | None = None,
) -> tuple[Response, int]:
    """Translate an upstream 403 into HTTP 403 with the dedicated audit event."""
    details: dict[str, Any] = {
        "upstream_status": 403,
        "reason": "bot_account_lacks_read_access",
        "path": exc.path,
        **_session_confluence_context(),
    }
    if page_id is not None:
        details["pageId"] = page_id
    if space_key is not None:
        details["spaceKey"] = space_key
    _b().audit_log(event, event, success=False, details=details)
    body: dict[str, Any] = {
        "status": "forbidden",
        "reason": "bot_account_lacks_read_access",
    }
    if page_id is not None:
        body["pageId"] = page_id
    if space_key is not None:
        body["spaceKey"] = space_key
    return make_error(
        "Confluence upstream forbidden",
        status_code=403,
        details=body,
    )


def _confluence_space_denied_response(
    *,
    event: str,
    page_id: str | None,
    space_key: str | None,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Emit a structured audit record and return the canonical 403."""
    details: dict[str, Any] = {"spaceKey": space_key, "reason": reason}
    if page_id is not None:
        details["pageId"] = page_id
    if extra:
        details.update(extra)
    details.update(_session_confluence_context())
    _b().audit_log(event, event, success=False, details=details)
    return make_error(
        "Confluence space not allowlisted",
        status_code=403,
        details={"spaceKey": space_key, "reason": reason},
    )


def _resolve_space_key_for_payload(payload: Any) -> str | None:
    """Extract a ``spaceKey`` from an upstream payload, using the client's
    space cache if only ``spaceId`` is present.

    Returns the space key on success; ``None`` if the payload doesn't carry
    one (e.g. v1 fallback with no spaceId — caller falls back to a manual
    list_spaces lookup).
    """
    if not isinstance(payload, dict):
        return None
    direct = payload.get("spaceKey") or payload.get("space_key")
    if isinstance(direct, str) and direct:
        return direct
    # v2 returns ``spaceId`` on page reads; the client caches the mapping
    # opportunistically once ``list_spaces`` runs.
    space_id = payload.get("spaceId")
    if space_id is None:
        space = payload.get("space")
        if isinstance(space, dict):
            sk = space.get("key")
            if isinstance(sk, str) and sk:
                return sk
            space_id = space.get("id")
    if space_id is None:
        return None
    client = _b().get_confluence_client()
    return client.space_cache.key_for_id(str(space_id))


def _resolve_space_key_via_list(allowed: frozenset[str], space_id: str | None) -> str | None:
    """Look up a space key for a space id by warming the space cache.

    Used by the post-fetch allowlist check when the page response carries
    ``spaceId`` but the cache hasn't been populated yet.  Returns ``None``
    if the space isn't visible to the bot (which is itself a deny signal).

    ``allowed`` is unused at this layer; the cache is populated with every
    space the bot can see and the post-fetch allowlist check applies the
    operator allowlist on the resolved key.
    """
    del allowed  # cache holds every visible space; allowlist enforced upstream
    if not space_id:
        return None
    client = _b().get_confluence_client()
    cached = client.space_cache.key_for_id(str(space_id))
    if cached is not None:
        return cached
    # Walk paginated /wiki/api/v2/spaces so a target space on page 2+ still
    # resolves.  populate_space_cache caps iterations defensively.
    try:
        client.populate_space_cache()
    except (
        ConfluenceCredentialsUnavailable,
        ConfluenceUpstreamError,
        ConfluenceUpstreamForbidden,
    ):
        # Forbidden on /wiki/api/v2/spaces (bot lacks space:read globally)
        # is not its own ConfluenceUpstreamError subclass — catch it here
        # so the outer post-fetch check fail-closes through
        # confluence_space_denied rather than leaking a Flask 500.
        return None
    return client.space_cache.key_for_id(str(space_id))


def _confluence_clamp_limit(value: Any) -> int | None:
    """Coerce + clamp a caller-supplied limit (1..HARD_MAX_LIMIT)."""
    if value is None:
        return None
    try:
        parsed = int(value)
    except TypeError, ValueError:
        raise ValueError("limit must be an integer") from None
    if parsed <= 0:
        raise ValueError("limit must be positive")
    return min(parsed, CONFLUENCE_HARD_MAX_LIMIT)


def _validate_confluence_page_id(page_id: Any) -> tuple[bool, str]:
    if not isinstance(page_id, str) or not _CONFLUENCE_PAGE_ID_RE.fullmatch(page_id):
        return False, "invalid pageId shape"
    return True, ""


def _validate_confluence_space_key(space_key: Any) -> tuple[bool, str]:
    if not isinstance(space_key, str) or not _CONFLUENCE_SPACE_KEY_RE.fullmatch(space_key):
        return False, "invalid spaceKey shape"
    return True, ""


def _check_post_fetch_space_allowlist(
    payload: Any,
    *,
    allowed: frozenset[str],
    page_id: str | None,
) -> tuple[bool, str | None]:
    """Verify the response's spaceKey is in the allowlist.

    Returns ``(ok, space_key)``.  When ``ok`` is False the route returns
    HTTP 403 without forwarding the response body; ``space_key`` is the
    resolved key for audit purposes (may be ``None`` if unresolvable).
    """
    if not isinstance(payload, dict):
        return False, None
    if payload.get("status") == "not_found":
        # 404 envelope passes through — no space leakage.
        return True, None
    space_key = _resolve_space_key_for_payload(payload)
    if space_key is None:
        space_id = payload.get("spaceId")
        if isinstance(space_id, (str, int)):
            space_key = _resolve_space_key_via_list(allowed, str(space_id))
    if space_key is None:
        # Couldn't resolve — fail closed.  This protects against the upstream
        # response shape changing.
        return False, None
    return space_key in allowed, space_key


def confluence_page_get() -> tuple[Response, int] | Response:
    """Fetch a single Confluence page (v2).

    Request body::

        {"pageId": "12345",
         "bodyFormat": ["storage"],
         "expand": null}
    """
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    body_format = data.get("bodyFormat")
    expand = data.get("expand")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        _b().audit_log(
            "confluence_page_get_rejected",
            "confluence_page_get",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    allowed = _b().confluence_allowed_spaces()
    try:
        body = (
            _b().get_confluence_client().get_page(page_id, body_format=body_format, expand=expand)
        )
    except ValueError as exc:
        _b().audit_log(
            "confluence_page_get_rejected",
            "confluence_page_get",
            success=False,
            details={"reason": str(exc), "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        _b().audit_log(
            "confluence_response_too_large",
            "confluence_page_get",
            success=False,
            details={
                "pageId": page_id,
                "size_bytes": exc.size_bytes,
                **_session_confluence_context(),
            },
        )
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_page_get_upstream_error",
            "confluence_page_get",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    ok_space, space_key = _check_post_fetch_space_allowlist(body, allowed=allowed, page_id=page_id)
    if not ok_space:
        return _confluence_space_denied_response(
            event="confluence_space_denied",
            page_id=page_id,
            space_key=space_key,
            reason="space not allowlisted",
        )

    _b().audit_log(
        "confluence_page_get",
        "confluence_page_get",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": space_key,
            "not_found": body.get("status") == "not_found",
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence page fetched", body)


def confluence_page_descendants() -> tuple[Response, int] | Response:
    """List the descendants of a Confluence page."""
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    depth = data.get("depth")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        _b().audit_log(
            "confluence_page_descendants_rejected",
            "confluence_page_descendants",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    # Apply sensible defaults for runaway-tree protection (risk R8).
    if depth is None:
        depth = 1
    if limit_raw is None:
        limit_raw = CONFLUENCE_DEFAULT_LIMIT
    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        _b().audit_log(
            "confluence_page_descendants_rejected",
            "confluence_page_descendants",
            success=False,
            details={"reason": str(exc), "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = _b().confluence_allowed_spaces()
    try:
        body = (
            _b()
            .get_confluence_client()
            .get_page_descendants(
                page_id,
                depth=depth,
                limit=limit,
                cursor=cursor if isinstance(cursor, str) else None,
            )
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_page_descendants_upstream_error",
            "confluence_page_descendants",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    # Resolve the parent page's space for the allowlist check.  The
    # descendants response doesn't carry it directly, so we fetch the parent
    # page once (cheap — the v2 page endpoint is small).
    parent_space_key: str | None = None
    if body.get("status") != "not_found":
        try:
            parent = _b().get_confluence_client().get_page(page_id, body_format=("storage",))
        except (
            ConfluenceCredentialsUnavailable,
            ConfluenceUpstreamError,
            ConfluenceUpstreamForbidden,
        ):
            parent = None
        if parent is not None and parent.get("status") != "not_found":
            ok_space, parent_space_key = _check_post_fetch_space_allowlist(
                parent, allowed=allowed, page_id=page_id
            )
            if not ok_space:
                return _confluence_space_denied_response(
                    event="confluence_space_denied",
                    page_id=page_id,
                    space_key=parent_space_key,
                    reason="space not allowlisted",
                )
        else:
            return _confluence_space_denied_response(
                event="confluence_space_denied",
                page_id=page_id,
                space_key=None,
                reason="parent page space could not be resolved",
            )

    _b().audit_log(
        "confluence_page_descendants",
        "confluence_page_descendants",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": parent_space_key,
            "depth": depth,
            "limit": limit,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence descendants fetched", body)


def confluence_page_footer_comments() -> tuple[Response, int] | Response:
    """Fetch footer comments on a Confluence page."""
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    body_format = data.get("bodyFormat")
    include_replies = bool(data.get("includeReplies"))
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        _b().audit_log(
            "confluence_page_footer_comments_rejected",
            "confluence_page_footer_comments",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = _b().confluence_allowed_spaces()
    try:
        body = (
            _b()
            .get_confluence_client()
            .get_page_footer_comments(
                page_id,
                body_format=body_format,
                include_replies=include_replies,
                limit=limit,
                cursor=cursor if isinstance(cursor, str) else None,
            )
        )
    except ValueError as exc:
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_page_footer_comments_upstream_error",
            "confluence_page_footer_comments",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    parent_space_key: str | None = None
    if body.get("status") != "not_found":
        try:
            parent = _b().get_confluence_client().get_page(page_id, body_format=("storage",))
        except (
            ConfluenceCredentialsUnavailable,
            ConfluenceUpstreamError,
            ConfluenceUpstreamForbidden,
        ):
            parent = None
        if parent is not None and parent.get("status") != "not_found":
            ok_space, parent_space_key = _check_post_fetch_space_allowlist(
                parent, allowed=allowed, page_id=page_id
            )
            if not ok_space:
                return _confluence_space_denied_response(
                    event="confluence_space_denied",
                    page_id=page_id,
                    space_key=parent_space_key,
                    reason="space not allowlisted",
                )
        else:
            # Fail-closed when the parent page's space cannot be resolved
            # (parent fetch raised, or returned the not_found envelope while
            # the comment fetch returned data — Atlassian's per-page
            # restriction inheritance can produce exactly this shape).
            # We MUST NOT ship the comment body to the sandbox without an
            # allowlist verdict.
            return _confluence_space_denied_response(
                event="confluence_space_denied",
                page_id=page_id,
                space_key=None,
                reason="parent page space could not be resolved",
            )

    _b().audit_log(
        "confluence_page_footer_comments",
        "confluence_page_footer_comments",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": parent_space_key,
            "includeReplies": include_replies,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence footer comments fetched", body)


def confluence_page_inline_comments() -> tuple[Response, int] | Response:
    """Fetch inline comments on a Confluence page (with v1 fallback)."""
    data = request.get_json(silent=True) or {}
    page_id = data.get("pageId")
    body_format = data.get("bodyFormat")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    ok, reason = _validate_confluence_page_id(page_id)
    if not ok:
        _b().audit_log(
            "confluence_page_inline_comments_rejected",
            "confluence_page_inline_comments",
            success=False,
            details={"reason": reason, "pageId": page_id, **_session_confluence_context()},
        )
        return make_error(
            "Invalid pageId (expected numeric string)",
            status_code=400,
            details={"pageId": page_id},
        )
    assert isinstance(page_id, str)  # narrowed by _validate_confluence_page_id

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = _b().confluence_allowed_spaces()
    try:
        body = (
            _b()
            .get_confluence_client()
            .get_page_inline_comments(
                page_id,
                body_format=body_format,
                limit=limit,
                cursor=cursor if isinstance(cursor, str) else None,
            )
        )
    except ValueError as exc:
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_page_inline_comments_upstream_error",
            "confluence_page_inline_comments",
            success=False,
            details={
                "pageId": page_id,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    used_fallback = bool(body.get("used_fallback"))
    parent_space_key: str | None = None
    if body.get("status") != "not_found":
        try:
            parent = _b().get_confluence_client().get_page(page_id, body_format=("storage",))
        except (
            ConfluenceCredentialsUnavailable,
            ConfluenceUpstreamError,
            ConfluenceUpstreamForbidden,
        ):
            parent = None
        if parent is not None and parent.get("status") != "not_found":
            ok_space, parent_space_key = _check_post_fetch_space_allowlist(
                parent, allowed=allowed, page_id=page_id
            )
            if not ok_space:
                return _confluence_space_denied_response(
                    event="confluence_space_denied",
                    page_id=page_id,
                    space_key=parent_space_key,
                    reason="space not allowlisted",
                )
        else:
            # Fail-closed when the parent page's space cannot be resolved.
            # See confluence_page_footer_comments — same risk applies here:
            # the v1 fallback can return inline comments even when v2 page
            # reads 403, so we MUST NOT ship the body without an allowlist
            # verdict.
            return _confluence_space_denied_response(
                event="confluence_space_denied",
                page_id=page_id,
                space_key=None,
                reason="parent page space could not be resolved",
            )

    _b().audit_log(
        "confluence_page_inline_comments",
        "confluence_page_inline_comments",
        success=True,
        details={
            "pageId": page_id,
            "spaceKey": parent_space_key,
            "used_fallback": used_fallback,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence inline comments fetched", body)


def confluence_space_pages() -> tuple[Response, int] | Response:
    """List pages in a Confluence space."""
    data = request.get_json(silent=True) or {}
    space_key = data.get("spaceKey")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")
    body_format = data.get("bodyFormat")

    ok, reason = _validate_confluence_space_key(space_key)
    if not ok:
        _b().audit_log(
            "confluence_space_pages_rejected",
            "confluence_space_pages",
            success=False,
            details={"reason": reason, "spaceKey": space_key, **_session_confluence_context()},
        )
        return make_error(
            "Invalid spaceKey",
            status_code=400,
            details={"spaceKey": space_key},
        )
    assert isinstance(space_key, str)  # narrowed by _validate_confluence_space_key

    if not _b().is_confluence_space_allowed(space_key):
        return _confluence_space_denied_response(
            event="confluence_space_pages_denied",
            page_id=None,
            space_key=space_key,
            reason="space not allowlisted",
        )

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    client = _b().get_confluence_client()

    # Resolve spaceKey → spaceId, using the cache when populated.  Walk
    # paginated /wiki/api/v2/spaces so tenants with more spaces than fit on
    # one v2 page still resolve a target on page 2+.
    space_id = client.space_cache.id_for_key(space_key)
    if space_id is None:
        try:
            client.populate_space_cache()
        except ConfluenceCredentialsUnavailable as exc:
            return _confluence_not_configured_error(exc)
        except ConfluenceUpstreamForbidden as exc:
            return _confluence_forbidden_response(
                exc, event="confluence_upstream_403", space_key=space_key
            )
        except ConfluenceUpstreamError as exc:
            return _confluence_error_from_upstream(exc)
        space_id = client.space_cache.id_for_key(space_key)

    if space_id is None:
        return make_error(
            "Confluence space not found or not visible to bot account",
            status_code=404,
            details={"status": "not_found", "spaceKey": space_key},
        )

    try:
        body = client.get_space_pages(
            space_id,
            limit=limit,
            cursor=cursor if isinstance(cursor, str) else None,
            body_format=body_format,
        )
    except ValueError as exc:
        return make_error(f"Invalid request: {exc}", status_code=400)
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(
            exc, event="confluence_upstream_403", space_key=space_key
        )
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, space_key=space_key)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_space_pages_upstream_error",
            "confluence_space_pages",
            success=False,
            details={
                "spaceKey": space_key,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    _b().audit_log(
        "confluence_space_pages",
        "confluence_space_pages",
        success=True,
        details={
            "spaceKey": space_key,
            "limit": limit,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence space pages fetched", body)


def confluence_space_list() -> tuple[Response, int] | Response:
    """List Confluence spaces (filtered to the operator's allowlist)."""
    data = request.get_json(silent=True) or {}
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    allowed = _b().confluence_allowed_spaces()

    try:
        body = (
            _b()
            .get_confluence_client()
            .list_spaces(
                allowed_spaces=allowed,
                limit=limit,
                cursor=cursor if isinstance(cursor, str) else None,
            )
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403")
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_space_list_upstream_error",
            "confluence_space_list",
            success=False,
            details={
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    spaces_returned = 0
    if isinstance(body, dict):
        results = body.get("results")
        if isinstance(results, list):
            spaces_returned = len(results)

    _b().audit_log(
        "confluence_space_list",
        "confluence_space_list",
        success=True,
        details={
            "spaces_returned": spaces_returned,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence spaces fetched", body)


def confluence_search() -> tuple[Response, int] | Response:
    """Run a CQL search against Atlassian Cloud Confluence.

    Request body::

        {"cql": "space = ENG AND text ~ \"rfc\"",
         "limit": 50,
         "cursor": null}

    The CQL must be statically provable as scoped to allowlisted spaces.
    """
    data = request.get_json(silent=True) or {}
    cql = data.get("cql")
    limit_raw = data.get("limit")
    cursor = data.get("cursor")

    if not isinstance(cql, str) or not cql.strip():
        _b().audit_log(
            "confluence_search_rejected",
            "confluence_search",
            success=False,
            details={"reason": "cql required", **_session_confluence_context()},
        )
        return make_error("cql is required", status_code=400)

    allowed = _b().confluence_allowed_spaces()
    scope = extract_search_spaces(cql, allowed)
    if scope.spaces is None:
        _b().audit_log(
            "confluence_search_rejected",
            "confluence_search",
            success=False,
            details={
                "reason": scope.reason,
                "cql_length": len(cql),
                **_session_confluence_context(),
            },
        )
        return make_error(
            f"CQL rejected: {scope.reason}",
            status_code=403,
            details={"reason": scope.reason},
        )

    try:
        limit = _confluence_clamp_limit(limit_raw)
    except ValueError as exc:
        return make_error(f"Invalid limit: {exc}", status_code=400)

    try:
        body = (
            _b()
            .get_confluence_client()
            .search_cql(
                cql=cql,
                limit=limit,
                cursor=cursor if isinstance(cursor, str) else None,
            )
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403")
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_search_upstream_error",
            "confluence_search",
            success=False,
            details={
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    _b().audit_log(
        "confluence_search",
        "confluence_search",
        success=True,
        details={
            "spaces_extracted": sorted(scope.spaces),
            "cql_length": len(cql),
            "limit": limit,
            "cursor_present": bool(cursor),
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence search executed", body)


def confluence_execute() -> tuple[Response, int] | Response:
    """Generic read-only passthrough for whitelisted Confluence REST paths.

    Request body::

        {"method": "GET",
         "path": "api/v2/pages/12345",
         "query": {"body-format": "storage"},
         "body": null}
    """
    data = request.get_json(silent=True) or {}
    method = data.get("method") or "GET"
    path = data.get("path")
    query = data.get("query")
    req_body = data.get("body")

    if not isinstance(path, str) or not path:
        _b().audit_log(
            "confluence_execute_rejected",
            "confluence_execute",
            success=False,
            details={"reason": "path required", **_session_confluence_context()},
        )
        return make_error("path is required", status_code=400)

    if not isinstance(method, str):
        _b().audit_log(
            "confluence_execute_rejected",
            "confluence_execute",
            success=False,
            details={"reason": "method must be a string", **_session_confluence_context()},
        )
        return make_error("method must be a string", status_code=400)

    method_upper = method.upper()
    ok, reason = validate_confluence_api_path(path, method_upper)
    if not ok:
        _b().audit_log(
            "confluence_execute_denied",
            "confluence_execute",
            success=False,
            details={
                "method": method_upper,
                "path": path,
                "reason": reason,
                **_session_confluence_context(),
            },
        )
        return make_error(
            f"Confluence API call rejected: {reason}",
            status_code=403,
            details={"method": method_upper, "path": path, "reason": reason},
        )

    stripped = path.strip("/").split("?", 1)[0]
    head = stripped.split("/")
    page_id: str | None = None
    space_id_in_path: str | None = None
    if len(head) >= 4 and head[0] == "api" and head[1] == "v2" and head[2] == "pages":
        # api/v2/pages/<id>
        if head[3].isdigit():
            page_id = head[3]
    elif len(head) >= 5 and head[0] == "api" and head[1] == "v2" and head[2] == "spaces":
        # api/v2/spaces/<id>/pages
        if head[3].isdigit():
            space_id_in_path = head[3]

    # Anti-bypass invariant (issue #1931 cycle-3 NACK from reviewer_code +
    # reviewer_security): the four path families an attacker could use to
    # bypass narrow-route safeguards — ``rest/api/search`` (CQL extractor
    # bypass), ``api/v2/spaces`` (allowlist-filter bypass),
    # ``api/v2/footer-comments`` / ``api/v2/inline-comments`` (flat
    # endpoints with page-id-in-query and no upstream spaceKey filter) —
    # are dropped from CONFLUENCE_API_ALLOWED_PATHS in confluence_client.py,
    # so reaching this point implies a page- or space-scoped path family.
    # All of those carry an id inline that the post-fetch allowlist check
    # below resolves to a spaceKey.

    if query is not None and not isinstance(query, dict):
        return make_error("query must be an object", status_code=400)
    if req_body is not None and not isinstance(req_body, dict):
        return make_error("body must be an object", status_code=400)

    allowed = _b().confluence_allowed_spaces()
    client = _b().get_confluence_client()

    try:
        body = client.execute_raw(
            method=method_upper,
            path=stripped,
            query=query,
            body=req_body,
        )
    except ConfluenceCredentialsUnavailable as exc:
        return _confluence_not_configured_error(exc)
    except ConfluenceUpstreamForbidden as exc:
        return _confluence_forbidden_response(exc, event="confluence_upstream_403", page_id=page_id)
    except ConfluenceResponseTooLarge as exc:
        return _confluence_response_too_large(exc, page_id=page_id)
    except ConfluenceUpstreamError as exc:
        _b().audit_log(
            "confluence_execute_upstream_error",
            "confluence_execute",
            success=False,
            details={
                "method": method_upper,
                "path": stripped,
                "upstream_status": exc.status_code,
                **_session_confluence_context(),
            },
        )
        return _confluence_error_from_upstream(exc)

    # Post-fetch allowlist check for path families that carry an id inline.
    audited_space_key: str | None = None
    if page_id is not None and isinstance(body, dict) and body.get("status") != "not_found":
        ok_space, audited_space_key = _check_post_fetch_space_allowlist(
            body, allowed=allowed, page_id=page_id
        )
        if not ok_space:
            return _confluence_space_denied_response(
                event="confluence_execute_denied",
                page_id=page_id,
                space_key=audited_space_key,
                reason="space not allowlisted",
                extra={"method": method_upper, "path": stripped},
            )
    elif space_id_in_path is not None:
        resolved = client.space_cache.key_for_id(space_id_in_path)
        if resolved is None:
            # Walk paginated /wiki/api/v2/spaces so a target on page 2+
            # still resolves.  Catch ConfluenceUpstreamForbidden alongside
            # the other upstream errors — it's a sibling of
            # ConfluenceUpstreamError (both inherit from RuntimeError, not
            # one from the other) and would otherwise escape as a Flask
            # 500 when the bot lacks space:read globally.  Mirrors the
            # handler at _resolve_space_key_via_list.
            try:
                client.populate_space_cache()
            except (
                ConfluenceCredentialsUnavailable,
                ConfluenceUpstreamError,
                ConfluenceUpstreamForbidden,
            ):
                resolved = None
            else:
                resolved = client.space_cache.key_for_id(space_id_in_path)
        if resolved is None or resolved not in allowed:
            return _confluence_space_denied_response(
                event="confluence_execute_denied",
                page_id=None,
                space_key=resolved,
                reason="space not allowlisted",
                extra={"method": method_upper, "path": stripped},
            )
        audited_space_key = resolved

    _b().audit_log(
        "confluence_execute",
        "confluence_execute",
        success=True,
        details={
            "method": method_upper,
            "path": stripped,
            "pageId": page_id,
            "spaceKey": audited_space_key,
            **_session_confluence_context(),
        },
    )
    return make_success("Confluence API call executed", body)

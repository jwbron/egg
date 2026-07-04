"""Gateway jira cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from flask import Response, g, request

try:
    from ..jira_client import (
        JiraCredentialsUnavailable,
        JiraUpstreamError,
        validate_jira_api_path,
    )
    from ..jira_client import (
        validate_fields as validate_jira_fields,
    )
    from ..jira_policy import (
        extract_project_key,
    )
    from ..jira_search import (
        extract_search_projects,
    )
except ImportError:  # flat/container import mode
    from jira_client import (  # type: ignore[no-redef, import-untyped]
        JiraCredentialsUnavailable,
        JiraUpstreamError,
        validate_jira_api_path,
    )
    from jira_client import (  # type: ignore[no-redef, import-untyped]
        validate_fields as validate_jira_fields,
    )
    from jira_policy import (  # type: ignore[no-redef, import-untyped]
        extract_project_key,
    )
    from jira_search import (  # type: ignore[no-redef, import-untyped]
        extract_search_projects,
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

_JIRA_TICKET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


_JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _session_jira_context() -> dict[str, Any]:
    """Return session-scoped fields to include in Jira audit records.

    Pipeline ID, agent role, and the new ``jira_ticket`` are observational
    — they aren't used as policy gates (the project allowlist is the only
    hard boundary — refine decision #9) but they make the audit trail
    self-describing.
    """
    ctx: dict[str, Any] = {
        "session_mode": getattr(g, "session_mode", None),
    }
    session = getattr(g, "session", None)
    if session is not None:
        ctx["pipeline_id"] = getattr(session, "pipeline_id", None)
        ctx["agent_role"] = getattr(session, "agent_role", None)
        ctx["jira_ticket"] = getattr(session, "jira_ticket", None)
    return ctx


def _jira_error_from_upstream(exc: JiraUpstreamError) -> tuple[Response, int]:
    """Translate a ``JiraUpstreamError`` to an HTTP response.

    Atlassian status codes in the 4xx range are passed through so the agent
    sees the real reason; 5xx upstream errors collapse to a 502 with the
    raw body in the audit trail.
    """
    if 400 <= exc.status_code < 500:
        status = exc.status_code
    else:
        status = 502
    return make_error(
        f"Jira upstream error {exc.status_code}",
        status_code=status,
        details={
            "upstream_status": exc.status_code,
            "upstream_body": exc.body,
            "path": exc.path,
        },
    )


def _jira_not_configured_error(exc: JiraCredentialsUnavailable) -> tuple[Response, int]:
    """Translate missing credentials to an HTTP 503 response."""
    return make_error(
        "Jira credentials not configured on the gateway",
        status_code=503,
        details={"reason": str(exc)},
    )


def _project_not_allowlisted_response(
    *,
    event: str,
    ticket: str | None,
    project: str | None,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> tuple[Response, int]:
    """Emit a structured audit record and return the canonical 403."""
    details: dict[str, Any] = {"project": project, "reason": reason}
    if ticket is not None:
        details["ticket"] = ticket
    if extra:
        details.update(extra)
    details.update(_session_jira_context())
    _b().audit_log(event, event, success=False, details=details)
    return make_error(
        "Jira project not allowlisted",
        status_code=403,
        details={"project": project, "reason": reason},
    )


def jira_ticket_get() -> tuple[Response, int] | Response:
    """Fetch a single Jira issue.

    Request body::

        {"ticket": "FOO-123", "fields": ["summary", "status"]}

    ``fields`` is optional; when omitted, Atlassian returns the default field
    set.  ``expand`` defaults to ``renderedBody,renderedFields`` in the
    client so agents receive both ADF and rendered HTML.
    """
    data = request.get_json(silent=True) or {}
    ticket = data.get("ticket")
    fields = data.get("fields")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        _b().audit_log(
            "jira_ticket_get_rejected",
            "jira_ticket_get",
            success=False,
            details={"reason": "invalid ticket shape", "ticket": ticket, **_session_jira_context()},
        )
        return make_error(
            "Invalid ticket key (expected e.g. 'FOO-123')",
            status_code=400,
            details={"ticket": ticket},
        )

    project = extract_project_key(ticket)
    if not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_ticket_get_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    try:
        cleaned_fields = validate_jira_fields(fields)
    except ValueError as exc:
        _b().audit_log(
            "jira_ticket_get_rejected",
            "jira_ticket_get",
            success=False,
            details={"reason": str(exc), "ticket": ticket, **_session_jira_context()},
        )
        return make_error(f"Invalid fields: {exc}", status_code=400)

    try:
        body = _b().get_jira_client().get_ticket(ticket, cleaned_fields or None)
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            "jira_ticket_get_upstream_error",
            "jira_ticket_get",
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        "jira_ticket_get",
        "jira_ticket_get",
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "not_found": body.get("status") == "not_found",
            **_session_jira_context(),
        },
    )
    return make_success("Jira ticket fetched", body)


def jira_search() -> tuple[Response, int] | Response:
    """Run a JQL query against Atlassian Cloud.

    Request body::

        {"jql": "project = ENG AND status = Open",
         "fields": [...],
         "nextPageToken": "...",
         "maxResults": 50}

    The JQL must be statically provable as scoped to allowlisted projects.
    See ``gateway/jira_search.py`` for the exact acceptance rules.
    """
    data = request.get_json(silent=True) or {}
    jql = data.get("jql")
    fields = data.get("fields")
    next_page_token = data.get("nextPageToken")
    max_results = data.get("maxResults")

    if not isinstance(jql, str) or not jql.strip():
        _b().audit_log(
            "jira_search_rejected",
            "jira_search",
            success=False,
            details={"reason": "jql required", **_session_jira_context()},
        )
        return make_error("jql is required", status_code=400)

    # Import allowlist lazily because ``allowed_projects`` resolves the
    # policy singleton on first access.  Getting the frozenset once per
    # request keeps the mtime check out of the hot path for tests that
    # monkeypatch ``is_project_allowed`` directly.
    try:
        from ..jira_policy import allowed_projects
    except ImportError:
        from jira_policy import allowed_projects  # type: ignore[no-redef]
    allowed = allowed_projects()

    scope = extract_search_projects(jql, allowed)
    if scope.projects is None:
        _b().audit_log(
            "jira_search_rejected",
            "jira_search",
            success=False,
            details={
                "reason": scope.reason,
                "jql_length": len(jql),
                **_session_jira_context(),
            },
        )
        return make_error(
            f"JQL rejected: {scope.reason}",
            status_code=403,
            details={"reason": scope.reason},
        )

    try:
        cleaned_fields = validate_jira_fields(fields)
    except ValueError as exc:
        _b().audit_log(
            "jira_search_rejected",
            "jira_search",
            success=False,
            details={"reason": str(exc), **_session_jira_context()},
        )
        return make_error(f"Invalid fields: {exc}", status_code=400)

    # Normalise max_results: accept an int or a string-that-parses.  Missing
    # / invalid falls back to the client-side default (50, capped at 100).
    effective_max: int | None = None
    if max_results is not None:
        try:
            effective_max = max(1, min(int(max_results), 100))
        except TypeError, ValueError:
            _b().audit_log(
                "jira_search_rejected",
                "jira_search",
                success=False,
                details={
                    "reason": "maxResults must be an integer",
                    **_session_jira_context(),
                },
            )
            return make_error("maxResults must be an integer", status_code=400)

    try:
        body = (
            _b()
            .get_jira_client()
            .search(
                jql=jql,
                fields=cleaned_fields or None,
                next_page_token=next_page_token if isinstance(next_page_token, str) else None,
                max_results=effective_max,
            )
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            "jira_search_upstream_error",
            "jira_search",
            success=False,
            details={
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        "jira_search",
        "jira_search",
        success=True,
        details={
            "projects_extracted": sorted(scope.projects),
            "jql_length": len(jql),
            "max_results": effective_max,
            "next_page_token_present": bool(next_page_token),
            **_session_jira_context(),
        },
    )
    return make_success("Jira search executed", body)


def jira_ticket_comments() -> tuple[Response, int] | Response:
    """Fetch comments for a Jira issue."""
    data = request.get_json(silent=True) or {}
    ticket = data.get("ticket")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        _b().audit_log(
            "jira_ticket_comments_rejected",
            "jira_ticket_comments",
            success=False,
            details={"reason": "invalid ticket shape", "ticket": ticket, **_session_jira_context()},
        )
        return make_error(
            "Invalid ticket key (expected e.g. 'FOO-123')",
            status_code=400,
            details={"ticket": ticket},
        )

    project = extract_project_key(ticket)
    if not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_ticket_comments_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    try:
        body = _b().get_jira_client().get_comments(ticket)
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            "jira_ticket_comments_upstream_error",
            "jira_ticket_comments",
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        "jira_ticket_comments",
        "jira_ticket_comments",
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "not_found": body.get("status") == "not_found",
            **_session_jira_context(),
        },
    )
    return make_success("Jira ticket comments fetched", body)


def jira_ticket_remotelinks() -> tuple[Response, int] | Response:
    """Fetch the remote-link list for a Jira issue (issue #1557 slice-2).

    Request body::

        {"ticket": "FOO-123"}

    Read-only — wraps the Atlassian ``GET /rest/api/3/issue/{key}/
    remotelink`` endpoint. Used by the orchestrator's reassess
    sweep's in-flight classifier (decision-7 signal b) and the
    sandbox ``jira ticket remotelinks <KEY>`` CLI subcommand to
    catch human-opened PRs that the orchestrator's reverse-index
    doesn't track. Inherits the same project-allowlist boundary as
    every other Jira route — ``JIRA_WRITE_VERBS_DENIED`` and
    ``validate_jira_api_path`` keep the path GET-only.
    """
    data = request.get_json(silent=True) or {}
    ticket = data.get("ticket")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        _b().audit_log(
            "jira_ticket_remotelinks_rejected",
            "jira_ticket_remotelinks",
            success=False,
            details={
                "reason": "invalid ticket shape",
                "ticket": ticket,
                **_session_jira_context(),
            },
        )
        return make_error(
            "Invalid ticket key (expected e.g. 'FOO-123')",
            status_code=400,
            details={"ticket": ticket},
        )

    project = extract_project_key(ticket)
    if not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_ticket_remotelinks_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    try:
        body = _b().get_jira_client().get_remotelinks(ticket)
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            "jira_ticket_remotelinks_upstream_error",
            "jira_ticket_remotelinks",
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        "jira_ticket_remotelinks",
        "jira_ticket_remotelinks",
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "not_found": body.get("status") == "not_found",
            "remotelink_count": len(body.get("remotelinks") or [])
            if isinstance(body.get("remotelinks"), list)
            else 0,
            **_session_jira_context(),
        },
    )
    return make_success("Jira remote links fetched", body)


_TRANSITION_ALLOWLIST: frozenset[str] = frozenset(
    {name.lower() for name in ("Won't Do", "Won't Fix", "Wontfix")}
)


def _verify_orchestrator_transition_auth() -> tuple[bool, str]:
    """Verify the caller of ``/api/v1/jira/ticket/transition`` is the
    orchestrator (issue #1557 task-2-6).

    Two-factor check:
      1. ``Authorization: Bearer <launcher_secret>`` must validate
         against the gateway's launcher secret. Note: sandbox pods
         ALSO mount the launcher secret (it backs the standard
         session-creation flow), so the bearer alone does not
         distinguish orchestrator from sandbox — the loopback /
         in-cluster check plus NetworkPolicy on the gateway pod
         provides that scoping. See ``docs/architecture/
         orchestrator.md`` § "Trust model" for the full discussion.
      2. The request must originate from a loopback / in-cluster
         source. We accept any caller whose source IP equals the
         orchestrator's gateway-side IP, the loopback addresses
         (``127.0.0.1`` / ``::1``), or anything in the cluster pod
         subnet. This is a coarse RFC1918 check — it excludes
         external traffic but does not by itself distinguish
         orchestrator pods from sandbox pods. Without NetworkPolicy
         restricting ``/transition`` ingress to the orchestrator's
         pod selector, the launcher secret is the only remaining
         barrier between a compromised sandbox and this route.

    Returns ``(ok, reason)``.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False, "missing_bearer_auth"
    presented = auth_header[len("Bearer ") :]
    try:
        launcher_secret = _b().get_launcher_secret()
    except _b().LauncherSecretNotConfiguredError:
        return False, "launcher_secret_not_configured"
    if not launcher_secret or not secrets.compare_digest(presented, launcher_secret):
        return False, "bad_bearer_auth"

    # Loopback / in-cluster source check. ``request.remote_addr`` is
    # the immediate peer; for in-cluster traffic this is the
    # orchestrator pod IP. We accept anything from RFC1918 / IPv6
    # link-local / loopback so the orchestrator can reach us via any
    # ingress-side path (k3s NodePort, direct service IP, …). Public
    # IPs are rejected.
    remote_addr = request.remote_addr or ""
    if not _is_in_cluster_source(remote_addr):
        return False, "source_not_in_cluster"

    return True, ""


def _is_in_cluster_source(remote_addr: str) -> bool:
    """Return True if ``remote_addr`` is a loopback / RFC1918 address."""
    if not remote_addr:
        return False
    try:
        import ipaddress

        ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.is_private:
        return True
    if ip.is_link_local:
        return True
    return False


def jira_ticket_transition() -> tuple[Response, int] | Response:
    """Transition a Jira issue (issue #1557 slice-2 task-2-6).

    **Orchestrator-only**. The agent-facing Jira surface continues to
    deny transitions via ``JIRA_WRITE_VERBS_DENIED`` — this route
    bypasses the agent path entirely. Auth is a two-factor check:
    a launcher-secret bearer token AND a loopback / in-cluster
    source IP. Transition names are restricted to the allowlist
    (``Won't Do`` / ``Won't Fix``) — anything else returns 400.

    Request body::

        {"ticket": "FOO-123",
         "transition_name": "Won't Do",
         "comment": "Consolidated into FOO-200"}

    Returns ``200 OK`` on success with the upstream status code in
    the response body. Audit log entry covers caller IP, transition
    name, ticket key, and outcome.
    """
    ok, reason = _verify_orchestrator_transition_auth()
    if not ok:
        _b().audit_log(
            "jira_ticket_transition_unauthorized",
            "jira_ticket_transition",
            success=False,
            details={
                "reason": reason,
                "remote_addr": request.remote_addr,
            },
        )
        return make_error(
            "Unauthorized — orchestrator-only route",
            status_code=401 if reason != "source_not_in_cluster" else 403,
            details={"reason": reason},
        )

    data = request.get_json(silent=True) or {}
    ticket = data.get("ticket")
    transition_name = data.get("transition_name")
    comment_text = data.get("comment")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        _b().audit_log(
            "jira_ticket_transition_rejected",
            "jira_ticket_transition",
            success=False,
            details={
                "reason": "invalid ticket shape",
                "ticket": ticket,
            },
        )
        return make_error(
            "Invalid ticket key (expected e.g. 'FOO-123')",
            status_code=400,
            details={"ticket": ticket},
        )

    if not isinstance(transition_name, str) or not transition_name.strip():
        return make_error(
            "transition_name is required",
            status_code=400,
            details={"reason": "missing_transition_name"},
        )
    if transition_name.strip().lower() not in _TRANSITION_ALLOWLIST:
        _b().audit_log(
            "jira_ticket_transition_denied",
            "jira_ticket_transition",
            success=False,
            details={
                "reason": "transition_not_allowlisted",
                "transition_name": transition_name,
                "ticket": ticket,
            },
        )
        return make_error(
            f"transition_name {transition_name!r} is not on the allowlist",
            status_code=400,
            details={
                "reason": "transition_not_allowlisted",
                "allowed": sorted(_TRANSITION_ALLOWLIST),
            },
        )

    project = extract_project_key(ticket)
    if not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_ticket_transition_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    comment_adf: dict[str, Any] | None = None
    if isinstance(comment_text, str) and comment_text.strip():
        try:
            from ..jira_adf import wrap_text_as_adf
        except ImportError:
            # Issue #1557 tester v1 lint finding: ``jira_adf`` ships
            # without a ``py.typed`` marker so mypy reports it as
            # ``import-untyped``. The companion import at line 5849
            # already uses the dual-ignore; mirror it here.
            from jira_adf import wrap_text_as_adf  # type: ignore[no-redef, import-untyped]
        comment_adf = wrap_text_as_adf(comment_text.strip())

    try:
        status_code, body = (
            _b()
            .get_jira_client()
            .transition_issue(
                ticket,
                transition_name=transition_name.strip(),
                comment_adf=comment_adf,
            )
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            "jira_ticket_transition_upstream_error",
            "jira_ticket_transition",
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "transition_name": transition_name,
                "upstream_status": exc.status_code,
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        "jira_ticket_transition",
        "jira_ticket_transition",
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "transition_name": transition_name,
            "upstream_status": status_code,
            "comment_attached": bool(comment_adf),
            "remote_addr": request.remote_addr,
        },
    )
    return make_success(
        "Jira ticket transitioned",
        {"upstream_status": status_code, "body": body},
    )


def jira_execute() -> tuple[Response, int] | Response:
    """Generic read-only passthrough for whitelisted Jira REST paths.

    Request body::

        {"method": "GET",
         "path": "issue/FOO-123",
         "query": {"fields": "summary"},
         "body": null}

    Only methods + paths accepted by ``validate_jira_api_path`` are allowed.
    Write verbs (DELETE/PUT/PATCH) and path fragments listed in
    ``JIRA_WRITE_VERBS_DENIED`` are refused unconditionally.
    """
    data = request.get_json(silent=True) or {}
    method = data.get("method") or "GET"
    path = data.get("path")
    query = data.get("query")
    req_body = data.get("body")

    if not isinstance(path, str) or not path:
        _b().audit_log(
            "jira_execute_rejected",
            "jira_execute",
            success=False,
            details={"reason": "path required", **_session_jira_context()},
        )
        return make_error("path is required", status_code=400)

    if not isinstance(method, str):
        _b().audit_log(
            "jira_execute_rejected",
            "jira_execute",
            success=False,
            details={"reason": "method must be a string", **_session_jira_context()},
        )
        return make_error("method must be a string", status_code=400)

    method_upper = method.upper()
    ok, reason = validate_jira_api_path(path, method_upper)
    if not ok:
        _b().audit_log(
            "jira_execute_denied",
            "jira_execute",
            success=False,
            details={
                "method": method_upper,
                "path": path,
                "reason": reason,
                **_session_jira_context(),
            },
        )
        return make_error(
            f"Jira API call rejected: {reason}",
            status_code=403,
            details={"method": method_upper, "path": path, "reason": reason},
        )

    # Path is structurally OK — extract project key (if any) and allowlist it.
    # The accepted shapes are ``issue/<KEY>[/comment]`` and
    # ``project/<KEY>``.  Both carry a project key inline that is checked
    # against the allowlist.  Bare ``project`` is excluded (would leak all
    # projects visible to the API token).
    stripped = path.strip("/").split("?", 1)[0]
    ticket: str | None = None
    project: str | None = None
    head = stripped.split("/")
    if head and head[0] == "issue" and len(head) >= 2:
        ticket = head[1]
        project = extract_project_key(ticket)
    elif head and head[0] == "project" and len(head) >= 2:
        project = head[1]

    if project is not None and not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event="jira_execute_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
            extra={"method": method_upper, "path": path},
        )

    # Normalise query & body — they must be dicts or None.
    if query is not None and not isinstance(query, dict):
        return make_error("query must be an object", status_code=400)
    if req_body is not None and not isinstance(req_body, dict):
        return make_error("body must be an object", status_code=400)

    try:
        body = (
            _b()
            .get_jira_client()
            .execute_raw(
                method=method_upper,
                path=stripped,
                query=query,
                body=req_body,
            )
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            "jira_execute_upstream_error",
            "jira_execute",
            success=False,
            details={
                "method": method_upper,
                "path": stripped,
                "upstream_status": exc.status_code,
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        "jira_execute",
        "jira_execute",
        success=True,
        details={
            "method": method_upper,
            "path": stripped,
            "project": project,
            "ticket": ticket,
            **_session_jira_context(),
        },
    )
    return make_success("Jira API call executed", body)

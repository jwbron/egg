"""Gateway jira_writes cluster (#3312 slice-3 extraction from gateway.py).

Pure refactor: handler/helper bodies are AST-identical to the pre-split
gateway.py. Route @app.route decorators stay on thin wrappers in the barrel
(gateway/gateway/__init__.py); this module holds their implementations, and
the barrel re-exports every symbol here so gateway.gateway.<name> resolves.
"""

from __future__ import annotations

import json
from typing import Any

from flask import Response, request

try:
    from ..jira_client import (
        JiraCredentialsUnavailable,
        JiraUpstreamError,
    )
    from ..jira_policy import (
        extract_project_key,
    )
except ImportError:  # flat/container import mode
    from jira_client import (  # type: ignore[no-redef, import-untyped]
        JiraCredentialsUnavailable,
        JiraUpstreamError,
    )
    from jira_policy import (  # type: ignore[no-redef, import-untyped]
        extract_project_key,
    )

from ._helpers import make_error, make_success
from ._jira import (
    _JIRA_PROJECT_KEY_RE,
    _JIRA_TICKET_KEY_RE,
    _jira_error_from_upstream,
    _jira_not_configured_error,
    _project_not_allowlisted_response,
    _session_jira_context,
)


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

_JIRA_SUMMARY_MAX_CHARS: int = 255


_JIRA_BODY_MAX_CHARS: int = 32 * 1024


_JIRA_LABELS_MAX_COUNT: int = 30


_JIRA_LABEL_MAX_CHARS: int = 50


_JIRA_CREATE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "project",
        "issuetype",
        "summary",
        "description",
        "labels",
        "parent",
        "epicLink",
        "idempotencyKey",
    }
)


_JIRA_EDIT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "ticket",
        "summary",
        "description",
        "labels",
        "addLabels",
        "removeLabels",
        "notifyUsers",
    }
)


_JIRA_COMMENT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "ticket",
        "body",
        "idempotencyKey",
    }
)


_JIRA_LINK_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "type",
        "inwardIssue",
        "outwardIssue",
        "comment",
        "idempotencyKey",
    }
)


_JIRA_ALLOWED_ISSUETYPE_NAMES: frozenset[str] = frozenset(
    {"Task", "Story", "Bug", "Epic", "Sub-task", "Subtask"}
)


def _jira_write_audit_meta(body: dict[str, Any]) -> dict[str, Any]:
    """Return structural metadata for a write-verb audit record.

    Logs **field names changed**, **content lengths**, **label values**, and
    **link-type names** (refine feedback Q5) — never raw body content.
    """
    meta: dict[str, Any] = {}
    fields_present: list[str] = []
    for key in (
        "summary",
        "description",
        "labels",
        "addLabels",
        "removeLabels",
        "parent",
        "epicLink",
        "issuetype",
        "project",
        "ticket",
        "body",
        "comment",
        "type",
        "inwardIssue",
        "outwardIssue",
    ):
        if key in body:
            fields_present.append(key)
    if fields_present:
        meta["fields_present"] = fields_present

    summary = body.get("summary")
    if isinstance(summary, str):
        meta["summary_length"] = len(summary)

    description = body.get("description")
    if isinstance(description, str):
        meta["description_length"] = len(description)
    elif isinstance(description, dict):
        meta["description_length"] = -1  # ADF passthrough; length unknown
        meta["description_kind"] = "adf"

    comment_body = body.get("body")
    if isinstance(comment_body, str):
        meta["body_length"] = len(comment_body)
    elif isinstance(comment_body, dict):
        meta["body_length"] = -1
        meta["body_kind"] = "adf"

    labels = body.get("labels")
    if isinstance(labels, list):
        meta["labels"] = [v for v in labels if isinstance(v, str)]
    add_labels = body.get("addLabels")
    if isinstance(add_labels, list):
        meta["add_labels"] = [v for v in add_labels if isinstance(v, str)]
    remove_labels = body.get("removeLabels")
    if isinstance(remove_labels, list):
        meta["remove_labels"] = [v for v in remove_labels if isinstance(v, str)]

    link_type = body.get("type")
    if isinstance(link_type, str):
        meta["link_type"] = link_type

    issuetype = body.get("issuetype")
    if isinstance(issuetype, dict):
        if isinstance(issuetype.get("name"), str):
            meta["issuetype_name"] = issuetype["name"]
        if isinstance(issuetype.get("id"), str):
            meta["issuetype_id"] = issuetype["id"]
    elif isinstance(issuetype, str):
        meta["issuetype_name"] = issuetype

    return meta


def _validate_jira_write_keys(
    body: dict[str, Any], allowed: frozenset[str], operation: str
) -> tuple[Response, int] | None:
    """Reject unknown / suspect top-level body keys.

    Returns a 400 response when an unknown key is found (custom-field
    smuggling, ``method``-tunnel attempts, or typos), otherwise ``None``.
    """
    extras = sorted(set(body) - allowed)
    if not extras:
        return None
    _b().audit_log(
        f"{operation}_rejected",
        operation,
        success=False,
        details={
            "reason": "unknown_body_keys",
            "unknown_keys": extras,
            **_session_jira_context(),
        },
    )
    return make_error(
        f"Unknown body keys: {extras}",
        status_code=400,
        details={"unknown_keys": extras},
    )


def _validate_jira_text_field(
    value: Any,
    *,
    field: str,
    max_chars: int,
    allow_adf: bool = False,
) -> tuple[str | dict[str, Any] | None, tuple[Response, int] | None]:
    """Validate a string-or-ADF text field.

    Returns ``(cleaned_value, None)`` on success or
    ``(None, error_response)`` on failure.  ``None`` is treated as "not
    supplied"; callers handle the optional vs required distinction.
    """
    if value is None:
        return None, None

    if allow_adf and isinstance(value, dict):
        # ADF dict — ensure it's structurally valid; size cap applied to
        # serialised length so a malicious nested ADF tree can't hide.
        try:
            from ..jira_adf import is_adf_dict
        except ImportError:
            from jira_adf import is_adf_dict  # type: ignore[no-redef, import-untyped]
        if not is_adf_dict(value):
            return None, make_error(
                f"{field} must be a string or a valid ADF document",
                status_code=400,
            )
        # Size check via serialised length as a proxy.
        serialised = json.dumps(value)
        if len(serialised) > max_chars:
            return None, make_error(
                f"{field} exceeds maximum length ({max_chars} chars)",
                status_code=400,
            )
        return value, None

    if not isinstance(value, str):
        return None, make_error(f"{field} must be a string", status_code=400)
    if len(value) > max_chars:
        return None, make_error(
            f"{field} exceeds maximum length ({max_chars} chars)",
            status_code=400,
        )
    return value, None


def _validate_jira_labels(
    value: Any, *, field: str
) -> tuple[list[str] | None, tuple[Response, int] | None]:
    """Validate a labels list (count cap + per-entry length cap)."""
    if value is None:
        return None, None
    if not isinstance(value, list):
        return None, make_error(f"{field} must be a list", status_code=400)
    if len(value) > _JIRA_LABELS_MAX_COUNT:
        return None, make_error(
            f"{field} exceeds maximum of {_JIRA_LABELS_MAX_COUNT} entries",
            status_code=400,
        )
    cleaned: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            return None, make_error(f"{field} entries must be strings", status_code=400)
        if not entry:
            return None, make_error(f"{field} entries must be non-empty", status_code=400)
        if len(entry) > _JIRA_LABEL_MAX_CHARS:
            return None, make_error(
                f"{field} entry exceeds maximum length ({_JIRA_LABEL_MAX_CHARS} chars)",
                status_code=400,
            )
        if " " in entry:
            return None, make_error(
                f"{field} entries must not contain whitespace",
                status_code=400,
            )
        cleaned.append(entry)
    return cleaned, None


def jira_ticket_create() -> tuple[Response, int] | Response:
    """Create a Jira issue via ``POST /rest/api/3/issue``.

    Request body::

        {"project": "ENG",
         "issuetype": "Task" | {"name": "Task"} | {"id": "10001"},
         "summary": "...",
         "description": "..." | <ADF dict> | null,
         "labels": ["foo", "bar"],
         "parent": "ENG-1" | null,
         "epicLink": "ENG-2" | null,
         "idempotencyKey": "..." | null}

    ``parent`` and ``epicLink`` are mutually exclusive.  Cross-project
    parents are rejected (refine decision-17).  ``epicLink`` dispatches via
    ``JiraPolicy.epic_link_field`` (``parent`` or ``customfield_10014``).
    """
    operation = "jira_ticket_create"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    err = _validate_jira_write_keys(data, _JIRA_CREATE_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    project = data.get("project")
    issuetype = data.get("issuetype")
    summary = data.get("summary")
    description = data.get("description")
    labels = data.get("labels")
    parent = data.get("parent")
    epic_link = data.get("epicLink")
    idempotency_key = data.get("idempotencyKey")

    if not isinstance(project, str) or not _JIRA_PROJECT_KEY_RE.fullmatch(project):
        _b().audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "invalid project shape", **_session_jira_context()},
        )
        return make_error("Invalid project key", status_code=400)

    if not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event=f"{operation}_denied",
            ticket=None,
            project=project,
            reason="project not allowlisted",
        )

    # issuetype: name or numeric id (refine decision-8).
    if isinstance(issuetype, str):
        if issuetype not in _JIRA_ALLOWED_ISSUETYPE_NAMES:
            _b().audit_log(
                f"{operation}_rejected",
                operation,
                success=False,
                details={
                    "reason": "unknown issuetype",
                    "issuetype": issuetype,
                    **_session_jira_context(),
                },
            )
            return make_error(
                f"Unknown issuetype name: {issuetype!r}",
                status_code=400,
            )
        issuetype_arg: dict[str, Any] | str = issuetype
    elif isinstance(issuetype, dict):
        if "name" in issuetype:
            name = issuetype["name"]
            if not isinstance(name, str) or name not in _JIRA_ALLOWED_ISSUETYPE_NAMES:
                _b().audit_log(
                    f"{operation}_rejected",
                    operation,
                    success=False,
                    details={"reason": "unknown issuetype name", **_session_jira_context()},
                )
                return make_error(f"Unknown issuetype name: {name!r}", status_code=400)
            issuetype_arg = {"name": name}
        elif "id" in issuetype:
            type_id = issuetype["id"]
            if not isinstance(type_id, str) or not type_id.isdigit():
                return make_error("issuetype.id must be a numeric string", status_code=400)
            issuetype_arg = {"id": type_id}
        else:
            return make_error("issuetype must include name or id", status_code=400)
    else:
        return make_error(
            "issuetype must be a string, or a dict with 'name' or 'id'",
            status_code=400,
        )

    if not isinstance(summary, str) or not summary.strip():
        return make_error("summary is required", status_code=400)
    if len(summary) > _JIRA_SUMMARY_MAX_CHARS:
        return make_error(
            f"summary exceeds maximum length ({_JIRA_SUMMARY_MAX_CHARS} chars)",
            status_code=400,
        )

    cleaned_description, err = _validate_jira_text_field(
        description, field="description", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err

    cleaned_labels, err = _validate_jira_labels(labels, field="labels")
    if err is not None:
        return err

    if parent is not None and epic_link is not None:
        _b().audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "parent_and_epic_link", **_session_jira_context()},
        )
        return make_error(
            "parent and epicLink are mutually exclusive",
            status_code=400,
        )

    if parent is not None:
        if not isinstance(parent, str) or not _JIRA_TICKET_KEY_RE.fullmatch(parent):
            return make_error("Invalid parent ticket key", status_code=400)
        # Cross-project parent rejection (refine decision-17).
        parent_project = extract_project_key(parent)
        if parent_project != project:
            _b().audit_log(
                f"{operation}_rejected",
                operation,
                success=False,
                details={
                    "reason": "cross_project_parent",
                    "project": project,
                    "parent_project": parent_project,
                    **_session_jira_context(),
                },
            )
            return make_error(
                "parent.key project must match the new ticket's project",
                status_code=400,
                details={"project": project, "parent_project": parent_project},
            )

    if epic_link is not None:
        if not isinstance(epic_link, str) or not _JIRA_TICKET_KEY_RE.fullmatch(epic_link):
            return make_error("Invalid epicLink ticket key", status_code=400)
        # epicLink writes to the same Atlassian field as `parent` when the
        # site uses next-gen / company-managed projects (default
        # `epic_link_field == "parent"`).  That makes `epicLink` a literal
        # alias for `parent` at the wire level, so it MUST inherit the same
        # allowlist + cross-project policy as `parent` (decision-9, decision-17).
        # Otherwise an agent in an allowlisted project could parent a new
        # ticket under an epic in a non-allowlisted project just by routing
        # through the `epicLink` shorthand instead of `parent`.
        epic_project = extract_project_key(epic_link)
        if not _b().is_project_allowed(epic_project):
            return _project_not_allowlisted_response(
                event=f"{operation}_denied",
                ticket=epic_link,
                project=epic_project,
                reason="epicLink project not allowlisted",
            )
        if epic_project != project:
            _b().audit_log(
                f"{operation}_rejected",
                operation,
                success=False,
                details={
                    "reason": "cross_project_epic_link",
                    "project": project,
                    "epic_project": epic_project,
                    **_session_jira_context(),
                },
            )
            return make_error(
                "epicLink project must match the new ticket's project",
                status_code=400,
                details={"project": project, "epic_project": epic_project},
            )

    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return make_error("idempotencyKey must be a string", status_code=400)

    try:
        status_code, body_json, cache_hit = (
            _b()
            .get_jira_client()
            .create_issue(
                project_key=project,
                issuetype=issuetype_arg,
                summary=summary,
                description=cleaned_description,
                labels=cleaned_labels,
                parent=parent,
                epic_link=epic_link,
                epic_link_field=_b().jira_epic_link_field(),
                idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
            )
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "project": project,
                "upstream_status": exc.status_code,
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    new_key = body_json.get("key") if isinstance(body_json, dict) else None
    new_id = body_json.get("id") if isinstance(body_json, dict) else None
    self_url = body_json.get("self") if isinstance(body_json, dict) else None
    browse_url: str | None = None
    if isinstance(self_url, str) and "/rest/api/" in self_url and isinstance(new_key, str):
        # Trim the trailing /rest/api/3/issue/<id> to recover the site root,
        # then append /browse/<KEY>.  This mirrors what Atlassian shows in
        # its UI links.
        site = self_url.split("/rest/api/", 1)[0]
        browse_url = f"{site}/browse/{new_key}"

    # Match the doc's audit grammar: rejection events use ``_rejected`` /
    # ``_denied`` / ``_upstream_error`` suffixes, so successful writes use
    # ``_ok`` (reviewer_code_holistic cycle 1 finding #3, #1924).
    _b().audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "project": project,
            "ticket": new_key,
            "upstream_status": status_code,
            "idempotency_key_present": bool(idempotency_key),
            "idempotency_hit": cache_hit,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )

    envelope: dict[str, Any] = {
        "status": "created",
        "key": new_key,
        "id": new_id,
        "browse_url": browse_url,
    }
    return make_success("Jira ticket created", envelope)


def jira_ticket_edit() -> tuple[Response, int] | Response:
    """Edit a Jira issue via ``PUT /rest/api/3/issue/{key}``.

    Request body::

        {"ticket": "ENG-1",
         "summary": "..." | null,
         "description": "..." | <ADF dict> | null,
         "labels": [...] | null,                # replace mode
         "addLabels": [...] | null,             # incremental mode
         "removeLabels": [...] | null,
         "notifyUsers": false | true}           # default: false

    Replace-mode (``labels``) and incremental-mode
    (``addLabels``/``removeLabels``) are mutually exclusive.
    """
    operation = "jira_ticket_edit"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    err = _validate_jira_write_keys(data, _JIRA_EDIT_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    ticket = data.get("ticket")
    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        _b().audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "invalid ticket shape", **_session_jira_context()},
        )
        return make_error("Invalid ticket key", status_code=400)

    project = extract_project_key(ticket)
    if not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event=f"{operation}_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    summary = data.get("summary")
    description = data.get("description")
    labels = data.get("labels")
    add_labels = data.get("addLabels")
    remove_labels = data.get("removeLabels")
    notify_users = data.get("notifyUsers", False)

    if summary is not None:
        if not isinstance(summary, str):
            return make_error("summary must be a string", status_code=400)
        if len(summary) > _JIRA_SUMMARY_MAX_CHARS:
            return make_error(
                f"summary exceeds maximum length ({_JIRA_SUMMARY_MAX_CHARS} chars)",
                status_code=400,
            )

    cleaned_description, err = _validate_jira_text_field(
        description, field="description", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err

    has_replace = labels is not None
    has_incremental = (add_labels is not None) or (remove_labels is not None)
    if has_replace and has_incremental:
        _b().audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "mixed_label_modes", **_session_jira_context()},
        )
        return make_error(
            "labels and addLabels/removeLabels are mutually exclusive",
            status_code=400,
        )

    cleaned_labels, err = _validate_jira_labels(labels, field="labels")
    if err is not None:
        return err
    cleaned_add, err = _validate_jira_labels(add_labels, field="addLabels")
    if err is not None:
        return err
    cleaned_remove, err = _validate_jira_labels(remove_labels, field="removeLabels")
    if err is not None:
        return err

    if not isinstance(notify_users, bool):
        return make_error("notifyUsers must be a boolean", status_code=400)

    # Require at least one mutating field to avoid no-op edits hitting upstream.
    if (
        summary is None
        and cleaned_description is None
        and cleaned_labels is None
        and cleaned_add is None
        and cleaned_remove is None
    ):
        return make_error(
            "edit requires at least one of summary/description/labels/addLabels/removeLabels",
            status_code=400,
        )

    try:
        _b().get_jira_client().edit_issue(
            key=ticket,
            summary=summary,
            description=cleaned_description,
            labels=cleaned_labels,
            add_labels=cleaned_add,
            remove_labels=cleaned_remove,
            notify_users=notify_users,
        )
    except ValueError as exc:
        # Defence in depth — the route already rejected mixed modes.
        return make_error(str(exc), status_code=400)
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "notify_users": notify_users,
            # editIssue does not consult the idempotency cache (Atlassian
            # PUT is naturally idempotent), but the field is included here
            # for grammar parity with the create / comment / link routes.
            "idempotency_key_present": False,
            "idempotency_hit": False,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )
    return make_success("Jira ticket updated", {"status": "updated", "key": ticket})


def jira_ticket_comment_add() -> tuple[Response, int] | Response:
    """Add a comment to a Jira issue.

    Request body::

        {"ticket": "ENG-1",
         "body": "..." | <ADF dict>,
         "idempotencyKey": "..." | null}

    Visibility (role/group restriction) is rejected — v1 does not expose
    that knob (refine decision-6).  Body content is **never** logged.
    """
    operation = "jira_ticket_comment_add"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    if "visibility" in data:
        return make_error(
            "comment visibility is not supported in v1",
            status_code=400,
        )

    err = _validate_jira_write_keys(data, _JIRA_COMMENT_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    ticket = data.get("ticket")
    body = data.get("body")
    idempotency_key = data.get("idempotencyKey")

    if not isinstance(ticket, str) or not _JIRA_TICKET_KEY_RE.fullmatch(ticket):
        _b().audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={"reason": "invalid ticket shape", **_session_jira_context()},
        )
        return make_error("Invalid ticket key", status_code=400)

    project = extract_project_key(ticket)
    if not _b().is_project_allowed(project):
        return _project_not_allowlisted_response(
            event=f"{operation}_denied",
            ticket=ticket,
            project=project,
            reason="project not allowlisted",
        )

    cleaned_body, err = _validate_jira_text_field(
        body, field="body", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err
    if cleaned_body is None:
        return make_error("body is required", status_code=400)

    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return make_error("idempotencyKey must be a string", status_code=400)

    try:
        _status, comment_json, cache_hit = (
            _b()
            .get_jira_client()
            .add_comment(
                key=ticket,
                body=cleaned_body,
                idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
            )
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "ticket": ticket,
                "project": project,
                "upstream_status": exc.status_code,
                # Note: _jira_write_audit_meta intentionally avoids body content;
                # we still record body_length / body_kind here.
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "ticket": ticket,
            "project": project,
            "idempotency_key_present": bool(idempotency_key),
            "idempotency_hit": cache_hit,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )
    return make_success("Jira comment added", comment_json)


def jira_issue_link_create() -> tuple[Response, int] | Response:
    """Create an issue link between two tickets.

    Request body::

        {"type": "Blocks",
         "inwardIssue": "ENG-1",
         "outwardIssue": "ENG-2",
         "comment": "..." | <ADF dict> | null,
         "idempotencyKey": "..." | null}

    Both tickets' projects must be in the allowlist (refine decision-9).
    Atlassian does **not** dedupe identical triples, so the gateway uses
    its idempotency cache (decision-28) when ``idempotencyKey`` is set.
    """
    operation = "jira_issue_link_create"
    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return make_error("body must be a JSON object", status_code=400)

    err = _validate_jira_write_keys(data, _JIRA_LINK_ALLOWED_KEYS, operation)
    if err is not None:
        return err

    link_type = data.get("type")
    inward = data.get("inwardIssue")
    outward = data.get("outwardIssue")
    comment = data.get("comment")
    idempotency_key = data.get("idempotencyKey")

    if not isinstance(link_type, str) or not link_type:
        return make_error("type is required", status_code=400)
    if not _b().jira_link_type_allowed(link_type):
        _b().audit_log(
            f"{operation}_rejected",
            operation,
            success=False,
            details={
                "reason": "link_type_not_allowlisted",
                "link_type": link_type,
                **_session_jira_context(),
            },
        )
        return make_error(
            f"Link type {link_type!r} not in allowlist",
            status_code=400,
            details={"link_type": link_type},
        )

    if not isinstance(inward, str) or not _JIRA_TICKET_KEY_RE.fullmatch(inward):
        return make_error("inwardIssue must be a Jira ticket key", status_code=400)
    if not isinstance(outward, str) or not _JIRA_TICKET_KEY_RE.fullmatch(outward):
        return make_error("outwardIssue must be a Jira ticket key", status_code=400)

    inward_project = extract_project_key(inward)
    outward_project = extract_project_key(outward)
    for proj, ticket in ((inward_project, inward), (outward_project, outward)):
        if not _b().is_project_allowed(proj):
            return _project_not_allowlisted_response(
                event=f"{operation}_denied",
                ticket=ticket,
                project=proj,
                reason="project not allowlisted",
            )

    cleaned_comment, err = _validate_jira_text_field(
        comment, field="comment", max_chars=_JIRA_BODY_MAX_CHARS, allow_adf=True
    )
    if err is not None:
        return err

    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return make_error("idempotencyKey must be a string", status_code=400)

    try:
        _status, _link_json, cache_hit = (
            _b()
            .get_jira_client()
            .create_issue_link(
                link_type=link_type,
                inward_key=inward,
                outward_key=outward,
                comment=cleaned_comment,
                idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
            )
        )
    except JiraCredentialsUnavailable as exc:
        return _jira_not_configured_error(exc)
    except JiraUpstreamError as exc:
        _b().audit_log(
            f"{operation}_upstream_error",
            operation,
            success=False,
            details={
                "inwardIssue": inward,
                "outwardIssue": outward,
                "type": link_type,
                "upstream_status": exc.status_code,
                **_jira_write_audit_meta(data),
                **_session_jira_context(),
            },
        )
        return _jira_error_from_upstream(exc)

    _b().audit_log(
        f"{operation}_ok",
        operation,
        success=True,
        details={
            "inwardIssue": inward,
            "outwardIssue": outward,
            "type": link_type,
            "inward_project": inward_project,
            "outward_project": outward_project,
            "idempotency_key_present": bool(idempotency_key),
            "idempotency_hit": cache_hit,
            **_jira_write_audit_meta(data),
            **_session_jira_context(),
        },
    )
    return make_success(
        "Jira issue link created",
        {
            "status": "created",
            "inwardIssue": inward,
            "outwardIssue": outward,
            "type": link_type,
        },
    )

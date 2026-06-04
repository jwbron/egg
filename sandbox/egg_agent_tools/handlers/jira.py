"""Jira handlers — gateway-backed reads + writes for sandbox agents (#2994).

These mirror the verbs in the ``sandbox/scripts/jira`` shell wrapper
one-for-one, POSTing to the same ``/api/v1/jira/*`` gateway routes with
the session token :mod:`egg_agent_tools.handlers._gateway` already
resolves.  They hold **no Atlassian credentials** and add **no new
capability** — the gateway still enforces the project allowlist, the
read-only vs. four-write-route split, JQL scope extraction, and the
private-network-mode gate.  This is a presentation layer that makes the
routes discoverable as ``mcp__jira__*`` tools.

The ``transition`` route the gateway exposes is deliberately *not*
mirrored here: the bash wrapper does not surface it either (it is an
operator-only, separately-authorised path), and #2994 scopes the MCP
surface to the wrapper's verbs.

Each handler accepts snake_case request keys and translates them to the
camelCase field names the gateway expects.  None of these verbs has a
Python ``egg-*`` CLI counterpart the MCP↔CLI drift test can walk — the
human-facing analog is the bash ``jira`` wrapper, not an argparse parser
— so every registration sets ``cli_command=None`` and each docstring
records the "no CLI" rationale required by the decision-13 drift gate.
"""

from __future__ import annotations

from typing import Any

from egg_agent_tools.handlers._gateway import gateway_data_request
from egg_agent_tools.handlers.errors import HandlerError


def _as_str_list(value: Any) -> list[str] | None:
    """Normalise a list-or-CSV-string into a clean ``list[str]`` (or None)."""
    if value is None:
        return None
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        raise HandlerError(f"expected a string or list of strings, got {type(value).__name__}")
    cleaned = [v for v in items if v]
    return cleaned or None


def _require_str(req: dict[str, Any], key: str, label: str) -> str:
    value = req.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HandlerError(f"{label} is required (pass '{key}')")
    return value.strip()


def jira_ticket_get(req: dict[str, Any]) -> Any:
    """Fetch a single Jira ticket by key (no CLI counterpart).

    Mirrors ``jira ticket get`` → ``POST /api/v1/jira/ticket/get``.
    Optional ``fields`` narrows the returned field set; omitting it
    returns Atlassian's default set plus rendered body.
    """
    body: dict[str, Any] = {"ticket": _require_str(req, "ticket", "ticket key")}
    fields = _as_str_list(req.get("fields"))
    if fields:
        body["fields"] = fields
    return gateway_data_request("/api/v1/jira/ticket/get", body=body)


def jira_ticket_comments(req: dict[str, Any]) -> Any:
    """Fetch comments on a Jira ticket (no CLI counterpart).

    Mirrors ``jira ticket comments`` →
    ``POST /api/v1/jira/ticket/comments``.
    """
    body = {"ticket": _require_str(req, "ticket", "ticket key")}
    return gateway_data_request("/api/v1/jira/ticket/comments", body=body)


def jira_ticket_remotelinks(req: dict[str, Any]) -> Any:
    """Fetch the remote links on a Jira ticket (no CLI counterpart).

    Mirrors ``jira ticket remotelinks`` →
    ``POST /api/v1/jira/ticket/remotelinks``.  Surfaces PRs humans opened
    against a child ticket so a reassess sweep can treat it as in-flight.
    """
    body = {"ticket": _require_str(req, "ticket", "ticket key")}
    return gateway_data_request("/api/v1/jira/ticket/remotelinks", body=body)


def jira_search(req: dict[str, Any]) -> Any:
    """Search issues with JQL (no CLI counterpart).

    Mirrors ``jira search`` → ``POST /api/v1/jira/search``.  The JQL must
    statically scope to allowlisted projects; the gateway denies on
    ambiguity (any ``OR`` over ``project``).  Paginate with
    ``next_page_token``.
    """
    body: dict[str, Any] = {"jql": _require_str(req, "jql", "jql")}
    if req.get("max_results") is not None:
        body["maxResults"] = int(req["max_results"])
    fields = _as_str_list(req.get("fields"))
    if fields:
        body["fields"] = fields
    if req.get("next_page_token"):
        body["nextPageToken"] = req["next_page_token"]
    return gateway_data_request("/api/v1/jira/search", body=body)


def jira_ticket_create(req: dict[str, Any]) -> Any:
    """Create a new Jira ticket (no CLI counterpart).

    Mirrors ``jira ticket create`` →
    ``POST /api/v1/jira/ticket/create``.  ``project``, ``issue_type``,
    and ``summary`` are required; the gateway enforces the project
    allowlist and private-network mode.  Pass ``idempotency_key`` to make
    a retried create safe.
    """
    body: dict[str, Any] = {
        "project": _require_str(req, "project", "project key"),
        "issuetype": _require_str(req, "issue_type", "issue_type"),
        "summary": _require_str(req, "summary", "summary"),
    }
    if "description" in req and req["description"] is not None:
        body["description"] = req["description"]
    labels = _as_str_list(req.get("labels"))
    if labels:
        body["labels"] = labels
    if req.get("parent"):
        body["parent"] = req["parent"]
    if req.get("epic_link"):
        body["epicLink"] = req["epic_link"]
    if req.get("idempotency_key"):
        body["idempotencyKey"] = req["idempotency_key"]
    return gateway_data_request("/api/v1/jira/ticket/create", body=body)


def jira_ticket_edit(req: dict[str, Any]) -> Any:
    """Edit an existing Jira ticket (no CLI counterpart).

    Mirrors ``jira ticket edit`` → ``POST /api/v1/jira/ticket/edit``.
    ``labels`` (replace) is mutually exclusive with
    ``add_labels``/``remove_labels`` (incremental).  ``notify_users``
    defaults to True to match the ``sandbox/scripts/jira`` wrapper, which
    deliberately overrides the gateway's notify-off default so that a
    planner-authored task referring to either front-end produces the same
    observable side effects; pass ``notify_users=False`` to suppress
    notifications (mirrors the bash ``--no-notify`` flag).
    """
    body: dict[str, Any] = {"ticket": _require_str(req, "ticket", "ticket key")}
    if "summary" in req and req["summary"] is not None:
        body["summary"] = req["summary"]
    if "description" in req and req["description"] is not None:
        body["description"] = req["description"]

    labels = _as_str_list(req.get("labels"))
    add_labels = _as_str_list(req.get("add_labels"))
    remove_labels = _as_str_list(req.get("remove_labels"))
    if labels is not None and (add_labels is not None or remove_labels is not None):
        raise HandlerError(
            "'labels' (replace) is mutually exclusive with "
            "'add_labels'/'remove_labels' (incremental)"
        )
    if labels is not None:
        body["labels"] = labels
    if add_labels is not None:
        body["addLabels"] = add_labels
    if remove_labels is not None:
        body["removeLabels"] = remove_labels

    body["notifyUsers"] = bool(req.get("notify_users", True))
    return gateway_data_request("/api/v1/jira/ticket/edit", body=body)


def jira_ticket_comment_add(req: dict[str, Any]) -> Any:
    """Add a comment to a Jira ticket (no CLI counterpart).

    Mirrors ``jira ticket comment add`` →
    ``POST /api/v1/jira/ticket/comment/add``.  Pass ``idempotency_key``
    to make a retried add safe.
    """
    body: dict[str, Any] = {
        "ticket": _require_str(req, "ticket", "ticket key"),
        "body": _require_str(req, "body", "comment body"),
    }
    if req.get("idempotency_key"):
        body["idempotencyKey"] = req["idempotency_key"]
    return gateway_data_request("/api/v1/jira/ticket/comment/add", body=body)


def jira_link_create(req: dict[str, Any]) -> Any:
    """Link two Jira tickets (no CLI counterpart).

    Mirrors ``jira link create`` →
    ``POST /api/v1/jira/issue-link/create``.  Both issues must be on
    allowlisted projects.  ``link_type`` is the Jira link name (e.g.
    ``Blocks``); ``inward_issue``/``outward_issue`` are the two keys.
    """
    body: dict[str, Any] = {
        "type": _require_str(req, "link_type", "link_type"),
        "inwardIssue": _require_str(req, "inward_issue", "inward_issue"),
        "outwardIssue": _require_str(req, "outward_issue", "outward_issue"),
    }
    if "comment" in req and req["comment"] is not None:
        body["comment"] = req["comment"]
    if req.get("idempotency_key"):
        body["idempotencyKey"] = req["idempotency_key"]
    return gateway_data_request("/api/v1/jira/issue-link/create", body=body)


def jira_execute(req: dict[str, Any]) -> Any:
    """Raw read-only Jira REST passthrough (no CLI counterpart).

    Mirrors ``jira execute`` → ``POST /api/v1/jira/execute``.  GET-only
    escape hatch for routes without a dedicated verb; the gateway rejects
    non-GET methods and denied paths with 403.
    """
    body: dict[str, Any] = {
        "method": _require_str(req, "method", "method"),
        "path": _require_str(req, "path", "path"),
    }
    query = req.get("query")
    if query is not None:
        if not isinstance(query, dict):
            raise HandlerError("'query' must be an object of string key/value pairs")
        body["query"] = query
    return gateway_data_request("/api/v1/jira/execute", body=body)

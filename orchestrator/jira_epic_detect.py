"""
Jira epic detection helpers (issue #1557, TASK-1-2 + TASK-1-3).

Two single-purpose primitives consumed by ``_handle_submit_task`` and the
existing-children sweep (TASK-1-12):

* :func:`detect_jira_issuetype` — call ``GET /api/v1/jira/ticket/get`` once
  per submit_task invocation to figure out whether the supplied Jira key
  belongs to an Epic (which lights up the epic-keyed flow) or a Task /
  Bug / Story (which stays on the existing single-ticket flow).

* :func:`search_epic_children` — fetch every child of an epic via JQL.
  Issues TWO independent queries (``parent = "<KEY>"`` and
  ``"Epic Link" = "<KEY>"``) and merges results by key.  Per architect
  ad-9 a single-OR disjunctive fails with HTTP 400 on team-managed
  (Next-gen) projects that lack the ``Epic Link`` custom field.  The
  second query is skipped when
  :func:`resolve_hierarchy_field(project_key) == "parent"` because the
  ``"Epic Link"`` query would be a tautology / known-empty for that
  project.

Both helpers tolerate per-query HTTP 400 (the
``jira_epic_search_field_missing`` warning is logged and that query's
result set is treated as empty) so a project's hierarchy-field choice
isn't a hard prerequisite for running the probe.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

# Add shared directory to path for egg_logging.
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover — exercised when egg_logging missing
    import logging

    def get_logger(name: str, **kwargs: Any) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from jira_hierarchy_config import (
    JiraHierarchyUnmappedError,
    resolve_hierarchy_field,
)

logger = get_logger("orchestrator.jira_epic_detect")


# Caller-supplied gateway invoker. Signature mirrors the public surface of
# ``orchestrator.gateway_client.GatewayClient._make_request`` so production
# callers pass that bound method directly and tests pass a stub.
GatewayInvoker = Callable[..., dict[str, Any]]


EFFECTIVE_MODE = Literal["fresh", "reassess"]


class JiraEpicDetectionError(Exception):
    """Raised when the detection probe fails fatally (HTTP error, no creds)."""


@dataclass(frozen=True)
class IssuetypeProbeResult:
    """Outcome of :func:`detect_jira_issuetype`."""

    issuetype: str
    is_epic: bool
    project_key: str


def _extract_issuetype_name(body: dict[str, Any]) -> str:
    """Pull ``fields.issuetype.name`` out of a Jira issue payload.

    The gateway's ``jira_ticket_get`` endpoint wraps the Atlassian response
    in a ``data`` envelope (per ``gateway.gateway.make_success``); the
    actual Atlassian payload lives under either ``body["data"]["fields"]``
    or directly under ``body["fields"]`` depending on whether the caller
    inspects the gateway envelope or the raw upstream response.  This
    helper accepts both shapes so tests don't have to mirror the gateway
    envelope precisely.
    """
    # Unwrap the gateway "data" envelope when present.
    if "fields" not in body and isinstance(body.get("data"), dict):
        body = body["data"]

    fields = body.get("fields") or {}
    issuetype = fields.get("issuetype") or {}
    name = issuetype.get("name")
    if not isinstance(name, str) or not name:
        raise JiraEpicDetectionError(
            f"Jira response missing fields.issuetype.name (got {body!r})"
        )
    return name


def _project_key_from_jira_key(jira_key: str) -> str:
    """Return the project portion of a Jira key (e.g. 'ENG' from 'ENG-1234')."""
    if "-" not in jira_key:
        raise JiraEpicDetectionError(
            f"Invalid Jira key '{jira_key}': expected '<PROJECT>-<NUMBER>'"
        )
    return jira_key.split("-", 1)[0]


def detect_jira_issuetype(
    jira_key: str,
    *,
    gateway_invoker: GatewayInvoker,
) -> IssuetypeProbeResult:
    """Probe Atlassian for the issuetype of ``jira_key``.

    Calls the gateway's ``POST /api/v1/jira/ticket/get`` with a minimal
    ``fields=["issuetype"]`` projection so the probe stays cheap on the
    submit_task critical path.

    Returns:
        :class:`IssuetypeProbeResult` with the resolved issuetype name,
        whether it's an Epic, and the project key.

    Raises:
        :class:`JiraEpicDetectionError` when the probe fails (HTTP error,
        unparseable response, missing creds).
    """
    project_key = _project_key_from_jira_key(jira_key)

    try:
        response = gateway_invoker(
            "/api/v1/jira/ticket/get",
            method="POST",
            data={"ticket": jira_key, "fields": ["issuetype"]},
        )
    except Exception as exc:  # noqa: BLE001 — keep callers' error type opaque
        raise JiraEpicDetectionError(
            f"Failed to fetch Jira ticket {jira_key}: {exc}"
        ) from exc

    issuetype_name = _extract_issuetype_name(response)
    is_epic = issuetype_name.strip().lower() == "epic"

    logger.info(
        "jira_epic_detect_issuetype",
        jira_key=jira_key,
        issuetype=issuetype_name,
        is_epic=is_epic,
        project_key=project_key,
    )

    return IssuetypeProbeResult(
        issuetype=issuetype_name,
        is_epic=is_epic,
        project_key=project_key,
    )


def _normalise_children_payload(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the ``issues`` list from a Jira search response.

    Mirrors :func:`_extract_issuetype_name`'s gateway-envelope handling.
    """
    if "issues" not in body and isinstance(body.get("data"), dict):
        body = body["data"]
    issues = body.get("issues")
    if not isinstance(issues, list):
        return []
    return [i for i in issues if isinstance(i, dict)]


def _run_jql(
    jql: str,
    *,
    gateway_invoker: GatewayInvoker,
    fields: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Run a single JQL query, tolerating HTTP 400.

    Returns:
        ``None`` when the upstream rejected the JQL (typically the
        ``"Epic Link"`` field doesn't exist on a team-managed project);
        the caller treats that as an empty result set.  ``[]`` for an
        empty result.  A populated ``list[dict]`` on success.
    """
    payload: dict[str, Any] = {"jql": jql}
    if fields is not None:
        payload["fields"] = fields

    try:
        response = gateway_invoker(
            "/api/v1/jira/search",
            method="POST",
            data=payload,
        )
    except Exception as exc:  # noqa: BLE001
        # HTTP 400 typically means the field referenced in the JQL doesn't
        # exist on the target project (Next-gen / team-managed don't
        # carry the legacy ``Epic Link`` custom field). Log and degrade to
        # an empty set rather than raising — the caller still has the
        # ``parent =`` query to fall back on.
        status_code = getattr(exc, "status_code", None)
        if status_code == 400:
            logger.warning(
                "jira_epic_search_field_missing",
                jql=jql,
                error=str(exc),
            )
            return None
        # Any other error is fatal — surface it.
        raise

    return _normalise_children_payload(response)


def search_epic_children(
    epic_key: str,
    project_key: str | None = None,
    *,
    gateway_invoker: GatewayInvoker,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch the merged child set for an epic.

    Issues TWO independent JQL queries (``parent = "<KEY>"`` and
    ``"Epic Link" = "<KEY>"``) and merges results by ``key``.  When
    :func:`resolve_hierarchy_field` declares the project uses
    ``parent`` only, the second query is skipped.

    Per architect ad-9 a single-OR disjunctive
    ``parent = "<KEY>" OR "Epic Link" = "<KEY>"`` fails with HTTP 400
    on team-managed projects that lack the ``Epic Link`` field, so the
    sweep is forced to run two separate queries and tolerate per-query
    400s.

    Returns:
        Merged list of issue dicts.  Order is not guaranteed (callers
        sort by key if they need stable ordering).
    """
    resolved_project = project_key or _project_key_from_jira_key(epic_key)

    skip_epic_link = False
    try:
        hierarchy_field = resolve_hierarchy_field(resolved_project)
        if hierarchy_field == "parent":
            skip_epic_link = True
    except JiraHierarchyUnmappedError:
        # Without a mapping we don't know which field the project uses,
        # so issue both queries — let per-query 400-tolerance handle the
        # one that doesn't apply.
        skip_epic_link = False

    queries: list[str] = [f'parent = "{epic_key}"']
    if not skip_epic_link:
        queries.append(f'"Epic Link" = "{epic_key}"')

    merged: dict[str, dict[str, Any]] = {}
    for jql in queries:
        result = _run_jql(jql, gateway_invoker=gateway_invoker, fields=fields)
        if result is None:
            # 400-tolerated empty set — keep going.
            continue
        for issue in result:
            key = issue.get("key")
            if isinstance(key, str) and key not in merged:
                merged[key] = issue

    logger.info(
        "jira_epic_search_children",
        epic_key=epic_key,
        project_key=resolved_project,
        merged_count=len(merged),
        queries_run=queries,
    )

    return list(merged.values())


def resolve_effective_mode(
    requested_mode: str,
    *,
    epic_key: str,
    project_key: str,
    gateway_invoker: GatewayInvoker,
) -> tuple[EFFECTIVE_MODE, list[dict[str, Any]]]:
    """Resolve the effective epic-detection mode.

    Implements #1557 decision-12 + TASK-1-3:

    * ``auto`` → ``reassess`` when the merged child set is non-empty,
      else ``fresh``.
    * ``reassess`` on a childless epic emits a structured warning and
      degrades to ``fresh``.
    * ``fresh`` on an epic with children emits a structured warning and
      proceeds with ``fresh`` (the operator asked for it explicitly).

    Returns:
        Tuple of (resolved mode, merged child list).  The child list is
        returned so the caller can persist it without re-issuing the
        JQL — the existing-children sweep (TASK-1-12) reuses the same
        helper.
    """
    if requested_mode not in ("auto", "reassess", "fresh"):
        raise JiraEpicDetectionError(
            f"requested_mode must be one of auto / reassess / fresh "
            f"(got {requested_mode!r})"
        )

    children = search_epic_children(
        epic_key,
        project_key=project_key,
        gateway_invoker=gateway_invoker,
        # We only need keys / status / issuetype for the auto-detection
        # decision; downstream callers (existing-children sweep) re-run
        # the helper with a richer field projection.
        fields=["status", "issuetype"],
    )
    has_children = len(children) > 0

    if requested_mode == "auto":
        mode: EFFECTIVE_MODE = "reassess" if has_children else "fresh"
    elif requested_mode == "reassess":
        if has_children:
            mode = "reassess"
        else:
            logger.warning(
                "jira_epic_reassess_degraded_to_fresh",
                epic_key=epic_key,
                project_key=project_key,
                reason="no children on epic",
            )
            mode = "fresh"
    else:  # requested_mode == "fresh"
        if has_children:
            logger.warning(
                "jira_epic_fresh_with_existing_children",
                epic_key=epic_key,
                project_key=project_key,
                child_count=len(children),
            )
        mode = "fresh"

    logger.info(
        "jira_epic_resolve_mode",
        epic_key=epic_key,
        requested_mode=requested_mode,
        effective_mode=mode,
        child_count=len(children),
    )

    return mode, children


if TYPE_CHECKING:  # pragma: no cover
    # Keep the public symbols visible to type-checkers without forcing a
    # heavy import at module load time.
    __all__ = [
        "EFFECTIVE_MODE",
        "GatewayInvoker",
        "IssuetypeProbeResult",
        "JiraEpicDetectionError",
        "detect_jira_issuetype",
        "resolve_effective_mode",
        "search_epic_children",
    ]

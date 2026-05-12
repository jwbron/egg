"""
Reassess sweep + in-flight detection (issue #1557 slice-2 tasks 2-1 + 2-4).

When ``Pipeline.pipeline_mode == 'reassess'`` the orchestrator calls
``run_reassess_sweep`` to fetch every Atlassian child of the epic via
the gateway's JQL search and classify each as one of:

- ``done``       — ``statusCategory.key == 'done'``; excluded from
                   the planner prompt entirely (decision-5) but
                   persisted to ``EGG_DONE_CHILDREN_PATH`` for
                   provenance.
- ``in_flight``  — ``statusCategory.key == 'indeterminate'`` OR an
                   ``open`` PR exists in the orchestrator reverse-
                   index OR a GitHub remote-link on the ticket matches
                   ``^https?://github\\.com/.+/pull/\\d+$`` (two-signal
                   detection per decision-7).
- ``updatable``  — anything else (default class).

The result is serialised to a JSON file under
``.egg-state/agent-outputs/`` and the path is exported to the sandbox
env as ``EGG_REASSESS_SWEEP_PATH`` so the task-planner prompt
(``epic-reassess`` mode block) can render the classification diff.
The Done summary list is written to a separate file referenced by
``EGG_DONE_CHILDREN_PATH``.

The module is pure-Python with no Flask / app-context dependency so
it can be unit-tested directly against a mock gateway client.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

logger = logging.getLogger(__name__)


_REASSESS_TIMEOUT_SECONDS = 20

# Two-signal in-flight detection: GitHub PR URL pattern that
# ``_remotelinks_indicate_pr`` matches against (decision-7 signal b).
# Same regex used by the planner prompt's example output.
_GITHUB_PR_URL_RE = re.compile(r"^https?://github\.com/.+/pull/\d+$")

_REASSESS_FIELDS = (
    "summary",
    "status",
    "description",
    "parent",
    "issuetype",
)


def _resolve_launcher_secret() -> str:
    """Read the orchestrator's launcher secret (mirror of jira_epic).

    Tries ``/secrets/launcher-secret`` first; falls back to
    ``EGG_LAUNCHER_SECRET`` env. Returns empty string on miss so the
    caller can decide whether to omit the header entirely.
    """
    mount_path = "/secrets/launcher-secret"
    try:
        with open(mount_path, encoding="utf-8") as fh:
            secret = fh.read().strip()
            if secret:
                return secret
    except OSError:
        pass
    return os.environ.get("EGG_LAUNCHER_SECRET", "")


def _gateway_base_url() -> str:
    """Mirror of :func:`orchestrator.jira_epic._gateway_base_url`.

    Duplicated to avoid a coupling between the slice-1 helper and the
    slice-2 helper — they have different fail-open semantics.
    """
    explicit = os.environ.get("EGG_GATEWAY_URL", "").rstrip("/")
    if explicit:
        return explicit
    host = os.environ.get("GATEWAY_HOST", "gateway.egg-system.svc.cluster.local")
    port = os.environ.get("GATEWAY_PORT", "9848")
    return f"http://{host}:{port}"


def _gateway_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """Issue a POST to the gateway and return the decoded body.

    Raises on transport error.
    """
    url = f"{_gateway_base_url()}{path}"
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    launcher = _resolve_launcher_secret()
    if launcher:
        headers["Authorization"] = f"Bearer {launcher}"
    opener = build_opener()
    req = Request(url, data=payload, headers=headers, method="POST")
    with opener.open(req, timeout=_REASSESS_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8")
        if not raw:
            return {}
        return json.loads(raw)


@dataclass
class ReassessChild:
    """A single child of a Jira epic, after classification.

    The shape is intentionally JSON-friendly so the orchestrator can
    splat ``[asdict(c) for c in result.children]`` into a file under
    ``.egg-state/agent-outputs/`` and the task-planner prompt can
    consume it with no extra translation.
    """

    key: str
    summary: str
    status_name: str = ""
    status_category: str = ""
    classification: str = "updatable"  # one of: done | in_flight | updatable
    in_flight: bool = False
    in_flight_evidence: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class ReassessSweepResult:
    """Aggregate result returned by :func:`run_reassess_sweep`.

    ``done`` children are kept in their own list so callers can write
    them to ``EGG_DONE_CHILDREN_PATH`` without filtering twice.
    ``children`` contains the planning-relevant entries (Updatable +
    In-flight); Done children are intentionally excluded from this
    list (decision-5).
    """

    epic_key: str
    project: str
    children: list[ReassessChild] = field(default_factory=list)
    done: list[ReassessChild] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _classify_status_category(category_key: str) -> str:
    """Map an Atlassian ``statusCategory.key`` to a sweep class."""
    if not isinstance(category_key, str):
        return "updatable"
    normalised = category_key.strip().lower()
    if normalised == "done":
        return "done"
    if normalised == "indeterminate":
        # Map to in_flight as a baseline; downstream may upgrade with
        # PR / remotelink evidence.
        return "in_flight"
    return "updatable"


def _remotelinks_indicate_pr(remotelinks: list[dict[str, Any]] | None) -> list[str]:
    """Return the GitHub PR URLs found in a remote-link payload.

    Each entry is an Atlassian remote-link object; the URL lives at
    ``object.url``. Returns an empty list if no PR URLs are present
    (or input is malformed).
    """
    matches: list[str] = []
    if not remotelinks or not isinstance(remotelinks, list):
        return matches
    for entry in remotelinks:
        if not isinstance(entry, dict):
            continue
        obj = entry.get("object") or {}
        if not isinstance(obj, dict):
            continue
        url = obj.get("url")
        if isinstance(url, str) and _GITHUB_PR_URL_RE.match(url):
            matches.append(url)
    return matches


def fetch_remote_links(child_key: str) -> list[dict[str, Any]]:
    """Wrap the gateway's ``/api/v1/jira/ticket/remotelinks`` route.

    Returns ``[]`` on transport error or non-2xx. Caller treats an
    empty list as "no PR signal".
    """
    if not child_key:
        return []
    try:
        response = _gateway_post(
            "/api/v1/jira/ticket/remotelinks",
            {"key": child_key},
        )
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Reassess sweep: remotelinks fetch failed for %s — %s",
            child_key,
            exc,
        )
        return []
    data = response.get("data") or response
    links = data.get("remotelinks") or data.get("links") or []
    if isinstance(links, list):
        return [link for link in links if isinstance(link, dict)]
    return []


def pipelines_for_ticket_pr_url(
    state_store: Any,
    ticket: str,
) -> list[str]:
    """Return the open PR URLs the orchestrator already tracks for
    ``ticket`` (signal a of decision-7).

    Calls :meth:`StateStore.pipelines_for_jira_ticket` (added by
    task-2-2) and returns a list of ``pr_url`` strings. Callers treat
    a non-empty result as "in-flight". Defensive: any state-store
    error returns ``[]`` so the sweep does not fail closed.
    """
    if state_store is None or not ticket:
        return []
    if not hasattr(state_store, "pipelines_for_jira_ticket"):
        return []
    try:
        pipelines = state_store.pipelines_for_jira_ticket(ticket)
    except Exception as exc:
        logger.warning(
            "Reassess sweep: pipelines_for_jira_ticket failed for %s — %s",
            ticket,
            exc,
        )
        return []
    urls: list[str] = []
    for pipeline in pipelines or []:
        pr_url = getattr(pipeline, "pr_url", None)
        if isinstance(pr_url, str) and pr_url:
            urls.append(pr_url)
    return urls


def classify_in_flight(
    *,
    status_category: str,
    pr_urls_from_index: list[str],
    pr_urls_from_remotelinks: list[str],
) -> tuple[bool, list[str]]:
    """Apply the two-signal in-flight rule (decision-7).

    Returns ``(in_flight, evidence_list)``. ``evidence_list`` contains
    human-readable strings naming which signal(s) fired — surfaced in
    the planner prompt so the operator can audit the decision.
    """
    evidence: list[str] = []
    in_flight = False

    if isinstance(status_category, str) and status_category.strip().lower() == "indeterminate":
        evidence.append("status_category=indeterminate")
        in_flight = True

    if pr_urls_from_index:
        evidence.extend(
            [f"egg_pipeline_pr={url}" for url in pr_urls_from_index]
        )
        in_flight = True

    if pr_urls_from_remotelinks:
        evidence.extend(
            [f"remotelink_pr={url}" for url in pr_urls_from_remotelinks]
        )
        in_flight = True

    return in_flight, evidence


def run_reassess_sweep(
    *,
    epic_key: str,
    project: str | None = None,
    state_store: Any = None,
    check_remotelinks: bool = True,
) -> ReassessSweepResult:
    """Run a reassess sweep against a Jira epic.

    Parameters
    ----------
    epic_key:
        Atlassian epic key (e.g. ``"ENG-1234"``). Must already be
        normalised to upper-case.
    project:
        Project segment override. When omitted it is parsed from the
        epic key. Constraints: same-project only (decision-12).
    state_store:
        The orchestrator's state store, used for the reverse-index
        in-flight signal (signal a of decision-7). Pass ``None`` from
        callers that don't have one (e.g. unit tests).
    check_remotelinks:
        When True, augments in-flight classification with the
        remote-link signal (decision-7 signal b). Set False in unit
        tests that don't want the extra network hop.

    Returns
    -------
    :class:`ReassessSweepResult`
        Always returned — even on transport error the result is a
        valid (empty) sweep with a warning enumerated.
    """
    if not epic_key:
        return ReassessSweepResult(epic_key="", project="")
    project_segment = project or (
        epic_key.split("-", 1)[0] if "-" in epic_key else ""
    )
    result = ReassessSweepResult(epic_key=epic_key, project=project_segment)

    if not project_segment:
        result.warnings.append(
            f"Reassess sweep: could not derive project from epic key {epic_key!r}"
        )
        return result

    jql = f"project = {project_segment} AND parent = {epic_key}"
    try:
        response = _gateway_post(
            "/api/v1/jira/search",
            {
                "jql": jql,
                "maxResults": 200,
                "fields": list(_REASSESS_FIELDS),
            },
        )
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Reassess sweep: JQL search failed for epic %s — %s",
            epic_key,
            exc,
        )
        result.warnings.append(f"jql_search_failed: {exc}")
        return result

    data = response.get("data") or response
    issues = data.get("issues")
    if not isinstance(issues, list):
        result.warnings.append("jql_search_returned_no_issues_list")
        return result

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        key = issue.get("key") or ""
        fields_obj = issue.get("fields") or {}
        summary = fields_obj.get("summary") or ""
        status_obj = fields_obj.get("status") or {}
        status_name = (
            status_obj.get("name", "") if isinstance(status_obj, dict) else ""
        )
        status_category_obj = (
            status_obj.get("statusCategory") if isinstance(status_obj, dict) else None
        )
        status_category_key = ""
        if isinstance(status_category_obj, dict):
            status_category_key = status_category_obj.get("key", "") or ""
        description = fields_obj.get("description")
        if not isinstance(description, str):
            description = ""

        classification = _classify_status_category(status_category_key)

        # In-flight refinement: classify_in_flight may flag a child
        # as in_flight even when statusCategory says 'new', if signals
        # a / b fire. ``done`` children never flip to in_flight per
        # decision-5 — done is terminal.
        pr_urls_index = pipelines_for_ticket_pr_url(state_store, key)
        pr_urls_remotelinks: list[str] = []
        if check_remotelinks and classification != "done":
            remote_links = fetch_remote_links(key)
            pr_urls_remotelinks = _remotelinks_indicate_pr(remote_links)
        in_flight, evidence = classify_in_flight(
            status_category=status_category_key,
            pr_urls_from_index=pr_urls_index,
            pr_urls_from_remotelinks=pr_urls_remotelinks,
        )
        if classification != "done" and in_flight:
            classification = "in_flight"

        child = ReassessChild(
            key=key,
            summary=summary,
            status_name=status_name,
            status_category=status_category_key,
            classification=classification,
            in_flight=in_flight,
            in_flight_evidence=evidence,
            description=description,
        )
        if classification == "done":
            result.done.append(child)
        else:
            result.children.append(child)

    return result


def serialise_sweep_to_disk(
    *,
    result: ReassessSweepResult,
    agent_outputs_dir: Path,
    pipeline_id: str,
) -> tuple[Path, Path]:
    """Persist the sweep result + Done-children list to disk.

    Returns ``(sweep_path, done_path)``. The sweep path is exported
    to the sandbox as ``EGG_REASSESS_SWEEP_PATH`` and the done path as
    ``EGG_DONE_CHILDREN_PATH``.
    """
    agent_outputs_dir.mkdir(parents=True, exist_ok=True)
    sweep_path = agent_outputs_dir / f"{pipeline_id}-reassess-sweep.json"
    done_path = agent_outputs_dir / f"{pipeline_id}-done-children.json"

    sweep_payload = {
        "epic_key": result.epic_key,
        "project": result.project,
        "children": [asdict(c) for c in result.children],
        "warnings": list(result.warnings),
    }
    sweep_path.write_text(json.dumps(sweep_payload, indent=2), encoding="utf-8")

    done_payload = {
        "epic_key": result.epic_key,
        "project": result.project,
        "done_children": [
            {"key": c.key, "summary": c.summary, "status_name": c.status_name}
            for c in result.done
        ],
    }
    done_path.write_text(json.dumps(done_payload, indent=2), encoding="utf-8")

    return sweep_path, done_path


__all__ = [
    "ReassessChild",
    "ReassessSweepResult",
    "classify_in_flight",
    "fetch_remote_links",
    "pipelines_for_ticket_pr_url",
    "run_reassess_sweep",
    "serialise_sweep_to_disk",
]

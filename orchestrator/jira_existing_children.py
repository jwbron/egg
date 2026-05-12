"""
Existing-children sweep for Jira epic reassess (issue #1557 TASK-1-12).

Classifies every existing child of a Jira epic into one of three buckets:

* ``done`` — Jira status name is "Done".
* ``in_flight`` — any of three signals fires (decision-8 OR semantics):
  Jira status is in the "actively-worked" set
  ({"In Progress", "In Review", "Code Review", "Blocked"}), the
  orchestrator's reverse-index records an in-flight pipeline whose PR has
  opened, OR a remote link on the child references a GitHub PR URL.
* ``to_do`` — none of the above; the child is open but no work is
  visibly in progress.

All firing signals are recorded on
:attr:`ExistingChild.in_flight_signals` so the in-flight HITL gate
(TASK-1-17) can surface every piece of evidence to the operator —
matching #1557 decision-8 / R2 mitigation.

Reverse-index file:

To avoid scanning every ``.egg-state/pipelines/<id>.json`` file (R3
performance), the orchestrator maintains a reverse-index at
``.egg-state/jira-child-pipeline-index.json`` mapping
``<JIRA-KEY> -> [pipeline_id, ...]``.  The sweep loads the index once,
then opens only the pipelines that match.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Add shared directory to path for egg_logging.
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str, **kwargs: Any):  # type: ignore[misc]
        return logging.getLogger(name)


from jira_epic_detect import search_epic_children

logger = get_logger("orchestrator.jira_existing_children")


ChildClassification = Literal["done", "to_do", "in_flight"]
InFlightSource = Literal["jira_status", "orchestrator_pr_url", "remote_link"]


# Jira statuses we treat as "actively worked" (in_flight). Lower-cased
# matching — operators customise Jira statuses and the casing varies
# across team-managed vs company-managed projects.
ACTIVELY_WORKED_STATUSES: frozenset[str] = frozenset(
    {"in progress", "in review", "code review", "blocked"}
)

# Jira statuses we treat as "done" (terminal state).
DONE_STATUSES: frozenset[str] = frozenset({"done", "closed", "resolved"})

# Regex matching a GitHub pull-request URL on the remote-links list.
GITHUB_PR_URL_RE = re.compile(r"^https?://github\.com/[^/]+/[^/]+/pull/\d+\b")

# Default location of the reverse-index file.
DEFAULT_INDEX_PATH = Path(".egg-state/jira-child-pipeline-index.json")


@dataclass(frozen=True)
class InFlightSignal:
    source: InFlightSource
    detail: str


@dataclass(frozen=True)
class ExistingChild:
    key: str
    summary: str
    status: str
    description: str
    classification: ChildClassification
    in_flight_signals: tuple[InFlightSignal, ...] = field(default_factory=tuple)


# Caller-supplied gateway invoker; same signature as
# ``orchestrator.jira_epic_detect.GatewayInvoker``.
GatewayInvoker = Callable[..., dict[str, Any]]


def _classify_by_status(status: str) -> tuple[ChildClassification, InFlightSignal | None]:
    s = status.strip().lower()
    if s in DONE_STATUSES:
        return ("done", None)
    if s in ACTIVELY_WORKED_STATUSES:
        return (
            "in_flight",
            InFlightSignal(source="jira_status", detail=status),
        )
    return ("to_do", None)


def _load_reverse_index(index_path: Path) -> dict[str, list[str]]:
    """Load the JIRA-KEY -> [pipeline_id, ...] reverse-index file.

    Returns an empty mapping when the file is missing — the sweep
    degrades gracefully to "no orchestrator-side evidence" rather than
    failing the whole reassess flow.
    """
    if not index_path.exists():
        return {}
    try:
        raw = json.loads(index_path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning(
            "jira_child_pipeline_index_unreadable",
            path=str(index_path),
            error=str(exc),
        )
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, list):
            result[k] = [item for item in v if isinstance(item, str)]
    return result


def _read_pipeline_pr_url(repo_path: Path, pipeline_id: str) -> str | None:
    """Return the PR URL recorded on ``phases["pr"].artifacts["pr_url"]``.

    Returns ``None`` when the pipeline file is missing, malformed, or
    has no recorded PR URL.
    """
    pipeline_file = repo_path / ".egg-state" / "pipelines" / f"{pipeline_id}.json"
    if not pipeline_file.exists():
        return None
    try:
        data = json.loads(pipeline_file.read_text())
    except OSError, ValueError:
        return None
    phases = data.get("phases") or {}
    pr_phase = phases.get("pr") or {}
    artifacts = pr_phase.get("artifacts") or {}
    raw_url = artifacts.get("pr_url")
    if isinstance(raw_url, str) and raw_url:
        return raw_url
    return None


def _check_orchestrator_pr_signal(
    child_key: str,
    repo_path: Path,
    index: dict[str, list[str]],
) -> InFlightSignal | None:
    """Check the orchestrator's pipeline-state for an open PR on ``child_key``."""
    pipeline_ids = index.get(child_key) or []
    for pid in pipeline_ids:
        pr_url = _read_pipeline_pr_url(repo_path, pid)
        if pr_url:
            return InFlightSignal(source="orchestrator_pr_url", detail=pr_url)
    return None


def _check_remote_link_signal(
    child_key: str,
    *,
    gateway_invoker: GatewayInvoker,
) -> InFlightSignal | None:
    """Check the child's Jira remote-links for a GitHub PR URL."""
    try:
        response = gateway_invoker(
            "/api/v1/jira/ticket/remotelinks",
            method="POST",
            data={"ticket": child_key},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "jira_existing_children_remotelink_error",
            child_key=child_key,
            error=str(exc),
        )
        return None

    payload = response.get("data") if isinstance(response, dict) else response
    if not isinstance(payload, dict):
        return None
    remote_links = payload.get("remoteLinks") or payload.get("remote_links") or []
    if not isinstance(remote_links, list):
        return None

    for link in remote_links:
        if not isinstance(link, dict):
            continue
        obj = link.get("object") or {}
        url = obj.get("url") if isinstance(obj, dict) else None
        if isinstance(url, str) and GITHUB_PR_URL_RE.match(url):
            return InFlightSignal(source="remote_link", detail=url)
    return None


def sweep_existing_children(
    epic_key: str,
    *,
    project_key: str | None = None,
    gateway_invoker: GatewayInvoker,
    repo_path: Path | str = ".",
    index_path: Path | None = None,
) -> list[ExistingChild]:
    """Return the classified children of ``epic_key``.

    Combines the two-query JQL fetch from
    :func:`jira_epic_detect.search_epic_children` with the three
    in-flight signal sources (Jira status, orchestrator pr_url
    artifact, remote-link GitHub PR URL).

    Args:
        epic_key: The epic to sweep.
        project_key: Optional project key (defaults to the prefix of
            ``epic_key``).  Passed through to the two-query helper so it
            can skip the ``Epic Link`` query when the hierarchy config
            says the project uses ``parent``.
        gateway_invoker: Callable matching the
            ``GatewayClient._make_request`` signature.  Used for the
            JQL search AND the per-child ``remotelinks`` calls.
        repo_path: Repo root containing ``.egg-state/``.  Defaults to
            the current working directory.
        index_path: Override the reverse-index location (defaults to
            ``<repo_path>/.egg-state/jira-child-pipeline-index.json``).
    """
    repo = Path(repo_path)
    resolved_index_path = index_path or (repo / DEFAULT_INDEX_PATH)

    # 1. Two-query JQL fetch (architect ad-9).
    children = search_epic_children(
        epic_key,
        project_key=project_key,
        gateway_invoker=gateway_invoker,
        fields=["summary", "status", "description"],
    )

    # 2. Load reverse-index once (constant cost regardless of child count).
    index = _load_reverse_index(resolved_index_path)

    # 3. Classify each child + cross-check the two non-status signals.
    results: list[ExistingChild] = []
    for issue in children:
        key = issue.get("key")
        if not isinstance(key, str):
            continue
        fields = issue.get("fields") or {}
        status_block = fields.get("status") or {}
        status = status_block.get("name") if isinstance(status_block, dict) else None
        if not isinstance(status, str):
            status = "Unknown"
        summary = fields.get("summary") or ""
        if not isinstance(summary, str):
            summary = ""
        description = fields.get("description") or ""
        if not isinstance(description, str):
            description = ""

        signals: list[InFlightSignal] = []
        classification, status_signal = _classify_by_status(status)
        if status_signal is not None:
            signals.append(status_signal)

        # Only cross-check the orchestrator + remote-link signals when
        # the child isn't already classified as ``done``.  A "Done"
        # child with a stale GitHub PR remote-link is still Done.
        if classification != "done":
            pr_signal = _check_orchestrator_pr_signal(key, repo, index)
            if pr_signal is not None:
                signals.append(pr_signal)
                classification = "in_flight"
            remote_link_signal = _check_remote_link_signal(key, gateway_invoker=gateway_invoker)
            if remote_link_signal is not None:
                signals.append(remote_link_signal)
                classification = "in_flight"

        results.append(
            ExistingChild(
                key=key,
                summary=summary,
                status=status,
                description=description,
                classification=classification,
                in_flight_signals=tuple(signals),
            )
        )

    logger.info(
        "jira_existing_children_sweep_complete",
        epic_key=epic_key,
        total=len(results),
        done=sum(1 for c in results if c.classification == "done"),
        to_do=sum(1 for c in results if c.classification == "to_do"),
        in_flight=sum(1 for c in results if c.classification == "in_flight"),
    )

    return results


def update_reverse_index(
    repo_path: Path | str,
    pipeline_id: str,
    jira_key: str,
    *,
    index_path: Path | None = None,
) -> None:
    """Append ``pipeline_id`` to the index entry for ``jira_key``.

    Called by the orchestrator at pipeline-creation time when a child
    pipeline is associated with a Jira ticket.  Idempotent — a
    pipeline_id is added at most once per key.
    """
    repo = Path(repo_path)
    target = index_path or (repo / DEFAULT_INDEX_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        try:
            raw = json.loads(target.read_text())
        except OSError, ValueError:
            raw = {}
    else:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    pipelines = raw.get(jira_key)
    if not isinstance(pipelines, list):
        pipelines = []
    if pipeline_id not in pipelines:
        pipelines.append(pipeline_id)
    raw[jira_key] = pipelines

    target.write_text(json.dumps(raw, indent=2, sort_keys=True))


__all__ = [
    "ACTIVELY_WORKED_STATUSES",
    "DONE_STATUSES",
    "ChildClassification",
    "ExistingChild",
    "GITHUB_PR_URL_RE",
    "GatewayInvoker",
    "InFlightSignal",
    "InFlightSource",
    "sweep_existing_children",
    "update_reverse_index",
]

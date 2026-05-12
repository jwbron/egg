"""
Refine-phase input gatherer for Jira epic pipelines (#1557 TASK-1-9).

Assembles three sections before the refine agent runs:

1. **Epic self** — summary + description (+ remote links) of the epic
   itself, fetched via the gateway's ``ticket/get`` and ``remotelinks``
   routes.
2. **Existing children** — for reassess-mode pipelines, every child's
   key / summary / status / description (via the two-query
   ``search_epic_children`` helper).  Done children stay in the refine
   input so the agent has full context; the plan-prompt exclusion is
   per-phase, not per-input (decision-4).
3. **Confluence pages** — discovered per decision-7: epic-level remote
   links to Confluence + URL-scrape of the epic description body +
   remote-links on linked Jira issues, **recursion capped at exactly
   one level**.

The gathered payload is written under
``.egg-state/agent-outputs/<id>-refine-input.json`` so the refine
agent can ``Read`` it.  The gatherer also stamps the
``refine_description_sha256`` field on the (initially-empty) epic_apply
artifact (architect ad-5 concurrent-edit guard) so the refine-apply
step (TASK-1-10) can detect operator edits between refine kick-off and
apply.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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

logger = get_logger("orchestrator.jira_epic_inputs")


GatewayInvoker = Callable[..., dict[str, Any]]


# Match Atlassian Cloud Confluence URLs:
#   https://<tenant>.atlassian.net/wiki/...
# (Atlassian Confluence Cloud always lives under /wiki on the tenant
# origin.)  The regex tolerates ``http`` for self-hosted Confluence
# Data Center / Server URLs that operators occasionally embed.
CONFLUENCE_URL_RE = re.compile(
    r"https?://[^\s/<>\"]+\.atlassian\.net/wiki/[^\s<>\"]+",
    re.IGNORECASE,
)


# Match a generic URL inside the description body. We then filter by
# the more permissive Confluence regex above so non-Atlassian URLs
# don't pollute the recursion candidate set.
GENERIC_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


@dataclass
class ConfluenceCandidate:
    """Single discovered Confluence URL."""

    url: str
    source: str  # 'epic_remote_link', 'epic_description', 'child_remote_link'
    via: str  # epic key or child key the link was discovered through


@dataclass
class RefineInputs:
    """Bundle returned by :func:`gather_refine_inputs`."""

    epic_key: str
    epic_summary: str
    epic_description: str
    epic_description_sha256: str
    epic_remote_links: list[dict[str, Any]] = field(default_factory=list)
    existing_children: list[dict[str, Any]] = field(default_factory=list)
    confluence_candidates: list[ConfluenceCandidate] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "epic_key": self.epic_key,
            "epic_summary": self.epic_summary,
            "epic_description": self.epic_description,
            "epic_description_sha256": self.epic_description_sha256,
            "epic_remote_links": self.epic_remote_links,
            "existing_children": self.existing_children,
            "confluence_candidates": [
                {"url": c.url, "source": c.source, "via": c.via} for c in self.confluence_candidates
            ],
        }


def _flatten_description(value: Any) -> str:
    """Render Atlassian's ADF (Atlassian Document Format) into plain text.

    The Atlassian REST API returns the description as either a raw
    string (rare, only for tickets that haven't been edited via the
    rich editor) or an ADF JSON object.  We don't need fidelity — just
    enough text for the agent to read and for the URL-scrape to match
    against.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # ADF: walk the tree, collect every "text" leaf.
        parts: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("text"), str):
                    parts.append(node["text"])
                for child in node.get("content", []) or []:
                    _walk(child)
            elif isinstance(node, list):
                for child in node:
                    _walk(child)

        _walk(value)
        return "\n".join(parts)
    return str(value)


def _fetch_epic_payload(epic_key: str, *, gateway_invoker: GatewayInvoker) -> dict[str, Any]:
    response = gateway_invoker(
        "/api/v1/jira/ticket/get",
        method="POST",
        data={"ticket": epic_key, "fields": ["summary", "description"]},
    )
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    return response if isinstance(response, dict) else {}


def _fetch_remote_links(jira_key: str, *, gateway_invoker: GatewayInvoker) -> list[dict[str, Any]]:
    """Return the list of remote links on ``jira_key``.

    Empty list when the call fails or returns a 404 envelope — the
    refine flow degrades gracefully on missing remote-links data.
    """
    try:
        response = gateway_invoker(
            "/api/v1/jira/ticket/remotelinks",
            method="POST",
            data={"ticket": jira_key},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "jira_epic_remotelinks_fetch_failed",
            jira_key=jira_key,
            error=str(exc),
        )
        return []
    payload = response.get("data") if isinstance(response, dict) else response
    if not isinstance(payload, dict):
        return []
    if payload.get("status") == "not_found":
        return []
    links = payload.get("remoteLinks") or payload.get("remote_links") or []
    if not isinstance(links, list):
        return []
    return [link for link in links if isinstance(link, dict)]


def _extract_confluence_urls_from_remote_links(
    remote_links: list[dict[str, Any]], *, source: str, via: str
) -> list[ConfluenceCandidate]:
    candidates: list[ConfluenceCandidate] = []
    for link in remote_links:
        obj = link.get("object") or {}
        url = obj.get("url") if isinstance(obj, dict) else None
        if isinstance(url, str) and CONFLUENCE_URL_RE.match(url):
            candidates.append(ConfluenceCandidate(url=url, source=source, via=via))
    return candidates


def _extract_confluence_urls_from_text(
    text: str, *, source: str, via: str
) -> list[ConfluenceCandidate]:
    candidates: list[ConfluenceCandidate] = []
    seen: set[str] = set()
    for match in GENERIC_URL_RE.finditer(text):
        url = match.group(0)
        if not CONFLUENCE_URL_RE.match(url):
            continue
        # Strip trailing punctuation that is commonly captured by the
        # URL regex (e.g. ``http://x/wiki/foo).``).
        url = url.rstrip(".,);:]\"'")
        if url in seen:
            continue
        seen.add(url)
        candidates.append(ConfluenceCandidate(url=url, source=source, via=via))
    return candidates


def gather_refine_inputs(
    epic_key: str,
    *,
    project_key: str | None = None,
    effective_mode: str | None = None,
    gateway_invoker: GatewayInvoker,
) -> RefineInputs:
    """Assemble the three-section input bundle for the refine agent.

    Args:
        epic_key: The epic to gather inputs for.
        project_key: Optional project key (defaults to the prefix of
            ``epic_key``); passed to ``search_epic_children`` so it can
            skip the ``Epic Link`` query when the hierarchy YAML says
            the project uses ``parent``.
        effective_mode: When ``"reassess"``, fetch every existing
            child's full payload.  Otherwise children are still listed
            (the refine prompt may want to mention them) but with a
            lighter projection.
        gateway_invoker: ``GatewayClient._make_request``-shaped callable.
    """
    # 1. Epic self.
    epic_payload = _fetch_epic_payload(epic_key, gateway_invoker=gateway_invoker)
    fields = epic_payload.get("fields") or {}
    summary = fields.get("summary") if isinstance(fields, dict) else None
    if not isinstance(summary, str):
        summary = ""
    description = _flatten_description(
        fields.get("description") if isinstance(fields, dict) else None
    )
    description_sha256 = hashlib.sha256(description.encode("utf-8")).hexdigest()

    # 2. Epic remote links.
    epic_remote_links = _fetch_remote_links(epic_key, gateway_invoker=gateway_invoker)

    # 3. Existing children.
    children = search_epic_children(
        epic_key,
        project_key=project_key,
        gateway_invoker=gateway_invoker,
        fields=["summary", "status", "description"],
    )

    # 4. Confluence candidates: epic remote-links + URL-scrape on the
    #    description body + remote-links on linked Jira issues
    #    (recursive ONE level only per decision-7).
    confluence_candidates: list[ConfluenceCandidate] = []
    confluence_candidates.extend(
        _extract_confluence_urls_from_remote_links(
            epic_remote_links, source="epic_remote_link", via=epic_key
        )
    )
    confluence_candidates.extend(
        _extract_confluence_urls_from_text(description, source="epic_description", via=epic_key)
    )

    # 5. Discover linked Jira issues from the epic's remote_links AND
    #    inspect their remote-links (depth-1 recursion).  The
    #    recursion stops here — under no circumstances do we follow
    #    second-degree links (#1557 decision-7).
    linked_jira_keys: list[str] = []
    for link in epic_remote_links:
        obj = link.get("object") or {}
        url = obj.get("url") if isinstance(obj, dict) else None
        # Match Jira ticket URLs of the form
        # ``https://<tenant>.atlassian.net/browse/PROJECT-N``.
        if isinstance(url, str):
            m = re.search(r"/browse/([A-Z][A-Z0-9_]*-\d+)", url)
            if m and m.group(1) != epic_key:
                linked_jira_keys.append(m.group(1))

    seen_keys: set[str] = set()
    for child_key in linked_jira_keys:
        if child_key in seen_keys:
            continue
        seen_keys.add(child_key)
        child_links = _fetch_remote_links(child_key, gateway_invoker=gateway_invoker)
        confluence_candidates.extend(
            _extract_confluence_urls_from_remote_links(
                child_links,
                source="child_remote_link",
                via=child_key,
            )
        )

    # De-duplicate Confluence candidates by URL (keep the first
    # source/via we saw — typically the epic itself).
    seen_urls: set[str] = set()
    deduped_candidates: list[ConfluenceCandidate] = []
    for candidate in confluence_candidates:
        if candidate.url in seen_urls:
            continue
        seen_urls.add(candidate.url)
        deduped_candidates.append(candidate)

    inputs = RefineInputs(
        epic_key=epic_key,
        epic_summary=summary,
        epic_description=description,
        epic_description_sha256=description_sha256,
        epic_remote_links=epic_remote_links,
        existing_children=children,
        confluence_candidates=deduped_candidates,
    )

    logger.info(
        "jira_epic_refine_inputs_gathered",
        epic_key=epic_key,
        effective_mode=effective_mode,
        children_count=len(children),
        remote_links_count=len(epic_remote_links),
        confluence_candidates_count=len(deduped_candidates),
    )

    return inputs


def write_inputs_to_agent_outputs(
    inputs: RefineInputs,
    *,
    pipeline_id: str,
    issue_number: int | None,
    repo_path: Path | str = ".",
) -> Path:
    """Write the gathered inputs to ``.egg-state/agent-outputs/<id>-refine-input.json``.

    The prefix mirrors the orchestrator's draft-file naming: when
    ``issue_number`` is set, use that; otherwise fall back to the
    pipeline_id.  Returns the absolute Path of the written file.
    """
    repo = Path(repo_path)
    prefix = str(issue_number) if issue_number is not None else pipeline_id
    target = repo / ".egg-state" / "agent-outputs" / f"{prefix}-refine-input.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(inputs.to_payload(), indent=2, sort_keys=True))
    logger.info(
        "jira_epic_refine_inputs_written",
        path=str(target),
        bytes=target.stat().st_size,
    )
    return target


__all__ = [
    "CONFLUENCE_URL_RE",
    "ConfluenceCandidate",
    "GatewayInvoker",
    "RefineInputs",
    "gather_refine_inputs",
    "write_inputs_to_agent_outputs",
]

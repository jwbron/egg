"""Tests for ``orchestrator/jira_epic_inputs.py`` (issue #1557).

Covers the refine-input gatherer:

* ``_flatten_description`` — Atlassian ADF tree walker (paragraph,
  heading, text leaf nodes).
* ``_extract_confluence_urls_from_remote_links`` — pulls Confluence
  URLs out of the Atlassian remote-link JSON envelope.
* ``_extract_confluence_urls_from_text`` — regex scan of an
  unstructured description body for Confluence URLs (with trailing-
  punctuation strip + dedup).
* ``gather_refine_inputs`` — assembles epic self / remote links /
  existing children / confluence candidates with depth-1 recursion on
  linked Jira issues (per decision-7).
* ``RefineInputs.write_inputs_to_agent_outputs`` — writes the JSON
  payload to ``.egg-state/agent-outputs/<prefix>-refine-input.json``
  with ``issue_number`` taking precedence over ``pipeline_id`` for the
  filename prefix.
* Adversarial: malformed ADF, gateway raising, missing remote_links.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from jira_epic_inputs import (
    CONFLUENCE_URL_RE,
    ConfluenceCandidate,
    RefineInputs,
    _extract_confluence_urls_from_remote_links,
    _extract_confluence_urls_from_text,
    _fetch_remote_links,
    _flatten_description,
    gather_refine_inputs,
    write_inputs_to_agent_outputs,
)

# ---------------------------------------------------------------------------
# Helper: fake gateway invoker
# ---------------------------------------------------------------------------


def _make_invoker(
    *,
    ticket_get: dict | None = None,
    remotelinks: dict[str, list[dict]] | None = None,
    on_remotelinks_error: str | None = None,
):
    """Build a gateway invoker that returns the canned epic + remote-link data."""
    ticket_get = ticket_get or {}
    remotelinks = remotelinks or {}

    def invoker(path: str, *, method: str = "POST", data: dict | None = None, **_):
        if path == "/api/v1/jira/ticket/get":
            ticket = (data or {}).get("ticket")
            return {"data": ticket_get.get(ticket, {})}
        if path == "/api/v1/jira/ticket/remotelinks":
            ticket = (data or {}).get("ticket")
            if on_remotelinks_error and ticket == on_remotelinks_error:
                raise RuntimeError("gateway exploded")
            return {"data": {"remoteLinks": remotelinks.get(ticket, [])}}
        return {"data": {}}

    return invoker


# ---------------------------------------------------------------------------
# _flatten_description
# ---------------------------------------------------------------------------


class TestFlattenDescription:
    def test_none_returns_empty_string(self):
        assert _flatten_description(None) == ""

    def test_raw_string_passes_through(self):
        assert _flatten_description("plain text body") == "plain text body"

    def test_simple_adf_paragraph(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "hello world"}],
                }
            ],
        }
        assert _flatten_description(adf) == "hello world"

    def test_adf_with_heading_and_multiple_paragraphs(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "content": [{"type": "text", "text": "Title"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "first"}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "second"}],
                },
            ],
        }
        out = _flatten_description(adf)
        # Each text leaf is its own line — order preserved.
        assert out == "Title\nfirst\nsecond"

    def test_adf_with_nested_marks_and_links(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "see "},
                        {
                            "type": "text",
                            "text": "the doc",
                            "marks": [{"type": "link", "attrs": {"href": "https://x"}}],
                        },
                    ],
                }
            ],
        }
        assert _flatten_description(adf) == "see \nthe doc"

    def test_malformed_string_inside_dict_returns_str_repr(self):
        # If the value is neither None / str / dict it falls through to str().
        assert _flatten_description(12345) == "12345"

    def test_empty_adf_doc(self):
        # A doc with no content should yield an empty string, not crash.
        assert _flatten_description({"type": "doc", "content": []}) == ""


# ---------------------------------------------------------------------------
# _extract_confluence_urls_from_remote_links
# ---------------------------------------------------------------------------


class TestExtractConfluenceFromRemoteLinks:
    def test_extracts_atlassian_wiki_url(self):
        links = [
            {"object": {"url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/123"}},
        ]
        candidates = _extract_confluence_urls_from_remote_links(
            links, source="epic_remote_link", via="PROJ-1"
        )
        assert len(candidates) == 1
        assert candidates[0].url.endswith("/pages/123")
        assert candidates[0].source == "epic_remote_link"
        assert candidates[0].via == "PROJ-1"

    def test_ignores_non_confluence_urls(self):
        links = [
            {"object": {"url": "https://github.com/owner/repo"}},
            {"object": {"url": "https://acme.atlassian.net/browse/PROJ-2"}},
        ]
        candidates = _extract_confluence_urls_from_remote_links(
            links, source="epic_remote_link", via="PROJ-1"
        )
        assert candidates == []

    def test_handles_missing_object(self):
        links = [{}, {"object": None}, {"object": {}}]
        candidates = _extract_confluence_urls_from_remote_links(
            links, source="epic_remote_link", via="PROJ-1"
        )
        assert candidates == []


# ---------------------------------------------------------------------------
# _extract_confluence_urls_from_text
# ---------------------------------------------------------------------------


class TestExtractConfluenceFromText:
    def test_finds_url_in_body(self):
        text = "See https://acme.atlassian.net/wiki/spaces/ENG/pages/1 for details."
        candidates = _extract_confluence_urls_from_text(
            text, source="epic_description", via="PROJ-1"
        )
        assert len(candidates) == 1
        # Trailing period is stripped.
        assert candidates[0].url.endswith("/pages/1")
        assert candidates[0].source == "epic_description"

    def test_deduplicates_repeated_urls(self):
        text = (
            "https://x.atlassian.net/wiki/spaces/A/pages/9 and again "
            "https://x.atlassian.net/wiki/spaces/A/pages/9 here too"
        )
        candidates = _extract_confluence_urls_from_text(
            text, source="epic_description", via="PROJ-1"
        )
        assert len(candidates) == 1

    def test_strips_trailing_punctuation(self):
        text = "Linked: (https://acme.atlassian.net/wiki/spaces/E/pages/5),"
        candidates = _extract_confluence_urls_from_text(
            text, source="epic_description", via="PROJ-1"
        )
        assert len(candidates) == 1
        # No trailing ),
        assert not candidates[0].url.endswith(",")
        assert not candidates[0].url.endswith(")")
        assert candidates[0].url.endswith("/pages/5")

    def test_ignores_non_confluence_urls(self):
        text = "https://example.com/something and https://github.com/owner/repo"
        candidates = _extract_confluence_urls_from_text(
            text, source="epic_description", via="PROJ-1"
        )
        assert candidates == []

    def test_empty_text_returns_empty(self):
        assert _extract_confluence_urls_from_text("", source="epic_description", via="PROJ-1") == []


# ---------------------------------------------------------------------------
# gather_refine_inputs
# ---------------------------------------------------------------------------


class TestGatherRefineInputs:
    def test_happy_path_assembles_all_sections(self):
        # Epic with summary, ADF description containing a Confluence URL,
        # remote-links pointing at Confluence + a linked Jira issue, and
        # one child returned by ``search_epic_children``.
        epic_payload = {
            "PROJ-EPIC": {
                "key": "PROJ-EPIC",
                "fields": {
                    "summary": "Epic title",
                    "description": {
                        "type": "doc",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "see https://acme.atlassian.net/wiki/spaces/ENG/pages/1"
                                        ),
                                    }
                                ],
                            }
                        ],
                    },
                },
            }
        }
        remotelinks = {
            "PROJ-EPIC": [
                {"object": {"url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/2"}},
                {"object": {"url": "https://acme.atlassian.net/browse/PROJ-200"}},
            ],
            # Depth-1 recursion into the linked Jira issue surfaces ONE
            # extra Confluence candidate.
            "PROJ-200": [
                {"object": {"url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/3"}},
            ],
        }
        invoker = _make_invoker(ticket_get=epic_payload, remotelinks=remotelinks)
        children = [
            {"key": "PROJ-100", "fields": {"summary": "child", "status": {"name": "To Do"}}}
        ]

        with patch("jira_epic_inputs.search_epic_children", return_value=children):
            inputs = gather_refine_inputs(
                "PROJ-EPIC",
                gateway_invoker=invoker,
            )

        assert isinstance(inputs, RefineInputs)
        assert inputs.epic_key == "PROJ-EPIC"
        assert inputs.epic_summary == "Epic title"
        assert "atlassian.net/wiki/spaces/ENG/pages/1" in inputs.epic_description
        # sha256 of the canonicalised description is recorded for ad-5
        # concurrent-edit detection. v5 canonicalises the *raw* ADF document
        # (not the flattened text) via ``compute_description_sha256``, so we
        # reuse the source's helper applied to the same ADF payload the test
        # fixture sent in.
        from jira_epic_inputs import compute_description_sha256

        expected_sha = compute_description_sha256(
            epic_payload["PROJ-EPIC"]["fields"]["description"]
        )
        assert inputs.epic_description_sha256 == expected_sha
        # Whatever the canonicalisation, the hash must be a 64-char hex string.
        assert len(inputs.epic_description_sha256) == 64
        assert all(c in "0123456789abcdef" for c in inputs.epic_description_sha256)
        assert len(inputs.epic_remote_links) == 2
        assert len(inputs.existing_children) == 1
        # Confluence candidate dedup keeps the first observed source/via.
        urls = {c.url for c in inputs.confluence_candidates}
        assert urls == {
            "https://acme.atlassian.net/wiki/spaces/ENG/pages/1",
            "https://acme.atlassian.net/wiki/spaces/ENG/pages/2",
            "https://acme.atlassian.net/wiki/spaces/ENG/pages/3",
        }
        # The depth-1 recursion attributes the child link to PROJ-200.
        by_url = {c.url: c for c in inputs.confluence_candidates}
        assert (
            by_url["https://acme.atlassian.net/wiki/spaces/ENG/pages/3"].source
            == "child_remote_link"
        )
        assert by_url["https://acme.atlassian.net/wiki/spaces/ENG/pages/3"].via == "PROJ-200"

    def test_missing_remote_links_returns_empty_list(self):
        # Gateway responds with no ``remoteLinks`` field — gather_refine_inputs
        # must degrade gracefully, not crash.
        epic_payload = {
            "PROJ-EPIC": {
                "key": "PROJ-EPIC",
                "fields": {"summary": "title", "description": "body"},
            }
        }
        invoker = _make_invoker(ticket_get=epic_payload)
        with patch("jira_epic_inputs.search_epic_children", return_value=[]):
            inputs = gather_refine_inputs("PROJ-EPIC", gateway_invoker=invoker)
        assert inputs.epic_remote_links == []
        assert inputs.confluence_candidates == []

    def test_malformed_adf_string_falls_through(self):
        # Description arrives as a raw string instead of a dict ADF tree —
        # the flattener returns it verbatim, gather still succeeds.
        epic_payload = {
            "PROJ-EPIC": {
                "key": "PROJ-EPIC",
                "fields": {"summary": "title", "description": "raw body text"},
            }
        }
        invoker = _make_invoker(ticket_get=epic_payload)
        with patch("jira_epic_inputs.search_epic_children", return_value=[]):
            inputs = gather_refine_inputs("PROJ-EPIC", gateway_invoker=invoker)
        assert inputs.epic_description == "raw body text"

    def test_gateway_remotelinks_raises_does_not_propagate(self):
        # The epic-self call succeeds, but the remotelinks call raises.
        epic_payload = {
            "PROJ-EPIC": {
                "key": "PROJ-EPIC",
                "fields": {"summary": "title", "description": "body"},
            }
        }
        invoker = _make_invoker(ticket_get=epic_payload, on_remotelinks_error="PROJ-EPIC")
        with patch("jira_epic_inputs.search_epic_children", return_value=[]):
            inputs = gather_refine_inputs("PROJ-EPIC", gateway_invoker=invoker)
        assert inputs.epic_remote_links == []
        assert inputs.confluence_candidates == []

    def test_not_found_remote_links_envelope_returns_empty(self):
        # Gateway 404 envelope: payload has ``status: not_found``.
        def invoker(path, *, method="POST", data=None, **_):
            if path == "/api/v1/jira/ticket/get":
                return {"data": {"fields": {"summary": "x", "description": "y"}}}
            return {"data": {"status": "not_found"}}

        with patch("jira_epic_inputs.search_epic_children", return_value=[]):
            inputs = gather_refine_inputs("PROJ-EPIC", gateway_invoker=invoker)
        assert inputs.epic_remote_links == []

    def test_self_link_to_epic_not_recursed(self):
        # The /browse/<key> link points back at the epic itself — the
        # gatherer must NOT recurse into it.
        epic_payload = {
            "PROJ-EPIC": {
                "key": "PROJ-EPIC",
                "fields": {"summary": "x", "description": "y"},
            }
        }
        remotelinks = {
            "PROJ-EPIC": [
                {"object": {"url": "https://acme.atlassian.net/browse/PROJ-EPIC"}},
            ],
        }
        invoker = _make_invoker(ticket_get=epic_payload, remotelinks=remotelinks)
        with patch("jira_epic_inputs.search_epic_children", return_value=[]):
            inputs = gather_refine_inputs("PROJ-EPIC", gateway_invoker=invoker)
        assert inputs.confluence_candidates == []


# ---------------------------------------------------------------------------
# write_inputs_to_agent_outputs
# ---------------------------------------------------------------------------


class TestWriteInputsToAgentOutputs:
    def _base_inputs(self) -> RefineInputs:
        return RefineInputs(
            epic_key="PROJ-EPIC",
            epic_summary="title",
            epic_description="body",
            epic_description_sha256=hashlib.sha256(b"body").hexdigest(),
            epic_remote_links=[],
            existing_children=[],
            confluence_candidates=[
                ConfluenceCandidate(
                    url="https://acme.atlassian.net/wiki/spaces/E/pages/1",
                    source="epic_remote_link",
                    via="PROJ-EPIC",
                )
            ],
        )

    def test_writes_payload_using_issue_number_prefix(self, tmp_path: Path):
        inputs = self._base_inputs()
        target = write_inputs_to_agent_outputs(
            inputs,
            pipeline_id="pipe-xyz",
            issue_number=1557,
            repo_path=tmp_path,
        )
        # When issue_number is set it wins over pipeline_id.
        assert target == tmp_path / ".egg-state" / "agent-outputs" / "1557-refine-input.json"
        assert target.exists()
        payload = json.loads(target.read_text())
        assert payload["epic_key"] == "PROJ-EPIC"
        assert payload["epic_summary"] == "title"
        assert payload["confluence_candidates"][0]["url"].endswith("/pages/1")
        assert payload["confluence_candidates"][0]["source"] == "epic_remote_link"

    def test_falls_back_to_pipeline_id_when_no_issue_number(self, tmp_path: Path):
        inputs = self._base_inputs()
        target = write_inputs_to_agent_outputs(
            inputs,
            pipeline_id="pipe-xyz",
            issue_number=None,
            repo_path=tmp_path,
        )
        assert target.name == "pipe-xyz-refine-input.json"
        assert target.exists()

    def test_creates_parent_directories(self, tmp_path: Path):
        inputs = self._base_inputs()
        # tmp_path has no .egg-state/ yet — the helper must mkdir(parents=True).
        target = write_inputs_to_agent_outputs(
            inputs,
            pipeline_id="pipe-xyz",
            issue_number=999,
            repo_path=tmp_path,
        )
        assert target.parent.is_dir()
        assert target.parent.name == "agent-outputs"
        assert target.parent.parent.name == ".egg-state"


# ---------------------------------------------------------------------------
# CONFLUENCE_URL_RE constant
# ---------------------------------------------------------------------------


class TestConfluenceUrlRegex:
    def test_matches_https_atlassian_wiki(self):
        assert CONFLUENCE_URL_RE.match("https://acme.atlassian.net/wiki/spaces/ENG/pages/9")

    def test_does_not_match_browse_url(self):
        assert CONFLUENCE_URL_RE.match("https://acme.atlassian.net/browse/PROJ-1") is None

    def test_matches_self_hosted_http_variant(self):
        # The source allows http (Server / Data Center variants).
        assert CONFLUENCE_URL_RE.match("http://acme.atlassian.net/wiki/display/ENG/Page")


# ---------------------------------------------------------------------------
# _fetch_remote_links direct unit
# ---------------------------------------------------------------------------


class TestFetchRemoteLinks:
    def test_returns_list_when_envelope_normal(self):
        def invoker(*args, **kwargs):
            return {
                "data": {
                    "remoteLinks": [
                        {"object": {"url": "https://x"}},
                        {"object": {"url": "https://y"}},
                    ]
                }
            }

        out = _fetch_remote_links("PROJ-1", gateway_invoker=invoker)
        assert len(out) == 2

    def test_returns_empty_when_invoker_raises(self):
        def invoker(*args, **kwargs):
            raise RuntimeError("boom")

        assert _fetch_remote_links("PROJ-1", gateway_invoker=invoker) == []

    def test_filters_out_non_dict_link_entries(self):
        def invoker(*args, **kwargs):
            return {
                "data": {
                    "remoteLinks": [
                        {"object": {"url": "https://x"}},
                        "not a link",
                        None,
                    ]
                }
            }

        out = _fetch_remote_links("PROJ-1", gateway_invoker=invoker)
        assert len(out) == 1
        assert out[0]["object"]["url"] == "https://x"

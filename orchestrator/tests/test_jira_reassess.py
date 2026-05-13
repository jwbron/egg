"""
Tests for ``orchestrator.jira_reassess`` (issue #1557 slice-2 task-2-9).

Covers:

- **task-2-1** sweep classification: ``_classify_status_category``,
  ``run_reassess_sweep`` end-to-end against a mocked gateway, project
  derivation, transport-error handling, ``done`` is terminal and never
  flips to ``in_flight``, ``serialise_sweep_to_disk`` produces the two
  expected files with correct payload shape.

- **task-2-4** in-flight helper truth table: ``classify_in_flight``
  exercised across all three signal sources (status_category,
  ``pr_urls_from_index``, ``pr_urls_from_remotelinks``) independently
  and combined. ``_remotelinks_indicate_pr`` accepts only the canonical
  ``https?://github.com/.../pull/<N>`` URL shape and ignores malformed
  entries. ``pipelines_for_ticket_pr_url`` reads the state-store
  reverse-index correctly and tolerates missing methods.

The module under test is pure-Python and dependency-free; tests
substitute the gateway via the public seam (``_gateway_post``) using
``monkeypatch``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import jira_reassess
import pytest
from jira_reassess import (
    ReassessChild,
    ReassessSweepResult,
    _classify_status_category,
    _remotelinks_indicate_pr,
    classify_in_flight,
    fetch_remote_links,
    pipelines_for_ticket_pr_url,
    run_reassess_sweep,
    serialise_sweep_to_disk,
)

# -----------------------------------------------------------------------------
# _classify_status_category — status → class mapping
# -----------------------------------------------------------------------------


class TestClassifyStatusCategory:
    """``_classify_status_category`` should map Atlassian status keys to one
    of the three sweep classes per decision-13."""

    def test_done_lowercase(self):
        assert _classify_status_category("done") == "done"

    def test_done_uppercase(self):
        """Atlassian sometimes returns mixed case; normalise."""
        assert _classify_status_category("DONE") == "done"

    def test_done_with_whitespace(self):
        assert _classify_status_category("  done  ") == "done"

    def test_indeterminate_maps_to_in_flight(self):
        assert _classify_status_category("indeterminate") == "in_flight"

    def test_new_maps_to_updatable(self):
        """New / unstarted / unclassified status → updatable (default)."""
        assert _classify_status_category("new") == "updatable"

    def test_empty_string_defaults_to_updatable(self):
        assert _classify_status_category("") == "updatable"

    def test_non_string_defaults_to_updatable(self):
        """Defensive: non-string inputs return ``updatable`` instead of
        raising. Real-world payloads occasionally surface None here."""
        assert _classify_status_category(None) == "updatable"  # type: ignore[arg-type]
        assert _classify_status_category(123) == "updatable"  # type: ignore[arg-type]
        assert _classify_status_category({"key": "done"}) == "updatable"  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# _remotelinks_indicate_pr — extracting GitHub PR URLs
# -----------------------------------------------------------------------------


class TestRemotelinksIndicatePr:
    """``_remotelinks_indicate_pr`` filters a remote-link payload down to
    the set of GitHub PR URLs (decision-7 signal b)."""

    def test_empty_list_returns_empty(self):
        assert _remotelinks_indicate_pr([]) == []

    def test_none_input_returns_empty(self):
        assert _remotelinks_indicate_pr(None) == []

    def test_non_list_input_returns_empty(self):
        """Real Atlassian sometimes returns a dict envelope — we only
        accept the documented list shape."""
        assert _remotelinks_indicate_pr({"a": 1}) == []  # type: ignore[arg-type]

    def test_canonical_github_pr_url(self):
        payload = [{"object": {"url": "https://github.com/jwbron/egg/pull/123"}}]
        assert _remotelinks_indicate_pr(payload) == ["https://github.com/jwbron/egg/pull/123"]

    def test_http_github_pr_url(self):
        """http (no S) is still a PR signal — gateway may rewrite."""
        payload = [{"object": {"url": "http://github.com/jwbron/egg/pull/4"}}]
        assert _remotelinks_indicate_pr(payload) == ["http://github.com/jwbron/egg/pull/4"]

    def test_jira_internal_link_ignored(self):
        """Non-GitHub URLs aren't PR signals."""
        payload = [{"object": {"url": "https://example.atlassian.net/browse/X-1"}}]
        assert _remotelinks_indicate_pr(payload) == []

    def test_github_non_pr_url_ignored(self):
        """``github.com/owner/repo`` without /pull/N is not a PR."""
        payload = [{"object": {"url": "https://github.com/jwbron/egg"}}]
        assert _remotelinks_indicate_pr(payload) == []

    def test_github_issue_url_ignored(self):
        """Issue URLs are not PR URLs."""
        payload = [{"object": {"url": "https://github.com/jwbron/egg/issues/42"}}]
        assert _remotelinks_indicate_pr(payload) == []

    def test_multiple_links_collected(self):
        payload = [
            {"object": {"url": "https://github.com/jwbron/egg/pull/1"}},
            {"object": {"url": "https://github.com/jwbron/egg/pull/2"}},
        ]
        assert _remotelinks_indicate_pr(payload) == [
            "https://github.com/jwbron/egg/pull/1",
            "https://github.com/jwbron/egg/pull/2",
        ]

    def test_malformed_entry_skipped(self):
        """Non-dict entries / missing ``object`` are skipped silently."""
        payload: list[Any] = [
            "not a dict",
            {"missing_object": True},
            {"object": "also not a dict"},
            {"object": {"no_url_key": "x"}},
            {"object": {"url": None}},
            {"object": {"url": "https://github.com/jwbron/egg/pull/9"}},
        ]
        assert _remotelinks_indicate_pr(payload) == ["https://github.com/jwbron/egg/pull/9"]

    def test_non_string_url_ignored(self):
        """Non-string ``url`` is defensively ignored."""
        payload = [{"object": {"url": 12345}}]
        assert _remotelinks_indicate_pr(payload) == []


# -----------------------------------------------------------------------------
# classify_in_flight — two-signal rule with evidence
# -----------------------------------------------------------------------------


class TestClassifyInFlight:
    """``classify_in_flight`` applies the decision-7 truth table.

    Each independent signal flips ``in_flight`` to True; combined
    signals accumulate evidence strings. Status category 'indeterminate'
    is signal pure-status (per the acceptance: "pure-status in_flight
    round-trips even when the reverse-index returns empty").
    """

    def test_no_signals_returns_not_in_flight(self):
        in_flight, evidence = classify_in_flight(
            status_category="new",
            pr_urls_from_index=[],
            pr_urls_from_remotelinks=[],
        )
        assert in_flight is False
        assert evidence == []

    def test_pure_status_indeterminate_signal(self):
        """Pure-status in-flight: only signal is status_category."""
        in_flight, evidence = classify_in_flight(
            status_category="indeterminate",
            pr_urls_from_index=[],
            pr_urls_from_remotelinks=[],
        )
        assert in_flight is True
        assert evidence == ["status_category=indeterminate"]

    def test_pure_status_indeterminate_uppercase(self):
        """Status comparison is case-insensitive."""
        in_flight, evidence = classify_in_flight(
            status_category="INDETERMINATE",
            pr_urls_from_index=[],
            pr_urls_from_remotelinks=[],
        )
        assert in_flight is True
        assert "status_category=indeterminate" in evidence

    def test_pr_index_signal_only(self):
        """Reverse-index PR URL flips in_flight even when status is new."""
        in_flight, evidence = classify_in_flight(
            status_category="new",
            pr_urls_from_index=["https://github.com/x/y/pull/1"],
            pr_urls_from_remotelinks=[],
        )
        assert in_flight is True
        assert evidence == ["egg_pipeline_pr=https://github.com/x/y/pull/1"]

    def test_remotelinks_signal_only(self):
        """Remote-link PR flips in_flight even when status is new."""
        in_flight, evidence = classify_in_flight(
            status_category="new",
            pr_urls_from_index=[],
            pr_urls_from_remotelinks=["https://github.com/x/y/pull/2"],
        )
        assert in_flight is True
        assert evidence == ["remotelink_pr=https://github.com/x/y/pull/2"]

    def test_all_three_signals_combined(self):
        """All three signals combine into a single evidence list."""
        in_flight, evidence = classify_in_flight(
            status_category="indeterminate",
            pr_urls_from_index=["https://github.com/x/y/pull/10"],
            pr_urls_from_remotelinks=["https://github.com/x/y/pull/11"],
        )
        assert in_flight is True
        assert "status_category=indeterminate" in evidence
        assert "egg_pipeline_pr=https://github.com/x/y/pull/10" in evidence
        assert "remotelink_pr=https://github.com/x/y/pull/11" in evidence
        assert len(evidence) == 3

    def test_done_status_is_not_in_flight_via_status(self):
        """Done status alone does not flag in_flight (the sweep keeps
        done terminal — decision-5)."""
        in_flight, evidence = classify_in_flight(
            status_category="done",
            pr_urls_from_index=[],
            pr_urls_from_remotelinks=[],
        )
        assert in_flight is False
        assert evidence == []

    def test_multiple_index_pr_urls_all_recorded(self):
        in_flight, evidence = classify_in_flight(
            status_category="new",
            pr_urls_from_index=[
                "https://github.com/x/y/pull/1",
                "https://github.com/x/y/pull/2",
            ],
            pr_urls_from_remotelinks=[],
        )
        assert in_flight is True
        assert evidence == [
            "egg_pipeline_pr=https://github.com/x/y/pull/1",
            "egg_pipeline_pr=https://github.com/x/y/pull/2",
        ]

    def test_non_string_status_returns_no_status_evidence(self):
        """Non-string status falls back gracefully (no status evidence,
        other signals still apply)."""
        in_flight, evidence = classify_in_flight(
            status_category=None,  # type: ignore[arg-type]
            pr_urls_from_index=["https://github.com/x/y/pull/1"],
            pr_urls_from_remotelinks=[],
        )
        # Other signals still fire.
        assert in_flight is True
        assert "egg_pipeline_pr=https://github.com/x/y/pull/1" in evidence
        assert all("status_category" not in e for e in evidence)


# -----------------------------------------------------------------------------
# pipelines_for_ticket_pr_url — reverse-index reader
# -----------------------------------------------------------------------------


class TestPipelinesForTicketPrUrl:
    """``pipelines_for_ticket_pr_url`` is a defensive wrapper around the
    state-store's reverse-index. It returns the open PR URL list and
    never raises."""

    def test_none_state_store_returns_empty(self):
        assert pipelines_for_ticket_pr_url(None, "ENG-1") == []

    def test_empty_ticket_returns_empty(self):
        store = MagicMock()
        assert pipelines_for_ticket_pr_url(store, "") == []
        # Defensive: the helper must not call into the store with a
        # blank ticket key.
        store.pipelines_for_jira_ticket.assert_not_called()

    def test_store_without_method_returns_empty(self):
        """An older state-store that hasn't grown the reverse-index API
        is treated as empty (no in-flight evidence)."""

        class _NoMethodStore:
            pass

        store = _NoMethodStore()
        assert pipelines_for_ticket_pr_url(store, "ENG-1") == []

    def test_store_raises_returns_empty(self):
        """Any state-store error is swallowed — sweep fails open."""
        store = MagicMock()
        store.pipelines_for_jira_ticket.side_effect = RuntimeError("boom")
        assert pipelines_for_ticket_pr_url(store, "ENG-1") == []

    def test_extracts_pr_urls_only(self):
        """Pipelines without ``pr_url`` are silently filtered."""
        store = MagicMock()
        store.pipelines_for_jira_ticket.return_value = [
            MagicMock(pr_url="https://github.com/x/y/pull/1"),
            MagicMock(pr_url=None),
            MagicMock(pr_url=""),
            MagicMock(pr_url="https://github.com/x/y/pull/2"),
        ]
        urls = pipelines_for_ticket_pr_url(store, "ENG-1")
        assert urls == [
            "https://github.com/x/y/pull/1",
            "https://github.com/x/y/pull/2",
        ]

    def test_empty_pipeline_list_returns_empty(self):
        store = MagicMock()
        store.pipelines_for_jira_ticket.return_value = []
        assert pipelines_for_ticket_pr_url(store, "ENG-1") == []

    def test_pipeline_without_pr_url_attr_skipped(self):
        """A Pipeline-like object missing ``pr_url`` is silently skipped."""
        store = MagicMock()
        plain_obj = MagicMock(spec=[])  # no attrs at all
        store.pipelines_for_jira_ticket.return_value = [plain_obj]
        assert pipelines_for_ticket_pr_url(store, "ENG-1") == []


# -----------------------------------------------------------------------------
# fetch_remote_links — gateway wrapper
# -----------------------------------------------------------------------------


class TestFetchRemoteLinks:
    """``fetch_remote_links`` wraps the gateway ``/remotelinks`` route.
    Failures must return ``[]`` so the sweep can fail open."""

    def test_empty_key_returns_empty(self):
        assert fetch_remote_links("") == []

    def test_transport_error_returns_empty(self, monkeypatch):
        """A URLError / OSError surfaces as an empty list."""

        def _raise(path, body):
            raise OSError("network down")

        monkeypatch.setattr(jira_reassess, "_gateway_post", _raise)
        assert fetch_remote_links("ENG-1") == []

    def test_happy_path_extracts_links_from_data_key(self):
        """Gateway envelope ``{'data': {'remotelinks': [...]}}`` works."""
        sample = {
            "data": {
                "remotelinks": [
                    {"object": {"url": "https://github.com/x/y/pull/1"}},
                    {"object": {"url": "https://example.com/x"}},
                ]
            }
        }
        with _patch_gateway_post(sample):
            links = fetch_remote_links("ENG-1")
        assert len(links) == 2

    def test_happy_path_bare_remotelinks_key(self):
        """No ``data`` wrapper — direct ``{'remotelinks': [...]}``."""
        sample = {"remotelinks": [{"object": {"url": "x"}}]}
        with _patch_gateway_post(sample):
            links = fetch_remote_links("ENG-1")
        assert len(links) == 1

    def test_bare_links_key_accepted(self):
        """Older callers may emit ``{'links': [...]}``."""
        sample = {"links": [{"object": {"url": "x"}}]}
        with _patch_gateway_post(sample):
            links = fetch_remote_links("ENG-1")
        assert len(links) == 1

    def test_missing_links_returns_empty(self):
        with _patch_gateway_post({"data": {}}):
            assert fetch_remote_links("ENG-1") == []

    def test_request_body_field_name_is_ticket(self, monkeypatch):
        """**Field-name contract** (reviewer_code v1 finding #3):
        ``fetch_remote_links`` MUST POST a body keyed on ``ticket``
        (the gateway route validates ``data.get("ticket")``). A v1
        bug shipped with the field named ``key``, which the route
        rejected as ``invalid ticket shape``. This test pins the
        orchestrator → gateway contract so any future drift surfaces
        immediately, even without an integration test against the
        live gateway.

        Captures the (path, body) pair the helper sends and asserts
        both the route path and the body field name.
        """
        captured: list[tuple[str, dict]] = []

        def _capture(path, body):
            captured.append((path, body))
            return {"data": {"remotelinks": []}}

        monkeypatch.setattr(jira_reassess, "_gateway_post", _capture)
        fetch_remote_links("ENG-1")
        assert len(captured) == 1
        path, body = captured[0]
        assert path == "/api/v1/jira/ticket/remotelinks"
        # The route validates ``ticket`` exactly — do not weaken this
        # assertion to ``"key" in body or "ticket" in body`` because
        # that would re-introduce the v1 bug.
        assert body == {"key": "ENG-1"} or body == {"ticket": "ENG-1"}, (
            f"fetch_remote_links must POST with field name 'ticket' "
            f"(or 'key' if the route accepts both) — got {body!r}"
        )
        # Strict-mode assertion: the production contract is 'ticket'
        # (matches the route's `_JIRA_TICKET_KEY_RE.fullmatch(ticket)`
        # validation). A regression to 'key' alone fails this branch
        # because the route returns 400.
        assert "ticket" in body, (
            f"fetch_remote_links must POST {{'ticket': <KEY>}} to match "
            f"the gateway route's body parser; got {body!r}. The v1 "
            f"bug used 'key' instead of 'ticket' — see reviewer_code "
            f"v1 finding #3."
        )


# -----------------------------------------------------------------------------
# run_reassess_sweep — end-to-end orchestration
# -----------------------------------------------------------------------------


class TestRunReassessSweep:
    """Exercises the sweep against a mocked ``_gateway_post``.

    These tests cover the acceptance criteria for task-2-1:
      - Helper unit-tested against a mocked gateway response covering
        all three classes.
      - JQL passes ``gateway/jira_search.py`` extractor — exercised by
        asserting the JQL the sweep emits is well-formed.
      - Sweep result + Done-children handoff files land in the agent-
        outputs path (covered in ``TestSerialiseSweepToDisk``).

    Done children are split off into ``result.done`` and are excluded
    from ``result.children`` so the planner prompt doesn't see them
    (decision-5).
    """

    def test_empty_epic_key_returns_empty_result(self):
        result = run_reassess_sweep(epic_key="")
        assert result.epic_key == ""
        assert result.children == []
        assert result.done == []

    def test_project_derived_from_key(self, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_post(path, body):
            captured["path"] = path
            captured["body"] = body
            return {"issues": []}

        monkeypatch.setattr(jira_reassess, "_gateway_post", _fake_post)
        result = run_reassess_sweep(epic_key="ENG-1234")
        assert result.project == "ENG"
        assert captured["path"] == "/api/v1/jira/search"
        assert captured["body"]["jql"] == "project = ENG AND parent = ENG-1234"

    def test_project_explicit_override(self, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_post(path, body):
            captured["body"] = body
            return {"issues": []}

        monkeypatch.setattr(jira_reassess, "_gateway_post", _fake_post)
        run_reassess_sweep(epic_key="ENG-1234", project="OTHER")
        assert "project = OTHER AND parent = ENG-1234" in captured["body"]["jql"]

    def test_unparseable_epic_key_returns_warning(self, monkeypatch):
        """Adversarial: an epic key with no '-' segment can't yield a
        project. The sweep must warn rather than emit malformed JQL."""

        def _fail(path, body):
            pytest.fail("gateway should not be called for unparseable key")

        monkeypatch.setattr(jira_reassess, "_gateway_post", _fail)
        result = run_reassess_sweep(epic_key="MALFORMED")
        assert result.project == ""
        assert any("project" in w for w in result.warnings)

    def test_transport_error_returns_warning(self, monkeypatch):
        def _raise(path, body):
            raise OSError("connection refused")

        monkeypatch.setattr(jira_reassess, "_gateway_post", _raise)
        result = run_reassess_sweep(epic_key="ENG-1")
        assert result.children == []
        assert any("jql_search_failed" in w for w in result.warnings)

    def test_classification_done_path(self, monkeypatch):
        """A Done child lands in ``result.done`` and is NOT in
        ``result.children``."""
        sample = {
            "issues": [
                {
                    "key": "ENG-2",
                    "fields": {
                        "summary": "Already shipped",
                        "status": {
                            "name": "Done",
                            "statusCategory": {"key": "done"},
                        },
                    },
                }
            ]
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)
        # Disable remotelinks fetch so we don't need to mock another seam.
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert len(result.done) == 1
        assert result.done[0].key == "ENG-2"
        assert result.done[0].classification == "done"
        assert result.children == []

    def test_classification_updatable_path(self, monkeypatch):
        sample = {
            "issues": [
                {
                    "key": "ENG-3",
                    "fields": {
                        "summary": "New work",
                        "status": {
                            "name": "To Do",
                            "statusCategory": {"key": "new"},
                        },
                    },
                }
            ]
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert len(result.children) == 1
        assert result.children[0].classification == "updatable"
        assert result.done == []

    def test_classification_in_flight_via_status(self, monkeypatch):
        """statusCategory.indeterminate → in_flight."""
        sample = {
            "issues": [
                {
                    "key": "ENG-4",
                    "fields": {
                        "summary": "In progress",
                        "status": {
                            "name": "In Progress",
                            "statusCategory": {"key": "indeterminate"},
                        },
                    },
                }
            ]
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert len(result.children) == 1
        assert result.children[0].classification == "in_flight"
        assert result.children[0].in_flight is True
        assert "status_category=indeterminate" in result.children[0].in_flight_evidence

    def test_done_terminal_never_flips_to_in_flight(self, monkeypatch):
        """Adversarial: a Done child with an open PR remote-link still
        classifies as ``done`` (decision-5)."""

        sample = {
            "issues": [
                {
                    "key": "ENG-5",
                    "fields": {
                        "summary": "Done with stale PR link",
                        "status": {
                            "name": "Done",
                            "statusCategory": {"key": "done"},
                        },
                    },
                }
            ]
        }

        call_log: list[str] = []

        def _fake_post(path, body):
            call_log.append(path)
            if path == "/api/v1/jira/search":
                return sample
            return {"data": {"remotelinks": []}}

        monkeypatch.setattr(jira_reassess, "_gateway_post", _fake_post)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=True)
        # Done child went to result.done with classification 'done'.
        assert len(result.done) == 1
        assert result.done[0].classification == "done"
        # Acceptance: done children skip the remotelinks fetch
        # (check_remotelinks branch is gated on classification != 'done').
        assert "/api/v1/jira/ticket/remotelinks" not in call_log

    def test_non_dict_issue_skipped(self, monkeypatch):
        """Malformed issue entries are silently skipped (defensive)."""
        sample = {
            "issues": [
                "not a dict",
                None,
                {
                    "key": "ENG-6",
                    "fields": {
                        "summary": "Good",
                        "status": {
                            "name": "To Do",
                            "statusCategory": {"key": "new"},
                        },
                    },
                },
            ]
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert len(result.children) == 1
        assert result.children[0].key == "ENG-6"

    def test_issues_not_a_list_returns_warning(self, monkeypatch):
        """Defensive: malformed gateway response."""
        monkeypatch.setattr(
            jira_reassess,
            "_gateway_post",
            lambda p, b: {"issues": "not a list"},
        )
        result = run_reassess_sweep(epic_key="ENG-1")
        assert result.children == []
        assert "jql_search_returned_no_issues_list" in result.warnings

    def test_in_flight_via_pr_url_index(self, monkeypatch):
        """Reverse-index signal (decision-7 signal a) flips a Status-New
        child to in_flight."""
        sample = {
            "issues": [
                {
                    "key": "ENG-7",
                    "fields": {
                        "summary": "Has open PR but Atlassian status is new",
                        "status": {
                            "name": "To Do",
                            "statusCategory": {"key": "new"},
                        },
                    },
                }
            ]
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)

        # State-store reports an open PR for ENG-7.
        store = MagicMock()
        store.pipelines_for_jira_ticket.return_value = [
            MagicMock(pr_url="https://github.com/x/y/pull/1")
        ]

        result = run_reassess_sweep(
            epic_key="ENG-1",
            state_store=store,
            check_remotelinks=False,
        )
        assert len(result.children) == 1
        assert result.children[0].classification == "in_flight"
        assert (
            "egg_pipeline_pr=https://github.com/x/y/pull/1" in result.children[0].in_flight_evidence
        )

    def test_in_flight_via_remote_link(self, monkeypatch):
        """Remote-link signal (decision-7 signal b) flips status-new to
        in_flight."""
        sample = {
            "issues": [
                {
                    "key": "ENG-8",
                    "fields": {
                        "summary": "Human opened a PR",
                        "status": {
                            "name": "To Do",
                            "statusCategory": {"key": "new"},
                        },
                    },
                }
            ]
        }

        def _fake_post(path, body):
            if path == "/api/v1/jira/search":
                return sample
            assert path == "/api/v1/jira/ticket/remotelinks"
            return {
                "data": {
                    "remotelinks": [{"object": {"url": "https://github.com/jwbron/egg/pull/55"}}]
                }
            }

        monkeypatch.setattr(jira_reassess, "_gateway_post", _fake_post)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=True)
        assert len(result.children) == 1
        assert result.children[0].classification == "in_flight"
        assert any("remotelink_pr=" in e for e in result.children[0].in_flight_evidence)


class TestRunReassessSweepPaginationWarning:
    """The sweep emits a single JQL search with ``maxResults=200``.
    When the upstream reports a larger ``total``, the sweep must
    surface an explicit warning so the planner does not act on a
    silently truncated child set.
    """

    def test_total_above_page_emits_truncation_warning(self, monkeypatch):
        sample = {
            "issues": [
                {
                    "key": f"ENG-{i}",
                    "fields": {
                        "summary": f"child {i}",
                        "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    },
                }
                for i in range(2, 5)
            ],
            "total": 250,
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert any("jql_search_truncated" in w for w in result.warnings)
        assert any("250 matching children" in w for w in result.warnings)

    def test_total_within_page_no_warning(self, monkeypatch):
        sample = {
            "issues": [
                {
                    "key": "ENG-2",
                    "fields": {
                        "summary": "child",
                        "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    },
                },
            ],
            "total": 1,
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert not any("jql_search_truncated" in w for w in result.warnings)

    def test_missing_total_field_no_warning(self, monkeypatch):
        """Older Atlassian responses may omit ``total``; we must not
        falsely warn in that case."""
        sample = {
            "issues": [
                {
                    "key": "ENG-2",
                    "fields": {
                        "summary": "child",
                        "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                    },
                },
            ],
        }
        monkeypatch.setattr(jira_reassess, "_gateway_post", lambda p, b: sample)
        result = run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert not any("jql_search_truncated" in w for w in result.warnings)


class TestReassessFieldsListNoDescription:
    """The sweep no longer requests ``description`` (review feedback
    #7 / agent-mode reviewer): per ``task-planner.md``'s
    ``[mode: epic-reassess]`` block, the planner re-authors per-task
    descriptions from scratch, so the field is not load-bearing and
    Atlassian's ADF dict would just be dropped silently anyway."""

    def test_description_not_requested(self, monkeypatch):
        captured: dict[str, Any] = {}

        def _fake_post(path, body):
            captured["body"] = body
            return {"issues": []}

        monkeypatch.setattr(jira_reassess, "_gateway_post", _fake_post)
        run_reassess_sweep(epic_key="ENG-1", check_remotelinks=False)
        assert "description" not in captured["body"]["fields"]


# -----------------------------------------------------------------------------
# serialise_sweep_to_disk — file IO contract
# -----------------------------------------------------------------------------


class TestSerialiseSweepToDisk:
    """Round-trip the sweep result through the serialise helper.

    Acceptance: "Sweep result + Done-children handoff files land in
    ``.egg-state/agent-outputs/`` and the env vars point at them."
    """

    def test_writes_two_files(self, tmp_path: Path):
        result = ReassessSweepResult(
            epic_key="ENG-1",
            project="ENG",
            children=[
                ReassessChild(
                    key="ENG-2",
                    summary="Open",
                    classification="updatable",
                ),
            ],
            done=[
                ReassessChild(
                    key="ENG-3",
                    summary="Closed",
                    classification="done",
                )
            ],
        )
        sweep_path, done_path = serialise_sweep_to_disk(
            result=result,
            agent_outputs_dir=tmp_path / "out",
            pipeline_id="issue-1557-v2",
        )

        assert sweep_path.exists()
        assert done_path.exists()
        assert sweep_path.name == "issue-1557-v2-reassess-sweep.json"
        assert done_path.name == "issue-1557-v2-done-children.json"

        sweep_payload = json.loads(sweep_path.read_text())
        done_payload = json.loads(done_path.read_text())

        assert sweep_payload["epic_key"] == "ENG-1"
        assert sweep_payload["project"] == "ENG"
        # Sweep payload contains only non-done children (decision-5).
        assert [c["key"] for c in sweep_payload["children"]] == ["ENG-2"]
        # Done payload has summary-only entries (no description /
        # status_category).
        assert done_payload["done_children"] == [
            {"key": "ENG-3", "summary": "Closed", "status_name": ""}
        ]

    def test_creates_output_dir_if_missing(self, tmp_path: Path):
        nested = tmp_path / "a" / "b" / "c"
        assert not nested.exists()
        result = ReassessSweepResult(epic_key="ENG-1", project="ENG")
        sweep_path, done_path = serialise_sweep_to_disk(
            result=result,
            agent_outputs_dir=nested,
            pipeline_id="x",
        )
        assert nested.is_dir()
        assert sweep_path.exists()
        assert done_path.exists()

    def test_empty_result_writes_well_formed_json(self, tmp_path: Path):
        """An empty sweep still produces valid JSON files."""
        result = ReassessSweepResult(epic_key="ENG-1", project="ENG")
        sweep_path, done_path = serialise_sweep_to_disk(
            result=result,
            agent_outputs_dir=tmp_path,
            pipeline_id="empty",
        )
        sweep_payload = json.loads(sweep_path.read_text())
        done_payload = json.loads(done_path.read_text())
        assert sweep_payload["children"] == []
        assert done_payload["done_children"] == []


# -----------------------------------------------------------------------------
# ReassessChild dataclass — JSON-friendly shape
# -----------------------------------------------------------------------------


class TestReassessChildShape:
    """The dataclass must asdict cleanly so the planner prompt can
    consume it without extra translation."""

    def test_asdict_default_values(self):
        child = ReassessChild(key="ENG-1", summary="x")
        data = asdict(child)
        # Verify exhaustive shape so a future field rename breaks loudly.
        assert set(data.keys()) == {
            "key",
            "summary",
            "status_name",
            "status_category",
            "classification",
            "in_flight",
            "in_flight_evidence",
            "description",
        }
        # Defaults that the planner prompt template relies on:
        assert data["classification"] == "updatable"
        assert data["in_flight"] is False
        assert data["in_flight_evidence"] == []
        assert data["description"] == ""

    def test_evidence_default_is_isolated_per_instance(self):
        """Defensive: ``field(default_factory=list)`` so multiple
        instances don't share one list."""
        c1 = ReassessChild(key="A", summary="")
        c2 = ReassessChild(key="B", summary="")
        c1.in_flight_evidence.append("x")
        assert c2.in_flight_evidence == []


# -----------------------------------------------------------------------------
# Test helpers
# -----------------------------------------------------------------------------


class _PatchGatewayPost:
    """Context manager that swaps ``jira_reassess._gateway_post`` with a
    constant-return shim. Used by the fetch_remote_links happy-path
    tests to avoid setting up monkeypatch fixtures manually."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self._orig: Any = None

    def __enter__(self) -> None:
        self._orig = jira_reassess._gateway_post
        jira_reassess._gateway_post = lambda p, b: self._response  # type: ignore[assignment]

    def __exit__(self, *exc: object) -> None:
        jira_reassess._gateway_post = self._orig  # type: ignore[assignment]


def _patch_gateway_post(response: dict[str, Any]) -> _PatchGatewayPost:
    return _PatchGatewayPost(response)

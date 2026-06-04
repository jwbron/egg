"""Unit tests for egg_agent_tools.handlers.jira (#2994).

The handlers build the gateway body (snake_case args → camelCase fields)
and POST it via ``gateway_data_request`` (patched here).  We assert the
endpoint + body, required-field validation, list normalisation, the
edit-labels mutual-exclusion rule, and the notify default.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import jira  # noqa: E402
from egg_agent_tools.handlers.errors import HandlerError  # noqa: E402


def _patch_gateway(return_value=None):
    return patch(
        "egg_agent_tools.handlers.jira.gateway_data_request",
        return_value=return_value if return_value is not None else {"ok": True},
    )


class TestTicketGet:
    def test_minimal(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_get({"ticket": "ENG-1"})
        assert gw.call_args.args[0] == "/api/v1/jira/ticket/get"
        assert gw.call_args.kwargs["body"] == {"ticket": "ENG-1"}

    def test_fields_list(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_get({"ticket": "ENG-1", "fields": ["summary", "status"]})
        assert gw.call_args.kwargs["body"]["fields"] == ["summary", "status"]

    def test_fields_csv_string(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_get({"ticket": "ENG-1", "fields": "summary, status"})
        assert gw.call_args.kwargs["body"]["fields"] == ["summary", "status"]

    def test_ticket_required(self):
        with pytest.raises(HandlerError):
            jira.jira_ticket_get({})


class TestTicketCommentsAndRemoteLinks:
    def test_comments_endpoint(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_comments({"ticket": "ENG-2"})
        assert gw.call_args.args[0] == "/api/v1/jira/ticket/comments"
        assert gw.call_args.kwargs["body"] == {"ticket": "ENG-2"}

    def test_remotelinks_endpoint(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_remotelinks({"ticket": "ENG-2"})
        assert gw.call_args.args[0] == "/api/v1/jira/ticket/remotelinks"


class TestSearch:
    def test_jql_required(self):
        with pytest.raises(HandlerError):
            jira.jira_search({})

    def test_translates_camel(self):
        with _patch_gateway() as gw:
            jira.jira_search(
                {
                    "jql": "project = ENG",
                    "max_results": 50,
                    "fields": ["summary"],
                    "next_page_token": "tok",
                }
            )
        body = gw.call_args.kwargs["body"]
        assert gw.call_args.args[0] == "/api/v1/jira/search"
        assert body == {
            "jql": "project = ENG",
            "maxResults": 50,
            "fields": ["summary"],
            "nextPageToken": "tok",
        }


class TestTicketCreate:
    def test_required_fields(self):
        with pytest.raises(HandlerError):
            jira.jira_ticket_create({"project": "ENG", "summary": "x"})  # missing issue_type

    def test_full_body(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_create(
                {
                    "project": "ENG",
                    "issue_type": "Task",
                    "summary": "Do thing",
                    "description": "details",
                    "labels": ["a", "b"],
                    "parent": "ENG-1",
                    "epic_link": "ENG-9",
                    "idempotency_key": "k1",
                }
            )
        body = gw.call_args.kwargs["body"]
        assert gw.call_args.args[0] == "/api/v1/jira/ticket/create"
        assert body == {
            "project": "ENG",
            "issuetype": "Task",
            "summary": "Do thing",
            "description": "details",
            "labels": ["a", "b"],
            "parent": "ENG-1",
            "epicLink": "ENG-9",
            "idempotencyKey": "k1",
        }


class TestTicketEdit:
    def test_notify_default_false(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_edit({"ticket": "ENG-1", "summary": "new"})
        body = gw.call_args.kwargs["body"]
        assert body["notifyUsers"] is False
        assert body["summary"] == "new"

    def test_notify_true(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_edit({"ticket": "ENG-1", "notify_users": True})
        assert gw.call_args.kwargs["body"]["notifyUsers"] is True

    def test_incremental_labels(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_edit({"ticket": "ENG-1", "add_labels": ["x"], "remove_labels": ["y"]})
        body = gw.call_args.kwargs["body"]
        assert body["addLabels"] == ["x"]
        assert body["removeLabels"] == ["y"]

    def test_replace_and_incremental_mutually_exclusive(self):
        with pytest.raises(HandlerError):
            jira.jira_ticket_edit({"ticket": "ENG-1", "labels": ["a"], "add_labels": ["b"]})


class TestCommentAdd:
    def test_body_required(self):
        with pytest.raises(HandlerError):
            jira.jira_ticket_comment_add({"ticket": "ENG-1"})

    def test_body(self):
        with _patch_gateway() as gw:
            jira.jira_ticket_comment_add({"ticket": "ENG-1", "body": "hi", "idempotency_key": "k"})
        body = gw.call_args.kwargs["body"]
        assert gw.call_args.args[0] == "/api/v1/jira/ticket/comment/add"
        assert body == {"ticket": "ENG-1", "body": "hi", "idempotencyKey": "k"}


class TestLinkCreate:
    def test_required(self):
        with pytest.raises(HandlerError):
            jira.jira_link_create({"link_type": "Blocks", "inward_issue": "A-1"})

    def test_body(self):
        with _patch_gateway() as gw:
            jira.jira_link_create(
                {
                    "link_type": "Blocks",
                    "inward_issue": "A-1",
                    "outward_issue": "A-2",
                    "comment": "see",
                }
            )
        body = gw.call_args.kwargs["body"]
        assert gw.call_args.args[0] == "/api/v1/jira/issue-link/create"
        assert body == {
            "type": "Blocks",
            "inwardIssue": "A-1",
            "outwardIssue": "A-2",
            "comment": "see",
        }


class TestExecute:
    def test_query_must_be_object(self):
        with pytest.raises(HandlerError):
            jira.jira_execute({"method": "GET", "path": "/x", "query": "bad"})

    def test_body(self):
        with _patch_gateway() as gw:
            jira.jira_execute({"method": "GET", "path": "/rest/api/3/myself"})
        assert gw.call_args.args[0] == "/api/v1/jira/execute"
        assert gw.call_args.kwargs["body"] == {"method": "GET", "path": "/rest/api/3/myself"}

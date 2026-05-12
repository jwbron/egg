"""Tests for ``orchestrator.jira_epic_detect`` reassess helpers (TASK-1-3).

Covers:

* :func:`_normalise_children_payload` — gateway-envelope handling +
  adversarial / malformed search responses.
* :func:`_run_jql` — single-query dispatch and the per-query
  ``tolerate_400`` behavior.
* :func:`search_epic_children` — the architect-noted (ad-9) two-query
  JQL pattern (``parent =`` + ``"Epic Link" =``), result merging /
  deduplication, per-query 400 tolerance (Epic Link 400 swallowed,
  ``parent =`` 400 propagated), ``require_hierarchy_mapping=True``
  re-raise of :class:`JiraHierarchyUnmappedError`, and the ``parent``-
  only skip path.
* :func:`resolve_effective_mode` — auto / reassess / fresh dispatch
  and validation of unknown modes.

The module's public surface accepts a ``gateway_invoker`` callable; we
mock it via :class:`MagicMock` so nothing in this suite touches the
network.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jira_epic_detect import (
    JiraEpicDetectionError,
    _normalise_children_payload,
    _run_jql,
    resolve_effective_mode,
    search_epic_children,
)
from jira_hierarchy_config import JiraHierarchyUnmappedError

# ---------------------------------------------------------------------------
# _normalise_children_payload
# ---------------------------------------------------------------------------


class TestNormaliseChildrenPayload:
    def test_direct_issues_list(self):
        body = {"issues": [{"key": "ENG-2"}, {"key": "ENG-3"}]}
        assert _normalise_children_payload(body) == [
            {"key": "ENG-2"},
            {"key": "ENG-3"},
        ]

    def test_envelope_data_issues(self):
        body = {"data": {"issues": [{"key": "ENG-7"}]}}
        assert _normalise_children_payload(body) == [{"key": "ENG-7"}]

    def test_missing_issues_returns_empty(self):
        assert _normalise_children_payload({}) == []

    def test_non_list_issues_returns_empty(self):
        # Adversarial: ``issues`` is the wrong type — degrade gracefully.
        assert _normalise_children_payload({"issues": "not a list"}) == []
        assert _normalise_children_payload({"issues": None}) == []
        assert _normalise_children_payload({"issues": {"key": "ENG-1"}}) == []

    def test_non_dict_items_filtered(self):
        body = {"issues": [{"key": "ENG-1"}, "junk", None, 42, {"key": "ENG-2"}]}
        assert _normalise_children_payload(body) == [
            {"key": "ENG-1"},
            {"key": "ENG-2"},
        ]

    def test_empty_issues_list(self):
        assert _normalise_children_payload({"issues": []}) == []


# ---------------------------------------------------------------------------
# _run_jql
# ---------------------------------------------------------------------------


class TestRunJql:
    def test_happy_path_dispatches_post_with_payload(self):
        invoker = MagicMock()
        invoker.return_value = {"issues": [{"key": "ENG-1"}]}
        result = _run_jql('parent = "ENG-10"', gateway_invoker=invoker)
        assert result == [{"key": "ENG-1"}]
        invoker.assert_called_once()
        assert invoker.call_args.args[0] == "/api/v1/jira/search"
        assert invoker.call_args.kwargs["method"] == "POST"
        # ``fields`` omitted when not provided.
        assert invoker.call_args.kwargs["data"] == {"jql": 'parent = "ENG-10"'}

    def test_fields_projection_included(self):
        invoker = MagicMock()
        invoker.return_value = {"issues": []}
        _run_jql(
            'parent = "ENG-1"',
            gateway_invoker=invoker,
            fields=["status", "issuetype"],
        )
        assert invoker.call_args.kwargs["data"] == {
            "jql": 'parent = "ENG-1"',
            "fields": ["status", "issuetype"],
        }

    def test_400_tolerated_returns_none(self):
        class GatewayError(Exception):
            status_code = 400

        invoker = MagicMock()
        invoker.side_effect = GatewayError("Epic Link field unknown")
        result = _run_jql(
            '"Epic Link" = "ENG-1"',
            gateway_invoker=invoker,
            tolerate_400=True,
        )
        assert result is None

    def test_400_not_tolerated_propagates(self):
        class GatewayError(Exception):
            status_code = 400

        invoker = MagicMock()
        invoker.side_effect = GatewayError("malformed jql")
        with pytest.raises(GatewayError):
            _run_jql(
                'parent = "ENG-1"',
                gateway_invoker=invoker,
                tolerate_400=False,
            )

    def test_500_propagates_even_when_tolerate_400(self):
        # ``tolerate_400`` only swallows status_code == 400; everything
        # else surfaces.
        class GatewayError(Exception):
            status_code = 500

        invoker = MagicMock()
        invoker.side_effect = GatewayError("upstream broke")
        with pytest.raises(GatewayError):
            _run_jql(
                '"Epic Link" = "ENG-1"',
                gateway_invoker=invoker,
                tolerate_400=True,
            )

    def test_default_tolerate_400_is_false(self):
        class GatewayError(Exception):
            status_code = 400

        invoker = MagicMock()
        invoker.side_effect = GatewayError("default rejects 400")
        with pytest.raises(GatewayError):
            _run_jql('parent = "ENG-1"', gateway_invoker=invoker)


# ---------------------------------------------------------------------------
# search_epic_children — two-query merge / 400 tolerance
# ---------------------------------------------------------------------------


class TestSearchEpicChildren:
    @staticmethod
    def _unmapped_hierarchy(*_a, **_kw):
        raise JiraHierarchyUnmappedError("ENG", Path("/dev/null"))

    def test_two_queries_dispatched_and_merged(self):
        # parent= returns A, B ; Epic Link= returns B, C — merged = A, B, C
        responses = [
            {"issues": [{"key": "ENG-2"}, {"key": "ENG-3"}]},
            {"issues": [{"key": "ENG-3"}, {"key": "ENG-4"}]},
        ]
        invoker = MagicMock()
        invoker.side_effect = responses
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            result = search_epic_children("ENG-1", gateway_invoker=invoker)

        assert invoker.call_count == 2
        # Verify both JQL queries were dispatched.
        jqls = [c.kwargs["data"]["jql"] for c in invoker.call_args_list]
        assert 'parent = "ENG-1"' in jqls
        assert '"Epic Link" = "ENG-1"' in jqls

        # Deduplicated by key, parent-result wins on duplicates.
        keys = sorted(item["key"] for item in result)
        assert keys == ["ENG-2", "ENG-3", "ENG-4"]

    def test_epic_link_400_tolerated_parent_results_returned(self):
        # parent= returns A; Epic Link= 400s; result is just A.
        class GatewayError(Exception):
            status_code = 400

        parent_response = {"issues": [{"key": "ENG-2"}]}

        def side_effect(endpoint, **kwargs):
            jql = kwargs["data"]["jql"]
            if jql.startswith("parent"):
                return parent_response
            raise GatewayError("Epic Link unknown")

        invoker = MagicMock(side_effect=side_effect)
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            result = search_epic_children("ENG-1", gateway_invoker=invoker)

        assert result == [{"key": "ENG-2"}]

    def test_parent_400_propagates(self):
        # ``parent =`` 400 is a real Jira error and must surface.
        class GatewayError(Exception):
            status_code = 400

        def side_effect(endpoint, **kwargs):
            jql = kwargs["data"]["jql"]
            if jql.startswith("parent"):
                raise GatewayError("malformed jql")
            return {"issues": []}

        invoker = MagicMock(side_effect=side_effect)
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            with pytest.raises(GatewayError):
                search_epic_children("ENG-1", gateway_invoker=invoker)

    def test_require_hierarchy_mapping_true_reraises(self):
        invoker = MagicMock()
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            with pytest.raises(JiraHierarchyUnmappedError):
                search_epic_children(
                    "ENG-1",
                    gateway_invoker=invoker,
                    require_hierarchy_mapping=True,
                )
        # No JQL was dispatched — we bailed early.
        invoker.assert_not_called()

    def test_require_hierarchy_mapping_false_runs_both_queries(self):
        # Default behavior on detection-probe path.
        invoker = MagicMock()
        invoker.return_value = {"issues": []}
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            result = search_epic_children(
                "ENG-1",
                gateway_invoker=invoker,
                require_hierarchy_mapping=False,
            )
        assert result == []
        assert invoker.call_count == 2

    def test_hierarchy_parent_field_skips_epic_link_query(self):
        # When the YAML says project uses ``parent``, the ``Epic Link``
        # query is skipped entirely.
        invoker = MagicMock()
        invoker.return_value = {"issues": [{"key": "ENG-2"}]}
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            return_value="parent",
        ):
            result = search_epic_children("ENG-1", gateway_invoker=invoker)

        assert invoker.call_count == 1
        assert invoker.call_args.kwargs["data"]["jql"] == 'parent = "ENG-1"'
        assert result == [{"key": "ENG-2"}]

    def test_hierarchy_epic_link_field_runs_both_queries(self):
        # When the YAML maps the project to ``epic_link``, both queries
        # still fire so the merge handles any stragglers.
        invoker = MagicMock()
        invoker.return_value = {"issues": []}
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            return_value="epic_link",
        ):
            search_epic_children("ENG-1", gateway_invoker=invoker)
        assert invoker.call_count == 2

    def test_results_deduplicated_by_key(self):
        # Both queries return the same key — result has one entry.
        responses = [
            {"issues": [{"key": "ENG-9", "fields": {"status": "Open"}}]},
            {"issues": [{"key": "ENG-9", "fields": {"status": "Done"}}]},
        ]
        invoker = MagicMock(side_effect=responses)
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            result = search_epic_children("ENG-1", gateway_invoker=invoker)
        assert len(result) == 1
        assert result[0]["key"] == "ENG-9"
        # First-write-wins: parent query (issued first) keeps its payload.
        assert result[0]["fields"]["status"] == "Open"

    def test_no_children_returns_empty_list(self):
        invoker = MagicMock()
        invoker.return_value = {"issues": []}
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            result = search_epic_children("ENG-1", gateway_invoker=invoker)
        assert result == []

    def test_malformed_response_missing_issues_treated_as_empty(self):
        # Adversarial: response missing ``issues`` — treated as empty.
        invoker = MagicMock()
        invoker.return_value = {"weird": "shape"}
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            side_effect=self._unmapped_hierarchy,
        ):
            result = search_epic_children("ENG-1", gateway_invoker=invoker)
        assert result == []

    def test_explicit_project_key_skips_inference(self):
        # When the caller supplies project_key, the helper doesn't have
        # to parse ``epic_key``.
        invoker = MagicMock()
        invoker.return_value = {"issues": []}
        with patch(
            "jira_epic_detect.resolve_hierarchy_field",
            return_value="parent",
        ) as mock_resolve:
            search_epic_children(
                "ENG-1",
                project_key="OVERRIDE",
                gateway_invoker=invoker,
            )
        mock_resolve.assert_called_once_with("OVERRIDE")


# ---------------------------------------------------------------------------
# resolve_effective_mode
# ---------------------------------------------------------------------------


class TestResolveEffectiveMode:
    @staticmethod
    def _patch_search(children):
        """Patch ``search_epic_children`` to return ``children``."""
        return patch(
            "jira_epic_detect.search_epic_children",
            return_value=children,
        )

    def test_invalid_mode_raises(self):
        with pytest.raises(JiraEpicDetectionError) as exc:
            resolve_effective_mode(
                "invalid",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert "auto" in str(exc.value)
        assert "reassess" in str(exc.value)
        assert "fresh" in str(exc.value)

    def test_empty_string_mode_raises(self):
        with pytest.raises(JiraEpicDetectionError):
            resolve_effective_mode(
                "",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )

    def test_auto_with_children_returns_reassess(self):
        with self._patch_search([{"key": "ENG-2"}, {"key": "ENG-3"}]):
            mode, children = resolve_effective_mode(
                "auto",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert mode == "reassess"
        assert len(children) == 2

    def test_auto_with_no_children_returns_fresh(self):
        with self._patch_search([]):
            mode, children = resolve_effective_mode(
                "auto",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert mode == "fresh"
        assert children == []

    def test_reassess_with_children_stays_reassess(self):
        with self._patch_search([{"key": "ENG-2"}]):
            mode, _ = resolve_effective_mode(
                "reassess",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert mode == "reassess"

    def test_reassess_with_no_children_degrades_to_fresh(self):
        # Decision-12: reassess on a childless epic degrades to fresh.
        with self._patch_search([]):
            mode, children = resolve_effective_mode(
                "reassess",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert mode == "fresh"
        assert children == []

    def test_fresh_with_children_stays_fresh(self):
        # Operator asked for fresh explicitly — honor it even if children
        # exist.
        with self._patch_search([{"key": "ENG-2"}, {"key": "ENG-3"}]):
            mode, children = resolve_effective_mode(
                "fresh",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert mode == "fresh"
        # Children list is still returned so callers can persist it.
        assert len(children) == 2

    def test_fresh_with_no_children_returns_fresh(self):
        with self._patch_search([]):
            mode, _ = resolve_effective_mode(
                "fresh",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert mode == "fresh"

    def test_search_called_with_field_projection(self):
        # The reassess decision only needs status / issuetype — verify
        # the helper passes that projection through (cheap-probe contract).
        invoker = MagicMock()
        with patch(
            "jira_epic_detect.search_epic_children",
            return_value=[],
        ) as mock_search:
            resolve_effective_mode(
                "auto",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=invoker,
            )
        mock_search.assert_called_once()
        kwargs = mock_search.call_args.kwargs
        assert kwargs["fields"] == ["status", "issuetype"]
        assert kwargs["project_key"] == "ENG"
        assert kwargs["gateway_invoker"] is invoker

    def test_returned_children_match_search_result(self):
        sentinel = [{"key": "ENG-X", "fields": {"status": "In Progress"}}]
        with self._patch_search(sentinel):
            _, children = resolve_effective_mode(
                "auto",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )
        assert children == sentinel

    def test_uppercase_mode_rejected(self):
        # The mode is case-sensitive — only the lowercase literals are accepted.
        with pytest.raises(JiraEpicDetectionError):
            resolve_effective_mode(
                "AUTO",
                epic_key="ENG-1",
                project_key="ENG",
                gateway_invoker=MagicMock(),
            )

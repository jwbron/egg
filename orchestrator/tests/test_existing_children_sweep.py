"""Tests for ``orchestrator/jira_existing_children.py`` (issue #1557).

Covers the existing-children sweep used by the Jira-epic reassess flow:

* ``_classify_by_status`` — status-bucket classification with InFlightSignal
  emission for actively-worked statuses.
* ``sweep_existing_children`` — happy path + decision-8 OR-semantics
  in-flight detection across the three signal sources (Jira status,
  orchestrator pr_url artifact, remote-link GitHub PR URL).
* ``_load_reverse_index`` / ``update_reverse_index`` — round-trip
  semantics, ``os.replace`` crash-atomicity, and the ``threading.Lock``
  concurrent-writer smoke test (reviewer_concurrency v3 finding).
* Adversarial: corrupted JSON index, missing pipeline file, missing
  ``remote_links`` envelope.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from jira_existing_children import (
    DEFAULT_INDEX_PATH,
    ExistingChild,
    InFlightSignal,
    _check_orchestrator_pr_signal,
    _check_remote_link_signal,
    _classify_by_status,
    _load_reverse_index,
    sweep_existing_children,
    update_reverse_index,
)

# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _issue(key: str, status: str, *, summary: str = "", description: str = "") -> dict:
    """Build a Jira ``search`` issue payload with the fields the sweep reads."""
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status},
            "description": description,
        },
    }


def _make_search_invoker(children: list[dict], remotelinks: dict[str, list[dict]] | None = None):
    """Build a fake gateway invoker that returns the canned JQL+remote-link data.

    The sweep calls the gateway twice per child for remote-link signals
    plus once per JQL bucket (``parent`` / ``Epic Link``) inside
    ``search_epic_children``.  We don't need to faithfully reproduce
    every code path — patching ``search_epic_children`` is simpler.
    """
    remotelinks = remotelinks or {}

    def invoker(path: str, *, method: str = "POST", data: dict | None = None, **_):
        if path == "/api/v1/jira/ticket/remotelinks":
            ticket = (data or {}).get("ticket")
            return {"data": {"remoteLinks": remotelinks.get(ticket, [])}}
        # Default empty JQL response — sweep_existing_children uses the
        # ``search_epic_children`` helper which we patch separately.
        return {"data": {"issues": children}}

    return invoker


# ---------------------------------------------------------------------------
# _classify_by_status
# ---------------------------------------------------------------------------


class TestClassifyByStatus:
    def test_done_status_returns_done_no_signal(self):
        cls, signal = _classify_by_status("Done")
        assert cls == "done"
        assert signal is None

    def test_closed_status_returns_done(self):
        cls, signal = _classify_by_status("Closed")
        assert cls == "done"
        assert signal is None

    def test_resolved_status_returns_done(self):
        cls, signal = _classify_by_status("Resolved")
        assert cls == "done"
        assert signal is None

    def test_in_progress_returns_in_flight_with_signal(self):
        cls, signal = _classify_by_status("In Progress")
        assert cls == "in_flight"
        assert signal == InFlightSignal(source="jira_status", detail="In Progress")

    def test_in_review_returns_in_flight(self):
        cls, signal = _classify_by_status("In Review")
        assert cls == "in_flight"
        assert signal is not None
        assert signal.source == "jira_status"

    def test_code_review_returns_in_flight(self):
        cls, signal = _classify_by_status("Code Review")
        assert cls == "in_flight"
        assert signal is not None
        assert signal.detail == "Code Review"

    def test_blocked_returns_in_flight(self):
        cls, signal = _classify_by_status("Blocked")
        assert cls == "in_flight"
        assert signal is not None

    def test_to_do_returns_to_do(self):
        cls, signal = _classify_by_status("To Do")
        assert cls == "to_do"
        assert signal is None

    def test_open_returns_to_do(self):
        cls, signal = _classify_by_status("Open")
        assert cls == "to_do"
        assert signal is None

    def test_backlog_returns_to_do(self):
        cls, signal = _classify_by_status("Backlog")
        assert cls == "to_do"
        assert signal is None

    def test_unknown_status_defaults_to_to_do(self):
        cls, signal = _classify_by_status("Not-A-Real-Status")
        assert cls == "to_do"
        assert signal is None

    def test_status_match_is_case_insensitive(self):
        # Operators customise Jira statuses; the matcher lower-cases first.
        cls, _ = _classify_by_status("DONE")
        assert cls == "done"
        cls, _ = _classify_by_status("in PROGRESS")
        assert cls == "in_flight"


# ---------------------------------------------------------------------------
# sweep_existing_children happy path + signal precedence
# ---------------------------------------------------------------------------


class TestSweepExistingChildren:
    def test_happy_path_classifies_children(self, tmp_path: Path):
        children = [
            _issue("PROJ-1", "Done", summary="finished"),
            _issue("PROJ-2", "To Do", summary="not started"),
            _issue("PROJ-3", "In Progress", summary="working", description="body"),
        ]
        invoker = _make_search_invoker(children)
        with patch("jira_existing_children.search_epic_children", return_value=children):
            results = sweep_existing_children(
                "PROJ-100",
                gateway_invoker=invoker,
                repo_path=tmp_path,
            )

        assert {c.key for c in results} == {"PROJ-1", "PROJ-2", "PROJ-3"}
        by_key = {c.key: c for c in results}
        assert by_key["PROJ-1"].classification == "done"
        assert by_key["PROJ-2"].classification == "to_do"
        assert by_key["PROJ-3"].classification == "in_flight"
        # In-flight child carries a jira_status signal.
        assert any(s.source == "jira_status" for s in by_key["PROJ-3"].in_flight_signals)
        # Fields propagate.
        assert by_key["PROJ-3"].summary == "working"
        assert by_key["PROJ-3"].description == "body"
        assert by_key["PROJ-1"].summary == "finished"

    def test_jira_status_signal_takes_precedence(self, tmp_path: Path):
        # When the Jira status already says "in flight", that signal is
        # the FIRST entry on the in_flight_signals tuple — the orchestrator
        # and remote-link checks still also run on non-done children
        # (decision-8 OR semantics).
        children = [_issue("PROJ-10", "In Progress")]
        invoker = _make_search_invoker(children)
        with patch("jira_existing_children.search_epic_children", return_value=children):
            results = sweep_existing_children(
                "PROJ-EPIC",
                gateway_invoker=invoker,
                repo_path=tmp_path,
            )

        assert len(results) == 1
        signals = results[0].in_flight_signals
        assert signals[0].source == "jira_status"

    def test_orchestrator_pr_url_signal_promotes_to_do_to_in_flight(self, tmp_path: Path):
        # A "To Do" child whose reverse-index points at a pipeline with
        # phases.pr.artifacts.pr_url should be reclassified in_flight.
        index_path = tmp_path / ".egg-state" / "jira-child-pipeline-index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps({"PROJ-20": ["pipeline-abc"]}))

        pipeline_file = tmp_path / ".egg-state" / "pipelines" / "pipeline-abc.json"
        pipeline_file.parent.mkdir(parents=True, exist_ok=True)
        pipeline_file.write_text(
            json.dumps(
                {"phases": {"pr": {"artifacts": {"pr_url": "https://github.com/o/r/pull/42"}}}}
            )
        )

        children = [_issue("PROJ-20", "To Do")]
        invoker = _make_search_invoker(children)
        with patch("jira_existing_children.search_epic_children", return_value=children):
            results = sweep_existing_children(
                "PROJ-EPIC",
                gateway_invoker=invoker,
                repo_path=tmp_path,
            )

        assert len(results) == 1
        assert results[0].classification == "in_flight"
        sources = [s.source for s in results[0].in_flight_signals]
        assert "orchestrator_pr_url" in sources

    def test_remote_link_signal_promotes_to_do_to_in_flight(self, tmp_path: Path):
        children = [_issue("PROJ-30", "To Do")]
        remotelinks = {
            "PROJ-30": [
                {"object": {"url": "https://github.com/owner/repo/pull/77"}},
            ],
        }
        invoker = _make_search_invoker(children, remotelinks=remotelinks)
        with patch("jira_existing_children.search_epic_children", return_value=children):
            results = sweep_existing_children(
                "PROJ-EPIC",
                gateway_invoker=invoker,
                repo_path=tmp_path,
            )

        assert len(results) == 1
        assert results[0].classification == "in_flight"
        sources = [s.source for s in results[0].in_flight_signals]
        assert "remote_link" in sources

    def test_done_child_with_stale_pr_link_remains_done(self, tmp_path: Path):
        # Per the source comment: "A 'Done' child with a stale GitHub PR
        # remote-link is still Done."  The cross-check is skipped.
        children = [_issue("PROJ-40", "Done")]
        remotelinks = {
            "PROJ-40": [
                {"object": {"url": "https://github.com/owner/repo/pull/99"}},
            ],
        }
        invoker = _make_search_invoker(children, remotelinks=remotelinks)
        with patch("jira_existing_children.search_epic_children", return_value=children):
            results = sweep_existing_children(
                "PROJ-EPIC",
                gateway_invoker=invoker,
                repo_path=tmp_path,
            )

        assert len(results) == 1
        assert results[0].classification == "done"
        assert results[0].in_flight_signals == ()

    def test_multiple_signals_or_together(self, tmp_path: Path):
        # decision-8: signals are OR'ed and ALL firing ones are recorded.
        index_path = tmp_path / ".egg-state" / "jira-child-pipeline-index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps({"PROJ-50": ["p-x"]}))
        pipeline_file = tmp_path / ".egg-state" / "pipelines" / "p-x.json"
        pipeline_file.parent.mkdir(parents=True, exist_ok=True)
        pipeline_file.write_text(
            json.dumps(
                {"phases": {"pr": {"artifacts": {"pr_url": "https://github.com/o/r/pull/1"}}}}
            )
        )

        children = [_issue("PROJ-50", "In Progress")]
        remotelinks = {
            "PROJ-50": [
                {"object": {"url": "https://github.com/owner/repo/pull/123"}},
            ],
        }
        invoker = _make_search_invoker(children, remotelinks=remotelinks)
        with patch("jira_existing_children.search_epic_children", return_value=children):
            results = sweep_existing_children(
                "PROJ-EPIC",
                gateway_invoker=invoker,
                repo_path=tmp_path,
            )

        sources = [s.source for s in results[0].in_flight_signals]
        assert sources == ["jira_status", "orchestrator_pr_url", "remote_link"]


# ---------------------------------------------------------------------------
# _check_orchestrator_pr_signal / _check_remote_link_signal
# ---------------------------------------------------------------------------


class TestOrchestratorPrSignal:
    def test_returns_signal_when_index_and_pipeline_record_pr_url(self, tmp_path: Path):
        pipeline_file = tmp_path / ".egg-state" / "pipelines" / "pipe-1.json"
        pipeline_file.parent.mkdir(parents=True, exist_ok=True)
        pipeline_file.write_text(
            json.dumps(
                {"phases": {"pr": {"artifacts": {"pr_url": "https://github.com/o/r/pull/7"}}}}
            )
        )

        signal = _check_orchestrator_pr_signal("PROJ-1", tmp_path, {"PROJ-1": ["pipe-1"]})
        assert signal is not None
        assert signal.source == "orchestrator_pr_url"
        assert signal.detail == "https://github.com/o/r/pull/7"

    def test_returns_none_when_pipeline_file_missing(self, tmp_path: Path):
        signal = _check_orchestrator_pr_signal("PROJ-1", tmp_path, {"PROJ-1": ["pipe-missing"]})
        assert signal is None

    def test_returns_none_when_index_has_no_entry(self, tmp_path: Path):
        assert _check_orchestrator_pr_signal("PROJ-NEW", tmp_path, {}) is None

    def test_returns_none_when_pipeline_has_no_pr_url(self, tmp_path: Path):
        pipeline_file = tmp_path / ".egg-state" / "pipelines" / "pipe-2.json"
        pipeline_file.parent.mkdir(parents=True, exist_ok=True)
        pipeline_file.write_text(json.dumps({"phases": {"pr": {"artifacts": {}}}}))

        signal = _check_orchestrator_pr_signal("PROJ-1", tmp_path, {"PROJ-1": ["pipe-2"]})
        assert signal is None


class TestRemoteLinkSignal:
    def test_returns_signal_for_github_pr_url(self):
        def invoker(path, *, method="POST", data=None, **_):
            return {
                "data": {
                    "remoteLinks": [
                        {"object": {"url": "https://github.com/owner/repo/pull/45"}},
                    ],
                }
            }

        signal = _check_remote_link_signal("PROJ-1", gateway_invoker=invoker)
        assert signal is not None
        assert signal.source == "remote_link"
        assert "pull/45" in signal.detail

    def test_returns_none_when_remote_links_field_missing(self):
        def invoker(path, *, method="POST", data=None, **_):
            return {"data": {}}

        assert _check_remote_link_signal("PROJ-1", gateway_invoker=invoker) is None

    def test_returns_none_when_non_pr_url(self):
        def invoker(path, *, method="POST", data=None, **_):
            return {
                "data": {
                    "remoteLinks": [
                        {"object": {"url": "https://github.com/owner/repo/issues/12"}},
                    ],
                }
            }

        assert _check_remote_link_signal("PROJ-1", gateway_invoker=invoker) is None

    def test_returns_none_when_gateway_raises(self):
        def invoker(*args, **kwargs):
            raise RuntimeError("network down")

        # The error path logs a warning and returns None — it must not
        # propagate the exception out of the sweep.
        assert _check_remote_link_signal("PROJ-1", gateway_invoker=invoker) is None


# ---------------------------------------------------------------------------
# _load_reverse_index + update_reverse_index round-trip
# ---------------------------------------------------------------------------


class TestReverseIndexRoundTrip:
    def test_update_then_load_round_trip(self, tmp_path: Path):
        index_path = tmp_path / "reverse-index.json"
        update_reverse_index(tmp_path, "pipe-1", "PROJ-1", index_path=index_path)
        update_reverse_index(tmp_path, "pipe-2", "PROJ-1", index_path=index_path)
        update_reverse_index(tmp_path, "pipe-3", "PROJ-2", index_path=index_path)

        loaded = _load_reverse_index(index_path)
        assert loaded == {"PROJ-1": ["pipe-1", "pipe-2"], "PROJ-2": ["pipe-3"]}

    def test_update_is_idempotent(self, tmp_path: Path):
        index_path = tmp_path / "reverse-index.json"
        for _ in range(3):
            update_reverse_index(tmp_path, "pipe-X", "PROJ-7", index_path=index_path)
        loaded = _load_reverse_index(index_path)
        assert loaded == {"PROJ-7": ["pipe-X"]}

    def test_update_uses_os_replace_for_crash_atomic_write(self, tmp_path: Path):
        # The source documents that writes go via a sibling ``.tmp``
        # file + ``os.replace``.  Spy on os.replace to verify the final
        # rename happens.
        index_path = tmp_path / "reverse-index.json"
        with patch("jira_existing_children.os.replace", wraps=__import__("os").replace) as spy:
            update_reverse_index(tmp_path, "pipe-1", "PROJ-1", index_path=index_path)

        assert spy.call_count == 1
        # Called with (tmp_path, target).
        call = spy.call_args
        src_arg, dest_arg = call.args
        assert str(src_arg).endswith(".json.tmp")
        assert Path(dest_arg) == index_path
        # The .tmp file must not linger after the rename.
        assert not (tmp_path / "reverse-index.json.tmp").exists()
        assert index_path.exists()

    def test_load_returns_empty_when_file_missing(self, tmp_path: Path):
        assert _load_reverse_index(tmp_path / "absent.json") == {}

    def test_load_returns_empty_when_json_corrupted(self, tmp_path: Path):
        index_path = tmp_path / "broken.json"
        index_path.write_text("{not valid json")
        # The loader degrades to {} so the whole reassess flow doesn't fail.
        assert _load_reverse_index(index_path) == {}

    def test_load_returns_empty_when_top_level_not_dict(self, tmp_path: Path):
        index_path = tmp_path / "wrong-shape.json"
        index_path.write_text(json.dumps(["not", "a", "mapping"]))
        assert _load_reverse_index(index_path) == {}


# ---------------------------------------------------------------------------
# Concurrent-writer smoke test (reviewer_concurrency v3 finding)
# ---------------------------------------------------------------------------


class TestUpdateReverseIndexConcurrency:
    def test_concurrent_writers_do_not_corrupt_index(self, tmp_path: Path):
        # 20 threads racing on the same index file.  The module-level
        # threading.Lock + os.replace guards must produce a final
        # well-formed JSON file with all 20 pipeline_ids recorded against
        # PROJ-CONCURRENT.
        index_path = tmp_path / "concurrent-index.json"
        thread_count = 20

        def writer(i: int) -> None:
            update_reverse_index(
                tmp_path,
                f"pipe-{i:02d}",
                "PROJ-CONCURRENT",
                index_path=index_path,
            )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # File must parse as JSON and contain every pipeline_id.
        loaded = _load_reverse_index(index_path)
        assert set(loaded.keys()) == {"PROJ-CONCURRENT"}
        recorded = set(loaded["PROJ-CONCURRENT"])
        assert recorded == {f"pipe-{i:02d}" for i in range(thread_count)}
        # No stray tmp file left behind.
        assert not (tmp_path / "concurrent-index.json.tmp").exists()


# ---------------------------------------------------------------------------
# Misc dataclass / constants surface
# ---------------------------------------------------------------------------


class TestDataclassSurface:
    def test_existing_child_is_frozen(self):
        child = ExistingChild(
            key="PROJ-1",
            summary="x",
            status="Done",
            description="",
            classification="done",
        )
        with pytest.raises(AttributeError):
            child.key = "PROJ-2"  # type: ignore[misc]

    def test_in_flight_signal_is_frozen(self):
        sig = InFlightSignal(source="jira_status", detail="In Progress")
        with pytest.raises(AttributeError):
            sig.detail = "other"  # type: ignore[misc]

    def test_default_index_path_is_under_egg_state(self):
        # Sanity-check the constant other modules pin against.
        assert DEFAULT_INDEX_PATH == Path(".egg-state/jira-child-pipeline-index.json")

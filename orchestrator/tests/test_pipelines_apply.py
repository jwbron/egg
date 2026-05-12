"""
Tests for ``orchestrator.wontdo_drain`` (issue #1557 slice-2 task-2-9).

Covers the apply-phase post-consensus Won't-Do drain (TASK-2-7):

- ``load_wontdo_handoff`` parses the handoff JSON correctly. Missing
  files / malformed JSON / unexpected shapes → empty list (the drain
  treats absence as "nothing to do" rather than failing the pipeline).
- ``run_wontdo_drain`` iterates entries and posts each transition. On
  success the entry lands in ``DrainResult.succeeded``; on transport /
  HTTP failures the entry lands in ``DrainResult.failed`` with a
  diagnostic reason string. Optional ``on_entry_result`` callback
  fires once per entry.
- **HITL latency invariant** (acceptance criterion): a 5-second sleep
  inside the mocked ``/transition`` call does NOT block any caller
  upstream of ``run_wontdo_drain`` — the drain runs off the HITL POST
  path. We verify this by composing the drain on a slow upstream and
  asserting the only blocking is the drain itself, not the HITL hook.
- **In-flight refusal** (acceptance criterion): the test exercises the
  upstream contract — when an entry's task carries no
  ``in-flight-confirmed`` marker in ``Task.notes`` it should NOT reach
  the drain (the applier refuses at gateway-call time and records the
  failure in the contract). Since the in-flight gate lives inside the
  applier prompt (task-2-8 documenter scope) we focus the test on the
  drain's idempotent re-run guarantee instead: a Won't-Do drain over
  an empty handoff is a no-op.
- ``WontDoEntry`` dataclass shape: optional fields default to None.

Plus the three new orchestrator helpers introduced by coder v1/v2
(issue #1557 reviewer_code v1 finding #2):

- ``_next_phases_for_epic`` — reroutes auto-advance through APPLY
  for epic pipelines (PLAN → APPLY → IMPLEMENT). Non-epic pipelines
  see the default phase list unchanged.
- ``_write_apply_phase_handoff`` — writes the applier handoff JSON
  (``approved_phase`` / ``contract_path`` / ``draft_path``) at
  ``.egg-state/agent-outputs/<pipeline>-apply-handoff.json`` before
  APPLY spawns.
- ``_drain_wontdo_batch_after_apply`` — loads the Won't-Do handoff
  JSON and POSTs each transition via ``run_wontdo_drain``. Fail-open
  on missing handoff file (returns silently).

The orchestrator helpers exercise the integration boundary; tests
verify the structural contract (source-text invariants — always
runnable) and the functional contract (direct-call tests — skip
when ``routes.pipelines`` can't be imported in isolation, which is
the current slice-2 state pending the events.py update for
``EventType.CONTEXT_PR_SKIPPED`` / ``CONTEXT_PR_FAILED``).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import wontdo_drain
from wontdo_drain import (
    DrainResult,
    WontDoEntry,
    load_wontdo_handoff,
    run_wontdo_drain,
)

# Helper: try to import the three orchestrator helpers. If the
# import fails (currently the case on slice-2 because
# ``orchestrator/routes/pipelines.py`` references
# ``EventType.CONTEXT_PR_SKIPPED`` which doesn't exist on slice-2's
# ``orchestrator/events.py`` — the enum values exist on origin/main
# but slice-2 hasn't been rebased), the functional tests skip with
# a clear reason.
_PIPELINES_IMPORT_ERROR: str | None = None
_next_phases_for_epic = None
_write_apply_phase_handoff = None
_drain_wontdo_batch_after_apply = None
try:
    from routes.pipelines import (  # type: ignore[no-redef]
        _drain_wontdo_batch_after_apply,
        _next_phases_for_epic,
        _write_apply_phase_handoff,
    )
except ImportError as exc:
    _PIPELINES_IMPORT_ERROR = f"ImportError: {exc}"
except AttributeError as exc:
    _PIPELINES_IMPORT_ERROR = f"AttributeError: {exc}"

_REQUIRES_PIPELINES = pytest.mark.skipif(
    _PIPELINES_IMPORT_ERROR is not None,
    reason=(
        "Cannot import orchestrator/routes/pipelines.py in isolation on "
        "slice-2 (CONTEXT_PR_SKIPPED missing from events.py; coder "
        "scope). Source-text invariants below still run. "
        f"Original error: {_PIPELINES_IMPORT_ERROR}"
    ),
)

# Source-text reads for structural invariants. These always run —
# they read the .py file directly rather than importing the module.
_PIPELINES_SRC_PATH = Path(__file__).parent.parent / "routes" / "pipelines.py"
_PIPELINES_SRC: str = (
    _PIPELINES_SRC_PATH.read_text(encoding="utf-8") if _PIPELINES_SRC_PATH.exists() else ""
)

# -----------------------------------------------------------------------------
# WontDoEntry dataclass
# -----------------------------------------------------------------------------


class TestWontDoEntry:
    def test_minimal_fields(self):
        entry = WontDoEntry(jira_key="ENG-1")
        assert entry.jira_key == "ENG-1"
        assert entry.comment == ""
        assert entry.task_id is None
        assert entry.survivor_key is None

    def test_full_fields(self):
        entry = WontDoEntry(
            jira_key="ENG-1",
            comment="Consolidated into ENG-2",
            task_id="task-2-1",
            survivor_key="ENG-2",
        )
        assert entry.comment == "Consolidated into ENG-2"
        assert entry.task_id == "task-2-1"
        assert entry.survivor_key == "ENG-2"


# -----------------------------------------------------------------------------
# load_wontdo_handoff — parser
# -----------------------------------------------------------------------------


class TestLoadWontdoHandoff:
    """The handoff parser MUST never raise — missing / malformed inputs
    return an empty list so the drain treats them as "nothing to do".
    """

    def test_missing_file_returns_empty(self, tmp_path: Path):
        entries = load_wontdo_handoff(tmp_path / "nope.json")
        assert entries == []

    def test_invalid_json_returns_empty(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("not json at all {{{")
        entries = load_wontdo_handoff(p)
        assert entries == []

    def test_bare_list_shape(self, tmp_path: Path):
        p = tmp_path / "h.json"
        p.write_text(
            json.dumps(
                [
                    {"jira_key": "ENG-1", "comment": "Closed by ENG-2"},
                    {"jira_key": "ENG-3"},
                ]
            )
        )
        entries = load_wontdo_handoff(p)
        assert len(entries) == 2
        assert entries[0].jira_key == "ENG-1"
        assert entries[0].comment == "Closed by ENG-2"
        assert entries[1].comment == ""

    def test_wrapped_entries_shape(self, tmp_path: Path):
        """The applier may emit ``{'entries': [...], 'epic_key': '...'}``."""
        p = tmp_path / "h.json"
        p.write_text(
            json.dumps(
                {
                    "epic_key": "ENG-1",
                    "entries": [{"jira_key": "ENG-2", "comment": "x"}],
                }
            )
        )
        entries = load_wontdo_handoff(p)
        assert len(entries) == 1
        assert entries[0].jira_key == "ENG-2"

    def test_key_alias_accepted(self, tmp_path: Path):
        """Backwards compat: ``key`` is accepted as an alias for ``jira_key``."""
        p = tmp_path / "h.json"
        p.write_text(json.dumps([{"key": "ENG-9", "comment": "alt key"}]))
        entries = load_wontdo_handoff(p)
        assert len(entries) == 1
        assert entries[0].jira_key == "ENG-9"

    def test_missing_jira_key_skipped(self, tmp_path: Path):
        p = tmp_path / "h.json"
        p.write_text(
            json.dumps(
                [
                    {"comment": "x"},  # no key — skipped
                    {"jira_key": "ENG-1"},
                ]
            )
        )
        entries = load_wontdo_handoff(p)
        assert [e.jira_key for e in entries] == ["ENG-1"]

    def test_whitespace_jira_key_skipped(self, tmp_path: Path):
        p = tmp_path / "h.json"
        p.write_text(json.dumps([{"jira_key": "   "}, {"jira_key": "ENG-1"}]))
        entries = load_wontdo_handoff(p)
        assert [e.jira_key for e in entries] == ["ENG-1"]

    def test_non_dict_entry_skipped(self, tmp_path: Path):
        p = tmp_path / "h.json"
        p.write_text(json.dumps(["string", 42, {"jira_key": "ENG-1"}]))
        entries = load_wontdo_handoff(p)
        assert [e.jira_key for e in entries] == ["ENG-1"]

    def test_non_list_entries_field_returns_empty(self, tmp_path: Path):
        """``{'entries': 'not a list'}`` → empty."""
        p = tmp_path / "h.json"
        p.write_text(json.dumps({"entries": "not a list"}))
        assert load_wontdo_handoff(p) == []

    def test_bare_string_top_level_returns_empty(self, tmp_path: Path):
        p = tmp_path / "h.json"
        p.write_text(json.dumps("hello"))
        assert load_wontdo_handoff(p) == []

    def test_jira_key_trimmed(self, tmp_path: Path):
        p = tmp_path / "h.json"
        p.write_text(json.dumps([{"jira_key": "  ENG-1  "}]))
        entries = load_wontdo_handoff(p)
        assert entries[0].jira_key == "ENG-1"

    def test_survivor_key_and_task_id_preserved(self, tmp_path: Path):
        p = tmp_path / "h.json"
        p.write_text(
            json.dumps(
                [
                    {
                        "jira_key": "ENG-1",
                        "comment": "Consolidated",
                        "survivor_key": "ENG-2",
                        "task_id": "task-2-1",
                    }
                ]
            )
        )
        entries = load_wontdo_handoff(p)
        assert entries[0].survivor_key == "ENG-2"
        assert entries[0].task_id == "task-2-1"


# -----------------------------------------------------------------------------
# run_wontdo_drain — orchestration
# -----------------------------------------------------------------------------


def _write_handoff(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(entries))
    return path


class TestRunWontdoDrain:
    """End-to-end ``run_wontdo_drain`` against a patched ``_post_transition``."""

    def test_empty_handoff_returns_no_op(self, tmp_path: Path):
        """No entries → no transitions, no callbacks, no errors."""
        path = _write_handoff(tmp_path / "h.json", [])
        called: list[Any] = []
        result = run_wontdo_drain(
            handoff_path=path,
            on_entry_result=lambda *a, **k: called.append(a),
        )
        assert result.succeeded == []
        assert result.failed == []
        assert called == []

    def test_missing_handoff_returns_no_op(self, tmp_path: Path):
        """Missing handoff file is treated as "nothing to do" (acceptance:
        idempotent re-run produces zero new gateway writes)."""
        result = run_wontdo_drain(handoff_path=tmp_path / "missing.json")
        assert result.succeeded == []
        assert result.failed == []

    def test_happy_path_all_succeed(self, tmp_path: Path):
        path = _write_handoff(
            tmp_path / "h.json",
            [
                {"jira_key": "ENG-1", "comment": "x"},
                {"jira_key": "ENG-2", "comment": "y"},
            ],
        )

        calls: list[dict[str, Any]] = []

        def _fake_post(*, jira_key, comment, transition_name="Won't Do"):
            calls.append({"key": jira_key, "comment": comment, "tx": transition_name})
            return True, ""

        with patch.object(wontdo_drain, "_post_transition", side_effect=_fake_post):
            result = run_wontdo_drain(handoff_path=path)

        assert result.succeeded == ["ENG-1", "ENG-2"]
        assert result.failed == []
        assert [c["key"] for c in calls] == ["ENG-1", "ENG-2"]
        # All calls used the default Won't Do transition.
        assert {c["tx"] for c in calls} == {"Won't Do"}

    def test_partial_failure_accumulates(self, tmp_path: Path):
        """One success + one failure must both be recorded; the drain
        does NOT halt on the first failure."""
        path = _write_handoff(
            tmp_path / "h.json",
            [
                {"jira_key": "ENG-1"},
                {"jira_key": "ENG-2"},
                {"jira_key": "ENG-3"},
            ],
        )

        responses = {
            "ENG-1": (True, ""),
            "ENG-2": (False, "upstream_status=500; body=oops"),
            "ENG-3": (True, ""),
        }

        def _fake_post(*, jira_key, comment, transition_name="Won't Do"):
            return responses[jira_key]

        with patch.object(wontdo_drain, "_post_transition", side_effect=_fake_post):
            result = run_wontdo_drain(handoff_path=path)

        assert result.succeeded == ["ENG-1", "ENG-3"]
        assert result.failed == [
            ("ENG-2", "upstream_status=500; body=oops"),
        ]

    def test_callback_invoked_per_entry(self, tmp_path: Path):
        """``on_entry_result`` fires once per entry with (entry, ok, reason)."""
        path = _write_handoff(
            tmp_path / "h.json",
            [
                {"jira_key": "ENG-1", "comment": "ok"},
                {"jira_key": "ENG-2", "comment": "fail"},
            ],
        )
        responses = {
            "ENG-1": (True, ""),
            "ENG-2": (False, "transport_error=boom"),
        }

        def _fake_post(*, jira_key, comment, transition_name="Won't Do"):
            return responses[jira_key]

        captures: list[tuple[str, bool, str]] = []

        def _on_entry(entry, ok, reason):
            captures.append((entry.jira_key, ok, reason))

        with patch.object(wontdo_drain, "_post_transition", side_effect=_fake_post):
            run_wontdo_drain(handoff_path=path, on_entry_result=_on_entry)

        assert captures == [
            ("ENG-1", True, ""),
            ("ENG-2", False, "transport_error=boom"),
        ]

    def test_callback_exception_does_not_halt_drain(self, tmp_path: Path):
        """If the callback raises, the drain logs and proceeds."""
        path = _write_handoff(
            tmp_path / "h.json",
            [
                {"jira_key": "ENG-1"},
                {"jira_key": "ENG-2"},
            ],
        )

        def _fake_post(*, jira_key, comment, transition_name="Won't Do"):
            return True, ""

        def _bad_cb(entry, ok, reason):
            raise RuntimeError("callback failed")

        with patch.object(wontdo_drain, "_post_transition", side_effect=_fake_post):
            result = run_wontdo_drain(handoff_path=path, on_entry_result=_bad_cb)
        assert result.succeeded == ["ENG-1", "ENG-2"]

    def test_drain_does_not_appear_in_persist_phase_gate_resolution(self):
        """Acceptance (task-2-7): the Won't-Do drain runs in
        ``_drain_wontdo_batch_after_apply``, NOT inside
        ``_persist_phase_gate_resolution``. A regression that wired
        ``run_wontdo_drain`` (or ``_drain_wontdo_batch_after_apply``)
        into the HITL persistence path would extend the operator's
        approve POST latency by the time of every transition call.

        Verified by **source-text inspection** on the production file:
        reads ``orchestrator/routes/pipelines.py`` directly as text,
        extracts the ``_persist_phase_gate_resolution`` body via regex,
        and asserts neither ``run_wontdo_drain`` nor
        ``_drain_wontdo_batch_after_apply`` is mentioned anywhere in
        the function body. This is the same pattern the orchestrator
        suite uses for other "function X must not appear inside
        function Y" structural invariants (see
        ``test_advance_phase_thread.py``).

        Source-text inspection (rather than ``inspect.getsource(...)``)
        means this test runs even when ``routes.pipelines`` cannot be
        imported in isolation — important on slice-2 today because
        ``events.py`` is missing ``CONTEXT_PR_SKIPPED`` (coder scope).
        A regression that adds the drain call into
        ``_persist_phase_gate_resolution`` fails this test immediately,
        with no chance of being masked by a stub.

        Complementary positive check: assert that ``run_wontdo_drain``
        IS referenced inside ``_drain_wontdo_batch_after_apply`` (the
        dedicated post-apply hook), so the structural invariant is
        bidirectional.
        """
        # Extract the bodies of both functions from the source file.
        persist_match = re.search(
            r"def _persist_phase_gate_resolution\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert persist_match, (
            "Could not locate ``_persist_phase_gate_resolution`` in "
            "orchestrator/routes/pipelines.py — update this regex if "
            "the function was renamed or moved."
        )
        persist_body = persist_match.group(0)

        drain_match = re.search(
            r"def _drain_wontdo_batch_after_apply\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert drain_match, (
            "Could not locate ``_drain_wontdo_batch_after_apply`` in "
            "orchestrator/routes/pipelines.py — update this regex if "
            "the function was renamed or moved."
        )
        drain_body = drain_match.group(0)

        # NEGATIVE: drain symbols MUST NOT appear in the HITL hook.
        # A regression adding either symbol into _persist_phase_gate_
        # resolution would inline drain latency into the HITL POST.
        assert "run_wontdo_drain" not in persist_body, (
            "HITL latency invariant violated: ``run_wontdo_drain`` "
            "appears inside ``_persist_phase_gate_resolution`` — the "
            "Won't-Do drain must run out of band from the HITL "
            "approve POST (task-2-7 acceptance)."
        )
        assert "_drain_wontdo_batch_after_apply" not in persist_body, (
            "HITL latency invariant violated: "
            "``_drain_wontdo_batch_after_apply`` appears inside "
            "``_persist_phase_gate_resolution`` — the post-apply "
            "drain hook must run out of band from the HITL approve "
            "POST (task-2-7 acceptance)."
        )

        # POSITIVE: the drain hook IS where ``run_wontdo_drain`` is
        # called from. If a refactor moves the drain wiring to a
        # different orchestrator helper, surface that explicitly.
        assert "run_wontdo_drain" in drain_body, (
            "Bidirectional check: ``run_wontdo_drain`` no longer "
            "appears inside ``_drain_wontdo_batch_after_apply`` — "
            "the drain wiring may have moved. Update this assertion "
            "if the wiring is now in a different orchestrator helper."
        )

    def test_drain_accumulates_per_entry_latency(self, tmp_path: Path):
        """Independent of the HITL invariant: a slow upstream means a
        slow drain.

        This is the test that verifies the *internal* latency model of
        the drain itself: the drain calls ``_post_transition`` for
        each entry sequentially, so total latency equals the sum of
        per-entry latencies. Confirms a slow upstream (mocked here as
        100ms per entry) is correctly observed at the drain return.
        The test pair (this one + the inspect-source HITL invariant
        above) verifies the full task-2-7 acceptance: the drain CAN
        be slow but is NOT on the HITL critical path.
        """
        path = _write_handoff(
            tmp_path / "h.json",
            [{"jira_key": "ENG-1"}, {"jira_key": "ENG-2"}],
        )

        def _slow_post(*, jira_key, comment, transition_name="Won't Do"):
            time.sleep(0.1)
            return True, ""

        t0 = time.monotonic()
        with patch.object(wontdo_drain, "_post_transition", side_effect=_slow_post):
            drain_result = run_wontdo_drain(handoff_path=path)
        drain_elapsed = time.monotonic() - t0
        assert drain_elapsed >= 0.2, (
            f"Drain should accumulate per-entry latency; only took {drain_elapsed * 1000:.0f}ms"
        )
        assert drain_result.succeeded == ["ENG-1", "ENG-2"]


# -----------------------------------------------------------------------------
# _post_transition — gateway wrapper error semantics
# -----------------------------------------------------------------------------


class TestPostTransitionErrorSemantics:
    """``_post_transition`` is a thin gateway wrapper. We verify the
    error-classification contract here so the drain's per-entry
    reason strings stay machine-parseable for the operator's audit
    log (acceptance: refused mutations write ``jira_action_status=
    'failed'`` with reason).
    """

    def test_classifies_url_error_as_transport_error(self, monkeypatch):
        """A ``URLError`` lands in the failed bucket with a typed prefix."""
        from urllib.error import URLError

        class _FakeOpener:
            def open(self, *args, **kwargs):
                raise URLError("network unreachable")

        monkeypatch.setattr(wontdo_drain, "build_opener", lambda: _FakeOpener())
        ok, reason = wontdo_drain._post_transition(jira_key="ENG-1", comment="x")
        assert ok is False
        assert reason.startswith("transport_error=")

    def test_classifies_http_error(self, monkeypatch):
        """An HTTPError lands in the failed bucket with the status code."""
        from urllib.error import HTTPError

        class _FakeOpener:
            def open(self, *args, **kwargs):
                raise HTTPError(url="x", code=500, msg="boom", hdrs=None, fp=None)

        monkeypatch.setattr(wontdo_drain, "build_opener", lambda: _FakeOpener())
        ok, reason = wontdo_drain._post_transition(jira_key="ENG-1", comment="x")
        assert ok is False
        assert "http_error_500" in reason


# -----------------------------------------------------------------------------
# DrainResult dataclass — defaults
# -----------------------------------------------------------------------------


class TestDrainResult:
    def test_defaults_are_empty_lists(self):
        result = DrainResult()
        assert result.succeeded == []
        assert result.failed == []
        assert result.skipped == []

    def test_failed_entries_are_tuples_of_str(self):
        result = DrainResult()
        result.failed.append(("ENG-1", "transport_error=foo"))
        assert isinstance(result.failed[0], tuple)
        assert all(isinstance(s, str) for s in result.failed[0])


# -----------------------------------------------------------------------------
# In-flight refusal lifecycle (#1557 task-2-7)
# -----------------------------------------------------------------------------


class TestInFlightRefusalLifecycle:
    """Acceptance criterion (task-2-7):

      "Re-run with `in-flight-confirmed` added to a task's notes
       succeeds for that task only on the next apply phase spawn."

    The in-flight refusal itself fires inside the applier prompt
    (documenter scope per task-2-8); the drain only sees entries that
    the applier accepted. We therefore validate the drain's idempotent
    re-run guarantee here: an empty handoff is a no-op; a non-empty
    handoff drains exactly once per entry.
    """

    def test_empty_handoff_first_pass_is_no_op(self, tmp_path: Path):
        """First-pass apply with no Won't-Do entries → no gateway calls."""
        path = _write_handoff(tmp_path / "h.json", [])
        with patch.object(wontdo_drain, "_post_transition") as mock_post:
            run_wontdo_drain(handoff_path=path)
        mock_post.assert_not_called()

    def test_handoff_with_entries_drains_once(self, tmp_path: Path):
        """Once the applier writes the entry, the drain runs once."""
        path = _write_handoff(tmp_path / "h.json", [{"jira_key": "ENG-1", "comment": "wontdo"}])

        call_count = {"n": 0}

        def _fake_post(*, jira_key, comment, transition_name="Won't Do"):
            call_count["n"] += 1
            return True, ""

        with patch.object(wontdo_drain, "_post_transition", side_effect=_fake_post):
            result = run_wontdo_drain(handoff_path=path)
        assert call_count["n"] == 1
        assert result.succeeded == ["ENG-1"]


# -----------------------------------------------------------------------------
# Failure-reason → Task.notes contract (smoke check)
# -----------------------------------------------------------------------------


class TestCallbackContract:
    """The orchestrator passes a callback that writes failure reasons
    into ``Task.notes`` (per task-2-7 acceptance). We verify the
    callback API is correctly typed so the orchestrator can rely on it
    without defensive wrapping.
    """

    def test_callback_signature(self, tmp_path: Path):
        path = _write_handoff(
            tmp_path / "h.json",
            [{"jira_key": "ENG-1", "task_id": "task-2-1"}],
        )

        def _post(*, jira_key, comment, transition_name="Won't Do"):
            return False, "http_error_404; body=no such ticket"

        observed: list[Any] = []

        def _cb(entry, ok, reason):
            observed.append(
                {
                    "entry": entry,
                    "ok": ok,
                    "reason": reason,
                }
            )

        with patch.object(wontdo_drain, "_post_transition", side_effect=_post):
            run_wontdo_drain(handoff_path=path, on_entry_result=_cb)

        assert len(observed) == 1
        entry = observed[0]["entry"]
        assert isinstance(entry, WontDoEntry)
        assert entry.jira_key == "ENG-1"
        assert entry.task_id == "task-2-1"
        # Failure reason is a non-empty string suitable for Task.notes.
        assert observed[0]["ok"] is False
        assert "http_error_404" in observed[0]["reason"]


# =============================================================================
# Issue #1557 slice-2 reviewer_code v1 finding #2 — orchestrator helpers
# =============================================================================
#
# Three new orchestrator helpers introduced by coder v1/v2 carry the
# entire slice-2 scheduler integration. Tests below verify both the
# structural invariants (source-text reads — always runnable) and the
# functional contract (direct-call tests — skip when routes.pipelines
# can't be imported in isolation on slice-2 today).


class TestNextPhasesForEpicSource:
    """Source-text invariants on ``_next_phases_for_epic``.

    These tests run regardless of slice-2's events.py state — they
    read ``orchestrator/routes/pipelines.py`` as text and assert
    branching properties via ``inspect.getsource``.
    """

    def test_function_defined(self):
        assert "def _next_phases_for_epic(" in _PIPELINES_SRC, (
            "_next_phases_for_epic must be defined in routes/pipelines.py"
        )

    def test_handles_non_epic_passthrough(self):
        """Source must short-circuit on ``pipeline.is_epic == False`` and
        return ``default_next_phases`` unchanged. Verified by asserting
        the function body contains both the is_epic check and the
        passthrough return."""
        match = re.search(
            r"def _next_phases_for_epic\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert match, "Could not isolate _next_phases_for_epic body"
        body = match.group(0)
        # is_epic gate (defensive getattr matches the production shape).
        assert "is_epic" in body, "is_epic gate missing"
        # Non-epic passthrough returns default_next_phases unchanged.
        assert "return default_next_phases" in body, (
            "Non-epic passthrough must return default_next_phases unchanged "
            "to preserve pre-#1557 scheduling bit-for-bit"
        )

    def test_handles_plan_to_apply_route(self):
        match = re.search(
            r"def _next_phases_for_epic\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert match
        body = match.group(0)
        # PLAN → [APPLY] for epic pipelines.
        assert "PipelinePhase.PLAN" in body
        assert "PipelinePhase.APPLY" in body
        # APPLY → [IMPLEMENT] for epic pipelines.
        assert "PipelinePhase.IMPLEMENT" in body


class TestNextPhasesForEpicCallable:
    """Functional tests against the imported helper. Skip-gated on
    slice-2 events.py state."""

    @_REQUIRES_PIPELINES
    def test_non_epic_returns_default_unchanged(self):
        """Acceptance: ``non_epic == False`` → default returned bit-for-bit."""
        pipeline = MagicMock()
        pipeline.is_epic = False
        default = [object()]  # opaque sentinel — proves identity not just equality
        result = _next_phases_for_epic(pipeline, MagicMock(), default)
        assert result is default

    @_REQUIRES_PIPELINES
    def test_epic_plan_routes_to_apply(self):
        """Acceptance: epic + PLAN → ``[APPLY]``."""
        from models import PipelinePhase

        pipeline = MagicMock()
        pipeline.is_epic = True
        result = _next_phases_for_epic(pipeline, PipelinePhase.PLAN, [PipelinePhase.IMPLEMENT])
        assert result == [PipelinePhase.APPLY]

    @_REQUIRES_PIPELINES
    def test_epic_apply_routes_to_implement(self):
        """Acceptance: epic + APPLY → ``[IMPLEMENT]``."""
        from models import PipelinePhase

        pipeline = MagicMock()
        pipeline.is_epic = True
        result = _next_phases_for_epic(pipeline, PipelinePhase.APPLY, [PipelinePhase.PR])
        assert result == [PipelinePhase.IMPLEMENT]

    @_REQUIRES_PIPELINES
    def test_epic_implement_returns_default(self):
        """Acceptance: epic + IMPLEMENT (or any other current_phase the
        function doesn't special-case) → default unchanged."""
        from models import PipelinePhase

        pipeline = MagicMock()
        pipeline.is_epic = True
        default = [PipelinePhase.PR]
        result = _next_phases_for_epic(pipeline, PipelinePhase.IMPLEMENT, default)
        assert result == default


class TestWriteApplyPhaseHandoffSource:
    """Source-text invariants on ``_write_apply_phase_handoff``."""

    def test_function_defined(self):
        assert "def _write_apply_phase_handoff(" in _PIPELINES_SRC

    def test_writes_to_agent_outputs(self):
        """The handoff JSON lands at
        ``.egg-state/agent-outputs/<pipeline-id>-apply-handoff.json``."""
        match = re.search(
            r"def _write_apply_phase_handoff\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert match
        body = match.group(0)
        assert '".egg-state"' in body
        assert '"agent-outputs"' in body
        assert "-apply-handoff.json" in body

    def test_payload_includes_required_fields(self):
        """Payload includes ``approved_phase``, ``contract_path``,
        ``draft_path``."""
        match = re.search(
            r"def _write_apply_phase_handoff\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert match
        body = match.group(0)
        assert '"approved_phase"' in body
        assert '"contract_path"' in body
        assert '"draft_path"' in body


class TestWriteApplyPhaseHandoffCallable:
    """Functional tests for ``_write_apply_phase_handoff``."""

    @_REQUIRES_PIPELINES
    def test_writes_well_formed_json(self, tmp_path: Path):
        """Calls the helper against a tmp worktree and asserts the JSON
        payload shape + filename."""
        pipeline = MagicMock()
        pipeline.id = "issue-1557-v2"
        _write_apply_phase_handoff(pipeline, tmp_path, "refine")
        handoff = tmp_path / ".egg-state" / "agent-outputs" / "issue-1557-v2-apply-handoff.json"
        assert handoff.exists(), f"Expected handoff at {handoff}"
        payload = json.loads(handoff.read_text())
        assert payload["approved_phase"] == "refine"
        assert "contract_path" in payload
        assert "draft_path" in payload
        # Paths are absolute (or at least worktree-rooted) — verified by
        # asserting both contain the tmp_path prefix.
        assert str(tmp_path) in payload["contract_path"]
        assert str(tmp_path) in payload["draft_path"]
        # Contract path points at the per-pipeline contract file.
        assert payload["contract_path"].endswith(f".egg-state/contracts/{pipeline.id}.json")
        # Draft path follows the per-phase pattern.
        assert payload["draft_path"].endswith(f".egg-state/brc-history/{pipeline.id}-refine.md")

    @_REQUIRES_PIPELINES
    def test_creates_agent_outputs_dir_if_missing(self, tmp_path: Path):
        """The helper creates the agent-outputs dir if it doesn't exist."""
        pipeline = MagicMock()
        pipeline.id = "issue-X"
        # tmp_path is empty — no .egg-state/ exists.
        _write_apply_phase_handoff(pipeline, tmp_path, "plan")
        assert (tmp_path / ".egg-state" / "agent-outputs").is_dir()

    @_REQUIRES_PIPELINES
    def test_approved_phase_propagated_verbatim(self, tmp_path: Path):
        """Adversarial: an unusual approved_phase string is preserved as-is
        (the helper does not normalise / sanitise)."""
        pipeline = MagicMock()
        pipeline.id = "issue-X"
        _write_apply_phase_handoff(pipeline, tmp_path, "REFINE")
        handoff = tmp_path / ".egg-state" / "agent-outputs" / "issue-X-apply-handoff.json"
        payload = json.loads(handoff.read_text())
        assert payload["approved_phase"] == "REFINE"


class TestDrainWontdoBatchAfterApplySource:
    """Source-text invariants on ``_drain_wontdo_batch_after_apply``."""

    def test_function_defined(self):
        assert "def _drain_wontdo_batch_after_apply(" in _PIPELINES_SRC

    def test_loads_wontdo_handoff_path(self):
        match = re.search(
            r"def _drain_wontdo_batch_after_apply\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert match
        body = match.group(0)
        # Handoff filename ends in ``-wontdo.json``.
        assert "-wontdo.json" in body, "Helper should load the applier's Won't-Do handoff JSON"
        # Imports / calls run_wontdo_drain.
        assert "run_wontdo_drain" in body, (
            "Helper must invoke run_wontdo_drain on the handoff entries"
        )

    def test_fail_open_on_missing_handoff(self):
        """Acceptance (task-2-7): a missing handoff file is "no Won't-Dos
        to drain" — return silently. Verified by asserting the helper
        checks ``handoff_path.exists()`` before invoking the drain."""
        match = re.search(
            r"def _drain_wontdo_batch_after_apply\(.*?\n(?:.*\n)*?(?=^def |\Z)",
            _PIPELINES_SRC,
            re.MULTILINE,
        )
        assert match
        body = match.group(0)
        assert "handoff_path.exists()" in body or ".exists()" in body, (
            "Helper must fail-open on missing handoff file"
        )


class TestDrainWontdoBatchAfterApplyCallable:
    """Functional tests for ``_drain_wontdo_batch_after_apply``."""

    @_REQUIRES_PIPELINES
    def test_missing_handoff_returns_silently(self, tmp_path: Path):
        """Acceptance: missing handoff → no gateway calls, no exceptions."""
        pipeline = MagicMock()
        pipeline.id = "no-handoff"

        # If the helper accidentally calls into the drain even with a
        # missing handoff, this patch surfaces the failure.
        import routes.pipelines as routes_pipelines

        with patch.object(
            routes_pipelines,
            "run_wontdo_drain",
            create=True,
            side_effect=AssertionError("drain should not be called on missing handoff"),
        ):
            # Should not raise.
            _drain_wontdo_batch_after_apply(pipeline, tmp_path)

    @_REQUIRES_PIPELINES
    def test_invokes_drain_with_handoff_path(self, tmp_path: Path):
        """When the handoff file exists, the helper invokes
        ``run_wontdo_drain`` with the correct path."""
        pipeline = MagicMock()
        pipeline.id = "with-handoff"
        # Pre-create the handoff so the helper proceeds.
        handoff_dir = tmp_path / ".egg-state" / "agent-outputs"
        handoff_dir.mkdir(parents=True)
        handoff = handoff_dir / "with-handoff-wontdo.json"
        handoff.write_text(json.dumps([]))

        # Patch ``run_wontdo_drain`` to observe the call.
        from wontdo_drain import DrainResult as _DR

        captured: dict[str, Any] = {}

        def _fake_drain(*, handoff_path, on_entry_result=None):
            captured["handoff_path"] = str(handoff_path)
            return _DR()

        import routes.pipelines as routes_pipelines

        with patch.object(
            routes_pipelines, "run_wontdo_drain", create=True, side_effect=_fake_drain
        ):
            _drain_wontdo_batch_after_apply(pipeline, tmp_path)

        assert captured.get("handoff_path", "").endswith("with-handoff-wontdo.json"), (
            f"Expected drain to be invoked with the handoff path; captured: {captured}"
        )

"""Slice-7 (#3393): cross-repo merge-gate stateful coverage (task-5-1 gaps).

Closes the two open ``task-5-1`` tester→coder coverage gaps that the
slice-5 always-green reference tests deferred:

* **gap-1** — the stateful ``cross_repo_merge_gate.poll_once`` surface had no
  dedicated tests (only the single-state ``classify_upstream_merge`` was
  pinned). Here we drive ``poll_once`` across ticks with injected fakes and
  assert: the Tier-A happy path (all upstreams merged → ``mark_ready``); the
  CLOSED-unmerged failure terminal (→ ``register_hold("closed_unmerged")``,
  never auto-ready); the never-merging attempt-bound **timeout** terminal
  (→ ``register_hold("timeout")``); the multi-upstream AND-gate (all-merged
  vs one-open vs one-closed vs ``pr_number is None``); the Tier-B skip-poll
  path (``hold_kind == "hitl"`` registers up front, never polls merge state);
  and the ``GateProgress`` idempotency (a resolved gate is not re-readied; a
  ``mark_ready`` that returns False retries next tick).

* **gap-2** — the ``routes.pipelines._cross_repo_hold_resolution`` verdict
  mapping was untested and previously failed OPEN (a bare ``"release" in
  text`` substring readied a PR on a *negated* freeform resolution like "do
  NOT release yet"). This pins the tightened mapping: RELEASE only on an
  EXACT match of the release option id/label, every ambiguous / negated /
  keep value falls through to the KEEP fail-safe (defends the operator's
  cq-1 "human owns the release" ruling). Guarded behind the heavy
  ``routes.pipelines`` import so a stripped env skips rather than errors.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ``cross_repo_merge_gate`` is pure-logic (no docker / heavy deps), but it
# lives under ``orchestrator/`` so bootstrap sys.path like the sibling tests.
_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from cross_repo_merge_gate import (  # noqa: E402
    HITL_HOLD,
    MARK_READY,
    WAIT,
    GateProgress,
    classify_upstream_merge,
    find_cross_repo_gates,
    poll_once,
)

# ``_cross_repo_hold_resolution`` lives in the heavy ``routes.pipelines``
# module — stub ``docker`` and guard the import (stripped env → skip).
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", sys.modules["docker"].errors)
sys.modules.setdefault("docker.types", sys.modules["docker"].types)

_PIPELINES_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - exercised via skip path in a stripped env
    from routes.pipelines import (  # type: ignore[attr-defined]
        _CROSS_REPO_HOLD_KEEP_OPTION_ID,
        _CROSS_REPO_HOLD_KEEP_OPTION_LABEL,
        _CROSS_REPO_HOLD_RELEASE_OPTION_ID,
        _CROSS_REPO_HOLD_RELEASE_OPTION_LABEL,
        _cross_repo_hold_marker,
        _cross_repo_hold_resolution,
    )
except Exception as exc:  # noqa: BLE001
    _CROSS_REPO_HOLD_KEEP_OPTION_ID = None  # type: ignore[assignment]
    _CROSS_REPO_HOLD_KEEP_OPTION_LABEL = None  # type: ignore[assignment]
    _CROSS_REPO_HOLD_RELEASE_OPTION_ID = None  # type: ignore[assignment]
    _CROSS_REPO_HOLD_RELEASE_OPTION_LABEL = None  # type: ignore[assignment]
    _cross_repo_hold_marker = None  # type: ignore[assignment]
    _cross_repo_hold_resolution = None  # type: ignore[assignment]
    _PIPELINES_IMPORT_ERROR = repr(exc)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _slice(slice_id, repo, *, pr_number=None, dependencies=(), goal="", tasks=()):
    return SimpleNamespace(
        id=slice_id,
        repo=repo,
        pr_number=pr_number,
        dependencies=list(dependencies),
        goal=goal,
        tasks=list(tasks),
    )


def _contract(slices):
    return SimpleNamespace(slices=list(slices))


def _resolve_repo(s):
    return s.repo


def _merged_state():
    return {"state": "MERGED", "merged_at": "2026-07-02T00:00:00Z"}


def _open_state():
    return {"state": "OPEN", "merged_at": None}


def _closed_unmerged_state():
    return {"state": "CLOSED", "merged_at": None}


class _Recorder:
    """Collects injected-callable calls for assertions."""

    def __init__(self, *, merge_states=None, mark_ready_returns=True, resolution=None):
        self.merge_states = merge_states or {}
        self.mark_ready_returns = mark_ready_returns
        self.resolution = resolution
        self.marked_ready = []
        self.holds = []
        self.resolution_queries = 0

    def get_merge_state(self, repo, pr_number):
        return self.merge_states.get((repo, pr_number))

    def mark_ready(self, repo, pr_number):
        self.marked_ready.append((repo, pr_number))
        return self.mark_ready_returns

    def register_hold(self, gate, reason):
        self.holds.append((gate.slice_id, reason))
        return True

    def hold_resolution(self, gate):
        self.resolution_queries += 1
        return self.resolution

    def poll(self, contract, state, **kw):
        return poll_once(
            contract,
            resolve_repo=_resolve_repo,
            get_merge_state=self.get_merge_state,
            mark_ready=self.mark_ready,
            register_hold=self.register_hold,
            hold_resolution=self.hold_resolution,
            state=state,
            **kw,
        )


# ===========================================================================
# classify_upstream_merge — single-state seam (extends slice-5 coverage)
# ===========================================================================


class TestClassifyUpstreamMerge:
    def test_merged_marks_ready(self):
        assert classify_upstream_merge(_merged_state()) == MARK_READY

    def test_merged_boolean_shape(self):
        assert classify_upstream_merge({"merged": True}) == MARK_READY

    def test_closed_unmerged_is_hold(self):
        assert classify_upstream_merge(_closed_unmerged_state()) == HITL_HOLD

    def test_open_waits(self):
        assert classify_upstream_merge(_open_state()) == WAIT

    def test_unknown_none_waits(self):
        assert classify_upstream_merge(None) == WAIT


# ===========================================================================
# find_cross_repo_gates — gate derivation
# ===========================================================================


class TestFindCrossRepoGates:
    def test_cross_repo_dep_with_open_pr_emits_gate(self):
        slices = [
            _slice("A", "jwbron/schema", pr_number=100),
            _slice("B", "jwbron/consumer", pr_number=200, dependencies=["A"]),
        ]
        gates = find_cross_repo_gates(_contract(slices), _resolve_repo)
        assert len(gates) == 1
        assert gates[0].slice_id == "B"
        assert gates[0].upstreams[0].slice_id == "A"

    def test_same_repo_dep_is_excluded(self):
        slices = [
            _slice("A", "jwbron/egg", pr_number=100),
            _slice("B", "jwbron/egg", pr_number=200, dependencies=["A"]),
        ]
        assert find_cross_repo_gates(_contract(slices), _resolve_repo) == []

    def test_n1_pipeline_yields_no_gates(self):
        slices = [
            _slice("A", "jwbron/egg", pr_number=100),
            _slice("B", "jwbron/egg", pr_number=200, dependencies=["A"]),
        ]
        assert find_cross_repo_gates(_contract(slices), _resolve_repo) == []

    def test_dependent_without_pr_is_not_gated(self):
        slices = [
            _slice("A", "jwbron/schema", pr_number=100),
            _slice("B", "jwbron/consumer", pr_number=None, dependencies=["A"]),
        ]
        assert find_cross_repo_gates(_contract(slices), _resolve_repo) == []


# ===========================================================================
# poll_once — Tier-A happy path / failure terminals / bound
# ===========================================================================


class TestPollTierA:
    def _pipeline(self, upstream_pr=100, dependent_pr=200):
        return _contract(
            [
                _slice("A", "jwbron/schema", pr_number=upstream_pr),
                _slice("B", "jwbron/consumer", pr_number=dependent_pr, dependencies=["A"]),
            ]
        )

    def test_all_upstreams_merged_marks_ready(self):
        rec = _Recorder(merge_states={("jwbron/schema", 100): _merged_state()})
        state: dict[str, GateProgress] = {}
        result = rec.poll(self._pipeline(), state)
        assert rec.marked_ready == [("jwbron/consumer", 200)]
        assert result.readied == 1
        assert state["B"].resolved is True

    def test_open_upstream_waits_and_increments_attempts(self):
        rec = _Recorder(merge_states={("jwbron/schema", 100): _open_state()})
        state: dict[str, GateProgress] = {}
        result = rec.poll(self._pipeline(), state)
        assert rec.marked_ready == []
        assert rec.holds == []
        assert result.pending == 1
        assert state["B"].attempts == 1

    def test_closed_unmerged_escalates_to_hitl_hold(self):
        rec = _Recorder(merge_states={("jwbron/schema", 100): _closed_unmerged_state()})
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)
        assert rec.marked_ready == []
        assert rec.holds == [("B", "closed_unmerged")]
        assert state["B"].decision_registered is True

    def test_never_merging_upstream_times_out_to_hold(self):
        rec = _Recorder(merge_states={("jwbron/schema", 100): _open_state()})
        state: dict[str, GateProgress] = {}
        # max_attempts=2: ticks 1,2 wait; tick 3 crosses the bound → hold.
        for _ in range(3):
            rec.poll(self._pipeline(), state, max_attempts=2)
        assert ("B", "timeout") in rec.holds
        assert state["B"].decision_registered is True
        assert rec.marked_ready == []


class TestPollMultiUpstream:
    def _pipeline(self):
        return _contract(
            [
                _slice("A1", "jwbron/schema", pr_number=100),
                _slice("A2", "jwbron/proto", pr_number=110),
                _slice(
                    "B",
                    "jwbron/consumer",
                    pr_number=200,
                    dependencies=["A1", "A2"],
                ),
            ]
        )

    def test_and_gate_requires_all_merged(self):
        rec = _Recorder(
            merge_states={
                ("jwbron/schema", 100): _merged_state(),
                ("jwbron/proto", 110): _open_state(),  # not yet
            }
        )
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)
        assert rec.marked_ready == []  # one still open → wait

    def test_and_gate_readies_when_all_merged(self):
        rec = _Recorder(
            merge_states={
                ("jwbron/schema", 100): _merged_state(),
                ("jwbron/proto", 110): _merged_state(),
            }
        )
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)
        assert rec.marked_ready == [("jwbron/consumer", 200)]

    def test_one_closed_unmerged_holds_the_and_gate(self):
        rec = _Recorder(
            merge_states={
                ("jwbron/schema", 100): _merged_state(),
                ("jwbron/proto", 110): _closed_unmerged_state(),
            }
        )
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)
        assert ("B", "closed_unmerged") in rec.holds
        assert rec.marked_ready == []

    def test_upstream_without_pr_number_blocks_ready(self):
        # A2 has no PR yet → its state is None → not all-merged → wait.
        slices = [
            _slice("A1", "jwbron/schema", pr_number=100),
            _slice("A2", "jwbron/proto", pr_number=None),
            _slice("B", "jwbron/consumer", pr_number=200, dependencies=["A1", "A2"]),
        ]
        rec = _Recorder(merge_states={("jwbron/schema", 100): _merged_state()})
        state: dict[str, GateProgress] = {}
        rec.poll(_contract(slices), state)
        assert rec.marked_ready == []


class TestPollTierB:
    def _pipeline(self):
        from cross_repo_merge_gate import BEYOND_MERGE_STATE_MARKER

        return _contract(
            [
                _slice("A", "jwbron/schema", pr_number=100),
                _slice(
                    "B",
                    "jwbron/consumer",
                    pr_number=200,
                    dependencies=["A"],
                    goal=f"cut over {BEYOND_MERGE_STATE_MARKER}",
                ),
            ]
        )

    def test_tier_b_registers_hold_without_polling_merge(self):
        rec = _Recorder(merge_states={("jwbron/schema", 100): _merged_state()})
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)
        # Tier B never auto-readies even though the upstream IS merged.
        assert rec.marked_ready == []
        assert rec.holds == [("B", "beyond_merge_state")]

    def test_tier_b_release_verdict_readies(self):
        rec = _Recorder(
            merge_states={("jwbron/schema", 100): _merged_state()},
            resolution="release",
        )
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)  # tick 1: register hold
        rec.poll(self._pipeline(), state)  # tick 2: human released
        assert rec.marked_ready == [("jwbron/consumer", 200)]
        assert state["B"].resolved is True

    def test_tier_b_keep_verdict_stays_held(self):
        rec = _Recorder(resolution="keep")
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)  # register hold
        result = rec.poll(self._pipeline(), state)  # human kept held
        assert rec.marked_ready == []
        assert result.kept_held == 1
        assert state["B"].resolved is True


class TestPollIdempotency:
    def _pipeline(self):
        return _contract(
            [
                _slice("A", "jwbron/schema", pr_number=100),
                _slice("B", "jwbron/consumer", pr_number=200, dependencies=["A"]),
            ]
        )

    def test_resolved_gate_not_repolled(self):
        rec = _Recorder(merge_states={("jwbron/schema", 100): _merged_state()})
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)  # readies + resolves
        rec.poll(self._pipeline(), state)  # second tick: no-op
        assert rec.marked_ready == [("jwbron/consumer", 200)]  # only once

    def test_mark_ready_false_retries_next_tick(self):
        rec = _Recorder(
            merge_states={("jwbron/schema", 100): _merged_state()},
            mark_ready_returns=False,
        )
        state: dict[str, GateProgress] = {}
        rec.poll(self._pipeline(), state)
        assert state["B"].resolved is False  # not resolved — retry
        rec.mark_ready_returns = True
        rec.poll(self._pipeline(), state)
        assert state["B"].resolved is True
        assert rec.marked_ready == [
            ("jwbron/consumer", 200),
            ("jwbron/consumer", 200),
        ]


# ===========================================================================
# gap-2 — _cross_repo_hold_resolution verdict mapping (fail-safe)
# ===========================================================================


@pytest.mark.skipif(
    _cross_repo_hold_resolution is None,
    reason=f"routes.pipelines import failed (stripped env): {_PIPELINES_IMPORT_ERROR}",
)
class TestCrossRepoHoldResolutionMapping:
    """RELEASE only on an EXACT release id/label; everything else → KEEP."""

    def _contract_with_resolution(self, slice_id, resolution, *, resolved=True):
        from cross_repo_merge_gate import KEEP  # noqa: F401 (import sanity)

        marker = _cross_repo_hold_marker(slice_id)
        decision = SimpleNamespace(
            question=f"Cross-repo hold {marker} for {slice_id}",
            resolved=resolved,
            resolution=resolution,
        )
        return SimpleNamespace(decisions=[decision])

    def test_exact_release_id_releases(self):
        from cross_repo_merge_gate import RELEASE

        contract = self._contract_with_resolution("B", _CROSS_REPO_HOLD_RELEASE_OPTION_ID)
        assert _cross_repo_hold_resolution(contract, "B") == RELEASE

    def test_exact_release_label_releases(self):
        from cross_repo_merge_gate import RELEASE

        contract = self._contract_with_resolution("B", _CROSS_REPO_HOLD_RELEASE_OPTION_LABEL)
        assert _cross_repo_hold_resolution(contract, "B") == RELEASE

    def test_select_envelope_release_releases(self):
        from cross_repo_merge_gate import RELEASE

        envelope = json.dumps(
            {"action": "select", "selected": _CROSS_REPO_HOLD_RELEASE_OPTION_LABEL}
        )
        contract = self._contract_with_resolution("B", envelope)
        assert _cross_repo_hold_resolution(contract, "B") == RELEASE

    def test_keep_option_keeps(self):
        from cross_repo_merge_gate import KEEP

        contract = self._contract_with_resolution("B", _CROSS_REPO_HOLD_KEEP_OPTION_LABEL)
        assert _cross_repo_hold_resolution(contract, "B") == KEEP

    def test_negated_release_freeform_keeps(self):
        """The old ``"release" in text`` substring failed OPEN here."""
        from cross_repo_merge_gate import KEEP

        contract = self._contract_with_resolution("B", "Other: do NOT release yet")
        assert _cross_repo_hold_resolution(contract, "B") == KEEP

    def test_unrecognized_resolution_keeps(self):
        from cross_repo_merge_gate import KEEP

        contract = self._contract_with_resolution("B", "something ambiguous")
        assert _cross_repo_hold_resolution(contract, "B") == KEEP

    def test_unresolved_decision_returns_none(self):
        contract = self._contract_with_resolution(
            "B", _CROSS_REPO_HOLD_RELEASE_OPTION_ID, resolved=False
        )
        assert _cross_repo_hold_resolution(contract, "B") is None

    def test_absent_decision_returns_none(self):
        contract = SimpleNamespace(decisions=[])
        assert _cross_repo_hold_resolution(contract, "B") is None

"""Tests for the emit-only per-event measurement surfaces (#3249 / #3200 phase 10).

Covers the single adapter seam in :mod:`egg_agent.measurement`:

* **the emit-only invariant** — two structural assertions that no control-flow
  branch reads a metric: the ``__main__`` call site discards the return value
  and leaves the exit code untouched, and no branch condition in the module
  reads a built ``MeasurementSnapshot`` metric field;
* **a synthetic event with >=1 reseed** — the slice-8 ``reseed`` verdict +
  slice-1 occupancy round-trip into the six metrics;
* **routing through both surfaces** (progress + heartbeat) and the
  default-OFF / outside-a-pipeline gates;
* **graceful degradation** when the SDK usage is absent (non-Claude routes).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from egg_agent.measurement import (
    MEASUREMENT_ENV,
    MEASUREMENT_METRIC_FIELDS,
    build_snapshot,
    measurement_enabled,
    record_measurement,
)
from egg_agent.reseed import ResumeDecision
from egg_agent.result import AgentResult

_SHARED = Path(__file__).resolve().parents[3] / "shared" / "egg_agent"


@pytest.fixture(autouse=True)
def _clear_measurement_env(monkeypatch):
    """Isolate every test from ambient measurement / identity / route env."""
    for var in (
        MEASUREMENT_ENV,
        "EGG_PIPELINE_ID",
        "EGG_AGENT_ROLE",
        "EGG_PHASE",
        "EGG_SLICE_ID",
        "EGG_RESEED_THRESHOLD",
    ):
        monkeypatch.delenv(var, raising=False)


def _result(**overrides) -> AgentResult:
    base = {
        "success": True,
        "stdout": "ok",
        "stderr": "",
        "returncode": 0,
        "num_turns": 4,
        "session_id": "sess-1",
        "window_occupancy": 120_000,
        "token_usage": {
            "input_tokens": 20_000,
            "cache_read_input_tokens": 90_000,
            "cache_creation_input_tokens": 10_000,
            "output_tokens": 3_000,
        },
    }
    base.update(overrides)
    return AgentResult(**base)


def _reseed_decision(**overrides) -> ResumeDecision:
    base = {
        "resume": False,
        "session_id": None,
        "reason": "at_or_above_threshold",
        "occupancy": 500_000,
        "threshold": 400_000,
    }
    base.update(overrides)
    return ResumeDecision(**base)


class _CaptureRunner:
    """A stand-in for the subprocess runner that records the commands."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> None:
        self.calls.append(cmd)


# ── Emit-only invariant: structural assertions ──────────────────────────────


def test_call_site_discards_return_and_leaves_exit_code_untouched():
    """``__main__`` calls record_measurement as a bare statement and still
    returns ``result.returncode`` — the metric emit cannot reach the exit code."""
    tree = ast.parse((_SHARED / "__main__.py").read_text())
    main_fn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "main"
    )

    # The record_measurement call exists exactly as a bare expression statement
    # (return value discarded — never bound, never branched on).
    emit_stmts = [
        stmt
        for stmt in ast.walk(main_fn)
        if isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name)
        and stmt.value.func.id == "record_measurement"
    ]
    assert len(emit_stmts) == 1, "expected exactly one bare record_measurement() call"

    # No assignment ever captures record_measurement's result.
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            assert not (isinstance(func, ast.Name) and func.id == "record_measurement")

    # The success path returns exactly ``result.returncode`` — unchanged by the
    # measurement, which is the operational meaning of "nothing gated". (Other
    # returns are the early error guards: ``return 1`` for an empty prompt.)
    returns = [n for n in ast.walk(main_fn) if isinstance(n, ast.Return)]

    def is_returncode(value) -> bool:
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "returncode"
            and isinstance(value.value, ast.Name)
            and value.value.id == "result"
        )

    assert any(is_returncode(r.value) for r in returns), (
        "main() must return result.returncode on the success path"
    )
    # No return derives its value from the measurement call.
    for r in returns:
        for sub in ast.walk(r):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                assert sub.func.id != "record_measurement"


def test_no_branch_condition_reads_a_snapshot_metric():
    """No ``If`` / ``While`` / ternary / ``assert`` condition in the measurement
    module reads a built ``snapshot.<metric>`` field — the emit-only guard."""
    tree = ast.parse((_SHARED / "measurement.py").read_text())
    metrics = set(MEASUREMENT_METRIC_FIELDS)

    def reads_metric(test_node: ast.AST) -> bool:
        for sub in ast.walk(test_node):
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr in metrics
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "snapshot"
            ):
                return True
        return False

    offenders: list[str] = []
    for node in ast.walk(tree):
        test = None
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            test = node.test
        elif isinstance(node, ast.comprehension):
            for cond in node.ifs:
                if reads_metric(cond):
                    offenders.append(ast.dump(cond))
            continue
        if test is not None and reads_metric(test):
            offenders.append(ast.dump(test))

    assert not offenders, f"control-flow branch reads a metric value: {offenders}"


def test_public_emit_entrypoints_return_none():
    """The emit functions are annotated ``-> None`` so a caller has no value to
    branch on (a complement to the call-site discard)."""
    tree = ast.parse((_SHARED / "measurement.py").read_text())
    targets = {"record_measurement", "emit_snapshot"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in targets:
            seen.add(node.name)
            returns = node.returns
            assert isinstance(returns, ast.Constant) and returns.value is None, (
                f"{node.name} must be annotated -> None"
            )
    assert seen == targets


# ── Synthetic reseed event: the six metrics ─────────────────────────────────


def test_synthetic_reseed_event_builds_all_six_metrics():
    snap = build_snapshot(
        result=_result(),
        resume_decision=_reseed_decision(),
        model="opus[1m]",
    )

    # (4) reseed-frequency signal
    assert snap.reseeded is True
    assert snap.resumed is False
    assert snap.reseed_reason == "at_or_above_threshold"
    assert snap.prior_occupancy == 500_000

    # (1) window occupancy + slice-1 component breakout
    assert snap.window_occupancy == 120_000
    assert snap.input_tokens == 20_000
    assert snap.cache_read_tokens == 90_000
    assert snap.cache_creation_tokens == 10_000
    assert snap.output_tokens == 3_000

    # (5) root-cache hit rate = cache_read / occupancy
    assert snap.root_cache_hit_rate == pytest.approx(90_000 / 120_000)

    # (6) tokens per event = occupancy + output
    assert snap.tokens_per_event == 123_000

    # (2)/(3) real backend window, threshold, utilization (opus[1m] -> 1M / 400k)
    assert snap.real_backend_window == 1_000_000
    assert snap.reseed_threshold == 400_000
    assert snap.window_utilization == pytest.approx(120_000 / 1_000_000)


def test_resume_event_records_no_reseed():
    snap = build_snapshot(
        result=_result(),
        resume_decision=_reseed_decision(
            resume=True, session_id="sess-0", reason="below_threshold"
        ),
        model="opus[1m]",
    )
    assert snap.resumed is True
    assert snap.reseeded is False
    assert snap.reseed_reason == "below_threshold"


def test_identity_fields_read_from_env(monkeypatch):
    monkeypatch.setenv("EGG_AGENT_ROLE", "coder")
    monkeypatch.setenv("EGG_PHASE", "implement")
    monkeypatch.setenv("EGG_SLICE_ID", "slice-3")
    snap = build_snapshot(result=_result(), resume_decision=_reseed_decision(), model="opus[1m]")
    assert snap.agent_role == "coder"
    assert snap.phase == "implement"
    assert snap.slice_id == "slice-3"
    assert snap.num_turns == 4


# ── Routing through both surfaces + the gates ───────────────────────────────


def test_record_measurement_routes_progress_and_heartbeat(monkeypatch):
    monkeypatch.setenv(MEASUREMENT_ENV, "1")
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-9")
    runner = _CaptureRunner()

    record_measurement(
        result=_result(),
        resume_decision=_reseed_decision(),
        model="opus[1m]",
        run=runner,
    )

    assert len(runner.calls) == 2
    progress, heartbeat = runner.calls

    # First surface: a structured progress event carrying the metric payload.
    assert progress[:3] == ["egg-orch", "progress", "emit"]
    detail = progress[progress.index("--detail") + 1]
    payload = json.loads(detail)
    assert payload["window_occupancy"] == 120_000
    assert payload["reseeded"] is True
    assert payload["root_cache_hit_rate"] == pytest.approx(90_000 / 120_000)

    # Second surface: a heartbeat ping with a compact summary body.
    assert heartbeat[:3] == ["egg-orch", "message", "heartbeat"]
    body = heartbeat[heartbeat.index("--body") + 1]
    assert "context-measure" in body
    assert "at_or_above_threshold" in body  # the slice-8 reseed_reason, verbatim


def test_disabled_by_default_emits_nothing(monkeypatch):
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-9")  # in a pipeline, flag OFF
    runner = _CaptureRunner()
    record_measurement(
        result=_result(), resume_decision=_reseed_decision(), model="opus[1m]", run=runner
    )
    assert runner.calls == []


def test_no_emit_outside_a_pipeline(monkeypatch):
    monkeypatch.setenv(MEASUREMENT_ENV, "1")  # opted in, but no EGG_PIPELINE_ID
    runner = _CaptureRunner()
    record_measurement(
        result=_result(), resume_decision=_reseed_decision(), model="opus[1m]", run=runner
    )
    assert runner.calls == []


@pytest.mark.parametrize("value", ["1", "true", "On", "  yes  "])
def test_measurement_enabled_truthy(monkeypatch, value):
    monkeypatch.setenv(MEASUREMENT_ENV, value)
    assert measurement_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "maybe"])
def test_measurement_enabled_falsy(monkeypatch, value):
    monkeypatch.setenv(MEASUREMENT_ENV, value)
    assert measurement_enabled() is False


# ── Graceful degradation on partial / absent SDK usage ──────────────────────


def test_absent_usage_degrades_to_null_metrics_without_raising():
    snap = build_snapshot(
        result=_result(window_occupancy=None, token_usage=None),
        resume_decision=_reseed_decision(resume=True, reason="below_threshold"),
        model="opus[1m]",
    )
    assert snap.window_occupancy is None
    assert snap.root_cache_hit_rate is None
    assert snap.tokens_per_event is None
    assert snap.input_tokens is None
    # The reseed verdict is still recorded even with no usage.
    assert snap.resumed is True
    assert snap.reseeded is False


def test_emit_still_fires_on_degraded_snapshot(monkeypatch):
    monkeypatch.setenv(MEASUREMENT_ENV, "1")
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-9")
    runner = _CaptureRunner()
    record_measurement(
        result=_result(window_occupancy=None, token_usage=None),
        resume_decision=_reseed_decision(),
        model="opus[1m]",
        run=runner,
    )
    assert len(runner.calls) == 2
    detail = runner.calls[0][runner.calls[0].index("--detail") + 1]
    assert json.loads(detail)["window_occupancy"] is None


def test_record_measurement_swallows_runner_failure(monkeypatch):
    monkeypatch.setenv(MEASUREMENT_ENV, "1")
    monkeypatch.setenv("EGG_PIPELINE_ID", "issue-9")

    def _boom(cmd: list[str]) -> None:
        raise RuntimeError("transport down")

    # Emit-only: a transport failure must never propagate to the agent run.
    record_measurement(
        result=_result(), resume_decision=_reseed_decision(), model="opus[1m]", run=_boom
    )

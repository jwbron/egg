"""Tests for scripts/scaffold_first_telemetry.py.

Covers the BRC-history → scaffold-first signal pipeline:

* Tester heartbeat with scaffold language before upstream propose → signal=yes.
* Tester heartbeat with non-scaffold language → signal=no.
* No tester / no upstream / upstream never proposed → ineligible (skipped).
* Wait-minutes math when both producers propose.
* Pipeline id parsing strips the phase suffix.

The script is intentionally tolerant of malformed input (missing fields,
unparseable timestamps, non-list JSON) so callers running it against
historical data don't fail on one bad row. Tests cover those tolerances.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow ``import scaffold_first_telemetry`` from the scripts directory
# (mirrors the pattern in test_check_model_versions.py).
_scripts_path = Path(__file__).parent.parent
if str(_scripts_path) not in sys.path:
    sys.path.insert(0, str(_scripts_path))

import scaffold_first_telemetry as sft  # noqa: E402  (path-injection above)


def _msg(
    role: str,
    msg_type: str,
    timestamp: str,
    *,
    body: str = "",
    state: str | None = None,
) -> dict:
    """Build a minimal BRC message dict matching the on-disk shape."""
    msg: dict = {
        "from_role": role,
        "to_role": "all",
        "message_type": msg_type,
        "subject": f"{msg_type.lower()}",
        "body": body,
        "metadata": {},
        "timestamp": timestamp,
        "phase": "implement",
    }
    if state:
        msg["metadata"]["state"] = state
    return msg


def _write_history(path: Path, messages: list[dict]) -> None:
    path.write_text(json.dumps(messages), encoding="utf-8")


# ── pipeline_id parsing ─────────────────────────────────────────────────────


def test_pipeline_id_strips_phase_suffix(tmp_path: Path) -> None:
    """Filenames are ``<id>-<phase>.json``; the id excludes the phase suffix."""
    assert sft._pipeline_id_from_filename(tmp_path / "1556-implement.json") == "1556"
    assert (
        sft._pipeline_id_from_filename(tmp_path / "issue-1907-v2-implement.json") == "issue-1907-v2"
    )
    assert sft._pipeline_id_from_filename(tmp_path / "pipeline-2d7-plan.json") == "pipeline-2d7"


def test_pipeline_id_unknown_suffix_returns_full_stem(tmp_path: Path) -> None:
    """Unrecognized suffix → return the whole stem unchanged."""
    assert sft._pipeline_id_from_filename(tmp_path / "weird-name.json") == "weird-name"


# ── scaffold-signal keyword matching ────────────────────────────────────────


def test_scaffold_signal_matches_keyword_variants() -> None:
    """All documented scaffold keywords trigger the signal."""
    bodies = [
        "Drafted test scaffolding for Phase 4 (7 test files).",
        "Prepared tests for the auth-widening surface.",
        "Sketched fixture for the new client.",
        "Wrote test signatures for tasks 1-3.",
        "Added test stubs covering edge cases.",
    ]
    heartbeats = [{"body": b} for b in bodies]
    matched, excerpts = sft._scaffold_signal(heartbeats)
    assert matched
    assert len(excerpts) == len(bodies)


def test_scaffold_signal_ignores_unrelated_bodies() -> None:
    """Generic 'waiting on coder' bodies must not fire the signal.

    The ``drafted`` and ``signature`` keywords are qualified with
    test-context anchors, so unrelated phrasing like "drafted plan" or
    "method signature changed in dep" must NOT trigger.
    """
    heartbeats = [
        {"body": "Waiting on coder for CONSENSUS_PROPOSE."},
        {"body": "Reviewing the contract."},
        {"body": ""},
        {"body": "Drafted plan for Phase 2 implementation."},
        {"body": "Method signature changed in upstream dep."},
    ]
    matched, excerpts = sft._scaffold_signal(heartbeats)
    assert not matched
    assert excerpts == []


def test_scaffold_signal_truncates_long_bodies() -> None:
    """Excerpts cap at ~140 chars so verbose output stays readable."""
    long_body = "Drafted test scaffolding " + "x" * 500
    matched, excerpts = sft._scaffold_signal([{"body": long_body}])
    assert matched
    assert len(excerpts) == 1
    assert len(excerpts[0]) <= 140


# ── analyze_file: end-to-end on synthetic BRC histories ─────────────────────


def test_analyze_file_detects_scaffold_first(tmp_path: Path) -> None:
    """Tester heartbeat with scaffold language before coder propose → yes."""
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:55+00:00",
            body="Drafted test scaffolding for Phase 4 (7 test files).",
            state="WAITING_ON_ROLE",
        ),
        _msg(
            "coder",
            "CONSENSUS_PROPOSE",
            "2026-04-24T00:33:36+00:00",
            body="Coder propose.",
        ),
        _msg(
            "tester",
            "CONSENSUS_PROPOSE",
            "2026-04-24T00:57:54+00:00",
            body="Tester propose.",
        ),
    ]
    f = tmp_path / "1556-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f)
    assert row.pipeline_id == "1556"
    assert row.has_tester
    assert row.has_upstream
    assert row.tester_scaffold_signal
    assert row.tester_heartbeats_before_upstream == 1
    assert row.tester_wait_minutes is not None
    # tester proposed ~24 min after coder: (00:57:54 - 00:33:36) ≈ 24.3
    assert 24.0 < row.tester_wait_minutes < 25.0


def test_analyze_file_detects_no_scaffold(tmp_path: Path) -> None:
    """Generic 'waiting' heartbeats with no scaffold language → no signal."""
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:55+00:00",
            body="Waiting on coder.",
            state="WAITING_ON_ROLE",
        ),
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:33:36+00:00"),
        _msg("tester", "CONSENSUS_PROPOSE", "2026-04-24T00:50:00+00:00"),
    ]
    f = tmp_path / "1700-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f)
    assert row.has_tester
    assert row.has_upstream
    assert not row.tester_scaffold_signal
    assert row.tester_heartbeats_before_upstream == 1


def test_analyze_file_ignores_heartbeats_after_upstream_propose(tmp_path: Path) -> None:
    """Scaffold language after coder propose doesn't count — that's reactive
    work, not scaffold-first."""
    history = [
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:33:36+00:00"),
        # Scaffold language but AFTER coder's propose — should not count.
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:40:00+00:00",
            body="Drafting test scaffolding now that coder has committed.",
        ),
        _msg("tester", "CONSENSUS_PROPOSE", "2026-04-24T00:55:00+00:00"),
    ]
    f = tmp_path / "1701-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f)
    assert row.has_tester
    assert row.has_upstream
    assert not row.tester_scaffold_signal
    assert row.tester_heartbeats_before_upstream == 0


def test_analyze_file_ineligible_when_upstream_never_proposes(tmp_path: Path) -> None:
    """No CONSENSUS_PROPOSE from coder → ineligible (cannot evaluate)."""
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:55+00:00",
            body="Drafted test scaffolding.",
        ),
    ]
    f = tmp_path / "1702-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f)
    assert row.has_tester
    assert not row.has_upstream
    assert row.upstream_propose_ts is None
    # Still computes scaffold_signal=False since there's no wait window
    # to scope heartbeats to.
    assert not sft._eligible(row)


def test_analyze_file_ineligible_when_no_tester(tmp_path: Path) -> None:
    """Phases without a tester role → ineligible."""
    history = [
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:33:36+00:00"),
        _msg("documenter", "CONSENSUS_PROPOSE", "2026-04-24T00:35:00+00:00"),
    ]
    f = tmp_path / "1703-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f)
    assert not row.has_tester
    assert row.has_upstream
    assert not sft._eligible(row)


def test_analyze_file_handles_malformed_json(tmp_path: Path) -> None:
    """Malformed JSON → script logs to stderr and emits a no-data row."""
    f = tmp_path / "broken-implement.json"
    f.write_text("{not json", encoding="utf-8")

    row = sft.analyze_file(f)
    assert row.pipeline_id == "broken"
    assert not row.has_tester
    assert not row.has_upstream
    assert not sft._eligible(row)


def test_analyze_file_handles_non_list_json(tmp_path: Path) -> None:
    """Top-level non-list JSON → emits a no-data row instead of crashing."""
    f = tmp_path / "weird-implement.json"
    f.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    row = sft.analyze_file(f)
    assert not row.has_tester
    assert not sft._eligible(row)


def test_analyze_file_sorts_out_of_order_messages(tmp_path: Path) -> None:
    """Defensive sort: out-of-order entries don't break propose-window logic."""
    history = [
        # Tester scaffold heartbeat written AFTER coder's propose timestamp
        # but stored first in the file. After sorting, it should be in the
        # post-propose window and NOT count as scaffold-first.
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:40:00+00:00",
            body="Drafted test scaffolding (reactive).",
        ),
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:33:36+00:00"),
        _msg("tester", "CONSENSUS_PROPOSE", "2026-04-24T00:55:00+00:00"),
    ]
    f = tmp_path / "1704-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f)
    assert not row.tester_scaffold_signal


def test_analyze_file_custom_upstream(tmp_path: Path) -> None:
    """--upstream picks a non-default producer (e.g. a custom roster)."""
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:55+00:00",
            body="Drafted test scaffolding.",
        ),
        _msg("custom_producer", "CONSENSUS_PROPOSE", "2026-04-24T00:33:36+00:00"),
        _msg("tester", "CONSENSUS_PROPOSE", "2026-04-24T00:50:00+00:00"),
    ]
    f = tmp_path / "1705-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f, upstream="custom_producer")
    assert row.has_upstream
    assert row.tester_scaffold_signal


# ── _summarize aggregation ───────────────────────────────────────────────────


def test_summarize_computes_fraction_and_wait_stats() -> None:
    rows = [
        sft.PipelineRow(
            pipeline_id="a",
            source_file="a.json",
            has_tester=True,
            has_upstream=True,
            upstream_propose_ts="2026-04-24T00:00:00+00:00",
            tester_propose_ts="2026-04-24T00:10:00+00:00",
            tester_wait_minutes=10.0,
            tester_heartbeats_before_upstream=1,
            tester_scaffold_signal=True,
            tester_heartbeat_excerpts=["..."],
        ),
        sft.PipelineRow(
            pipeline_id="b",
            source_file="b.json",
            has_tester=True,
            has_upstream=True,
            upstream_propose_ts="2026-04-24T00:00:00+00:00",
            tester_propose_ts="2026-04-24T00:30:00+00:00",
            tester_wait_minutes=30.0,
            tester_heartbeats_before_upstream=0,
            tester_scaffold_signal=False,
            tester_heartbeat_excerpts=[],
        ),
        sft.PipelineRow(
            pipeline_id="c",
            source_file="c.json",
            has_tester=False,
            has_upstream=True,
            upstream_propose_ts="2026-04-24T00:00:00+00:00",
            tester_propose_ts=None,
            tester_wait_minutes=None,
            tester_heartbeats_before_upstream=0,
            tester_scaffold_signal=False,
            tester_heartbeat_excerpts=[],
        ),
    ]
    summary = sft._summarize(rows)
    assert summary["total_files"] == 3
    assert summary["eligible_pipelines"] == 2
    assert summary["ineligible_pipelines"] == 1
    assert summary["scaffold_signal_count"] == 1
    assert summary["scaffold_signal_fraction"] == 0.5
    # statistics.median averages the two middle values for even-length
    # samples — strict-median semantics, not "high median".
    assert summary["wait_minutes_median"] == 20.0


def test_summarize_handles_no_eligible_pipelines() -> None:
    """Empty / all-ineligible input → fraction=None, no crash."""
    rows: list[sft.PipelineRow] = []
    summary = sft._summarize(rows)
    assert summary["total_files"] == 0
    assert summary["scaffold_signal_fraction"] is None


# ── main(): JSON output and missing dir handling ────────────────────────────


def test_main_json_emits_one_record_per_file_plus_summary(tmp_path: Path, capsys) -> None:
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:00+00:00",
            body="Drafted test scaffolding.",
        ),
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:30:00+00:00"),
        _msg("tester", "CONSENSUS_PROPOSE", "2026-04-24T00:45:00+00:00"),
    ]
    _write_history(tmp_path / "1900-implement.json", history)

    rc = sft.main(["--brc-dir", str(tmp_path), "--json"])
    assert rc == 0
    out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    # one record per file + one summary record
    assert len(out_lines) == 2
    record = json.loads(out_lines[0])
    summary = json.loads(out_lines[1])
    assert record["pipeline_id"] == "1900"
    assert record["tester_scaffold_signal"] is True
    assert summary["summary"]["scaffold_signal_count"] == 1


def test_main_missing_brc_dir_returns_error(tmp_path: Path, capsys) -> None:
    rc = sft.main(["--brc-dir", str(tmp_path / "does-not-exist")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "is not a directory" in err


def test_main_text_output_includes_header_and_summary(tmp_path: Path, capsys) -> None:
    """Default (non-JSON) path renders the table header + aggregate summary."""
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:00+00:00",
            body="Drafted test scaffolding.",
        ),
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:30:00+00:00"),
        _msg("tester", "CONSENSUS_PROPOSE", "2026-04-24T00:45:00+00:00"),
    ]
    _write_history(tmp_path / "1901-implement.json", history)

    rc = sft.main(["--brc-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pipeline_id" in out
    assert "scaffold-first fraction" in out
    assert "1901" in out
    # 1/1 eligible pipelines matched scaffold-first.
    assert "1/1 (100.0%)" in out


def test_main_text_output_verbose_includes_excerpts(tmp_path: Path, capsys) -> None:
    """--verbose surfaces the matched heartbeat excerpts under each row."""
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:00+00:00",
            body="Drafted test scaffolding for Phase 4.",
        ),
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:30:00+00:00"),
    ]
    _write_history(tmp_path / "1902-implement.json", history)

    rc = sft.main(["--brc-dir", str(tmp_path), "--verbose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "matched: Drafted test scaffolding" in out


def test_heartbeats_before_handles_z_suffix_timestamps(tmp_path: Path) -> None:
    """``Z``-suffixed timestamps sort correctly against ``+00:00`` cutoffs.

    Lex compare on raw strings would order ``...Z`` AFTER ``...+00:00``
    even when the moments are equal, silently dropping pre-cutoff
    heartbeats. Datetime-parsed compare avoids that.
    """
    history = [
        _msg(
            "tester",
            "HEARTBEAT",
            "2026-04-24T00:10:00Z",
            body="Drafted test scaffolding.",
        ),
        _msg("coder", "CONSENSUS_PROPOSE", "2026-04-24T00:30:00+00:00"),
    ]
    f = tmp_path / "1903-implement.json"
    _write_history(f, history)

    row = sft.analyze_file(f)
    assert row.has_tester
    assert row.has_upstream
    assert row.tester_heartbeats_before_upstream == 1
    assert row.tester_scaffold_signal

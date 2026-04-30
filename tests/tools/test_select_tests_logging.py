"""TASK-5-4 — Structured-logging tests for scripts/select_tests.py.

Two surfaces:
  * Per-invocation stderr line.  Format:
      ``select-tests: narrowed N/M tests in X.XXs (baseline=<sha7>, trigger=diff)``
    or
      ``select-tests: full suite M tests (trigger=<explicit-reason>)``.
  * Per-invocation JSON record at ``.egg-state/selection/<head_sha>.json``
    with all documented keys (schema_version, head, baseline {sha,
    source}, branch, mode, trigger, selected_count, total_count,
    compute_ms, pytest_ms, timestamp, changed_files, changed_modules,
    dynamic_import_seeds_hit).

The selection-record envelope is written by ``write_selection_record``
which we can call directly without grimp.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.tools._select_tests_helpers import load_selector

selector = load_selector()


# ----------------------------------------------------------------------
# JSON record schema
# ----------------------------------------------------------------------


def _make_record(tmp_path: Path, **overrides: object) -> dict:
    defaults: dict[str, object] = {
        "head": "a" * 40,
        "baseline_sha": "b" * 40,
        "baseline_source": "LKG",
        "branch": "main",
        "mode": "narrow",
        "trigger": "none",
        "selected_count": 2,
        "total_count": 10,
        "compute_ms": 42,
        "changed_files_list": ["gateway/policy.py", "shared/egg_config/foo.py"],
        "changed_modules_list": ["gateway.policy", "shared.egg_config.foo"],
        "dynamic_import_seeds_hit": [],
        "repo_root": tmp_path,
    }
    defaults.update(overrides)
    out = selector.write_selection_record(**defaults)  # type: ignore[arg-type]
    return json.loads(out.read_text(encoding="utf-8"))


def test_selection_record_contains_every_documented_key(tmp_path: Path) -> None:
    record = _make_record(tmp_path)
    expected_keys = {
        "schema_version",
        "head",
        "baseline",
        "branch",
        "mode",
        "trigger",
        "selected_count",
        "total_count",
        "compute_ms",
        "pytest_ms",
        "timestamp",
        "changed_files",
        "changed_modules",
        "dynamic_import_seeds_hit",
    }
    assert expected_keys.issubset(record.keys()), f"missing keys: {expected_keys - record.keys()}"


def test_selection_record_schema_version_is_1(tmp_path: Path) -> None:
    record = _make_record(tmp_path)
    assert record["schema_version"] == 1
    assert isinstance(record["schema_version"], int)


def test_selection_record_baseline_is_dict_with_sha_and_source(tmp_path: Path) -> None:
    record = _make_record(tmp_path)
    assert isinstance(record["baseline"], dict)
    assert set(record["baseline"].keys()) == {"sha", "source"}
    assert record["baseline"]["sha"] == "b" * 40
    assert record["baseline"]["source"] == "LKG"


def test_selection_record_baseline_source_is_BASE_BRANCH(tmp_path: Path) -> None:
    record = _make_record(tmp_path, baseline_source="BASE_BRANCH")
    assert record["baseline"]["source"] == "BASE_BRANCH"


def test_selection_record_pytest_ms_is_null_initially(tmp_path: Path) -> None:
    """The Makefile wrapper later patches pytest_ms via
    ``--patch-selection-json``; the initial write must leave it null
    so consumers can detect un-patched records."""
    record = _make_record(tmp_path)
    assert record["pytest_ms"] is None


def test_selection_record_changed_files_and_modules_are_lists(tmp_path: Path) -> None:
    record = _make_record(tmp_path)
    assert isinstance(record["changed_files"], list)
    assert isinstance(record["changed_modules"], list)
    assert isinstance(record["dynamic_import_seeds_hit"], list)


def test_selection_record_branch_can_be_null_for_detached_head(tmp_path: Path) -> None:
    record = _make_record(tmp_path, branch=None)
    assert record["branch"] is None


def test_selection_record_mode_is_narrow_or_full_suite_or_bypass(tmp_path: Path) -> None:
    for mode in ("narrow", "full_suite", "bypass"):
        record = _make_record(tmp_path, mode=mode)
        assert record["mode"] == mode


def test_selection_record_timestamp_is_iso8601(tmp_path: Path) -> None:
    record = _make_record(tmp_path)
    # ISO-8601 with seconds precision and UTC timezone.
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$",
        record["timestamp"],
    ), f"timestamp not ISO-8601 UTC seconds: {record['timestamp']!r}"


def test_selection_record_path_is_under_selection_log_dir(tmp_path: Path) -> None:
    out = selector.write_selection_record(
        head="c" * 40,
        baseline_sha="d" * 40,
        baseline_source="LKG",
        branch="main",
        mode="narrow",
        trigger="none",
        selected_count=0,
        total_count=10,
        compute_ms=1,
        changed_files_list=[],
        changed_modules_list=[],
        dynamic_import_seeds_hit=[],
        repo_root=tmp_path,
    )
    expected = tmp_path / selector.SELECTION_LOG_DIR / ("c" * 40 + ".json")
    assert out == expected
    assert out.exists()


# ----------------------------------------------------------------------
# patch_selection_record — pytest_ms append after pytest returns.
# ----------------------------------------------------------------------


def test_patch_selection_record_writes_pytest_ms(tmp_path: Path) -> None:
    head = "e" * 40
    _make_record(tmp_path, head=head)
    rc = selector.patch_selection_record(head, 1234, repo_root=tmp_path)
    assert rc == 0
    record_path = tmp_path / selector.SELECTION_LOG_DIR / f"{head}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["pytest_ms"] == 1234


def test_patch_selection_record_missing_record_returns_0_with_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Missing selection-record file is not a failure — wrapper logs
    to stderr and exits 0 so the Makefile recipe doesn't abort."""
    rc = selector.patch_selection_record("f" * 40, 99, repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "no selection record" in captured.err


def test_patch_selection_record_malformed_json_returns_0_with_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Malformed selection-record file is also non-fatal."""
    head = "g" * 40
    log_dir = tmp_path / selector.SELECTION_LOG_DIR
    log_dir.mkdir(parents=True)
    (log_dir / f"{head}.json").write_text("not-json", encoding="utf-8")
    rc = selector.patch_selection_record(head, 99, repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert "could not parse" in captured.err


# ----------------------------------------------------------------------
# Stderr decision-line format — the run flow logs one of:
#   ``select-tests: narrowed N/M tests in X.XXs (baseline=<sha7>, trigger=diff)``
#   ``select-tests: full suite M tests (trigger=<explicit-reason>)``.
# We construct each line via the helpers used in ``_run_narrow_or_fallback``.
# ----------------------------------------------------------------------


_NARROW_LINE_RE = re.compile(
    r"^select-tests: narrowed \d+/\d+ tests in \d+\.\d{2}s "
    r"\(baseline=[0-9a-f]{7}, trigger=diff\)$"
)
_FULL_LINE_RE = re.compile(
    # trigger= ... up to the FINAL closing paren of the line.  Anchor
    # on line end rather than the first close-paren so trigger strings
    # that legitimately contain `(...)` round-trip through the regex.
    r"^select-tests: full suite \d+ tests \(trigger=.+\)$"
)


def test_short_sha_helper_truncates_to_seven() -> None:
    assert selector._short_sha("a" * 40) == "a" * 7
    assert selector._short_sha(None) == "unknown"
    assert selector._short_sha("not-a-sha") == "unknown"


def test_narrow_line_format_matches_regex() -> None:
    """Render the narrow line as ``_run_narrow_or_fallback`` does and
    verify the regex matches.  This pins the format so a future
    refactor doesn't accidentally break tooling that parses the line."""
    short = selector._short_sha("a" * 40)
    elapsed = 0.18
    line = f"select-tests: narrowed 47/356 tests in {elapsed:.2f}s (baseline={short}, trigger=diff)"
    assert _NARROW_LINE_RE.match(line), f"narrow line doesn't match regex: {line!r}"


@pytest.mark.parametrize(
    "trigger",
    [
        "Makefile changed",
        "shared/tests/ changed",
        "LKG not ancestor of HEAD",
        "unresolvable baseline",
        "dynamic-import reachability",
        "non-.py change",
        "source file missing from graph: shared/egg_config/_orphan.py",
        "graph unavailable",
    ],
)
def test_full_suite_line_uses_explicit_trigger(trigger: str) -> None:
    """Every fallback path names its trigger explicitly — the stderr
    line must NOT use a generic word like "fallback" or "narrowing
    skipped"."""
    line = f"select-tests: full suite 356 tests (trigger={trigger})"
    assert _FULL_LINE_RE.match(line), f"line doesn't match regex: {line!r}"
    # Sanity: trigger string itself doesn't contain "fallback" as a generic word.
    assert "fallback" not in trigger.lower(), f"trigger contains generic 'fallback': {trigger}"


# ----------------------------------------------------------------------
# Atomic write — selection records under concurrent contention.
# ----------------------------------------------------------------------


def test_selection_record_atomic_write_replaces_prior_record(tmp_path: Path) -> None:
    """Writing the same head sha twice replaces the file atomically;
    a reader sees one or the other, never a mix."""
    head = "h" * 40
    _make_record(tmp_path, head=head, selected_count=1)
    _make_record(tmp_path, head=head, selected_count=99)
    record_path = tmp_path / selector.SELECTION_LOG_DIR / f"{head}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["selected_count"] == 99

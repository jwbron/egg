"""Tests for scripts/check-ledger-references.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check-ledger-references.py"
_spec = importlib.util.spec_from_file_location("check_ledger_references", _SCRIPT_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
# Register in sys.modules so dataclasses can resolve __module__ during class
# construction (Python 3.14 _is_type lookup).
sys.modules["check_ledger_references"] = _mod
_spec.loader.exec_module(_mod)

LEDGER_PATTERN = _mod.LEDGER_PATTERN
evaluate = _mod.evaluate
is_excluded = _mod.is_excluded
is_test_path = _mod.is_test_path
iter_scanned_files = _mod.iter_scanned_files
load_baseline = _mod.load_baseline
scan_all = _mod.scan_all
scan_file = _mod.scan_file
update_baseline = _mod.update_baseline
write_baseline = _mod.write_baseline


class TestPattern:
    @pytest.mark.parametrize(
        "text",
        [
            "added in slice-4",
            "slice-12 landed it",
            "Slice-4 at a sentence start",  # slice-N is case-insensitive
            "SLICE-7 shouted",
            "see TASK-4-5",
            "TASK-9 was the task",
            "per cq-2",
            "resolved by cq-10",
            "per CQ-2",  # cq-N is case-insensitive
        ],
    )
    def test_matches_ledger_tokens(self, text: str) -> None:
        assert LEDGER_PATTERN.search(text)

    @pytest.mark.parametrize(
        "text",
        [
            "slice_id is the runtime key",  # underscore identifier
            "contract.slices iterates each slice",  # dotted access
            "EGG_ACTIVE_SLICES env var",  # env var
            "the slice DAG model",  # bare word
            "a task description",  # bare word
            "acquittal",  # 'cq' substring inside a word
            # TASK-N is UPPERCASE-only: lowercase task-N is live runtime
            # vocabulary, not ledger narration, and must not match.
            "task-5 is a runtime contract id",
            "task-20251129-222239 is a timestamped run id",
            "see task-123 in the tool docs example",
        ],
    )
    def test_ignores_live_vocabulary(self, text: str) -> None:
        assert not LEDGER_PATTERN.search(text)


class TestScanFile:
    def test_counts_occurrences_not_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.md"
        f.write_text("slice-1 then slice-2 on one line\nand TASK-3-1 here\n")
        findings = scan_file(f, tmp_path)
        assert findings.count == 3
        assert len(findings.lines) == 2

    def test_suppress_marker_excludes_line(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.md"
        f.write_text("slice-1 narration here\nlive error: slice-1 -> slice-2 <!-- ledger-ok -->\n")
        findings = scan_file(f, tmp_path)
        assert findings.count == 1
        assert findings.lines[0][0] == 1

    def test_no_matches_is_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.md"
        f.write_text("This doc describes the current state of the system.\n")
        assert scan_file(f, tmp_path).count == 0


class TestExclusions:
    def test_egg_state_excluded(self) -> None:
        assert is_excluded(".egg-state/brc-history/42-implement-slice-1.md")

    def test_plan_template_excluded(self) -> None:
        assert is_excluded("docs/templates/plan.md")

    def test_normal_doc_not_excluded(self) -> None:
        assert not is_excluded("docs/architecture/orchestrator.md")

    def test_test_files_skipped(self) -> None:
        assert is_test_path(Path("orchestrator/tests/test_foo.py"))
        assert is_test_path(Path("orchestrator/foo_test.py"))
        assert is_test_path(Path("gateway/test_bar.py"))
        assert not is_test_path(Path("orchestrator/routes/pipelines.py"))


class TestIterScannedFiles:
    def test_collects_md_and_nontest_py(self, tmp_path: Path) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.md").write_text("slice-1\n")
        (tmp_path / "orchestrator").mkdir()
        (tmp_path / "orchestrator" / "src.py").write_text("# slice-1\n")
        (tmp_path / "orchestrator" / "test_src.py").write_text("# slice-1\n")
        rels = sorted(p.relative_to(tmp_path).as_posix() for p in iter_scanned_files(tmp_path))
        assert rels == ["docs/guide.md", "orchestrator/src.py"]

    def test_excluded_prefixes_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".egg-state").mkdir()
        (tmp_path / ".egg-state" / "x.md").write_text("slice-1\n")
        (tmp_path / "docs" / "templates").mkdir(parents=True)
        (tmp_path / "docs" / "templates" / "plan.md").write_text("TASK-1-1\n")
        (tmp_path / "docs" / "real.md").write_text("slice-1\n")
        rels = sorted(p.relative_to(tmp_path).as_posix() for p in iter_scanned_files(tmp_path))
        assert rels == ["docs/real.md"]

    def test_test_dir_markdown_excluded(self, tmp_path: Path) -> None:
        """Markdown under a test directory is excluded too (symmetry with .py)."""
        (tmp_path / "gateway" / "tests").mkdir(parents=True)
        (tmp_path / "gateway" / "tests" / "README.md").write_text("slice-1 fixture\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "real.md").write_text("slice-1\n")
        rels = sorted(p.relative_to(tmp_path).as_posix() for p in iter_scanned_files(tmp_path))
        assert rels == ["docs/real.md"]


class TestEvaluate:
    def test_over_baseline_is_net_new(self) -> None:
        findings = {"a.md": _mod.FileFindings(rel="a.md", count=5, lines=())}
        assert [f.rel for f in evaluate(findings, {"a.md": 3})] == ["a.md"]

    def test_at_baseline_is_clean(self) -> None:
        findings = {"a.md": _mod.FileFindings(rel="a.md", count=3, lines=())}
        assert evaluate(findings, {"a.md": 3}) == []

    def test_under_baseline_is_clean(self) -> None:
        """Lowering the count (de-ledgering) must never warn."""
        findings = {"a.md": _mod.FileFindings(rel="a.md", count=1, lines=())}
        assert evaluate(findings, {"a.md": 3}) == []

    def test_new_file_with_refs_is_net_new(self) -> None:
        findings = {"a.md": _mod.FileFindings(rel="a.md", count=1, lines=())}
        assert [f.rel for f in evaluate(findings, {})] == ["a.md"]


class TestBaselineRoundTrip:
    def test_write_then_load(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        write_baseline({"docs/a.md": 5, "docs/b.md": 2}, path)
        assert load_baseline(path) == {"docs/a.md": 5, "docs/b.md": 2}

    def test_write_drops_zero_counts(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.yaml"
        write_baseline({"docs/a.md": 0, "docs/b.md": 2}, path)
        assert load_baseline(path) == {"docs/b.md": 2}

    def test_load_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert load_baseline(tmp_path / "absent.yaml") == {}

    def test_update_baseline_snapshots_current(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "a.md").write_text("slice-1 and slice-2\n")
        path = tmp_path / "baseline.yaml"
        rc = update_baseline(tmp_path, path)
        capsys.readouterr()
        assert rc == 0
        assert load_baseline(path) == {"docs/a.md": 2}


# NOTE: the committed-baseline freshness check intentionally lives in the
# advisory `scripts/check-ledger-references.py` script (surfaced via
# `make lint-custom`), NOT in this blocking unit suite. Asserting the live merge
# corpus against the committed baseline inside `make test-all` turned the
# advisory ratchet into a de-facto hard CI gate: any later PR that merged
# `main`'s new slice-N / TASK-N / cq-N tokens would redden the blocking suite
# until someone re-ran `--update-baseline`, a maintenance treadmill at odds with
# the issue's "advisory, never block" intent (#3328). The advisory run prints the
# net-new files and the `--update-baseline` reminder without blocking a PR.

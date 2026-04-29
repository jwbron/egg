"""Tests for scripts/check-file-sizes.py."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check-file-sizes.py"
_spec = importlib.util.spec_from_file_location("check_file_sizes", _SCRIPT_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
# Register in sys.modules so dataclasses can resolve __module__ during class
# construction (Python 3.14 _is_type lookup).
sys.modules["check_file_sizes"] = _mod
_spec.loader.exec_module(_mod)

Caps = _mod.Caps
Config = _mod.Config
FileStats = _mod.FileStats
evaluate = _mod.evaluate
is_test_file = _mod.is_test_file
iter_source_files = _mod.iter_source_files
load_config = _mod.load_config
measure = _mod.measure
update_allowlist = _mod.update_allowlist
write_allowlist = _mod.write_allowlist


@pytest.fixture
def caps() -> Caps:
    return Caps(hard_lines=1500, hard_bytes=100_000, soft_lines=800, soft_bytes=60_000)


@pytest.fixture
def empty_config(caps: Caps) -> Config:
    return Config(caps=caps, allowlist={})


def _stats(lines: int, bts: int, name: str = "x.py") -> FileStats:
    return FileStats(path=Path(name), lines=lines, bytes=bts)


class TestEvaluate:
    def test_under_all_caps_passes(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(100, 5_000), "x.py", empty_config)
        assert errors == []
        assert warnings == []

    def test_over_hard_lines_fails(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(1501, 50_000), "x.py", empty_config)
        assert len(errors) == 1
        assert "exceeds hard cap" in errors[0]
        assert warnings == []

    def test_over_hard_bytes_fails(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(100, 100_001), "x.py", empty_config)
        assert len(errors) == 1
        assert "exceeds hard cap" in errors[0]

    def test_over_soft_lines_warns(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(801, 30_000), "x.py", empty_config)
        assert errors == []
        assert len(warnings) == 1
        assert "soft cap" in warnings[0]

    def test_over_soft_bytes_warns(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(100, 60_001), "x.py", empty_config)
        assert errors == []
        assert len(warnings) == 1

    def test_at_caps_passes(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(1500, 100_000), "x.py", empty_config)
        # Exactly at the hard cap is allowed; soft cap is exceeded -> warns.
        assert errors == []
        assert len(warnings) == 2

    def test_allowlisted_over_hard_cap_passes(self, caps: Caps) -> None:
        config = Config(caps=caps, allowlist={"big.py": "2248"})
        errors, warnings = evaluate(_stats(2000, 80_000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_growth_is_allowed(self, caps: Caps) -> None:
        """Allowlisted files may grow freely -- removing the baseline check
        is the whole point of dropping per-file lines/bytes from the YAML."""
        config = Config(caps=caps, allowlist={"big.py": "2248"})
        errors, warnings = evaluate(_stats(50_000, 5_000_000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_with_no_issue_link_still_allowed(self, caps: Caps) -> None:
        """Membership alone gates the lint; the issue field is documentation."""
        config = Config(caps=caps, allowlist={"big.py": None})
        errors, warnings = evaluate(_stats(2000, 80_000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_under_caps_passes_silently(self, caps: Caps) -> None:
        """A file in the allowlist that's already shrunk under caps is silent.

        It should not produce a soft-cap warning for an allowlisted file.
        """
        config = Config(caps=caps, allowlist={"big.py": "2248"})
        errors, warnings = evaluate(_stats(900, 30_000), "big.py", config)
        assert errors == []
        assert warnings == []


class TestIsTestFile:
    def test_test_prefix(self) -> None:
        assert is_test_file(Path("orchestrator/test_foo.py"))

    def test_test_suffix(self) -> None:
        assert is_test_file(Path("orchestrator/foo_test.py"))

    def test_tests_subdir(self) -> None:
        assert is_test_file(Path("orchestrator/tests/anything.py"))

    def test_pycache(self) -> None:
        assert is_test_file(Path("orchestrator/__pycache__/foo.py"))

    def test_normal_source(self) -> None:
        assert not is_test_file(Path("orchestrator/routes/pipelines.py"))


class TestMeasure:
    def test_counts_lines_and_bytes(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n")
        s = measure(f)
        assert s.lines == 3
        assert s.bytes == 6

    def test_counts_no_trailing_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc")
        s = measure(f)
        assert s.lines == 3
        assert s.bytes == 5

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("")
        s = measure(f)
        assert s.lines == 0
        assert s.bytes == 0


class TestIterSourceFiles:
    def test_skips_tests_dir(self, tmp_path: Path) -> None:
        (tmp_path / "orchestrator").mkdir()
        (tmp_path / "orchestrator" / "tests").mkdir()
        (tmp_path / "orchestrator" / "good.py").write_text("x = 1\n")
        (tmp_path / "orchestrator" / "tests" / "test_a.py").write_text("x = 1\n")
        (tmp_path / "orchestrator" / "test_inline.py").write_text("x = 1\n")
        result = iter_source_files(tmp_path)
        rels = sorted(p.relative_to(tmp_path).as_posix() for p in result)
        assert rels == ["orchestrator/good.py"]

    def test_only_source_roots(self, tmp_path: Path) -> None:
        (tmp_path / "orchestrator").mkdir()
        (tmp_path / "orchestrator" / "x.py").write_text("x = 1\n")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "ignored.py").write_text("x = 1\n")
        result = iter_source_files(tmp_path)
        rels = [p.relative_to(tmp_path).as_posix() for p in result]
        assert rels == ["orchestrator/x.py"]

    def test_missing_root_is_fine(self, tmp_path: Path) -> None:
        # No source roots present at all -- should return empty list.
        assert iter_source_files(tmp_path) == []


class TestYamlRoundTrip:
    """Cover load_config + write_allowlist to guard against losing the
    ``issue:`` tracking field on --update-allowlist."""

    def _yaml(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "allowlist.yaml"
        p.write_text(textwrap.dedent(body))
        return p

    def test_load_preserves_issue_field(self, tmp_path: Path) -> None:
        path = self._yaml(
            tmp_path,
            """
            caps:
              hard_lines: 1500
              hard_bytes: 100000
              soft_lines: 800
              soft_bytes: 60000
            files:
              foo/bar.py:
                issue: "2248"
            """,
        )
        config = load_config(path)
        assert config.allowlist["foo/bar.py"] == "2248"

    def test_load_handles_missing_issue_field(self, tmp_path: Path) -> None:
        path = self._yaml(
            tmp_path,
            """
            caps:
              hard_lines: 1500
              hard_bytes: 100000
              soft_lines: 800
              soft_bytes: 60000
            files:
              foo/bar.py: {}
            """,
        )
        config = load_config(path)
        assert config.allowlist["foo/bar.py"] is None

    def test_load_handles_null_entry(self, tmp_path: Path) -> None:
        """An entry with no value at all is just a path -- the bare-key
        form keeps the YAML maximally compact when there's no issue link."""
        path = self._yaml(
            tmp_path,
            """
            caps:
              hard_lines: 1500
              hard_bytes: 100000
              soft_lines: 800
              soft_bytes: 60000
            files:
              foo/bar.py:
            """,
        )
        config = load_config(path)
        assert "foo/bar.py" in config.allowlist
        assert config.allowlist["foo/bar.py"] is None

    def test_load_ignores_legacy_lines_bytes(self, tmp_path: Path) -> None:
        """Legacy YAMLs with lines/bytes keys still load -- the lint just
        ignores the now-defunct fields. This protects branches that haven't
        rebased onto the simplified schema yet."""
        path = self._yaml(
            tmp_path,
            """
            caps:
              hard_lines: 1500
              hard_bytes: 100000
              soft_lines: 800
              soft_bytes: 60000
            files:
              foo/bar.py:
                lines: 2000
                bytes: 80000
                issue: "2248"
            """,
        )
        config = load_config(path)
        assert config.allowlist["foo/bar.py"] == "2248"

    def test_write_emits_issue_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        config = Config(caps=caps, allowlist={})
        write_allowlist(config, {"foo/bar.py": "2248"})
        loaded = yaml.safe_load(out.read_text())
        assert loaded["files"]["foo/bar.py"] == {"issue": "2248"}

    def test_write_omits_issue_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        config = Config(caps=caps, allowlist={})
        write_allowlist(config, {"foo/bar.py": None})
        loaded = yaml.safe_load(out.read_text())
        # Bare key (null value) when there's no issue link -- avoids
        # littering the YAML with `issue: null` placeholders.
        assert loaded["files"]["foo/bar.py"] is None

    def test_round_trip_preserves_issue(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        """Regression: --update-allowlist must not silently drop issue links."""
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        config = Config(caps=caps, allowlist={})
        original = {
            "a.py": "2248",
            "b.py": "9999",
            "c.py": None,
        }
        write_allowlist(config, original)
        reloaded = load_config(out)
        assert reloaded.allowlist == original

    def test_update_allowlist_carries_issue_forward(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """update_allowlist must preserve the existing entry's issue link."""
        repo_root = tmp_path / "repo"
        (repo_root / "orchestrator").mkdir(parents=True)
        # Write a file that will trip the (small) hard-line cap below.
        big = repo_root / "orchestrator" / "big.py"
        big.write_text("x = 1\n" * 50)

        # Seed deliberately stale line/byte values so we also confirm that
        # legacy fields don't trip up loading or get re-emitted on write.
        allowlist = tmp_path / "allowlist.yaml"
        allowlist.write_text(
            textwrap.dedent(
                """
                caps:
                  hard_lines: 10
                  hard_bytes: 1000000
                  soft_lines: 5
                  soft_bytes: 500000
                files:
                  orchestrator/big.py:
                    lines: 1
                    bytes: 1
                    issue: "2248"
                """
            )
        )
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", allowlist)

        rc = update_allowlist(repo_root)
        assert rc == 0
        capsys.readouterr()  # discard the "Wrote N entries" print

        reloaded = load_config(allowlist)
        assert reloaded.allowlist["orchestrator/big.py"] == "2248"
        # Legacy lines/bytes keys are not re-emitted on write.
        raw = yaml.safe_load(allowlist.read_text())
        entry = raw["files"]["orchestrator/big.py"]
        assert entry == {"issue": "2248"}

    def test_update_allowlist_adds_new_over_cap_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A newly oversize file not yet listed gets added with no issue."""
        repo_root = tmp_path / "repo"
        (repo_root / "orchestrator").mkdir(parents=True)
        big = repo_root / "orchestrator" / "new_big.py"
        big.write_text("x = 1\n" * 50)

        allowlist = tmp_path / "allowlist.yaml"
        allowlist.write_text(
            textwrap.dedent(
                """
                caps:
                  hard_lines: 10
                  hard_bytes: 1000000
                  soft_lines: 5
                  soft_bytes: 500000
                files: {}
                """
            )
        )
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", allowlist)

        rc = update_allowlist(repo_root)
        assert rc == 0
        capsys.readouterr()

        reloaded = load_config(allowlist)
        assert "orchestrator/new_big.py" in reloaded.allowlist
        assert reloaded.allowlist["orchestrator/new_big.py"] is None

    def test_update_allowlist_drops_now_under_cap_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A file that has shrunk under the cap is dropped from the allowlist."""
        repo_root = tmp_path / "repo"
        (repo_root / "orchestrator").mkdir(parents=True)
        small = repo_root / "orchestrator" / "small.py"
        small.write_text("x = 1\n")

        allowlist = tmp_path / "allowlist.yaml"
        allowlist.write_text(
            textwrap.dedent(
                """
                caps:
                  hard_lines: 10
                  hard_bytes: 1000000
                  soft_lines: 5
                  soft_bytes: 500000
                files:
                  orchestrator/small.py:
                    issue: "2248"
                """
            )
        )
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", allowlist)

        rc = update_allowlist(repo_root)
        assert rc == 0
        capsys.readouterr()

        reloaded = load_config(allowlist)
        assert "orchestrator/small.py" not in reloaded.allowlist

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
count_code_lines = _mod.count_code_lines
evaluate = _mod.evaluate
is_test_file = _mod.is_test_file
iter_source_files = _mod.iter_source_files
load_config = _mod.load_config
measure = _mod.measure
update_allowlist = _mod.update_allowlist
write_allowlist = _mod.write_allowlist


@pytest.fixture
def caps() -> Caps:
    return Caps(hard_code_lines=1000, soft_code_lines=500)


@pytest.fixture
def empty_config(caps: Caps) -> Config:
    return Config(caps=caps, allowlist={})


def _stats(code_lines: int, name: str = "x.py") -> FileStats:
    return FileStats(path=Path(name), code_lines=code_lines, raw_lines=code_lines * 2)


class TestCountCodeLines:
    """Only non-blank, non-comment, non-docstring lines count."""

    def test_blank_lines_excluded(self) -> None:
        assert count_code_lines("a = 1\n\n\nb = 2\n") == 2

    def test_whitespace_only_lines_excluded(self) -> None:
        assert count_code_lines("a = 1\n   \n\t\nb = 2\n") == 2

    def test_comment_only_lines_excluded(self) -> None:
        src = "# header\na = 1\n    # indented note\nb = 2\n"
        assert count_code_lines(src) == 2

    def test_trailing_comment_still_counts_its_line(self) -> None:
        """The line carries code, so it counts -- only *whole* comment
        lines are excluded."""
        assert count_code_lines("a = 1  # why\n") == 1

    def test_module_docstring_excluded(self) -> None:
        src = '"""Module doc.\n\nMore prose.\n"""\n\nimport os\n'
        assert count_code_lines(src) == 1

    def test_class_and_function_docstrings_excluded(self) -> None:
        src = textwrap.dedent(
            '''
            class Widget:
                """Class doc.

                Extended prose.
                """

                def method(self):
                    """Method doc."""
                    return 1
            '''
        )
        # class Widget: / def method: / return 1
        assert count_code_lines(src) == 3

    def test_async_function_docstring_excluded(self) -> None:
        src = textwrap.dedent(
            '''
            async def go():
                """Async doc.

                Prose.
                """
                return 1
            '''
        )
        assert count_code_lines(src) == 2

    def test_nested_function_docstring_excluded(self) -> None:
        src = textwrap.dedent(
            '''
            def outer():
                def inner():
                    """Nested doc.

                    Prose.
                    """
                    return 1

                return inner
            '''
        )
        # def outer / def inner / return 1 / return inner
        assert count_code_lines(src) == 4

    def test_non_docstring_string_literal_counts(self) -> None:
        """A multi-line string that is not in docstring position is code.

        Excluding it would just move the gaming vector: park prose in a
        module constant and it would stop counting.
        """
        src = 'TEMPLATE = """\nline one\nline two\n"""\n'
        assert count_code_lines(src) == 4

    def test_string_after_assignment_is_not_a_docstring(self) -> None:
        """PEP 224 attribute "docstrings" are not what ast.get_docstring
        recognises, so they count as code."""
        src = 'FOO = 1\n"""Doc for FOO."""\n'
        assert count_code_lines(src) == 2

    def test_hash_inside_a_string_is_not_a_comment(self) -> None:
        src = 'a = "# not a comment"\n'
        assert count_code_lines(src) == 1

    def test_empty_source(self) -> None:
        assert count_code_lines("") == 0

    def test_unparseable_source_counts_everything(self) -> None:
        """A file that does not parse falls back to counting every
        non-blank line, so a syntax error can never make a file measure
        smaller than a working one."""
        src = "def broken(:\n    x = 1\n\ny = 2\n"
        assert count_code_lines(src) == 3


class TestProseCannotLowerTheCount:
    """The property this whole check exists for (issue #3671).

    Under raw-line counting, the cheapest way to pass was to delete
    documentation -- and an agent did exactly that in commit 68b185ca,
    "trim health_monitor.py docstring under file-size hard cap", which
    took orchestrator/health_monitor.py from 1503 to 1498 raw lines
    across the old 1500 cap. Under this metric both revisions measure
    767 code lines, so that commit would have bought nothing. Every test
    here asserts the same property on synthetic inputs.
    """

    WITH_PROSE = textwrap.dedent(
        '''
        """Module docstring.

        A long explanation that a size-pressured agent would be tempted
        to delete, spanning several lines of genuinely useful prose.
        """

        import os


        class Widget:
            """Class docstring.

            More prose.
            """

            def method(self) -> int:
                """Method docstring."""
                # An explanatory comment.
                return 1


        async def afunc() -> None:
            """Async docstring.

            Still more prose.
            """
            await os.sleep(1)
        '''
    ).lstrip()

    # Same module with every docstring, comment and blank line removed.
    WITHOUT_PROSE = textwrap.dedent(
        """
        import os
        class Widget:
            def method(self) -> int:
                return 1
        async def afunc() -> None:
            await os.sleep(1)
        """
    ).lstrip()

    def test_stripping_all_prose_changes_nothing(self) -> None:
        assert count_code_lines(self.WITH_PROSE) == count_code_lines(self.WITHOUT_PROSE)

    def test_the_two_versions_differ_in_raw_lines(self) -> None:
        """Guard the guard: the fixtures really are different files, so
        the test above is asserting something."""
        assert len(self.WITH_PROSE.splitlines()) > len(self.WITHOUT_PROSE.splitlines()) + 10

    def test_deleting_only_the_module_docstring_changes_nothing(self) -> None:
        before = count_code_lines(self.WITH_PROSE)
        trimmed = self.WITH_PROSE.split('"""', 2)[2].lstrip("\n")
        assert '"""Module docstring.' not in trimmed
        assert count_code_lines(trimmed) == before

    def test_padding_a_docstring_changes_nothing(self) -> None:
        """The converse: growing prose cannot push a file over the cap
        either, so documenting a module is never penalised."""
        padded = self.WITH_PROSE.replace(
            "A long explanation",
            "\n".join(["Padding line."] * 200) + "\nA long explanation",
        )
        assert count_code_lines(padded) == count_code_lines(self.WITH_PROSE)

    def test_deleting_comments_and_blanks_changes_nothing(self) -> None:
        stripped = "\n".join(
            line
            for line in self.WITH_PROSE.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        assert count_code_lines(stripped) == count_code_lines(self.WITH_PROSE)

    def test_trimming_a_docstring_cannot_turn_a_failure_into_a_pass(
        self, empty_config: Config, tmp_path: Path
    ) -> None:
        """End-to-end via measure(): an over-cap file stays over cap after
        the docstring trim that used to be the standard fix."""
        body = "x = 1\n" * 1200
        over = tmp_path / "over.py"
        over.write_text('"""Doc.\n' + "prose\n" * 400 + '"""\n' + body)
        errors, _ = evaluate(measure(over), "over.py", empty_config)
        assert len(errors) == 1

        over.write_text(body)  # the 68b185ca move: delete the docstring
        errors_after, _ = evaluate(measure(over), "over.py", empty_config)
        assert len(errors_after) == 1
        assert errors_after == errors  # identical report, identical number


class TestEvaluate:
    def test_under_all_caps_passes(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(100), "x.py", empty_config)
        assert errors == []
        assert warnings == []

    def test_over_hard_cap_fails(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(1001), "x.py", empty_config)
        assert len(errors) == 1
        assert "exceeds hard cap" in errors[0]
        assert warnings == []

    def test_message_names_the_metric(self, empty_config: Config) -> None:
        """The report has to say which count it is reporting, and say that
        deleting prose will not move it."""
        errors, _ = evaluate(_stats(1001), "x.py", empty_config)
        assert "1001 code lines" in errors[0]
        assert "1000 code lines" in errors[0]
        assert "docstrings, comments and blank lines" in errors[0]
        assert "deleting documentation will not lower this number" in errors[0]

        _, warnings = evaluate(_stats(501), "x.py", empty_config)
        assert "501 code lines" in warnings[0]
        assert "500 code lines" in warnings[0]

    def test_over_soft_cap_warns(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(501), "x.py", empty_config)
        assert errors == []
        assert len(warnings) == 1
        assert "soft cap" in warnings[0]

    def test_at_caps_passes(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(1000), "x.py", empty_config)
        # Exactly at the hard cap is allowed; soft cap is exceeded -> warns.
        assert errors == []
        assert len(warnings) == 1

    def test_allowlisted_over_hard_cap_passes(self, caps: Caps) -> None:
        config = Config(caps=caps, allowlist={"big.py": "2248"})
        errors, warnings = evaluate(_stats(2000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_growth_is_allowed(self, caps: Caps) -> None:
        """Allowlisted files may grow freely -- removing the baseline check
        is the whole point of dropping per-file lines/bytes from the YAML."""
        config = Config(caps=caps, allowlist={"big.py": "2248"})
        errors, warnings = evaluate(_stats(50_000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_with_no_issue_link_still_allowed(self, caps: Caps) -> None:
        """Membership alone gates the lint; the issue field is documentation."""
        config = Config(caps=caps, allowlist={"big.py": None})
        errors, warnings = evaluate(_stats(2000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_under_caps_passes_silently(self, caps: Caps) -> None:
        """A file in the allowlist that's already shrunk under caps is silent.

        It should not produce a soft-cap warning for an allowlisted file.
        """
        config = Config(caps=caps, allowlist={"big.py": "2248"})
        errors, warnings = evaluate(_stats(900), "big.py", config)
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
    def test_reports_code_lines_and_raw_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text('"""Doc."""\n\n# note\na = 1\nb = 2\n')
        s = measure(f)
        assert s.code_lines == 2
        assert s.raw_lines == 5

    def test_counts_no_trailing_newline(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("a = 1\nb = 2\nc = 3")
        s = measure(f)
        assert s.code_lines == 3
        assert s.raw_lines == 3

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_text("")
        s = measure(f)
        assert s.code_lines == 0
        assert s.raw_lines == 0


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

    CAPS_YAML = "caps:\n  hard_code_lines: 1000\n  soft_code_lines: 500\n"

    def _yaml(self, tmp_path: Path, files_body: str) -> Path:
        """Write an allowlist with the standard caps and the given files block."""
        p = tmp_path / "allowlist.yaml"
        p.write_text(self.CAPS_YAML + textwrap.dedent(files_body))
        return p

    def test_load_preserves_issue_field(self, tmp_path: Path) -> None:
        path = self._yaml(
            tmp_path,
            """
            files:
              foo/bar.py:
                issue: "2248"
            """,
        )
        config = load_config(path)
        assert config.caps == Caps(hard_code_lines=1000, soft_code_lines=500)
        assert config.allowlist["foo/bar.py"] == "2248"

    def test_load_handles_missing_issue_field(self, tmp_path: Path) -> None:
        path = self._yaml(
            tmp_path,
            """
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
            files:
              foo/bar.py:
            """,
        )
        config = load_config(path)
        assert "foo/bar.py" in config.allowlist
        assert config.allowlist["foo/bar.py"] is None

    def test_load_ignores_legacy_lines_bytes(self, tmp_path: Path) -> None:
        """Legacy YAMLs with per-file lines/bytes keys still load -- the
        lint just ignores the now-defunct fields. This protects branches
        that haven't rebased onto the simplified schema yet."""
        path = self._yaml(
            tmp_path,
            """
            files:
              foo/bar.py:
                lines: 2000
                bytes: 80000
                issue: "2248"
            """,
        )
        config = load_config(path)
        assert config.allowlist["foo/bar.py"] == "2248"

    def test_legacy_caps_schema_fails_loudly(self, tmp_path: Path) -> None:
        """A pre-#3671 allowlist must not be silently measured against a
        cap that was calibrated for raw lines."""
        path = self._yaml(
            tmp_path,
            """
            caps:
              hard_lines: 1500
              hard_bytes: 100000
              soft_lines: 800
              soft_bytes: 60000
            files: {}
            """,
        )
        with pytest.raises(SystemExit) as excinfo:
            load_config(path)
        assert "hard_code_lines" in str(excinfo.value)
        assert "3671" in str(excinfo.value)

    def test_write_emits_issue_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        config = Config(caps=caps, allowlist={})
        write_allowlist(config, {"foo/bar.py": "2248"})
        loaded = yaml.safe_load(out.read_text())
        assert loaded["files"]["foo/bar.py"] == {"issue": "2248"}
        assert loaded["caps"] == {"hard_code_lines": 1000, "soft_code_lines": 500}

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
                  hard_code_lines: 10
                  soft_code_lines: 5
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
                  hard_code_lines: 10
                  soft_code_lines: 5
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

    def test_update_allowlist_ignores_prose_only_files(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A file that is mostly docstring is under cap and never listed."""
        repo_root = tmp_path / "repo"
        (repo_root / "orchestrator").mkdir(parents=True)
        prose = repo_root / "orchestrator" / "documented.py"
        prose.write_text('"""Doc.\n' + "prose\n" * 200 + '"""\nx = 1\n')

        allowlist = tmp_path / "allowlist.yaml"
        allowlist.write_text(
            textwrap.dedent(
                """
                caps:
                  hard_code_lines: 10
                  soft_code_lines: 5
                files: {}
                """
            )
        )
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", allowlist)

        assert update_allowlist(repo_root) == 0
        capsys.readouterr()
        assert load_config(allowlist).allowlist == {}

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
                  hard_code_lines: 10
                  soft_code_lines: 5
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


class TestLiveAllowlist:
    """The checked-in allowlist must match the checked-in caps."""

    def test_every_entry_is_still_over_the_cap(self) -> None:
        """No stale entries: the #3671 re-baseline dropped the files that
        fell under the cap once prose stopped counting, and nothing should
        creep back in without being re-verified."""
        repo_root = _SCRIPT_PATH.parents[1]
        config = load_config()
        over = {
            str(p.relative_to(repo_root))
            for p in iter_source_files(repo_root)
            if measure(p).code_lines > config.caps.hard_code_lines
        }
        assert set(config.allowlist) == over

    def test_repo_passes_the_check(self) -> None:
        errors, _warnings, stale = _mod.check_all(_SCRIPT_PATH.parents[1])
        assert errors == []
        assert stale == []

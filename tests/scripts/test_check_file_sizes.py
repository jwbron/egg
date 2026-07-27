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

AllowlistEntry = _mod.AllowlistEntry
Caps = _mod.Caps
Config = _mod.Config
FileStats = _mod.FileStats
count_code_lines = _mod.count_code_lines
evaluate = _mod.evaluate
find_stale_entries = _mod.find_stale_entries
is_test_file = _mod.is_test_file
iter_source_files = _mod.iter_source_files
load_config = _mod.load_config
measure = _mod.measure
update_allowlist = _mod.update_allowlist
write_allowlist = _mod.write_allowlist


@pytest.fixture
def caps() -> Caps:
    return Caps(hard_code_lines=1000, soft_code_lines=500, hard_bytes=150_000)


@pytest.fixture
def empty_config(caps: Caps) -> Config:
    return Config(caps=caps, allowlist={})


def _stats(code_lines: int, name: str = "x.py", size_bytes: int | None = None) -> FileStats:
    return FileStats(
        path=Path(name),
        code_lines=code_lines,
        raw_lines=code_lines * 2,
        # Default well inside the byte backstop so line-cap tests only
        # ever exercise the line cap.
        size_bytes=code_lines * 60 if size_bytes is None else size_bytes,
    )


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


class TestPhysicalLineMapping:
    """The 1-based numbers from tokenize/ast must index the same list of
    physical lines the count is taken over."""

    @pytest.mark.parametrize(
        "sep", ["\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85"]
    )
    def test_line_separators_in_a_literal_do_not_shift_the_count(self, sep: str) -> None:
        """``str.splitlines()`` breaks on characters the tokenizer treats as
        ordinary, which would shift every index after the literal and land
        the comment exclusion on the wrong line."""
        src = f'X = "a{sep}b"\n# a comment\nY = 2\n'
        # Guard the guard: the two line models really do disagree here.
        assert len(src.splitlines()) == 4
        assert count_code_lines(src) == 2

    def test_docstring_sharing_a_line_with_code_keeps_the_code(self) -> None:
        """``def f(): \"\"\"doc\"\"\"`` is one code line, not zero -- excluding
        the docstring's line range wholesale would delete the ``def``."""
        assert count_code_lines('def f(): """doc"""\n') == 1

    def test_code_after_a_docstring_on_the_same_line_still_counts(self) -> None:
        src = 'def f():\n    """doc"""; x = 1; y = 2\n    return x\n'
        assert count_code_lines(src) == 3

    def test_trailing_comment_after_a_docstring_is_not_code(self) -> None:
        """Only real code on the docstring's line disqualifies it; a
        trailing comment is not code either."""
        src = 'def f():\n    """doc"""  # note\n    return 1\n'
        assert count_code_lines(src) == 2


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
        config = Config(caps=caps, allowlist={"big.py": AllowlistEntry("2248")})
        errors, warnings = evaluate(_stats(2000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_growth_is_allowed(self, caps: Caps) -> None:
        """Allowlisted files may grow freely -- removing the baseline check
        is the whole point of dropping per-file lines/bytes from the YAML."""
        config = Config(caps=caps, allowlist={"big.py": AllowlistEntry("2248")})
        errors, warnings = evaluate(_stats(50_000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_with_no_issue_link_still_allowed(self, caps: Caps) -> None:
        """Membership alone gates the lint; the issue field is documentation."""
        config = Config(caps=caps, allowlist={"big.py": AllowlistEntry()})
        errors, warnings = evaluate(_stats(2000), "big.py", config)
        assert errors == []
        assert warnings == []

    def test_allowlisted_under_caps_passes_silently(self, caps: Caps) -> None:
        """A file in the allowlist that's already shrunk under caps is silent.

        It should not produce a soft-cap warning for an allowlisted file.
        """
        config = Config(caps=caps, allowlist={"big.py": AllowlistEntry("2248")})
        errors, warnings = evaluate(_stats(900), "big.py", config)
        assert errors == []
        assert warnings == []


class TestByteBackstop:
    """Raw bytes are the only metric that still sees unbounded prose.

    Code lines deliberately ignore docstrings, so on their own they place
    no bound at all on what reading a file costs -- a module with a padded
    docstring can reach hundreds of KB at a constant code-line count. The
    byte cap exists solely to catch that pathology, sits far above where
    real files live, and never creates pressure to trim documentation.
    """

    def test_padded_docstring_is_caught_by_bytes_alone(self, empty_config: Config) -> None:
        """The reviewer's reproduction: health_monitor.py at its real code
        size, with 20k lines of prose bolted onto the module docstring."""
        stats = _stats(749, size_bytes=544_331)
        errors, _ = evaluate(stats, "health_monitor.py", empty_config)
        assert len(errors) == 1
        assert "544331 bytes" in errors[0]
        assert "150000 bytes" in errors[0]

    def test_message_does_not_point_at_prose(self, empty_config: Config) -> None:
        """A byte failure must not read as an invitation to delete
        documentation -- that is the incentive #3671 removed."""
        errors, _ = evaluate(_stats(10, size_bytes=200_000), "x.py", empty_config)
        assert "pathology check" in errors[0]
        assert "not a prose budget" in errors[0]
        assert "Decompose the file" in errors[0]

    def test_a_file_over_both_caps_reports_both(self, empty_config: Config) -> None:
        errors, _ = evaluate(_stats(1500, size_bytes=200_000), "x.py", empty_config)
        assert len(errors) == 2
        assert "code lines exceeds hard cap" in errors[0]
        assert "bytes exceeds the byte backstop" in errors[1]

    def test_at_the_byte_cap_passes(self, empty_config: Config) -> None:
        errors, warnings = evaluate(_stats(10, size_bytes=150_000), "x.py", empty_config)
        assert errors == []
        assert warnings == []

    def test_there_is_no_soft_byte_warning(self, empty_config: Config) -> None:
        """A soft byte cap would sit near real files and re-create exactly
        the prose-trimming pressure this check was rewritten to remove."""
        _, warnings = evaluate(_stats(10, size_bytes=149_999), "x.py", empty_config)
        assert warnings == []

    def test_allowlist_waives_the_byte_cap_too(self, caps: Caps) -> None:
        """The allowlist is a single, uniform exemption from the hard caps;
        adding a file this large is a deliberate, reviewed act."""
        config = Config(caps=caps, allowlist={"big.py": AllowlistEntry("2248")})
        errors, warnings = evaluate(_stats(10, size_bytes=999_999), "big.py", config)
        assert errors == []
        assert warnings == []


class TestFindStaleEntries:
    """The ratchet only turns one way.

    Checking existence alone let an exemption outlive its cause: the #3671
    re-baseline left five entries sitting under the cap and ``make lint``
    said nothing about any of them.
    """

    def _config(self, caps: Caps, *paths: str) -> Config:
        return Config(caps=caps, allowlist={p: AllowlistEntry("2248") for p in paths})

    def test_entry_for_a_missing_file_is_stale(self, caps: Caps) -> None:
        stale = find_stale_entries({}, self._config(caps, "gone.py"))
        assert len(stale) == 1
        assert "no longer exists" in stale[0]

    def test_entry_now_under_the_caps_is_stale(self, caps: Caps) -> None:
        measured = {"shrunk.py": _stats(900)}
        stale = find_stale_entries(measured, self._config(caps, "shrunk.py"))
        assert len(stale) == 1
        assert "now under the caps" in stale[0]
        assert "900 code lines" in stale[0]

    def test_entry_still_over_the_line_cap_is_not_stale(self, caps: Caps) -> None:
        measured = {"big.py": _stats(1001)}
        assert find_stale_entries(measured, self._config(caps, "big.py")) == []

    def test_entry_still_over_the_byte_cap_is_not_stale(self, caps: Caps) -> None:
        """Under the line cap but over the byte backstop still needs the
        exemption -- removing it would fail the lint."""
        measured = {"big.py": _stats(10, size_bytes=200_000)}
        assert find_stale_entries(measured, self._config(caps, "big.py")) == []

    def test_stale_entries_fail_the_lint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A warning is what let five stale entries through unnoticed, so
        this is surfaced at the lint layer as a failure."""
        repo_root = tmp_path / "repo"
        (repo_root / "orchestrator").mkdir(parents=True)
        (repo_root / "orchestrator" / "small.py").write_text("x = 1\n")

        allowlist = tmp_path / "allowlist.yaml"
        allowlist.write_text(
            textwrap.dedent(
                """
                caps:
                  hard_code_lines: 10
                  soft_code_lines: 5
                  hard_bytes: 150000
                  hard_bytes: 150000
                files:
                  orchestrator/small.py:
                    issue: "2248"
                """
            )
        )
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", allowlist)
        monkeypatch.setattr(_mod, "REPO_ROOT", repo_root)

        assert _mod.main([]) == 1
        out = capsys.readouterr().out
        assert "stale allowlist entry" in out
        assert "orchestrator/small.py" in out


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

    CAPS_YAML = "caps:\n  hard_code_lines: 1000\n  soft_code_lines: 500\n  hard_bytes: 150000\n"

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
        assert config.caps == Caps(hard_code_lines=1000, soft_code_lines=500, hard_bytes=150_000)
        assert config.allowlist["foo/bar.py"].issue == "2248"

    def test_load_preserves_note_field(self, tmp_path: Path) -> None:
        """Per-entry rationale lives in the structured entry precisely so
        --update-allowlist's rewrite cannot destroy it."""
        path = self._yaml(
            tmp_path,
            """
            files:
              foo/bar.py:
                issue: "2248"
                note: The request-handling barrel.
            """,
        )
        config = load_config(path)
        assert config.allowlist["foo/bar.py"] == AllowlistEntry(
            issue="2248", note="The request-handling barrel."
        )

    def test_load_handles_missing_issue_field(self, tmp_path: Path) -> None:
        path = self._yaml(
            tmp_path,
            """
            files:
              foo/bar.py: {}
            """,
        )
        config = load_config(path)
        assert config.allowlist["foo/bar.py"] == AllowlistEntry()

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
        assert config.allowlist["foo/bar.py"] == AllowlistEntry()

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
        assert config.allowlist["foo/bar.py"].issue == "2248"

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

    def test_missing_byte_cap_fails_loudly(self, tmp_path: Path) -> None:
        """A branch predating the byte backstop must not run without it --
        silently skipping the cap is how a 544KB file passes clean."""
        path = tmp_path / "allowlist.yaml"
        path.write_text("caps:\n  hard_code_lines: 1000\n  soft_code_lines: 500\nfiles: {}\n")
        with pytest.raises(SystemExit) as excinfo:
            load_config(path)
        assert "hard_bytes" in str(excinfo.value)

    @pytest.mark.parametrize("bad", ["abc", "", "[]"])
    def test_non_integer_cap_fails_with_a_message(self, tmp_path: Path, bad: str) -> None:
        """A typo in a file that gates CI gets the same friendly exit as a
        missing key, not a raw ValueError from int()."""
        path = tmp_path / "allowlist.yaml"
        path.write_text(
            f"caps:\n  hard_code_lines: {bad}\n  soft_code_lines: 500\n"
            "  hard_bytes: 150000\nfiles: {}\n"
        )
        with pytest.raises(SystemExit) as excinfo:
            load_config(path)
        assert "must be an integer" in str(excinfo.value)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_cap_fails_with_a_message(self, tmp_path: Path, bad: int) -> None:
        """A zero or negative cap would reject every file or gate nothing;
        either way it is a typo, not a configuration."""
        path = tmp_path / "allowlist.yaml"
        path.write_text(
            f"caps:\n  hard_code_lines: {bad}\n  soft_code_lines: 500\n"
            "  hard_bytes: 150000\nfiles: {}\n"
        )
        with pytest.raises(SystemExit) as excinfo:
            load_config(path)
        assert "must be positive" in str(excinfo.value)

    def test_write_emits_issue_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        config = Config(caps=caps, allowlist={})
        write_allowlist(config, {"foo/bar.py": AllowlistEntry("2248")})
        loaded = yaml.safe_load(out.read_text())
        assert loaded["files"]["foo/bar.py"] == {"issue": "2248"}
        assert loaded["caps"] == {
            "hard_code_lines": 1000,
            "soft_code_lines": 500,
            "hard_bytes": 150_000,
        }

    def test_write_omits_issue_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        config = Config(caps=caps, allowlist={})
        write_allowlist(config, {"foo/bar.py": AllowlistEntry()})
        loaded = yaml.safe_load(out.read_text())
        # Bare key (null value) when there's no issue link -- avoids
        # littering the YAML with `issue: null` placeholders.
        assert loaded["files"]["foo/bar.py"] is None

    def test_round_trip_preserves_issue_and_note(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        """Regression: --update-allowlist must not silently drop the
        documentation attached to an entry."""
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        config = Config(caps=caps, allowlist={})
        original = {
            "a.py": AllowlistEntry("2248", "The barrel: awaiting decomposition."),
            "b.py": AllowlistEntry("9999"),
            "c.py": AllowlistEntry(note="No issue yet -- just context."),
            "d.py": AllowlistEntry(),
        }
        write_allowlist(config, original)
        reloaded = load_config(out)
        assert reloaded.allowlist == original

    def test_write_emits_the_header_prose(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caps: Caps
    ) -> None:
        """The header is regenerated from the script rather than preserved
        from the parsed document -- a plain ``yaml.safe_dump`` deleted every
        comment in the file, which for this file is most of its substance."""
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)
        write_allowlist(Config(caps=caps, allowlist={}), {})
        text = out.read_text()
        assert "# Allowlist for scripts/check-file-sizes.py." in text
        assert "# Schema:" in text
        assert "hard_bytes is a pathology backstop" in text
        # The decomposition-program note survives too.
        assert "#3312" in text
        # An empty allowlist must still round-trip as valid YAML.
        assert load_config(out).allowlist == {}

    def test_rewriting_the_live_allowlist_is_a_no_op(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The checked-in file must be exactly what write_allowlist emits.

        This is the strong form of the guarantee: running
        ``--update-allowlist`` on an unchanged tree produces a byte-identical
        file, so the flag can never silently eat the file's documentation.
        """
        live = _SCRIPT_PATH.parent / "file-size-allowlist.yaml"
        original = live.read_text()
        out = tmp_path / "out.yaml"
        monkeypatch.setattr(_mod, "ALLOWLIST_PATH", out)

        repo_root = _SCRIPT_PATH.parents[1]
        config = load_config(live)
        measured = {rel: measure(repo_root / rel) for rel in config.allowlist}
        write_allowlist(config, config.allowlist, measured)
        assert out.read_text() == original

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
                  hard_bytes: 150000
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
        assert reloaded.allowlist["orchestrator/big.py"].issue == "2248"
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
                  hard_bytes: 150000
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
        assert reloaded.allowlist["orchestrator/new_big.py"] == AllowlistEntry()

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
                  hard_bytes: 150000
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
                  hard_bytes: 150000
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

    def test_every_entry_is_still_over_a_cap(self) -> None:
        """No stale entries: the #3671 re-baseline dropped the files that
        fell under the cap once prose stopped counting, and nothing should
        creep back in without being re-verified."""
        repo_root = _SCRIPT_PATH.parents[1]
        config = load_config()
        over = {
            str(p.relative_to(repo_root))
            for p in iter_source_files(repo_root)
            if _mod._is_over_a_hard_cap(measure(p), config.caps)
        }
        assert set(config.allowlist) == over

    def test_every_entry_has_a_tracking_issue(self) -> None:
        """The checker's own failure message tells the user to add an
        entry "with a tracking issue", so the reference file cannot be the
        first thing to break that rule -- an entry without one has no
        decomposition owner."""
        missing = sorted(rel for rel, entry in load_config().allowlist.items() if not entry.issue)
        assert missing == []

    def test_repo_passes_the_check(self) -> None:
        errors, _warnings, stale = _mod.check_all(_SCRIPT_PATH.parents[1])
        assert errors == []
        assert stale == []

    def test_no_source_file_blows_the_read_budget(self) -> None:
        """The cap the check exists for is the agent's Read cost, and code
        lines cannot see it. Nothing outside the allowlist may exceed the
        byte backstop."""
        repo_root = _SCRIPT_PATH.parents[1]
        config = load_config()
        over = sorted(
            rel
            for p in iter_source_files(repo_root)
            if (rel := str(p.relative_to(repo_root))) not in config.allowlist
            and measure(p).size_bytes > config.caps.hard_bytes
        )
        assert over == []

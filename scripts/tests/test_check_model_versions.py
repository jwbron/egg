"""Tests for the model alias linter (scripts/check-model-versions.py).

Verifies that the linter correctly detects:
- Full Claude model identifiers (both pinned and unpinned)
- Proper suppression via noqa: EGG201
- Clean code passes without violations
"""

import sys
import textwrap
from pathlib import Path

# Add scripts directory to path so we can import the linter
_scripts_path = Path(__file__).parent.parent
if str(_scripts_path) not in sys.path:
    sys.path.insert(0, str(_scripts_path))

# Import after path manipulation (module has hyphens, use importlib)
import importlib.util

spec = importlib.util.spec_from_file_location(
    "check_model_versions",
    _scripts_path / "check-model-versions.py",
)
check_model_versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_model_versions)


def _write_and_check(tmp_path: Path, code: str) -> list[tuple[int, str]]:
    """Write code to a temp Python file and check it."""
    py_file = tmp_path / "test_file.py"
    py_file.write_text(textwrap.dedent(code))
    return check_model_versions.check_python_file(py_file)


class TestDetectsPinnedVersions:
    """Tests that date-pinned model versions are detected."""

    def test_pinned_sonnet(self, tmp_path):
        code = 'model = "claude-sonnet-4-20250514"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "sonnet" in violations[0][1]

    def test_pinned_opus(self, tmp_path):
        code = 'model = "claude-opus-4-5-20251101"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "opus" in violations[0][1]

    def test_pinned_haiku(self, tmp_path):
        code = 'model = "claude-haiku-4-5-20251001"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "haiku" in violations[0][1]

    def test_pinned_claude3(self, tmp_path):
        code = 'model = "claude-3-haiku-20240307"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "haiku" in violations[0][1]

    def test_pinned_claude35(self, tmp_path):
        code = 'model = "claude-3-5-sonnet-20241022"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "sonnet" in violations[0][1]


class TestDetectsUnpinnedFullIds:
    """Tests that non-alias full model identifiers are detected."""

    def test_unpinned_sonnet(self, tmp_path):
        code = 'model = "claude-sonnet-4"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "sonnet" in violations[0][1]

    def test_unpinned_opus(self, tmp_path):
        code = 'model = "claude-opus-4"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "opus" in violations[0][1]

    def test_unpinned_haiku(self, tmp_path):
        code = 'model = "claude-haiku-4-5"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "haiku" in violations[0][1]

    def test_unpinned_opus_45(self, tmp_path):
        code = 'model = "claude-opus-4-5"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "opus" in violations[0][1]


class TestAliasesPass:
    """Tests that short aliases are NOT flagged."""

    def test_alias_sonnet(self, tmp_path):
        code = 'model = "sonnet"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_alias_opus(self, tmp_path):
        code = 'model = "opus"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_alias_haiku(self, tmp_path):
        code = 'model = "haiku"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0


class TestNoqaSuppression:
    """Tests that noqa: EGG201 suppresses violations."""

    def test_noqa_suppresses_pinned(self, tmp_path):
        code = 'model = "claude-sonnet-4-20250514"  # noqa: EGG201 - test\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_noqa_suppresses_unpinned(self, tmp_path):
        code = 'model = "claude-opus-4"  # noqa: EGG201 - required\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_noqa_on_preceding_line_suppresses(self, tmp_path):
        code = '# noqa: EGG201 - docstring example\nmodel = "claude-sonnet-4-20250514"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_wrong_noqa_code_does_not_suppress(self, tmp_path):
        code = 'model = "claude-sonnet-4"  # noqa: EGG200 - wrong code\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1


class TestCleanCode:
    """Tests that clean code passes without violations."""

    def test_normal_python_code(self, tmp_path):
        code = """\
        import json
        import os
        model = "opus"
        """
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_unrelated_strings(self, tmp_path):
        code = """\
        url = "https://api.example.com"
        name = "claude code"
        cmd = "claude --print"
        """
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_bare_family_names_not_flagged(self, tmp_path):
        """Bare claude-sonnet/opus/haiku (no version) are not model IDs."""
        code = """\
        text = "claude-sonnet"
        text2 = "claude-opus"
        text3 = "claude-haiku"
        """
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_syntax_error_returns_empty(self, tmp_path):
        """Files with syntax errors return empty violations (not crash)."""
        py_file = tmp_path / "broken.py"
        py_file.write_text("def broken(\n")
        violations = check_model_versions.check_python_file(py_file)
        assert len(violations) == 0


class TestPathFiltering:
    """Tests for _should_skip_path."""

    def test_skip_venv(self):
        p = Path(".venv/lib/python3.11/model.py")
        assert check_model_versions._should_skip_path(p) is True

    def test_skip_test_files(self):
        p = Path("orchestrator/tests/test_model.py")
        assert check_model_versions._should_skip_path(p) is True

    def test_skip_test_prefix(self):
        p = Path("orchestrator/test_something.py")
        assert check_model_versions._should_skip_path(p) is True

    def test_allow_source_files(self):
        p = Path("sandbox/bin/egg")
        assert check_model_versions._should_skip_path(p) is False

    def test_skip_pycache(self):
        p = Path("orchestrator/__pycache__/module.py")
        assert check_model_versions._should_skip_path(p) is True


class TestSuggestAlias:
    """Tests for _suggest_alias helper — takes family name, not full model ID."""

    def test_suggest_sonnet(self):
        assert check_model_versions._suggest_alias("sonnet") == "sonnet"

    def test_suggest_opus(self):
        assert check_model_versions._suggest_alias("opus") == "opus"

    def test_suggest_haiku(self):
        assert check_model_versions._suggest_alias("haiku") == "haiku"


class TestHasPythonShebang:
    """Tests for _has_python_shebang."""

    def test_python3_shebang(self, tmp_path):
        f = tmp_path / "script"
        f.write_text("#!/usr/bin/env python3\nprint('hello')\n")
        assert check_model_versions._has_python_shebang(f) is True

    def test_python_shebang(self, tmp_path):
        f = tmp_path / "script"
        f.write_text("#!/usr/bin/python\nprint('hello')\n")
        assert check_model_versions._has_python_shebang(f) is True

    def test_bash_shebang(self, tmp_path):
        f = tmp_path / "script"
        f.write_text("#!/bin/bash\necho hello\n")
        assert check_model_versions._has_python_shebang(f) is False

    def test_no_shebang(self, tmp_path):
        f = tmp_path / "script"
        f.write_text("print('hello')\n")
        assert check_model_versions._has_python_shebang(f) is False

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "missing"
        assert check_model_versions._has_python_shebang(f) is False


class TestMultipleViolations:
    """Tests that multiple violations in one file are all detected."""

    def test_multiple_models(self, tmp_path):
        code = """\
        model_a = "claude-sonnet-4-20250514"
        model_b = "claude-opus-4-5-20251101"
        model_c = "claude-haiku-4-5"
        """
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 3

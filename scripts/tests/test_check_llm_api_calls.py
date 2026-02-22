"""Tests for the LLM API boundary linter (scripts/check-llm-api-calls.py).

Verifies that the linter correctly detects:
- Direct Anthropic SDK imports
- Anthropic API URL string literals
- ANTHROPIC_API_KEY environment access
- Proper suppression via noqa: EGG200
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
    "check_llm_api_calls",
    _scripts_path / "check-llm-api-calls.py",
)
check_llm_api_calls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_llm_api_calls)


def _write_and_check(tmp_path: Path, code: str) -> list[tuple[int, str]]:
    """Write code to a temp Python file and check it."""
    py_file = tmp_path / "test_file.py"
    py_file.write_text(textwrap.dedent(code))
    return check_llm_api_calls.check_python_file(py_file)


class TestDetectsAnthropicImports:
    """Tests that direct Anthropic SDK imports are detected."""

    def test_import_anthropic(self, tmp_path):
        violations = _write_and_check(tmp_path, "import anthropic\n")
        assert len(violations) == 1
        assert "import" in violations[0][1].lower()

    def test_from_anthropic_import(self, tmp_path):
        violations = _write_and_check(tmp_path, "from anthropic import Client\n")
        assert len(violations) == 1
        assert "import" in violations[0][1].lower()

    def test_import_anthropic_submodule(self, tmp_path):
        violations = _write_and_check(tmp_path, "import anthropic.resources\n")
        assert len(violations) == 1

    def test_from_anthropic_submodule(self, tmp_path):
        violations = _write_and_check(tmp_path, "from anthropic.types import Message\n")
        assert len(violations) == 1

    def test_unrelated_import_clean(self, tmp_path):
        violations = _write_and_check(tmp_path, "import json\nimport os\n")
        assert len(violations) == 0


class TestDetectsApiUrls:
    """Tests that Anthropic API URL string literals are detected."""

    def test_api_url_in_string(self, tmp_path):
        code = 'url = "https://api.anthropic.com/v1/messages"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "api.anthropic.com" in violations[0][1]

    def test_api_url_in_httpx_call(self, tmp_path):
        code = 'resp = httpx.post("https://api.anthropic.com/v1/messages", json={})\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1

    def test_no_false_positive_on_unrelated_urls(self, tmp_path):
        code = 'url = "https://api.github.com/repos"\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0


class TestDetectsApiKeyAccess:
    """Tests that ANTHROPIC_API_KEY environment access is detected."""

    def test_os_environ_get(self, tmp_path):
        code = 'key = os.environ.get("ANTHROPIC_API_KEY")\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "ANTHROPIC_API_KEY" in violations[0][1]

    def test_os_environ_subscript(self, tmp_path):
        code = 'key = os.environ["ANTHROPIC_API_KEY"]\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1
        assert "ANTHROPIC_API_KEY" in violations[0][1]

    def test_os_environ_get_other_key_clean(self, tmp_path):
        code = 'val = os.environ.get("HOME")\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0


class TestNoqaSuppression:
    """Tests that noqa: EGG200 suppresses violations."""

    def test_noqa_suppresses_import(self, tmp_path):
        code = "import anthropic  # noqa: EGG200 - test helper\n"
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_noqa_suppresses_url(self, tmp_path):
        code = 'url = "https://api.anthropic.com"  # noqa: EGG200 - constant for docs\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_noqa_suppresses_env_get(self, tmp_path):
        code = 'key = os.environ.get("ANTHROPIC_API_KEY")  # noqa: EGG200 - forwarding\n'
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_wrong_noqa_code_does_not_suppress(self, tmp_path):
        code = "import anthropic  # noqa: EGG100 - wrong code\n"
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 1


class TestCleanCode:
    """Tests that clean code passes without violations."""

    def test_normal_python_code(self, tmp_path):
        code = """\
        import json
        import os
        from pathlib import Path

        def hello():
            return "world"
        """
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_httpx_to_other_apis(self, tmp_path):
        code = """\
        import httpx
        resp = httpx.post("https://api.openai.com/v1/chat", json={})
        """
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 0

    def test_syntax_error_returns_empty(self, tmp_path):
        """Files with syntax errors return empty violations (not crash)."""
        py_file = tmp_path / "broken.py"
        py_file.write_text("def broken(\n")
        violations = check_llm_api_calls.check_python_file(py_file)
        assert len(violations) == 0


class TestPathFiltering:
    """Tests for _should_skip_path."""

    def test_skip_venv(self):
        p = Path(".venv/lib/python3.11/anthropic.py")
        assert check_llm_api_calls._should_skip_path(p) is True

    def test_skip_test_files(self):
        p = Path("orchestrator/tests/test_inspector.py")
        assert check_llm_api_calls._should_skip_path(p) is True

    def test_skip_test_prefix(self):
        p = Path("orchestrator/test_something.py")
        assert check_llm_api_calls._should_skip_path(p) is True

    def test_allow_source_files(self):
        p = Path("orchestrator/health_checks/tier2/agent_inspector.py")
        assert check_llm_api_calls._should_skip_path(p) is False

    def test_skip_pycache(self):
        p = Path("orchestrator/__pycache__/module.py")
        assert check_llm_api_calls._should_skip_path(p) is True


class TestMultipleViolations:
    """Tests that multiple violations in one file are all detected."""

    def test_multiple_patterns(self, tmp_path):
        code = """\
        import anthropic
        url = "https://api.anthropic.com/v1/messages"
        key = os.environ.get("ANTHROPIC_API_KEY")
        """
        violations = _write_and_check(tmp_path, code)
        assert len(violations) == 3

"""Tests for scripts/hello_world.py.

Validates:
- Correct output ("hello world")
- Script has valid Python syntax
- Script has proper shebang line
- Script is executable as a subprocess
- No extraneous output (stderr is clean)
"""

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "hello_world.py"


class TestHelloWorldScript:
    """Tests for the hello world script."""

    def test_script_exists(self):
        """The script file must exist at the expected path."""
        assert SCRIPT_PATH.exists(), f"Script not found at {SCRIPT_PATH}"

    def test_script_is_file(self):
        """The script must be a regular file, not a directory or symlink."""
        assert SCRIPT_PATH.is_file(), f"{SCRIPT_PATH} is not a regular file"

    def test_script_has_shebang(self):
        """The script must start with a proper Python 3 shebang line."""
        content = SCRIPT_PATH.read_text()
        first_line = content.splitlines()[0]
        assert first_line == "#!/usr/bin/env python3", (
            f"Expected shebang '#!/usr/bin/env python3', got '{first_line}'"
        )

    def test_script_output_exact(self):
        """Running the script must print exactly 'hello world' to stdout."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Script exited with code {result.returncode}"
        assert result.stdout.strip() == "hello world", (
            f"Expected 'hello world', got '{result.stdout.strip()}'"
        )

    def test_script_no_stderr(self):
        """The script must not produce any output on stderr."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.stderr == "", f"Unexpected stderr output: {result.stderr}"

    def test_script_exit_code_zero(self):
        """The script must exit with code 0 (success)."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Expected exit code 0, got {result.returncode}. stderr: {result.stderr}"
        )

    def test_script_output_single_line(self):
        """The script should produce exactly one line of output."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.splitlines()
        assert len(lines) == 1, f"Expected 1 line of output, got {len(lines)}: {lines}"

    def test_script_output_trailing_newline(self):
        """The script output should end with exactly one newline (print default)."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # print() adds a single trailing newline
        assert result.stdout == "hello world\n", f"Expected 'hello world\\n', got {result.stdout!r}"

    def test_script_valid_python_syntax(self):
        """The script must be valid Python that compiles without errors."""
        source = SCRIPT_PATH.read_text()
        try:
            compile(source, str(SCRIPT_PATH), "exec")
        except SyntaxError as e:
            pytest.fail(f"Script has syntax error: {e}")

    def test_script_has_docstring(self):
        """The script should include a module-level docstring."""
        source = SCRIPT_PATH.read_text()
        # After shebang, the next non-empty line should be a docstring
        lines = source.splitlines()
        # Skip shebang and blank lines
        content_lines = [line for line in lines[1:] if line.strip()]
        assert len(content_lines) > 0, "Script has no content after shebang"
        first_content = content_lines[0].strip()
        assert first_content.startswith('"""') or first_content.startswith("'''"), (
            f"Expected docstring after shebang, got: {first_content}"
        )

    def test_script_not_empty(self):
        """The script must not be empty."""
        content = SCRIPT_PATH.read_text()
        assert len(content.strip()) > 0, "Script is empty"

    def test_script_encoding_utf8(self):
        """The script must be readable as UTF-8."""
        try:
            SCRIPT_PATH.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            pytest.fail(f"Script is not valid UTF-8: {e}")

"""
Test that all Bash scripts have valid syntax.

Uses `bash -n` for syntax checking, which parses scripts without
executing them. This is the Bash equivalent of Python's py_compile.
"""

import subprocess
from pathlib import Path

import pytest

# Get project root
PROJECT_ROOT = Path(__file__).parent.parent

# Directories to exclude from testing
EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
}


def get_bash_files():
    """Discover all Bash scripts in the repository."""
    bash_files = []

    # Find .sh files
    for sh_file in PROJECT_ROOT.rglob("*.sh"):
        if any(excluded in sh_file.parts for excluded in EXCLUDE_DIRS):
            continue
        bash_files.append(sh_file)

    # Also check for executable scripts without extension that have bash shebang
    for path in PROJECT_ROOT.rglob("*"):
        if path.is_file() and path.suffix == "":
            if any(excluded in path.parts for excluded in EXCLUDE_DIRS):
                continue
            # Check if it's a bash script by shebang
            try:
                with open(path, "rb") as f:
                    first_line = f.readline().decode("utf-8", errors="ignore").strip()
                    if first_line.startswith("#!") and (
                        "bash" in first_line or "/sh" in first_line
                    ):
                        bash_files.append(path)
            except OSError:
                pass

    return bash_files


def get_bash_file_ids():
    """Get test IDs (relative paths) for each Bash file."""
    return [str(f.relative_to(PROJECT_ROOT)) for f in get_bash_files()]


class TestBashSyntax:
    """Test Bash scripts for valid syntax."""

    @pytest.mark.parametrize("bash_file", get_bash_files(), ids=get_bash_file_ids())
    def test_bash_syntax_valid(self, bash_file: Path):
        """Test that Bash script has valid syntax using bash -n."""
        result = subprocess.run(
            ["bash", "-n", str(bash_file)], check=False, capture_output=True, text=True
        )
        if result.returncode != 0:
            pytest.fail(f"Bash syntax error in {bash_file}:\n{result.stderr}")

    @pytest.mark.parametrize("bash_file", get_bash_files(), ids=get_bash_file_ids())
    def test_shebang_present(self, bash_file: Path):
        """Test that Bash scripts have a proper shebang line."""
        with open(bash_file, encoding="utf-8", errors="ignore") as f:
            first_line = f.readline().strip()

        valid_shebangs = [
            "#!/bin/bash",
            "#!/usr/bin/env bash",
            "#!/bin/sh",
            "#!/usr/bin/env sh",
        ]
        assert any(
            first_line.startswith(shebang) for shebang in valid_shebangs
        ), f"{bash_file} missing valid shebang. Got: {first_line!r}"

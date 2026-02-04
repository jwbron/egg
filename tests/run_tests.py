#!/usr/bin/env python3
"""
Unified test runner for egg.

This script provides a single entry point for running all tests:
- Python syntax tests (via pytest)
- Bash syntax tests (via bash -n, wrapped in pytest)

Usage:
    ./tests/run_tests.py           # Run all tests
    ./tests/run_tests.py --python  # Run only Python tests
    ./tests/run_tests.py --bash    # Run only Bash tests
    ./tests/run_tests.py -v        # Verbose output
    ./tests/run_tests.py --quick   # Quick syntax-only check (no pytest overhead)
"""

import argparse
import ast
import py_compile
import subprocess
import sys
from pathlib import Path

# Get project root
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

# Colors for output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


def print_header(msg: str):
    """Print a header message."""
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}{msg}{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def print_success(msg: str):
    """Print a success message."""
    print(f"{GREEN}✓ {msg}{RESET}")


def print_error(msg: str):
    """Print an error message."""
    print(f"{RED}✗ {msg}{RESET}")


def print_warning(msg: str):
    """Print a warning message."""
    print(f"{YELLOW}! {msg}{RESET}")


def get_python_files() -> list[Path]:
    """Get all Python files in the project."""
    exclude_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules"}
    files = []
    for f in PROJECT_ROOT.rglob("*.py"):
        if not any(excluded in f.parts for excluded in exclude_dirs):
            files.append(f)
    return files


def get_bash_files() -> list[Path]:
    """Get all Bash files in the project."""
    exclude_dirs = {".git", "node_modules", ".venv", "venv"}
    files = []

    # .sh files
    for f in PROJECT_ROOT.rglob("*.sh"):
        if not any(excluded in f.parts for excluded in exclude_dirs):
            files.append(f)

    # Executable scripts with bash shebang
    for f in PROJECT_ROOT.rglob("*"):
        if f.is_file() and f.suffix == "":
            if any(excluded in f.parts for excluded in exclude_dirs):
                continue
            try:
                with open(f, "rb") as fp:
                    first_line = fp.readline().decode("utf-8", errors="ignore").strip()
                    if first_line.startswith("#!") and (
                        "bash" in first_line or "/sh" in first_line
                    ):
                        files.append(f)
            except OSError:
                pass

    return files


def quick_python_check(verbose: bool = False) -> tuple[int, int]:
    """Quick Python syntax check without pytest overhead."""
    files = get_python_files()
    passed = 0
    failed = 0

    for f in files:
        rel_path = f.relative_to(PROJECT_ROOT)
        try:
            py_compile.compile(str(f), doraise=True)
            source = f.read_text(encoding="utf-8")
            ast.parse(source, filename=str(f))
            passed += 1
            if verbose:
                print_success(str(rel_path))
        except (py_compile.PyCompileError, SyntaxError) as e:
            failed += 1
            print_error(f"{rel_path}: {e}")

    return passed, failed


def quick_bash_check(verbose: bool = False) -> tuple[int, int]:
    """Quick Bash syntax check without pytest overhead."""
    files = get_bash_files()
    passed = 0
    failed = 0

    for f in files:
        rel_path = f.relative_to(PROJECT_ROOT)
        result = subprocess.run(["bash", "-n", str(f)], check=False, capture_output=True, text=True)
        if result.returncode == 0:
            passed += 1
            if verbose:
                print_success(str(rel_path))
        else:
            failed += 1
            print_error(f"{rel_path}: {result.stderr.strip()}")

    return passed, failed


def run_pytest(test_file: str | None = None, verbose: bool = False) -> int:
    """Run pytest on tests directory or specific file."""
    cmd = ["python", "-m", "pytest"]
    if verbose:
        cmd.append("-v")
    if test_file:
        cmd.append(str(SCRIPT_DIR / test_file))
    else:
        cmd.append(str(SCRIPT_DIR))

    result = subprocess.run(cmd, check=False, cwd=PROJECT_ROOT)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Run egg tests")
    parser.add_argument("--python", action="store_true", help="Run only Python tests")
    parser.add_argument("--bash", action="store_true", help="Run only Bash tests")
    parser.add_argument(
        "--quick", action="store_true", help="Quick syntax check without pytest overhead"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # If no specific test type selected, run all
    run_python = args.python or (not args.python and not args.bash)
    run_bash = args.bash or (not args.python and not args.bash)

    total_passed = 0
    total_failed = 0
    exit_code = 0

    if args.quick:
        # Quick mode: direct syntax checking
        if run_python:
            print_header("Python Syntax Check")
            passed, failed = quick_python_check(args.verbose)
            total_passed += passed
            total_failed += failed
            print(f"\nPython: {passed} passed, {failed} failed")
            if failed > 0:
                exit_code = 1

        if run_bash:
            print_header("Bash Syntax Check")
            passed, failed = quick_bash_check(args.verbose)
            total_passed += passed
            total_failed += failed
            print(f"\nBash: {passed} passed, {failed} failed")
            if failed > 0:
                exit_code = 1

        print_header("Summary")
        print(f"Total: {total_passed} passed, {total_failed} failed")

    # Full mode: use pytest
    elif run_python and not run_bash:
        exit_code = run_pytest("test_python_syntax.py", args.verbose)
    elif run_bash and not run_python:
        exit_code = run_pytest("test_bash_syntax.py", args.verbose)
    else:
        exit_code = run_pytest(verbose=args.verbose)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Lint check: Ensure docker run and claude CLI invocations are explicitly justified.

PR #159 extracts a shared build_sandbox_docker_cmd() to unify container launches.
This linter prevents future divergence by requiring explicit justification for:

1. Python subprocess calls with `docker run`
2. Shell commands with `docker run`
3. Python subprocess calls invoking the `claude` CLI
4. Dangerous docker flags (--privileged, --network host, --pid host, --ipc host)

Suppression:
    Each invocation must have a noqa comment with justification:
        # noqa: EGG100 - <reason why this invocation is necessary>

    Examples:
        subprocess.run(["docker", "run", ...])  # noqa: EGG100 - test helper container
        subprocess.run(["claude", "--version"])  # noqa: EGG100 - version check

Usage:
    python3 scripts/check-docker-and-claude-invocations.py

Exit codes:
    0 - No violations found
    1 - Found violations
"""

import ast
import re
import sys
from pathlib import Path

NOQA_CODE = "EGG100"


# ── AST Visitor ─────────────────────────────────────────────────────────────


class DockerClaudeVisitor(ast.NodeVisitor):
    """AST visitor that detects docker run and claude CLI subprocess calls."""

    def __init__(self, source_lines: list[str]):
        self.source_lines = source_lines
        self.docker_run_lines: list[tuple[int, str]] = []
        self.claude_cli_lines: list[tuple[int, str]] = []
        self.dangerous_flag_lines: list[tuple[int, str]] = []
        self.shell_string_lines: list[tuple[int, str]] = []

    def _has_noqa(self, lineno: int) -> bool:
        """Check if a line has a noqa: EGG100 comment."""
        if 1 <= lineno <= len(self.source_lines):
            line = self.source_lines[lineno - 1]
            return f"noqa: {NOQA_CODE}" in line
        return False

    def _get_call_name(self, node: ast.Call) -> str:
        """Extract the full dotted name of a function call."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            parts: list[str] = []
            current: ast.expr = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))
        return ""

    def _is_subprocess_call(self, node: ast.Call) -> bool:
        """Check if node is a subprocess invocation."""
        name = self._get_call_name(node)
        return name in (
            "subprocess.run",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "subprocess.Popen",
        )

    def _get_list_string_elements(self, node: ast.List) -> list[str | None]:
        """Extract string values from a list literal. Non-string elements become None."""
        result: list[str | None] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                result.append(elt.value)
            else:
                result.append(None)
        return result

    def _check_dangerous_flags(self, elts: list[str | None], lineno: int) -> None:
        """Check for dangerous docker flags in command elements."""
        if self._has_noqa(lineno):
            return
        for i, val in enumerate(elts):
            if val == "--privileged":
                self.dangerous_flag_lines.append((lineno, "Dangerous flag: --privileged"))
            elif val == "--network" and i + 1 < len(elts) and elts[i + 1] == "host":
                self.dangerous_flag_lines.append((lineno, "Dangerous flag: --network host"))
            elif val == "--network=host":
                self.dangerous_flag_lines.append((lineno, "Dangerous flag: --network=host"))
            elif val == "--pid" and i + 1 < len(elts) and elts[i + 1] == "host":
                self.dangerous_flag_lines.append((lineno, "Dangerous flag: --pid host"))
            elif val == "--pid=host":
                self.dangerous_flag_lines.append((lineno, "Dangerous flag: --pid=host"))
            elif val == "--ipc" and i + 1 < len(elts) and elts[i + 1] == "host":
                self.dangerous_flag_lines.append((lineno, "Dangerous flag: --ipc host"))
            elif val == "--ipc=host":
                self.dangerous_flag_lines.append((lineno, "Dangerous flag: --ipc=host"))

    def _has_shell_true(self, node: ast.Call) -> bool:
        """Check if a subprocess call has shell=True."""
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant):
                return kw.value.value is True
        return False

    def _check_string_command(self, node: ast.Call) -> None:
        """Check string-based subprocess calls with shell=True for docker run/claude."""
        if not node.args:
            return
        first_arg = node.args[0]
        if not isinstance(first_arg, ast.Constant) or not isinstance(first_arg.value, str):
            return
        if not self._has_shell_true(node):
            return
        if self._has_noqa(node.lineno):
            return

        cmd = first_arg.value
        if re.search(r"\bdocker\s+run\b", cmd):
            self.shell_string_lines.append(
                (node.lineno, "shell=True string: docker run (use list form)")
            )
        if re.search(r"\bclaude\s+-", cmd):
            self.shell_string_lines.append(
                (node.lineno, "shell=True string: claude CLI (use list form)")
            )

    def visit_Call(self, node: ast.Call) -> None:
        """Check subprocess calls for docker run and claude CLI usage."""
        if not self._is_subprocess_call(node):
            self.generic_visit(node)
            return

        if not node.args:
            self.generic_visit(node)
            return

        first_arg = node.args[0]
        # Check for string commands with shell=True
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            self._check_string_command(node)
            self.generic_visit(node)
            return

        if not isinstance(first_arg, ast.List) or not first_arg.elts:
            self.generic_visit(node)
            return

        elts = self._get_list_string_elements(first_arg)
        if len(elts) < 2:
            self.generic_visit(node)
            return

        # Check 1: docker run detection
        if elts[0] == "docker" and elts[1] == "run":
            self._check_dangerous_flags(elts, node.lineno)
            if not self._has_noqa(node.lineno):
                self.docker_run_lines.append((node.lineno, "subprocess call: docker run"))

        # Check 3: claude CLI detection — "claude" as first command element
        if elts[0] == "claude":
            if not self._has_noqa(node.lineno):
                self.claude_cli_lines.append((node.lineno, 'subprocess call: "claude" CLI'))

        # Also detect claude embedded in docker run commands
        # e.g. ["docker", "run", ..., "image", "claude", "--print", ...]
        # Only detect if "claude" is a standalone element (not part of an env var like CLAUDE_KEY=abc)
        if elts[0] == "docker" and elts[1] == "run":
            for elt in elts[2:]:
                if elt == "claude":
                    if not self._has_noqa(node.lineno):
                        self.claude_cli_lines.append(
                            (node.lineno, 'docker run with embedded "claude" CLI')
                        )
                    break

        self.generic_visit(node)


# ── File Checkers ───────────────────────────────────────────────────────────


def check_python_file(file_path: Path) -> DockerClaudeVisitor | None:
    """Parse a Python file and return the visitor with findings."""
    try:
        content = file_path.read_text()
        tree = ast.parse(content, filename=str(file_path))
        lines = content.split("\n")
        visitor = DockerClaudeVisitor(lines)
        visitor.visit(tree)
        return visitor
    except SyntaxError as e:
        print(f"Warning: Could not parse {file_path}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
        return None


def check_shell_file(
    file_path: Path,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Check a shell file for docker run and dangerous flags using regex.

    Returns (docker_run_violations, dangerous_flag_violations).
    """
    docker_violations: list[tuple[int, str]] = []
    dangerous_violations: list[tuple[int, str]] = []

    try:
        content = file_path.read_text()
    except Exception as e:
        print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
        return docker_violations, dangerous_violations

    lines = content.split("\n")

    def has_noqa(lineno: int) -> bool:
        """Check if line or preceding line has noqa comment.

        In shell scripts, noqa comments can be on the same line or the
        preceding line (since inline comments after backslash continuations
        are not valid shell syntax).
        """
        idx = lineno - 1
        if 0 <= idx < len(lines) and f"noqa: {NOQA_CODE}" in lines[idx]:
            return True
        # Also check preceding line for shell scripts
        if 0 <= idx - 1 < len(lines) and f"noqa: {NOQA_CODE}" in lines[idx - 1]:
            return True
        return False

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        # Skip comments
        if stripped.startswith("#"):
            continue
        # Skip noqa lines (same line or preceding line)
        if has_noqa(i):
            continue

        if re.search(r"docker\s+run\b", line):
            docker_violations.append((i, "shell command: docker run"))

        # Dangerous flags in shell
        if re.search(r"--privileged\b", line):
            dangerous_violations.append((i, "Dangerous flag: --privileged"))
        if re.search(r"--network[\s=]+host\b", line):
            dangerous_violations.append((i, "Dangerous flag: --network host"))
        if re.search(r"--pid[\s=]+host\b", line):
            dangerous_violations.append((i, "Dangerous flag: --pid host"))
        if re.search(r"--ipc[\s=]+host\b", line):
            dangerous_violations.append((i, "Dangerous flag: --ipc host"))

    return docker_violations, dangerous_violations


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    """Run all checks and report violations."""
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    all_violations: list[tuple[str, list[tuple[int, str]]]] = []

    # Directories to skip entirely
    skip_dirs = {".venv", ".git", "__pycache__", "node_modules", ".mypy_cache"}

    def should_skip(path: Path) -> bool:
        return any(part in skip_dirs for part in path.parts)

    # ── Check Python files ──────────────────────────────────────────────
    py_files = [f for f in repo_root.rglob("*.py") if not should_skip(f)]

    for py_file in py_files:
        rel = str(py_file.relative_to(repo_root))
        visitor = check_python_file(py_file)
        if visitor is None:
            continue

        file_violations: list[tuple[int, str]] = []

        # docker run detection
        for lineno, desc in visitor.docker_run_lines:
            file_violations.append((lineno, desc))

        # claude CLI detection
        for lineno, desc in visitor.claude_cli_lines:
            file_violations.append((lineno, desc))

        # shell=True string bypasses
        for lineno, desc in visitor.shell_string_lines:
            file_violations.append((lineno, desc))

        # dangerous flags
        for lineno, desc in visitor.dangerous_flag_lines:
            file_violations.append((lineno, desc))

        if file_violations:
            all_violations.append((rel, file_violations))

    # ── Check Shell files ───────────────────────────────────────────────
    sh_files = [f for f in repo_root.rglob("*.sh") if not should_skip(f)]

    for sh_file in sh_files:
        rel = str(sh_file.relative_to(repo_root))
        docker_viols, danger_viols = check_shell_file(sh_file)

        file_violations = []
        file_violations.extend(docker_viols)
        file_violations.extend(danger_viols)

        if file_violations:
            all_violations.append((rel, file_violations))

    # ── Report ──────────────────────────────────────────────────────────
    if all_violations:
        print("ERROR: Found docker/claude invocation violations!\n")
        print("=" * 70)
        print("Each docker run / claude CLI invocation must be explicitly justified.")
        print("=" * 70)
        print()

        for file_path, violations in sorted(all_violations):
            print(f"File: {file_path}")
            for lineno, desc in sorted(violations):
                print(f"  Line {lineno}: {desc}")
            print()

        print("How to fix:")
        print("  Add a noqa comment with justification to the flagged line:")
        print()
        print("    subprocess.run([...])  # noqa: EGG100 - <reason>")
        print()
        print("  Examples of valid justifications:")
        print("    # noqa: EGG100 - test helper container for network isolation tests")
        print("    # noqa: EGG100 - version check for Claude CLI")
        print("    # noqa: EGG100 - gateway container startup")
        print()
        print("  For sandbox containers, prefer build_sandbox_docker_cmd() from")
        print("  shared/egg_container instead of direct docker run calls.")
        print()

        return 1
    else:
        print("OK: No docker/claude invocation violations found")
        return 0


if __name__ == "__main__":
    sys.exit(main())

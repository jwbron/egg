"""Tests for scripts/check-docker-and-claude-invocations.py."""

# Import the linter module
import importlib.util
import textwrap
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_docker_and_claude_invocations",
    Path(__file__).resolve().parents[2] / "scripts" / "check-docker-and-claude-invocations.py",
)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check_python_file = _mod.check_python_file
check_shell_file = _mod.check_shell_file
DockerClaudeVisitor = _mod.DockerClaudeVisitor


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_py(tmp_path: Path, code: str, name: str = "test.py") -> Path:
    """Write a Python file and return its path."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(code))
    return p


def _write_sh(tmp_path: Path, code: str, name: str = "test.sh") -> Path:
    """Write a shell file and return its path."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(code))
    return p


# ── Python docker run detection ────────────────────────────────────────────


class TestDockerRunPython:
    def test_detects_docker_run_list(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--rm", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 1
        assert visitor.docker_run_lines[0][0] == 2

    def test_detects_docker_run_check_call(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.check_call(["docker", "run", "-d", "myimage"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 1

    def test_ignores_docker_build(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "build", "-t", "img", "."])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 0

    def test_ignores_non_subprocess(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            some_func(["docker", "run", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 0

    def test_detects_shell_true_string_docker_run(self, tmp_path: Path) -> None:
        """String commands with shell=True are now detected."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run("docker run alpine", shell=True)
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.shell_string_lines) == 1
        assert "docker run" in visitor.shell_string_lines[0][1]

    def test_detects_shell_true_string_claude(self, tmp_path: Path) -> None:
        """String commands with shell=True invoking claude CLI are detected."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run("claude --print hello", shell=True)
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.shell_string_lines) == 1
        assert "claude CLI" in visitor.shell_string_lines[0][1]

    def test_ignores_claude_word_in_shell_string(self, tmp_path: Path) -> None:
        """Strings containing 'claude' as a word (not CLI) should not be flagged."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run("echo 'claude is cool'", shell=True)
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.shell_string_lines) == 0

    def test_ignores_string_without_shell_true(self, tmp_path: Path) -> None:
        """String commands without shell=True are not checked."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run("docker run alpine")
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.shell_string_lines) == 0


# ── Python claude CLI detection ────────────────────────────────────────────


class TestClaudeCLIPython:
    def test_detects_claude_as_first_element(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["claude", "--print", "hello"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.claude_cli_lines) == 1

    def test_detects_claude_in_docker_run(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--rm", "egg-sandbox:latest", "claude", "--print"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.claude_cli_lines) == 1
        assert "docker run" in visitor.claude_cli_lines[0][1]

    def test_ignores_claude_in_non_command(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            x = "claude is great"
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.claude_cli_lines) == 0

    def test_ignores_claude_in_env_var(self, tmp_path: Path) -> None:
        """Should not flag CLAUDE_KEY=abc as embedded claude CLI."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--env", "CLAUDE_KEY=abc", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.claude_cli_lines) == 0


# ── noqa suppression ──────────────────────────────────────────────────────


class TestNoqaSuppression:
    def test_noqa_suppresses_docker_run(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--rm", "alpine"])  # noqa: EGG100
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 0

    def test_noqa_suppresses_claude(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["claude", "--print"])  # noqa: EGG100
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.claude_cli_lines) == 0

    def test_noqa_with_justification_suppresses(self, tmp_path: Path) -> None:
        """noqa with justification (the required format) should suppress."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--rm", "alpine"])  # noqa: EGG100 - test helper container
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 0

    def test_wrong_noqa_code_does_not_suppress(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--rm", "alpine"])  # noqa: EGG001
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 1


# ── Shell file detection ──────────────────────────────────────────────────


class TestShellFiles:
    def test_detects_docker_run_in_shell(self, tmp_path: Path) -> None:
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker run -d --name mycontainer alpine
            """,
        )
        docker_viols, _ = check_shell_file(f)
        assert len(docker_viols) == 1
        assert docker_viols[0][0] == 2

    def test_skips_comments_in_shell(self, tmp_path: Path) -> None:
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            # docker run -d alpine
            """,
        )
        docker_viols, _ = check_shell_file(f)
        assert len(docker_viols) == 0

    def test_noqa_in_shell(self, tmp_path: Path) -> None:
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker run -d alpine  # noqa: EGG100
            """,
        )
        docker_viols, _ = check_shell_file(f)
        assert len(docker_viols) == 0

    def test_detects_docker_run_with_extra_spaces(self, tmp_path: Path) -> None:
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker   run --rm alpine
            """,
        )
        docker_viols, _ = check_shell_file(f)
        assert len(docker_viols) == 1


# ── Dangerous flags ──────────────────────────────────────────────────────


class TestDangerousFlags:
    def test_detects_privileged_python(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--privileged", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 1
        assert "--privileged" in visitor.dangerous_flag_lines[0][1]

    def test_detects_network_host_python(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--network", "host", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 1
        assert "--network host" in visitor.dangerous_flag_lines[0][1]

    def test_network_non_host_is_fine(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--network", "mynetwork", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 0

    def test_detects_privileged_shell(self, tmp_path: Path) -> None:
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker run --privileged alpine
            """,
        )
        _, danger_viols = check_shell_file(f)
        assert len(danger_viols) == 1
        assert "--privileged" in danger_viols[0][1]

    def test_detects_network_host_shell(self, tmp_path: Path) -> None:
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker run --network host alpine
            """,
        )
        _, danger_viols = check_shell_file(f)
        assert len(danger_viols) == 1
        assert "--network host" in danger_viols[0][1]

    def test_noqa_suppresses_dangerous_flags(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--privileged", "alpine"])  # noqa: EGG100
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 0

    def test_detects_network_equals_host_python(self, tmp_path: Path) -> None:
        """--network=host (equals syntax) is detected."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--network=host", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 1
        assert "--network=host" in visitor.dangerous_flag_lines[0][1]

    def test_detects_network_equals_host_shell(self, tmp_path: Path) -> None:
        """--network=host (equals syntax) is detected in shell."""
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker run --network=host alpine
            """,
        )
        _, danger_viols = check_shell_file(f)
        assert len(danger_viols) == 1
        assert "--network host" in danger_viols[0][1]

    def test_detects_pid_host_python(self, tmp_path: Path) -> None:
        """--pid host is detected."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--pid", "host", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 1
        assert "--pid host" in visitor.dangerous_flag_lines[0][1]

    def test_detects_pid_equals_host_python(self, tmp_path: Path) -> None:
        """--pid=host is detected."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--pid=host", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 1
        assert "--pid=host" in visitor.dangerous_flag_lines[0][1]

    def test_detects_ipc_host_python(self, tmp_path: Path) -> None:
        """--ipc host is detected."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--ipc", "host", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 1
        assert "--ipc host" in visitor.dangerous_flag_lines[0][1]

    def test_detects_ipc_equals_host_python(self, tmp_path: Path) -> None:
        """--ipc=host is detected."""
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--ipc=host", "alpine"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.dangerous_flag_lines) == 1
        assert "--ipc=host" in visitor.dangerous_flag_lines[0][1]

    def test_detects_pid_host_shell(self, tmp_path: Path) -> None:
        """--pid host is detected in shell."""
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker run --pid=host alpine
            """,
        )
        _, danger_viols = check_shell_file(f)
        assert len(danger_viols) == 1
        assert "--pid host" in danger_viols[0][1]

    def test_detects_ipc_host_shell(self, tmp_path: Path) -> None:
        """--ipc host is detected in shell."""
        f = _write_sh(
            tmp_path,
            """\
            #!/bin/bash
            docker run --ipc host alpine
            """,
        )
        _, danger_viols = check_shell_file(f)
        assert len(danger_viols) == 1
        assert "--ipc host" in danger_viols[0][1]


# ── Syntax error handling ────────────────────────────────────────────────


class TestEdgeCases:
    def test_syntax_error_returns_none(self, tmp_path: Path) -> None:
        f = _write_py(tmp_path, "def broken(:\n")
        visitor = check_python_file(f)
        assert visitor is None

    def test_empty_file(self, tmp_path: Path) -> None:
        f = _write_py(tmp_path, "")
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 0
        assert len(visitor.claude_cli_lines) == 0

    def test_multiple_violations_in_one_file(self, tmp_path: Path) -> None:
        f = _write_py(
            tmp_path,
            """\
            import subprocess
            subprocess.run(["docker", "run", "--rm", "alpine"])
            subprocess.run(["claude", "--print", "hello"])
            subprocess.run(["docker", "run", "--privileged", "bad"])
            """,
        )
        visitor = check_python_file(f)
        assert visitor is not None
        assert len(visitor.docker_run_lines) == 2
        assert len(visitor.claude_cli_lines) == 1
        assert len(visitor.dangerous_flag_lines) == 1

#!/usr/bin/env python3
"""
Test Discovery and Execution Tool

Dynamically discovers test configurations and patterns in a codebase,
determines how to run tests, and provides test commands.

Usage:
  # Discover tests in current directory
  discover-tests.py

  # Discover tests in specific directory
  discover-tests.py /path/to/project

  # Output JSON format
  discover-tests.py --json

  # Run discovered tests
  discover-tests.py --run

  # Run tests for specific files (changed files)
  discover-tests.py --run --files "src/foo.py,src/bar.py"
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class TestFramework:
    """Represents a detected test framework."""

    name: str
    language: str
    config_file: str | None = None
    test_command: str = ""
    test_dirs: list[str] = field(default_factory=list)
    test_patterns: list[str] = field(default_factory=list)
    watch_command: str | None = None
    coverage_command: str | None = None


@dataclass
class TestDiscoveryResult:
    """Result of test discovery."""

    project_root: str
    frameworks: list[TestFramework] = field(default_factory=list)
    makefile_targets: list[str] = field(default_factory=list)
    recommended_command: str = ""
    lint_command: str | None = None
    test_file_count: int = 0
    notes: list[str] = field(default_factory=list)


class TestDiscovery:
    """Discovers test configurations and patterns in a codebase."""

    # Common test directory names
    TEST_DIRS = ["test", "tests", "spec", "specs", "__tests__", "test_*"]

    # Common test file patterns
    TEST_FILE_PATTERNS = {
        "python": ["test_*.py", "*_test.py", "tests.py"],
        "javascript": [
            "*.test.js",
            "*.spec.js",
            "*.test.ts",
            "*.spec.ts",
            "*.test.jsx",
            "*.spec.jsx",
            "*.test.tsx",
            "*.spec.tsx",
        ],
        "go": ["*_test.go"],
        "java": ["*Test.java", "*Tests.java", "*Spec.java"],
        "ruby": ["*_spec.rb", "*_test.rb"],
        "rust": ["*_test.rs"],  # Also inline #[test] modules
    }

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        self.result = TestDiscoveryResult(project_root=str(self.project_root))

    def discover(self) -> TestDiscoveryResult:
        """Run full test discovery."""
        # Check for various test configurations
        self._check_python_tests()
        self._check_javascript_tests()
        self._check_go_tests()
        self._check_java_tests()
        self._check_makefile()
        self._check_shell_scripts()

        # Count test files
        self._count_test_files()

        # Determine recommended command
        self._determine_recommended_command()

        return self.result

    def _check_python_tests(self):
        """Check for Python test frameworks."""
        # Check for pytest
        pytest_configs = ["pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py"]
        pytest_found = False

        for config in pytest_configs:
            config_path = self.project_root / config
            if config_path.exists():
                content = config_path.read_text() if config != "conftest.py" else ""
                if config == "conftest.py" or "[tool.pytest" in content or "[pytest]" in content:
                    pytest_found = True
                    framework = TestFramework(
                        name="pytest",
                        language="python",
                        config_file=config,
                        test_command="pytest",
                        test_patterns=["test_*.py", "*_test.py"],
                        watch_command="pytest-watch",
                        coverage_command="pytest --cov",
                    )
                    # Try to find test directories
                    framework.test_dirs = self._find_test_dirs(["test", "tests"])
                    self.result.frameworks.append(framework)
                    break

        # Check for unittest (if no pytest)
        if not pytest_found:
            # Look for unittest-style tests
            test_dirs = self._find_test_dirs(["test", "tests"])
            if test_dirs or list(self.project_root.glob("test_*.py")):
                framework = TestFramework(
                    name="unittest",
                    language="python",
                    test_command="python -m unittest discover",
                    test_patterns=["test_*.py"],
                    test_dirs=test_dirs,
                )
                self.result.frameworks.append(framework)

        # Check for requirements-dev.txt or test dependencies
        for req_file in ["requirements-dev.txt", "requirements-test.txt", "test-requirements.txt"]:
            if (self.project_root / req_file).exists():
                self.result.notes.append(f"Test dependencies in {req_file}")

    def _check_javascript_tests(self):
        """Check for JavaScript/TypeScript test frameworks."""
        package_json = self.project_root / "package.json"
        if not package_json.exists():
            return

        try:
            pkg = json.loads(package_json.read_text())
        except json.JSONDecodeError:
            return

        scripts = pkg.get("scripts", {})
        dev_deps = pkg.get("devDependencies", {})
        deps = pkg.get("dependencies", {})
        all_deps = {**deps, **dev_deps}

        # Check for Jest
        jest_config_files = [
            "jest.config.js",
            "jest.config.ts",
            "jest.config.json",
            "jest.config.mjs",
        ]
        jest_found = any((self.project_root / f).exists() for f in jest_config_files)
        jest_found = jest_found or "jest" in pkg or "jest" in all_deps

        if jest_found:
            config_file = next(
                (f for f in jest_config_files if (self.project_root / f).exists()), None
            )
            test_cmd = scripts.get("test", "jest")
            if "test" in scripts:
                test_cmd = "npm test"
            framework = TestFramework(
                name="jest",
                language="javascript",
                config_file=config_file,
                test_command=test_cmd,
                test_patterns=["*.test.js", "*.spec.js", "*.test.ts", "*.spec.ts"],
                test_dirs=self._find_test_dirs(["__tests__", "test", "tests"]),
                watch_command=f"{test_cmd} -- --watch"
                if test_cmd == "npm test"
                else "jest --watch",
                coverage_command=f"{test_cmd} -- --coverage"
                if test_cmd == "npm test"
                else "jest --coverage",
            )
            self.result.frameworks.append(framework)

        # Check for Mocha
        elif "mocha" in all_deps:
            framework = TestFramework(
                name="mocha",
                language="javascript",
                test_command=scripts.get("test", "mocha"),
                test_patterns=["*.test.js", "*.spec.js"],
                test_dirs=self._find_test_dirs(["test", "tests"]),
                watch_command="mocha --watch",
            )
            self.result.frameworks.append(framework)

        # Check for Vitest
        elif "vitest" in all_deps:
            framework = TestFramework(
                name="vitest",
                language="javascript",
                config_file="vitest.config.ts"
                if (self.project_root / "vitest.config.ts").exists()
                else None,
                test_command=scripts.get("test", "vitest"),
                test_patterns=["*.test.ts", "*.spec.ts"],
                test_dirs=self._find_test_dirs(["test", "tests", "__tests__"]),
                watch_command="vitest --watch",
                coverage_command="vitest --coverage",
            )
            self.result.frameworks.append(framework)

        # Check for Playwright (e2e)
        playwright_config_files = ["playwright.config.ts", "playwright.config.js"]
        playwright_config = next(
            (f for f in playwright_config_files if (self.project_root / f).exists()), None
        )
        if playwright_config or "playwright" in all_deps or "@playwright/test" in all_deps:
            framework = TestFramework(
                name="playwright",
                language="javascript",
                config_file=playwright_config,
                test_command="npx playwright test",
                test_patterns=["*.spec.ts", "*.test.ts"],
                test_dirs=self._find_test_dirs(["e2e", "tests", "test"]),
            )
            self.result.frameworks.append(framework)
            self.result.notes.append("Playwright detected - E2E tests may require browser setup")

        # Check for lint scripts
        if "lint" in scripts:
            self.result.lint_command = "npm run lint"
        elif "eslint" in all_deps:
            self.result.lint_command = "npx eslint ."

    def _check_go_tests(self):
        """Check for Go tests."""
        go_mod = self.project_root / "go.mod"
        if not go_mod.exists():
            return

        # Go has built-in testing
        framework = TestFramework(
            name="go-test",
            language="go",
            config_file="go.mod",
            test_command="go test ./...",
            test_patterns=["*_test.go"],
            watch_command=None,  # Could suggest gotestsum or similar
            coverage_command="go test -cover ./...",
        )
        self.result.frameworks.append(framework)

    def _check_java_tests(self):
        """Check for Java/Gradle/Maven tests."""
        # Check for Gradle
        if (self.project_root / "build.gradle").exists() or (
            self.project_root / "build.gradle.kts"
        ).exists():
            config_file = (
                "build.gradle"
                if (self.project_root / "build.gradle").exists()
                else "build.gradle.kts"
            )
            framework = TestFramework(
                name="gradle",
                language="java",
                config_file=config_file,
                test_command="./gradlew test",
                test_patterns=["*Test.java", "*Tests.java"],
                test_dirs=["src/test/java"],
            )
            self.result.frameworks.append(framework)
            return

        # Check for Maven
        if (self.project_root / "pom.xml").exists():
            framework = TestFramework(
                name="maven",
                language="java",
                config_file="pom.xml",
                test_command="mvn test",
                test_patterns=["*Test.java", "*Tests.java"],
                test_dirs=["src/test/java"],
            )
            self.result.frameworks.append(framework)

    def _check_makefile(self):
        """Check Makefile for test targets."""
        makefile_names = ["Makefile", "makefile", "GNUmakefile"]
        makefile = None
        for name in makefile_names:
            mf = self.project_root / name
            if mf.exists():
                makefile = mf
                break

        if not makefile:
            return

        content = makefile.read_text()

        # Find test-related targets
        test_targets = []
        # Match lines like "test:" or "test-unit:" at the start of a line
        for match in re.finditer(r"^([a-zA-Z_-]*test[a-zA-Z_-]*)\s*:", content, re.MULTILINE):
            test_targets.append(match.group(1))

        # Also check for lint targets
        for match in re.finditer(r"^(lint|check|verify)\s*:", content, re.MULTILINE):
            if not self.result.lint_command:
                self.result.lint_command = f"make {match.group(1)}"

        self.result.makefile_targets = list(set(test_targets))

        if test_targets:
            self.result.notes.append(f"Makefile test targets: {', '.join(test_targets)}")

    def _check_shell_scripts(self):
        """Check for test runner shell scripts."""
        scripts_dirs = [".", "scripts", "bin", "tools"]
        test_script_patterns = ["test.sh", "run-tests.sh", "run_tests.sh", "test-*.sh"]

        for scripts_dir in scripts_dirs:
            dir_path = self.project_root / scripts_dir
            if not dir_path.exists():
                continue
            for pattern in test_script_patterns:
                for script in dir_path.glob(pattern):
                    self.result.notes.append(
                        f"Test script found: {script.relative_to(self.project_root)}"
                    )

    def _find_test_dirs(self, candidates: list[str]) -> list[str]:
        """Find existing test directories from candidates."""
        found = []
        for candidate in candidates:
            if (self.project_root / candidate).is_dir():
                found.append(candidate)
        return found

    def _count_test_files(self):
        """Count total test files found."""
        count = 0
        counted_files: set[Path] = set()

        for framework in self.result.frameworks:
            for pattern in framework.test_patterns:
                for test_file in self.project_root.rglob(pattern):
                    # Skip node_modules, vendor, etc.
                    if any(
                        part in test_file.parts
                        for part in ["node_modules", "vendor", ".git", "dist", "build"]
                    ):
                        continue
                    if test_file not in counted_files:
                        counted_files.add(test_file)
                        count += 1

        self.result.test_file_count = count

    def _determine_recommended_command(self):
        """Determine the best command to run all tests."""
        # Prefer Makefile test target if available
        if "test" in self.result.makefile_targets:
            self.result.recommended_command = "make test"
            return

        # Otherwise use the first framework's test command
        if self.result.frameworks:
            self.result.recommended_command = self.result.frameworks[0].test_command
            return

        # Fallback
        self.result.recommended_command = ""
        self.result.notes.append("No test framework detected - manual configuration may be needed")

    def run_tests(self, files: list[str] | None = None) -> int:
        """Run the discovered tests."""
        if not self.result.recommended_command:
            print("No test command discovered. Please configure tests manually.", file=sys.stderr)
            return 1

        cmd = self.result.recommended_command

        # If specific files provided, try to run targeted tests
        if files and self.result.frameworks:
            framework = self.result.frameworks[0]
            if framework.name == "pytest":
                # pytest can take file paths
                cmd = f"pytest {' '.join(files)}"
            elif framework.name == "jest":
                # Jest can use --findRelatedTests
                cmd = f"jest --findRelatedTests {' '.join(files)}"

        print(f"Running: {cmd}")
        result = subprocess.run(cmd, check=False, shell=True, cwd=self.project_root)
        return result.returncode


def format_output(result: TestDiscoveryResult, as_json: bool = False) -> str:
    """Format the discovery result for output."""
    if as_json:
        output = {
            "project_root": result.project_root,
            "frameworks": [asdict(f) for f in result.frameworks],
            "makefile_targets": result.makefile_targets,
            "recommended_command": result.recommended_command,
            "lint_command": result.lint_command,
            "test_file_count": result.test_file_count,
            "notes": result.notes,
        }
        return json.dumps(output, indent=2)

    lines = [
        "# Test Discovery Results",
        "",
        f"**Project**: {result.project_root}",
        f"**Test Files Found**: {result.test_file_count}",
        "",
    ]

    if result.frameworks:
        lines.append("## Detected Frameworks")
        lines.append("")
        for fw in result.frameworks:
            lines.append(f"### {fw.name} ({fw.language})")
            if fw.config_file:
                lines.append(f"- **Config**: {fw.config_file}")
            lines.append(f"- **Test Command**: `{fw.test_command}`")
            if fw.test_dirs:
                lines.append(f"- **Test Directories**: {', '.join(fw.test_dirs)}")
            if fw.watch_command:
                lines.append(f"- **Watch Mode**: `{fw.watch_command}`")
            if fw.coverage_command:
                lines.append(f"- **Coverage**: `{fw.coverage_command}`")
            lines.append("")

    if result.makefile_targets:
        lines.append("## Makefile Targets")
        for target in result.makefile_targets:
            lines.append(f"- `make {target}`")
        lines.append("")

    lines.append("## Recommended Commands")
    lines.append("")
    if result.recommended_command:
        lines.append(f"**Run Tests**: `{result.recommended_command}`")
    if result.lint_command:
        lines.append(f"**Run Linting**: `{result.lint_command}`")
    lines.append("")

    if result.notes:
        lines.append("## Notes")
        for note in result.notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Discover test configurations and patterns in a codebase"
    )
    parser.add_argument("path", nargs="?", default=".", help="Project root directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--run", action="store_true", help="Run discovered tests")
    parser.add_argument(
        "--files", help="Comma-separated list of changed files (for targeted testing)"
    )

    args = parser.parse_args()

    discovery = TestDiscovery(args.path)
    result = discovery.discover()

    if args.run:
        files = args.files.split(",") if args.files else None
        sys.exit(discovery.run_tests(files))
    else:
        print(format_output(result, as_json=args.json))


if __name__ == "__main__":
    main()

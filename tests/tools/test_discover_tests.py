"""
Tests for the discover-tests.py tool.
"""

import json

# Import the module directly for testing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox" / "tools"))

from importlib.machinery import SourceFileLoader

# Load discover-tests module (hyphenated filename requires special handling)
discover_tests_path = (
    Path(__file__).parent.parent.parent / "sandbox" / "tools" / "discover-tests.py"
)
loader = SourceFileLoader("discover_tests", str(discover_tests_path))
discover_tests = loader.load_module()

TestFramework = discover_tests.TestFramework
TestDiscoveryResult = discover_tests.TestDiscoveryResult
TestDiscovery = discover_tests.TestDiscovery
format_output = discover_tests.format_output


class TestTestFramework:
    """Tests for TestFramework dataclass."""

    def test_default_values(self):
        fw = TestFramework(name="pytest", language="python")
        assert fw.name == "pytest"
        assert fw.language == "python"
        assert fw.config_file is None
        assert fw.test_command == ""
        assert fw.test_dirs == []
        assert fw.test_patterns == []
        assert fw.watch_command is None
        assert fw.coverage_command is None

    def test_with_all_values(self):
        fw = TestFramework(
            name="jest",
            language="javascript",
            config_file="jest.config.js",
            test_command="npm test",
            test_dirs=["__tests__", "test"],
            test_patterns=["*.test.js", "*.spec.js"],
            watch_command="npm test -- --watch",
            coverage_command="npm test -- --coverage",
        )
        assert fw.name == "jest"
        assert fw.config_file == "jest.config.js"
        assert "__tests__" in fw.test_dirs


class TestTestDiscoveryResult:
    """Tests for TestDiscoveryResult dataclass."""

    def test_default_values(self):
        result = TestDiscoveryResult(project_root="/test/path")
        assert result.project_root == "/test/path"
        assert result.frameworks == []
        assert result.makefile_targets == []
        assert result.recommended_command == ""
        assert result.lint_command is None
        assert result.test_file_count == 0
        assert result.notes == []


class TestTestDiscoveryPython:
    """Tests for Python test framework detection."""

    def test_detect_pytest_from_conftest(self, temp_dir):
        """Test detecting pytest from conftest.py."""
        (temp_dir / "conftest.py").write_text("import pytest")
        (temp_dir / "tests").mkdir()
        (temp_dir / "tests" / "test_example.py").write_text("def test_foo(): pass")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        assert len(result.frameworks) >= 1
        pytest_fw = next((f for f in result.frameworks if f.name == "pytest"), None)
        assert pytest_fw is not None
        assert pytest_fw.language == "python"
        assert pytest_fw.test_command == "pytest"

    def test_detect_pytest_from_pyproject(self, temp_dir):
        """Test detecting pytest from pyproject.toml."""
        pyproject_content = """
[project]
name = "test-project"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""
        (temp_dir / "pyproject.toml").write_text(pyproject_content)
        (temp_dir / "tests").mkdir()

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        pytest_fw = next((f for f in result.frameworks if f.name == "pytest"), None)
        assert pytest_fw is not None
        assert pytest_fw.config_file == "pyproject.toml"

    def test_detect_unittest_fallback(self, temp_dir):
        """Test detecting unittest when no pytest config exists."""
        (temp_dir / "tests").mkdir()
        (temp_dir / "tests" / "test_example.py").write_text(
            "import unittest\nclass TestFoo(unittest.TestCase): pass"
        )

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        unittest_fw = next((f for f in result.frameworks if f.name == "unittest"), None)
        assert unittest_fw is not None
        assert unittest_fw.test_command == "python -m unittest discover"


class TestTestDiscoveryJavaScript:
    """Tests for JavaScript/TypeScript test framework detection."""

    def test_detect_jest_from_package_json(self, temp_dir):
        """Test detecting Jest from package.json."""
        package_json = {
            "name": "test-project",
            "devDependencies": {"jest": "^29.0.0"},
            "scripts": {"test": "jest"},
        }
        (temp_dir / "package.json").write_text(json.dumps(package_json))

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        jest_fw = next((f for f in result.frameworks if f.name == "jest"), None)
        assert jest_fw is not None
        assert jest_fw.language == "javascript"
        assert "npm test" in jest_fw.test_command

    def test_detect_jest_config_file(self, temp_dir):
        """Test detecting Jest from config file."""
        (temp_dir / "jest.config.js").write_text("module.exports = {}")
        (temp_dir / "package.json").write_text(json.dumps({"name": "test"}))

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        jest_fw = next((f for f in result.frameworks if f.name == "jest"), None)
        assert jest_fw is not None
        assert jest_fw.config_file == "jest.config.js"

    def test_detect_vitest(self, temp_dir):
        """Test detecting Vitest."""
        package_json = {
            "name": "test-project",
            "devDependencies": {"vitest": "^1.0.0"},
            "scripts": {"test": "vitest"},
        }
        (temp_dir / "package.json").write_text(json.dumps(package_json))

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        vitest_fw = next((f for f in result.frameworks if f.name == "vitest"), None)
        assert vitest_fw is not None
        assert "vitest" in vitest_fw.test_command

    def test_detect_mocha(self, temp_dir):
        """Test detecting Mocha."""
        package_json = {"name": "test-project", "devDependencies": {"mocha": "^10.0.0"}}
        (temp_dir / "package.json").write_text(json.dumps(package_json))

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        mocha_fw = next((f for f in result.frameworks if f.name == "mocha"), None)
        assert mocha_fw is not None

    def test_detect_playwright(self, temp_dir):
        """Test detecting Playwright."""
        package_json = {"name": "test-project", "devDependencies": {"@playwright/test": "^1.40.0"}}
        (temp_dir / "package.json").write_text(json.dumps(package_json))
        (temp_dir / "playwright.config.ts").write_text("export default {}")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        playwright_fw = next((f for f in result.frameworks if f.name == "playwright"), None)
        assert playwright_fw is not None
        assert playwright_fw.config_file == "playwright.config.ts"
        assert "Playwright detected" in str(result.notes)


class TestTestDiscoveryGo:
    """Tests for Go test detection."""

    def test_detect_go_tests(self, temp_dir):
        """Test detecting Go tests from go.mod."""
        (temp_dir / "go.mod").write_text("module example.com/test\n\ngo 1.21")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        go_fw = next((f for f in result.frameworks if f.name == "go-test"), None)
        assert go_fw is not None
        assert go_fw.language == "go"
        assert go_fw.test_command == "go test ./..."
        assert go_fw.coverage_command == "go test -cover ./..."


class TestTestDiscoveryJava:
    """Tests for Java test detection."""

    def test_detect_gradle(self, temp_dir):
        """Test detecting Gradle tests."""
        (temp_dir / "build.gradle").write_text("apply plugin: 'java'")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        gradle_fw = next((f for f in result.frameworks if f.name == "gradle"), None)
        assert gradle_fw is not None
        assert gradle_fw.language == "java"
        assert gradle_fw.test_command == "./gradlew test"

    def test_detect_gradle_kotlin(self, temp_dir):
        """Test detecting Gradle Kotlin DSL."""
        (temp_dir / "build.gradle.kts").write_text("plugins { java }")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        gradle_fw = next((f for f in result.frameworks if f.name == "gradle"), None)
        assert gradle_fw is not None
        assert gradle_fw.config_file == "build.gradle.kts"

    def test_detect_maven(self, temp_dir):
        """Test detecting Maven tests."""
        (temp_dir / "pom.xml").write_text("<project></project>")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        maven_fw = next((f for f in result.frameworks if f.name == "maven"), None)
        assert maven_fw is not None
        assert maven_fw.test_command == "mvn test"


class TestMakefileDetection:
    """Tests for Makefile test target detection."""

    def test_detect_makefile_test_targets(self, temp_dir):
        """Test detecting test targets in Makefile."""
        makefile_content = """
.PHONY: test test-unit test-integration lint

test:
\tpytest

test-unit:
\tpytest tests/unit

test-integration:
\tpytest tests/integration

lint:
\truff check .
"""
        (temp_dir / "Makefile").write_text(makefile_content)

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        assert "test" in result.makefile_targets
        assert "test-unit" in result.makefile_targets
        assert "test-integration" in result.makefile_targets
        assert result.lint_command == "make lint"

    def test_recommended_command_uses_makefile(self, temp_dir):
        """Test that make test is recommended when available."""
        (temp_dir / "Makefile").write_text("test:\n\tpytest")
        (temp_dir / "conftest.py").write_text("")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        assert result.recommended_command == "make test"


class TestTestFileCount:
    """Tests for test file counting."""

    def test_count_python_test_files(self, temp_dir):
        """Test counting Python test files."""
        (temp_dir / "conftest.py").write_text("")
        (temp_dir / "tests").mkdir()
        (temp_dir / "tests" / "test_one.py").write_text("")
        (temp_dir / "tests" / "test_two.py").write_text("")
        (temp_dir / "tests" / "helper.py").write_text("")  # Not a test file

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        assert result.test_file_count == 2

    def test_ignores_node_modules(self, temp_dir):
        """Test that node_modules is ignored in test file count."""
        package_json = {"name": "test", "devDependencies": {"jest": "1.0"}}
        (temp_dir / "package.json").write_text(json.dumps(package_json))
        (temp_dir / "node_modules").mkdir()
        (temp_dir / "node_modules" / "pkg").mkdir()
        (temp_dir / "node_modules" / "pkg" / "test.spec.js").write_text("")
        (temp_dir / "src").mkdir()
        (temp_dir / "src" / "app.test.js").write_text("")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()

        assert result.test_file_count == 1


class TestFormatOutput:
    """Tests for output formatting."""

    def test_format_output_json(self, temp_dir):
        """Test JSON output format."""
        (temp_dir / "conftest.py").write_text("")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()
        output = format_output(result, as_json=True)

        parsed = json.loads(output)
        assert "project_root" in parsed
        assert "frameworks" in parsed
        assert "recommended_command" in parsed

    def test_format_output_markdown(self, temp_dir):
        """Test markdown output format."""
        (temp_dir / "conftest.py").write_text("")

        discovery = TestDiscovery(str(temp_dir))
        result = discovery.discover()
        output = format_output(result, as_json=False)

        assert "# Test Discovery Results" in output
        assert "## Detected Frameworks" in output
        assert "pytest" in output


class TestFindTestDirs:
    """Tests for test directory discovery."""

    def test_find_existing_test_dirs(self, temp_dir):
        """Test finding existing test directories."""
        (temp_dir / "tests").mkdir()
        (temp_dir / "test").mkdir()
        (temp_dir / "__tests__").mkdir()

        discovery = TestDiscovery(str(temp_dir))
        found = discovery._find_test_dirs(["tests", "test", "spec", "__tests__"])

        assert "tests" in found
        assert "test" in found
        assert "__tests__" in found
        assert "spec" not in found

    def test_find_no_test_dirs(self, temp_dir):
        """Test when no test directories exist."""
        discovery = TestDiscovery(str(temp_dir))
        found = discovery._find_test_dirs(["tests", "test"])

        assert found == []

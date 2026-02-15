"""
Tests for CI configuration consistency.

Validates that test.yml, pyproject.toml, CONTRIBUTING.md, and Makefile
are consistent regarding which test directories and scan targets are
included. Ensures pytest.ini does not exist (config consolidated into
pyproject.toml).
"""

from pathlib import Path

import yaml

# Repository root (tests/config/ -> ../..)
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestPytestConfigConsolidation:
    """Verify pytest.ini is removed and pyproject.toml has correct config."""

    def test_pytest_ini_does_not_exist(self):
        """pytest.ini should not exist; all config lives in pyproject.toml."""
        assert not (REPO_ROOT / "pytest.ini").exists(), (
            "pytest.ini still exists — pytest config should be consolidated "
            "into pyproject.toml [tool.pytest.ini_options]"
        )

    def test_pyproject_has_pytest_config(self):
        """pyproject.toml must contain [tool.pytest.ini_options]."""
        content = (REPO_ROOT / "pyproject.toml").read_text()
        assert "[tool.pytest.ini_options]" in content

    def test_pyproject_testpaths_include_all_suites(self):
        """testpaths must include tests/, gateway/tests/, and orchestrator/tests/."""
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)

        testpaths = cfg["tool"]["pytest"]["ini_options"]["testpaths"]
        expected = {"tests", "gateway/tests", "orchestrator/tests"}
        assert set(testpaths) == expected, (
            f"testpaths={testpaths} does not match expected={expected}"
        )

    def test_pyproject_has_required_markers(self):
        """Standard markers must be defined to avoid warnings."""
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)

        markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
        marker_names = {m.split(":")[0].strip() for m in markers}
        required = {"integration", "functional", "e2e", "security", "agent_flaky"}
        assert required.issubset(marker_names), (
            f"Missing markers: {required - marker_names}"
        )

    def test_pyproject_has_docker_dev_dependency(self):
        """docker package must be in dev dependencies for orchestrator tests."""
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)

        dev_deps = cfg["project"]["optional-dependencies"]["dev"]
        docker_deps = [d for d in dev_deps if d.startswith("docker")]
        assert len(docker_deps) > 0, (
            "docker package not found in dev dependencies — "
            "required by orchestrator/tests/test_devserver.py and test_docker_client.py"
        )


class TestCIWorkflowConsistency:
    """Verify test.yml includes all test suites and scan targets."""

    def _load_test_workflow(self):
        with open(REPO_ROOT / ".github" / "workflows" / "test.yml") as f:
            return yaml.safe_load(f)

    def test_unit_job_runs_all_test_directories(self):
        """The unit test command must include tests/, gateway/tests/, and orchestrator/tests/."""
        wf = self._load_test_workflow()
        run_cmd = wf["jobs"]["unit"]["steps"][-1]["run"]

        for test_dir in ["tests/", "gateway/tests/", "orchestrator/tests/"]:
            assert test_dir in run_cmd, (
                f"'{test_dir}' not found in unit test run command"
            )

    def test_unit_job_pythonpath_includes_orchestrator(self):
        """PYTHONPATH must include orchestrator for import resolution."""
        wf = self._load_test_workflow()
        run_cmd = wf["jobs"]["unit"]["steps"][-1]["run"]

        assert "orchestrator" in run_cmd.split("PYTHONPATH=")[1].split()[0], (
            "orchestrator not in PYTHONPATH for unit tests"
        )

    def test_bandit_scan_includes_orchestrator(self):
        """The Bandit security scan must include the orchestrator directory."""
        wf = self._load_test_workflow()
        security_steps = wf["jobs"]["security"]["steps"]
        bandit_step = [s for s in security_steps if "bandit" in s.get("run", "")]
        assert len(bandit_step) == 1, "Expected exactly one bandit step"

        bandit_cmd = bandit_step[0]["run"]
        assert "orchestrator" in bandit_cmd, (
            "orchestrator not included in bandit security scan"
        )


class TestDocumentationConsistency:
    """Verify docs mention orchestrator tests."""

    def test_contributing_lists_orchestrator_tests(self):
        """CONTRIBUTING.md must mention orchestrator tests."""
        content = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert "orchestrator/tests/" in content or "Orchestrator tests" in content, (
            "CONTRIBUTING.md does not mention orchestrator tests"
        )

    def test_makefile_bandit_help_includes_orchestrator(self):
        """Makefile help text for bandit must include orchestrator."""
        content = (REPO_ROOT / "Makefile").read_text()
        # Find the bandit help line
        for line in content.splitlines():
            if "bandit" in line and "-r" in line:
                assert "orchestrator" in line, (
                    f"Makefile bandit help line does not include orchestrator: {line}"
                )
                return
        # If no bandit line found in Makefile, that's also fine (not all Makefiles have it)


class TestMypyOverrides:
    """Verify mypy config includes orchestrator test overrides."""

    def test_orchestrator_tests_mypy_override_exists(self):
        """mypy overrides must include orchestrator.tests.* to match gateway.tests.* pattern."""
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)

        overrides = cfg["tool"]["mypy"]["overrides"]
        override_modules = []
        for override in overrides:
            modules = override.get("module", [])
            if isinstance(modules, str):
                modules = [modules]
            override_modules.extend(modules)

        assert "orchestrator.tests.*" in override_modules, (
            "Missing mypy override for orchestrator.tests.* — "
            "orchestrator tests should have relaxed type checking like gateway tests"
        )

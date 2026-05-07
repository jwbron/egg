"""
Tests for CI configuration consistency.

Validates that Makefile, pyproject.toml, and CONTRIBUTING.md are consistent
regarding which test directories and scan targets are included. CI workflows
delegate to Makefile targets, so consistency checks focus on the Makefile.
Ensures pytest.ini does not exist (config consolidated into pyproject.toml).
"""

from pathlib import Path

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
        expected = {"tests", "gateway/tests", "orchestrator/tests", "shared/tests"}
        assert set(testpaths) == expected, (
            f"testpaths={testpaths} does not match expected={expected}"
        )

    def test_pyproject_has_required_markers(self):
        """Standard markers must be defined to avoid warnings.

        Issue #2474 retired the docker-compose runtime, deleting the
        ``functional`` tier (``tests/functional/``) and the real-LLM
        ``e2e`` / ``agent_flaky`` tiers (``integration_tests/test_e2e_*``).
        Only ``integration`` (k3s) and ``security`` markers remain.
        """
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)

        markers = cfg["tool"]["pytest"]["ini_options"]["markers"]
        marker_names = {m.split(":")[0].strip() for m in markers}
        required = {"integration", "security"}
        assert required.issubset(marker_names), f"Missing markers: {required - marker_names}"

    def test_pyproject_has_kubernetes_dev_dependency(self):
        """kubernetes package must be in dev dependencies for orchestrator tests."""
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)

        dev_deps = cfg["project"]["optional-dependencies"]["dev"]
        k8s_deps = [d for d in dev_deps if d.startswith("kubernetes")]
        assert len(k8s_deps) > 0, (
            "kubernetes package not found in dev dependencies — "
            "required by orchestrator/tests/test_kubernetes_client.py"
        )


class TestCIWorkflowConsistency:
    """Verify CI delegates to Makefile and Makefile includes all targets."""

    def _load_makefile(self):
        return (REPO_ROOT / "Makefile").read_text()

    def test_unit_job_runs_all_test_directories(self):
        """The Makefile test target must include all test directories."""
        makefile = self._load_makefile()

        for test_dir in ["tests/", "gateway/tests/", "orchestrator/tests/", "shared/tests/"]:
            assert test_dir in makefile, f"'{test_dir}' not found in Makefile test target"

    def test_unit_job_pythonpath_includes_orchestrator(self):
        """PYTHONPATH must include orchestrator for import resolution."""
        makefile = self._load_makefile()

        # The Makefile exports PYTHONPATH with orchestrator
        assert "PYTHONPATH" in makefile, "PYTHONPATH not set in Makefile"
        # Find the PYTHONPATH line and check it includes orchestrator
        for line in makefile.splitlines():
            if "PYTHONPATH" in line and ":=" in line:
                assert "orchestrator" in line, "orchestrator not in PYTHONPATH in Makefile"
                break

    def test_bandit_scan_includes_orchestrator(self):
        """The Makefile security target must include the orchestrator directory."""
        makefile = self._load_makefile()

        # Find bandit command in the security target
        found_bandit_with_orchestrator = False
        for line in makefile.splitlines():
            if "bandit" in line.lower() and "-r" in line and "orchestrator" in line:
                found_bandit_with_orchestrator = True
                break

        assert found_bandit_with_orchestrator, (
            "orchestrator not included in bandit security scan in Makefile"
        )


class TestDocumentationConsistency:
    """Verify docs mention orchestrator tests."""

    def test_contributing_lists_orchestrator_tests(self):
        """CONTRIBUTING.md must mention orchestrator tests."""
        content = (REPO_ROOT / "CONTRIBUTING.md").read_text()
        assert "orchestrator/tests/" in content or "Orchestrator tests" in content, (
            "CONTRIBUTING.md does not mention orchestrator tests"
        )

    def test_makefile_bandit_includes_orchestrator(self):
        """Makefile security target must run bandit on orchestrator."""
        content = (REPO_ROOT / "Makefile").read_text()

        found_bandit_with_orchestrator = False
        for line in content.splitlines():
            if "bandit" in line.lower() and "orchestrator" in line:
                found_bandit_with_orchestrator = True
                break

        assert found_bandit_with_orchestrator, (
            "Makefile does not include orchestrator in bandit security scan"
        )


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

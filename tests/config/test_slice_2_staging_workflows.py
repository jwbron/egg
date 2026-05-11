"""Structural assertions over the slice-2 staged workflow YAMLs.

Slice-2 of issue #2474 wires `test-integration.yml` into PR CI by
staging two workflow files under ``.github-staging/workflows/``:

* ``.github-staging/workflows/test.yml`` — end-state of the existing
  ``Test`` workflow with a new ``integration`` job sibling to
  ``unit`` / ``security``, folded into the ``aggregate`` job's
  ``needs`` and the if-all-passed check.
* ``.github-staging/workflows/test-integration.yml`` — end-state of
  the existing reusable integration workflow hardened with
  HITL-Q1 flake guards: image-import retry, explicit ``kubectl
  wait`` timeouts, and an on-failure ``k3s-debug`` artifact
  capturing ``kubectl get events --all-namespaces`` and pod logs.

A human reviewer performs ``git mv .github-staging/workflows/*.yml
.github/workflows/`` before merging slice-2's PR (the coder role is
gateway-blocked from ``.github/``). Until then, the staged files
carry the proposed end-state and these tests guard the invariants.

The tests skip cleanly when slice-2 has not landed yet on the
working branch (the staged files are absent on ``main``) so the
unit suite stays green for the pipeline's pre-slice-2 history.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STAGED_TEST_YML = REPO_ROOT / ".github-staging" / "workflows" / "test.yml"
STAGED_TEST_INTEGRATION_YML = REPO_ROOT / ".github-staging" / "workflows" / "test-integration.yml"


def _load_yaml(path: Path) -> dict:
    """Parse a YAML file, treating GitHub Actions' bare ``on:`` key as a dict.

    PyYAML's safe_load maps the unquoted ``on:`` top-level key to the
    Python boolean ``True``, not the string ``"on"``. The assertions
    below don't actually probe that key (the triggers themselves are
    not load-bearing for slice-2's structural invariants) so we keep
    the loader simple and let the boolean key sit unmolested.
    """
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def staged_test_yml() -> dict:
    if not STAGED_TEST_YML.exists():
        pytest.skip(
            f"{STAGED_TEST_YML.relative_to(REPO_ROOT)} not present — "
            "slice-2 of #2474 has not landed yet on this branch"
        )
    return _load_yaml(STAGED_TEST_YML)


@pytest.fixture
def staged_test_integration_yml_text() -> str:
    if not STAGED_TEST_INTEGRATION_YML.exists():
        pytest.skip(
            f"{STAGED_TEST_INTEGRATION_YML.relative_to(REPO_ROOT)} not "
            "present — slice-2 of #2474 has not landed yet on this branch"
        )
    return STAGED_TEST_INTEGRATION_YML.read_text(encoding="utf-8")


@pytest.fixture
def staged_test_integration_yml(staged_test_integration_yml_text: str) -> dict:
    return yaml.safe_load(staged_test_integration_yml_text)


class TestStagedTestYmlStructure:
    """Slice-2 task-2-1: ``.github-staging/workflows/test.yml`` end-state."""

    def test_integration_job_exists(self, staged_test_yml: dict) -> None:
        """A new ``integration`` job must be defined as a sibling of unit/security."""
        jobs = staged_test_yml.get("jobs", {})
        assert "integration" in jobs, (
            "missing `integration` job in staged test.yml — slice-2 "
            "task-2-1 acceptance criterion: integration job sibling of "
            "unit/security"
        )
        assert "unit" in jobs and "security" in jobs, (
            "unit/security jobs missing — coder must preserve the existing "
            "test.yml end-state when staging"
        )

    def test_integration_job_uses_reusable_workflow(self, staged_test_yml: dict) -> None:
        """integration.uses must reference the post-mv reusable workflow path."""
        integration = staged_test_yml["jobs"]["integration"]
        uses = integration.get("uses", "")
        assert uses == "./.github/workflows/test-integration.yml", (
            f"jobs.integration.uses={uses!r}; expected "
            "'./.github/workflows/test-integration.yml' (the path AFTER "
            "the human `git mv` — staged path itself is never invoked)"
        )

    def test_integration_job_has_30_minute_timeout(self, staged_test_yml: dict) -> None:
        """integration job must set ``timeout-minutes: 30`` (plan task-2-1 (b))."""
        integration = staged_test_yml["jobs"]["integration"]
        assert integration.get("timeout-minutes") == 30, (
            f"jobs.integration.timeout-minutes={integration.get('timeout-minutes')!r}; "
            "expected 30 (plan task-2-1 (b))"
        )

    def test_aggregate_needs_includes_integration(self, staged_test_yml: dict) -> None:
        """aggregate.needs must contain unit, security, AND integration."""
        aggregate = staged_test_yml["jobs"]["aggregate"]
        needs = aggregate.get("needs", [])
        assert isinstance(needs, list), (
            f"jobs.aggregate.needs is {type(needs).__name__}; expected list"
        )
        assert set(needs) == {"unit", "security", "integration"}, (
            f"jobs.aggregate.needs={needs!r}; expected "
            "['unit', 'security', 'integration'] (any order)"
        )

    def test_aggregate_check_inspects_integration_result(self, staged_test_yml: dict) -> None:
        """aggregate's check_all_passed script must reference needs.integration.result.

        The if-all-passed check currently inspects ``needs.unit.result`` /
        ``needs.security.result``. Slice-2 requires the integration
        result to gate the aggregate's ``passed`` output so a red
        integration tier fails the aggregate.
        """
        aggregate = staged_test_yml["jobs"]["aggregate"]
        steps = aggregate.get("steps", [])
        # Collect every ``run`` block.
        script_text = "\n".join(step.get("run", "") for step in steps if isinstance(step, dict))
        assert "needs.integration.result" in script_text, (
            "aggregate job's check_all_passed script does not inspect "
            "`needs.integration.result` — a red integration tier would "
            "not fail the aggregate (plan task-2-1 (c))"
        )

    def test_workflow_call_output_passed_preserved(self, staged_test_yml: dict) -> None:
        """workflow_call output `passed` must remain so callers don't break.

        PyYAML maps the bare GitHub Actions ``on:`` key to the Python
        bool ``True``; access the trigger block via ``True`` so the
        assertion runs against the actual loaded structure.
        """
        on_block = staged_test_yml.get(True) or staged_test_yml.get("on")
        assert on_block is not None, "missing top-level `on:` block"
        workflow_call = on_block.get("workflow_call")
        assert workflow_call is not None, "workflow_call trigger removed"
        outputs = workflow_call.get("outputs") or {}
        assert "passed" in outputs, (
            "workflow_call output `passed` missing — downstream callers "
            "(e.g. branch protection aggregate) read this output"
        )

    def test_concurrency_block_preserved(self, staged_test_yml: dict) -> None:
        """Existing concurrency block must be unchanged (plan task-2-1 (d))."""
        concurrency = staged_test_yml.get("concurrency") or {}
        assert "group" in concurrency, "concurrency.group removed"
        assert concurrency.get("cancel-in-progress") is True, (
            "concurrency.cancel-in-progress flipped to false — the "
            "existing PR concurrency semantics must be preserved"
        )


class TestStagedTestIntegrationYmlFlakeGuards:
    """Slice-2 task-2-2: ``.github-staging/workflows/test-integration.yml``."""

    def test_image_import_step_has_retry(self, staged_test_integration_yml_text: str) -> None:
        """`Import images into k3s` step must run inside a retry loop.

        HITL Q1: 2-3 attempts with a sleep between attempts to absorb
        transient image-import flakes. We accept any retry shape —
        ``for i in 1 2 3``, ``until``, ``--retry`` flag — but require
        evidence of both a retry loop and the image-import command.
        """
        text = staged_test_integration_yml_text
        assert "Import images into k3s" in text, (
            "image-import step removed from staged test-integration.yml"
        )
        # Cheap heuristic: a `for` / `until` / `retry` token in the
        # neighborhood of the image-import step.
        # We capture the step body via a regex over the YAML text so we
        # don't depend on YAML key ordering.
        step_block_match = re.search(
            r"-\s+name:\s+Import images into k3s.*?(?=\n\s*-\s+name:|\Z)",
            text,
            re.DOTALL,
        )
        assert step_block_match is not None, "could not locate `Import images into k3s` step body"
        step_body = step_block_match.group(0)
        retry_markers = ("for i in", "for attempt", "until ", "while ", "--retry")
        assert any(m in step_body for m in retry_markers), (
            f"`Import images into k3s` step has no retry loop "
            f"(looked for any of {retry_markers!r}) — HITL Q1 flake guard"
        )

    def test_every_kubectl_wait_has_timeout(self, staged_test_integration_yml_text: str) -> None:
        """Every ``kubectl wait`` call must carry an explicit ``--timeout=`` flag."""
        text = staged_test_integration_yml_text
        # Pull every line containing a kubectl wait invocation.
        wait_lines = [
            line.strip()
            for line in text.splitlines()
            if "kubectl" in line and "wait" in line and "--for" in line
        ]
        assert wait_lines, (
            "no `kubectl wait` calls found in staged test-integration.yml — "
            "either the workflow lost its readiness checks or this scan "
            "missed them"
        )
        for line in wait_lines:
            assert "--timeout=" in line, (
                f"`kubectl wait` without `--timeout=` flag: {line!r} — "
                "every wait must be deadline-bounded to defend against "
                "hung-pod flakes (HITL Q1)"
            )

    def test_on_failure_artifact_upload_present(
        self,
        staged_test_integration_yml: dict,
        staged_test_integration_yml_text: str,
    ) -> None:
        """An ``if: failure()`` step must upload a k3s-debug artifact.

        Plan task-2-2 (c): on failure, capture ``kubectl get events
        --all-namespaces -o yaml`` plus pod logs and upload as the
        ``k3s-debug`` artifact via ``actions/upload-artifact@v4``.
        """
        text = staged_test_integration_yml_text
        # We do the substring checks against raw text so we don't have
        # to mind YAML-shape variation between ``- if: failure()`` and
        # block-styled equivalents.
        assert "if: failure()" in text, (
            "no `if: failure()` step in staged test-integration.yml — "
            "HITL Q1 requires an on-failure diagnostic capture"
        )
        assert "kubectl get events --all-namespaces" in text, (
            "on-failure step does not run `kubectl get events "
            "--all-namespaces` — required for HITL Q1 k3s-debug artifact"
        )
        # Pod-log capture: tolerate either explicit `kubectl logs` or a
        # script wrapper. Require evidence of a logs collection.
        assert "kubectl logs" in text, (
            "on-failure step does not capture pod logs via `kubectl logs` — "
            "HITL Q1 requires pod logs in the k3s-debug artifact"
        )
        assert "actions/upload-artifact@v4" in text, (
            "on-failure step does not upload the k3s-debug artifact via "
            "`actions/upload-artifact@v4`"
        )
        assert "k3s-debug" in text, (
            "uploaded artifact is not named `k3s-debug` — plan task-2-2 "
            "(c) requires this exact artifact name so reviewers know "
            "where to look"
        )

    def test_workflow_call_trigger_preserved(self, staged_test_integration_yml: dict) -> None:
        """workflow_call must remain so the parent `test.yml` can invoke it."""
        on_block = staged_test_integration_yml.get(True) or staged_test_integration_yml.get("on")
        assert on_block is not None, "missing top-level `on:` block"
        assert "workflow_call" in on_block, (
            "workflow_call trigger removed — the staged test.yml's "
            "integration job references this as a reusable workflow"
        )

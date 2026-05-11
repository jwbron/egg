"""Structural assertions over the CI `Test` workflow YAMLs.

The repository's PR-level CI is two workflow files:

* ``.github/workflows/test.yml`` — the ``Test`` workflow with
  ``unit`` / ``security`` / ``integration`` jobs and an
  ``aggregate`` job whose rendered check name is the canonical
  ``Test / aggregate`` required-for-merge target.
* ``.github/workflows/test-integration.yml`` — the reusable
  integration workflow invoked by ``test.yml``'s ``integration``
  job. Carries HITL-Q1 flake guards introduced in slice-2 of
  #2474: image-import retry, explicit ``kubectl wait`` timeouts,
  and an on-failure ``k3s-debug`` artifact capturing
  ``kubectl get events --all-namespaces`` and pod logs.

These tests guard the structural invariants in perpetuity.
During slice-2 of #2474 the files live under
``.github-staging/workflows/`` (the coder role is gateway-blocked
from ``.github/``, so the human reviewer performs the
``git mv .github-staging/workflows/*.yml .github/workflows/``
before merging slice-2's PR). The path resolver below prefers the
staged copies when present so the same assertions cover both the
pre-merge staging state AND the post-merge production state — and
a future regression that drops the integration job from
``.github/workflows/test.yml`` is caught by the same suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STAGED_DIR = REPO_ROOT / ".github-staging" / "workflows"
_FINAL_DIR = REPO_ROOT / ".github" / "workflows"


def _resolve_workflow(filename: str) -> Path | None:
    """Resolve a workflow file, preferring the staged copy.

    Returns the path to ``.github-staging/workflows/<filename>`` if it
    exists (slice-2 pre-merge state), else
    ``.github/workflows/<filename>`` (post-merge production state),
    else ``None`` so the fixture can skip cleanly.
    """
    staged = _STAGED_DIR / filename
    if staged.exists():
        return staged
    final = _FINAL_DIR / filename
    if final.exists():
        return final
    return None


def _load_yaml(path: Path) -> dict:
    """Parse a YAML file, leaving GitHub Actions' bare ``on:`` key as bool True.

    PyYAML's safe_load maps the unquoted ``on:`` top-level key to the
    Python boolean ``True``, not the string ``"on"``. Callers that
    need the trigger block use ``data.get(True) or data.get("on")``.
    """
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def test_yml_path() -> Path:
    resolved = _resolve_workflow("test.yml")
    if resolved is None:
        pytest.skip(
            "neither .github-staging/workflows/test.yml nor "
            ".github/workflows/test.yml found — repo is in an "
            "unexpected state"
        )
    return resolved


@pytest.fixture
def test_yml(test_yml_path: Path) -> dict:
    return _load_yaml(test_yml_path)


@pytest.fixture
def test_integration_yml_path() -> Path:
    resolved = _resolve_workflow("test-integration.yml")
    if resolved is None:
        pytest.skip(
            "neither .github-staging/workflows/test-integration.yml nor "
            ".github/workflows/test-integration.yml found — repo is in "
            "an unexpected state"
        )
    return resolved


@pytest.fixture
def test_integration_yml_text(test_integration_yml_path: Path) -> str:
    return test_integration_yml_path.read_text(encoding="utf-8")


@pytest.fixture
def test_integration_yml(test_integration_yml_text: str) -> dict:
    return yaml.safe_load(test_integration_yml_text)


class TestTestYmlStructure:
    """Invariants over ``test.yml`` (staged or final).

    Originally introduced as slice-2 task-2-1 acceptance scaffolding
    for #2474; promoted to perpetual coverage so the post-merge
    ``.github/workflows/test.yml`` is guarded by the same suite.
    """

    def test_integration_job_exists(self, test_yml: dict) -> None:
        """A new ``integration`` job must be defined as a sibling of unit/security."""
        jobs = test_yml.get("jobs", {})
        assert "integration" in jobs, (
            "missing `integration` job in test.yml — slice-2 task-2-1 "
            "acceptance criterion: integration job sibling of "
            "unit/security"
        )
        assert "unit" in jobs and "security" in jobs, (
            "unit/security jobs missing — these are the historic "
            "siblings of the new integration job"
        )

    def test_integration_job_uses_reusable_workflow(self, test_yml: dict) -> None:
        """integration.uses must reference the reusable workflow path."""
        integration = test_yml["jobs"]["integration"]
        uses = integration.get("uses", "")
        assert uses == "./.github/workflows/test-integration.yml", (
            f"jobs.integration.uses={uses!r}; expected "
            "'./.github/workflows/test-integration.yml' — the path "
            "must reference the post-mv production location, never "
            "`.github-staging/...` (the staged path is never invoked)"
        )

    def test_integration_job_has_30_minute_timeout(self, test_yml: dict) -> None:
        """integration job must set ``timeout-minutes: 30`` (plan task-2-1 (b))."""
        integration = test_yml["jobs"]["integration"]
        assert integration.get("timeout-minutes") == 30, (
            f"jobs.integration.timeout-minutes={integration.get('timeout-minutes')!r}; "
            "expected 30 (plan task-2-1 (b))"
        )

    def test_aggregate_needs_includes_integration(self, test_yml: dict) -> None:
        """aggregate.needs must contain unit, security, AND integration."""
        aggregate = test_yml["jobs"]["aggregate"]
        needs = aggregate.get("needs", [])
        assert isinstance(needs, list), (
            f"jobs.aggregate.needs is {type(needs).__name__}; expected list"
        )
        assert set(needs) == {"unit", "security", "integration"}, (
            f"jobs.aggregate.needs={needs!r}; expected "
            "['unit', 'security', 'integration'] (any order)"
        )

    def test_aggregate_fails_on_red_tier(self, test_yml: dict) -> None:
        """aggregate's check_all_passed script must FAIL the job on a red tier.

        Verifies two things, since one without the other is a
        non-functional gate:

        1. The script inspects ``needs.integration.result`` (so a red
           integration tier reaches the failure branch at all).
        2. The failure branch terminates with ``exit 1`` (so the step
           — and therefore the aggregate job, and therefore the
           canonical ``Test / aggregate`` required-for-merge check —
           actually reports failure to GitHub).

        Without the ``exit 1``, the failure branch falls through with
        a zero exit code under the default GitHub Actions
        ``bash -eo pipefail`` shell, the step reports success, the
        aggregate job reports success, and a PR with a red tier
        merges through the required check. Slice-2 of #2474 makes
        ``Test / aggregate`` the canonical required-for-merge gate;
        if the gate doesn't gate, that's the whole point of the
        slice gone.

        Additionally constrains the ``exit 1`` to appear AFTER the
        failure-branch markers but BEFORE the ``else`` keyword — a
        future regression that moved ``exit 1`` into the success
        branch (or removed it entirely) is caught.
        """
        aggregate = test_yml["jobs"]["aggregate"]
        steps = aggregate.get("steps", [])
        script_text = "\n".join(step.get("run", "") for step in steps if isinstance(step, dict))
        assert "needs.integration.result" in script_text, (
            "aggregate job's check_all_passed script does not inspect "
            "`needs.integration.result` — a red integration tier would "
            "not fail the aggregate (plan task-2-1 (c))"
        )
        # The failure branch must terminate the script with a non-zero
        # exit. Locate it by matching from a failure-branch marker
        # (the "Some tests failed" echo) to the ``else`` keyword and
        # require ``exit 1`` between them.
        failure_branch_match = re.search(
            r"echo\s+\"Some tests failed\".*?(?=\belse\b)",
            script_text,
            re.DOTALL,
        )
        assert failure_branch_match is not None, (
            "could not locate aggregate's failure branch — expected "
            '`echo "Some tests failed"` followed by an `else` branch'
        )
        failure_branch = failure_branch_match.group(0)
        assert "exit 1" in failure_branch, (
            "aggregate job's failure branch does not call `exit 1` — "
            "the script falls through with a zero exit code under "
            "`bash -eo pipefail` and the aggregate job reports success "
            "even when a tier was red, defeating the canonical "
            "`Test / aggregate` required-for-merge check"
        )

    def test_workflow_call_output_passed_preserved(self, test_yml: dict) -> None:
        """workflow_call output `passed` must remain so callers don't break.

        PyYAML maps the bare GitHub Actions ``on:`` key to the Python
        bool ``True``; access the trigger block via ``True`` so the
        assertion runs against the actual loaded structure.
        """
        on_block = test_yml.get(True) or test_yml.get("on")
        assert on_block is not None, "missing top-level `on:` block"
        workflow_call = on_block.get("workflow_call")
        assert workflow_call is not None, "workflow_call trigger removed"
        outputs = workflow_call.get("outputs") or {}
        assert "passed" in outputs, (
            "workflow_call output `passed` missing — downstream callers "
            "(e.g. branch protection aggregate) read this output"
        )

    def test_concurrency_block_preserved(self, test_yml: dict) -> None:
        """Existing concurrency block must keep PR-scoped semantics.

        The historical group is ``test-${{ github.head_ref ||
        github.ref }}`` — one in-flight run per PR (head_ref) and one
        per branch (ref) for non-PR triggers, with mid-flight
        cancellation on a new push. A future regression that flipped
        ``group`` to e.g. ``test-${{ github.run_id }}`` would give
        every run a unique group, break PR concurrency entirely, and
        an assertion of just ``"group" in concurrency`` would not
        catch it. Require the group to actually reference
        ``github.head_ref`` so the PR-scoping invariant is held.
        """
        concurrency = test_yml.get("concurrency") or {}
        group = concurrency.get("group")
        assert group, "concurrency.group removed"
        assert "github.head_ref" in group, (
            f"concurrency.group={group!r}; expected the group expression "
            "to reference `github.head_ref` so concurrency is scoped "
            "per PR. A group keyed on `github.run_id` or similar would "
            "give every run a unique key and silently disable PR "
            "concurrency."
        )
        assert concurrency.get("cancel-in-progress") is True, (
            "concurrency.cancel-in-progress flipped to false — the "
            "existing PR concurrency semantics must be preserved"
        )


class TestTestIntegrationYmlFlakeGuards:
    """Invariants over ``test-integration.yml`` (staged or final).

    Originally introduced as slice-2 task-2-2 acceptance scaffolding
    for #2474; promoted to perpetual coverage so the HITL-Q1 flake
    guards on the post-merge ``.github/workflows/test-integration.yml``
    cannot silently regress.
    """

    def test_image_import_step_has_retry(self, test_integration_yml_text: str) -> None:
        """`Import images into k3s` step must run inside a retry loop.

        HITL Q1: 2-3 attempts with a sleep between attempts to absorb
        transient image-import flakes. We accept any retry shape —
        ``for i in 1 2 3``, ``until``, ``--retry`` flag — but require
        evidence of both a retry loop and the image-import command.
        """
        text = test_integration_yml_text
        assert "Import images into k3s" in text, (
            "image-import step removed from test-integration.yml"
        )
        # Cheap heuristic: a `for` / `until` / `retry` token in the
        # neighborhood of the image-import step. Capture the step
        # body via a regex over the YAML text so we don't depend on
        # YAML key ordering.
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

    def test_every_kubectl_wait_has_timeout(self, test_integration_yml_text: str) -> None:
        """Every ``kubectl wait`` call must carry an explicit ``--timeout=`` flag."""
        text = test_integration_yml_text
        wait_lines = [
            line.strip()
            for line in text.splitlines()
            if "kubectl" in line and "wait" in line and "--for" in line
        ]
        assert wait_lines, (
            "no `kubectl wait` calls found in test-integration.yml — "
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
        test_integration_yml: dict,
        test_integration_yml_text: str,
    ) -> None:
        """An ``if: failure()`` step must upload a k3s-debug artifact.

        Plan task-2-2 (c): on failure, capture ``kubectl get events
        --all-namespaces -o yaml`` plus pod logs and upload as the
        ``k3s-debug`` artifact via ``actions/upload-artifact@v4``.
        """
        text = test_integration_yml_text
        assert "if: failure()" in text, (
            "no `if: failure()` step in test-integration.yml — "
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

    def test_workflow_call_trigger_preserved(self, test_integration_yml: dict) -> None:
        """workflow_call must remain so the parent `test.yml` can invoke it."""
        on_block = test_integration_yml.get(True) or test_integration_yml.get("on")
        assert on_block is not None, "missing top-level `on:` block"
        assert "workflow_call" in on_block, (
            "workflow_call trigger removed — test.yml's integration "
            "job references this as a reusable workflow"
        )

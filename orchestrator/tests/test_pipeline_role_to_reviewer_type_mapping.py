"""Pitfall-1 + Pitfall-4 guards from issue #1965 / TASK-1-3 (c, d).

Pitfall 1: ``orchestrator/routes/pipelines.py`` already maps
``reviewer_<name>`` → ``<name with hyphens>`` via the single line

    reviewer_type = role_value.replace("reviewer_", "", 1).replace("_", "-")

Adding a redundant dict / if-elif chain near that line shadows the
one-liner. The plan forbids that. This test fails with a clear message
if a future PR introduces such a redundant mapping.

Pitfall 4: ``REVIEWER_ATTESTATION_MODELS`` (in
``orchestrator/attestation_schemas.py``) must NOT register the new
``reviewer_security`` / ``reviewer_concurrency`` roles. The lens
reviewers ship with no attestation payload — same shape as
``reviewer_plan``, ``reviewer_refine``, ``reviewer_agent_design``.
``validate_attestation`` is conditional on ``review.attestation`` being
truthy so attestation-less roles work fine.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Pipelines.py imports docker at module load; stub it out the same way
# test_pipeline_prompts.py does so the mapping helpers are importable
# in unit-test contexts without a Docker daemon.
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

PIPELINES_PATH = Path(__file__).resolve().parent.parent / "routes" / "pipelines.py"


def _role_value_to_reviewer_type(role_value: str) -> str:
    """Replicate the mapping in ``orchestrator/routes/pipelines.py``.

    The plan calls out the literal one-liner

        reviewer_type = role_value.replace("reviewer_", "", 1).replace("_", "-")

    as the only mapping. This helper mirrors it so tests can assert the
    invariant without having to import the closure in pipelines.py.
    """
    return role_value.replace("reviewer_", "", 1).replace("_", "-")


class TestRoleValueToReviewerTypeMappingInvariant:
    """The single-line mapping covers both new role names without a dict."""

    @pytest.mark.parametrize(
        "role_value, expected_type",
        [
            ("reviewer_code", "code"),
            ("reviewer_contract", "contract"),
            ("reviewer_agent_design", "agent-design"),
            ("reviewer_refine", "refine"),
            ("reviewer_plan", "plan"),
            # The two new lens reviewers — pitfall-1 guard. The current
            # one-liner already produces the right reviewer_type for
            # both without any new dict / if-elif chain.
            ("reviewer_security", "security"),
            ("reviewer_concurrency", "concurrency"),
        ],
    )
    def test_one_liner_maps_role_to_reviewer_type(
        self, role_value: str, expected_type: str
    ) -> None:
        assert _role_value_to_reviewer_type(role_value) == expected_type


class TestNoRedundantMappingDictNearLine:
    """Pitfall-1 guard: forbid a redundant dict / if-elif chain near the mapping.

    The plan's wording: "Do not add a dict or if/elif chain. Make this a
    verification + unit-test task only." This test scans
    ``orchestrator/routes/pipelines.py`` for any newly added mapping
    that would shadow the one-liner. Specifically:

    - No literal dict like ``{"reviewer_security": "security", ...}``
      mapping the new role names to reviewer types.
    - No ``if role_value == "reviewer_security": reviewer_type = "security"``
      style chain.

    If such a redundant mapping is introduced the test fails with a
    clear pointer at the offending file/line.
    """

    def setup_method(self) -> None:
        self.source = PIPELINES_PATH.read_text(encoding="utf-8")

    def test_no_explicit_dict_for_new_role_names(self) -> None:
        # Reject any dict literal that maps a new role-name string key to
        # the bare reviewer-type string — that would shadow the one-liner.
        for role, reviewer_type in [
            ("reviewer_security", "security"),
            ("reviewer_concurrency", "concurrency"),
        ]:
            pattern = rf'["\']{role}["\']\s*:\s*["\']{reviewer_type}["\']'
            offending = re.findall(pattern, self.source)
            assert not offending, (
                f"Pitfall-1: a redundant dict mapping {role!r} → "
                f"{reviewer_type!r} was added to pipelines.py. The "
                "single line "
                '`reviewer_type = role_value.replace("reviewer_", "", 1)'
                '.replace("_", "-")` already covers it. Remove the '
                "redundant entry."
            )

    def test_no_if_elif_chain_for_new_role_names(self) -> None:
        for role, reviewer_type in [
            ("reviewer_security", "security"),
            ("reviewer_concurrency", "concurrency"),
        ]:
            # An if/elif of the form
            # `if role_value == "reviewer_security": reviewer_type = "security"`
            pattern = (
                rf'role_value\s*==\s*["\']{role}["\'][^\n]*\n[^\n]*reviewer_type\s*='
                rf'\s*["\']{reviewer_type}["\']'
            )
            offending = re.findall(pattern, self.source)
            assert not offending, (
                f"Pitfall-1: an if/elif chain mapping {role!r} → "
                f"{reviewer_type!r} shadows the one-liner. Remove it."
            )

    def test_one_liner_present(self) -> None:
        """The canonical one-liner stays in pipelines.py."""
        assert 'role_value.replace("reviewer_", "", 1).replace("_", "-")' in self.source, (
            "The canonical role-name → reviewer-type mapping line was "
            "removed from pipelines.py. Restoring it is the only "
            "supported way to register new reviewer roles."
        )


class TestNoAttestationModelsForNewLensReviewers:
    """Pitfall-4: ``REVIEWER_ATTESTATION_MODELS`` does NOT include either lens role."""

    def test_reviewer_security_has_no_attestation_model(self) -> None:
        from attestation_schemas import REVIEWER_ATTESTATION_MODELS

        assert "reviewer_security" not in REVIEWER_ATTESTATION_MODELS, (
            "Pitfall-4: reviewer_security must NOT have an attestation "
            "model. Existing attestation-less reviewers (reviewer_plan, "
            "reviewer_refine, reviewer_agent_design) work fine because "
            "validate_attestation is conditional on review.attestation "
            "being truthy. Adding a model breaks day-1 ACK shape parity."
        )

    def test_reviewer_concurrency_has_no_attestation_model(self) -> None:
        from attestation_schemas import REVIEWER_ATTESTATION_MODELS

        assert "reviewer_concurrency" not in REVIEWER_ATTESTATION_MODELS, (
            "Pitfall-4: reviewer_concurrency must NOT have an attestation "
            "model. See test_reviewer_security_has_no_attestation_model "
            "for rationale."
        )

    def test_reviewer_code_holistic_has_no_attestation_model(self) -> None:
        """Issue #2126: ``reviewer_code_holistic`` intentionally has no model.

        The holistic reviewer follows the same attestation-less pattern as
        the lens reviewers (``reviewer_security`` / ``reviewer_concurrency``)
        and the other generalists (``reviewer_plan``, ``reviewer_refine``,
        ``reviewer_agent_design``). The default empty-attestation path
        works because ``validate_attestation`` only fires when
        ``review.attestation`` is truthy. This explicit guard prevents a
        future contributor from adding a partial ``ReviewerCodeAttestation``
        copy without designing a holistic-specific schema first (e.g.
        passes_run, findings_per_pass).
        """
        from attestation_schemas import REVIEWER_ATTESTATION_MODELS

        assert "reviewer_code_holistic" not in REVIEWER_ATTESTATION_MODELS, (
            "Pitfall-4: reviewer_code_holistic must NOT have an attestation "
            "model. The holistic ACK shape intentionally diverges from "
            "reviewer_code's slice attestation (files_reviewed / "
            "issues_found) — silently registering ReviewerCodeAttestation "
            "for the holistic role would force the wrong schema. If a "
            "schema is needed, design a ReviewerCodeHolisticAttestation "
            "around the four holistic passes and update this guard."
        )

    def test_existing_attestation_models_unchanged(self) -> None:
        """Sanity check that the existing models stay registered.

        Catches a mistaken refactor that drops other reviewer attestations
        while wiring the new roles.
        """
        from attestation_schemas import REVIEWER_ATTESTATION_MODELS

        assert "reviewer_code" in REVIEWER_ATTESTATION_MODELS
        assert "reviewer_contract" in REVIEWER_ATTESTATION_MODELS
        assert "tester" in REVIEWER_ATTESTATION_MODELS

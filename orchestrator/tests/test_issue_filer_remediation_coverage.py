"""Regression guard: every detector finding_class has a remediation entry.

The slice-8 detector reconcile (#2270 §5) renamed the ``finding_class`` strings
the tier1 / detection-plane detectors emit but, in an earlier cut, left
``orchestrator/overseer/issue_filer.py``'s ``FINDING_CLASS_REMEDIATIONS`` keyed
on the old, dead names — so ``remediation_for_finding_class()`` silently fell
through to ``_DEFAULT_REMEDIATION`` for ~10 production classes. The calibration
corpus bridges detectors by ``detector_key`` and never exercises issue_filer, so
the green corpus masked the regression (both reviewer_code and reviewer_contract
flagged exactly this).

This test closes that gap: it discovers every ``finding_class`` string the
detectors actually emit by statically scanning the live detector source, and
asserts each one is a key in ``FINDING_CLASS_REMEDIATIONS`` (and that the map
carries no orphan keys no detector will ever emit). It reads the live source, so
it self-maintains: a future rename that forgets issue_filer fails here loudly.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirror the sibling overseer tests)
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from health_checks.types import FindingClass
    from overseer.issue_filer import (
        FINDING_CLASS_REMEDIATIONS,
        remediation_for_finding_class,
    )
except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover
    pytest.skip(
        f"overseer.issue_filer / health_checks not available yet: {exc}",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Static discovery of every emitted finding_class string
# ---------------------------------------------------------------------------

_HEALTH_CHECKS_DIR = _orchestrator_path / "health_checks"
_SELF_MONITOR = _orchestrator_path / "overseer" / "self_monitor.py"


def _detector_source_files() -> list[Path]:
    files = sorted(_HEALTH_CHECKS_DIR.rglob("*.py"))
    if _SELF_MONITOR.exists():
        files.append(_SELF_MONITOR)
    return files


def _emitted_finding_classes() -> set[str]:
    """Resolve every ``finding_class=`` value the detector source emits.

    Handles the three shapes the detectors use:
      * a bare string literal  -> ``finding_class="hitl_queue_backlog"``
      * a module ``FINDING_*`` constant -> resolved via its ``= "..."`` literal
      * a ``FindingClass.MEMBER`` attribute -> resolved via the StrEnum value
    """
    emitted: set[str] = set()
    const_values: dict[str, str] = {}

    trees: list[ast.AST] = []
    # First pass: collect ``FINDING_X = "literal"`` constants across all files.
    for path in _detector_source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        trees.append(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if not isinstance(node.value.value, str):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.startswith("FINDING_"):
                        const_values[target.id] = node.value.value

    # Second pass: resolve every ``finding_class=<expr>`` keyword.
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "finding_class":
                    continue
                value = kw.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    emitted.add(value.value)
                elif isinstance(value, ast.Name) and value.id in const_values:
                    emitted.add(const_values[value.id])
                elif (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id == "FindingClass"
                ):
                    emitted.add(str(getattr(FindingClass, value.attr)))
    return emitted


def test_every_emitted_finding_class_has_a_remediation() -> None:
    emitted = _emitted_finding_classes()
    # Sanity: discovery actually found the detector classes (guards a broken scan
    # silently passing). The reconciled survey carries well over a dozen classes.
    assert len(emitted) >= 15, f"finding_class discovery looks broken: {sorted(emitted)}"

    missing = sorted(c for c in emitted if c not in FINDING_CLASS_REMEDIATIONS)
    assert not missing, (
        "Detectors emit finding_class strings with no FINDING_CLASS_REMEDIATIONS "
        f"entry (they fall through to the generic fallback): {missing}"
    )


def test_no_orphan_remediation_keys() -> None:
    """Every remediation key maps to a class some detector actually emits."""
    emitted = _emitted_finding_classes()
    orphans = sorted(k for k in FINDING_CLASS_REMEDIATIONS if k not in emitted)
    assert not orphans, (
        "FINDING_CLASS_REMEDIATIONS has keys no detector emits (dead/renamed "
        f"entries to remove or re-key): {orphans}"
    )


def test_remediation_lookup_returns_specific_text_not_default() -> None:
    """The reconciled/new classes resolve to actionable, class-specific text."""
    for finding_class in (
        "brc_thrash",
        "container_restart_loop",
        "cost_anomaly",
        "gateway_repeated_denial",
        "llm_substrate_unreachable",
        "runtime_thread_dead",
        "anthropic_5xx",
        "container_oom_evicted",
        "hitl_queue_backlog",
        "overseer_self_health",
    ):
        remediation = remediation_for_finding_class(finding_class)
        assert remediation, finding_class
        assert remediation != "Investigate the agent logs and pipeline state", (
            f"{finding_class} fell through to the generic default remediation"
        )

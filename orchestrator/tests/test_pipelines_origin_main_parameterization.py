"""Tests for ``_resolve_origin_ref`` helper and its call-site parameterization.

Regression-lock for #1748 ("parameterize origin/main behind PR base ref
helper"). The helper centralises the ``origin/<base_branch>`` resolution so
every orient-prompt / diff-command call site honours the resolved base
branch instead of hardcoding ``"origin/main"``.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

import pytest
from routes import pipelines as pipelines_module
from routes.pipelines import (
    _build_producer_orientation,
    _build_reviewer_preparation,
    _resolve_origin_ref,
)


class TestResolveOriginRef:
    """Direct unit tests for the ``_resolve_origin_ref`` helper."""

    @pytest.mark.parametrize(
        "base_branch,expected",
        [
            ("main", "origin/main"),
            ("develop", "origin/develop"),
            ("release-2026-04", "origin/release-2026-04"),
        ],
    )
    def test_plain_branch_names_are_prefixed(self, base_branch: str, expected: str) -> None:
        """A bare branch name gets the ``origin/`` prefix."""
        assert _resolve_origin_ref(base_branch) == expected

    def test_none_falls_back_to_origin_main(self) -> None:
        """``None`` falls back to the ``origin/main`` default."""
        assert _resolve_origin_ref(None) == "origin/main"

    def test_empty_string_falls_back_to_origin_main(self) -> None:
        """An empty string falls back to the ``origin/main`` default."""
        assert _resolve_origin_ref("") == "origin/main"

    def test_whitespace_only_falls_back_to_origin_main(self) -> None:
        """A whitespace-only string falls back to the ``origin/main`` default."""
        assert _resolve_origin_ref("   ") == "origin/main"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        """Leading/trailing whitespace is stripped before prefixing."""
        assert _resolve_origin_ref("  develop  ") == "origin/develop"

    @pytest.mark.parametrize(
        "already_prefixed",
        ["origin/develop", "origin/main", "origin/release-2026-04"],
    )
    def test_already_prefixed_is_idempotent(self, already_prefixed: str) -> None:
        """An input already prefixed with ``origin/`` is returned unchanged."""
        assert _resolve_origin_ref(already_prefixed) == already_prefixed


class TestParameterizationCallSites:
    """Regression-lock: the helper must be wired into multiple call sites."""

    def test_helper_has_multiple_call_sites(self) -> None:
        """Verify ``_resolve_origin_ref(`` is invoked at 5+ call sites.

        The parameterization in #1748 replaced hardcoded ``"origin/main"``
        literals at multiple sites (producer orient prompts, reviewer prep,
        diff commands, recovery paths). If this count drops below 5, the
        parameterization has likely been partially reverted.
        """
        source = inspect.getsource(pipelines_module)
        # Count invocations, NOT the def line.
        invocation_count = source.count("_resolve_origin_ref(")
        # Definition (``def _resolve_origin_ref(``) contributes 1 match;
        # subtract it to get the count of call sites.
        definition_count = source.count("def _resolve_origin_ref(")
        call_site_count = invocation_count - definition_count
        assert call_site_count >= 5, (
            f"Expected at least 5 call sites to _resolve_origin_ref, "
            f"found {call_site_count}. Parameterization may have been "
            f"reverted to hardcoded 'origin/main' literals."
        )

    def test_reviewer_preparation_uses_helper_not_literal(self) -> None:
        """``_build_reviewer_preparation`` must not contain a hardcoded
        ``"origin/main"`` literal — it must go through ``_resolve_origin_ref``.
        """
        source = inspect.getsource(_build_reviewer_preparation)
        assert '"origin/main"' not in source, (
            "_build_reviewer_preparation contains a hardcoded "
            '"origin/main" literal; it must call _resolve_origin_ref instead.'
        )

    def test_producer_orientation_uses_helper_not_literal(self) -> None:
        """``_build_producer_orientation`` must not contain a hardcoded
        ``"origin/main"`` literal — it must go through ``_resolve_origin_ref``.
        """
        source = inspect.getsource(_build_producer_orientation)
        assert '"origin/main"' not in source, (
            "_build_producer_orientation contains a hardcoded "
            '"origin/main" literal; it must call _resolve_origin_ref instead.'
        )


class TestRegressionLockNoLiteralInDiffCommands:
    """Regression-lock via source inspection on the two prep-prompt builders."""

    @pytest.mark.parametrize(
        "func,func_name",
        [
            (_build_reviewer_preparation, "_build_reviewer_preparation"),
            (_build_producer_orientation, "_build_producer_orientation"),
        ],
    )
    def test_function_has_no_hardcoded_origin_main(self, func, func_name: str) -> None:
        """Verify no hardcoded ``"origin/main"`` literal appears in the
        prep-prompt builder functions. The only way ``origin/main`` should
        enter these prompts is as the runtime fallback within
        ``_resolve_origin_ref``.
        """
        source = inspect.getsource(func)
        assert '"origin/main"' not in source, (
            f"{func_name} contains a hardcoded 'origin/main' literal. "
            f"All base-ref resolution should flow through _resolve_origin_ref."
        )
        # Also check the single-quoted form for good measure.
        assert "'origin/main'" not in source, (
            f"{func_name} contains a hardcoded 'origin/main' literal "
            f"(single-quoted). All base-ref resolution should flow through "
            f"_resolve_origin_ref."
        )

    def test_function_invokes_resolve_origin_ref(self) -> None:
        """Verify ``_build_reviewer_preparation`` actually calls
        ``_resolve_origin_ref`` (positive check complementing the
        no-literal regression lock).
        """
        source = inspect.getsource(_build_reviewer_preparation)
        assert "_resolve_origin_ref(" in source, (
            "_build_reviewer_preparation does not invoke _resolve_origin_ref; "
            "base-ref resolution may have been bypassed."
        )
